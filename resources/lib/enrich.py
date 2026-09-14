"""Doplnění popisu k titulům Sosáče.

Veřejné exporty Sosáče (`vystupy5981/*.json`) nesou jen název, rok, žánry, hodnocení
a IMDb id – popis filmu v nich není. Popis, stopáž, režie a obsazení se proto dotahují
podle IMDb id: přednostně z Luny (česky, TMDB), bez Luny z Cinemety (Stremio, anglicky).
Výsledek se ukládá do cache doplňku na 30 dní, takže seznam se zdrží jen napoprvé.
"""
import json
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed

CINEMETA = "https://v3-cinemeta.strem.io/meta/{ctype}/{imdb}.json"
TTL = 30 * 86400
TIMEOUT = 8
WORKERS = 8
DEAD_IMAGES = "movies.sosac.tv"  # jejich náhledy jsou od 2026-09 pryč (404)
DEADLINE = 6.0  # s – déle seznam nezdržovat; zbytek se dotáhne na pozadí do cache
# jeden executor pro celý proces: dřív nový na každé hledání s `shutdown(wait=False)`, takže po
# několika hledáních za sebou běžely desítky visících vláken (Luna má timeout 40 s)
_POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="nokturno-enrich")
_INFLIGHT = {}          # (ctype, klíč titulu) → future — tentýž titul se nedotahuje dvakrát naráz
_INFLIGHT_LOCK = threading.Lock()
FIELDS = ("description", "runtime", "director", "writer", "cast", "app_extras", "released", "country", "imdb_id",
          # veřejné exporty Sosáče mívají žánry jako syrové anglické tagy s velkými
          # a malými písmeny na hromádce (a občas i vyloženě smetí typu "html5") —
          # Luna/Cinemeta/TMDB dávají čistý, přeložitelný seznam, ten má vždy přednost
          "genres")


def _cinemeta(ctype, imdb):
    req = urllib.request.Request(CINEMETA.format(ctype=ctype, imdb=imdb),
                                 headers={"User-Agent": "Nokturno (+https://github.com/matata86/nokturno-core)"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8")).get("meta") or {}


def _poster_broken(meta):
    """Náhledy Sosáče (movies.sosac.tv) vracejí 404 — poster musí přijít z TMDB."""
    return DEAD_IMAGES in (meta.get("poster") or "")


def _needs(meta):
    if meta.get("imdb_id"):
        return not meta.get("description") or _poster_broken(meta)
    # bez IMDb id zbývá dohledat podle názvu a roku — jen když chybí nebo je mrtvý obrázek
    return bool(meta.get("name") or meta.get("_title")) and (not meta.get("poster") or _poster_broken(meta))


def _norm(text):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", (text or "").lower()) if c.isalnum() or c == " ").strip()


def _fetch_title(luna, store, ctype, title, year):
    """Poster/fanart/popis z TMDB (přes Lunu) podle názvu a roku — pro tituly bez IMDb id."""
    if not luna or not title:
        return {}

    def load():
        try:
            cid = "search.movie" if ctype == "movie" else "search.series"
            metas = luna.catalog(ctype, cid, search=title)
        except Exception:  # noqa: BLE001
            return {}
        want = _norm(title)
        for m in metas[:10]:
            if _norm(m.get("name")) != want:
                continue
            my = str(m.get("year") or m.get("releaseInfo") or "")[:4]
            if year and my.isdigit() and abs(int(my) - int(year)) > 1:
                continue
            picked = {k: m[k] for k in ("poster", "background", "description", "imdbRating", "genres") if m.get(k)}
            if m.get("imdb_id") or str(m.get("id", "")).startswith("tt"):
                picked["imdb_id"] = m.get("imdb_id") or m["id"]
            return picked
        return {}
    key = f"ttname:{ctype}:{_norm(title)}:{year or ''}"
    return store.cached(key, TTL, load) if store else load()


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
        if k in ("poster", "background"):
            if not meta.get(k) or DEAD_IMAGES in (meta.get(k) or ""):
                meta[k] = v
            continue
        if k in FIELDS or not meta.get(k):
            meta.setdefault(k, v) if k in ("imdb_id",) else meta.__setitem__(k, v)
    return meta


def _lookup(luna, store, ctype, meta):
    if meta.get("imdb_id"):
        return _fetch(luna, store, ctype, meta["imdb_id"])
    year = str(meta.get("year") or "")[:4]
    return _fetch_title(luna, store, ctype, meta.get("_title") or meta.get("name"), year if year.isdigit() else "")


def enrich(metas, luna=None, store=None, ctype="movie", deadline=DEADLINE, on_tick=None, on_count=None):
    """Doplní popis do metas (in-place). Vrátí počet doplněných položek.

    `on_count(n)` se zavolá jednou se skutečným počtem položek k dohledání (pro
    přepočet ukazatele průběhu na reálná 100 %) a `on_tick()` po každé dokončené —
    stejný vzor jako `_fill_audio()` v engine.py.
    """
    todo = [m for m in metas if _needs(m)]
    if on_count:
        on_count(len(todo))
    if not todo:
        return 0
    by_future = {}
    for m in todo:
        by_future.setdefault(_submit(luna, store, ctype, m), []).append(m)
    filled = 0
    try:
        # jako dřívější wait(timeout=deadline) — po timeoutu se přestane čekat,
        # nedokončené doběhnou na pozadí a zapíšou se do cache; tady navíc tiká
        # ukazatel průběhu po každé položce, která stihla doběhnout včas
        for fut in as_completed(list(by_future), timeout=deadline):
            for _ in by_future[fut]:
                if on_tick:
                    on_tick()
            try:
                extra = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if extra:
                for m in by_future[fut]:
                    _apply(m, extra)
                    filled += 1
    except FuturesTimeoutError:
        pass
    return filled


def _submit(luna, store, ctype, meta):
    """Dotaz na titul ve sdíleném executoru; běží-li už pro tentýž titul, vrátí jeho future."""
    key = (ctype, meta.get("imdb_id") or _norm(meta.get("_title") or meta.get("name")), str(meta.get("year") or "")[:4])
    with _INFLIGHT_LOCK:
        fut = _INFLIGHT.get(key)
        if fut is not None and not fut.done():
            return fut
        fut = _POOL.submit(_lookup, luna, store, ctype, meta)
        _INFLIGHT[key] = fut

        def hotovo(f, key=key):
            with _INFLIGHT_LOCK:
                if _INFLIGHT.get(key) is f:
                    del _INFLIGHT[key]
        fut.add_done_callback(hotovo)
        return fut


def enrich_one(meta, luna=None, store=None, ctype="movie"):
    if _needs(meta):
        try:
            extra = _lookup(luna, store, ctype, meta)
        except Exception:  # noqa: BLE001
            return meta
        if extra:
            _apply(meta, extra)
    return meta
