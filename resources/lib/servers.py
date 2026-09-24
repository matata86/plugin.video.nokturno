"""Adresy serveru Nokturna: vlastní doména napřed, Tailscale Funnel jako záloha.

Do 2026-09-22 měl server jedinou veřejnou adresu — `nokturno.tailf0014.ts.net`
přes Tailscale Funnel. Když Tailscale přestal vydávat ingress pro celý tailnet,
spadlo s ní naráz všechno: doplněk pro Stremio, žebříčky, katalogy z dashboardu,
statistiky, hlášení o pádech, odeslané logy i synchronizace. Od 2026-09-23 má
server vlastní doménu `nokturno.stream` (Cloudflare Tunnel z LXC 124) a klient
zná obě adresy: zkusí první, a když se na ni nedostane, sáhne po druhé. Obě
vedou na tentýž nginx, takže je jedno, kterou se to povede.

Odpověď serveru se za výpadek **nepovažuje** — na HTTP chybu (404, 429, 500) se
druhá adresa nezkouší, vrátila by totéž. Přepíná se jen při chybě spojení: DNS,
odmítnuté spojení, timeout, TLS.

Adresa, která selhala, se na `FAIL_PAUSE` přeskakuje, ať se za každý požadavek
neplatí timeout znovu. Paměť je jen v procesu: služba na pozadí si ji drží,
plugin v Kodi startuje pokaždé znovu a zaplatí tedy nejvýš jeden timeout navíc.

Když selžou obě, klient si stáhne záchranný seznam adres z GitHubu
(`DIRECTORY_URL`) a zkusí adresy z něj. Novou adresu serveru (třeba VPS po
výpadku Cloudflaru) tak jde přidat jedním commitem do `servers.json`, bez
vydání klientů — stačí, aby klient měl tuhle verzi jádra dřív, než výpadek
přijde. Chybu spojení tu zastupuje i odpověď `EDGE_DOWN`: Cloudflare při
vypnutém tunelu nebo zóně odpoví sám (530, 52x), spojení se tedy naváže.

Bez závislostí na Kodi ani na zbytku jádra.
"""
import json
import re
import time
import urllib.error
import urllib.request

# Pořadí je pořadí zkoušení. První je zdroj pravdy pro adresy, které si klient
# skládá sám (`COLLECT_URL` a spol.).
BASES = ("https://nokturno.stream", "https://nokturno.tailf0014.ts.net")
BASE = BASES[0]
FAIL_PAUSE = 300

DIRECTORY_URL = "https://raw.githubusercontent.com/matata86/nokturno-core/main/servers.json"
DIRECTORY_TTL = 3600
# odpovědi, které dává Cloudflare místo našeho serveru (tunel neběží, zóna vypnutá)
EDGE_DOWN = {502, 521, 522, 523, 524, 525, 526, 530}
_BASE_RE = re.compile(r"https://[a-z0-9.-]+(:[0-9]{1,5})?")

_dead = {}
_extra = []           # adresy ze záchranného seznamu, jen v paměti procesu
_directory_at = 0.0


def known():
    return BASES + tuple(_extra)


def note_fail(base):
    """Adresa se nedovolala — na `FAIL_PAUSE` ji odsuň na konec pořadí."""
    _dead[base] = time.time() + FAIL_PAUSE


def alive(base):
    return _dead.get(base, 0) <= time.time()


def _split(url):
    """Adresa → (známý hostitel, zbytek cesty), nebo (None, adresa) u cizí adresy."""
    for base in known():
        if url == base or url.startswith(base + "/"):
            return base, url[len(base):]
    return None, url


def variants(url):
    """Táž cesta na všech známých hostitelích; ty, co nedávno selhaly, nakonec."""
    base, rest = _split(url)
    if base is None:
        return [url]
    bases = known()
    order = sorted(bases, key=lambda b: (not alive(b), bases.index(b)))
    return [b + rest for b in order]


# Cloudflare před `nokturno.stream` odmítá výchozí `Python-urllib/3.x` s 403 (error 1010,
# „browser signature banned") — od přechodu na doménu 2026-09-23 tak neprošla synchronizace
# ani přenos nastavení, které hlavičku nenastavovaly. Kdo žádnou nemá, dostane tuhle.
USER_AGENT = "Nokturno"


def _retarget(req, url):
    """Týž požadavek (metoda, hlavičky, tělo) na jinou adresu, vždy s `User-Agent`."""
    if not isinstance(req, urllib.request.Request):
        req = urllib.request.Request(url)
    headers = dict(req.header_items())
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = USER_AGENT
    return urllib.request.Request(url, data=req.data, headers=headers, method=req.get_method())


def _load_directory():
    """Stáhni záchranný seznam; True, když přibyla nová adresa. Nejvýš jednou za `DIRECTORY_TTL`."""
    global _directory_at
    if time.time() - _directory_at < DIRECTORY_TTL:
        return False
    _directory_at = time.time()
    try:
        req = urllib.request.Request(DIRECTORY_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read(65536).decode("utf-8"))
        bases = [str(b).rstrip("/") for b in data.get("bases", [])]
    except Exception:   # noqa: BLE001 – GitHub nedostupný nebo vadný soubor
        return False
    added = [b for b in bases if _BASE_RE.fullmatch(b) and b not in known()]
    _extra.extend(added)
    return bool(added)


def urlopen(req, timeout=None):
    """Jako `urllib.request.urlopen`, ale adresu našeho serveru zkusí i na zálohách."""
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    last = None
    tried = set()
    while True:
        for target in variants(url):
            if target in tried:
                continue
            tried.add(target)
            base, _ = _split(target)
            try:
                resp = urllib.request.urlopen(_retarget(req, target), timeout=timeout)
            except urllib.error.HTTPError as e:
                if not (base and e.code in EDGE_DOWN):
                    raise   # server odpověděl; další adresa vede na tentýž server
                note_fail(base)
                last = e
                continue
            except Exception as e:   # noqa: BLE001 – DNS, spojení, timeout, TLS
                if base:
                    note_fail(base)
                last = e
                continue
            if base:
                _dead.pop(base, None)
            return resp
        if _split(url)[0] is None or not _load_directory():
            raise last
