"""Klient pro HTTP API serveru Luna: Absolute Cinema (Stremio protokol).

Bez závislostí na Kodi — jde testovat samostatně:
    python3 luna_api.py http://192.168.1.10:7126 e1.XXXX
"""
import json
import re
import unicodedata
import urllib.parse
import urllib.request

TOKEN_RE = re.compile(r"(e1\.[A-Za-z0-9_\-]+)")
TIMEOUT = 40

# Luna používá emoji, které fonty Kodi skinů většinou neumí – nahradíme textem.
EMOJI_MAP = {
    "\U0001F50A": "Zvuk:",   # 🔊
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
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


class LunaError(Exception):
    pass


class LunaApi:
    def __init__(self, base_url, token):
        self.base = base_url.rstrip("/")
        self.token = token

    # --- HTTP -------------------------------------------------------------
    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "Kodi plugin.video.luna"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise LunaError(f"{e} ({url})") from e

    def _meta_url(self, *parts):
        return "/".join([self.base, "metadata", self.token] + [str(p) for p in parts])

    # --- Stremio zdroje -----------------------------------------------------
    def manifest(self):
        return self._get(self._meta_url("manifest.json"))

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
        return self._get(url).get("metas") or []

    def meta(self, ctype, item_id):
        return self._get(self._meta_url("meta", ctype, item_id + ".json")).get("meta") or {}

    def streams(self, ctype, item_id):
        url = "/".join([self.base, self.token, "stream", ctype, item_id + ".json"])
        streams = self._get(url).get("streams") or []
        for s in streams:
            s["label"] = clean_label(s.get("name") or "")
            s["detail"] = " | ".join(
                clean_label(line) for line in (s.get("title") or s.get("description") or "").split("\n") if line.strip()
            )
        return streams


if __name__ == "__main__":
    import sys

    api = LunaApi(sys.argv[1], parse_token(sys.argv[2]))
    print("katalogy filmů:", [c["id"] for c in api.catalogs("movie")])
    metas = api.catalog("movie", "search.movie", search="matrix")
    print("hledání matrix:", [(m["id"], m["name"]) for m in metas[:3]])
    for s in api.streams("movie", metas[0]["id"])[:3]:
        print("  ", s["label"], "|", s["detail"], "|", s["url"][:50])
