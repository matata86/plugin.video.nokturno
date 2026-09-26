"""Vlastní úložiště — složka s vlastními soubory na WebDAV (NAS, Nextcloud, server Nokturna).

Uživatel zadá adresu složky a případně jméno s heslem (HTTP Basic). Nokturno ji
projde, zapamatuje si seznam videí a hledá v něm stejně jako ve fulltextu
WebShare: podle názvu souboru a podle složek nad ním („Sherlock/S01E02.mkv").

    PROPFIND <složka>  Depth: 1     → seznam položek (WebDAV, RFC 4918)
    GET <složka>                    → HTML výpis, když server WebDAV neumí

`Depth: infinity` se nepoužívá — Apache ho ve výchozím stavu odmítá — a složky
se procházejí po jedné. Seznam se pamatuje hodinu (`INDEX_TTL`), takže nový film
se ve výsledcích objeví nejpozději do hodiny.

Dřív, když úložiště nese značku změny `.nokturno-rev` v kořeni: její obsah je
součástí klíče seznamu, takže jakmile se změní, seznam se načte znovu — značka se
čte nejvýš jednou za `REV_TTL` (minutu), nový soubor se tedy objeví do minuty.
Dashboard Nokturna ji přepisuje po každém nahrání, přesunu a smazání a na
tlačítko „Přeindexovat“. Jiná úložiště ji nemají a platí jen hodinová paměť.

Odkaz na soubor se nosí jako `dav:<slot>:<cesta uvnitř úložiště>`. Adresa
serveru v něm není záměrně: slot se překládá na úložiště z nastavení, takže
z odkazu nejde přesměrovat jinam, a cesta se kontroluje, aby nevedla ven
(`..`). Přehrát jde s hlavičkou `Authorization` — Kodi ji bere za svislítkem
v adrese (`kodi_url`), Stremio přes proxy doplňku (`request`).
"""
import base64
import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

from abort import check as check_stop, gather
from safe_redirect import OPENER as SAFE_OPENER

SLOTS = 3                   # kolik vlastních úložišť jde nastavit
TIMEOUT = 20
INDEX_TTL = 3600            # jak dlouho platí seznam souborů
REV_FILE = ".nokturno-rev"  # značka změny v kořeni úložiště (viz docstring)
REV_TTL = 60                # jak často se značka čte znovu — každý výpis streamů ji jinak četl po síti
MAX_DIRS = 3000             # pojistka proti nekonečnému procházení
MAX_FILES = 50000
CRAWL_WORKERS = 8           # kolik složek se čte souběžně (PROPFIND po jedné byl na velké knihovně pomalý)
REV_TIMEOUT = 3             # značku změny nemá smysl čekat celých TIMEOUT — bez ní se jen projde znovu
# Veřejná instance (doplněk pro Stremio) prochází cizí úložiště zadané v adrese. Bez stropu
# stačí server, který na každý PROPFIND odpovídá pomalu a stále novými podsložkami, a jediný
# požadavek na streamy drží osm vláken hodiny. Soukromé instalace (Kodi, HA) strop nemají —
# tam si uživatel zadává vlastní NAS a velká knihovna se prochází jednou za hodinu.
PUBLIC_CRAWL_DEADLINE = 15  # sekund na celý průchod
PUBLIC_MAX_DIRS = 100       # kolik složek se nejvýš otevře
PUBLIC_TIMEOUT = 8          # sekund na jednu odpověď
UA = "Mozilla/5.0 (compatible; Nokturno/1.0)"
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".wmv", ".webm",
             ".mpg", ".mpeg", ".flv", ".iso")
# systémové složky NAS a košů — videa v nich nejsou a jen zdržují
SKIP_DIRS = {"@eadir", "#recycle", "#snapshot", "lost+found", "$recycle.bin", ".trash", ".thumbnails"}

PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
                 b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/>'
                 b'</d:prop></d:propfind>')


class StorageError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


from streams import fold as _fold, human_size  # noqa: F401


