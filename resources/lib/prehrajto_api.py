"""Přehraj.to — hledání videí a odkaz na přehrání.

Vlastní rozhraní server nemá: `/api/*` vrací 404 a oficiální doplněk pro Kodi
(`plugin.video.prehrajto` 2.0.5, autor WrightDerby) čte tytéž stránky jako
prohlížeč. Čte se proto HTML:

    GET  /hledej/<text>[?videoListing-visualPaginator-page=N]  → výpis, 32 na stranu
    GET  /<slug>/<hash>                                        → stránka videa
    POST /?frm=loginDialog-login-loginForm                     → přihlášení, cookies
    GET  /<slug>/<hash>?do=download                            → 302 na podepsaný odkaz CDN

**Bez přihlášení** je vidět jen první strana hledání (dál se chce účet) a stránka
videa nabídne překódované verze 1080p a 720p. **S účtem Premium** jde stránkovat
a `?do=download` vydá **původní soubor** — tedy i 4K a HDR, které v překódování
nejsou. Změřeno na jednom titulu: originál 2,87 GB proti překódovaným 2,92 GB
(1080p) a 1,38 GB (720p), rychlost 33 MB/s proti 6,8 MB/s bez účtu.

Podepsaný odkaz CDN **nedrží na IP** (ověřeno ze dvou sítí) a nechce hlavičky ani
cookie, takže hraje i tam, kde nemůže projít proxy — v Kodi, v Stremiu i ve
stahování. Platí zhruba den (`expires` v adrese), dohledává se proto až při
přehrání, jako u WebShare a HellSpy.

Odkaz nese slug i hash (`pt:<slug>:<hash>`), protože `?do=download` na cizím slugu
neodpoví odkazem, jen přesměruje zpátky na stránku videa. Stránka videa sama je
na slugu nezávislá.

Dvě vlastnosti serveru, se kterými je nutné počítat:

* **HTTP 429.** Limit je plovoucí a nepravidelný — dvacet dotazů v dávce projde,
  dvanáct po 1,5 s ne. Po první 429 se zdroj na `RATE_LIMIT_COOLDOWN` přeskakuje,
  stejně jako HellSpy (6.0.2, 6.0.4): opakované dotazy blokaci jen prodlužují.
* **Prázdná odpověď na některé dotazy.** Hledání je jinak citlivé na diakritiku i
  na pořadí slov, ale `okresni prebor` vrátí nula výsledků, zatímco `prebor
  okresni` i `okresni prebo` vrátí plnou stranu. Je to vada jejich indexu u
  konkrétního řetězce, ne pravidlo, které by šlo obejít výpočtem. Jádro zkouší
  víc variant názvu (`MAX_TITLE_VARIANTS`), takže se přes to obvykle přenese samo.
"""
import hashlib
import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from streams import human_size

BASE = "https://prehraj.to"
TIMEOUT = 20
SEARCH_TTL = 12 * 3600
LINK_TTL = 6 * 3600          # podepsaný odkaz CDN platí ~24 h, bereme si rezervu
SESSION_TTL = 6 * 3600       # jak dlouho věřit uloženým cookies bez ověření
SESSION_STORE = "prehrajto_session"
PER_PAGE = 32                # kolik výsledků vrátí jedna strana hledání
MAX_PAGES = 5                # strop stránkování, ať se jedno hledání nerozroste do desítek dotazů
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

REF_RE = re.compile(r"^pt:([A-Za-z0-9._-]{1,120}):([0-9a-f]{8,32})$")
#: Titulky téhož videa — `pts:<slug>:<hash>:<pořadí>`. Podepsaná adresa `.vtt` platí
#: jen den, takže se do výpisu streamů nesmí uložit; dohledá se až při přehrání.
SUB_REF_RE = re.compile(r"^pts:([A-Za-z0-9._-]{1,120}):([0-9a-f]{8,32}):(\d{1,2})$")

