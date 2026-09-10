"""Přímý klient WebShare.cz API (bez serveru Luna).

Přihlášení: WebShare chce heslo jako sha1(md5crypt(heslo, salt)) — stejný „salted
hash“ používá i WebShare doplněk pro Stremio, takže do nastavení jde vložit buď
čisté heslo, nebo rovnou ten 40znakový hash. Odpovědi API jsou XML.

Test bez Kodi: python3 webshare_api.py <email> <heslo|hash> matrix
"""
import hashlib
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

API = "https://webshare.cz/api/"
TIMEOUT = 40
ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SORTS = ("", "recent", "rating", "largest", "smallest")
SEARCH_TTL = 12 * 3600  # cache hledání jde smazat ručně — akce „Vymazat cache API“


def _to64(v, n):
    out = ""
    while n > 0:
        out += ITOA64[v & 0x3F]
        v >>= 6
        n -= 1
    return out


def md5crypt(password, salt, magic="$1$"):
    """FreeBSD MD5-crypt (jako `openssl passwd -1`), WebShare ho používá při loginu."""
    pw = password.encode("utf-8")
    salt = salt.encode("utf-8")[:8]
    ctx = hashlib.md5(pw + magic.encode() + salt)
    final = hashlib.md5(pw + salt + pw).digest()
    pl = len(pw)
    while pl > 0:
        ctx.update(final[:min(16, pl)])
        pl -= 16
    i = len(pw)
    while i:
        ctx.update(b"\0" if i & 1 else pw[:1])
        i >>= 1
    final = ctx.digest()
    for i in range(1000):
        c = hashlib.md5()
        c.update(pw if i & 1 else final)
        if i % 3:
            c.update(salt)
        if i % 7:
            c.update(pw)
        c.update(final if i & 1 else pw)
        final = c.digest()
    rv = magic + salt.decode() + "$"
    rv += _to64((final[0] << 16) | (final[6] << 8) | final[12], 4)
    rv += _to64((final[1] << 16) | (final[7] << 8) | final[13], 4)
    rv += _to64((final[2] << 16) | (final[8] << 8) | final[14], 4)
    rv += _to64((final[3] << 16) | (final[9] << 8) | final[15], 4)
    rv += _to64((final[4] << 16) | (final[10] << 8) | final[5], 4)
    rv += _to64(final[11], 2)
    return rv


def is_salted_hash(value):
    return bool(re.fullmatch(r"[0-9a-f]{40}", (value or "").strip().lower()))


def human_size(nbytes):
    try:
        n = float(nbytes)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit not in ("B", "kB") else f"{int(n)} {unit}"
        n /= 1024
    return ""


class WebshareError(Exception):
    pass


class WebshareApi:
    def __init__(self, username, password, token=None, cache=None, cache_ttl=SEARCH_TTL):
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.token = token or ""
        self.cache = cache
        self.cache_ttl = cache_ttl

    # --- HTTP -------------------------------------------------------------
    def _call(self, endpoint, **data):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(API + endpoint + "/", data=body, headers={
            "Accept": "text/xml; charset=UTF-8",
            "User-Agent": "Kodi plugin.video.nokturno",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                root = ET.fromstring(resp.read())
        except Exception as e:  # noqa: BLE001
            raise WebshareError(f"{endpoint}: {e}") from e
        if root.findtext("status") != "OK":
            raise WebshareError(f"{endpoint}: {root.findtext('message') or root.findtext('code') or 'chyba'}")
        return root

    # --- login ------------------------------------------------------------
    def password_hash(self):
        if is_salted_hash(self.password):
            return self.password.lower()
        salt = self._call("salt", username_or_email=self.username).findtext("salt") or ""
        return hashlib.sha1(md5crypt(self.password, salt).encode()).hexdigest()

    def login(self):
        data = {"username_or_email": self.username, "password": self.password_hash(), "keep_logged_in": 1}
        if not is_salted_hash(self.password):
            data["digest"] = hashlib.md5(f"{self.username}:Webshare:{self.password}".encode()).hexdigest()
        self.token = self._call("login", **data).findtext("token") or ""
        if not self.token:
            raise WebshareError("login: bez tokenu")
        return self.token

    def _with_token(self, endpoint, **data):
        """Zavolá endpoint; při neplatném tokenu se jednou přihlásí znovu."""
        if not self.token:
            self.login()
        try:
            return self._call(endpoint, wst=self.token, **data)
        except WebshareError:
            self.login()
            return self._call(endpoint, wst=self.token, **data)

    # --- soubory ----------------------------------------------------------
    def search(self, what, sort="", limit=40, offset=0):
        def load():
            root = self._with_token("search", what=what, category="video", sort=sort, limit=limit, offset=offset)
            files = []
            for f in root.findall("file"):
                files.append({
                    "ident": f.findtext("ident"),
                    "name": f.findtext("name") or "",
                    "type": f.findtext("type") or "",
                    "img": f.findtext("img") or "",
                    "size": int(f.findtext("size") or 0),
                    "size_h": human_size(f.findtext("size")),
                    "positive": int(f.findtext("positive_votes") or 0),
                    "negative": int(f.findtext("negative_votes") or 0),
                    "password": f.findtext("password") == "1",
                })
            return files, int(root.findtext("total") or 0)
        if self.cache is None:
            return load()
        # klíč jen z parametrů dotazu — token se obnovuje při přihlášení, ale
        # stejný dotaz má vracet totéž bez ohledu na to, kterým tokenem se ptal
        key = f"ws:search:{what}:{sort}:{limit}:{offset}"
        return self.cache.cached(key, self.cache_ttl, load)

    def file_link(self, ident, password=None):
        data = {"ident": ident, "download_type": "video_stream"}
        if password:
            data["password"] = password
        return self._with_token("file_link", **data).findtext("link") or ""


if __name__ == "__main__":
    import sys

    assert md5crypt("heslo", "abcdefgh") == "$1$abcdefgh$IkglM093H2./3mGx.COMR/"  # openssl passwd -1 -salt abcdefgh heslo
    api = WebshareApi(sys.argv[1], sys.argv[2])
    print("token:", api.login()[:6] + "…")
    files, total = api.search(sys.argv[3] if len(sys.argv) > 3 else "matrix", limit=3)
    print("total:", total)
    for f in files:
        print("  ", f["name"], f["size_h"], f"+{f['positive']}/-{f['negative']}")
    print("link:", api.file_link(files[0]["ident"])[:60])
