"""Doplnění popisu k titulům Sosáče.

Veřejné exporty Sosáče (`vystupy5981/*.json`) nesou jen název, rok, žánry, hodnocení
a IMDb id – popis filmu v nich není. Popis, stopáž, režie a obsazení se proto dotahují
podle IMDb id: přednostně z Luny (česky, TMDB), bez Luny z Cinemety (Stremio, anglicky).
Výsledek se ukládá do cache doplňku na 30 dní, takže seznam se zdrží jen napoprvé.
"""
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

CINEMETA = "https://v3-cinemeta.strem.io/meta/{ctype}/{imdb}.json"
TTL = 30 * 86400
TIMEOUT = 8
WORKERS = 8
DEAD_IMAGES = "movies.sosac.tv"  # jejich náhledy jsou od 2026-09 pryč (404)
DEADLINE = 6.0  # s – déle seznam nezdržovat; zbytek se dotáhne na pozadí do cache
FIELDS = ("description", "runtime", "director", "writer", "cast", "app_extras", "released", "country", "imdb_id")


def _cinemeta(ctype, imdb):
    req = urllib.request.Request(CINEMETA.format(ctype=ctype, imdb=imdb),
                                 headers={"User-Agent": "Kodi plugin.video.nokturno"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8")).get("meta") or {}


def _poster_broken(meta):
    """Náhledy Sosáče (movies.sosac.tv) vracejí 404 — poster musí přijít z TMDB."""
    return DEAD_IMAGES in (meta.get("poster") or "")


def _needs(meta):
    if not meta.get("imdb_id"):
        return False
    return not meta.get("description") or _poster_broken(meta)


def _fetch(luna, store, ctype, imdb):
    def load():
        data = {}
        if luna:
            try:
                data = luna.meta(ctype, imdb) or {}
            except Exception:  # noqa: BLE001 – Luna nedostupná → Cinemeta
                data = {}
        if not data.get("description"):
            try:
                data = _cinemeta(ctype, imdb) or data
            except Exception:  # noqa: BLE001
                pass
        picked = {k: data[k] for k in FIELDS if data.get(k)}
        for k in ("imdbRating", "background", "genres", "year", "releaseInfo", "poster"):
            if data.get(k):
                picked[k] = data[k]
        return picked
    key = f"ttmeta:{ctype}:{imdb}"
    return store.cached(key, TTL, load) if store else load()


def _apply(meta, extra):
    for k, v in extra.items():
        if k == "poster":
            if not meta.get("poster") or _poster_broken(meta):
                meta["poster"] = v
            continue
        if k in FIELDS or not meta.get(k):
            meta.setdefault(k, v) if k in ("imdb_id",) else meta.__setitem__(k, v)
    return meta


def enrich(metas, luna=None, store=None, ctype="movie", deadline=DEADLINE):
    """Doplní popis do metas (in-place). Vrátí počet doplněných položek."""
    todo = [m for m in metas if _needs(m)]
    if not todo:
        return 0
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    futures = {pool.submit(_fetch, luna, store, ctype, m["imdb_id"]): m for m in todo}
    done, _pending = wait(futures, timeout=deadline)
    filled = 0
    for fut in done:
        try:
            extra = fut.result()
        except Exception:  # noqa: BLE001
            continue
        if extra:
            _apply(futures[fut], extra)
            filled += 1
    pool.shutdown(wait=False)  # nedokončené doběhnou na pozadí a zapíšou se do cache
    return filled


def enrich_one(meta, luna=None, store=None, ctype="movie"):
    if _needs(meta):
        try:
            extra = _fetch(luna, store, ctype, meta["imdb_id"])
        except Exception:  # noqa: BLE001
            return meta
        if extra:
            _apply(meta, extra)
    return meta
