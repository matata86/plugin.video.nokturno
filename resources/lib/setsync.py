"""Sdílení nastavení a účtů mezi Kodi — okruhy `settings` a `accounts`.

Zhlédnuto a Můj seznam leží ve `Store` a řeší je `sync.py`; volby doplňku a
přihlášení ke zdrojům jsou ale v `settings.xml`, kam jádro nevidí. Hostitel
(Kodi) proto posílá hodnoty dovnitř a zapisuje je zpátky sám, tady je jen to,
co musí být na obou koncích stejné: co se sdílí, co se nikdy nesdílí, a které
z dvojice hodnot vyhrává.

Deník `setlog.json` v profilu drží ke každému klíči poslední známou hodnotu
a čas, kdy se změnila:

    {"pref_lang": {"v": "1", "ts": 1789926589}, …}

Bez něj by nešlo poznat změnu od cizího zápisu: hodnota se porovnává proti
deníku, ne proti minulému kolu, takže jeden `ts` na klíč stačí a vyhrává
novější zápis (stejně jako u zhlédnuto).

**Účty zvlášť.** Uživatel může chtít mít na dvou televizích stejné volby, ale
hesla ne (nebo naopak). `ACCOUNT_KEYS` dělí klíče do dvou okruhů, které jdou
zapnout samostatně; oba jsou **výchozím stavem vypnuté** — přihlášení se sdílí
jen vědomě.

**`DENY` se nesdílí nikdy** ani jedním okruhem: `download_dir` (cesta, která na
druhém stroji nemusí existovat) a celá kategorie `sync_*` — jinak by jedno Kodi
vyplo synchronizaci všem, nebo by si zařízení navzájem přepsala kód skupiny.
Tokeny (CZtor, Trakt) nejsou v `settings.xml` vůbec, takže se nesdílí už tím:
CZtor mění obnovovací token použitím, kopie by původní zařízení odhlásila.
"""
import time

CIRCLE_SETTINGS = "settings"
CIRCLE_ACCOUNTS = "accounts"
SECTIONS = {CIRCLE_SETTINGS: "setlog", CIRCLE_ACCOUNTS: "acclog"}
STATE = "setlog"          # setlog.json v profilu

# přihlášení a adresy cizích služeb — vlastní okruh
ACCOUNT_KEYS = frozenset({
    "ws_username", "ws_password",
    "streamuj_username", "streamuj_password",
    "st_email", "st_password",
    "fs_username", "fs_password",
    "os_username", "os_password",
    "luna_url", "token",
    "tmdb_api_key",
    "trakt_client_id", "trakt_client_secret",
    "dav1_url", "dav1_username", "dav1_password", "dav1_name",
    "dav2_url", "dav2_username", "dav2_password", "dav2_name",
    "dav3_url", "dav3_username", "dav3_password", "dav3_name",
})

# co se nesdílí ani jedním okruhem
DENY = frozenset({
    "download_dir", "install_id",
    "sync_enabled", "sync_mode", "sync_url", "sync_key", "sync_code",
    "sync_watched", "sync_favourites", "sync_history", "sync_settings", "sync_accounts",
})


def circle_of(name):
    """Do kterého okruhu klíč patří, nebo None, když se nesdílí vůbec."""
    if not name or name in DENY:
        return None
    return CIRCLE_ACCOUNTS if name in ACCOUNT_KEYS else CIRCLE_SETTINGS


def _text(value):
    return "" if value is None else str(value)


def collect(store, values, circles, now=None):
    """Hodnoty hostitele → deník k odeslání, rozdělený po okruzích.

    Deník v profilu se zároveň srovná se skutečností: klíč se změněnou hodnotou
    dostane čerstvý `ts`, nezměněný si nechá starý, takže se stejný stav
    neposílá dokola s novým časem a nepřebíjel by cizí zápisy.
    """
    wanted = {c for c in (circles or ()) if c in SECTIONS}
    log = dict(store.reload(STATE, {}) or {})
    stamp = int(now or time.time())
    dirty = False
    for name, value in (values or {}).items():
        if circle_of(name) is None:
            continue
        value = _text(value)
        zaznam = log.get(name)
        if not isinstance(zaznam, dict) or _text(zaznam.get("v")) != value:
            log[name] = {"v": value, "ts": stamp}
            dirty = True
    # klíč, který hostitel přestal znát (starší verze doplňku), se z deníku nemaže:
    # cizí zařízení ho může dál používat a smazání by vypadalo jako změna
    if dirty:
        store.save(STATE, log)
    out = {}
    for name, zaznam in log.items():
        okruh = circle_of(name)
        if okruh in wanted and isinstance(zaznam, dict):
            out.setdefault(SECTIONS[okruh], {})[name] = {"v": _text(zaznam.get("v")),
                                                         "ts": int(zaznam.get("ts") or 0)}
    return out


def merge(store, incoming, values, circles):
    """Cizí deník → co má hostitel zapsat do nastavení (`{id: hodnota}`).

    Bere jen klíče ze zapnutých okruhů, které hostitel zná (jsou ve `values`) —
    jinak by starší doplněk uložil nastavení, které neumí, a při dalším kole by
    ho poslal zpátky jako své.
    """
    wanted = {c for c in (circles or ()) if c in SECTIONS}
    log = dict(store.reload(STATE, {}) or {})
    zmeny = {}
    for okruh in sorted(wanted):
        for name, zaznam in ((incoming or {}).get(SECTIONS[okruh]) or {}).items():
            if circle_of(name) != okruh or name not in (values or {}) or not isinstance(zaznam, dict):
                continue
            ts = int(zaznam.get("ts") or 0)
            value = _text(zaznam.get("v"))
            muj = log.get(name) if isinstance(log.get(name), dict) else {}
            muj_ts = int(muj.get("ts") or 0)
            if ts < muj_ts:
                continue                       # novější vyhrává
            if ts == muj_ts and not (value and not _text((values or {}).get(name))):
                # Remízu rozhodne vyplněnost: nové Kodi si účty stáhne, ale samo
                # cizí hodnotu prázdnou nepřepíše. Obě zařízení zapíšou první
                # deník ve stejné sekundě, takže bez tohohle by se nepotkala.
                continue
            log[name] = {"v": value, "ts": ts}
            if _text(values.get(name)) != value:
                zmeny[name] = value
    if zmeny or log != (store.reload(STATE, {}) or {}):
        store.save(STATE, log)
    return zmeny