#: Jedna položka výpisu. Kotví na `data-video-id`, za ním jde odkaz i titulek;
#: zbytek (stopáž, kvalita, velikost) se dočítá z těla položky.
ITEM_RE = re.compile(
    r'data-video-id="(\d+)">\s*<a class="video[^"]*"\s*href="/([^/"]+)/([0-9a-f]+)"\s*title="([^"]*)"(.*?)</a>',
    re.S)
# `[^"]*` za názvem třídy schválně: server u některých položek přidává další třídu
# (`video__tag--size video__tag--size-alone` u videa bez hlasů) a vzor bez toho
# velikost minul — ve výpisu pak chyběla u poloviny souborů
DUR_RE = re.compile(r'video__tag--time[^"]*">\s*([^<]+)')
QUALITY_RE = re.compile(r'format__text[^"]*">\s*([^<]+)')
SIZE_RE = re.compile(r'video__tag--size[^"]*">\s*([^<]+)')
#: `videos.push({ src: "…", type: 'video/mp4', res: '1080', label: '1080p' })`
SOURCE_RE = re.compile(r"""videos\.push\(\{\s*src:\s*["']([^"']+)["'].*?res:\s*['"](\d+)['"]""", re.S)
#: Titulky. Stránka je nese dvakrát: `var tracks` pro jwplayer má `file:`, tentýž
#: seznam pro videojs má `src:` a navíc `srclang:`. Bereme kterýkoli klíč a jazyk
#: dohledáváme zvlášť, protože u jwplayer varianty `srclang` chybí.
TRACK_RE = re.compile(r"""(?:file|src):\s*["']([^"']+\.(?:vtt|srt)[^"']*)["']""")
TRACK_BLOCK_RE = re.compile(r"var\s+tracks\s*=\s*\[(.*?)\]\s*;", re.S)
TRACK_LANG_RE = re.compile(r"""(?:srclang|label):\s*["']([A-Za-z]{2,4})""")
DAYS_RE = re.compile(r"Vyprší za\s*</?[^>]*>?\s*(\d+)", re.S)
PREMIUM_RE = re.compile(r"PREMIUM\s*(?:<[^>]*>\s*)*(\d+)\s*dn", re.S)

#: Po první 429 se zdroj celému procesu přeskočí na tuhle dobu.
RATE_LIMIT_COOLDOWN = 10 * 60
BLOCK_STORE = "prehrajto_block"
_blocked_until = 0.0


class PrehrajtoError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class PrehrajtoRateLimited(PrehrajtoError):
    """HTTP 429 — Přehraj.to omezuje tuhle IP. Další dotazy jsou zbytečné.
    `paused` = bez dotazu na síť, jen pauza po dřívější 429."""

    paused = False


def blocked_for(cache=None):
    """Kolik sekund ještě Přehraj.to nevolat (0 = lze).

    Paměť procesu stačí na jedno hledání, ale ne na celý doplněk: v Kodi je
    plugin jiný interpret než služba na pozadí. S `cache` se bere pozdější
    z obou konců, takže pauza zapsaná kterýmkoli procesem platí pro všechny.
    """
    konec = _blocked_until
    if cache is not None and hasattr(cache, "load"):
        try:
            konec = max(konec, float((cache.load(BLOCK_STORE, {}) or {}).get("until") or 0))
        except Exception:  # noqa: BLE001 – rozsypaný soubor nesmí shodit hledání
            pass
    return max(0.0, konec - time.time())


def _note_block(cache=None):
    global _blocked_until
    _blocked_until = time.time() + RATE_LIMIT_COOLDOWN
    if cache is not None and hasattr(cache, "save"):
        try:
            cache.save(BLOCK_STORE, {"until": _blocked_until})
        except Exception:  # noqa: BLE001
            pass


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _duration(text):
    """„02:10:46" → sekundy."""
    parts = str(text or "").strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return 0


def _size(text):
    """„2.68 GB" → bajty. Výpis píše velikost původního souboru, ne překódování."""
    m = re.match(r"\s*([\d.,]+)\s*([KMGT]?)i?B", str(text or ""), re.I)
    if not m:
        return 0
    try:
        number = float(m.group(1).replace(",", "."))
    except ValueError:
        return 0
    return int(number * 1024 ** {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}[m.group(2).upper()])


