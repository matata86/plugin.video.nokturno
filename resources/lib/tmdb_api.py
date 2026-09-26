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
import re
import urllib.error
import urllib.parse
import urllib.request

_IMDB_RE = re.compile(r"^tt\d{1,12}$")
_TMDB_ID_RE = re.compile(r"^tmdb:(\d{1,10})$")
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
# katalogy bez žánrů — trendy za týden a výběr podle roku (rok je v roli „žánru“)
EXTRA_CATALOGS = {"trending": "Trendy tento týden", "year": "Podle roku"}
FIRST_YEAR = 1920


class TmdbError(Exception):
    pass


def _certification(kind, raw):
    """Věkový rating ze `release_dates` (film) nebo `content_ratings` (seriál) —
    přednostně český, jinak americký (nejběžnější, skoro vždy vyplněný)."""
    zeme = raw.get("results") or []
    if kind == "movie":
        def pro_zemi(kod):
            polozka = next((z for z in zeme if z.get("iso_3166_1") == kod), None)
            for vydani in (polozka or {}).get("release_dates") or []:
                if vydani.get("certification"):
                    return vydani["certification"]
            return ""
        return pro_zemi("CZ") or pro_zemi("US")
    def pro_zemi_serial(kod):
        polozka = next((z for z in zeme if z.get("iso_3166_1") == kod), None)
        return (polozka or {}).get("rating") or ""
    return pro_zemi_serial("CZ") or pro_zemi_serial("US")


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
        import datetime
        genres = list(self._genres(ctype).values())
        years = [str(y) for y in range(datetime.date.today().year, FIRST_YEAR - 1, -1)]
        return [{"id": cid, "name": name, "search": False, "genre_required": False, "genres": genres}
                for cid, name in CATALOGS.items()] + [
            {"id": "trending", "name": EXTRA_CATALOGS["trending"], "search": False, "genre_required": False,
             "genres": []},
            {"id": "year", "name": EXTRA_CATALOGS["year"], "search": False, "genre_required": True,
             "genres": years},
        ]

    def _details(self, kind, tmdb_id):
        """Detail titulu s `external_ids` a `images` jedním dotazem (`append_to_response`) —
        dřív dva dotazy na položku, stránka katalogu = 1 + 40 požadavků, teď 1 + 20."""
        return self._cached(f"tmdb:detail:{kind}:{tmdb_id}", DETAIL_TTL,
                            lambda: self._get(f"/{kind}/{tmdb_id}", append_to_response="external_ids,images",
                                              include_image_language="cs,en,null")) or {}

    def _imdb_id(self, ctype, tmdb_id, details=None):
        data = details if details is not None else self._details(self._kind(ctype), tmdb_id)
        return ((data.get("external_ids") or {}).get("imdb_id")) or ""

    def imdb_id(self, ctype, tmdb_id):
        """`id z TMDB → tt…`, prázdno když TMDB titul nezná nebo IMDb id nemá.

        Celý doplněk stojí na IMDb id (podle něj se hledá ve zdrojích), ale klienti
        Stremia posílají u titulů z TMDB katalogů `tmdb:<id>` — viz
        `nokturno-stremio/nokturno/routes.py`. Detail je cachovaný (`_details`),
        takže je to jeden dotaz na titul, ne na požadavek.
        """
        return self._imdb_id(ctype, tmdb_id)

    def _art(self, kind, tmdb_id, background_path="", images=None):
        """Náhled (`landscapePoster`) a logo z obrázků TMDB.

        Skin (Arctic Fuse) kreslí v náhledu `landscape`, a když chybí, vezme tentýž
        obrázek jako na pozadí — u titulů Nokturna tak byl stejný obrázek dvakrát
        (Sám doma). Náhled je obrázek z filmu s názvem (jazyk cs/en), jinak nejlépe
        hodnocený jiný než pozadí; logo přednostně české. Obrázky se nemění, cache dlouhá."""
        data = images if images is not None else (self._details(kind, tmdb_id).get("images") or {})

        def best(items, langs):
            for lang in langs:
                rows = [i for i in items if i.get("iso_639_1") == lang and i.get("file_path")]
                if rows:
                    return max(rows, key=lambda i: i.get("vote_average") or 0)["file_path"]
            return ""

        backdrops = [b for b in data.get("backdrops") or [] if b.get("file_path") != background_path]
        landscape = best(backdrops, ("cs", "en")) or best(backdrops, (None,))
        logo = best(data.get("logos") or [], ("cs", "en", None))
        return {
            "landscapePoster": IMG_BIG + landscape if landscape else "",
            "logo": IMG + logo if logo else "",
        }

    def _item(self, ctype, raw, genre_map):
        kind = self._kind(ctype)
        try:
            details = self._details(kind, raw["id"])
            imdb_id = self._imdb_id(ctype, raw["id"], details)
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
                "ratingSource": "tmdb" if raw.get("vote_average") else None,
                "voteCount": int(raw.get("vote_count") or 0),   # v odpovědi katalogu zdarma, žádný dotaz navíc
                **self._art(kind, raw["id"], raw.get("backdrop_path") or "", images=details.get("images") or {}),
            }
        except Exception:
            return None   # jeden vadný titul (nedostupný, napůl vyplněná odpověď TMDB…) nesmí shodit stránku katalogu

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
        elif cid == "trending":
            raw = self._cached(f"tmdb:trending:{kind}:{page}", SEARCH_TTL,
                               lambda: self._get(f"/trending/{kind}/week", page=page).get("results") or [])
        elif cid == "year":
            field = "primary_release_year" if kind == "movie" else "first_air_date_year"
            params = {"sort_by": "popularity.desc", "page": page, field: str(genre or "")[:4]}
            raw = self._get(f"/discover/{kind}", **params).get("results") or []
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

    def similar(self, ctype, imdb_id, limit=40):
        """Podobné tituly: TMDB doporučení (podle toho, co sledují lidé se stejným
        titulem), doplněná o `/similar` (žánry a klíčová slova), když doporučení je
        málo. Bez samotného titulu a bez duplicit, tvar jako `catalog()`."""
        kind = self._kind(ctype)

        def load():
            found = self._get(f"/find/{imdb_id}", external_source="imdb_id")
            results = found.get(f"{kind}_results") or []
            if not results:
                return []
            tmdb_id = results[0]["id"]
            raw = list(self._get(f"/{kind}/{tmdb_id}/recommendations").get("results") or [])
            if len(raw) < 10:
                raw += self._get(f"/{kind}/{tmdb_id}/similar").get("results") or []
            seen, out = {tmdb_id}, []
            for r in raw:
                if r.get("id") and r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)
            return out[:limit]

        raw = self._cached(f"tmdb:similar:{kind}:{imdb_id}", SEARCH_TTL * 14, load)
        genre_map = self._genres(ctype)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            items = list(pool.map(lambda r: self._item(ctype, r, genre_map), raw))
        return [i for i in items if i and i["id"] != imdb_id]

    def brief(self, ctype, imdb_id):
        """Popis, hodnocení, žánry a obrázky česky jedním dotazem (`/find`) — pro
        `enrich()` u výpisů, kde plný `meta()` (u seriálu všechny sezóny) je zbytečně drahý."""
        if not _IMDB_RE.match(str(imdb_id or "")):
            raise TmdbError(f"neplatné IMDb id: {str(imdb_id)[:20]!r}")
        kind = self._kind(ctype)

        def load():
            found = self._get(f"/find/{imdb_id}", external_source="imdb_id")
            results = found.get(f"{kind}_results") or []
            if not results:
                return {}
            raw, genre_map = results[0], self._genres(ctype)
            return {
                "description": raw.get("overview") or "",
                "imdbRating": raw.get("vote_average") or None,
                "ratingSource": "tmdb" if raw.get("vote_average") else None,
                "genres": [g for g in (genre_map.get(gid, "") for gid in raw.get("genre_ids") or []) if g],
                "poster": IMG + raw["poster_path"] if raw.get("poster_path") else "",
                "background": IMG_BIG + raw["backdrop_path"] if raw.get("backdrop_path") else "",
                "year": (raw.get("release_date") or raw.get("first_air_date") or "")[:4],
            }
        data = self._cached(f"tmdb:brief:{kind}:{imdb_id}", DETAIL_TTL, load)
        if data and data.get("imdbRating"):
            data["ratingSource"] = "tmdb"   # i záznamy z cache před 8.4.0, jinak by je enrich označil za IMDb
        return data

    def meta(self, ctype, imdb_id):
        """Detail podle `tt…` id (přes TMDB `/find`) — titul, popis, žánry, obsazení,
        u seriálu i epizody (`videos`, stejný tvar jako Luna/Cinemeta).

        Bere i `tmdb:<id>` – titul, který IMDb id nemá (nové české a slovenské seriály).
        Metadata pak nesou `id` ve stejném tvaru, `imdb_id` prázdné a `_orig` s původním
        názvem, ať zdroje hledající podle názvu mají z čeho skládat dotazy."""
        own = _TMDB_ID_RE.match(str(imdb_id or ""))
        if not own and not _IMDB_RE.match(str(imdb_id or "")):
            # id posílá i klient (Stremio) — jde do cesty URL, `../` by měnilo endpoint
            raise TmdbError(f"neplatné IMDb id: {str(imdb_id)[:20]!r}")
        kind = self._kind(ctype)
        # u filmu je certifikace v `release_dates` (podle země a uvedení), u seriálu
        # v `content_ratings` (jedna hodnota na zemi) — jiný název i tvar odpovědi
        ratings_key = "release_dates" if kind == "movie" else "content_ratings"

        def load():
            if own:
                tmdb_id = own.group(1)
            else:
                found = self._get(f"/find/{imdb_id}", external_source="imdb_id")
                results = found.get(f"{kind}_results") or []
                if not results:
                    raise TmdbError(f"titul {imdb_id} v TMDB nenalezen")
                tmdb_id = results[0]["id"]
            data = self._get(f"/{kind}/{tmdb_id}", append_to_response=f"credits,images,videos,{ratings_key}",
                             include_image_language="cs,en,null")
            crew = (data.get("credits") or {}).get("crew") or []
            cast = [{"name": c.get("name") or "", "character": c.get("character") or "",
                    "photo": IMG + c["profile_path"] if c.get("profile_path") else ""}
                   for c in (data.get("credits") or {}).get("cast", [])[:10]]
            director = [c["name"] for c in crew if c.get("job") == "Director"][:3]
            writer = [c["name"] for c in crew if c.get("job") in ("Writer", "Screenplay")][:3]
            year = (data.get("release_date") or data.get("first_air_date") or "")[:4]
            videos = []
            if kind == "tv":
                numbers = [s.get("season_number") for s in data.get("seasons") or [] if s.get("season_number") is not None]

                def season(sn):
                    try:
                        return sn, self._cached(f"tmdb:season:{tmdb_id}:{sn}", DETAIL_TTL,
                                                lambda: self._get(f"/tv/{tmdb_id}/season/{sn}"))
                    except TmdbError:
                        return sn, {}
                # sezóny souběžně — seriál s 20 sezónami dřív znamenal 20 dotazů za sebou
                with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                    seasons = list(pool.map(season, numbers))
                for sn, sdata in seasons:
                    for ep in sdata.get("episodes") or []:
                        videos.append({
                            "id": f"{imdb_id}:{sn}:{ep.get('episode_number')}",
                            "season": sn, "episode": ep.get("episode_number"),
                            "title": ep.get("name") or "", "released": ep.get("air_date") or "",
                            "thumbnail": IMG + ep["still_path"] if ep.get("still_path") else "",
                            "description": ep.get("overview") or "",
                        })
            trailer_id = ""
            for v in (data.get("videos") or {}).get("results") or []:
                if v.get("site") == "YouTube" and v.get("type") == "Trailer":
                    trailer_id = v["key"]
                    break
            if not trailer_id:  # bez oficiálního traileru vezmi aspoň první YouTube video (teaser, klip)
                trailer_id = next((v["key"] for v in (data.get("videos") or {}).get("results") or []
                                   if v.get("site") == "YouTube" and v.get("key")), "")
            return {
                "id": imdb_id,
                "imdb_id": "" if own else imdb_id,
                "name": data.get("title") or data.get("name") or "",
                **({"_orig": data.get("original_title") or data.get("original_name") or ""} if own else {}),
                "year": year,
                "poster": IMG + data["poster_path"] if data.get("poster_path") else "",
                "background": IMG_BIG + data["backdrop_path"] if data.get("backdrop_path") else "",
                "description": data.get("overview") or "",
                "genres": [g["name"] for g in data.get("genres") or []],
                "director": director,
                "writer": writer,
                "cast": cast,
                "imdbRating": data.get("vote_average") or None,
                "ratingSource": "tmdb" if data.get("vote_average") else None,
                "voteCount": int(data.get("vote_count") or 0),
                "mpaa": _certification(kind, data.get(ratings_key) or {}),
                "trailerYoutubeId": trailer_id,
                "runtime": data.get("runtime") or next(iter(data.get("episode_run_time") or []), None),
                "videos": videos,
                **self._art(kind, tmdb_id, data.get("backdrop_path") or "", images=data.get("images") or {}),
            }
        return self._cached(f"tmdb:meta3:{kind}:{imdb_id}", DETAIL_TTL, load)


if __name__ == "__main__":
    import sys
    api = TmdbApi(sys.argv[1])
    res = api.catalog(sys.argv[2] if len(sys.argv) > 2 else "movie", "popular",
                      search=sys.argv[3] if len(sys.argv) > 3 else "matrix")
    print(json.dumps(res[:3], ensure_ascii=False, indent=2))
