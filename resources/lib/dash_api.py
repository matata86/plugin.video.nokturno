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
* `GET /concerts?sources=` + `GET /concerts/{id}?sources=` — katalog koncertů: interpreti a pod nimi
  koncerty se soubory jako hotové vnitřní odkazy (`ws:`/`hs:`/`fs:`). Koncert nemá IMDb id,
  proto jde mimo běžné katalogy; klient pošle zapnuté zdroje a dostane jen to, co umí přehrát.
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

from servers import BASE, urlopen as open_url
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
CONCERTS_TTL = 6 * 3600
CONCERT_SOURCES = ("webshare", "hellspy", "fastshare")
MAX_GENRE = 24
CONCERT_REF_RE = re.compile(r"^(ws|hs|fs):[A-Za-z0-9:_./=-]{1,160}$")
# ikony, které klient umí přeložit na obrázek — neznámá se zahodí na výchozí
ICONS = ("", "movies", "series", "star", "top", "new", "family", "christmas", "halloween", "calendar", "trophy",
         "fairytale", "comedy", "romance", "animation")
MAX_TITLE = 60
MAX_DEPTH = 3       # kolik úrovní menu se ze serveru vezme (složka → složka → katalog)
MAX_CHILDREN = 60   # kolik podkategorií na jedné úrovni
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\[\]]")   # [] kvůli formátovacím značkám Kodi ([COLOR] a spol.)


class DashApiError(Exception):
    pass


def _text(value, limit):
    return _CONTROL_RE.sub("", str(value or "")).strip()[:limit]


