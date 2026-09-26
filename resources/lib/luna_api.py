"""Klient pro HTTP API serveru Luna: Absolute Cinema (Stremio protokol).

Bez závislostí na Kodi — jde testovat samostatně:
    python3 luna_api.py http://192.168.1.10:7126 e1.XXXX
"""
import errno
import json
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


def _imdb(metas):
    """Hodnocení ze Stremio metadat (Cinemeta, Luna) je IMDb — značka pro klienty,
    kteří ho odliší od TMDB a Sosáče. Přidává se až po cache, platí i pro staré záznamy."""
    for m in metas if isinstance(metas, list) else [metas]:
        if m.get("imdbRating"):
            m.setdefault("ratingSource", "imdb")
    return metas


TOKEN_RE = re.compile(r"(e1\.[A-Za-z0-9_\-]+)")
TIMEOUT = 40
CONNECT_TIMEOUT = 5      # server, který za pět vteřin nepřijme spojení, neodpoví ani za čtyřicet
DOWN_TTL = 300           # po nedostupnosti se Luna pět minut nevolá — jinak každý výpis čeká na timeout znovu
_DOWN_ERRNO = {errno.ETIMEDOUT, errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ECONNRESET}
SEARCH_TTL = 12 * 3600   # cache hledání jde smazat ručně — akce „Vymazat cache API“
STREAM_TTL = 72 * 3600   # ale co je za soubory na WebShare/Sosáči, se skoro nemění —
                         # jen když se streamy skutečně našly, viz Store.cached_if

# Luna používá emoji, které fonty Kodi skinů většinou neumí – nahradíme textem.
EMOJI_MAP = {
    "\U0001F50A": "Zvuk:",   # 🔊
    "\U0001F50E": "(WS)",    # 🔎 – výsledek fulltextu WebShare (Luna: Search)
    "\U0001F4AC": "Tit.:",   # 💬
    "\U0001F3AC": "",        # 🎬
    "\U0001F4BE": "",        # 💾
    "\U0001F4E6": "",        # 📦
    "⚡": "",            # ⚡
    "⭐": "*",           # ⭐
}


def parse_token(value):
    """Přijme čistý token i celou instalační URL a vrátí token (nebo '')."""
    m = TOKEN_RE.search(value or "")
    return m.group(1) if m else ""


def parse_base_url(value, fallback):
    """Když uživatel vloží celou manifest URL, vezmeme z ní i adresu serveru."""
    m = re.match(r"(https?://[^/]+)", (value or "").strip())
    return m.group(1) if m else fallback


