"""Klient pro TMDB (themoviedb.org) — vlastní databáze filmů a seriálů česky,
přednostní náhrada za veřejný katalog Sosáče / Cinemetu, když Luna neběží.
Potřebuje vlastní (zdarma) API klíč uživatele — viz nápověda u nastavení.

Výsledky `catalog()`/`search()` mají rovnou `tt…` (IMDb) id — TMDB samo dává
jen svoje číselné id, tt… se dotáhne přes `external_ids` souběžně pro všechny
nalezené položky (`ThreadPoolExecutor`, stejný vzor jako `_fill_audio()` v
engine.py), aby zbytek doplňku (streamy, oblíbené, sync) fungoval beze změny.
Položka bez tt… id (vzácné – TMDB titul bez napojení na IMDb) se ze seznamu
vynechá, se zbytkem doplňku by stejně nešla spárovat.

Bez závislostí na Kodi — jde testovat samostatně:
    python3 tmdb_api.py <api_key> movie "matrix"
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
IMG_BIG = "https://image.tmdb.org/t/p/w1280"
LANG = "cs-CZ"
TIMEOUT = 15
WORKERS = 8
SEARCH_TTL = 12 * 3600
DETAIL_TTL = 30 * 86400
GENRE_TTL = 7 * 86400
CATALOGS = {"popular": "Populární", "top_rated": "Nejlépe hodnocené"}


class TmdbError(Exception):
    pass


class TmdbApi:
    def __init__(self, api_key, cache=None):
        self.key = (api_key or "").strip()
        self.cache = cache

    def _get(self, path, **params):
        params["api_key"] = self.key
        params.setdefault("language", LANG)
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Nokturno"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise TmdbError("neplatný TMDB API klíč") from e
            raise TmdbError(f"{e} ({path})") from e
        except Exception as e:  # noqa: BLE001
            raise TmdbError(f"{e} ({path})") from e

    def _cached(self, key, ttl, loader):
        if self.cache is None:
            return loader()
        return self.cache.cached(key, ttl, loader)

    def _kind(self, ctype):
        return "movie" if ctype == "movie" else "tv"

    def _genres(self, ctype):
        kind = self._kind(ctype)
        data = self._cached(f"tmdb:genres:{kind}", GENRE_TTL, lambda: self._get(f"/genre/{kind}/list"))
        return {g["id"]: g["name"] for g in data.get("genres") or []}

    def catalogs(self, ctype):
        genres = list(self._genres(ctype).values())
        return [{"id": cid, "name": name, "search": False, "genre_required": False, "genres": genres}
                for cid, name in CATALOGS.items()]

    def _imdb_id(self, ctype, tmdb_id):
        kind = self._kind(ctype)
        data = self._cached(f"tmdb:ext:{kind}:{tmdb_id}", DETAIL_TTL,
                            lambda: self._get(f"/{kind}/{tmdb_id}/external_ids"))
        return (data or {}).get("imdb_id") or ""

    def _item(self, ctype, raw, genre_map):
        imdb_id = self._imdb_id(ctype, raw["id"])
        if not imdb_id:
            return None
        name = raw.get("title") or raw.get("name") or ""
        year = (raw.get("release_date") or raw.get("first_air_date") or "")[:4]
        genres = [g for g in (genre_map.get(gid, "") for gid in (raw.get("genre_ids") or [])) if g]
        return {
            "id": imdb_id,
            "imdb_id": imdb_id,
            "type": ctype,
            "name": name,
            "_title": name,
            "year": year,
            "poster": IMG + raw["poster_path"] if raw.get("poster_path") else "",
            "background": IMG_BIG + raw["backdrop_path"] if raw.get("backdrop_path") else "",
            "description": raw.get("overview") or "",
            "genres": genres,
            "imdbRating": raw.get("vote_average") or None,
        }

    def catalog(self, ctype, cid, genre=None, search=None, skip=0):
        """Seznam metadat — stejný tvar jako `LunaApi.catalog()`/`CinemetaApi.catalog()`,
        rovnou s `tt…` id (viz `_item`). Externí id se pro celou stránku dohledávají
        souběžně, ne jeden po druhém — jinak by 20 titulů čekalo 20× na síť za sebou."""
        kind = self._kind(ctype)
        page = int(skip or 0) // 20 + 1
        genre_map = self._genres(ctype)
        if search:
            def load():
                return self._get(f"/search/{kind}", query=search, page=page).get("results") or []
            raw = self._cached(f"tmdb:search:{kind}:{search.strip().lower()}:{page}", SEARCH_TTL, load)
        else:
            params = {"sort_by": "vote_average.desc" if cid == "top_rated" else "popularity.desc", "page": page}
            if cid == "top_rated":
                params["vote_count.gte"] = 200
            if genre:
                by_name = {v: k for k, v in genre_map.items()}
                if genre in by_name:
                    params["with_genres"] = by_name[genre]
            raw = self._get(f"/discover/{kind}", **params).get("results") or []
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            items = list(pool.map(lambda r: self._item(ctype, r, genre_map), raw))
        return [i for i in items if i]

    def meta(self, ctype, imdb_id):
        """Detail podle `tt…` id (přes TMDB `/find`) — titul, popis, žánry, obsazení,
        u seriálu i epizody (`videos`, stejný tvar jako Luna/Cinemeta)."""
        kind = self._kind(ctype)

        def load():
            found = self._get(f"/find/{imdb_id}", external_source="imdb_id")
            results = found.get(f"{kind}_results") or []
            if not results:
                raise TmdbError(f"titul {imdb_id} v TMDB nenalezen")
            tmdb_id = results[0]["id"]
            data = self._get(f"/{kind}/{tmdb_id}", append_to_response="credits")
            crew = (data.get("credits") or {}).get("crew") or []
            cast = [c["name"] for c in (data.get("credits") or {}).get("cast", [])[:10]]
            director = [c["name"] for c in crew if c.get("job") == "Director"][:3]
            writer = [c["name"] for c in crew if c.get("job") in ("Writer", "Screenplay")][:3]
            year = (data.get("release_date") or data.get("first_air_date") or "")[:4]
            videos = []
            if kind == "tv":
                for season in data.get("seasons") or []:
                    sn = season.get("season_number")
                    if sn is None:
                        continue
                    try:
                        sdata = self._cached(f"tmdb:season:{tmdb_id}:{sn}", DETAIL_TTL,
                                             lambda sn=sn: self._get(f"/tv/{tmdb_id}/season/{sn}"))
                    except TmdbError:
                        continue
                    for ep in sdata.get("episodes") or []:
                        videos.append({
                            "id": f"{imdb_id}:{sn}:{ep.get('episode_number')}",
                            "season": sn, "episode": ep.get("episode_number"),
                            "title": ep.get("name") or "", "released": ep.get("air_date") or "",
                            "thumbnail": IMG + ep["still_path"] if ep.get("still_path") else "",
                            "description": ep.get("overview") or "",
                        })
            return {
                "id": imdb_id,
                "imdb_id": imdb_id,
                "name": data.get("title") or data.get("name") or "",
                "year": year,
                "poster": IMG + data["poster_path"] if data.get("poster_path") else "",
                "background": IMG_BIG + data["backdrop_path"] if data.get("backdrop_path") else "",
                "description": data.get("overview") or "",
                "genres": [g["name"] for g in data.get("genres") or []],
                "director": director,
                "writer": writer,
                "cast": cast,
                "imdbRating": data.get("vote_average") or None,
                "runtime": data.get("runtime") or next(iter(data.get("episode_run_time") or []), None),
                "videos": videos,
            }
        return self._cached(f"tmdb:meta:{kind}:{imdb_id}", DETAIL_TTL, load)


if __name__ == "__main__":
    import sys
    api = TmdbApi(sys.argv[1])
    res = api.catalog(sys.argv[2] if len(sys.argv) > 2 else "movie", "popular",
                      search=sys.argv[3] if len(sys.argv) > 3 else "matrix")
    print(json.dumps(res[:3], ensure_ascii=False, indent=2))
