"""FastShare.cz — hledání videí a odkaz na přehrání.

Rozhraní je totéž, které používá oficiální doplněk pro Kodi (repozitář
`https://kodi.fastshare.cloud/`, `plugin.video.fastshare` 1.0.7, `main.py`):

    GET api_kodi.php?process=login&login=&password=   → {"user": {"hash", "unlimited", "data": {"value": MB}}}
                                                         špatný účet: HTTP 401 {"response": "INVALID_LOGIN"}
    GET api_kodi.php?process=search&term=&pagination=&adult=false
                                                      → {"search": {"total", "file": [...]}}

Hledání účet nechce. Soubor (`download_url`, `https://data<N>.fastshare.cloud/download.php?id=`)
se stahuje s cookie `FASTSHARE=<hash>` z přihlášení a **odečítá se z kreditu** účtu,
pokud nemá neomezený tarif — a to za přenesená data (změřeno: hlavičky ~24 souborů 22 MB).
Rozlišení a stopáž posílá hledání, zvuk ne. Ten se čte z hlavičky souboru (`Engine._fill_audio`)
jen u účtu s neomezeným stahováním; kdo jede na kredit, má jazyk jen odhadem z názvu.

Odkaz pro přehrání se skládá až při přehrání (`fs:<id>:<server>:<velikost>`): hash
se po čase mění a přehrávače Stremia cookie neumí poslat (jde přes proxy doplňku).
"""
import hashlib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from streams import human_size

API = "https://fastshare.cz/api/api_kodi.php"
TIMEOUT = 20
SEARCH_TTL = 12 * 3600
HASH_TTL = 6 * 3600        # hash z přihlášení — jak dlouho platí, API neříká
HASH_STORE = "fastshare_hash"
UA = "Mozilla/5.0 (compatible; Nokturno/4.0)"
# datový server z `download_url` — do odkazu jde jen číslo, adresa se skládá tady,
# ať `fs:` odkaz z cizí adresy (Stremio) nevede kamkoli jinam
SERVER_RE = re.compile(r"^https://(data\d{1,3})\.fastshare\.cloud/download\.php\?id=(\d+)$")
REF_RE = re.compile(r"^fs:(\d+):(data\d{1,3})(?::(\d+))?$")


