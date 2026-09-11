"""HellSpy — hledání videí a odkaz na přehrání.

Veřejné rozhraní `api.hellspy.to/gw/`, bez účtu a bez tokenu:

    GET gw/search?query=<text>&limit=&offset=      → {"items": [...], "nextOffset": N}
    GET gw/video/<id>/<fileHash>/download          → 302 na podepsaný odkaz CDN

Bere se **původní soubor**, ne překódování. Rozhraní umí obojí: `gw/video/<id>/<hash>`
vrací `conversions` s 720p a 1080p, jenže k šestigigovému 4K souboru nabídne 720p,
takže by kvalita v seznamu lhala. Odkaz z `/download` vede na nahraný soubor v plné
velikosti, umí `Range` (Kodi tedy umí převíjet) a neměřil jsem na něm žádné škrcení.

Odkaz je podepsaný a časově omezený, dohledává se proto až při přehrání
(`hs:<id>:<hash>`), stejně jako u WebShare.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.hellspy.to/gw/"
TIMEOUT = 20
SEARCH_TTL = 12 * 3600
UA = "Mozilla/5.0 (compatible; Nokturno/1.0)"


class HellspyError(Exception):
    pass


def human_size(nbytes):
    try:
        gb = int(nbytes) / 2 ** 30
    except (TypeError, ValueError):
        return ""
    return f"{gb:.1f} GB" if gb >= 1 else f"{int(nbytes) / 2 ** 20:.0f} MB"


class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    """Přesměrování se nemá následovat — zajímá nás cílová adresa, ne obsah.
    Bez tohohle by urllib stáhl začátek filmu jen proto, aby zahodil hlavičky."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HellspyApi:
    def __init__(self, cache=None, cache_ttl=SEARCH_TTL):
        self.cache = cache
        self.cache_ttl = cache_ttl
        self._opener = urllib.request.build_opener(_KeepRedirect)

    def _get(self, path, **params):
        url = API + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise HellspyError(f"HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001 – síť, DNS, rozsypaný JSON
            raise HellspyError(str(e)[:120]) from e

    def search(self, what, limit=40, offset=0):
        """(soubory, další offset). Bere jen videa — `objectType` se kontroluje,
        aby případný nový typ v odpovědi nepropadl dál jako stream."""
        def load():
            data = self._get("search", query=what, limit=limit, offset=offset)
            files = []
            for i in data.get("items") or []:
                if i.get("objectType") != "GWSearchVideo" or not i.get("fileHash"):
                    continue
                size = int(i.get("size") or 0)
                files.append({
                    "id": i.get("id"),
                    "hash": i.get("fileHash"),
                    "name": i.get("title") or "",
                    "size": size,
                    "size_h": human_size(size),
                    "duration": int(i.get("duration") or 0),
                })
            return files, int(data.get("nextOffset") or 0)
        if self.cache is None:
            return load()
        return self.cache.cached(f"hs:search:{what}:{limit}:{offset}", self.cache_ttl, load)

    def file_link(self, file_id, file_hash):
        """Podepsaný odkaz na původní soubor. Vytažený z hlavičky přesměrování."""
        url = f"{API}video/{file_id}/{file_hash}/download"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with self._opener.open(req, timeout=TIMEOUT) as resp:
                link = resp.headers.get("Location") or ""
        except urllib.error.HTTPError as e:
            link = e.headers.get("Location") or ""
            if not link:
                raise HellspyError(f"HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001 – síť, DNS
            raise HellspyError(str(e)[:120]) from e
        if not link:
            raise HellspyError("odkaz na soubor se nevrátil")
        return link


if __name__ == "__main__":
    import sys

    api = HellspyApi()
    found, nxt = api.search(sys.argv[1] if len(sys.argv) > 1 else "matrix", limit=5)
    print(f"nalezeno {len(found)}, další offset {nxt}")
    for f in found:
        print(f"  {f['size_h']:>9} | {f['name'][:58]}")
    if found:
        print("odkaz:", api.file_link(found[0]["id"], found[0]["hash"])[:80])
