"""Klient pro obsah řízený dashboardem Nokturna (`Dashboard/backend`).

Tři veřejné endpointy, které server skládá sám z TMDB (klient nic nedohledává):

* `GET /catalogs` + `GET /catalogs/{slug}` — katalogy, které správce zapne na obrazovce
  Katalogy bez vydání nové verze (sezónní kolekce, ságy, výběry). Menu nese jen
  deklarativní položky: slug, název, druh, umístění a ikonu. **Klient je bere přes
  vlastní whitelist** (`_clean_entry`) — neznámé umístění, druh nebo ikona se zahodí,
  nic ze serveru se nespouští ani nesestavuje do adresy jinak než jako slug.
  Položka s `children` je **složka s podkategoriemi** (Vánoce → Komedie, Rodinné →
  teprve filmy); zanoření se ořízne na `MAX_DEPTH` a počet dětí na `MAX_CHILDREN`,
  ať server nemůže klientovi poslat nekonečné menu. Složku jde otevřít i jako obyčejný
  katalog — server pak vrátí slité položky jejích podkategorií.
* `GET /similar?kind=&id=` — podobné tituly pro uživatele bez vlastního TMDB klíče.
* `GET /tv-program?date=&kind=&channel=` — filmy a seriály v české a slovenské TV,
  jen ty, které server spároval s TMDB (mají `tt…` id).
* `GET /os-key` — klíč k API OpenSubtitles pro titulky (`lib/opensubtitles_api.py`).
  Klíč je vázaný na aplikaci, ne na uživatele, a denní kvóta se počítá na IP toho,
  kdo stahuje — proto ho dostane klient a volá OpenSubtitles přímo, ne přes nás.
  Do repozitáře se nesmí; tudy jde vyměnit bez vydání nové verze doplňku.

Dashboard nesmí zdržet menu doplňku: krátký timeout, výsledky v cache a při výpadku
se vrací poslední známá data (i prošlá) a na pět minut se síť přestane zkoušet
(značka `dash:down` v cache, sdílená i mezi spuštěními pluginu).

Tvar `catalogs()`/`catalog()` je stejný jako u ostatních katalogových klientů
(`TrendApi`, `TmdbApi`), aby šel použít jako `apis[src]`.

Bez závislostí na Kodi — jde testovat samostatně:
    python3 dash_api.py menu | similar movie tt0133093 | tv 2026-09-17
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://nokturno.tailf0014.ts.net"
TIMEOUT = 6
MENU_TTL = 3600
CATALOG_TTL = 6 * 3600
SIMILAR_TTL = 7 * 86400
TV_TTL = 30 * 60
OS_KEY_TTL = 7 * 86400   # klíč se nemění; při výměně se rozejde nejvýš na týden
STALE_TTL = 14 * 86400   # jak staré záložní data ještě ukázat při výpadku
DOWN_TTL = 300
DOWN_KEY = "nokturno:dash:down"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
IMDB_RE = re.compile(r"^tt\d{5,10}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
OS_KEY_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")   # tvar klíče OpenSubtitles
MEDIA_TIMEOUT = 2      # dotaz na hlavičky ze serveru — kratší než strop čtení hlaviček (3 s)
MEDIA_MAX = 50         # identů na jeden dotaz (server víc odmítne)
MEDIA_IDENT_RE = re.compile(r"^(ws|hs|fs|cz):[A-Za-z0-9:_./=-]{1,120}$")
KINDS = ("movie", "series")
PLACEMENTS = ("root", "browse")
# ikony, které klient umí přeložit na obrázek — neznámá se zahodí na výchozí
ICONS = ("", "movies", "series", "star", "top", "new", "family", "christmas", "halloween", "calendar", "trophy")
MAX_TITLE = 60
MAX_DEPTH = 3       # kolik úrovní menu se ze serveru vezme (složka → složka → katalog)
MAX_CHILDREN = 60   # kolik podkategorií na jedné úrovni
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\[\]]")   # [] kvůli formátovacím značkám Kodi ([COLOR] a spol.)


class DashApiError(Exception):
    pass


def _text(value, limit):
    return _CONTROL_RE.sub("", str(value or "")).strip()[:limit]


def _clean_entry(raw, depth=1):
    """Položka menu ze serveru → bezpečný slovník, nebo None. `children` (podkategorie)
    se čistí stejně, jen do hloubky `MAX_DEPTH`; hlubší úrovně se zahodí."""
    if not isinstance(raw, dict):
        return None
    slug, kind, placement = raw.get("slug"), raw.get("kind"), raw.get("placement")
    title = _text(raw.get("title"), MAX_TITLE)
    if not (isinstance(slug, str) and SLUG_RE.match(slug)) or kind not in KINDS or placement not in PLACEMENTS:
        return None
    if not title:
        return None
    icon = raw.get("icon") if raw.get("icon") in ICONS else ""
    children = []
    if depth < MAX_DEPTH and isinstance(raw.get("children"), list):
        for child in raw["children"][:MAX_CHILDREN]:
            entry = _clean_entry(child, depth + 1)
            if entry:
                children.append(entry)
    return {"slug": slug, "title": title, "kind": kind, "placement": placement, "icon": icon,
            "children": children}


def _clean_items(items, ctype):
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and isinstance(it.get("id"), str) and IMDB_RE.match(it["id"]):
            name = _text(it.get("name"), 200)
            out.append({**it, "name": name, "type": ctype, "_title": name})
    return out


class DashApi:
    def __init__(self, cache=None, base=BASE):
        self.cache = cache
        self.base = base

    # --- síť a cache -----------------------------------------------------------------

    def _get(self, path, timeout=TIMEOUT, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, headers={"User-Agent": "Nokturno"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise DashApiError(f"HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001 – síť, DNS, výpadek dashboardu
            raise DashApiError(str(e)[:120]) from e

    def _load(self, key, ttl, fetch):
        """Čerstvá cache → síť → při chybě poslední známá data (až `STALE_TTL`).
        `fetch` vrací data k uložení, nebo None (nic neukládat, nic není)."""
        if self.cache is None:
            try:
                return fetch()
            except DashApiError:
                return None
        fresh = self.cache.peek_cached(key, ttl)
        if fresh is not None:
            return fresh
        if self.cache.peek_cached(DOWN_KEY, DOWN_TTL) is None:
            try:
                return self.cache.cached_if(key, ttl, fetch, ok=lambda d: d is not None, fresh=True)
            except DashApiError:
                self.cache.cached_if(DOWN_KEY, DOWN_TTL, lambda: {"t": int(time.time())}, fresh=True)
        return self.cache.peek_cached(key, STALE_TTL)

    # --- katalogy --------------------------------------------------------------------

    def menu(self, placement=None, ctype=None):
        """Aktivní katalogy z dashboardu (už ověřené whitelistem), volitelně jen pro
        jedno umístění (`root`/`browse`) a druh (`movie`/`series`). Filtruje se jen
        nejvyšší úroveň — podkategorie ve `children` patří ke své složce."""
        def fetch():
            data = self._get("/catalogs")
            return data.get("catalogs") if isinstance(data, dict) and isinstance(data.get("catalogs"), list) else None

        entries = [e for e in (_clean_entry(r) for r in (self._load("nokturno:dash:menu", MENU_TTL, fetch) or []))
                   if e]
        return [e for e in entries
                if (placement is None or e["placement"] == placement) and (ctype is None or e["kind"] == ctype)]

    def group(self, slug):
        """Podkategorie složky `slug` (jedna úroveň). Prázdný seznam = neznámá složka
        nebo katalog bez podkategorií — volající pak nabídne rovnou položky."""
        if not (isinstance(slug, str) and SLUG_RE.match(slug)):
            return []
        level = self.menu()
        for _ in range(MAX_DEPTH):
            match = next((e for e in level if e["slug"] == slug), None)
            if match is not None:
                return match["children"]
            level = [c for e in level for c in e["children"]]
            if not level:
                break
        return []

    def catalogs(self, ctype):
        return [{"id": e["slug"], "name": e["title"], "search": False, "genre_required": False, "genres": []}
                for e in self.menu(ctype=ctype)]

    def catalog(self, ctype, cid, genre=None, search=None, skip=0):
        """Celý katalog najednou (server drží nejvýš 60 položek) — bez hledání a stránek."""
        if not (isinstance(cid, str) and SLUG_RE.match(cid)) or search or skip:
            return []

        def fetch():
            data = self._get(f"/catalogs/{cid}")
            return data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), list) else None

        return _clean_items(self._load(f"nokturno:dash:catalog:{cid}", CATALOG_TTL, fetch), ctype)

    # --- podobné tituly --------------------------------------------------------------

    def similar(self, ctype, imdb_id):
        if not (isinstance(imdb_id, str) and IMDB_RE.match(imdb_id)):
            return []
        kind = "series" if ctype == "series" else "movie"

        def fetch():
            data = self._get("/similar", kind=kind, id=imdb_id)
            return data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), list) else None

        return _clean_items(self._load(f"nokturno:dash:similar:{kind}:{imdb_id}", SIMILAR_TTL, fetch), ctype)

    # --- TV program ------------------------------------------------------------------

    def tv_program(self, day=None, kind=None, channel=None):
        """Program jednoho televizního dne (05:00–05:00). Vrací slovník s `dates`,
        `channels` a `items` (každá položka má `meta` ve tvaru katalogu), nebo None."""
        day = day if isinstance(day, str) and DAY_RE.match(day) else None
        kind = kind if kind in KINDS else None
        channel = channel if isinstance(channel, str) and CHANNEL_RE.match(channel) else None

        def fetch():
            data = self._get("/tv-program", date=day, kind=kind, channel=channel)
            return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None

        data = self._load(f"nokturno:dash:tv:{day}:{kind}:{channel}", TV_TTL, fetch)
        if not data:
            return None
        items = []
        for it in data.get("items") or []:
            if not isinstance(it, dict) or it.get("kind") not in KINDS:
                continue
            meta = _clean_items([it.get("meta")], it["kind"])
            try:
                start, stop = int(it.get("start")), int(it.get("stop"))
            except (TypeError, ValueError):
                continue
            if not meta:
                continue
            items.append({
                "channel": _text(it.get("channel"), 40), "channel_name": _text(it.get("channel_name"), 40),
                "start": start, "stop": stop, "kind": it["kind"], "title": _text(it.get("title"), 200),
                "episode_title": _text(it.get("episode_title"), 200),
                "season": it.get("season") if isinstance(it.get("season"), int) else None,
                "episode": it.get("episode") if isinstance(it.get("episode"), int) else None,
                "meta": meta[0],
            })
        channels = [{"slug": c["slug"], "name": _text(c.get("name"), 40)} for c in data.get("channels") or []
                    if isinstance(c, dict) and isinstance(c.get("slug"), str) and CHANNEL_RE.match(c["slug"])]
        dates = [d for d in data.get("dates") or [] if isinstance(d, str) and DAY_RE.match(d)]
        today = data.get("today") if isinstance(data.get("today"), str) and DAY_RE.match(data["today"]) else None
        date = data.get("date") if isinstance(data.get("date"), str) and DAY_RE.match(data["date"]) else None
        return {"today": today, "date": date, "dates": dates, "channels": channels, "items": items}


    # --- klíč k OpenSubtitles --------------------------------------------------------

    def opensubtitles_key(self):
        """Klíč k API OpenSubtitles, nebo prázdno. Prázdno = funkce je prostě vypnutá
        (server klíč nemá nastavený, nebo je dashboard nedostupný) — doplněk se pak
        chová jako dřív a titulky bere jen ze zdroje a z WebShare."""
        def fetch():
            data = self._get("/os-key")
            klic = data.get("key") if isinstance(data, dict) else None
            return {"key": klic} if isinstance(klic, str) and OS_KEY_RE.match(klic) else None

        data = self._load("nokturno:dash:os-key", OS_KEY_TTL, fetch) or {}
        klic = data.get("key") or ""
        return klic if OS_KEY_RE.match(str(klic)) else ""

    # --- hlavičky souborů ze společné cache serveru ------------------------------------

    def media(self, idents):
        """`{ident: hlavička}` pro soubory, které už někdo jiný přečetl — ze společné
        cache Stremia na serveru (`GET /media?ids=`). Jen trefy; co server nezná, si
        klient přečte sám jako dřív. Prázdný slovník při výpadku, a na `DOWN_TTL`
        se síť nezkouší — čtení hlaviček má strop 3 s a server ho nesmí prožrat.

        Výsledek se tu **necachuje**: volající ho zapíše pod týž klíč `media:<ident>`,
        pod kterým by ležela vlastní přečtená hlavička, takže zbytek kódu nepozná rozdíl.
        Idents jen ze zdrojů, kde jeden ident je tentýž soubor pro každého (`engine.SHARED_MEDIA`).
        """
        idents = [i for i in dict.fromkeys(idents) if isinstance(i, str) and MEDIA_IDENT_RE.match(i)][:MEDIA_MAX]
        if not idents:
            return {}
        if self.cache is not None and self.cache.peek_cached(DOWN_KEY, DOWN_TTL) is not None:
            return {}
        try:
            data = self._get("/media", timeout=MEDIA_TIMEOUT, ids=",".join(idents))
        except DashApiError:
            if self.cache is not None:
                self.cache.cached_if(DOWN_KEY, DOWN_TTL, lambda: {"t": int(time.time())}, fresh=True)
            return {}
        hits = (data or {}).get("hits") if isinstance(data, dict) else None
        if not isinstance(hits, dict):
            return {}
        # jen tvar, který dává `mediainfo.probe()` — server je cizí vstup jako každý jiný
        return {i: v for i, v in hits.items() if i in idents and isinstance(v, dict)
                and (v.get("audio") or v.get("height") or v.get("size"))}


if __name__ == "__main__":
    import sys
    api = DashApi()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "menu"
    if cmd == "similar":
        for m in api.similar(sys.argv[2], sys.argv[3]):
            print(m["id"], m.get("name"), m.get("year"))
    elif cmd == "tv":
        data = api.tv_program(sys.argv[2] if len(sys.argv) > 2 else None)
        for it in (data or {}).get("items", []):
            print(time.strftime("%H:%M", time.localtime(it["start"])), it["channel_name"], it["title"])
    else:
        print(api.menu())
