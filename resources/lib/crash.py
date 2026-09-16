"""Hlášení o pádech — neočekávaná výjimka v kódu doplňku jde do dashboardu (obrazovka Pády).

Jen chyby kódu, ne výpadky zdrojů: vypršelý účet, spadlý WebShare nebo DNS chytá
každá větev zvlášť (`LunaError`, `WebshareError`… → upozornění) a sem je nepouští.

Tok:
  1. `CrashReporter.capture(exc, …)` z výjimky složí hlášení: typ, zprávu, zkrácený
     traceback, pár řádků logu doplňku a kontext (verze, platforma, akce). Z textu
     se vymažou adresy, tokeny, hesla, e-maily, IP a jména uživatelů v cestách
     (`scrub`). Hlášení se jen zapíše do fronty v profilu (`crash/`), nic se neposílá
     — plugin Kodi po pádu nemá čekat na síť.
  2. `CrashReporter.flush()` frontu odešle (služba Kodi na pozadí, vlákno Stremia).
     Úspěch nebo odmítnutí serverem (4xx) soubor smaže, výpadek sítě ho nechá na
     příště. Nikdy nevyhodí výjimku.

Otisk (`fingerprint`) je typ výjimky + posledních pár vlastních rámců (soubor
a funkce, bez čísel řádků, ať se nemění s každou verzí). Dashboard podle něj
seskupuje, klient podle něj hlídá, aby tentýž pád z jedné instalace odešel nejvýš
jednou za verzi — widget, který padá při každém překreslení, tak server nezahltí.
Navíc nejvýš `MAX_PER_DAY` nových hlášení denně na instalaci.

Stav v `crash.json` v profilu: {"seen": {"otisk@verze": ts}, "day": "RRRR-MM-DD", "count": n}.
"""
import hashlib
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.request

from stats import COLLECT_URL

CRASH_URL = COLLECT_URL.rsplit("/", 1)[0] + "/crash"

MAX_PER_DAY = 5            # nová hlášení za den na instalaci (dedup otisku zvlášť)
QUEUE_MAX = 20             # víc souborů ve frontě = server dlouho nedostupný, nejstarší pryč
QUEUE_MAX_AGE = 7 * 86400  # staré hlášení už nikomu nepomůže
SEEN_MAX = 300
TIMEOUT = 10
TB_FRAMES = 25             # rámců tracebacku (nejhlubší), zbytek jen „…"
LOG_LINES = 80
LINE_MAX = 400
MESSAGE_MAX = 500
FP_FRAMES = 3

# Značky vlastního kódu v cestě souboru — od nich dál se cesta nechává, zbytek pryč
# (jméno uživatele, disk, instalační složka Kodi).
_OWN_MARKERS = ("plugin.video.nokturno", "nokturno_core", "custom_components/nokturno", "nokturno-stremio",
                "nokturno/")

_URL_RE = re.compile(r"\b([a-z][a-z0-9+.\-]{1,15})://([^\s/'\"<>)\]]*)([^\s'\"<>)\]]*)", re.I)
_SECRET_RE = re.compile(
    r"(?i)\b(token|access_token|refresh_token|password|passwd|pass|heslo|wst|api_?key|apikey|secret|"
    r"auth|authorization|cookie|session|sid|username|user|login|email|e-mail|pin|hash|salt)"
    r"(\s*[=:]\s*['\"]?|['\"]\s*:\s*['\"]?)([^\s'\"&,;)\]}]+)")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOME_RE = re.compile(r"(?i)(/home/|/Users/|[A-Z]:\\Users\\|/var/mobile/Containers/Data/Application/)[^/\\\s'\"]+")
_LONG_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,}\b")
_STREMIO_CFG_RE = re.compile(r"/c/[^/\s'\"]+")


