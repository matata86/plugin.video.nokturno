"""Sledujteto.cz — hledání videí a odkaz na přehrání přes jejich API pro aplikace.

Rozhraní je totéž, které používá oficiální doplněk pro Kodi (repozitář
`https://kodi.sledujteto.cz/`, `plugin.video.sledujteto` 1.0.0, `lib/api.py`).
Veřejné není — přihlášení chce i hledání:

    POST v1/token   {"email", "password"}          → {"data": {"token", "expires"}}
    GET  v1/me                                     → {"data": {"user": {"is_premium", …}}}
    GET  v1/videos?query=&limit=&offset=           → {"data": {"results": [...], "total"}}
    GET  v1/video/<id>/link                        → {"data": {"link", "id", "progress"}}

Přehrávat jde jen s Premium účtem — bez něj `link` odpoví 403. Token se drží
v úložišti jádra (`sledujteto_token.json`, klíč podle otisku účtu), ať se
nepřihlašuje každý dotaz; po 401 se jednou přihlásí znovu.

Velikost souboru jejich doplněk nepoužívá, takže není jisté, pod jakým klíčem
chodí — `_size()` zkouší obvyklé varianty.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.sledujteto.com/api/"
TIMEOUT = 20
SEARCH_TTL = 12 * 3600
TOKEN_TTL = 6 * 24 * 3600      # skutečnou platnost API posílá v `expires`; tohle je strop
TOKEN_STORE = "sledujteto_token"
UA = "Nokturno/3 (plugin.video.nokturno)"
DEVICE_ID = "nokturno"

ERROR_TEXTS = {
    "invalid_credentials": "přihlášení se nepovedlo — zkontroluj e-mail a heslo",
}


class SledujtetoError(Exception):
    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.paused = False


from streams import human_size  # noqa: F401
from badlogin import login_paused, mark_bad_login


def _duration(text):
    """„1:52:07" / „52:07" → sekundy; číslo bere rovnou."""
    if isinstance(text, (int, float)):
        return int(text)
    try:
        parts = [int(p) for p in str(text or "").split(":")]
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0


def _size(item):
    video = item.get("video") or {}
    for source in (item, video):
        for key in ("size", "filesize", "file_size", "size_bytes", "bytes"):
            value = source.get(key)
            try:
                if value and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                continue
    return 0


CHANNEL_COUNTS = {1: "1.0", 2: "2.0", 3: "2.1", 4: "4.0", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


def _channels(value):
    """Počet kanálů zvuku → „5.1". API ho může poslat jako počet stop (6) i jako text („5.1")."""
    try:
        num = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return ""
    if num <= 0:
        return ""
    return CHANNEL_COUNTS.get(int(num), "") if num.is_integer() else f"{num:.1f}"


def _resolution(value):
    """„1920x1080" / „1080p" / 1080 → (šířka, výška)."""
    text = str(value or "")
    m = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{3,4})", text)
    return (0, int(m.group(1))) if m else (0, 0)


def media(video):
    """Stopy a rozlišení z údajů API — stejný tvar jako `mediainfo.probe()`, takže je
    jádro i klienti použijí jako přečtenou hlavičku a soubor se číst nemusí.
    Jazyk stopy API neříká, stopa je proto bez jazyka."""
    channels = _channels(video.get("audio_channels"))
    codec = str(video.get("audio_codec") or "").strip().upper()[:12]
    width, height = _resolution(video.get("resolution"))
    return {
        "audio": [{"lang": "", "channels": channels, "codec": codec}] if channels or codec else [],
        "subs": [],
        "width": width,
        "height": height,
    }


def normalize(item):
    """Výsledek hledání do tvaru, se kterým pracuje jádro (jako u HellSpy)."""
    video = item.get("video") or {}
    size = _size(item)
    quality = "4K" if video.get("is_4k") else ("HD" if video.get("is_hd") else "")
    subs = [s.get("url") for s in (video.get("subtitles") or []) if isinstance(s, dict) and s.get("url")]
    return {
        "id": str(item.get("id") or ""),
        "name": item.get("name") or "",
        "size": size,
        "size_h": human_size(size) if size else "",
        "duration": _duration(video.get("duration")),
        "quality": quality,
        "subtitles": subs,
        "thumb": (video.get("thumb_urls") or [""])[0],
        "media": media(video),
    }


class SledujtetoApi:
    def __init__(self, email, password, cache=None, cache_ttl=SEARCH_TTL):
        self.email = (email or "").strip()
        self.password = password or ""
        self.cache = cache
        self.cache_ttl = cache_ttl
        self._token = None

    # --- síť ----------------------------------------------------------------
    def _request(self, method, path, data=None, token=None):
        headers = {"Accept": "application/json", "User-Agent": UA, "X-Device-ID": DEVICE_ID}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            code, message = None, ""
            try:
                parsed = json.loads(e.read().decode("utf-8", errors="replace"))
                inner = parsed.get("data") or {}
                errors = inner.get("errors") or []
                code = errors[0] if errors else None
                message = inner.get("message") or ""
            except Exception:  # noqa: BLE001 – tělo chyby není JSON
                pass
            text = ERROR_TEXTS.get(code) or (f"HTTP {e.code}" + (f" {message}" if message else ""))
            raise SledujtetoError(text, code=code, status=e.code) from e
        except Exception as e:  # noqa: BLE001 – síť, DNS, rozsypaný JSON
            raise SledujtetoError(str(e)[:120]) from e

    # --- token --------------------------------------------------------------
    def _account_key(self):
        return hashlib.sha256(f"{self.email}\0{self.password}".encode("utf-8")).hexdigest()[:16]

    def _saved_token(self):
        if self.cache is None or not hasattr(self.cache, "load"):
            return None
        rec = (self.cache.load(TOKEN_STORE, {}) or {}).get(self._account_key()) or {}
        if rec.get("token") and time.time() < float(rec.get("valid_until") or 0):
            return rec["token"]
        return None

    def _save_token(self, token, expires):
        valid_until = time.time() + TOKEN_TTL
        try:
            if isinstance(expires, (int, float)) or str(expires).isdigit():
                valid_until = min(valid_until, float(expires) - 3600)
        except (TypeError, ValueError):
            pass
        if self.cache is None or not hasattr(self.cache, "save"):
            return
        data = dict(self.cache.load(TOKEN_STORE, {}) or {})
        data[self._account_key()] = {"token": token, "valid_until": valid_until}
        self.cache.save(TOKEN_STORE, data)

    def login(self):
        if not self.email or not self.password:
            raise SledujtetoError("účet není vyplněný")
        if login_paused("sledujteto", self.email, self.password, self.cache):
            err = SledujtetoError(ERROR_TEXTS["invalid_credentials"], code="invalid_credentials", status=401)
            err.paused = True
            raise err
        try:
            resp = self._request("POST", "v1/token", {"email": self.email, "password": self.password})
        except SledujtetoError as err:
            if err.code == "invalid_credentials":
                mark_bad_login("sledujteto", self.email, self.password, self.cache)
            raise
        inner = resp.get("data") or {}
        token = inner.get("token")
        if not token:
            raise SledujtetoError("přihlášení nevrátilo token")
        self._token = token
        self._save_token(token, inner.get("expires"))
        return token

    def _authed(self, method, path):
        token = self._token or self._saved_token() or self.login()
        self._token = token
        try:
            return self._request(method, path, token=token)
        except SledujtetoError as err:
            if err.status != 401:
                raise
        # token vypršel nebo ho server zneplatnil — jednou znovu
        return self._request(method, path, token=self.login())

    # --- rozhraní -----------------------------------------------------------
    def me(self):
        data = self._authed("GET", "v1/me").get("data") or {}
        user = data.get("user") or {}
        # oficiální doplněk čte jen is_premium; konec Premium API zatím neposílá čitelně
        return user

    def search(self, query, limit=25, offset=0):
        def load():
            qs = urllib.parse.urlencode({"query": query, "limit": limit, "offset": offset})
            inner = self._authed("GET", "v1/videos?" + qs).get("data") or {}
            results = inner.get("results") or []
            files = [normalize(r) for r in results if isinstance(r, dict) and r.get("id")]
            return files, int(inner.get("total") or len(files))
        if self.cache is None:
            return load()
        files, total = self.cache.cached(f"sledujteto:{self._account_key()}:{query}:{limit}:{offset}",
                                         self.cache_ttl, lambda: list(load()))
        return files, total

    def file_link(self, video_id):
        try:
            inner = self._authed("GET", f"v1/video/{urllib.parse.quote(str(video_id))}/link").get("data") or {}
        except SledujtetoError as err:
            if err.status == 403:
                raise SledujtetoError("přehrávání vyžaduje Premium účet", code="premium", status=403) from err
            raise
        link = inner.get("link")
        if not link:
            raise SledujtetoError("odkaz na soubor se nevrátil")
        return link
