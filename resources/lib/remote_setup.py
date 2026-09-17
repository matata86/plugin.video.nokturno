"""„Nastavit z mobilu“ — krátkodobý webový server v místní síti.

Hostitel (Kodi) popíše formulář (`schema`), předá aktuální hodnoty a spustí
`SetupServer`. TV ukáže QR s adresou `http://<ip>:<port>/s/<klíč>`, mobil ve
stejné Wi-Fi otevře stránku, vyplní a odešle. Server přijme jedno odeslání,
zkontroluje ho podle schématu a hostiteli vydá jen změněné hodnoty
(`wait_result`). Nic neukládá sám a nesahá na hostitele.

Bezpečnost: server běží jen po dobu nastavování, adresa nese náhodný klíč
(bez něj 404, po `MAX_BAD_REQUESTS` špatných pokusech server skončí), přijme se
jediné odeslání do `MAX_BODY` bajtů. Hesla se na stránku nikdy neposílají —
jen informace, že jsou vyplněná; prázdné pole heslo nemění. Spojení je HTTP:
na domácí Wi-Fi přijatelné, stejně jako webové rozhraní Kodi.

Schéma: `[{"id", "label", "fields": [{"id", "label", "help", "type", "options",
"enable"}]}]`, `type` je `bool`/`text`/`password`/`choice`/`heading`, `options` u
`choice` seznam `(hodnota, popisek)`, `enable` volitelně `(id jiného pole, hodnota)`
— pole je jen zašedlé, když závislost neplatí, odešle se stejně. `heading` je jen
podnadpis uvnitř sekce (např. rozlišení více úložišť) — nemá `id`, do formuláře
se nic neodesílá a validace ho přeskočí.
"""
import hmac
import html
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

DEFAULT_PORTS = range(52100, 52110)
MAX_BODY = 64 * 1024
MAX_BAD_REQUESTS = 30
MAX_TEXT = 1000