def parse_listing(page):
    """Výpis hledání → položky ve tvaru, se kterým pracuje jádro."""
    out = []
    for m in ITEM_RE.finditer(page):
        body = m.group(5)

        def first(pattern, default=""):
            found = pattern.search(body)
            return found.group(1).strip() if found else default

        size = _size(first(SIZE_RE))
        out.append({
            "id": m.group(1),
            "slug": m.group(2),
            "hash": m.group(3),
            "name": html.unescape(m.group(4)),
            "size": size,
            "size_h": human_size(size) if size else "",
            "duration": _duration(first(DUR_RE)),
            "hd": first(QUALITY_RE).upper() == "HD",
        })
    return out


def make_ref(file):
    return "pt:{slug}:{hash}".format(**file)


def parse_ref(url):
    """`pt:<slug>:<hash>` → adresa stránky videa."""
    m = REF_RE.match(str(url or ""))
    if not m:
        raise PrehrajtoError("neplatný odkaz na soubor")
    return f"{BASE}/{m.group(1)}/{m.group(2)}"


def parse_sub_ref(url):
    """`pts:<slug>:<hash>:<pořadí>` → (adresa stránky videa, pořadí titulků)."""
    m = SUB_REF_RE.match(str(url or ""))
    if not m:
        raise PrehrajtoError("neplatný odkaz na titulky")
    return f"{BASE}/{m.group(1)}/{m.group(2)}", int(m.group(3))


def sub_refs(ref, count):
    """Odkazy na titulky téhož videa: `pt:a:b` + 2 → `["pts:a:b:0", "pts:a:b:1"]`."""
    m = REF_RE.match(str(ref or ""))
    if not m:
        return []
    return [f"pts:{m.group(1)}:{m.group(2)}:{i}" for i in range(count)]


def parse_tracks(page):
    """Titulky ze stránky videa → `[(adresa, jazyk)]`, bez duplicit.

    Stránka nese tentýž seznam dvakrát (jwplayer a videojs) — adresy se shodují,
    takže se druhý průchod jen zahodí. Jazyk bývá v `srclang`/`label` jako „cze",
    „cze1" nebo „CZE - 8138711 - cze"; bereme první dvě až čtyři písmena.
    """
    out, videno = [], set()
    for blok in TRACK_BLOCK_RE.findall(page):
        # položky se dělí čárkou na nejvyšší úrovni; stačí rozdělit podle `}`
        for kus in blok.split("}"):
            m = TRACK_RE.search(kus)
            if not m:
                continue
            # klíč bez dotazu: tentýž soubor přijde v obou blocích s jiným podpisem
            # a bez toho by v seznamu stál dvakrát
            klic = m.group(1).split("?", 1)[0]
            if klic in videno:
                continue
            videno.add(klic)
            lang = TRACK_LANG_RE.search(kus)
            out.append((m.group(1), (lang.group(1) if lang else "").upper()[:3]))
    return out


