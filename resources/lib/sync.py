"""Synchronizace zhlédnuto / rozkoukané / Můj seznam mezi více Kodi přes Home Assistant.

HA je střed: integrace Nokturno drží sloučený stav a každé Kodi si s ní jednou za
čas vymění změny. Bez HA jede doplněk dál z vlastních souborů a dorovná se, až
bude HA zpátky. Stejný modul běží na obou stranách (Kodi doplněk i integrace),
proto tu nejsou žádné závislosti na Kodi ani na HA — jen `Store`.

Jeden výměnný krok je POST na `/api/nokturno/sync`:

    {"device": "Obývák", "since": <čas HA z minula>,
     "changes": {"watched": {klíč: záznam}, "favlog": {klíč: {"on", "ts"}}, "items": {klíč: snímek}}}

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
ENDPOINT = "/api/nokturno/sync"
TIMEOUT = 20


def _ts(rec):
    try:
        return int((rec or {}).get("ts") or 0)
    except (TypeError, ValueError):
        return 0


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
    watched = {k: v for k, v in store.reload("watched", {}).items() if _ts(v) >= since}
    favlog = {k: v for k, v in store.reload("favlog", {}).items() if _ts(v) >= since}
    histlog = {k: v for k, v in store.reload("histlog", {}).items() if _ts(v) >= since}
    items = store.reload("items", {})
    keys = set(watched) | set(favlog)
    return {"watched": watched, "favlog": favlog, "histlog": histlog,
            "items": {k: items[k] for k in keys if k in items}}


def apply_changes(store, changes):
    """Slije cizí změny do místního úložiště. Vrací počet skutečně přijatých záznamů."""
    changes = changes or {}
    applied = 0
    with store._lock:
        watched = store.reload("watched", {})
        dirty = False
        for key, rec in (changes.get("watched") or {}).items():
            if isinstance(rec, dict) and _ts(rec) > _ts(watched.get(key)):
                watched[key] = rec
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
            favlog[key] = {"on": bool(rec.get("on")), "ts": _ts(rec)}
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
                histlog[key] = rec
                hist_dirty = True
                applied += 1
        if hist_dirty:
            store._trim(histlog, 500)
            store.save("histlog", histlog)

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


def sync_once(store, base_url, key, device=""):
    """Jedna výměna s HA. Vrací (ok, odesláno, přijato, důvod) — nikdy nevyhodí výjimku."""
    state = store.reload(STATE, {})
    since = int(state.get("since") or 0)
    outgoing = collect_changes(store, since)
    pushed = len(outgoing["watched"]) + len(outgoing["favlog"])
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
    pulled = apply_changes(store, answer.get("changes"))
    now = int(time.time())
    store.save(STATE, {"since": int(answer.get("now") or now), "last_ok": now, "last_error": "",
                       "pushed": pushed, "pulled": pulled})
    return True, pushed, pulled, ""


def _fail(store, state, why):
    state = dict(state, last_error=why, last_try=int(time.time()))
    store.save(STATE, state)
    return False, 0, 0, why
