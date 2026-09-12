"""Klient qBittorrentu — předání torrentu ke stažení a stav fronty.

Web UI qBittorrentu v místní síti bývá bez přihlášení (whitelist), a když ne,
klient se přihlásí jménem a heslem z nastavení a drží si session cookie.

Bez závislostí na Home Assistantu — jde testovat samostatně:
    python3 qbittorrent.py http://192.168.1.10:9091
"""
import http.cookiejar
import json
import urllib.parse
import urllib.request

TIMEOUT = 30


class QbitError(Exception):
    pass


class QbitApi:
    def __init__(self, base_url, username="", password="", timeout=TIMEOUT):
        self.base = (base_url or "").rstrip("/")
        self.user = username or ""
        self.password = password or ""
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._logged = False

    def _call(self, path, data=None, retry=True):
        if not self.base:
            raise QbitError("qBittorrent není nastavený.")
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(f"{self.base}{path}", data=body, headers={
            "User-Agent": "Home Assistant Nokturno",
            # qBittorrent bez tohohle hlavičkového Referer odmítá zápisy jako CSRF
            "Referer": self.base,
        })
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            if err.code in (401, 403) and retry and self.user:
                self.login()
                return self._call(path, data, retry=False)
            raise QbitError(f"qBittorrent vrátil HTTP {err.code}.") from err
        except Exception as err:  # noqa: BLE001 – síť, DNS
            raise QbitError(f"qBittorrent není dostupný: {err}") from err

    def login(self):
        if not self.user:
            return
        out = self._call("/api/v2/auth/login",
                         {"username": self.user, "password": self.password}, retry=False)
        if out.strip() != "Ok.":
            raise QbitError("qBittorrent nepřijal jméno a heslo.")
        self._logged = True

    def version(self):
        return self._call("/api/v2/app/version").strip()

    def add(self, url, save_path="", category="nokturno", rename=""):
        """Zařadí magnet nebo .torrent ke stažení. Vrací True, když to klient vzal."""
        if not (url or "").strip():
            raise QbitError("Chybí odkaz na torrent.")
        data = {"urls": url.strip(), "category": category}
        if save_path:
            data["savepath"] = save_path
        if rename:
            data["rename"] = rename[:180]
        out = self._call("/api/v2/torrents/add", data).strip()
        # qBittorrent 5.2 odpovídá JSONem s počty, starší verze textem „Ok.“;
        # u magnetu se torrent nejdřív počítá jako pending (stahují se metadata)
        try:
            counts = json.loads(out)
        except ValueError:
            return not out.lower().startswith("fail")
        if not isinstance(counts, dict):
            return True
        return (int(counts.get("failure_count") or 0) == 0
                and int(counts.get("success_count") or 0) + int(counts.get("pending_count") or 0) > 0)

    def torrents(self, category="nokturno"):
        """Stav fronty — jen to, co má karta ukázat."""
        raw = self._call("/api/v2/torrents/info?" + urllib.parse.urlencode(
            {"category": category, "sort": "added_on", "reverse": "true"}))
        try:
            rows = json.loads(raw or "[]")
        except ValueError as err:
            raise QbitError("qBittorrent vrátil nečitelnou odpověď.") from err
        return [{
            "hash": t.get("hash"),
            "name": t.get("name"),
            "state": t.get("state"),
            "progress": round(float(t.get("progress") or 0) * 100, 1),
            "speed": int(t.get("dlspeed") or 0),
            "eta": int(t.get("eta") or 0),
            "size_gb": round((t.get("size") or 0) / 1073741824, 2),
            "path": t.get("content_path") or "",
        } for t in rows if isinstance(t, dict)]


    def delete(self, torrent_hash, with_files=True):
        """Odebere torrent z klienta. `with_files` smaže i rozdělaná data."""
        self._call("/api/v2/torrents/delete",
                   {"hashes": torrent_hash, "deleteFiles": "true" if with_files else "false"})
        return True


if __name__ == "__main__":
    import sys
    api = QbitApi(sys.argv[1], *(sys.argv[2:4]))
    print("qBittorrent", api.version())
    for t in api.torrents():
        print(f"{t['progress']:>6} %  {t['state']:<12} {t['name'][:70]}")