class PrehrajtoApi:
    def __init__(self, email, password, cache=None, cache_ttl=SEARCH_TTL):
        self.email = (email or "").strip()
        self.password = password or ""
        self.cache = cache
        self.cache_ttl = cache_ttl
        self._cookies = None

    # --- síť ----------------------------------------------------------------
    def _open(self, path, data=None, headers=None, redirect=True):
        url = path if path.startswith("http") else BASE + path
        head = {"User-Agent": UA, "Accept-Language": "cs,sk;q=0.9,en;q=0.8"}
        head.update(headers or {})
        if self._cookies:
            head["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        req = urllib.request.Request(url, data=data, headers=head)
        opener = urllib.request.build_opener(_NoRedirect) if not redirect else urllib.request.build_opener()
        try:
            return opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _note_block(self.cache)
                raise PrehrajtoRateLimited("HTTP 429 — server omezuje tuhle adresu", status=429) from e
            if not redirect and e.code in (301, 302, 303, 307, 308):
                return e            # přesměrování je tu odpověď, ne chyba
            raise PrehrajtoError(f"HTTP {e.code}", status=e.code) from e
        except Exception as e:  # noqa: BLE001 – síť, DNS, rozpadlé spojení
            raise PrehrajtoError(str(e)[:120]) from e

    def _page(self, path, **kw):
        pauza = blocked_for(self.cache)
        if pauza:
            err = PrehrajtoRateLimited(f"pauza po HTTP 429, zbývá {int(pauza)} s", status=429)
            err.paused = True
            raise err
        resp = self._open(path, **kw)
        try:
            # `replace` — v názvech souborů od uživatelů se občas objeví bajt mimo UTF-8
            return resp.read().decode("utf-8", "replace")
        finally:
            resp.close()

    # --- účet ---------------------------------------------------------------
    def _account_key(self):
        return hashlib.sha256(f"{self.email}\0{self.password}".encode("utf-8")).hexdigest()[:16]

    def _saved_cookies(self):
        if self.cache is None or not hasattr(self.cache, "load"):
            return None
        rec = (self.cache.load(SESSION_STORE, {}) or {}).get(self._account_key()) or {}
        if rec.get("cookies") and time.time() < float(rec.get("valid_until") or 0):
            return dict(rec["cookies"])
        return None

    def _save_cookies(self, cookies):
        if self.cache is None or not hasattr(self.cache, "save"):
            return
        data = dict(self.cache.load(SESSION_STORE, {}) or {})
        data[self._account_key()] = {"cookies": dict(cookies), "valid_until": time.time() + SESSION_TTL}
        self.cache.save(SESSION_STORE, data)

    def forget(self):
        """Zahodí přihlášení — po odmítnuté odpovědi nebo při změně hesla."""
        self._cookies = None
        if self.cache is not None and hasattr(self.cache, "save"):
            data = dict(self.cache.load(SESSION_STORE, {}) or {})
            if data.pop(self._account_key(), None) is not None:
                self.cache.save(SESSION_STORE, data)

    def login(self):
        """Přihlásí se jménem a heslem a vrátí cookies relace.

        Každé přihlášení zakládá na serveru záznam v „Správě přihlášených
        zařízení", proto se cookies ukládají a znovu se přihlašuje až po
        `SESSION_TTL`.
        """
        if not self.email or not self.password:
            raise PrehrajtoError("účet není vyplněný")
        self._cookies = None
        resp = self._open("/")                       # bez první návštěvy server relaci nezaloží
        jar = _cookies_from(resp)
        resp.close()
        self._cookies = jar
        data = urllib.parse.urlencode({
            "email": self.email,
            "password": self.password,
            "remember_login": "on",
            "login": "Přihlásit se",
            "_do": "loginDialog-login-loginForm-submit",
        }).encode("utf-8")
        resp = self._open("/?frm=loginDialog-login-loginForm", data=data, headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        jar.update(_cookies_from(resp))
        resp.close()
        if not jar.get("access_token"):
            self._cookies = None
            raise PrehrajtoError("přihlášení se nepovedlo — zkontroluj e-mail a heslo", status=401)
        self._cookies = jar
        self._save_cookies(jar)
        return jar

    def session(self):
        """Cookies z paměti nebo úložiště, dokud jsou čerstvé; jinak nové přihlášení."""
        if self._cookies:
            return self._cookies
        saved = self._saved_cookies()
        if saved:
            self._cookies = saved
            return saved
        return self.login()

    def me(self):
        """{"premium": bool, "days": int} z „Můj účet". Vyžaduje přihlášení."""
        self.session()
        page = self._page("/profil")
        if "loginDialog" in page and "Odhlásit se" not in page:
            self.forget()
            raise PrehrajtoError("relace vypršela", status=401)
        m = PREMIUM_RE.search(page) or DAYS_RE.search(page)
        days = _int(m.group(1)) if m else 0
        return {"premium": bool(m) and days > 0, "days": days}

    # --- hledání ------------------------------------------------------------
    def search(self, query, limit=PER_PAGE):
        """Hledá podle názvu. Bez účtu jen první strana, s účtem se stránkuje.

        Vrací `(položky, celkem)`. `celkem` je jen počet nalezených — server
        celkový počet výsledků nikde nepíše.
        """
        query = str(query or "").strip()
        if not query:
            return [], 0

        def load():
            logged = bool(self.email and self.password)
            if logged:
                self.session()
            out, seen = [], set()
            pages = min(MAX_PAGES, max(1, -(-limit // PER_PAGE))) if logged else 1
            for page in range(1, pages + 1):
                path = "/hledej/" + urllib.parse.quote(query, safe="")
                if page > 1:
                    path += f"?videoListing-visualPaginator-page={page}"
                items = parse_listing(self._page(path))
                nove = [i for i in items if i["hash"] not in seen]
                seen.update(i["hash"] for i in nove)
                out.extend(nove)
                # kratší strana i strana bez nového = konec výpisu
                if len(items) < PER_PAGE or not nove or len(out) >= limit:
                    break
            return out[:limit], len(out[:limit])

        if self.cache is None:
            return load()
        who = "user" if self.email else "anon"
        files, total = self.cache.cached(f"prehrajto:search:{who}:{query}:{limit}",
                                         self.cache_ttl, lambda: list(load()))
        return files, total

    # --- přehrání -----------------------------------------------------------
    def _download_link(self, page_url):
        """Původní soubor přes `?do=download`, nebo prázdno. Vyžaduje Premium.

        Slug v adrese musí sedět — na cizím slugu server odkaz nevydá a jen
        přesměruje zpátky na stránku videa.
        """
        if not (self.email and self.password):
            return ""
        self.session()
        resp = self._open(page_url + "?do=download", redirect=False)
        location = resp.headers.get("Location") or ""
        resp.close()
        return location if "premiumcdn" in location else ""

    def _video_page(self, ref):
        """Stránka videa → (nejlepší překódovaná verze, titulky)."""
        page = self._page(parse_ref(ref))
        sources = [(int(res), url) for url, res in SOURCE_RE.findall(page)]
        if not sources:
            raise PrehrajtoError("na stránce videa není žádný soubor")
        return max(sources)[1], parse_tracks(page)

    def tracks(self, ref):
        """Titulky ze stránky videa jako `[(adresa, jazyk)]`. Prázdné, když nejsou."""
        return self._video_page(ref)[1]

    def _resolve(self, ref):
        """Adresa souboru. S Premium původní soubor, bez něj nejlepší překódovaná
        verze ze stránky videa."""
        link = self._download_link(parse_ref(ref))
        return link or self._video_page(ref)[0]

    def file_link(self, ref):
        """Přímá adresa souboru. Pamatuje se po `LINK_TTL` — podepsaný odkaz
        platí zhruba den."""
        if self.cache is None:
            return self._resolve(ref)
        return self.cache.cached(f"prehrajto:link:{ref}", LINK_TTL, lambda: self._resolve(ref))

    def subtitle_link(self, sub_ref):
        """Adresa souboru titulků z `pts:<slug>:<hash>:<pořadí>`."""
        page_url, index = parse_sub_ref(sub_ref)
        found = parse_tracks(self._page(page_url))
        if index >= len(found):
            raise PrehrajtoError("titulky už na stránce nejsou")
        return found[index][0]

    def request(self, ref):
        """(adresa, hlavičky) — hlavičky nejsou potřeba, odkaz CDN je podepsaný
        a nedrží na IP. Tvar drží kvůli shodě s ostatními zdroji."""
        return self.file_link(ref), {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _cookies_from(resp):
    """Cookies z odpovědi. `http.cookiejar` tu není potřeba — server posílá
    jednoduché `Set-Cookie` bez podmínek a jde nám jen o dvojice jméno/hodnota."""
    jar = {}
    for raw in resp.headers.get_all("Set-Cookie") or []:
        pair = raw.split(";", 1)[0].strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        if value:
            jar[name.strip()] = value.strip()
        else:
            jar.pop(name.strip(), None)   # `expires` v minulosti = zrušení cookie
    return jar
