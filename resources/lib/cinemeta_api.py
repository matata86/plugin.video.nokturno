"""Klient pro katalogy a hledání Cinemety (Stremio doplněk, tt id, bez účtu/klíče).

Cinemeta je jediný zdroj metadat, který funguje vždycky — bez Luny (LAN server)
i bez Sosáče (vlastní účet). Slouží jako záložní/vlastní databáze filmů a
seriálů, nezávislá na tom, jestli je zapnutý nějaký zdroj streamů.

Tvar výstupu (`catalogs()`/`catalog()`) je záměrně stejný jako u `LunaApi`, aby
šel použít jako drop-in náhrada všude, kde se dnes indexuje `apis[src]`.

Bez závislostí na Kodi — jde testovat samostatně:
    python3 cinemeta_api.py movie "jursky svet"
"""
import json
import urllib.parse
import urllib.request

BASE = "https://v3-cinemeta.strem.io"
TIMEOUT = 15
MANIFEST_TTL = 24 * 3600
SEARCH_TTL = 12 * 3600
# pořadí i české názvy — anglické `id` jde do URL beze změny
CATALOG_NAMES = {"top": "Populární", "year": "Podle roku", "imdbRating": "Nejlépe hodnocené"}


class CinemetaError(Exception):
    pass


class CinemetaApi:
    def __init__(self, cache=None, cache_ttl=MANIFEST_TTL):
        self.cache = cache  # objekt s .cached(key, ttl, loader) — stejné rozhraní jako LunaApi
        self.cache_ttl = cache_ttl

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "Nokturno"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise CinemetaError(f"{e} ({url})") from e

    def _get_cached(self, url, ttl=None):
        if self.cache is None:
            return self._get(url)
        return self.cache.cached(url, ttl or self.cache_ttl, lambda: self._get(url))

    def manifest(self):
        return self._get_cached(f"{BASE}/manifest.json")

    def catalogs(self, ctype):
        """Katalogy pro daný typ — stejný tvar jako `LunaApi.catalogs()`."""
        result = []
        for c in self.manifest().get("catalogs", []):
            if c.get("type") != ctype:
                continue
            cid = c.get("id", "")
            extra = {e.get("name"): e for e in c.get("extra", [])}
            if "search" not in extra and cid not in CATALOG_NAMES:
                continue  # last-videos/calendar-videos — interní, nepatří do menu
            result.append({
                "id": cid,
                "name": CATALOG_NAMES.get(cid, cid),
                "search": "search" in extra,
                "genre_required": bool(extra.get("genre", {}).get("isRequired")),
                "genres": extra.get("genre", {}).get("options") or [],
            })
        return result

    def catalog(self, ctype, cid, genre=None, search=None, skip=0):
        """Seznam metadat — stejný tvar jako `LunaApi.catalog()` (bez popisu; ten
        dotáhne `enrich()`/`meta()` až u konkrétního titulu)."""
        extra = []
        if search:
            extra.append("search=" + urllib.parse.quote(search, safe=""))
        if genre:
            extra.append("genre=" + urllib.parse.quote(genre, safe=""))
        if skip:
            extra.append(f"skip={int(skip)}")
        if extra:
            url = f"{BASE}/catalog/{ctype}/{cid}/{'&'.join(extra)}.json"
        else:
            url = f"{BASE}/catalog/{ctype}/{cid}.json"
        loader = lambda: self._get(url).get("metas") or []  # noqa: E731
        # hledání se kešuje jako u Luny — výsledky se nemění rychle
        if search and self.cache is not None:
            return self.cache.cached(url, SEARCH_TTL, loader)
        return loader()

    def meta(self, ctype, imdb_id):
        return self._get_cached(f"{BASE}/meta/{ctype}/{imdb_id}.json", ttl=SEARCH_TTL).get("meta") or {}


if __name__ == "__main__":
    import sys
    api = CinemetaApi()
    print(json.dumps(api.catalog(sys.argv[1] if len(sys.argv) > 1 else "movie",
                                  "top", search=sys.argv[2] if len(sys.argv) > 2 else "matrix")[:3],
                     ensure_ascii=False, indent=2))