class FastshareError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def strip_accents(text):
    """„Pelíšky" → „Pelisky"."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "")) if not unicodedata.combining(c))


def _height(resolution):
    """„1920x800" → (1920, 800), „1080p" → (0, 1080)."""
    text = str(resolution or "")
    m = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{3,4})\s*p", text, re.I)
    return (0, int(m.group(1))) if m else (0, 0)


def normalize(item):
    """Výsledek hledání do tvaru, se kterým pracuje jádro. Bez rozpoznaného
    datového serveru se soubor nevrátí — nešel by přehrát."""
    m = SERVER_RE.match(str(item.get("download_url") or ""))
    if not m:
        return None
    size = _int((item.get("data") or {}).get("value"))
    width, height = _height(item.get("resolution"))
    return {
        "id": m.group(2),
        "server": m.group(1),
        "name": item.get("filename") or "",
        "size": size,
        "size_h": human_size(size) if size else "",
        "duration": _int((item.get("duration") or {}).get("value")),
        "thumb": item.get("thumbnail") or "",
        "media": {"audio": [], "subs": [], "width": width, "height": height},
    }


def make_ref(file):
    return f"fs:{file['id']}:{file['server']}:{file.get('size') or 0}"


def parse_ref(url):
    """`fs:<id>:<server>[:<velikost>]` → (adresa souboru, velikost v bajtech)."""
    m = REF_RE.match(str(url or ""))
    if not m:
        raise FastshareError("neplatný odkaz na soubor")
    return f"https://{m.group(2)}.fastshare.cloud/download.php?id={m.group(1)}", _int(m.group(3))


class FastshareApi:
    def __init__(self, login, password, cache=None, cache_ttl=SEARCH_TTL):
        self.login_name = (login or "").strip()
        self.password = password or ""
        self.cache = cache
        self.cache_ttl = cache_ttl
        self._account = None

    # --- síť ----------------------------------------------------------------
    def _get(self, **params):
        req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params),
                                     headers={"Accept": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                # `replace` — pár názvů souborů umí FastShare poslat s bajtem, co do UTF-8
                # nepatří (cizí/poškozené znakové sady u uživatelských uploadů); "The Matrix
                # 1999" na Office 2026-09-14 spadlo na pozici 12320 uprostřed výpisu
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise FastshareError("přihlášení se nepovedlo — zkontroluj jméno a heslo", status=e.code) from e
            raise FastshareError(f"HTTP {e.code}", status=e.code) from e
        except Exception as e:  # noqa: BLE001 – síť, DNS, rozsypaný JSON
            # text chyby nesmí nést adresu dotazu — je v ní heslo
            raise FastshareError(type(e).__name__ if "password=" in str(e) else str(e)[:120]) from e

    # --- účet ---------------------------------------------------------------
    def _account_key(self):
        return hashlib.sha256(f"{self.login_name}\0{self.password}".encode("utf-8")).hexdigest()[:16]

    def login(self):
        """{"hash", "unlimited", "credit_mb"} — přihlásí se vždy znovu a výsledek si uloží."""
        if not self.login_name or not self.password:
            raise FastshareError("účet není vyplněný")
        user = (self._get(process="login", login=self.login_name, password=self.password) or {}).get("user") or {}
        if not user.get("hash"):
            raise FastshareError("přihlášení nevrátilo hash")
        account = {"hash": str(user["hash"]),
                   "unlimited": str(user.get("unlimited")).lower() == "true",
                   "credit_mb": _int((user.get("data") or {}).get("value"))}
        self._account = account
        if self.cache is not None and hasattr(self.cache, "save"):
            data = dict(self.cache.load(HASH_STORE, {}) or {})
            data[self._account_key()] = {**account, "valid_until": time.time() + HASH_TTL}
            self.cache.save(HASH_STORE, data)
        return account

    def account(self):
        """Přihlášení z paměti nebo úložiště, dokud je čerstvé; jinak nové."""
        if self._account:
            return self._account
        if self.cache is not None and hasattr(self.cache, "load"):
            rec = (self.cache.load(HASH_STORE, {}) or {}).get(self._account_key()) or {}
            if rec.get("hash") and time.time() < float(rec.get("valid_until") or 0):
                self._account = {k: rec[k] for k in ("hash", "unlimited", "credit_mb") if k in rec}
                return self._account
        return self.login()

    # --- rozhraní -----------------------------------------------------------
    def search(self, query, limit=25):
        # hledání diakritiku nezvládá: „Pelíšky" vrací cizí soubory, „Pelisky" 228 správných
        query = strip_accents(query)

        def load():
            data = self._get(process="search", term=query, pagination=limit, adult="false") or {}
            inner = data.get("search") or {}
            files = [f for f in (normalize(i) for i in inner.get("file") or [] if isinstance(i, dict)) if f]
            return files, _int(inner.get("total")) or len(files)
        if self.cache is None:
            return load()
        files, total = self.cache.cached(f"fastshare:search:{query}:{limit}", self.cache_ttl, lambda: list(load()))
        return files, total

    def request(self, ref):
        """(adresa, hlavičky) souboru. Přihlašuje se čerstvě, když by se soubor
        nevešel do zapamatovaného kreditu — kredit se mezitím mohl dobít."""
        url, size = parse_ref(ref)
        account = self.account()
        if not account.get("unlimited") and size and account.get("credit_mb", 0) * 2 ** 20 < size:
            account = self.login()
            if not account.get("unlimited") and account.get("credit_mb", 0) * 2 ** 20 < size:
                have = human_size(account.get("credit_mb", 0) * 2 ** 20) if account.get("credit_mb") else "0 MB"
                raise FastshareError(f"na soubor {human_size(size)} nestačí kredit ({have})", status=402)
        return url, {"Cookie": f"FASTSHARE={account['hash']}", "User-Agent": UA}

    def kodi_url(self, ref):
        """Adresa pro přehrávač Kodi — cookie za svislítkem, tak ji Kodi pošle."""
        url, headers = self.request(ref)
        return (f"{url}|Cookie={urllib.parse.quote(headers['Cookie'])}"
                f"&User-Agent={urllib.parse.quote(headers['User-Agent'])}")