def clean_label(text):
    """Matematické tučné písmo → normální, vlajky → kódy zemí, emoji → text."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text:
        cp = ord(ch)
        if 0x1F1E6 <= cp <= 0x1F1FF:            # regional indicator → písmeno
            out.append(chr(cp - 0x1F1E6 + ord("A")))
        elif ch in EMOJI_MAP:
            out.append(EMOJI_MAP[ch])
        elif cp in (0xFE0E, 0xFE0F, 0x200D):     # variation selectors / ZWJ
            continue
        else:
            out.append(ch)
    text = "".join(out)
    # název streamu má i zalomení řádku („🔎\n4K“, „4K\nHDR“) → jeden řádek
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_ITEM_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")


def _check_item_id(item_id):
    """Id jde do cesty URL — `../` nebo `?` by měnily endpoint (id posílá i klient Stremia)."""
    if not _ITEM_RE.match(str(item_id or "")):
        raise LunaError(f"neplatné id: {str(item_id)[:20]!r}")


class LunaError(Exception):
    pass


class LunaApi:
    def __init__(self, base_url, token, cache=None, cache_ttl=600, fresh=False):
        self.base = base_url.rstrip("/")
        self.token = token
        self.cache = cache  # objekt s .cached(key, ttl, loader) – manifest a meta se nemění každou minutu
        self.cache_ttl = cache_ttl
        self.fresh = fresh  # True = cache jen zapisovat, ne číst (zahřívání na pozadí obnoví, co ještě neprošlo)

    def _get_cached(self, url):
        if self.cache is None:
            return self._get(url)
        return self.cache.cached(url, 0 if self.fresh else self.cache_ttl, lambda: self._get(url))

    # --- HTTP -------------------------------------------------------------
    def _down_key(self):
        return "nokturno:luna:down:" + self.base

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "Nokturno (+https://github.com/matata86/nokturno-core)"})
        down = self.cache is not None and hasattr(self.cache, "peek_cached")
        if down and self.cache.peek_cached(self._down_key(), DOWN_TTL) is not None:
            raise LunaError(f"Luna neodpovídá (adresa {self.base} není dostupná, další pokus za pár minut)")
        try:
            if down:
                self._probe_connect()
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            if down and self._is_unreachable(e):
                self.cache.cached_if(self._down_key(), DOWN_TTL, lambda: {"t": int(time.time())}, fresh=True)
            raise LunaError(f"{e} ({self._bez_tokenu(url)})") from e

    def _probe_connect(self):
        """Spojení na server se ověří s krátkým limitem; systém jinak čeká na timeout přes 20 s."""
        parts = urllib.parse.urlsplit(self.base)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((parts.hostname, port), timeout=CONNECT_TIMEOUT):
            pass

    @staticmethod
    def _is_unreachable(err):
        """Server je mimo dosah (timeout, odmítnuté spojení, žádná cesta) — ne chyba odpovědi (HTTP 4xx/5xx)."""
        if isinstance(err, urllib.error.HTTPError):
            return False
        reason = getattr(err, "reason", err)
        return isinstance(reason, (socket.timeout, TimeoutError, ConnectionError)) or \
            getattr(reason, "errno", None) in _DOWN_ERRNO or isinstance(reason, socket.gaierror)

    def _bez_tokenu(self, url):
        """Adresa do chybové hlášky bez tokenu — hláška jde do notifikace Kodi i do
        kodi.log, který lidé posílají do fór, a token dává přístup k cizímu WebShare."""
        return url.replace(self.token, "…") if self.token else url

    def _meta_url(self, *parts):
        return "/".join([self.base, "metadata", self.token] + [str(p) for p in parts])

    # --- Stremio zdroje -----------------------------------------------------
    def manifest(self):
        return self._get_cached(self._meta_url("manifest.json"))

    def catalogs(self, ctype):
        """Katalogy pro daný typ, bez interních (calendar, people)."""
        result = []
        for c in self.manifest().get("catalogs", []):
            if c.get("type") != ctype:
                continue
            cid = c.get("id", "")
            if cid.startswith("calendar") or cid.startswith("people_search"):
                continue
            extra = {e.get("name"): e for e in c.get("extra", [])}
            result.append({
                "id": cid,
                "name": c.get("name") or cid,
                "search": "search" in extra,
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
        if extra:
            url = self._meta_url("catalog", ctype, cid, "&".join(extra) + ".json")
        else:
            url = self._meta_url("catalog", ctype, cid + ".json")
        loader = lambda: self._get(url).get("metas") or []  # noqa: E731
        # hledání (search=…) se kešuje — výsledky se mění (nové tituly, dostupnost),
        # ale ne rychle; procházení katalogu bez hledání necháváme jak bylo
        if search and self.cache is not None:
            return _imdb(self.cache.cached(url, SEARCH_TTL, loader))
        return _imdb(loader())

    def meta(self, ctype, item_id):
        _check_item_id(item_id)
        return _imdb(self._get_cached(self._meta_url("meta", ctype, item_id + ".json")).get("meta") or {})

    def _stream_source(self, prefix, ctype, item_id):
        _check_item_id(item_id)
        parts = [self.base] + ([prefix] if prefix else []) + [self.token, "stream", ctype, item_id + ".json"]
        url = "/".join(parts)
        loader = lambda: self._get(url).get("streams") or []  # noqa: E731
        # dlouhá cache, ale jen když se něco našlo — prázdný výsledek se
        # nepamatuje, ať další pokus (třeba za pár minut) zkusí znovu
        if self.cache is not None:
            return self.cache.cached_if(url, STREAM_TTL, loader)
        return loader()

    def streams(self, ctype, item_id, include_search=True):
        """Streamy z hlavního zdroje Luny + z „Luna: Search“ (fulltext WebShare).

        Stremio má oba nainstalované jako dva doplňky a ukazuje výsledky
        obou; tady je sloučíme — hlavní (přesná shoda) první, hledání za ním.
        Oba dotazy běží souběžně, hledání trvá i desítky sekund.
        """
        sources = [("", ctype, item_id)]
        if include_search:
            sources.append(("search", ctype, item_id))
        results = {}
        errors = []

        def worker(prefix):
            try:
                results[prefix] = self._stream_source(prefix, ctype, item_id)
            except LunaError as e:
                errors.append(e)
                results[prefix] = []

        threads = [threading.Thread(target=worker, args=(src[0],)) for src in sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors and not any(results.values()):
            raise errors[0]

        streams, seen = [], set()
        for prefix, _, _ in sources:
            for s in results.get(prefix, []):
                url = s.get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                s["source"] = "search" if prefix else "main"
                s["label"] = clean_label(s.get("name") or "")
                s["detail"] = " | ".join(
                    clean_label(line) for line in (s.get("title") or s.get("description") or "").split("\n")
                    if line.strip()
                )
                streams.append(s)
        return streams


# --- Rozpoznání serveru, diagnostika a hledání v síti ----------------------
#
# Proč tady a ne v Kodi: „nefunguje mi to" je u Luny vždycky stejná věta, ale
# stojí za ní šest různých příčin (server neběží, jiná adresa, firewall, token
# nezkopírovaný celý, neplatný token, WebShare nevyplněný v samotné Luně) a
# uživatel je od sebe nerozezná. Diagnóza je čistý Python, aby ji mohl použít
# i Stremio a HA a aby šla otestovat bez sítě; texty pro uživatele si podle
# vráceného kódu poskládá každá větev sama ve svém jazyce.

LUNA_MANIFEST_ID = "luna.absolutecinema"   # `GET /manifest.json` jde bez tokenu a Luna se v něm jmenuje
LUNA_PORT = 7126
# Známé tituly pro zkušební dotaz: hlavní zdroj Luny nemá všechno (třeba Matrix
# nevrátí nic ani s platným tokenem), takže se zkouší víc, než prohlásíme „nic".
PROBE_IDS = ("tt0111161", "tt0068646", "tt0133093")
DISCOVER_WORKERS = 64
DISCOVER_CONNECT_TIMEOUT = 0.4   # v LAN odpoví server do pár ms; delší čekání jen prodlužuje sken
DISCOVER_HTTP_TIMEOUT = 3

# stavy diagnózy
OK, WARN, FAIL = "ok", "warn", "fail"


def normalize_base_url(value, default_port=LUNA_PORT):
    """Z toho, co člověk vloží, udělá adresu serveru.

    Bere celou instalační adresu i s tokenem, holou IP bez schématu i adresu
    bez portu — všechny tři tvary lidem chodí z návodů a všechny tři končily
    tím, že se doplněk neměl kam připojit.
    """
    value = (value or "").strip()
    if not value:
        return ""
    value = re.sub(r"^https?://", lambda m: m.group(0).lower(), value)
    if not re.match(r"^https?://", value):
        value = "http://" + value
    m = re.match(r"(https?)://([^/:\s]+)(?::(\d+))?", value)
    if not m:
        return ""
    scheme, host, port = m.group(1), m.group(2), m.group(3)
    if not port:
        port = "443" if scheme == "https" else str(default_port)
    return f"{scheme}://{host}:{port}"


def _fetch_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "Nokturno (+https://github.com/matata86/nokturno-core)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def server_info(base_url, timeout=6):
    """Běží na téhle adrese Luna? Vrátí ``{"version": …, "name": …}``.

    Vyhazuje ``LunaError`` — rozlišit „nikdo neodpovídá" od „odpovídá něco
    jiného" musí volající, proto je důvod v textu výjimky i v ``code``.
    """
    base = normalize_base_url(base_url)
    if not base:
        raise LunaError("bad_url")
    try:
        data = _fetch_json(base + "/manifest.json", timeout)
    except Exception as e:  # noqa: BLE001 – síť, DNS, odmítnuté spojení, HTML místo JSON
        raise LunaError(f"unreachable: {e}") from e
    if LUNA_MANIFEST_ID not in str(data.get("id") or ""):
        raise LunaError("not_luna")
    return {"version": str(data.get("version") or "?"), "name": str(data.get("name") or "Luna")}


def diagnose(base_url, token, timeout=20, probe_ids=PROBE_IDS, deep=True):
    """Projde celý řetěz od adresy po streamy a řekne, na kterém článku to stojí.

    Vrací ``{"level": ok|warn|fail, "code": …, "base": …, "version": …, "detail": …}``.
    Kódy: ``no_url``, ``bad_url``, ``unreachable``, ``not_luna``, ``no_token``,
    ``bad_token_format``, ``bad_token``, ``main_empty``, ``no_streams``, ``ok``.

    Token se nedá ověřit z manifestu — ten Luna vrátí i pro naprostý nesmysl,
    jen s výchozím nastavením (ověřeno proti 1.7.0). Jediné, co je od sebe
    odliší, je dotaz na streamy: platný token vrátí JSON, neplatný utne spojení.
    """
    out = {"level": FAIL, "code": "no_url", "base": "", "token": "", "version": "", "detail": ""}
    if not (base_url or "").strip() and not (token or "").strip():
        return out

    # token bývá vložený jako celá instalační adresa — pak je v něm i adresa serveru
    clean_token = parse_token(token) or parse_token(base_url)
    base = normalize_base_url(parse_base_url(token, "") or base_url)
    out["base"], out["token"] = base, clean_token
    if not base:
        out["code"] = "bad_url"
        return out

    try:
        info = server_info(base, timeout=min(timeout, 8))
    except LunaError as e:
        out["code"] = "not_luna" if str(e) == "not_luna" else "unreachable"
        out["detail"] = str(e)
        return out
    out["version"] = info["version"]

    if not clean_token:
        # rozlišení pro hlášku: prázdné pole × „něco tam je, ale token to není"
        out["code"] = "bad_token_format" if (token or "").strip() else "no_token"
        return out
    if not deep:
        out.update(level=OK, code="ok")
        return out

    api = LunaApi(base, clean_token)
    errors = []
    for item_id in probe_ids:
        try:
            streams = api._get(_stream_url(base, clean_token, "movie", item_id)).get("streams") or []
        except LunaError as e:
            errors.append(str(e))
            continue
        if streams:
            out.update(level=OK, code="ok")
            return out
    if errors and len(errors) == len(probe_ids):
        # ani jeden dotaz neprošel, ale manifest šel — token Luna nepřijala
        out.update(code="bad_token", detail=errors[0])
        return out

    # hlavní zdroj mlčí; ještě fulltext WebShare, ať poznáme „token je v pořádku,
    # jen Luna nic nemá" od „Luna nemá vyplněný účet a nenajde vůbec nic"
    try:
        found = api._get(_stream_url(base, clean_token, "movie", probe_ids[0], prefix="search")).get("streams") or []
    except LunaError as e:
        found, out["detail"] = [], str(e)
    out.update(level=WARN, code="main_empty" if found else "no_streams")
    return out


def _stream_url(base, token, ctype, item_id, prefix=""):
    parts = [base] + ([prefix] if prefix else []) + [token, "stream", ctype, item_id + ".json"]
    return "/".join(parts)


def local_subnet(probe_host="8.8.8.8"):
    """První tři oktety adresy, na které je zařízení v síti — bez odeslání paketu
    (UDP ``connect`` jen vybere rozhraní). Vrátí '' tam, kde IPv4 adresa není."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((probe_host, 80))
        ip = s.getsockname()[0]
    except Exception:  # noqa: BLE001 – bez sítě nebo jen IPv6
        return ""
    finally:
        s.close()
    parts = ip.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else ""


