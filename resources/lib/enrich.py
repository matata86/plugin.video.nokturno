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
DEADLINE = 10.0  # s – déle seznam nezdržovat; zbytek se dotáhne na pozadí do cache (2026-09-15: 6 s
                 # nestíhalo u větších seznamů, např. 30 titulů v Kodi „Nově přidané s CZ dabingem")
TMDB_IMG_PREFIX = "https://image.tmdb.org/t/p/"
# jeden executor pro celý proces: dřív nový na každé hledání s `shutdown(wait=False)`, takže po
# několika hledáních za sebou běžely desítky visících vláken (Luna má timeout 40 s).
# Vzniká až při prvním dotazu a `shutdown_pool()` ho zavře: nečinná vlákna executoru
# čekají na frontu bez timeoutu a nikdy sama neskončí. Kodi po doběhnutí pluginu čeká
# na všechna jeho vlákna a vlákno zablokované v C nezabije ani „let's kill it" —
# Office 2026-09-16: widget „Nově přidané" ze Sosáče takhle zablokoval vypnutí Kodi
# natrvalo. HA a Stremio běží dlouhodobě a pool si nechávají.
_POOL = None
_POOL_LOCK = threading.Lock()
_INFLIGHT = {}          # (ctype, klíč titulu) → future — tentýž titul se nedotahuje dvakrát naráz
# RLock, ne Lock: `add_done_callback()` v `_submit()` spustí `hotovo()` HNED a v témž vlákně,
# pokud je future v okamžiku registrace už hotová (typicky bez Luny/sítě — `_fetch_title`
# vrátí `{}` okamžitě) — s prostým Lockem to byl jistý deadlock (stejné vlákno drží zámek
# ve `with` bloku výš a `hotovo()` se ho pokouší znovu zamknout).
_INFLIGHT_LOCK = threading.RLock()
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


def _capped(url, size):
    """Cinemeta (na rozdíl od vlastní cesty přes TMDB) vrací poster/fanart v plné
    velikosti `original` — u fanartu klidně 3840×2160. Jako dekódovaná bitmapa v paměti
    prohlížeče (karta HA) je to desítky MB na obrázek; při procházení katalogu se to sčítá
    do gigabajtů a vede to k „stránka neodpovídá". Zmenšíme na stejné rozměry jako IMG/IMG_BIG
    v `tmdb_api.py`, než se URL uloží do cache."""
    if url and url.startswith(TMDB_IMG_PREFIX) and "/original/" in url:
        return url.replace("/original/", f"/{size}/", 1)
    return url


def _poster_broken(meta):
    """Náhledy Sosáče (movies.sosac.tv) vracejí 404 — poster musí přijít z TMDB."""
    return DEAD_IMAGES in (meta.get("poster") or "")


def _needs(meta):
    if meta.get("imdb_id"):
        # Sosáčův export nosí krátký popis skoro vždy, ale hodnocení jen když ho
        # zrovna měl v datech — dřív se podle přítomnosti popisu titul označil
        # za „hotový" a hodnocení už nikdy nedotáhl, i když chybělo.
        return not meta.get("description") or not meta.get("imdbRating") or _poster_broken(meta)
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
            if picked.get("poster"):
                picked["poster"] = _capped(picked["poster"], "w500")
            if picked.get("background"):
                picked["background"] = _capped(picked["background"], "w1280")
            if m.get("imdb_id") or str(m.get("id", "")).startswith("tt"):
                picked["imdb_id"] = m.get("imdb_id") or m["id"]
            return picked
        return {}
    key = f"ttname:{ctype}:{_norm(title)}:{year or ''}"
    return store.cached(key, TTL, load) if store else load()


