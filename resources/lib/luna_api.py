"""Klient pro HTTP API serveru Luna: Absolute Cinema (Stremio protokol).

Bez závislostí na Kodi — jde testovat samostatně:
    python3 luna_api.py http://192.168.1.10:7126 e1.XXXX
"""
import json
import re
import threading
import unicodedata
import urllib.parse
import urllib.request

TOKEN_RE = re.compile(r"(e1\.[A-Za-z0-9_\-]+)")
TIMEOUT = 40
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


class LunaError(Exception):
    pass


class LunaApi:
    def __init__(self, base_url, token, cache=None, cache_ttl=600):
        self.base = base_url.rstrip("/")
        self.token = token
        self.cache = cache  # objekt s .cached(key, ttl, loader) – manifest a meta se nemění každou minutu
        self.cache_ttl = cache_ttl

    def _get_cached(self, url):
        if self.cache is None:
            return self._get(url)
        return self.cache.cached(url, self.cache_ttl, lambda: self._get(url))

    # --- HTTP -------------------------------------------------------------
    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "Kodi plugin.video.nokturno"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise LunaError(f"{e} ({url})") from e

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
            return self.cache.cached(url, SEARCH_TTL, loader)
        return loader()

    def meta(self, ctype, item_id):
        return self._get_cached(self._meta_url("meta", ctype, item_id + ".json")).get("meta") or {}

    def _stream_source(self, prefix, ctype, item_id):
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


if __name__ == "__main__":
    import sys

    api = LunaApi(sys.argv[1], parse_token(sys.argv[2]))
    print("katalogy filmů:", [c["id"] for c in api.catalogs("movie")])
    metas = api.catalog("movie", "search.movie", search="matrix")
    print("hledání matrix:", [(m["id"], m["name"]) for m in metas[:3]])
    for s in api.streams("movie", metas[0]["id"])[:3]:
        print("  ", s["label"], "|", s["detail"], "|", s["url"][:50])
