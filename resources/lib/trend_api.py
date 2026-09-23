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
import time
import urllib.error
import urllib.request

from servers import BASE, urlopen as open_url
TIMEOUT = 10
CACHE_TTL = 8 * 3600   # stejná platnost jako cache na serveru — kratší nemá smysl
# Když dashboard neodpovídá, čekalo se `TIMEOUT` při každém otevření menu Filmy/Seriály.
# Značka v cache (sdílená i mezi spuštěními pluginu) to na `DOWN_TTL` přeskočí — stejný
# vzor jako v `dash_api.py`, odkud je převzatý.
DOWN_TTL = 300
DOWN_KEY = "nokturno:trend:down"
STALE_TTL = 14 * 86400   # jak staré záložní data ještě ukázat při výpadku
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
            with open_url(req, timeout=TIMEOUT) as resp:
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

        def fetch():
            data = self._get(kind)
            return [i for i in (data.get("items") or []) if i.get("id")] or None

        items = self._cached(f"nokturno:trending:{kind}", fetch) or []
        return [{**it, "type": ctype, "_title": it.get("name") or ""} for it in items]

    def _cached(self, key, fetch):
        """Čerstvá cache → rovnou. Jinak dotaz, ale jen když dashboard neselhal
        v posledních `DOWN_TTL` sekundách; při výpadku se vrátí i starší data."""
        if self.cache is None:
            try:
                return fetch()
            except TrendApiError:
                return None
        fresh = self.cache.peek_cached(key, CACHE_TTL)
        if fresh is not None:
            return fresh
        if self.cache.peek_cached(DOWN_KEY, DOWN_TTL) is None:
            try:
                return self.cache.cached_if(key, CACHE_TTL, fetch, ok=lambda d: d is not None, fresh=True)
            except TrendApiError:
                self.cache.cached_if(DOWN_KEY, DOWN_TTL, lambda: {"t": int(time.time())}, fresh=True)
        return self.cache.peek_cached(key, STALE_TTL)


if __name__ == "__main__":
    import sys
    api = TrendApi()
    for m in api.catalog(sys.argv[1] if len(sys.argv) > 1 else "movie", CATALOG_ID):
        print(m["id"], m.get("name"), m.get("year"))