def discover(port=LUNA_PORT, subnet=None, on_progress=None, should_stop=None,
             connect_timeout=DISCOVER_CONNECT_TIMEOUT, workers=DISCOVER_WORKERS):
    """Projde vlastní podsíť a vrátí adresy, kde skutečně běží Luna.

    Nejdřív TCP klepnutí na port (levné, 254 adres proběhne za vteřiny), teprve
    u toho, co otevře, se ptáme na manifest — jinak by se za Lunu prohlásil
    kterýkoli web, co na tom portu náhodou poslouchá.
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor, as_completed

    subnet = subnet or local_subnet()
    if not subnet:
        return []
    hosts = [f"{subnet}.{i}" for i in range(1, 255)]
    found = []

    def probe(host):
        if should_stop and should_stop():
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(connect_timeout)
        try:
            if sock.connect_ex((host, port)) != 0:
                return
        except OSError:
            return
        finally:
            sock.close()
        try:
            info = server_info(f"http://{host}:{port}", timeout=DISCOVER_HTTP_TIMEOUT)
        except LunaError:
            return   # na portu poslouchá něco jiného
        found.append({"host": host, "url": f"http://{host}:{port}",
                      "version": info["version"], "name": info["name"]})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(probe, h) for h in hosts]
        for hotovo, _ in enumerate(as_completed(futures), 1):
            if on_progress:
                on_progress(hotovo, len(hosts))
    return sorted(found, key=lambda f: int(f["host"].rsplit(".", 1)[-1]))


if __name__ == "__main__":
    import sys

    api = LunaApi(sys.argv[1], parse_token(sys.argv[2]))
    print("katalogy filmů:", [c["id"] for c in api.catalogs("movie")])
    metas = api.catalog("movie", "search.movie", search="matrix")
    print("hledání matrix:", [(m["id"], m["name"]) for m in metas[:3]])
    for s in api.streams("movie", metas[0]["id"])[:3]:
        print("  ", s["label"], "|", s["detail"], "|", s["url"][:50])