def _img(value):
    """Náhled ze zdroje. Jen http(s) adresa — Kodi bere v `setArt` i `special://` a cesty
    k souborům, takže cizí řetězec se sem pouštět nesmí."""
    url = _text(value, 300)
    return url if url.startswith("http://") or url.startswith("https://") else ""


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
            with open_url(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise DashApiError(f"HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001 – síť, DNS, výpadek dashboardu
            raise DashApiError(str(e)[:120]) from e

    def _load(self, key, ttl, fetch):
        """Čerstvá cache → síť → při chybě poslední známá data (až `STALE_TTL`).
        `fetch` vrací data k uložení, nebo None (nic neukládat, nic není).

        Značka výpadku (`DOWN_KEY`) šetří čekání na timeout, ale **jen tam, kde je co
        ukázat místo toho**. Bez starých dat by kvůli ní uživatel dostal prázdno za
        chybu, která mohla dávno minout: Kodi na Androidu po startu chvíli nemá síť,
        zahřívání na pozadí tam narazí a značka pak pět minut umlčí i výpisy, které
        uživatel otevře rukou. Tahle podmínka to stála jednou celý rozcestník koncertů
        (2026-09-23, Office). Ruční výpis proto zaplatí nejvýš jeden timeout (6 s)."""
        if self.cache is None:
            try:
                return fetch()
            except DashApiError:
                return None
        fresh = self.cache.peek_cached(key, ttl)
        if fresh is not None:
            return fresh
        stale = self.cache.peek_cached(key, STALE_TTL)
        if stale is None or self.cache.peek_cached(DOWN_KEY, DOWN_TTL) is None:
            try:
                return self.cache.cached_if(key, ttl, fetch, ok=lambda d: d is not None, fresh=True)
            except DashApiError:
                self.cache.cached_if(DOWN_KEY, DOWN_TTL, lambda: {"t": int(time.time())}, fresh=True)
        return stale

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

    # --- koncerty --------------------------------------------------------------------

    def concert_groups(self, sources, install=""):
        """Žánry a počáteční písmena s počty (`GET /concerts/groups`) — rozcestník nad dvěma
        sty jmen. Starší server tuhle cestu nezná a odpoví 404, klient pak nabídne rovnou
        celý seznam."""
        srcs = ",".join(sorted(s for s in sources if s in CONCERT_SOURCES))

        def fetch():
            data = self._get("/concerts/groups", sources=srcs, install=install)
            return data if isinstance(data, dict) and isinstance(data.get("genres"), list) else None

        data = self._load(f"nokturno:dash:concertgroups:{srcs}", CONCERTS_TTL, fetch)
        if not data:
            return None
        def skupiny(klic):
            out = []
            for g in data.get(klic) or []:
                name = _text(g.get("name"), MAX_GENRE) if isinstance(g, dict) else ""
                if name:
                    out.append({"name": name, "artists": int(g.get("artists") or 0)})
            return out
        return {"artists": int(data.get("artists") or 0),
                "genres": skupiny("genres"), "letters": skupiny("letters")}

    def concerts(self, sources, install="", genre="", letter=""):
        """Interpreti s koncerty v zapnutých zdrojích (`GET /concerts?sources=`). Server
        klíčuje koncert názvem, ne IMDb id, a soubor nese hotový vnitřní odkaz
        (`ws:`/`hs:`/`fs:`), který klient rovnou předá `Engine.resolve()`. Zdroje jdou do
        klíče cache — jiná sada zapnutých zdrojů = jiný seznam. `install` (id instalace
        ze statistik) posílá klient kvůli zkušebnímu provozu: server smí katalog vydat
        jen vybraným instalacím, ostatním odpoví 404 (= None, nic se necachuje)."""
        srcs = ",".join(sorted(s for s in sources if s in CONCERT_SOURCES))

        def fetch():
            data = self._get("/concerts", sources=srcs, install=install,
                             **({"genre": genre} if genre else {}), **({"letter": letter} if letter else {}))
            return data.get("artists") if isinstance(data, dict) and isinstance(data.get("artists"), list) else None

        out = []
        klic = f"nokturno:dash:concerts:{srcs}:{genre}:{letter}"
        for a in self._load(klic, CONCERTS_TTL, fetch) or []:
            if isinstance(a, dict) and isinstance(a.get("id"), int) and a["id"] > 0:
                name = _text(a.get("name"), MAX_TITLE)
                if name:
                    out.append({"id": a["id"], "name": name, "concerts": int(a.get("concerts") or 0),
                                "genres": [g for g in (a.get("genres") or [])
                                           if isinstance(g, str)][:4],
                                "letter": _text(a.get("letter"), 1)})
        return out

    def concert_artist(self, artist_id, sources, install=""):
        """Koncerty jednoho interpreta i se soubory — jen odkazy známého tvaru."""
        if not isinstance(artist_id, int) or artist_id <= 0:
            return None
        srcs = ",".join(sorted(s for s in sources if s in CONCERT_SOURCES))

        def fetch():
            data = self._get(f"/concerts/{artist_id}", sources=srcs, install=install)
            return data if isinstance(data, dict) and isinstance(data.get("concerts"), list) else None

        data = self._load(f"nokturno:dash:concerts:{artist_id}:{srcs}", CONCERTS_TTL, fetch)
        if not data:
            return None
        concerts = []
        for c in data["concerts"]:
            if not isinstance(c, dict):
                continue
            files = [{"source": f["source"], "ref": f["ref"], "name": _text(f.get("name"), 200),
                      "size": int(f.get("size") or 0), "duration": int(f.get("duration") or 0),
                      # náhled je cizí URL — jen http(s), ať se z něj nestane `special://` ani soubor
                      "img": _img(f.get("img")), "width": int(f.get("width") or 0),
                      "height": int(f.get("height") or 0)}
                     for f in (c.get("files") or []) if isinstance(f, dict)
                     and f.get("source") in CONCERT_SOURCES and isinstance(f.get("ref"), str)
                     and CONCERT_REF_RE.match(f["ref"])]
            title = _text(c.get("title"), 200)
            if files and title:
                year = c.get("year") if isinstance(c.get("year"), int) else None
                concerts.append({"title": title, "year": year, "files": files})
        artist = data.get("artist") if isinstance(data.get("artist"), dict) else {}
        return {"artist": _text(artist.get("name"), MAX_TITLE), "concerts": concerts}

    def concert_items(self, sources, search="", skip=0, install=""):
        """Plochý seznam koncertů napříč interprety (`GET /concerts/items`) — pro klienta bez
        hierarchie (Stremio). Bez souborů; ty dá `concert()`. Vrací (položky, celkem)."""
        srcs = ",".join(sorted(s for s in sources if s in CONCERT_SOURCES))
        search = " ".join(str(search or "").split())[:80]
        skip = max(0, int(skip or 0))

        def fetch():
            data = self._get("/concerts/items", sources=srcs, search=search, skip=skip, install=install)
            return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None

        data = self._load(f"nokturno:dash:concerts:items:{srcs}:{search}:{skip}", CONCERTS_TTL, fetch)
        if not data:
            return [], 0
        out = []
        for i in data["items"]:
            if not (isinstance(i, dict) and isinstance(i.get("id"), int) and i["id"] > 0):
                continue
            artist, title = _text(i.get("artist"), MAX_TITLE), _text(i.get("title"), 200)
            if artist and title:
                out.append({"id": i["id"], "artist": artist, "title": title,
                            "year": i.get("year") if isinstance(i.get("year"), int) else None,
                            "sources": [s for s in (i.get("sources") or []) if s in CONCERT_SOURCES]})
        return out, int(data.get("total") or 0)

    def concert(self, concert_id, sources, install=""):
        """Jeden koncert se soubory (`GET /concerts/item/{id}`), stejná kontrola odkazů
        jako u `concert_artist()`."""
        if not isinstance(concert_id, int) or concert_id <= 0:
            return None
        srcs = ",".join(sorted(s for s in sources if s in CONCERT_SOURCES))

        def fetch():
            data = self._get(f"/concerts/item/{concert_id}", sources=srcs, install=install)
            return data if isinstance(data, dict) and isinstance(data.get("files"), list) else None

        data = self._load(f"nokturno:dash:concert:{concert_id}:{srcs}", CONCERTS_TTL, fetch)
        if not data:
            return None
        files = [{"source": f["source"], "ref": f["ref"], "name": _text(f.get("name"), 200),
                  "size": int(f.get("size") or 0), "duration": int(f.get("duration") or 0)}
                 for f in data["files"] if isinstance(f, dict)
                 and f.get("source") in CONCERT_SOURCES and isinstance(f.get("ref"), str)
                 and CONCERT_REF_RE.match(f["ref"])]
        artist, title = _text(data.get("artist"), MAX_TITLE), _text(data.get("title"), 200)
        if not (files and artist and title):
            return None
        return {"id": concert_id, "artist": artist, "title": title,
                "year": data.get("year") if isinstance(data.get("year"), int) else None, "files": files}


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
