"""Sosáč napřímo — bez Stremio doplňku a bez přihlášení k Sosáči.

Katalogy, hledání, seriály a epizody jsou veřejné JSON exporty `tv.sosac.to`
(stejné, jaké používá oficiální Kodi doplněk). Streamy dává `streamuj.tv`:
`json_api_player.php?action=get-video-links` vrátí odkazy podle jazyka a
kvality, GET na odkaz s `pass=uživatel:::md5(md5(heslo))` vrátí finální mp4.
Potřeba je jen účet Streamuj (pro premium rychlost; bez něj hraje omezeně).

ID: filmy `sosacd_m_<streamuj id>`, seriály `sosacd_s_<číslo>`, epizody
`sosacd_s_<číslo>:S:E`. Stejné rozhraní jako SosacApi (catalogs/catalog/search/
meta/streams/find_match/episode_id), aby default.py nemusel rozlišovat.
"""
import hashlib
import json
import re
import urllib.parse
import urllib.request

from sosac_api import SosacError, names_match, normalize  # Kodi načítá lib ploše, ne jako balíček

BASE = "http://tv.sosac.to"
EXPORT = BASE + "/vystupy5981/"
STREAMUJ_API = "https://www.streamuj.tv/json_api_player.php?"
LIST_TTL = 3 * 3600   # žebříčky (nejpopulárnější, nově přidané)
IMAGE_MOVIE = "https://movies.sosac.tv/images/75x109/movie-"
IMAGE_MOVIE_BIG = "https://movies.sosac.tv/images/558x313/movie-"
IMAGE_SERIES = "https://movies.sosac.tv/images/558x313/serial-"
TIMEOUT = 40
ID_PREFIX = "sosacd_"
LETTERS = list("abcdefghijklmnopqrstuvwxyz") + ["0-9"]
QUALITY_ORDER = ("UHD", "FHD", "HD", "SD")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Kodi plugin.video.nokturno"

MOVIE_LISTS = [
    ("moviesmostpopular", "Nejpopulárnější filmy"),
    ("moviesrecentlyadded", "Nově přidané filmy"),
]
SERIES_LISTS = [
    ("tvshowsmostpopular", "Nejpopulárnější seriály"),
    ("tvshowsrecentlyadded", "Nově přidané epizody"),
]


def is_direct_id(item_id):
    return str(item_id or "").startswith(ID_PREFIX)


