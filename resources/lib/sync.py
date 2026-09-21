"""Synchronizace zhlédnuto / rozkoukané / Můj seznam mezi více Kodi přes Home Assistant.

HA je střed: integrace Nokturno drží sloučený stav a každé Kodi si s ní jednou za
čas vymění změny. Bez HA jede doplněk dál z vlastních souborů a dorovná se, až
bude HA zpátky. Stejný modul běží na obou stranách (Kodi doplněk i integrace),
proto tu nejsou žádné závislosti na Kodi ani na HA — jen `Store`.

Jeden výměnný krok je POST na `/api/nokturno/sync`:

    {"device": "Obývák", "since": <čas HA z minula>,
     "changes": {"watched": {klíč: záznam}, "favlog": {klíč: {"on", "ts"}}, "items": {klíč: snímek},
                 "next_hidden": {seriál: {"ep", "ts"}}}}

a odpověď má stejný tvar plus `"now"` (čas HA). `since` je vždy čas HA, ne
místní — jinak by se rozešly hodiny dvou boxů. U každého titulu vyhrává novější
záznam (`ts`); Můj seznam se nesynchronizuje jako seznam, ale jako deník
zapnuto/vypnuto s časem (`favlog`), aby šlo přenést i odebrání.
"""
import json
import time
import urllib.error
import urllib.request

from store import ITEMS_MAX, WATCHED_MAX

STATE = "sync"          # sync.json v profilu: {"since", "last_ok", "last_error", "pushed", "pulled"}

# Okruhy: co se z celkového stavu posílá a přijímá. Uživatel si je zapíná zvlášť
# a platí pro obě střediska stejně — přes Home Assistant i přes slepý relay
# (`syncbox.py`, který je odsud importuje).
CIRCLES = {
    "watched": ("watched", "next_hidden"),   # zhlédnuto, rozkoukanost, skryté další díly
    "favourites": ("favlog",),               # Můj seznam jako deník zapnuto/vypnuto
    "history": ("histlog",),                 # historie hledání
    # volby doplňku a přihlášení ke zdrojům (`setsync.py`). Nejsou ve `Store`,
    # takže je `collect_changes` nesbírá — plní je hostitel přes `syncbox`
    # a přes Home Assistant nechodí vůbec.
    "settings": ("setlog",),
    "accounts": ("acclog",),
}
# Nastavení ani účty ve výchozím stavu nejdou — sdílení přihlášení má být vědomé.
DEFAULT_CIRCLES = ("watched", "favourites", "history")
# Snímky titulů jdou vždy k tomu, co se posílá — bez nich by druhá strana
# neuměla položku vykreslit. `collect_changes` je omezuje na dotčené klíče.
SNAPSHOTS = "items"
RECENT_SNAPSHOTS = 50   # kolik nejnovějších zhlédnutých nese snímek do synchronizace
# ...ale jen na ty, kde snímek opravdu chybět nesmí: Můj seznam a rozkoukané.
# Dokoukaný titul si druhá strana dohledá sama (`recover_snapshot`, hubený snímek),
# kdežto snímek váží asi 1,3 kB a `items` jich drží až `ITEMS_MAX`. Měřeno na
# skutečném profilu: 500 titulů se snímky = 177 kB blob, tedy **přes** `MAX_BLOB`
# (128 kB) — synchronizace by od pár set zhlédnutých titulů přestala fungovat úplně,
# ne zpomalit. Se stropem drží plný profil (5000 zhlédnutých) pod 40 kB.
SNAPSHOT_MAX = 300
ENDPOINT = "/api/nokturno/sync"
TIMEOUT = 20


def _ts(rec):
    try:
        return int((rec or {}).get("ts") or 0)
    except (TypeError, ValueError):
        return 0


def _seen(rec):
    """Kdy záznam dorazil na střed (`rts`, razí ho jen HA), jinak kdy vznikl.
    Filtr `since` musí jít podle příjmu: `ts` je čas změny na zařízení, takže
    záznam poslaný se zpožděním by jinak druhé Kodi, které se mezitím synchronizovalo,
    nikdy nedostalo."""
    return max(_ts(rec), int((rec or {}).get("rts") or 0) if isinstance(rec, dict) else 0)


def _stamped(rec, now):
    """Kopie záznamu s časem příjmu (`now`) nebo bez něj (`now` None — zařízení `rts` nenosí)."""
    out = {k: v for k, v in rec.items() if k != "rts"}
    if now:
        out["rts"] = now
    return out


def _keep_playcount(rec, mine):
    """Počet zhlédnutí je nejvyšší z obou stran, zbytek rozhodne novější záznam.

    Rozkoukanost je jediný údaj, který si dvě televize u téhož titulu přepisují,
    a tam last-write-wins dává smysl — poslední pozice je ta, kde se opravdu
    skončilo. Příznak „tohle už jsem viděl" se ale takhle ztratit nesmí: kdo
    film dokoukal na jedné TV a na druhé ho pak pustil znovu, přijde o jeho
    označení ve výpisech i v Traktu. Proto se `playcount` slévá maximem.
    """
    muj = int((mine or {}).get("playcount") or 0)
    if muj <= int((rec or {}).get("playcount") or 0):
        return rec
    return dict(rec, playcount=muj)


