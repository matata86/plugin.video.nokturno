"""Klient Prowlarru — hledání na torrentových trackerech.

Prowlarr drží definice trackerů (včetně přihlášení) a nabízí nad nimi jedno
API. Nokturno se tak nemusí starat o HTML jednotlivých stránek, které se mění.

Bez závislostí na Home Assistantu — jde testovat samostatně:
    python3 prowlarr.py http://192.168.1.10:9696 <api-klíč> "Na samotě u lesa"
"""
import json
import re
import urllib.parse
import urllib.request

TIMEOUT = 30
CAT_MOVIE = 2000
CAT_SERIES = 5000

# „Na.samote.u.lesa.1976.1080p.BluRay.x264-SKUPINA“ → rok, kvalita, velikost
YEAR_RE = re.compile(r"(?:^|[.\s(\[_-])(19\d{2}|20\d{2})(?:[.\s)\]_-]|$)")
QUALITY_RE = re.compile(r"\b(2160p|1080p|720p|480p|4K|UHD)\b", re.I)
# Trackery hledají fulltextem přes celý název souboru, takže interpunkce z názvu
# titulu („Okresní přebor – Poslední zápas…“) dotaz spolehlivě zabije.
PUNCT_RE = re.compile(r"[\u2010-\u2015\-:;,.!?()\[\]{}\"'/\\|]+")
WORDS_SHORT = 3

# Značky sezón a dílů, jak je píšou názvy souborů: „S02E10“, „S02.E10“, „2x10“,
# „Season 2“, „S01-S05“. Bez nich se u seriálu nedá poznat, co torrent obsahuje.
EP_RE = re.compile(r"\bS(\d{1,2})\s*[._ -]?\s*E(\d{1,3})\b", re.I)
X_RE = re.compile(r"\b(\d{1,2})[xX](\d{1,3})\b")
SEASON_RE = re.compile(r"\bS(?:eason[. _-]*)?(\d{1,2})\b(?![. _-]*E?\d)", re.I)
SEASON_RANGE_RE = re.compile(r"\bS(\d{1,2})\s*[-\u2013]\s*S?(\d{1,2})\b", re.I)
SEASON_TAIL_RE = re.compile(r"S\d{1,2}(?:E\d{1,3})?$", re.I)


def episode_match(title, season, episode=None):
    """Sedí torrent na hledaný díl?

    Balík celé sezóny se počítá — díl v něm je. Jiná sezóna ne, a název bez
    jakékoli značky sezóny taky ne: u seriálu je to nejspíš něco jiného."""
    if season is None:
        return True
    text = title or ""
    for first, last in SEASON_RANGE_RE.findall(text):
        if int(first) <= season <= int(last):
            return True
    for found, num in EP_RE.findall(text) + X_RE.findall(text):
        if int(found) == season and (episode is None or int(num) == episode):
            return True
    return any(int(num) == season for num in SEASON_RE.findall(text))


class ProwlarrError(Exception):
    pass