def streamuj_hash(password):
    """`pass=` na streamuj = md5(md5(heslo)); 32 hex znaků bere jako hotový hash."""
    password = (password or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", password.lower()):
        return password.lower()
    return hashlib.md5(hashlib.md5(password.encode("utf-8")).hexdigest().encode()).hexdigest()


# SosacError se dědí ze sosac_api — dvě stejnojmenné třídy by se navzájem nechytaly
class SosacDirect:
    def __init__(self, streamuj_user="", streamuj_pass="", cache=None, cache_ttl=600, index_store=None):
        self.user = (streamuj_user or "").strip()
        self.password = (streamuj_pass or "").strip()
        self.cache = cache
        self.cache_ttl = cache_ttl
        self.index = index_store  # objekt s remember_item/item – snímky filmů pro meta()

    # --- HTTP ---------------------------------------------------------------
    def _get(self, url, ttl=None):
        def load():
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                raise SosacError(f"{e} ({url})") from e
        if self.cache is None:
            return load()
        return self.cache.cached(url, ttl or self.cache_ttl, load)

    # --- převod položek ----------------------------------------------------
    @staticmethod
    def _name(n):
        if isinstance(n, dict):
            return n.get("cs") or n.get("en") or next(iter(n.values()), "")
        return str(n or "")

    @staticmethod
    def _orig(n):
        return n.get("en", "") if isinstance(n, dict) else ""

    def movie_meta(self, v):
        link = v.get("l") or ""
        meta = {
            "id": ID_PREFIX + "m_" + link,
            "type": "movie",
            "name": self._name(v.get("n")),
            "_title": self._name(v.get("n")),
            "_orig": self._orig(v.get("n")),
            "year": str(v.get("y") or ""),
            "poster": IMAGE_MOVIE_BIG + v["i"] if v.get("i") else "",
            "background": "",
            "description": v.get("p") or "",
            "genres": v.get("g") or [],
            "_dub": v.get("d") or [],
            "_subs": v.get("s") or [],
            "_quality": v.get("q") or "",
            "_link": link,
        }
        if v.get("m"):
            meta["imdb_id"] = "tt" + str(v["m"]).zfill(7)
        try:
            if v.get("r"):
                meta["imdbRating"] = float(v["r"]) * 2
        except (TypeError, ValueError):
            pass
        if self.index is not None and link:
            self.index.remember_item("idx:" + meta["id"], meta)
        return meta

    def series_meta(self, v):
        url = v.get("l") or ""
        sid = url.rstrip("/").split("/")[-1].replace(".json", "")
        meta = {
            "id": ID_PREFIX + "s_" + sid,
            "type": "series",
            "name": self._name(v.get("n")),
            "_title": self._name(v.get("n")),
            "_orig": self._orig(v.get("n")),
            "year": str(v.get("y") or ""),
            "poster": IMAGE_SERIES + v["i"] if v.get("i") else "",
            "background": IMAGE_SERIES + v["i"] if v.get("i") else "",
            "description": v.get("p") or "",
            "genres": v.get("g") or [],
            "_episodes_url": url,
        }
        if v.get("m"):
            meta["imdb_id"] = "tt" + str(v["m"]).zfill(7)
        try:
            if v.get("r"):
                meta["imdbRating"] = float(v["r"]) * 2
        except (TypeError, ValueError):
            pass
        if self.index is not None:
            self.index.remember_item("idx:" + meta["id"], meta)
        return meta

    # --- katalogy ----------------------------------------------------------
    def catalogs(self, ctype):
        result = []
        if ctype == "movie":
            for cid, name in MOVIE_LISTS:
                result.append({"id": cid, "name": name, "search": False, "genre_required": False, "genres": []})
            genres = self._get(EXPORT + "souboryzanry.json", ttl=86400)
            result.append({"id": "genre", "name": "Podle žánru", "search": False, "genre_required": True,
                           "genres": list(genres.keys())})
            result.append({"id": "az", "name": "Podle písmene", "search": False, "genre_required": True,
                           "genres": [x.upper() for x in LETTERS]})
        else:
            for cid, name in SERIES_LISTS:
                result.append({"id": cid, "name": name, "search": False, "genre_required": False, "genres": []})
            result.append({"id": "tvaz", "name": "Podle písmene", "search": False, "genre_required": True,
                           "genres": [x.upper() for x in LETTERS]})
        return result

    def catalog(self, ctype, cid, genre=None, search=None, skip=0, page=100):
        if search:
            return self.search(ctype, search)
        if cid == "genre":
            genres = self._get(EXPORT + "souboryzanry.json", ttl=86400)
            url = genres.get(genre) or ""
            if not url:
                return []
            items = [self.movie_meta(v) for v in self._get(url)]
        elif cid == "az":
            items = [self.movie_meta(v) for v in self._get(EXPORT + f"souboryaz/{(genre or 'a').lower()}.json")]
        elif cid == "tvaz":
            items = [self.series_meta(v) for v in self._get(EXPORT + f"tvpismena/{(genre or 'a').lower()}.json")]
        # žebříčky se mění pomalu a služba je na pozadí zahřívá po třech hodinách —
        # kratší TTL by znamenalo, že uživatel stejně trefí studenou cache
        elif cid == "tvshowsrecentlyadded":
            items = [self.episode_meta(v) for v in self._get(EXPORT + cid + ".json", ttl=LIST_TTL)]
        elif ctype == "series":
            items = [self.series_meta(v) for v in self._get(EXPORT + cid + ".json", ttl=LIST_TTL)]
        else:
            items = [self.movie_meta(v) for v in self._get(EXPORT + cid + ".json", ttl=LIST_TTL)]
        items = [m for m in items if m]
        return items[skip:skip + page]

    def episode_meta(self, v):
        """Položka z „nově přidané epizody“ – jen k přehrání, bez vazby na seriál."""
        link = v.get("l")
        if not link:
            return None
        title = self._name(v.get("t"))
        ep = self._name(v.get("n"))
        meta = {
            "id": ID_PREFIX + "m_" + link,
            "type": "movie",
            "name": f"{title} {int(v.get('s') or 0)}x{int(v.get('e') or 0):02d} {ep}".strip(),
            "_title": title,
            "_orig": "",
            "year": "",
            "poster": BASE + v["i"] if v.get("i") else "",
            "description": ep,
            "_link": link,
        }
        if self.index is not None:
            self.index.remember_item("idx:" + meta["id"], meta)
        return meta

    # --- hledání -------------------------------------------------------------
    def search(self, ctype, query):
        if ctype == "movie":
            data = self._get(BASE + "/jsonsearchapi.php?q=" + urllib.parse.quote_plus(query), ttl=12 * 3600)
            return [self.movie_meta(v) for v in data if v.get("l")]
        # seriály nemají vyhledávací endpoint → projít písmena (cache 1 den) a filtrovat podle názvu
        q = normalize(query)
        found = []
        for letter in LETTERS:
            try:
                data = self._get(EXPORT + f"tvpismena/{letter}.json", ttl=86400)
            except SosacError:
                continue
            for v in data:
                names = [normalize(self._name(v.get("n"))), normalize(self._orig(v.get("n")))]
                if any(q and q in n for n in names):
                    found.append(self.series_meta(v))
        return found[:60]

    @staticmethod
    def _short_title(title):
        """„Okresní přebor – Poslední zápas Pepika Hnátka“ → „Okresní přebor“.
        Fulltext Sosáče na celý název s podtitulem nic nenajde."""
        for sep in (" – ", " — ", " - ", ": "):
            head = (title or "").split(sep)[0].strip()
            if head and head != title and len(head) >= 3:
                return head
        return ""

    def find_match(self, ctype, title, year=None, orig_title=None):
        candidates = []
        queries = {title, orig_title, self._short_title(title), self._short_title(orig_title or "")}
        for q in list(queries - {None, ""}):
            try:
                candidates.extend(self.search(ctype, q))
            except SosacError:
                continue
        for m in candidates:
            if not (names_match(m.get("_title"), title) or names_match(m.get("_orig"), title)
                    or (orig_title and (names_match(m.get("_title"), orig_title) or names_match(m.get("_orig"), orig_title)))):
                continue
            if year and m.get("year"):
                try:
                    if abs(int(m["year"]) - int(year)) > 1:
                        continue
                except ValueError:
                    pass
            return m
        return None

    # --- meta -----------------------------------------------------------------
    def meta(self, ctype, item_id):
        if item_id.startswith(ID_PREFIX + "s_"):
            sid = item_id[len(ID_PREFIX) + 2:]
            base = (self.index.item("idx:" + item_id) if self.index is not None else None) or {
                "id": item_id, "type": "series", "name": sid, "_title": sid, "_orig": "", "year": "",
                "_episodes_url": EXPORT + f"serialy/{sid}.json"}
            meta = dict(base)
            meta["videos"] = self.episodes(sid)
            return meta
        snap = self.index.item("idx:" + item_id) if self.index is not None else None
        if snap:
            return dict(snap)
        raise SosacError(f"neznámý titul {item_id}")

    def episodes(self, sid):
        data = self._get(EXPORT + f"serialy/{sid}.json")
        videos = []
        for block in data:
            for season, eps in block.items():
                for ep, v in eps.items():
                    videos.append({
                        "id": f"{ID_PREFIX}s_{sid}:{int(season)}:{int(ep)}",
                        "season": int(season),
                        "episode": int(ep),
                        "title": v.get("n") or f"Epizoda {ep}",
                        "thumbnail": BASE + v["i"] if v.get("i") else "",
                        "_link": v.get("l") or "",
                    })
        videos.sort(key=lambda x: (x["season"], x["episode"]))
        return videos

    def episode_id(self, series_id, season, episode):
        for v in self.meta("series", series_id).get("videos") or []:
            if v["season"] == int(season) and v["episode"] == int(episode):
                return v["id"]
        return None

    # --- streamy -------------------------------------------------------------
    def _link_for(self, ctype, item_id):
        parts = item_id.split(":")
        if len(parts) >= 3 and parts[-1].isdigit():
            sid = ":".join(parts[:-2])[len(ID_PREFIX) + 2:]
            for v in self.episodes(sid):
                if v["season"] == int(parts[-2]) and v["episode"] == int(parts[-1]):
                    return v["_link"]
            return ""
        if item_id.startswith(ID_PREFIX + "m_"):
            return item_id[len(ID_PREFIX) + 2:]
        return ""

    def streams(self, ctype, item_id):
        link = self._link_for(ctype, item_id)
        if not link:
            return []
        url = STREAMUJ_API + urllib.parse.urlencode({
            "action": "get-video-links", "d": 19, "link": link,
            "login": self.user or "x", "password": streamuj_hash(self.password) if self.password else "x",
            "location": 1,
        })
        data = self._get(url, ttl=120)
        urls = data.get("URL") or {}
        if not self.password and data.get("errormessage"):
            pass  # bez účtu Streamuj hraje jen ukázka – hlásí se v popisku streamu
        streams = []
        for lang, quals in urls.items():
            if not isinstance(quals, dict):
                continue
            subs = quals.get("subtitles") if isinstance(quals.get("subtitles"), dict) else {}
            sub_urls = ["streamuj:" + u for u in subs.values() if isinstance(u, str) and u]
            qualities = [(q, u) for q, u in quals.items() if isinstance(u, str) and u]
            qualities.sort(key=lambda kv: QUALITY_ORDER.index(kv[0]) if kv[0] in QUALITY_ORDER else 9)
            for quality, link_url in qualities:
                streams.append({
                    "url": "streamuj:" + link_url,   # finální mp4 se dohledá až při přehrání (resolve)
                    "source": "sosac",
                    "label": f"Sosáč {lang} - {quality}",
                    "detail": ("Tit.: " + ", ".join(subs.keys())) if subs else "",
                    "quality": quality,
                    "subtitles": sub_urls,
                })
        return streams

    def resolve(self, url):
        """'streamuj:<odkaz>' → finální mp4 (GET odkazu vrací URL v těle)."""
        link = url[len("streamuj:"):] if url.startswith("streamuj:") else url
        if self.user and self.password:
            link += ("&" if "?" in link else "?") + "pass=" + urllib.parse.quote(f"{self.user}:::{streamuj_hash(self.password)}", safe=":")
        req = urllib.request.Request(link, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace").strip()
                final = body if body.startswith("http") else resp.geturl()
        except Exception as e:  # noqa: BLE001
            raise SosacError(f"streamuj: {e}") from e
        return final


if __name__ == "__main__":
    import sys

    api = SosacDirect(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else "")
    print("katalogy:", [c["name"] for c in api.catalogs("movie")], [c["name"] for c in api.catalogs("series")])
    ms = api.search("movie", "matrix")
    print("hledání:", [(m["id"][:22], m["name"], m["year"], m.get("imdb_id")) for m in ms[:3]])
    st = api.streams("movie", ms[0]["id"])
    print("streamy:", [(s["label"], s["url"][:60]) for s in st[:4]])
    if st:
        print("resolve:", api.resolve(st[0]["url"])[:90])
    ss = api.search("series", "perníkový")
    print("seriály:", [(m["id"], m["name"]) for m in ss[:2]])
    if ss:
        eps = api.meta("series", ss[0]["id"])["videos"]
        print("epizody:", len(eps), eps[0])
        print("stream epizody:", [(s["label"], s["url"][:50]) for s in api.streams("series", eps[0]["id"])][:2])
