"""CZtor (cztor.com) — katalog se streamy z úložiště giganthost.com, na předplatné.

Rozhraní je totéž, které používá oficiální doplněk pro Kodi (`plugin.video.cztor`
0.1.27, `resources/lib/cztor/api.py`), ověřené živě 2026-09-19:

    POST /auth/pin/start {device_id, device_name, device_type}
                          → {pin_code, poll_token, expires_at, interval}
    POST /auth/pin/poll  {poll_token, device_id}
                          → {status: pending|authorized, access_token, refresh_token, expires_at, user}
    POST /auth/refresh   {refresh_token, device_id} → nový pár tokenů
    GET  /profile        → {user, subscription: {active, plan, valid_until}}
    GET  /search?q=      → {items: [{id, type: movie|show, title, original_title, year,
                                     ids: {imdb, tmdb, csfd}, …}], pagination}
    GET  /titles/<id>/streams, /episodes/<id>/streams → {streams: [...]}
    GET  /shows/<id>/seasons → {items: [{id, season_number}]}
    GET  /shows/<id>/seasons/<sid>/episodes → {items: [{id, episode_number, …}]}

Bez tokenu vrací všechno 401. Zařízení se páruje PINem na `cztor.com/activate` —
heslo účtu doplněk nikdy nevidí. Přístupový token platí 24 h a **obnovovací token
se při každém použití vymění** (starý pak vrací 401), takže se pár ukládá hned
a před obnovou se čte znovu z úložiště: plugin i služba Kodi si ho sdílejí.

Hledání je volný fulltext („Matrix" vrátí i Počátek), titul se proto páruje přes
`ids.imdb`/`ids.tmdb`; název a rok jen tam, kde CZtor id nemá (české seriály mívají
jen ČSFD). Některé seriály má CZtor rozdělené po sériích jako samostatné tituly
(„Zrádci - Série 1").

Stream nese přímou adresu (`playback_url`, zeus.giganthost.com), hraje bez
hlaviček a není vázaná na IP — ale platí jen chvíli, takže do seznamu streamů
(a jeho 72h cache) jde jen odkaz `cz:<m|e>:<id titulu/dílu>:<id streamu>`
a adresa se bere čerstvě až při přehrání (`CztorApi.resolve`). Zvuk, titulky
i rozlišení posílá API samo, hlavičky souborů se číst nemusí.
"""
import difflib
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

from streams import human_size

API = "https://api.cztor.com/api"
ACTIVATE_URL = "https://cztor.com/activate"
TIMEOUT = 20                 # server občas odpoví až za ~14 s (2026-09-19)
UA = "Nokturno"
SESSION_STORE = "cztor_session"
SEARCH_TTL = 12 * 3600
TREE_TTL = 6 * 3600          # sezóny a díly seriálu
STREAMS_TTL = 5 * 60         # seznam streamů s adresami — adresa platí jen chvíli
NEAR_NAME = 0.85             # podobnost názvu bez id (CZ × SK: „přebor" × „prebor")
REFRESH_MARGIN = 300         # obnovit token, když do vypršení zbývá míň
REF_RE = re.compile(r"^cz:([me]):(\d+):(\d+)$")
SPLIT_RE = re.compile(r"\s*[-–:]\s*(?:s[eé]rie|s[eé]ria|season|sezona|sezóna)\s*(\d{1,2})\s*$", re.I)
LANG_ALIASES = {"CS": "CZ", "CZE": "CZ", "SLK": "SK", "GB": "EN", "US": "EN", "UK": "EN", "ENG": "EN",
                "HUN": "HU"}

_lock = threading.Lock()


class CztorError(Exception):
    def __init__(self, message, status=None, code=""):
        super().__init__(message)
        self.status = status
        self.code = code


class NotPaired(CztorError):
    """Zařízení není spárované nebo párování zaniklo (odhlášené na webu)."""


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def fold(text):
    """„Okresní přebor" → „okresni prebor" — pro porovnání názvů."""
    text = unicodedata.normalize("NFKD", str(text or "")).lower()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _lang(code):
    code = str(code or "").strip().upper()
    return LANG_ALIASES.get(code, code) if 2 <= len(code) <= 3 else ""