class ProwlarrApi:
    def __init__(self, base_url, api_key, timeout=TIMEOUT):
        self.base = (base_url or "").rstrip("/")
        self.key = (api_key or "").strip()
        self.timeout = timeout

    def _get(self, path, params=None):
        if not self.base or not self.key:
            raise ProwlarrError("Prowlarr není nastavený.")
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, headers={
            "X-Api-Key": self.key,
            "User-Agent": "Home Assistant Nokturno",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise ProwlarrError("Prowlarr odmítl API klíč.") from err
            raise ProwlarrError(f"Prowlarr vrátil HTTP {err.code}.") from err
        except Exception as err:  # noqa: BLE001 – síť, DNS, rozbitý JSON
            raise ProwlarrError(f"Prowlarr není dostupný: {err}") from err

    def ping(self):
        """Vrátí jméno a verzi, nebo vyhodí ProwlarrError."""
        data = self._get("/api/v1/system/status") or {}
        return f"{data.get('appName', 'Prowlarr')} {data.get('version', '')}".strip()

    def indexers(self):
        """Nastavené trackery — bez nich hledání nic nevrátí."""
        return [{"id": i.get("id"), "name": i.get("name"), "enable": bool(i.get("enable"))}
                for i in (self._get("/api/v1/indexer") or [])]

    @staticmethod
    def clean(query):
        """Dotaz bez interpunkce — pomlčka v názvu titulu jinak nenajde nic."""
        return " ".join(PUNCT_RE.sub(" ", query or "").split())

    @staticmethod
    def shorten(query):
        """Kratší dotaz pro druhý pokus: pár prvních slov a rok, pokud v dotazu je.

        Dlouhé podtituly („… Poslední zápas Pepika Hnátka“) se v názvech souborů
        na trackerech často zkracují nebo píšou jinak."""
        words = query.split()
        # koncovka se drží: bez „S02E01“ by zkrácený dotaz našel kteroukoli sezónu
        tail = ""
        if words and ((words[-1].isdigit() and len(words[-1]) == 4) or SEASON_TAIL_RE.match(words[-1])):
            tail = words[-1]
        head = [w for w in words if w != tail][:WORDS_SHORT]
        return " ".join(head + ([tail] if tail else []))

    def _raw(self, query, ctype, limit):
        # U seriálu se ptáme i na filmovou kategorii: české trackery tam balíky
        # sezón běžně zařazují. Nesouvisející nálezy stejně vyhodí filtr sezóny.
        cats = [CAT_SERIES, CAT_MOVIE] if ctype == "series" else [CAT_MOVIE]
        return self._get("/api/v1/search", {
            "query": query,
            "categories": cats,
            "type": "search",
            "limit": max(1, min(int(limit), 100)),
        }) or []

    def search(self, query, ctype="movie", limit=30, season=None, episode=None):
        """Výsledky trackerů seřazené podle seedů (nejlíp dostupné první).

        Se `season` se výsledky filtrují: fulltext trackerů vrací i jiné sezóny
        a balík S01 na díl 2x01 nesedí."""
        query = self.clean(query)
        if not query:
            return []
        found = self._raw(query, ctype, limit)
        if not found:
            short = self.shorten(query)
            if short and short != query:
                found = self._raw(short, ctype, limit)
        out = [self._item(row) for row in found if isinstance(row, dict)]
        out = [row for row in out if row["url"]]
        if season is not None:
            out = [row for row in out if episode_match(row["title"], season, episode)]
            if episode is not None:
                # konkrétní díl napřed, balík celé sezóny až za ním — stahovat
                # kvůli jednomu dílu deset hodin videa nemá smysl
                out.sort(key=lambda r: (bool(EP_RE.search(r["title"]) or X_RE.search(r["title"])),
                                        r["seeders"]), reverse=True)
        out.sort(key=lambda r: (r["seeders"], r["size_gb"] or 0), reverse=True)
        return out[:limit]

    @staticmethod
    def _item(row):
        title = str(row.get("title") or "").strip()
        size = row.get("size") or 0
        quality = QUALITY_RE.search(title)
        year = YEAR_RE.search(title)
        return {
            "title": title,
            # magnet je lepší: nepotřebuje stažení .torrent souboru přes přihlášení
            "url": row.get("magnetUrl") or row.get("downloadUrl") or "",
            "indexer": str(row.get("indexer") or "").strip(),
            "seeders": int(row.get("seeders") or 0),
            "leechers": int(row.get("leechers") or 0),
            "size_gb": round(size / 1073741824, 2) if size else None,
            "quality": (quality.group(1).upper() if quality else ""),
            "year": int(year.group(1)) if year else None,
        }


if __name__ == "__main__":
    import sys
    api = ProwlarrApi(sys.argv[1], sys.argv[2])
    print(api.ping())
    print("trackery:", api.indexers())
    for row in api.search(sys.argv[3] if len(sys.argv) > 3 else "Matrix"):
        print(f"{row['seeders']:>4} seedů  {str(row['size_gb']) + ' GB':>10}  "
              f"{row['quality']:>6}  {row['indexer']:<16} {row['title'][:70]}")
