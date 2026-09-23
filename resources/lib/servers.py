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

Bez závislostí na Kodi ani na zbytku jádra.
"""
import time
import urllib.error
import urllib.request

# Pořadí je pořadí zkoušení. První je zdroj pravdy pro adresy, které si klient
# skládá sám (`COLLECT_URL` a spol.).
BASES = ("https://nokturno.stream", "https://nokturno.tailf0014.ts.net")
BASE = BASES[0]
FAIL_PAUSE = 300

_dead = {}


def note_fail(base):
    """Adresa se nedovolala — na `FAIL_PAUSE` ji odsuň na konec pořadí."""
    _dead[base] = time.time() + FAIL_PAUSE


def alive(base):
    return _dead.get(base, 0) <= time.time()


def _split(url):
    """Adresa → (známý hostitel, zbytek cesty), nebo (None, adresa) u cizí adresy."""
    for base in BASES:
        if url == base or url.startswith(base + "/"):
            return base, url[len(base):]
    return None, url


def variants(url):
    """Táž cesta na všech známých hostitelích; ty, co nedávno selhaly, nakonec."""
    base, rest = _split(url)
    if base is None:
        return [url]
    order = sorted(BASES, key=lambda b: (not alive(b), BASES.index(b)))
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


def urlopen(req, timeout=None):
    """Jako `urllib.request.urlopen`, ale adresu našeho serveru zkusí i na záloze."""
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    last = None
    for target in variants(url):
        base, _ = _split(target)
        try:
            resp = urllib.request.urlopen(_retarget(req, target), timeout=timeout)
        except urllib.error.HTTPError:
            raise   # server odpověděl; druhá adresa vede na tentýž server
        except Exception as e:   # noqa: BLE001 – DNS, spojení, timeout, TLS
            if base:
                note_fail(base)
            last = e
            continue
        if base:
            _dead.pop(base, None)
        return resp
    raise last
