"""Trakt.tv — přihlášení kódem zařízení, scrobble a zápis zhlédnutí.

Trakt vyžaduje vlastní aplikaci (client id + secret) — vytvoří se na
https://trakt.tv/oauth/applications (redirect URI `urn:ietf:wg:oauth:2.0:oob`).
Přihlášení: Kodi ukáže kód, uživatel ho zadá na https://trakt.tv/activate.
Posílá se jen to, co má IMDb/TMDB id (tituly z Luny); Sosáč a soubory WebShare
Trakt nezná.
"""
import json
import time
import urllib.error
import urllib.request

API = "https://api.trakt.tv"
TIMEOUT = 30


class TraktError(Exception):
    pass


def ids_for(item_id):
    """'tt0133093' → {"imdb": …}; 'tmdb:603' → {"tmdb": 603}; jinak None."""
    item_id = str(item_id or "")
    if item_id.startswith("tt"):
        return {"imdb": item_id}
    if item_id.startswith("tmdb:") and item_id[5:].isdigit():
        return {"tmdb": int(item_id[5:])}
    return None


class TraktApi:
    def __init__(self, client_id, client_secret, tokens=None, on_tokens=None):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.tokens = tokens or {}
        self.on_tokens = on_tokens  # callback po obnově tokenu

    # --- HTTP -------------------------------------------------------------
    def _request(self, path, data=None, auth=True, method=None):
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "User-Agent": "Kodi plugin.video.nokturno",
        }
        if auth:
            if not self.tokens.get("access_token"):
                raise TraktError("nepřihlášeno")
            headers["Authorization"] = "Bearer " + self.tokens["access_token"]
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(API + path, data=body, headers=headers, method=method or ("POST" if body else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and auth and self.tokens.get("refresh_token"):
                self.refresh()
                return self._request(path, data, auth, method)
            raise TraktError(f"{path}: HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001
            raise TraktError(f"{path}: {e}") from e

    # --- přihlášení kódem zařízení ------------------------------------------------
    def device_code(self):
        return self._request("/oauth/device/code", {"client_id": self.client_id}, auth=False)

    def poll_token(self, device_code):
        """Vrátí tokeny, None když uživatel ještě kód nezadal; výjimka při zamítnutí/vypršení."""
        req = urllib.request.Request(API + "/oauth/device/token", data=json.dumps({
            "code": device_code, "client_id": self.client_id, "client_secret": self.client_secret,
        }).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                self._store(json.loads(resp.read()))
                return self.tokens
        except urllib.error.HTTPError as e:
            if e.code in (400, 429):
                return None
            raise TraktError({404: "neplatný kód", 409: "kód už použit", 410: "kód vypršel", 418: "zamítnuto"}.get(e.code, f"HTTP {e.code}")) from e
        except Exception as e:  # noqa: BLE001
            raise TraktError(str(e)) from e

    def refresh(self):
        data = self._request("/oauth/token", {
            "refresh_token": self.tokens.get("refresh_token"), "client_id": self.client_id,
            "client_secret": self.client_secret, "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "grant_type": "refresh_token",
        }, auth=False)
        self._store(data)

    def _store(self, data):
        self.tokens = {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_at": int(time.time()) + int(data.get("expires_in") or 0),
        }
        if self.on_tokens:
            self.on_tokens(self.tokens)

    def logged_in(self):
        return bool(self.tokens.get("access_token"))

    # --- scrobble --------------------------------------------------------------------
    @staticmethod
    def _payload(item_id, season=None, episode=None):
        ids = ids_for(item_id)
        if not ids:
            return None
        if season is not None:
            return {"show": {"ids": ids}, "episode": {"season": int(season), "number": int(episode)}}
        return {"movie": {"ids": ids}}

    def scrobble(self, action, item_id, progress, season=None, episode=None):
        """action: start | pause | stop; progress 0–100. Trakt sám označí zhlédnuto při stop ≥ 80 %."""
        payload = self._payload(item_id, season, episode)
        if not payload:
            return None
        payload["progress"] = round(float(progress), 2)
        return self._request(f"/scrobble/{action}", payload)

    def mark_watched(self, item_id, season=None, episode=None):
        payload = self._payload(item_id, season, episode)
        if not payload:
            return None
        body = {"movies": [payload["movie"]]} if "movie" in payload else {
            "shows": [{"ids": payload["show"]["ids"], "seasons": [
                {"number": int(season), "episodes": [{"number": int(episode)}]}]}]}
        return self._request("/sync/history", body)

    def unmark_watched(self, item_id, season=None, episode=None):
        payload = self._payload(item_id, season, episode)
        if not payload:
            return None
        body = {"movies": [payload["movie"]]} if "movie" in payload else {
            "shows": [{"ids": payload["show"]["ids"], "seasons": [
                {"number": int(season), "episodes": [{"number": int(episode)}]}]}]}
        return self._request("/sync/history/remove", body)
