"""Přehraj.to — hledání videí a odkaz na přehrání.

Server má **dvě rozhraní**:

* **JSON API** `https://prehrajto.cz/api/v2/` (doména `.cz`), které používá jejich
  oficiální appka pro Android (`to.prehraj.app`). Každý dotaz chce `Authorization:
  Bearer <JWT>`; bez tokenu vrací 401 „Missing access token" a **anonymní token
  nevydává** (žádný guest/anonymous endpoint neexistuje). Token je tentýž JWT, který
  web ukládá do cookie `access_token` po přihlášení — bere se proto z relace založené
  `login()` a obnovuje se, jakmile vyprší (JWT platí ~10 min, web ho na `GET /`
  vydá znovu z cookie `refresh_token`).
* **HTML** `https://prehraj.to/` — tytéž stránky jako prohlížeč, čte je i cizí doplněk
  pro Kodi (`plugin.video.prehrajto` 2.0.5). Bez účtu je vidět jen první strana
  hledání (32 položek) a přehraje se překódované 1080p.

Proto: **s účtem se jede přes JSON API** (stránkování `offset`, hlasy i titulky
rovnou ve výsledku hledání, přímý odkaz na **původní soubor** jedním dotazem, a hlavně
**bez plovoucí 429** — 40 souběžných hledání prošlo, kdežto HTML scraping dostával 429
už od ~20). **Bez účtu se čte HTML** (jediná možná cesta) — první strana, překódované
1080p, žádný originál.

`videos/{id}/download` (JSON) i `?do=download` (HTML) vydají podepsaný odkaz na CDN
(`premiumcdn.net`). Odkaz **nedrží na IP** (ověřeno ze dvou sítí) a nechce hlavičky ani
cookie, takže hraje i tam, kde nemůže projít proxy — v Kodi, v Stremiu i ve stahování.
Platí zhruba den (`expires` v adrese), dohledává se proto až při přehrání, jako u
WebShare a HellSpy.

Odkaz nese id, slug i hash (`pt:<id>:<slug>:<hash>`). Id potřebuje JSON API pro
download; slug a hash je adresa stránky videa pro HTML zálohu. Starší tvar bez id
(`pt:<slug>:<hash>`, z HTML výpisu bez účtu) se čte dál — použije jen HTML cestu.

Vlastnost serveru, se kterou je nutné počítat i na JSON API:

* **HTTP 429.** Na HTML je limit plovoucí a nepravidelný (dvacet dotazů v dávce projde,
  dvanáct po 1,5 s ne); JSON API tenhle strop nemá. Po první 429 (odkudkoli) se zdroj
  celému procesu přeskočí na `RATE_LIMIT_COOLDOWN`, stejně jako HellSpy (6.0.2, 6.0.4):
  opakované dotazy blokaci jen prodlužují. HTML dotazy navíc jdou po `MIN_GAP`
  (`_wait_for_slot`) za sebou — JSON dotazy tímhle frontu nedrží, limit tam není.
* **Prázdná odpověď na některé dotazy.** Hledání je citlivé na pořadí slov: `okresni
  prebor` vrátí nula výsledků, `prebor okresni` plnou stranu. Vada jejich indexu, ne
  pravidlo. Jádro zkouší víc variant názvu (`MAX_TITLE_VARIANTS`), takže se přes to
  obvykle přenese samo.
"""
import base64
import hashlib
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from badlogin import login_paused, mark_bad_login
from streams import human_size

BASE = "https://prehraj.to"
API_BASE = "https://prehrajto.cz/api/v2"
TIMEOUT = 20
SEARCH_TTL = 12 * 3600
LINK_TTL = 6 * 3600          # podepsaný odkaz CDN platí ~24 h, bereme si rezervu
SESSION_TTL = 6 * 3600       # jak dlouho věřit uloženým cookies bez ověření
SESSION_STORE = "prehrajto_session"
PER_PAGE = 32                # kolik výsledků vzít na jednu stranu (HTML dává vždy 32)
MAX_PAGES = 5                # strop stránkování, ať se jedno hledání nerozroste do desítek dotazů
JWT_SKEW = 60                # kolik sekund před vypršením JWT ho už považovat za starý
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