def scrub(text):
    """Text bez adres, tokenů, hesel, e-mailů, IP a jmen uživatelů v cestách."""
    if not text:
        return ""
    text = str(text)

    def url(m):
        scheme, host, rest = m.group(1), m.group(2), m.group(3)
        if scheme.lower() == "plugin":
            action = re.search(r"[?&]action=([\w\-]+)", rest)
            return f"plugin://{host}/" + (f"?action={action.group(1)}" if action else "")
        host = host.rsplit("@", 1)[-1]          # user:heslo@host
        if _IP_RE.fullmatch(host.split(":")[0]):
            host = "<ip>"
        return f"{scheme}://{host}/…" if rest.strip("/") else f"{scheme}://{host}"

    text = _URL_RE.sub(url, text)
    text = _STREMIO_CFG_RE.sub("/c/<nastavení>", text)
    text = _EMAIL_RE.sub("<e-mail>", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = _IP_RE.sub("<ip>", text)
    text = _HOME_RE.sub(lambda m: m.group(1) + "~", text)
    text = _LONG_TOKEN_RE.sub("***", text)
    return text


def short_path(path):
    """Cesta od vlastního kódu dál („plugin.video.nokturno/default.py"), cizí jen jméno souboru."""
    p = str(path or "").replace("\\", "/")
    for marker in _OWN_MARKERS:
        i = p.rfind(marker)
        if i >= 0:
            return p[i:]
    return p.rsplit("/", 1)[-1]


def _is_own(path):
    p = str(path or "").replace("\\", "/")
    return any(m in p for m in _OWN_MARKERS)


def _frames(exc):
    tb = getattr(exc, "__traceback__", None)
    return traceback.extract_tb(tb) if tb is not None else []


def fingerprint(exc_type, frames):
    """12 hex znaků z typu výjimky a posledních vlastních rámců (soubor:funkce, bez řádků)."""
    own = [f for f in frames if _is_own(f.filename)] or list(frames)
    parts = [f"{short_path(f.filename)}:{f.name}" for f in own[-FP_FRAMES:]]
    return hashlib.sha1("|".join([exc_type] + parts).encode("utf-8")).hexdigest()[:12]


def format_traceback(exc, frames):
    """Traceback se zkrácenými cestami. Řádky zdrojového kódu jsou z doplňku (ne data
    uživatele), `scrub` jde jen na zprávu výjimky — jinak by maskoval i `token = …` v kódu.
    Řádky cizího kódu (knihovny Kodi, Pythonu) se vynechají, cestu mohou nést."""
    lines = ["Traceback (most recent call last):"]
    if len(frames) > TB_FRAMES:
        lines.append(f"  … ({len(frames) - TB_FRAMES} vynechaných rámců)")
    for f in frames[-TB_FRAMES:]:
        lines.append(f'  File "{short_path(f.filename)}", line {f.lineno}, in {f.name}')
        if f.line and _is_own(f.filename):
            lines.append("    " + f.line.strip()[:LINE_MAX])
    lines.append(scrub("".join(traceback.format_exception_only(type(exc), exc)).strip()))
    return "\n".join(lines)


def build_report(exc, install_id, product, version, platform="", kodi="", action="", log_lines=(), now=None):
    frames = _frames(exc)
    exc_type = type(exc).__name__
    own = [f for f in frames if _is_own(f.filename)] or frames
    where = f"{short_path(own[-1].filename)}:{own[-1].name}:{own[-1].lineno}" if own else ""
    lines = [scrub(str(line).rstrip())[:LINE_MAX] for line in list(log_lines or ())[-LOG_LINES:]]
    return {
        "id": str(install_id or ""),
        "product": product,
        "version": str(version or "")[:30],
        "platform": str(platform or "")[:30],
        "kodi": str(kodi or "")[:30],
        "action": scrub(action)[:60],
        "fp": fingerprint(exc_type, frames),
        "type": exc_type[:100],
        "message": scrub(str(exc))[:MESSAGE_MAX],
        "where": where[:200],
        "traceback": format_traceback(exc, frames),
        "log": "\n".join(line for line in lines if line),
        "ts": int(now or time.time()),
    }


class CrashReporter:
    """Fronta hlášení v `<profil>/crash/` a stav deduplikace v `<profil>/crash.json`."""

    def __init__(self, directory):
        self.queue_dir = os.path.join(directory, "crash")
        self.state_path = os.path.join(directory, "crash.json")
        self._lock = threading.Lock()

    # --- stav -----------------------------------------------------------------
    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_json(self, path, data):
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    # --- zachycení --------------------------------------------------------------
    def capture(self, exc, install_id, product, version, platform="", kodi="", action="", log_lines=(),
                now=None):
        """Zařadí hlášení do fronty. Vrací True, když se zařadilo; False u opakovaného
        pádu v téže verzi, po dosažení denního limitu nebo při chybě. Nikdy nevyhodí výjimku."""
        try:
            now = int(now or time.time())
            report = build_report(exc, install_id, product, version, platform, kodi, action, log_lines, now)
            with self._lock:
                state = self._load()
                seen = state.get("seen") if isinstance(state.get("seen"), dict) else {}
                key = f"{report['fp']}@{report['version']}"
                if key in seen:
                    return False
                day = time.strftime("%Y-%m-%d", time.localtime(now))
                count = int(state.get("count") or 0) if state.get("day") == day else 0
                if count >= MAX_PER_DAY:
                    return False
                os.makedirs(self.queue_dir, exist_ok=True)
                name = os.path.join(self.queue_dir, f"{now}-{report['fp']}.json")
                if not self._write_json(name, report):
                    return False
                seen[key] = now
                if len(seen) > SEEN_MAX:
                    seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-SEEN_MAX:])
                self._write_json(self.state_path, {"seen": seen, "day": day, "count": count + 1})
            return True
        except Exception:  # noqa: BLE001 – hlášení o pádu nesmí samo nic shodit
            return False

    # --- odeslání ---------------------------------------------------------------
    def pending(self, now=None):
        """Soubory ve frontě od nejstaršího; prošlé a přebývající rovnou smaže."""
        now = now or time.time()
        try:
            names = sorted(n for n in os.listdir(self.queue_dir) if n.endswith(".json"))
        except OSError:
            return []
        keep = []
        for name in names:
            path = os.path.join(self.queue_dir, name)
            try:
                old = now - int(name.split("-", 1)[0]) > QUEUE_MAX_AGE
            except ValueError:
                old = True
            if old:
                _remove(path)
            else:
                keep.append(path)
        for path in keep[:-QUEUE_MAX]:
            _remove(path)
        return keep[-QUEUE_MAX:]

    def flush(self, url=CRASH_URL, agent="Nokturno", should_stop=None):
        """Odešle frontu. Vrací (odesláno, zbývá). Síťová chyba nebo 5xx/429 odesílání
        zastaví a soubor nechá; jiné 4xx (server hlášení odmítl) ho smaže."""
        sent = 0
        with self._lock:
            paths = self.pending()
            for i, path in enumerate(paths):
                if should_stop is not None and should_stop():
                    return sent, len(paths) - i
                try:
                    with open(path, "rb") as f:
                        body = f.read()
                    json.loads(body)
                except (OSError, ValueError):
                    _remove(path)
                    continue
                code = _post(url, body, agent)
                if code is None or code == 429 or code >= 500:
                    return sent, len(paths) - i
                _remove(path)
                if code < 400:
                    sent += 1
        return sent, 0


def _post(url, body, agent):
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": agent,
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read(1024)
            return resp.getcode() or 200
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 – síť, DNS, TLS
        return None


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