def normalize_url(url):
    """Adresa složky s lomítkem na konci. `dav://`/`davs://` (tvar z Kodi) → http(s).
    Vrací prázdný řetězec, když to adresa úložiště není."""
    url = str(url or "").strip()
    if not url:
        return ""
    low = url.lower()
    if low.startswith("davs://"):
        url = "https://" + url[7:]
    elif low.startswith("dav://"):
        url = "http://" + url[6:]
    elif "://" not in url:
        url = "http://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    try:
        port = parts.port
    except ValueError:   # místní cesta „C:\\Users\\…“ nebo nesmysl za dvojtečkou
        return ""
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    # přihlašovací údaje v adrese se berou jako jméno/heslo, do adresy nepatří
    netloc = parts.hostname + (f":{port}" if port else "")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, "", ""))


def safe_path(path):
    """Cesta uvnitř úložiště bez úniku ven. Vyhodí StorageError, když je podezřelá."""
    text = str(path or "").lstrip("/")
    parts = text.split("/")
    if not text or "\\" in text or "\x00" in text or any(p in ("", ".", "..") for p in parts):
        raise StorageError("neplatná cesta k souboru")
    return text


def match_texts(path):
    """Texty, podle kterých se soubor přiřazuje k titulu: samotný název a název
    s každou ze tří složek nad ním. „Serialy/Sherlock/Season 1/S01E02.mkv" tak
    dá i „Sherlock S01E02.mkv", kde přísný filtr název seriálu najde na začátku."""
    parts = [p for p in str(path).split("/") if p]
    if not parts:
        return []
    name = parts[-1]
    return [name] + [f"{folder} {name}" for folder in reversed(parts[-4:-1])]


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