#: `pt:<id>:<slug>:<hash>` (z JSON API, id pro download) nebo starší `pt:<slug>:<hash>`
#: (z HTML výpisu bez účtu, jen HTML cesta).
REF_ID_RE = re.compile(r"^pt:(\d{1,15}):([A-Za-z0-9._-]{1,120}):([0-9a-f]{8,32})$")
REF_RE = re.compile(r"^pt:([A-Za-z0-9._-]{1,120}):([0-9a-f]{8,32})$")
#: Titulky téhož videa — `pts:[<id>:]<slug>:<hash>:<pořadí>`. Podepsaná adresa `.vtt`
#: platí jen den, takže se do výpisu streamů nesmí uložit; dohledá se až při přehrání.
SUB_ID_RE = re.compile(r"^pts:(\d{1,15}):([A-Za-z0-9._-]{1,120}):([0-9a-f]{8,32}):(\d{1,2})$")
SUB_RE = re.compile(r"^pts:([A-Za-z0-9._-]{1,120}):([0-9a-f]{8,32}):(\d{1,2})$")

#: Jedna položka HTML výpisu. Kotví na `data-video-id`, za ním jde odkaz i titulek;
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

#: Odstup mezi dvěma **HTML** dotazy, ať jdou vlákna za sebou. JSON API tuhle frontu
#: nedrží — plovoucí limit je jen na HTML scrapingu, ověřeno 40 souběžnými dotazy.
MIN_GAP = 1.5
#: Kdo by na svůj termín čekal déle, dotaz vynechá; fronta desítek kandidátů katalogu
#: by jinak držela vlákna minuty a zdroj by se hlásil jako výpadek celého hledání.
MAX_QUEUE_WAIT = 8.0
_gate = threading.Lock()
_next_slot = 0.0


