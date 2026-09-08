"""Klient Stremio API Sosáče (stremio.sosac.tv).

Sosáč má vlastní katalogy a vlastní ID (`sosac2_123:movies`, `sosac2_456:serials`,
epizody `sosac2_789:S:E`); streamy vrací jen pro tato ID. Názvy jsou složené:
„Perníkový táta - (Breaking Bad) - CZ, EN + CZ titulky - (2008)“ → rozkládáme
je na titul, originální název, jazyky a rok, aby šly porovnat s TMDB tituly Luny.
"""
import json
import re
import unicodedata
import urllib.parse
import urllib.request

TIMEOUT = 40
ID_PREFIX = "sosac2_"
NAME_RE = re.compile(r"^(?P<title>.+?)(?:\s+-\s+\((?P<orig>[^()]+)\))?(?:\s+-\s+(?P<langs>[^()]+?))?\s*(?:-\s*)?\((?P<year>\d{4})\)\s*$")


def is_sosac_id(item_id):
    return str(item_id or "").startswith(ID_PREFIX)


ARTICLES = ("the ", "a ", "an ")


def normalize(text):
    """Bez diakritiky, malá písmena, jen alfanumerika, bez úvodního členu – pro porovnání názvů.

    TMDB (Luna) má „Matrix“, Sosáč „The Matrix“ – bez odstranění členu by se nepotkaly.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    for art in ARTICLES:
        if text.startswith(art):
            text = text[len(art):]
            break
    return text


def names_match(a, b):
    na, nb = normalize(a), normalize(b)
    return bool(na) and na == nb


def parse_name(name):
    """'Matrix Reloaded - (The Matrix Reloaded) - CZ - (2003)' → dict(title, orig, langs, year)."""
    m = NAME_RE.match(name or "")
    if not m:
        return {"title": (name or "").strip(), "orig": "", "langs": "", "year": ""}
    return {
        "title": m.group("title").strip(),
        "orig": (m.group("orig") or "").strip(),
        "langs": (m.group("langs") or "").strip(),
        "year": m.group("year") or "",
    }


class SosacError(Exception):
    pass


class SosacApi:
    def __init__(self, base_url, user_id):
        self.base = base_url.rstrip("/")
        self.user_id = user_id

    def _get(self, path, extra_query=None):
        query = {"userId": self.user_id}
        if extra_query:
            query.update(extra_query)
        url = f"{self.base}/{path}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Kodi plugin.video.luna"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise SosacError(f"{e} ({url})") from e

    def manifest(self):
        return self._get("manifest.json")

    def catalogs(self, ctype):
        result = []
        for c in self.manifest().get("catalogs", []):
            if c.get("type") != ctype:
                continue
            cid = c.get("id", "")
            extra = {e.get("name"): e for e in c.get("extra", [])}
            if "search" in extra:
                continue
            name = re.sub(r"^Sosac\s*-\s*", "", c.get("name") or cid)
            result.append({
                "id": cid,
                "name": name,
                "search": False,
                "genre_required": bool(extra.get("genre", {}).get("isRequired")),
                "genres": extra.get("genre", {}).get("options") or [],
            })
        return result

    def catalog(self, ctype, cid, genre=None, search=None, skip=0):
        extra = []
        if search:
            extra.append("search=" + urllib.parse.quote(search, safe=""))
        if genre:
            extra.append("genre=" + urllib.parse.quote(genre, safe=""))
        if skip:
            extra.append(f"skip={int(skip)}")
        path = f"catalog/{ctype}/{cid}" + ("/" + "&".join(extra) if extra else "") + ".json"
        metas = self._get(path).get("metas") or []
        for m in metas:
            self.enrich(m)
        return metas

    def search(self, ctype, query):
        cid = "sosac2-movies-search" if ctype == "movie" else "sosac2-series-search"
        return self.catalog(ctype, cid, search=query)

    def meta(self, ctype, item_id):
        meta = self._get(f"meta/{ctype}/{item_id}.json").get("meta") or {}
        return self.enrich(meta)

    def streams(self, ctype, item_id):
        streams = self._get(f"stream/{ctype}/{item_id}.json").get("streams") or []
        for s in streams:
            s["source"] = "sosac"
            s["label"] = "Sosáč " + (s.get("title") or s.get("quality") or "")
            s["detail"] = ""
        return streams

    @staticmethod
    def enrich(meta):
        """Doplní rozložený název (title/year) a rating v měřítku 0–10, jak čeká Kodi."""
        parsed = parse_name(meta.get("name") or "")
        meta["_title"] = parsed["title"]
        meta["_orig"] = parsed["orig"]
        meta["_langs"] = parsed["langs"]
        if parsed["year"] and not meta.get("year"):
            meta["year"] = parsed["year"]
        rating = meta.get("imdbRating")
        try:
            rating = float(rating)
            meta["imdbRating"] = rating / 10 if rating > 10 else rating
        except (TypeError, ValueError):
            meta.pop("imdbRating", None)
        for key in ("language", "country"):
            if isinstance(meta.get(key), list):
                meta[key] = ", ".join(str(x) for x in meta[key])
        return meta

    # --- hledání napříč: najít protějšek titulu z Luny (TMDB) -----------------
    def find_match(self, ctype, title, year=None, orig_title=None):
        """Vrátí Sosáč meta se stejným názvem (a rokem ±1, pokud je znám), jinak None."""
        wanted = {normalize(title)}
        if orig_title:
            wanted.add(normalize(orig_title))
        wanted.discard("")
        candidates = []
        for q in list({title, orig_title} - {None, ""}):
            try:
                candidates.extend(self.search(ctype, q))
            except SosacError:
                continue
        for m in candidates:
            names = {normalize(m.get("_title")), normalize(m.get("_orig"))} - {""}
            if not (names & wanted):
                continue
            if year and m.get("year"):
                try:
                    if abs(int(m["year"]) - int(year)) > 1:
                        continue
                except ValueError:
                    pass
            return m
        return None

    def episode_id(self, series_id, season, episode):
        for v in self.meta("series", series_id).get("videos") or []:
            if int(v.get("season") or 0) == int(season) and int(v.get("episode") or 0) == int(episode):
                return v.get("id")
        return None


if __name__ == "__main__":
    import sys

    api = SosacApi("https://stremio.sosac.tv/cs", sys.argv[1])
    print([c["name"] for c in api.catalogs("movie")][:5])
    m = api.find_match("movie", "Matrix", 1999, "The Matrix")
    print("match:", m and (m["id"], m["_title"], m["year"], m.get("imdbRating")))
    print("streams:", [(s["label"], s["url"][:40]) for s in api.streams("movie", m["id"])] if m else None)
    print("epizoda:", api.episode_id("sosac2_476:serials", 1, 2))