TEXTS = {
    "title": "Nokturno — nastavení",
    "intro": "Vyplň, co chceš změnit, a ulož. Nastavení se hned propíše do Kodi.",
    "save": "Uložit do Kodi",
    "saved": "Uloženo. Nastavení je v Kodi, stránku můžeš zavřít.",
    "password_set": "vyplněno — nech prázdné beze změny",
    "expired": "Tahle adresa už neplatí. Na TV spusť Nastavit z mobilu znovu.",
    "invalid": "Neplatná hodnota: {}",
}


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class SetupServer:
    def __init__(self, schema, values, texts=None, token=None):
        self.schema = schema
        self.values = dict(values)
        self.texts = dict(TEXTS, **(texts or {}))
        self.token = token or secrets.token_urlsafe(12)
        self.fields = {f["id"]: f for section in schema for f in section["fields"] if f.get("type") != "heading"}
        self.port = None
        self._httpd = None
        self._thread = None
        self._result = None
        self._done = threading.Event()
        self._bad = 0
        self._lock = threading.Lock()

    # --- životní cyklus ------------------------------------------------------------

    def start(self, host="0.0.0.0", ports=DEFAULT_PORTS):
        """Spustí server na prvním volném portu a vrátí ho; OSError, když není žádný volný."""
        last = None
        for port in ports:
            try:
                self._httpd = _Server((host, port), self._handler())
                break
            except OSError as err:
                last = err
        else:
            raise last or OSError("žádný volný port")
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="nokturno-remote-setup", daemon=True)
        self._thread.start()
        return self.port

    def url(self, ip):
        return f"http://{ip}:{self.port}/s/{self.token}"

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        self._done.set()

    def wait_result(self, timeout=None):
        """Změněné hodnoty `{id: str}` po odeslání, None dokud nic nepřišlo (nebo po `stop()`)."""
        self._done.wait(timeout)
        return self._result

    @property
    def finished(self):
        return self._done.is_set()

    # --- zpracování ----------------------------------------------------------------

    def parse(self, body):
        """Odeslaný formulář → (změny, chyby). Neznámá pole se ignorují."""
        form = urllib.parse.parse_qs(body, keep_blank_values=True, max_num_fields=500)
        changes, errors = {}, []
        for fid, field in self.fields.items():
            kind = field.get("type")
            raw = (form.get(fid) or [None])[-1]
            if kind == "bool":
                value = "true" if raw is not None else "false"
            elif raw is None:
                continue
            elif kind == "password":
                if raw == "":
                    continue
                value = raw[:MAX_TEXT]
            elif kind == "choice":
                allowed = [str(v) for v, _label in field.get("options") or []]
                if raw not in allowed:
                    errors.append(field.get("label") or fid)
                    continue
                value = raw
            else:
                value = raw.strip()[:MAX_TEXT]
            if value != str(self.values.get(fid, "")):
                changes[fid] = value
        return changes, errors

    def render(self, message="", error=False):
        t = self.texts
        esc = html.escape
        parts = []
        for section in self.schema:
            rows = []
            after_heading = False
            for f in section["fields"]:
                if f.get("type") == "heading":
                    rows.append(f'<h4 class="group">{esc(f.get("label") or "")}</h4>')
                    after_heading = True
                    continue
                row_class = "row grouped" if after_heading else "row"
                after_heading = False
                fid, kind, label = f["id"], f.get("type"), esc(f.get("label") or f["id"])
                current = str(self.values.get(fid, ""))
                enable = f.get("enable")
                attrs = f' data-dep="{esc(enable[0])}" data-val="{esc(str(enable[1]))}"' if enable else ""
                help_text = f'<small>{esc(f["help"])}</small>' if f.get("help") else ""
                if kind == "bool":
                    checked = " checked" if current == "true" else ""
                    rows.append(f'<label class="{row_class} bool"{attrs}><span>{label}{help_text}</span>'
                                f'<input type="checkbox" name="{esc(fid)}" id="{esc(fid)}"{checked}></label>')
                elif kind == "choice":
                    opts = "".join(f'<option value="{esc(str(v))}"{" selected" if str(v) == current else ""}>'
                                   f'{esc(str(lab))}</option>' for v, lab in f.get("options") or [])
                    rows.append(f'<label class="{row_class}"{attrs}><span>{label}{help_text}</span>'
                                f'<select name="{esc(fid)}" id="{esc(fid)}">{opts}</select></label>')
                elif kind == "password":
                    hint = esc(t["password_set"]) if current else ""
                    rows.append(f'<label class="{row_class}"{attrs}><span>{label}{help_text}</span>'
                                f'<input type="password" name="{esc(fid)}" id="{esc(fid)}" autocomplete="off" '
                                f'placeholder="{hint}"></label>')
                else:
                    rows.append(f'<label class="{row_class}"{attrs}><span>{label}{help_text}</span>'
                                f'<input type="text" name="{esc(fid)}" id="{esc(fid)}" value="{esc(current)}" '
                                f'autocapitalize="off" autocorrect="off" spellcheck="false"></label>')
            parts.append(f'<details{" open" if section.get("open") else ""}><summary>{esc(section["label"])}'
                         f'</summary>{"".join(rows)}</details>')
        note = f'<p class="note{" err" if error else ""}">{esc(message)}</p>' if message else ""
        return PAGE.format(title=esc(t["title"]), intro=esc(t["intro"]), note=note, sections="".join(parts),
                           save=esc(t["save"]), action=f"/s/{esc(self.token)}")

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Nokturno"
            sys_version = ""

            def log_message(self, *args):
                pass

            def _send(self, code, body, kind="text/html; charset=utf-8"):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                 "form-action 'self'")
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                path = urllib.parse.urlsplit(self.path).path
                ok = path.startswith("/s/") and hmac.compare_digest(path[3:].rstrip("/"), owner.token)
                if not ok:
                    with owner._lock:
                        owner._bad += 1
                        too_many = owner._bad >= MAX_BAD_REQUESTS
                    self._send(404, "Not found", "text/plain; charset=utf-8")
                    if too_many:
                        threading.Thread(target=owner.stop, daemon=True).start()
                return ok

            def do_GET(self):
                if not self._authorized():
                    return
                if owner.finished:
                    self._send(410, SIMPLE.format(text=html.escape(owner.texts["expired"])))
                    return
                self._send(200, owner.render())

            def do_POST(self):
                if not self._authorized():
                    return
                if owner.finished:
                    self._send(410, SIMPLE.format(text=html.escape(owner.texts["expired"])))
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY:
                    self._send(413, "Too large", "text/plain; charset=utf-8")
                    return
                body = self.rfile.read(length).decode("utf-8", "replace")
                changes, errors = owner.parse(body)
                if errors:
                    self._send(400, owner.render(owner.texts["invalid"].format(", ".join(errors)), error=True))
                    return
                with owner._lock:
                    if owner.finished:
                        self._send(410, SIMPLE.format(text=html.escape(owner.texts["expired"])))
                        return
                    owner._result = changes
                    owner._done.set()
                self._send(200, SIMPLE.format(text=html.escape(owner.texts["saved"])))

        return Handler