def _channels(value):
    """„5.1", „6", 6 → „5.1"; neznámé → ""."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d\.\d", text):
        return text
    n = _int(text)
    return {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}.get(n, "")


def make_ref(kind, media_id, stream_id):
    return f"cz:{kind}:{media_id}:{stream_id}"


def parse_ref(url):
    m = REF_RE.match(str(url or ""))
    if not m:
        raise CztorError("neplatný odkaz na stream CZtor")
    return m.group(1), m.group(2), m.group(3)


def media_info(stream):
    """Údaje o souboru ve tvaru `mediainfo.probe()` — jako by se přečetla hlavička."""
    audio = []
    for t in stream.get("audio_tracks") or []:
        if isinstance(t, dict):
            audio.append({"lang": _lang(t.get("language")), "channels": _channels(t.get("channels")),
                          "codec": str(t.get("codec") or "")})
    if not audio:
        audio = [{"lang": _lang(code), "channels": "", "codec": ""} for code in stream.get("audio_languages") or []]
    subs = []
    for code in [t.get("language") for t in stream.get("subtitle_tracks") or [] if isinstance(t, dict)] + \
            list(stream.get("subtitles") or []):
        code = _lang(code)
        if code and code not in subs:
            subs.append(code)
    return {"audio": audio, "subs": subs, "width": _int(stream.get("width")), "height": _int(stream.get("height")),
            "duration": _int(stream.get("duration_seconds")), "size": _int(stream.get("size_bytes"))}


def normalize_stream(stream, kind, media_id):
    """Stream z API do tvaru jádra. Bez id nebo adresy se nevrátí — nešel by přehrát."""
    if not stream.get("id") or not stream.get("playback_url"):
        return None
    size = _int(stream.get("size_bytes"))
    return {
        "ref": make_ref(kind, media_id, stream["id"]),
        "name": str(stream.get("release_name") or stream.get("name") or ""),
        "size": size,
        "size_h": human_size(size) if size else "",
        "hdr": bool(stream.get("hdr")),
        "dv": bool(stream.get("dolby_vision")),
        "media": media_info(stream),
    }


class CztorApi:
    """`store`: úložiště jádra (`load`/`save`/`cached`). Drží id zařízení a tokeny."""

    def __init__(self, store, device_name="Nokturno"):
        self.store = store
        self.device_name = device_name or "Nokturno"

    # --- úložiště -----------------------------------------------------------
    def _session(self):
        return dict(self.store.load(SESSION_STORE, {}) or {})

    def _save_session(self, data):
        self.store.save(SESSION_STORE, data)

    def device_id(self):
        data = self._session()
        if not data.get("device_id"):
            data["device_id"] = str(uuid.uuid4())
            self._save_session(data)
        return data["device_id"]

    def paired(self):
        return bool(self._session().get("refresh_token"))

    def account(self):
        """{"name", "plan", "active", "valid_until"} z posledního ověření (bez sítě)."""
        return dict(self._session().get("account") or {})

    # --- síť ----------------------------------------------------------------
    def _http(self, method, path, params=None, payload=None, token=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Accept": "application/json", "User-Agent": UA, "X-Device-ID": self.device_id()}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read().decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001 – tělo chyby není JSON
                    body = {}
                message = str(body.get("message") or f"HTTP {e.code}")[:160] if isinstance(body, dict) else f"HTTP {e.code}"
                code = str(body.get("code") or "") if isinstance(body, dict) else ""
                raise CztorError(message, status=e.code, code=code) from e
            except Exception as e:  # noqa: BLE001 – síť, DNS, rozsypaný JSON
                if attempt + 1 < attempts:
                    time.sleep(0.75)
                    continue
                raise CztorError(f"server nedostupný ({type(e).__name__})") from e
        raise CztorError("server nedostupný")

    def _store_tokens(self, response):
        tokens = response.get("tokens") if isinstance(response.get("tokens"), dict) else response
        access, refresh = tokens.get("access_token"), tokens.get("refresh_token")
        if not access or not refresh:
            return False
        data = self._session()
        data.update(access_token=access, refresh_token=refresh,
                    expires=self._expires(tokens.get("expires_at"), tokens.get("expires_in")))
        user = response.get("user") if isinstance(response.get("user"), dict) else {}
        if user:
            data.setdefault("account", {})["name"] = user.get("name") or user.get("email") or ""
        self._save_session(data)
        return True

    @staticmethod
    def _expires(expires_at, expires_in=None):
        if expires_in:
            return time.time() + _int(expires_in)
        text = str(expires_at or "")
        try:
            from datetime import datetime
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return time.time() + 3600

    def _refresh(self, stale_access=None):
        """Obnoví token. Obnovovací token se použitím mění, takže se čte čerstvě
        a pod zámkem; když ho mezitím obnovil někdo jiný (služba × plugin), vezme se jeho."""
        with _lock:
            data = self._session()
            if stale_access and data.get("access_token") and data["access_token"] != stale_access \
                    and time.time() < float(data.get("expires") or 0) - REFRESH_MARGIN:
                return data["access_token"]
            refresh = data.get("refresh_token")
            if not refresh:
                raise NotPaired("CZtor není spárovaný — spáruj zařízení v nastavení.")
            try:
                resp = self._http("POST", "/auth/refresh",
                                  payload={"refresh_token": refresh, "device_id": self.device_id()})
            except CztorError as err:
                again = self._session()
                if again.get("refresh_token") and again["refresh_token"] != refresh:
                    return again.get("access_token")
                if err.status in (401, 403):
                    self.forget()
                    raise NotPaired("Párování s CZtor zaniklo — spáruj zařízení znovu.",
                                    status=err.status) from err
                raise
            if not self._store_tokens(resp):
                raise CztorError("obnova přihlášení nevrátila token")
            return self._session().get("access_token")

    def _token(self):
        data = self._session()
        if not data.get("refresh_token"):
            raise NotPaired("CZtor není spárovaný — spáruj zařízení v nastavení.")
        if data.get("access_token") and time.time() < float(data.get("expires") or 0) - REFRESH_MARGIN:
            return data["access_token"]
        return self._refresh(data.get("access_token"))

    def request(self, method, path, params=None, payload=None):
        token = self._token()
        try:
            return self._http(method, path, params, payload, token)
        except CztorError as err:
            if err.status != 401:
                raise
        return self._http(method, path, params, payload, self._refresh(token))

    def _cached(self, key, ttl, loader):
        cached = getattr(self.store, "cached", None)
        return cached(key, ttl, loader) if cached else loader()

    # --- párování -----------------------------------------------------------
    def start_pin(self):
        """{"pin", "poll_token", "expires", "interval", "url"} — PIN se potvrdí na `url`."""
        resp = self._http("POST", "/auth/pin/start", payload={
            "device_id": self.device_id(), "device_name": self.device_name, "device_type": "kodi"})
        if not resp.get("pin_code") or not resp.get("poll_token"):
            raise CztorError("server nevydal PIN")
        return {"pin": str(resp["pin_code"]), "poll_token": resp["poll_token"],
                "expires": self._expires(resp.get("expires_at")),
                "interval": max(2, _int(resp.get("interval")) or 5), "url": ACTIVATE_URL}

    def poll_pin(self, poll_token):
        """True = spárováno (tokeny uložené), False = ještě nepotvrzeno."""
        resp = self._http("POST", "/auth/pin/poll", payload={"poll_token": poll_token, "device_id": self.device_id()})
        status = str(resp.get("status") or "").lower()
        if resp.get("access_token") or status in ("authorized", "active", "success"):
            if self._store_tokens(resp):
                try:
                    self.profile()
                except CztorError:
                    pass
                return True
        if status in ("expired", "denied", "rejected", "cancelled"):
            raise CztorError("PIN vypršel — zkus spárovat znovu.", code=status)
        return False

    def forget(self):
        """Zapomene tokeny (id zařízení zůstane — po novém párování je to totéž zařízení)."""
        data = self._session()
        for key in ("access_token", "refresh_token", "expires", "account"):
            data.pop(key, None)
        self._save_session(data)

    def logout(self):
        try:
            if self.paired():
                self.request("POST", "/auth/logout", payload={"device_id": self.device_id()})
        except CztorError:
            pass
        finally:
            self.forget()

    def profile(self):
        """{"name", "plan", "active", "valid_until"} — ověří účet a předplatné."""
        resp = self.request("GET", "/profile")
        user = resp.get("user") or {}
        sub = resp.get("subscription") or {}
        account = {"name": user.get("name") or user.get("email") or "", "plan": sub.get("plan") or "",
                   "active": bool(sub.get("active")), "valid_until": str(sub.get("valid_until") or "")[:10]}
        data = self._session()
        data["account"] = account
        self._save_session(data)
        return account

    # --- katalog ------------------------------------------------------------
    def search(self, query):
        query = str(query or "").strip()
        if not query:
            return []

        def load():
            resp = self.request("GET", "/search", {"q": query, "page": 1})
            return [i for i in resp.get("items") or [] if isinstance(i, dict)]
        return self._cached(f"cztor:search:{fold(query)}", SEARCH_TTL, load) or []

    def find_titles(self, ctype, titles, year=None, imdb="", tmdb=""):
        """Tituly CZtor, které jsou tímhle titulem. Přednost má shoda id; název
        a rok rozhodují jen u položek, které id nemají. U seriálu může vyjít víc
        položek — CZtor některé dělí po sériích (`_split_season`)."""
        kind = "show" if ctype == "series" else "movie"
        wanted = {fold(t) for t in titles if fold(t)}
        imdb, tmdb = str(imdb or ""), str(tmdb or "")
        found, seen = [], set()
        for query in dict.fromkeys(t for t in titles if str(t or "").strip()):
            for item in self.search(query):
                if item.get("type") != kind or item.get("id") in seen:
                    continue
                ids = item.get("ids") or {}
                if imdb and ids.get("imdb"):
                    ok = ids["imdb"] == imdb
                elif tmdb and ids.get("tmdb"):
                    ok = str(ids["tmdb"]) == tmdb
                else:
                    ok = self._name_match(item, wanted, year, kind)
                if ok:
                    seen.add(item.get("id"))
                    m = SPLIT_RE.search(str(item.get("title") or ""))
                    found.append({**item, "_split_season": int(m.group(1)) if m else 0})
            # shoda přes id je jistá — další dotazy by přidaly jen omyly
            if any((i.get("ids") or {}).get("imdb") == imdb for i in found if imdb):
                break
        return found

    @staticmethod
    def _name_match(item, wanted, year, kind):
        names = {fold(SPLIT_RE.sub("", str(item.get(k) or ""))) for k in ("title", "name", "original_title")} - {""}
        item_year = _int(item.get("year"))
        if wanted & names:
            if not year or not item_year:
                return True
            # rozdělený seriál má u pozdější série rok té série, ne premiéry
            return abs(item_year - int(year)) <= 1 or (kind == "show" and item_year >= int(year))
        # CZtor ukazuje slovenské názvy a české tituly bez IMDb id mívá jen pod nimi
        # („Okresný prebor"). Podobný název stačí, jen když sedí rok přesně.
        if not year or item_year != int(year):
            return False
        return any(difflib.SequenceMatcher(None, a, b).ratio() >= NEAR_NAME for a in wanted for b in names)

    def episode_id(self, show, season, episode):
        """Id dílu v CZtor, nebo None."""
        season, episode = int(season), int(episode)
        split = show.get("_split_season") or 0
        if split and split != season:
            return None

        def load_seasons():
            return self.request("GET", f"/shows/{show['id']}/seasons").get("items") or []
        seasons = self._cached(f"cztor:seasons:{show['id']}", TREE_TTL, load_seasons) or []
        match = [s for s in seasons if _int(s.get("season_number")) == season]
        if not match and split and len(seasons) == 1:
            match = seasons   # „Série 3" jako samostatný titul se svou jedinou „Season 1"
        for s in match:
            def load_episodes(sid=s["id"]):
                return self.request("GET", f"/shows/{show['id']}/seasons/{sid}/episodes").get("items") or []
            for ep in self._cached(f"cztor:episodes:{show['id']}:{s['id']}", TREE_TTL, load_episodes) or []:
                if _int(ep.get("episode_number")) == episode:
                    return str(ep["id"])
        return None

    def _raw_streams(self, kind, media_id, fresh=False):
        path = f"/episodes/{media_id}/streams" if kind == "e" else f"/titles/{media_id}/streams"

        def load():
            resp = self.request("GET", path)
            items = resp.get("streams") if isinstance(resp, dict) else resp
            return [s for s in items or [] if isinstance(s, dict)]
        if fresh:
            return load()
        return self._cached(f"cztor:streams:{kind}:{media_id}", STREAMS_TTL, load) or []

    def streams(self, kind, media_id):
        """Streamy titulu (`kind` „m") nebo dílu („e") v tvaru jádra."""
        out = []
        for raw in self._raw_streams(kind, media_id):
            stream = normalize_stream(raw, kind, media_id)
            if stream:
                out.append(stream)
        return out

    def resolve(self, ref):
        """Čerstvá přímá adresa souboru pro `cz:` odkaz."""
        kind, media_id, stream_id = parse_ref(ref)
        for fresh in (False, True):
            for raw in self._raw_streams(kind, media_id, fresh=fresh):
                if str(raw.get("id")) == stream_id and raw.get("playback_url"):
                    return raw["playback_url"]
        raise CztorError("stream už v CZtor není — vyber jiný ze seznamu", status=404)