def _backfill_favlog(store):
    """Oblíbené přidané předtím, než tenhle deník vůbec existoval (staré verze
    doplňku bez synchronizace), v něm chybí — bez záznamu se nikdy neodešlou,
    protože `collect_changes` čte jen `favlog`, ne `favourites` samotné.
    Doplní se jednou s aktuálním časem, ať se při nejbližší výměně pošlou taky."""
    favs = store.reload("favourites", [])
    log = store.reload("favlog", {})
    missing = [k for k in favs if k not in log]
    if not missing:
        return
    now = int(time.time())
    for k in missing:
        log[k] = {"on": True, "ts": now}
    store.save("favlog", log)


def collect_changes(store, since):
    """Co se tu změnilo od `since` — ke změněným klíčům i snímky titulů, aby
    druhá strana uměla položku vykreslit bez dotazu na síť."""
    _backfill_favlog(store)
    # >= schválně: `since` i `ts` jsou celé sekundy, takže změna zapsaná v téže
    # sekundě jako minulá výměna by se s ostrým > už nikdy neposlala. Dvojí
    # poslání nevadí — příjemce bere jen přísně novější záznam.
    watched = {k: v for k, v in store.reload("watched", {}).items() if _seen(v) >= since}
    favlog = {k: v for k, v in store.reload("favlog", {}).items() if _seen(v) >= since}
    histlog = {k: v for k, v in store.reload("histlog", {}).items() if _seen(v) >= since}
    # skryté „Další díly“ — záznamy starých doplňků bez času (jen řetězec) se neposílají
    next_hidden = {k: v for k, v in store.reload("next_hidden", {}).items()
                   if isinstance(v, dict) and _seen(v) >= since}
    items = store.reload("items", {})
    return {"watched": watched, "favlog": favlog, "histlog": histlog, "next_hidden": next_hidden,
            "items": _snapshots(items, watched, favlog)}


def _snapshots(items, watched, favlog):
    """Snímky jen k tomu, co se bez nich nevykreslí — Můj seznam a rozkoukané.

    Seřazeno od nejčerstvějšího a useknuto na `SNAPSHOT_MAX`; u dvou zařízení
    se stejnou skupinou se tím přenese to, co má uživatel rozkoukané teď, ne
    archiv z loňska. Zbytek dohledá příjemce sám.
    """
    def rozkoukany(rec):
        try:
            return float((rec or {}).get("resume") or 0) > 0
        except (TypeError, ValueError):
            return False

    keys = {k for k, v in watched.items() if rozkoukany(v)}
    # nejnovější zhlédnuté (menu „Naposledy") — příjemce jinak dostane jen záznam
    # bez snímku a titul si musí dohledávat po jednom přes síť
    zhlednute = sorted((k for k, v in watched.items() if isinstance(v, dict) and v.get("playcount")),
                       key=lambda k: _seen(watched[k]), reverse=True)
    keys |= set(zhlednute[:RECENT_SNAPSHOTS])
    keys |= {k for k, v in favlog.items() if isinstance(v, dict) and v.get("on")}
    keys = [k for k in keys if k in items]
    if len(keys) > SNAPSHOT_MAX:
        cas = dict(watched)
        cas.update(favlog)
        keys.sort(key=lambda k: _seen(cas.get(k)), reverse=True)
        keys = keys[:SNAPSHOT_MAX]
    return {k: items[k] for k in keys}


def reset_since(store):
    """Zahodí „kam jsme došli" a příští kolo s HA pošle celý stav.

    Volá se, když do `Store` přiteklo něco odjinud než z HA — typicky ze slepého
    relaye (`syncbox.py`), když je některé Kodi mostem mezi oběma středisky.
    Přijatý záznam si nese čas vzniku, a ten bývá starší než poslední výměna
    s HA, takže by ho filtr `since` už nikdy neposlal. `rts` razí jen střed,
    takže most jinou možnost nemá; celý stav je malý a pošle se jen po skutečné
    změně. Most je ale nouzové řešení — jede jen dokud to Kodi běží. Čistší je
    dát kód skupiny i samotnému HA (`CONF_SYNC_CODE` v integraci).
    """
    state = store.reload(STATE, {})
    if state.get("since"):
        store.save(STATE, dict(state, since=0))