class StorageApi:
    def __init__(self, url, username="", password="", name="", slot=1, cache=None, index_ttl=INDEX_TTL,
                 opener=None, should_stop=None, crawl_deadline=None, max_dirs=MAX_DIRS, timeout=TIMEOUT):
        """`opener` je volitelný `urllib.request.OpenerDirector` — veřejná instance
        (doplněk pro Stremio) jím hlídá, kam se smí připojit; bez něj se používá
        výchozí `urlopen`. `should_stop`: viz `Engine` a `lib/abort.py` — průchod
        stromu (`_crawl`) se mezi složkami ptá, jestli hostitel nekončí.

        `crawl_deadline` (sekundy), `max_dirs` a `timeout` jsou stropy pro veřejnou
        instanci — viz `PUBLIC_*` výš. Bez nich se prochází, dokud je co (soukromý NAS)."""
        self.base = normalize_url(url)
        if not self.base:
            raise StorageError("neplatná adresa úložiště")
        self.slot = int(slot)
        self.name = str(name or "").strip() or urllib.parse.urlsplit(self.base).hostname
        self.username = str(username or "").strip()
        self._auth = ("Basic " + base64.b64encode(f"{self.username}:{password or ''}".encode("utf-8")).decode("ascii")
                      if self.username else "")
        self._base_path = urllib.parse.unquote(urllib.parse.urlsplit(self.base).path)
        self.cache = cache
        self.index_ttl = index_ttl
        self.opener = opener
        self.should_stop = should_stop
        self.crawl_deadline = crawl_deadline
        self.max_dirs = int(max_dirs or MAX_DIRS)
        self.timeout = int(timeout or TIMEOUT)
        self._rev = (0.0, "")   # (kdy, hodnota) v tomhle procesu — viz revision()

    @property
    def key(self):
        return hashlib.sha1(f"{self.base}|{self.username}".encode("utf-8")).hexdigest()[:12]

    def headers(self):
        return {"Authorization": self._auth} if self._auth else {}

    # --- odkazy -------------------------------------------------------------

    def file_url(self, path):
        return self.base + urllib.parse.quote(safe_path(path))

    def kodi_url(self, path):
        """Adresa pro přehrávač Kodi — hlavičky za svislítkem, tak je Kodi čte."""
        url = self.file_url(path)
        if not self._auth:
            return url
        return f"{url}|Authorization={urllib.parse.quote(self._auth)}&User-Agent={urllib.parse.quote(UA)}"

    def request(self, path):
        """(adresa, hlavičky) pro proxy, která soubor stáhne sama (doplněk pro Stremio)."""
        return self.file_url(path), {**self.headers(), "User-Agent": UA}

    # --- procházení ---------------------------------------------------------

    def _open(self, url, method="GET", headers=None, data=None, timeout=None):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"User-Agent": UA, **self.headers(), **(headers or {})})
        try:
            # výchozí opener při 30x na cizí host nepošle Authorization (lib/safe_redirect.py)
            return (self.opener or SAFE_OPENER).open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code in (401, 403):
                raise StorageError("špatné jméno nebo heslo", e.code) from e
            if e.code == 404:
                raise StorageError("složka neexistuje", e.code) from e
            raise StorageError(f"HTTP {e.code}", e.code) from e
        except Exception as e:  # noqa: BLE001 – síť, DNS, certifikát
            raise StorageError(str(e)[:120]) from e

    def _relative(self, href, dir_url):
        """Absolutní či relativní href → cesta uvnitř úložiště, nebo None mimo něj."""
        path = urllib.parse.unquote(urllib.parse.urlsplit(urllib.parse.urljoin(dir_url, href)).path)
        if not path.startswith(self._base_path):
            return None
        return path[len(self._base_path):]

    def _list_dir(self, rel):
        """[(cesta, je_složka, velikost)] přímo ve složce `rel` (bez ní samé)."""
        dir_url = self.base + urllib.parse.quote(rel)
        try:
            with self._open(dir_url, "PROPFIND", {"Depth": "1", "Content-Type": "application/xml"},
                            PROPFIND_BODY) as resp:
                body = resp.read()
        except StorageError as err:
            if err.status in (400, 405, 501):
                return self._list_html(rel, dir_url)
            raise
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise StorageError("server nevrátil seznam souborů") from e
        out = []
        for response in root.findall("{DAV:}response"):
            href = response.findtext("{DAV:}href") or ""
            path = self._relative(href, dir_url)
            if path is None:
                continue
            is_dir = response.find(".//{DAV:}resourcetype/{DAV:}collection") is not None
            path = path.rstrip("/")
            if path == rel.rstrip("/"):
                continue
            size = response.findtext(".//{DAV:}getcontentlength") or "0"
            out.append((path, is_dir, int(size) if size.isdigit() else 0))
        return out

    def _list_html(self, rel, dir_url):
        with self._open(dir_url) as resp:
            text = resp.read().decode("utf-8", "replace")
        parser = _Links()
        parser.feed(text)
        prefix = rel.rstrip("/") + "/" if rel else ""
        out, seen = [], set()
        for href in parser.hrefs:
            if href.startswith(("?", "#", "mailto:")):
                continue
            path = self._relative(href, dir_url)
            if path is None or not path.startswith(prefix):
                continue
            name = path[len(prefix):]
            is_dir = name.endswith("/")
            name = name.rstrip("/")
            if not name or "/" in name or name in seen:
                continue   # rodičovská složka, řazení sloupců, hlubší odkazy
            seen.add(name)
            out.append((prefix + name, is_dir, 0))
        return out

    def _list_dir_safe(self, rel):
        """Jako `_list_dir`, ale nepřístupná podsložka (ne kořen) vrátí `None`
        místo pádu — nemá shodit procházení celého zbytku stromu."""
        try:
            return self._list_dir(rel + "/" if rel else "")
        except StorageError:
            if not rel:
                raise          # kořen nejde přečíst → úložiště nefunguje
            return None

    def _crawl(self):
        """Prochází strom po vrstvách (BFS) a v rámci jedné vrstvy čte složky
        souběžně (`CRAWL_WORKERS` vláken) — sériově po jedné to na knihovně
        s desítkami/stovkami složek (každý titul ve vlastní) trvalo řádově
        déle, protože každá PROPFIND čekala na tu předchozí."""
        files, dirs = [], 0
        level = [""]
        konec = time.time() + self.crawl_deadline if self.crawl_deadline else None
        while level and dirs < self.max_dirs and len(files) < MAX_FILES:
            check_stop(self.should_stop)
            if konec and time.time() >= konec:
                break   # veřejná instance: vrátit, co je — zbytek se dopočítá při dalším průchodu
            batch = level[:self.max_dirs - dirs]
            dirs += len(batch)
            # ne `pool.map()` ve `with`: to čeká na celou vrstvu (klidně stovky složek),
            # i když hostitel končí — `gather()` se ptá `should_stop()` a nezačaté zruší
            pool = ThreadPoolExecutor(max_workers=CRAWL_WORKERS)
            futures = [pool.submit(self._list_dir_safe, rel) for rel in batch]
            results = [f.result() for f in gather(pool, futures, self.should_stop)]
            next_level = []
            for entries in results:
                if entries is None:
                    continue
                for path, is_dir, size in entries:
                    name = path.rsplit("/", 1)[-1]
                    if name.startswith(".") or _fold(name) in SKIP_DIRS:
                        continue
                    if is_dir:
                        next_level.append(path)
                    elif name.lower().endswith(VIDEO_EXT):
                        files.append({"path": path, "name": name, "size": size, "size_h": human_size(size)})
            # o vrstvu nezpracovaná zbylá dávka (přes MAX_DIRS) se počítá jako oříznutí
            level = level[len(batch):] + next_level
        files.sort(key=lambda f: f["path"].lower())
        return {"ok": True, "files": files, "truncated": bool(level)}

    def _read_revision(self):
        try:
            with self._open(self.base + REV_FILE, headers={"Cache-Control": "no-cache"},
                            timeout=REV_TIMEOUT) as resp:
                return {"rev": resp.read(64).decode("ascii", "ignore").strip()}
        except StorageError:
            return {"rev": ""}

    def revision(self):
        """Obsah značky změny, prázdný řetězec, když ji úložiště nemá nebo nejde přečíst.

        Čte se nejvýš jednou za `REV_TTL`, a to **na disku** (`Store.cached`), ne jen v paměti:
        Kodi spouští plugin jako nový proces při každém kliknutí, takže memo v instanci
        platilo vždycky jen pro jeden výpis a NAS v režimu spánku se budil znovu a znovu —
        dialog streamů čekal na timeout. Čtení má vlastní kratší strop (`REV_TIMEOUT`):
        bez značky se úložiště jen projde znovu, což je lepší než čekat 20 s.
        Neúspěch se cachuje taky, ať se spící NAS nebudí každým výpisem."""
        kdy, hodnota = self._rev
        if time.time() - kdy < REV_TTL:
            return hodnota
        if self.cache is not None:
            hodnota = (self.cache.cached(f"dav:rev:{self.key}", REV_TTL, self._read_revision) or {}).get("rev") or ""
        else:
            hodnota = self._read_revision()["rev"]
        self._rev = (time.time(), hodnota)
        return hodnota

    def index(self):
        """Seznam videí v úložišti, z paměti nejvýš `INDEX_TTL` starý — nebo čerstvý,
        když se od posledního procházení změnila značka `.nokturno-rev`."""
        if self.cache is None:
            return self._crawl()
        rev = re.sub(r"[^0-9A-Za-z._-]", "", self.revision())[:40]
        return self.cache.cached_if(f"dav:index2:{self.key}:{rev}", self.index_ttl, self._crawl,
                                    ok=lambda d: bool(d and d.get("ok")))

    def files(self):
        return (self.index() or {}).get("files") or []

    def search(self, query, limit=40, offset=0):
        """(soubory, celkem) — všechna slova dotazu kdekoli v cestě k souboru."""
        words = re.findall(r"[a-z0-9]+", _fold(query))
        found = [f for f in self.files() if all(w in _fold(f["path"]) for w in words)]
        return found[offset:offset + limit], len(found)

    def check(self):
        """Počet položek v kořeni — ověření adresy a hesla, bez procházení celého stromu."""
        return len(self._list_dir(""))


def parse_ref(url):
    """`dav:<slot>:<cesta>` → (slot, cesta). Vyhodí StorageError, když odkaz nesedí."""
    m = re.match(r"^dav:(\d+):(.+)$", str(url or ""), re.S)
    if not m or not 1 <= int(m.group(1)) <= SLOTS:
        raise StorageError("neplatný odkaz na soubor v úložišti")
    return int(m.group(1)), safe_path(m.group(2))