class PrehrajtoError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status
        self.paused = False


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
    """HTML výpis hledání → položky ve tvaru, se kterým pracuje jádro."""
    out = []
    for m in ITEM_RE.finditer(page):
        body = m.group(5)

        def first(pattern, default=""):
            found = pattern.search(body)
            return found.group(1).strip() if found else default

        size = _size(first(SIZE_RE))
        out.append({
            "id": "",                      # HTML výpis id nenese, jen JSON API
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
    """`pt:<id>:<slug>:<hash>` s id z JSON API, jinak starší `pt:<slug>:<hash>`."""
    vid = str(file.get("id") or "").strip()
    if vid:
        return "pt:{id}:{slug}:{hash}".format(**file)
    return "pt:{slug}:{hash}".format(**file)


def _split_ref(url):
    """`pt:[<id>:]<slug>:<hash>` → (id nebo "", slug, hash). Neplatný → výjimka."""
    m = REF_ID_RE.match(str(url or ""))
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = REF_RE.match(str(url or ""))
    if m:
        return "", m.group(1), m.group(2)
    raise PrehrajtoError("neplatný odkaz na soubor")


def parse_ref(url):
    """`pt:[<id>:]<slug>:<hash>` → adresa stránky videa (HTML)."""
    _vid, slug, h = _split_ref(url)
    return f"{BASE}/{slug}/{h}"


def _split_sub_ref(url):
    """`pts:[<id>:]<slug>:<hash>:<pořadí>` → (id nebo "", slug, hash, pořadí)."""
    m = SUB_ID_RE.match(str(url or ""))
    if m:
        return m.group(1), m.group(2), m.group(3), int(m.group(4))
    m = SUB_RE.match(str(url or ""))
    if m:
        return "", m.group(1), m.group(2), int(m.group(3))
    raise PrehrajtoError("neplatný odkaz na titulky")


def parse_sub_ref(url):
    """`pts:[<id>:]<slug>:<hash>:<pořadí>` → (adresa stránky videa, pořadí)."""
    _vid, slug, h, index = _split_sub_ref(url)
    return f"{BASE}/{slug}/{h}", index


def sub_refs(ref, count):
    """Odkazy na titulky téhož videa: `pt:a:b` + 2 → `["pts:a:b:0", "pts:a:b:1"]`."""
    try:
        vid, slug, h = _split_ref(ref)
    except PrehrajtoError:
        return []
    prefix = f"pts:{vid}:{slug}:{h}" if vid else f"pts:{slug}:{h}"
    return [f"{prefix}:{i}" for i in range(count)]


def parse_tracks(page):
    """Titulky z HTML stránky videa → `[(adresa, jazyk)]`, bez duplicit.

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


def _jwt_expired(token):
    """JWT je (skoro) po platnosti? Rozsypaný token = ano, ať se obnoví."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0
        return time.time() + JWT_SKEW >= float(exp)
    except Exception:  # noqa: BLE001
        return True


class PrehrajtoApi:
    def __init__(self, email, password, cache=None, cache_ttl=SEARCH_TTL):
        self.email = (email or "").strip()
        self.password = password or ""
        self.cache = cache
        self.cache_ttl = cache_ttl
        self._cookies = None
        # sdílená instance (Stremio, jeden účet instance) běží z víc vláken naráz —
        # přihlášení a obnova tokenu se nesmí spustit dvakrát zároveň
        self._lock = threading.RLock()

    @property
    def _account(self):
        return bool(self.email and self.password)

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
                raise PrehrajtoRateLimited("HTTP 429 – server omezuje tuto adresu", status=429) from e
            if not redirect and e.code in (301, 302, 303, 307, 308):
                return e            # přesměrování je tu odpověď, ne chyba
            raise PrehrajtoError(f"HTTP {e.code}", status=e.code) from e
        except Exception as e:  # noqa: BLE001 – síť, DNS, rozpadlé spojení
            raise PrehrajtoError(str(e)[:120]) from e

    def _paused(self):
        """Zvedne `PrehrajtoRateLimited`, pokud po dřívější 429 ještě běží pauza."""
        zbyva = blocked_for(self.cache)
        if zbyva:
            err = PrehrajtoRateLimited(f"pauza po HTTP 429, zbývá {int(zbyva)} s", status=429)
            err.paused = True
            raise err

    @staticmethod
    def _wait_for_slot():
        """Zařadí HTML dotaz do fronty: termíny se rozdávají po `MIN_GAP`, spí se mimo zámek."""
        global _next_slot
        with _gate:
            now = time.time()
            slot = max(now, _next_slot)
            if slot - now > MAX_QUEUE_WAIT:
                err = PrehrajtoError("fronta dotazů je plná, přeskočeno")
                err.paused = True   # není to chyba serveru — loguje se jen ladicí řádek
                raise err
            _next_slot = slot + MIN_GAP
        if slot > now:
            time.sleep(slot - now)

    def _page(self, path, **kw):
        """Jedna HTML stránka jako text. Drží frontu `MIN_GAP` (plovoucí 429 scrapingu)."""
        self._paused()
        self._wait_for_slot()
        self._paused()   # za tu dobu mohla jiná fronta narazit na 429
        resp = self._open(path, **kw)
        try:
            # `replace` — v názvech souborů od uživatelů se občas objeví bajt mimo UTF-8
            return resp.read().decode("utf-8", "replace")
        finally:
            resp.close()

    def _api(self, path, params=None):
        """JSON API `videos/…` s Bearer tokenem → `payload`. Bez fronty `MIN_GAP`
        (JSON API plovoucí limit nemá), pauzu po 429 ale respektuje.

        Vypršelý token API odmítne 401 — obnoví se a dotaz se zopakuje jednou.
        """
        self._paused()
        url = f"{API_BASE}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last = None
        for attempt in (False, True):
            token = self._bearer(force=attempt)
            try:
                resp = self._open(url, headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                })
            except PrehrajtoError as e:
                if e.status == 401 and not attempt:
                    last = e
                    continue        # token vypršel — obnov a zkus znovu
                raise
            try:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            finally:
                resp.close()
            if body.get("status") == "ok":
                return body.get("payload") or {}
            zprava = str((body.get("payload") or {}).get("message") or "chyba API")[:120]
            if body.get("code") == 401 and not attempt:
                last = PrehrajtoError(zprava, status=401)
                continue
            raise PrehrajtoError(zprava, status=body.get("code"))
        raise last or PrehrajtoError("API nevydalo odpověď", status=401)

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
        """Přihlásí se jménem a heslem a vrátí cookies relace (vč. `access_token`).

        Každé přihlášení zakládá na serveru záznam v „Správě přihlášených
        zařízení", proto se cookies ukládají a znovu se přihlašuje až po
        `SESSION_TTL`.
        """
        if not self._account:
            raise PrehrajtoError("účet není vyplněný")
        if login_paused("prehrajto", self.email, self.password, self.cache):
            err = PrehrajtoError("přihlášení se nepovedlo – zkontroluj e-mail a heslo", status=401)
            err.paused = True
            raise err
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
            mark_bad_login("prehrajto", self.email, self.password, self.cache)
            raise PrehrajtoError("přihlášení se nepovedlo – zkontroluj e-mail a heslo", status=401)
        self._cookies = jar
        self._save_cookies(jar)
        return jar

    def session(self):
        """Cookies z paměti nebo úložiště, dokud jsou čerstvé; jinak nové přihlášení."""
        with self._lock:
            if self._cookies:
                return self._cookies
            saved = self._saved_cookies()
            if saved:
                self._cookies = saved
                return saved
            return self.login()

    def _refresh_web(self):
        """`GET /` s cookie `refresh_token` → web vydá nový `access_token`. True = obnoveno."""
        try:
            resp = self._open("/")
            jar = _cookies_from(resp)
            resp.close()
        except PrehrajtoError:
            return False
        if jar.get("access_token"):
            self._cookies = {**(self._cookies or {}), **jar}
            self._save_cookies(self._cookies)
            return True
        return False

    def _bearer(self, force=False):
        """Čerstvý JWT pro `Authorization: Bearer`. Vypršelý obnoví přes web, a když
        propadl i `refresh_token`, přihlásí se celé znovu."""
        with self._lock:
            self.session()
            token = (self._cookies or {}).get("access_token")
            if not force and token and not _jwt_expired(token):
                return token
            if self._refresh_web():
                token = (self._cookies or {}).get("access_token")
                if token and not _jwt_expired(token):
                    return token
            self.forget()
            self.login()
            token = (self._cookies or {}).get("access_token")
            if not token:
                raise PrehrajtoError("přihlášení nevydalo token", status=401)
            return token

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
    @staticmethod
    def _map_item(it):
        """Položka JSON API → tvar, se kterým pracuje jádro (stejný jako z HTML)."""
        size = _int(it.get("size"))
        return {
            "id": str(it.get("id") or ""),
            "slug": it.get("slug") or "",
            "hash": it.get("hash") or "",
            "name": it.get("name") or "",
            "size": size,
            "size_h": human_size(size) if size else "",
            "duration": _int(it.get("length")),
            "hd": bool(it.get("hd")),
        }

    def _search_api(self, query, limit):
        """JSON API se stránkováním přes `offset`. Nese id, takže download jde přímo."""
        out, seen = [], set()
        offset = 0
        for _page in range(MAX_PAGES):
            payload = self._api("videos/search", {"phrase": query, "limit": PER_PAGE, "offset": offset})
            data = (payload or {}).get("data") or []
            nove = 0
            for it in data:
                h = it.get("hash")
                if not h or h in seen:
                    continue
                seen.add(h)
                out.append(self._map_item(it))
                nove += 1
            # kratší strana i strana bez nového = konec výpisu
            if len(data) < PER_PAGE or not nove or len(out) >= limit:
                break
            offset += len(data)
        return out[:limit], len(out[:limit])

    def _search_html(self, query):
        """HTML výpis bez účtu — server dál chce přihlášení, druhou stranu nevydá."""
        path = "/hledej/" + urllib.parse.quote(query, safe="")
        items = parse_listing(self._page(path))
        return items, len(items)

    def search(self, query, limit=PER_PAGE):
        """Hledá podle názvu. S účtem přes JSON API (stránkování), bez účtu HTML
        (jen první strana). Vrací `(položky, celkem)`; `celkem` = počet nalezených."""
        query = str(query or "").strip()
        if not query:
            return [], 0

        def load():
            if self._account:
                return self._search_api(query, limit)
            return self._search_html(query)

        if self.cache is None:
            return load()
        who = "user" if self.email else "anon"
        files, total = self.cache.cached(f"prehrajto:search:{who}:{query}:{limit}",
                                         self.cache_ttl, lambda: list(load()))
        return files, total

    # --- přehrání -----------------------------------------------------------
    def _api_download(self, vid):
        """Podepsaný odkaz na **původní soubor** přes JSON API. Prázdno = nedostupný."""
        payload = self._api(f"videos/{vid}/download")
        data = (payload or {}).get("data") or {}
        return data.get("download_link") or ""

    def _html_download(self, page_url):
        """Původní soubor přes HTML `?do=download`. Vyžaduje Premium; slug musí sedět,
        na cizím slugu server jen přesměruje zpátky na stránku videa."""
        if not self._account:
            return ""
        self.session()
        resp = self._open(page_url + "?do=download", redirect=False)
        location = resp.headers.get("Location") or ""
        resp.close()
        return location if "premiumcdn" in location else ""

    def _html_page(self, page_url):
        """HTML stránka videa → (nejlepší překódovaná verze, titulky)."""
        page = self._page(page_url)
        sources = [(int(res), url) for url, res in SOURCE_RE.findall(page)]
        if not sources:
            raise PrehrajtoError("na stránce videa není žádný soubor")
        return max(sources)[1], parse_tracks(page)

    def _api_tracks(self, vid):
        """Titulky z detailu JSON API → `[(adresa, jazyk)]`."""
        payload = self._api(f"videos/{vid}")
        data = (payload or {}).get("data") or {}
        out = []
        for sub in data.get("subtitles") or []:
            url = sub.get("cdnUrl")
            if url:
                out.append((url, str(sub.get("language") or "").upper()[:3]))
        return out

    def tracks(self, ref):
        """Titulky videa jako `[(adresa, jazyk)]`. S účtem z JSON API, jinak z HTML."""
        vid, slug, h = _split_ref(ref)
        if vid and self._account:
            try:
                return self._api_tracks(vid)
            except PrehrajtoRateLimited:
                raise
            except PrehrajtoError:
                pass                # JSON selhalo → zkus HTML stránku
        return self._html_page(f"{BASE}/{slug}/{h}")[1]

    def _resolve(self, ref):
        """Adresa souboru. S Premium a id přímo z JSON API (původní soubor), jinak
        HTML: `?do=download` (Premium) nebo nejlepší překódovaná verze."""
        vid, slug, h = _split_ref(ref)
        if vid and self._account:
            try:
                link = self._api_download(vid)
                if link:
                    return link
            except PrehrajtoRateLimited:
                raise
            except PrehrajtoError:
                pass                # spadni na HTML
        page_url = f"{BASE}/{slug}/{h}"
        link = self._html_download(page_url)
        return link or self._html_page(page_url)[0]

    def file_link(self, ref):
        """Přímá adresa souboru. Pamatuje se po `LINK_TTL` — podepsaný odkaz
        platí zhruba den."""
        if self.cache is None:
            return self._resolve(ref)
        return self.cache.cached(f"prehrajto:link:{ref}", LINK_TTL, lambda: self._resolve(ref))

    def subtitle_link(self, sub_ref):
        """Adresa souboru titulků z `pts:[<id>:]<slug>:<hash>:<pořadí>`."""
        vid, slug, h, index = _split_sub_ref(sub_ref)
        if vid and self._account:
            try:
                found = self._api_tracks(vid)
            except PrehrajtoRateLimited:
                raise
            except PrehrajtoError:
                found = self._html_page(f"{BASE}/{slug}/{h}")[1]
        else:
            found = parse_tracks(self._page(f"{BASE}/{slug}/{h}"))
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