def apply_changes(store, changes, stamp=False):
    """Slije cizí změny do místního úložiště. Vrací počet skutečně přijatých záznamů.
    `stamp=True` (jen střed, HA) přijatým záznamům vyrazí čas příjmu `rts`."""
    changes = changes or {}
    now = int(time.time()) if stamp else 0
    applied = 0
    with store._lock:
        watched = store.reload("watched", {})
        dirty = False
        for key, rec in (changes.get("watched") or {}).items():
            if isinstance(rec, dict) and _ts(rec) > _ts(watched.get(key)):
                watched[key] = _keep_playcount(_stamped(rec, now), watched.get(key))
                dirty = True
                applied += 1
        if dirty:
            store._trim(watched, WATCHED_MAX)
            store.save("watched", watched)

        favlog = store.reload("favlog", {})
        favs = store.reload("favourites", [])
        dirty = False
        for key, rec in (changes.get("favlog") or {}).items():
            if not isinstance(rec, dict) or _ts(rec) <= _ts(favlog.get(key)):
                continue
            favlog[key] = _stamped({"on": bool(rec.get("on")), "ts": _ts(rec)}, now)
            if rec.get("on") and key not in favs:
                favs.insert(0, key)
            elif not rec.get("on") and key in favs:
                favs.remove(key)
            dirty = True
            applied += 1
        if dirty:
            store.save("favlog", favlog)
            store.save("favourites", favs)

        histlog = store.reload("histlog", {})
        hist_dirty = False
        for key, rec in (changes.get("histlog") or {}).items():
            if isinstance(rec, dict) and _ts(rec) > _ts(histlog.get(key)):
                histlog[key] = _stamped(rec, now)
                hist_dirty = True
                applied += 1
        if hist_dirty:
            store._trim(histlog, 500)
            store.save("histlog", histlog)

        hidden = store.reload("next_hidden", {})
        dirty = False
        for key, rec in (changes.get("next_hidden") or {}).items():
            current = hidden.get(key)
            if isinstance(rec, dict) and rec.get("ep") and _ts(rec) > (_ts(current) if isinstance(current, dict) else 0):
                hidden[key] = _stamped({"ep": str(rec["ep"]), "ts": _ts(rec)}, now)
                dirty = True
                applied += 1
        if dirty:
            store.save("next_hidden", hidden)

        items = store.reload("items", {})
        dirty = False
        for key, snap in (changes.get("items") or {}).items():
            if isinstance(snap, dict) and key not in items:
                items[key] = snap
                dirty = True
        if dirty:
            store._trim(items, ITEMS_MAX)
            store.save("items", items)
    if hist_dirty:
        store.rebuild_history()   # zobrazený seznam podle sloučeného deníku
    return applied


def filter_circles(changes, circles):
    """Ze stavu nechá jen zapnuté okruhy; snímky titulů jdou vždy s tím, co zbyde.

    `circles=None` znamená „neomezovat" — tak se modul choval, než okruhy vznikly.
    """
    if circles is None:
        return changes or {}
    allowed = set()
    for name in circles:
        allowed.update(CIRCLES.get(name, ()))
    out = {k: v for k, v in (changes or {}).items() if k in allowed}
    # snímky jen k tomu, co je umí potřebovat — blob jen s nastavením je nemá proč vézt
    if (changes or {}).get(SNAPSHOTS) and ({"watched", "favlog"} & set(out)):
        out[SNAPSHOTS] = changes[SNAPSHOTS]
    return out


def sync_once(store, base_url, key, device="", circles=None):
    """Jedna výměna s HA. Vrací (ok, odesláno, přijato, důvod) — nikdy nevyhodí výjimku.

    `circles` je sada zapnutých okruhů (viz `CIRCLES`); `None` posílá a přijímá vše.
    """
    state = store.reload(STATE, {})
    # stav bez `v` je z doby, kdy filtr šel podle času změny: jednou se vymění všechno,
    # aby se dorovnaly záznamy, které se tím mohly minout
    since = int(state.get("since") or 0) if state.get("v") == 2 else 0
    # Zapnutý okruh musí dostat i to, co přišlo, když byl vypnutý — filtr zahodil
    # změny, ale `since` se posouvalo dál, takže jinak by se dorovnal až novou změnou.
    znamka = ",".join(sorted(circles)) if circles is not None else "*"
    if state.get("circles") != znamka:
        since = 0
    outgoing = filter_circles(collect_changes(store, since), circles)
    pushed = len(outgoing.get("watched") or {}) + len(outgoing.get("favlog") or {})
    body = json.dumps({"device": device, "since": since, "changes": outgoing}).encode("utf-8")
    req = urllib.request.Request((base_url or "").rstrip("/") + ENDPOINT, data=body, headers={
        "Content-Type": "application/json", "X-Nokturno-Key": key or "",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return _fail(store, state, f"HTTP {e.code}" + (" (špatný klíč)" if e.code == 403 else ""))
    except Exception as e:  # noqa: BLE001 – síť, DNS, špatná adresa
        return _fail(store, state, str(e)[:120])
    pulled = apply_changes(store, filter_circles(answer.get("changes"), circles))
    now = int(time.time())
    store.save(STATE, {"v": 2, "since": int(answer.get("now") or now), "last_ok": now, "last_error": "",
                       "circles": znamka, "pushed": pushed, "pulled": pulled})
    return True, pushed, pulled, ""


def _fail(store, state, why):
    state = dict(state, last_error=why, last_try=int(time.time()))
    store.save(STATE, state)
    return False, 0, 0, why
