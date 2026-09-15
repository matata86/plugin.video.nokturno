"""Klient pro vlastní žebříček Nokturna (`Dashboard/backend`, veřejný `GET /trending`).

Na rozdíl od TMDB/Cinemety/Luny netahá z cizí databáze, ale z toho, co si sami
uživatelé Nokturna pouštějí (Kodi + HA + Stremio dohromady), vážené podle
čerstvosti (24 h/7 dní/30 dní zpětně, `Dashboard/backend/stats.py::stats_trending`).
Server k položkám rovnou dohledává TMDB id/plakát/popis, takže tenhle klient je
stejně "hloupý" jako `CinemetaApi`/`LunaApi` — žádné dohledávání tady, jen JSON.

Tvar výstupu (`catalogs()`/`catalog()`) je záměrně stejný jako u ostatních
katalogových klientů, aby šel použít jako drop-in náhrada všude, kde se dnes
indexuje `apis[src]`.

Bez závislostí na Kodi — jde testovat samostatně:
    python3 trend_api.py movie
"""
import json
import urllib.error
import urllib.request

BASE = "https://nokturno.tailf0014.ts.net"
TIMEOUT = 10
CACHE_TTL = 8 * 3600   # stejná platnost jako cache na serveru — kratší nemá smysl
CATALOG_ID = "nejsledovanejsi"
CATALOG_NAME = "Nejsledovanější tento týden"


class TrendApiError(Exception):
    pass


class TrendApi:
    def __init__(self, cache=None, base=BASE):
        self.cache = cache
        self.base = base

    def _get(self, kind):
        url = f"{self.base}/trending?kind={kind}"
        req = urllib.request.Request(url, headers={"User-Agent": "Nokturno"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 – síť, DNS, výpadek dashboardu
            raise TrendApiError(str(e)[:120]) from e

    def catalogs(self, ctype):
        return [{"id": CATALOG_ID, "name": CATALOG_NAME, "search": False, "genre_required": False, "genres": []}]

    def catalog(self, ctype, cid, genre=None, search=None, skip=0):
        """Jedna stránka (žebříček má nejvýš 50 položek, „Další" nemá co nabídnout)
        — vyhledávání a žánry tenhle katalog nepodporuje."""
        if cid != CATALOG_ID or search or skip:
            return []
        kind = "series" if ctype == "series" else "movie"

        def load():
            try:
                data = self._get(kind)
            except TrendApiError:
                return []
            return [i for i in (data.get("items") or []) if i.get("id")]

        items = self.cache.cached(f"nokturno:trending:{kind}", CACHE_TTL, load) if self.cache else load()
        return [{**it, "type": ctype, "_title": it.get("name") or ""} for it in items]


if __name__ == "__main__":
    import sys
    api = TrendApi()
    for m in api.catalog(sys.argv[1] if len(sys.argv) > 1 else "movie", CATALOG_ID):
        print(m["id"], m.get("name"), m.get("year"))
