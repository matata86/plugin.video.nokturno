"""Hostitel, ze kterého se nedá stáhnout, se na chvíli přeskočí.

Soubor filmu leží na konkrétním stroji zdroje (`s42.streamuj.tv`, `fs3.fastshare.cz`)
a ten může být dole, i když zdroj sám odpovídá. Naměřeno 2026-09-22: 18 z 54 serverů
streamuj.tv nebralo spojení. Bez pauzy by se u titulu s pěti streamy z téhož stroje
čekalo pětkrát na timeout místo jednou.

Paměť procesu nestačí: v Kodi je plugin jiný interpret než služba na pozadí, takže
stav se drží i na disku — stejně jako pauza HellSpy po 429 (`hellspy_api.blocked_for`).
"""
import time
import urllib.parse

COOLDOWN = 10 * 60
STORE = "dead_hosts"
MAX_HOSTS = 50

_dead = {}   # {host: until} — paměť procesu, sdílená se `Store` na disku


def host_of(url):
    """Hostitel z adresy, malými písmeny. Neplatná adresa → prázdný řetězec."""
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _from_disk(cache):
    if cache is None or not hasattr(cache, "load"):
        return {}
    # kopie: `Store.load()` vrací objekt ze své paměti a `mark_dead()` ho mění
    try:
        return dict(cache.load(STORE, {}) or {})
    except (OSError, ValueError, TypeError):
        return {}


def is_dead(host, cache=None):
    """Je hostitel teď v pauze? Bere pozdější ze stavu procesu a z disku."""
    if not host:
        return False
    until = max(_dead.get(host, 0), float(_from_disk(cache).get(host) or 0))
    return until > time.time()


def mark_dead(host, cache=None):
    """Uspí hostitele na `COOLDOWN`, zapíše do paměti procesu i na disk."""
    if not host:
        return
    until = time.time() + COOLDOWN
    _dead[host] = until
    if cache is None or not hasattr(cache, "save"):
        return
    data = _from_disk(cache)
    data[host] = until
    now = time.time()
    data = {h: u for h, u in data.items() if u > now}
    if len(data) > MAX_HOSTS:
        # zahodit nejdřív ty, kterým pauza skončí nejdřív
        for h in sorted(data, key=data.get)[:len(data) - MAX_HOSTS]:
            del data[h]
    try:
        cache.save(STORE, data)
    except OSError:
        pass   # pauza pak platí jen pro tenhle proces, což je pořád lepší než nic


def clear(cache=None):
    """Vyprázdní stav — pro testy."""
    _dead.clear()
    if cache is not None and hasattr(cache, "save"):
        try:
            cache.save(STORE, {})
        except OSError:
            pass
