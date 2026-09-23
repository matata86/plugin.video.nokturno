"""Pauza po odmítnutém přihlášení — sdílené pro zdroje s účtem jménem a heslem.

Když zdroj odmítne jméno nebo heslo, další pokus se stejnými údaji dopadne
stejně. Katalog s jazykem ale projde až 60 titulů a každý hledá několika
dotazy, takže se doplněk hlásil se špatným heslem stovkykrát za kolo (log
uživatele 2026-09-23: přes 400 odmítnutých přihlášení k Sledujteto
a Přehraj.to). Po odmítnutí se proto zdroj s týmiž údaji hodinu nezkouší.

Klíč je otisk jména a hesla: po opravě hesla v nastavení pauza hned
přestane platit. Stav je v paměti procesu i v úložišti, aby platil i mezi
pluginem a službou v Kodi.
"""
import hashlib
import time

COOLDOWN = 60 * 60
STORE = "badlogin"
_memory = {}


def _key(source, login, password):
    digest = hashlib.sha256(f"{login}\0{password}".encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


def login_paused(source, login, password, cache=None):
    """True = tyhle údaje zdroj nedávno odmítl, znovu se nepřihlašovat."""
    key = _key(source, login, password)
    until = _memory.get(key, 0.0)
    if cache is not None and hasattr(cache, "load"):
        try:
            until = max(until, float((cache.load(STORE, {}) or {}).get(key) or 0))
        except Exception:  # noqa: BLE001 – rozsypaný soubor nesmí shodit hledání
            pass
    return until > time.time()


def mark_bad_login(source, login, password, cache=None):
    """Zapíše odmítnuté přihlášení."""
    key = _key(source, login, password)
    now = time.time()
    _memory[key] = now + COOLDOWN
    if cache is not None and hasattr(cache, "save"):
        try:
            data = {k: v for k, v in (cache.load(STORE, {}) or {}).items() if float(v or 0) > now}
            data[key] = now + COOLDOWN
            cache.save(STORE, data)
        except Exception:  # noqa: BLE001
            pass