def _fetch(luna, store, ctype, imdb, tmdb=None):
    def load():
        data = {}
        if tmdb:
            # vlastní klíč TMDB má přednost: česky, kdežto Cinemeta (záloha bez Luny) anglicky
            try:
                data = {k: v for k, v in (tmdb.brief(ctype, imdb) or {}).items() if v}
            except Exception:  # noqa: BLE001 – TMDB nedostupné → Luna/Cinemeta
                data = {}
        if luna and (not data.get("description") or not data.get("imdbRating")):
            try:
                for k, v in (luna.meta(ctype, imdb) or {}).items():
                    if v:
                        data.setdefault(k, v)
            except Exception:  # noqa: BLE001 – Luna nedostupná → Cinemeta
                pass
        if not data.get("description") or not data.get("imdbRating"):
            # Luna umí vrátit popis bez hodnocení (2026-09-15: zjištěno u titulů ze
            # Sosáčova „nově přidané" — Sosáč sám hodnocení skoro nikdy nenosí, tak
            # se spoléhá na tohle dotažení) — doplnit z Cinemety jen to, co Luna
            # nedala, ne přepsat český popis anglickým jen kvůli chybějícímu číslu
            try:
                cinemeta = _cinemeta(ctype, imdb) or {}
            except Exception:  # noqa: BLE001
                cinemeta = {}
            for k, v in cinemeta.items():
                data.setdefault(k, v)
        picked = {k: data[k] for k in FIELDS if data.get(k)}
        for k in ("imdbRating", "background", "genres", "year", "releaseInfo", "poster"):
            if data.get(k):
                picked[k] = data[k]
        if picked.get("poster"):
            picked["poster"] = _capped(picked["poster"], "w500")
        if picked.get("background"):
            picked["background"] = _capped(picked["background"], "w1280")
        return picked
    # s TMDB jiný klíč: pod `ttmeta:` může 30 dní ležet anglický popis z Cinemety
    key = f"ttmeta:{ctype}:{imdb}" + (":tmdb" if tmdb else "")
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


def _lookup(luna, store, ctype, meta, tmdb=None):
    if meta.get("imdb_id"):
        return _fetch(luna, store, ctype, meta["imdb_id"], tmdb)
    year = str(meta.get("year") or "")[:4]
    return _fetch_title(luna, store, ctype, meta.get("_title") or meta.get("name"), year if year.isdigit() else "")


def enrich(metas, luna=None, store=None, ctype="movie", deadline=DEADLINE, on_tick=None, on_count=None, tmdb=None):
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
        by_future.setdefault(_submit(luna, store, ctype, m, tmdb), []).append(m)
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


def _submit(luna, store, ctype, meta, tmdb=None):
    """Dotaz na titul ve sdíleném executoru; běží-li už pro tentýž titul, vrátí jeho future."""
    key = (ctype, meta.get("imdb_id") or _norm(meta.get("_title") or meta.get("name")), str(meta.get("year") or "")[:4])
    with _INFLIGHT_LOCK:
        fut = _INFLIGHT.get(key)
        if fut is not None and not fut.done():
            return fut
        fut = _pool().submit(_lookup, luna, store, ctype, meta, tmdb)
        _INFLIGHT[key] = fut

        def hotovo(f, key=key):
            with _INFLIGHT_LOCK:
                if _INFLIGHT.get(key) is f:
                    del _INFLIGHT[key]
        fut.add_done_callback(hotovo)
        return fut


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="nokturno-enrich")
        return _POOL


def shutdown_pool(cancel=False):
    """Zavře sdílený executor, aby jeho nečinná vlákna skončila (viz `_POOL`). Rozběhnuté
    dotazy doběhnou samy na svůj timeout; `cancel=True` (hostitel končí) zruší i ty, které
    ve frontě ještě nezačaly — `shutdown(cancel_futures=)` je až od Pythonu 3.9, Kodi 20
    má 3.8. Další `enrich()` si založí nový executor."""
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is None:
        return
    if cancel:
        with _INFLIGHT_LOCK:
            pending = list(_INFLIGHT.values())
        for fut in pending:
            fut.cancel()
    pool.shutdown(wait=False)
    # Rozepsané dotazy patřily zavřenému executoru. Hotová i zrušená future se z
    # `_INFLIGHT` odebere sama v done-callbacku, ale na tom nejde stavět: v Kodi se
    # `reuselanguageinvoker` drží interpret mezi kliknutími, takže by si další
    # spuštění mohlo sáhnout na future, kterou už nikdo nedokončí, a čekat na ni.
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()


def enrich_one(meta, luna=None, store=None, ctype="movie", tmdb=None):
    if _needs(meta):
        try:
            extra = _lookup(luna, store, ctype, meta, tmdb)
        except Exception:  # noqa: BLE001
            return meta
        if extra:
            _apply(meta, extra)
    return meta