SIMPLE = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Nokturno</title>
<style>body{{font:17px/1.5 system-ui,sans-serif;background:#12101a;color:#eee;margin:0;padding:12vh 24px;
text-align:center}}p{{max-width:28em;margin:auto}}</style></head><body><p>{text}</p></body></html>"""

PAGE = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{color-scheme:dark;--bg:#12101a;--card:#1d1a28;--line:#2e2a3d;--text:#eeeaf6;--dim:#9b95ad;--accent:#8b5cf6}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);
font:16px/1.45 system-ui,-apple-system,sans-serif}}
main{{max-width:640px;margin:auto;padding:20px 14px 110px}}h1{{font-size:1.35rem;margin:.2em 0}}
.intro{{color:var(--dim);margin:0 0 16px}}
details{{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:0 0 10px;overflow:hidden}}
summary{{padding:14px 16px;font-weight:600;cursor:pointer;list-style:none}}
summary::after{{content:"›";float:right;transition:transform .2s;color:var(--dim)}}
details[open] summary::after{{transform:rotate(90deg)}}
.group{{margin:0;padding:12px 16px 4px;font-size:.8rem;font-weight:600;letter-spacing:.02em;
color:var(--dim);text-transform:uppercase;border-top:1px solid var(--line)}}
.group:first-child{{border-top:0}}
.row{{display:flex;flex-direction:column;gap:6px;padding:12px 16px;border-top:1px solid var(--line)}}
.row.grouped{{border-top:0}}
.row span{{font-size:.95rem}}.row small{{display:block;color:var(--dim);font-size:.8rem;margin-top:2px}}
.row.bool{{flex-direction:row;align-items:center;justify-content:space-between;gap:14px}}
input[type=text],input[type=password],select{{width:100%;font:inherit;color:var(--text);background:var(--bg);
border:1px solid var(--line);border-radius:10px;padding:11px 12px}}
input:focus,select:focus{{outline:2px solid var(--accent);border-color:transparent}}
input[type=checkbox]{{width:26px;height:26px;accent-color:var(--accent);flex:none}}
.off{{opacity:.45}}
.bar{{position:fixed;left:0;right:0;bottom:0;padding:12px 14px calc(12px + env(safe-area-inset-bottom));
background:linear-gradient(transparent,var(--bg) 35%)}}
button{{display:block;width:100%;max-width:640px;margin:auto;font:600 1.05rem system-ui,sans-serif;color:#fff;
background:var(--accent);border:0;border-radius:12px;padding:15px}}
.note{{padding:12px 14px;border-radius:10px;background:#1f3326;color:#bff0cc}}.note.err{{background:#3a1f24;color:#ffc9d0}}
</style></head><body><main>
<h1>{title}</h1><p class="intro">{intro}</p>{note}
<form method="post" action="{action}" autocomplete="off">{sections}
<div class="bar"><button type="submit">{save}</button></div></form></main>
<script>
function sync(){{document.querySelectorAll("[data-dep]").forEach(function(row){{
var dep=document.getElementById(row.dataset.dep);if(!dep)return;
var val=dep.type==="checkbox"?(dep.checked?"true":"false"):dep.value;
row.classList.toggle("off",val!==row.dataset.val);}});}}
document.addEventListener("change",sync);sync();
</script></body></html>"""


def wait_until(server, should_stop, timeout, tick=0.2):
    """Pomocník pro hostitele: čeká na odeslání, přerušení (`should_stop()`) nebo vypršení.

    Vrací `("saved", změny)`, `("stopped", None)` nebo `("timeout", None)`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = server.wait_result(tick)
        if result is not None:
            return "saved", result
        if server.finished:
            return "stopped", None
        if should_stop():
            return "stopped", None
    return "timeout", None

