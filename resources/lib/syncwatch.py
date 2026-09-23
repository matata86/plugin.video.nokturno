"""SyncWatch: společné sledování jednoho pořadu na víc zařízeních.

Jeden založí skupinu a dostane kód `SW-7K2Q-9MFX`; kdo ho opíše na jiném Kodi,
připojí se. Vedoucí skupiny pouští titul a stream, pauzu, play a přetočení smí
kdokoli — a projeví se u všech.

Server (dashboard, `/syncwatch/*`) jen přeposílá. Je **slepý** jako synchronizace
a přenos nastavení (`sealbox.py`): skupina se na serveru jmenuje `ident`
jednosměrně odvozeným z kódu a všechno, co by prozradilo, co se sleduje (odkaz
na stream, id titulu, jména zařízení), je zapečetěné klíčem z kódu. Server vidí
jen pořadí, čas, počty a velikosti.

Stav skupiny je **jeden záznam** (`state`), ne proud událostí: kdo se připojí
pozdě nebo po výpadku, přečte poslední stav a je hned srovnaný. Stav nese pozici
`pos` k serverovému času `at`; když se hraje, cílová pozice je
`pos + (teď na serveru − at)`. Posun hodin zařízení proti serveru se měří
z každé odpovědi (`Clock`).

Každé zařízení má vedle toho vlastní malý záznam (`me`: jméno, připraveno,
bufferuje) — z něj vedoucí pozná, že se všichni načetli, a bufferující zařízení,
jestli ještě někdo jiný nečeká.

Logika synchronizace (`Coordinator`) nezná Kodi: přehrávač dostane jako objekt
s pár metodami (`position`, `playing`, `caching`, `pause`, `resume`, `seek`,
`load`, `stop`), takže jde otestovat simulací bez sítě a bez Kodi.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import queue
except ImportError:  # pragma: no cover
    import Queue as queue  # noqa: N813

from sealbox import SealError, keys_for, new_code as _new_code, normalize_code, seal, unseal, \
    valid_code as _valid_code
from servers import urlopen as open_url
from stats import COLLECT_URL

SW_URL = COLLECT_URL.rsplit("/", 1)[0] + "/syncwatch"
SALT = b"nokturno-syncwatch-v1"
CODE_LEN = 8
PREFIX = "SW"

MAX_MEMBERS = 5          # včetně vedoucího; shoda se serverem (`backend/syncwatch.py`)
EMPTY_CLOSE = 300        # s bez připojeného zařízení → skupina zanikne (hlídá server)
POLL_WAIT = 25           # s long-pollu; Cloudflare drží spojení až 100 s
TIMEOUT = 10             # s na ostatní požadavky

TOLERANCE = 1.5          # s odchylky, kterou ještě snese — pod tím se nepřetáčí
CORRECT_EVERY = 10       # s mezi dvěma tichými srovnáními jednoho zařízení
ECHO_WINDOW = 2.5        # s po provedení cizího příkazu, kdy se vlastní události přehrávače nehlásí
START_WAIT = 20          # s, jak dlouho vedoucí čeká, než se všichni načtou
BUFFER_AFTER = 3.0       # s načítání, po kterých se na zařízení čeká
LOAD_TIMEOUT = 60        # s na rozklíčování a start streamu u člena
REPLAY_PREFIX = "plugin://plugin.video.nokturno/"
REPLAY_ACTIONS = ("play", "play_ws", "play_hs", "play_dav", "play_ref")


class SyncWatchError(Exception):
    """Chyba, kterou má smysl ukázat uživateli."""


class Closed(SyncWatchError):
    """Skupina už neexistuje (vedoucí ji ukončil, nebo se 5 minut nikdo nepřipojil)."""

    def __init__(self, reason=""):
        SyncWatchError.__init__(self, reason or "Skupina skončila")
        self.reason = reason


# --- kód ------------------------------------------------------------------------------

def new_code():
    return pretty_code(_new_code(CODE_LEN))


def _raw(text):
    raw = "".join((text or "").split()).replace("-", "").upper()
    if raw.startswith(PREFIX) and len(raw) == CODE_LEN + len(PREFIX):
        raw = raw[len(PREFIX):]
    return normalize_code(raw)


def valid_code(text):
    return _valid_code(_raw(text), CODE_LEN)


def pretty_code(text):
    """`7K2Q9MFX` → `SW-7K2Q-9MFX`. Jiný prefix než přenos nastavení (`NKT-`), ať
    si je uživatel nesplete."""
    raw = _raw(text)
    return "-".join([PREFIX, raw[:4], raw[4:]])


def keys(code):
    if not valid_code(code):
        raise SyncWatchError("Kód nemá správný tvar (SW-XXXX-XXXX)")
    try:
        return keys_for(_raw(code), SALT, CODE_LEN)
    except SealError as e:
        raise SyncWatchError(str(e))


def valid_replay(url):
    """Stream se u ostatních pouští adresou pluginu, kterou poslal vedoucí. Člen
    skupiny ji může podvrhnout — pustí se tedy jen adresa Nokturna s přehrávací
    akcí, nic jiného."""
    if not isinstance(url, str) or not url.startswith(REPLAY_PREFIX) or len(url) > 1500:
        return False
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return (query.get("action") or [""])[0] in REPLAY_ACTIONS


def with_sw(url):
    """Adresa přehrání pro člena skupiny: `sw=1` vypne dialog výběru streamu
    a bod pokračování (pozici určuje skupina, ne historie zařízení)."""
    return url + ("&" if "?" in url else "?") + "sw=1"


# --- hodiny -----------------------------------------------------------------------------

class Clock(object):
    """Posun místních hodin proti serveru. Server v každé odpovědi posílá `now`
    (ms); bere se medián posledních měření, ať jedna pomalá odpověď nepřehodí čas."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._samples = []

    def sample(self, server_ms, sent, received):
        rtt = max(0.0, received - sent)
        # long-poll drží spojení dlouho — `now` je z konce, odhad = příjem mínus půl cesty
        # zpátky, kterou u long-pollu neznáme; bereme tedy jen krátké odpovědi naplno
        if rtt > 3.0:
            offset = server_ms / 1000.0 - received
        else:
            offset = server_ms / 1000.0 - (sent + received) / 2.0
        self._samples = (self._samples + [offset])[-7:]

    @property
    def offset(self):
        if not self._samples:
            return 0.0
        s = sorted(self._samples)
        return s[len(s) // 2]

    def server_now(self):
        return self.clock() + self.offset


def target_position(state, server_now):
    """Kde má být přehrávání teď, podle stavu skupiny."""
    pos = float(state.get("pos") or 0)
    if state.get("playing"):
        pos += max(0.0, server_now - float(state.get("at") or 0) / 1000.0)
    total = float(((state.get("load") or {}).get("total")) or 0)
    if total > 0:
        pos = min(pos, max(0.0, total - 1))
    return pos


# --- klient serveru -----------------------------------------------------------------------

class Client(object):
    """HTTP klient. Kód ani nic nešifrovaného neposílá: `ident` a token chodí
    v hlavičkách (cesty se logují), obsah jen jako zapečetěný blob v base64."""

    def __init__(self, code, token="", base_url=SW_URL, clock=None, timeout=TIMEOUT):
        self.keys = keys(code)
        self.ident = self.keys.ident
        self.token = token
        self.base = (base_url or SW_URL).rstrip("/")
        self.clock = clock or Clock()
        self.timeout = timeout
        self.mid = 0

    def _call(self, method, path, body=None, query=None, timeout=None):
        url = self.base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        headers = {"X-Nokturno-SW": self.ident}
        if self.token:
            headers["X-Nokturno-SW-Token"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        sent = self.clock.clock()
        try:
            with open_url(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            reason = ""
            try:
                reason = json.loads(e.read().decode("utf-8")).get("error") or ""
            except Exception:  # noqa: BLE001
                pass
            if e.code == 410 or (e.code == 401 and path != "/join"):
                raise Closed(reason)
            raise SyncWatchError({
                404: "Skupina s tímhle kódem neexistuje nebo už skončila",
                409: reason or "Skupina je plná",
                429: "Moc častých pokusů, zkus to za chvíli",
            }.get(e.code, reason or "Server odpověděl %s" % e.code))
        except Exception as e:  # noqa: BLE001 – síť, DNS, timeout
            raise SyncWatchError(str(e)[:120] or "Server neodpovídá")
        received = self.clock.clock()
        try:
            out = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SyncWatchError("Server vrátil nečitelnou odpověď")
        if "now" in out:
            self.clock.sample(float(out["now"]), sent, received)
        return out

    def _seal(self, payload):
        return base64.b64encode(seal(self.keys, payload)).decode("ascii")

    def _unseal(self, text):
        if not text:
            return None
        try:
            return unseal(self.keys, base64.b64decode(text))
        except (ValueError, TypeError):
            return None

    def create(self, me):
        out = self._call("POST", "/room", {"me": self._seal(me)})
        self.token, self.mid = out["token"], int(out["mid"])
        return out

    def join(self, me):
        out = self._call("POST", "/join", {"me": self._seal(me)})
        self.token, self.mid = out["token"], int(out["mid"])
        return out

    def put_state(self, state):
        return self._call("PUT", "/state", {"state": self._seal(state)})

    def put_me(self, me):
        return self._call("PUT", "/me", {"me": self._seal(me)})

    def lock(self, locked):
        return self._call("POST", "/lock", {"locked": bool(locked)})

    def leave(self):
        try:
            self._call("POST", "/leave", {})
        except SyncWatchError:
            pass

    def poll(self, since=0, wait=POLL_WAIT):
        """Čeká, dokud se ve skupině něco nezmění (nejvýš `wait` s)."""
        out = self._call("GET", "/poll", query={"since": int(since), "wait": int(wait)},
                         timeout=wait + TIMEOUT)
        self.mid = int(out.get("you") or self.mid)
        state = out.get("state")
        if state:
            data = self._unseal(state.get("blob"))
            state = dict(data, seq=int(state["seq"]), at=int(state["at"]), by=int(state.get("by") or 0)) \
                if isinstance(data, dict) else None
        members = []
        for m in out.get("members") or []:
            info = self._unseal(m.get("blob")) or {}
            members.append(dict(info if isinstance(info, dict) else {}, mid=int(m["mid"]),
                                leader=bool(m.get("leader")), online=bool(m.get("online"))))
        return {"ver": int(out.get("ver") or 0), "state": state, "members": members,
                "locked": bool(out.get("locked")), "expires": out.get("expires"),
                "now": out.get("now")}


# --- logika synchronizace ----------------------------------------------------------------

# Hlášky, které `Coordinator` posílá přes `notify(kód, **údaje)`, s českým textem jako
# zálohou. Větve si je překládají samy; `who` je jméno zařízení, které akci udělalo.
NOTICES = {
    "paused_all": "Pauza pro všechny",
    "paused": "%(who)s: pauza",
    "played": "%(who)s: přehrávání pokračuje",
    "seeked": "%(who)s: přetočeno na %(pos)s",
    "buffering": "%(who)s: načítá se, čekáme",
    "stopped": "%(who)s: přehrávání skončilo",
    "loading": "Spouštím: %(title)s",
    "waiting_others": "Čekám, až se stream načte u ostatních…",
    "started_without": "Pouštím bez: %(names)s",
    "load_failed": "Stream se nepodařilo spustit",
    "other_version": "Hraješ jinou verzi — časy nemusí přesně sedět",
    "detached": "Hraješ něco jiného — skupina čeká na další titul od vedoucího",
    "not_shareable": "Tohle se ostatním pustit nedá (není to titul z Nokturna)",
    "left": "Zastavil jsi přehrávání, ostatní sledují dál. Vrátit se jde v SyncWatch",
}


def notice_text(code, template=None, **kw):
    """Text hlášky; `template` = přeložená šablona větve (se `%(who)s` a spol.)."""
    try:
        return (template or NOTICES.get(code, code)) % kw
    except (KeyError, TypeError, ValueError):
        return NOTICES.get(code, code) % kw if code in NOTICES else code


class Coordinator(object):
    """Srovnává místní přehrávač se stavem skupiny. Nezná síť ani Kodi.

    Volá se z jednoho vlákna (`Runtime`): `on_poll` s odpovědí serveru, `on_local`
    s událostí přehrávače a `tick` jednou za sekundu. Ven jde jen přes `publish`
    (nový stav skupiny), `publish_me` (vlastní záznam) a `notify` (hláška).
    """

    def __init__(self, player, name, leader, clock, publish, publish_me, notify=None, status=None):
        self.player = player
        self.name = name
        self.leader = leader
        self.clock = clock              # Clock
        self.publish = publish          # dict -> seq
        self.publish_me = publish_me    # dict -> None
        # notify(kód, **údaje) — text si skládá každá větev sama (Kodi má čtyři jazyky); kódy
        # a údaje jsou v `NOTICES`
        self.notify = notify or (lambda code, **kw: None)
        self.status = status or (lambda info: None)
        self.mid = 0
        self.state = None               # poslední známý stav skupiny
        self.members = []
        self.echo_until = 0.0           # do kdy jsou vlastní události přehrávače ozvěnou cizího příkazu
        self.corrected = 0.0
        self.loading = None             # lid, který se právě načítá u nás
        self.load_started = 0.0
        self.ready = ""                 # lid, na který jsme připraveni
        self.detached = False           # hraje se tu něco jiného než ve skupině
        self.buffering = False
        self.cache_since = 0.0
        self.wait_since = 0.0           # vedoucí: od kdy čeká na načtení ostatních
        self.last_me = None
        self.expires = None             # s do zániku skupiny, když není připojené žádné další zařízení
        self.expires_at = None          # time.time() zániku
        self.locked = False

    # -- pomocné

    def now(self):
        return self.clock.clock()

    def _echo(self):
        self.echo_until = self.now() + ECHO_WINDOW

    def _in_echo(self):
        return self.now() < self.echo_until

    def target(self, state=None):
        return target_position(state or self.state or {}, self.clock.server_now())

    def lid(self):
        return ((self.state or {}).get("load") or {}).get("lid") or ""

    def _name_of(self, mid):
        if mid == self.mid:
            return self.name
        return next((m.get("name") for m in self.members if m.get("mid") == mid), "") or "Někdo"

    def _me(self):
        me = {"name": self.name, "ready": self.ready, "buf": self.buffering,
              "st": "jinde" if self.detached else ("load" if self.loading else "ok")}
        if me != self.last_me:
            self.last_me = dict(me)
            self.publish_me(me)
        self.status(self.info())

    def _publish(self, why, playing, pos, load=None, phase=None):
        state = {"load": load if load is not None else (self.state or {}).get("load"),
                 "playing": bool(playing), "pos": round(float(pos), 2), "why": why,
                 "phase": phase or (self.state or {}).get("phase") or "live"}
        seq = self.publish(state)
        # optimisticky hned platí u nás; server odpoví vlastním časem a pořadím
        self.state = dict(state, at=int(self.clock.server_now() * 1000), by=self.mid,
                          seq=seq or ((self.state or {}).get("seq", 0)))
        return self.state

    # -- vstupy

    def on_poll(self, resp):
        self.members = resp.get("members") or []
        self.expires = resp.get("expires")
        # okno odpočítává samo z času zániku — `expires` přichází jen s odpovědí pollu (až 25 s)
        self.expires_at = time.time() + float(self.expires) if self.expires is not None else None
        self.locked = bool(resp.get("locked"))
        state = resp.get("state")
        if state and state.get("seq", 0) > (self.state or {}).get("seq", -1):
            old = self.state
            self.state = state
            if state.get("by") != self.mid:
                self._apply(old, state)
        self.status(self.info())
        self._me()

    def on_local(self, kind, item=None, pos=None):
        """Událost přehrávače: `started` (s položkou z pluginu), `paused`,
        `resumed`, `seek`, `stopped`."""
        if kind == "started":
            return self._local_started(item or {})
        if kind == "stopped":
            return self._local_stopped()
        if kind == "rejoin":
            return self.rejoin()
        if self.detached or self.loading or not self.state or not self.state.get("load"):
            return
        if self._in_echo():
            return
        cur = self.player.position()
        if cur is None:
            return
        if kind == "paused":
            self._publish("pause", False, cur)
            self.notify("paused_all")
        elif kind == "resumed":
            self._publish("play", True, cur)
        elif kind == "seek":
            self._publish("seek", self.player.playing(), pos if pos is not None else cur)

    def tick(self):
        now = self.now()
        st = self.state or {}
        if self.loading:
            if now - self.load_started > LOAD_TIMEOUT:
                self.loading = None
                self.detached = True
                self.notify("load_failed")
                self._me()
            return
        if self.leader and st.get("phase") == "loading" and st.get("load"):
            self._leader_wait()
            return
        if self.detached or not st.get("load"):
            return
        cur = self.player.position()
        if cur is None:
            return
        self._buffer_check(now, cur)
        if self._in_echo() or self.buffering:
            return
        playing = self.player.playing()
        if st.get("playing") != playing and now - self.corrected > 3:
            # někde se ztratil příkaz (výpadek spojení) — srovnat podle stavu skupiny
            self.corrected = now
            self._set(st)
            return
        if playing and abs(cur - self.target()) > TOLERANCE and now - self.corrected > CORRECT_EVERY:
            self.corrected = now
            self._echo()
            self.player.seek(self.target())

    # -- vedoucí

    def _local_started(self, item):
        replay = item.get("replay") or ""
        if self.loading:
            # u člena právě doběhlo spuštění streamu, o který si řekla skupina
            self.loading = None
            self.detached = False
            self._echo()
            st = self.state or {}
            total = float(item.get("total") or 0)
            want = float(((st.get("load") or {}).get("total")) or 0)
            if total and want and abs(total - want) / want > 0.01:
                self.notify("other_version")
            if st.get("phase") == "live" and st.get("playing"):
                self.player.seek(self.target())
            else:
                self.player.pause()
                self.player.seek(float(st.get("pos") or 0))
            self.ready = self.lid()
            self._me()
            return
        if not self.leader:
            # člen si pustil něco vlastního — do skupiny nezasahuje a skupina do něj ne
            if valid_replay(replay) and replay == ((self.state or {}).get("load") or {}).get("replay"):
                self.detached = False
                self._set(self.state)
            else:
                self.detached = True
                self.notify("detached")
            self._me()
            return
        if not valid_replay(replay):
            self.notify("not_shareable")
            return
        load = {"lid": os.urandom(4).hex(), "replay": replay, "title": item.get("title") or "",
                "total": float(item.get("total") or 0)}
        cur = self.player.position() or 0.0
        self._echo()
        self.player.pause()
        self.ready = load["lid"]
        self.detached = False
        self.wait_since = self.now()
        self._publish("load", False, cur, load=load, phase="loading")
        self._me()
        self.notify("waiting_others")

    def _leader_wait(self):
        lid = self.lid()
        others = [m for m in self.members if m.get("online") and m.get("mid") != self.mid
                  and m.get("st") != "jinde"]
        waiting = [m for m in others if m.get("ready") != lid]
        if waiting and self.now() - self.wait_since < START_WAIT:
            return
        cur = self.player.position()
        if cur is None:
            return
        self._echo()
        self.player.resume()
        self._publish("start", True, cur, phase="live")
        if waiting:
            self.notify("started_without", names=", ".join(m.get("name") or "?" for m in waiting))

    def _local_stopped(self):
        # Kodi posílá zastavení starého souboru i po startu nového (přepnutí titulu) —
        # když se pořád něco hraje, nic se nezastavilo
        if self.loading or self.player.position() is not None:
            return
        if self.leader and self.state and self.state.get("load") and not self.detached:
            self._publish("stop", False, 0, load={}, phase="live")
        elif not self.leader and self.lid() and not self.detached:
            # člen zastavil (třeba omylem) — skupina sleduje dál, jde se vrátit (`rejoin`)
            self.notify("left")
        self.detached = not self.leader
        self._me()

    # -- provedení cizího stavu

    def _apply(self, old, new):
        new_lid = (new.get("load") or {}).get("lid") or ""
        old_lid = ((old or {}).get("load") or {}).get("lid") or ""
        who = self._name_of(new.get("by"))
        if not new_lid:
            if old_lid and not self.detached:
                self._echo()
                self.player.stop()
                self.notify("stopped", who=who)
            self.ready = ""
            return
        if new_lid != old_lid or (self.detached and new.get("why") == "load"):
            self._load(new)
            return
        if self.detached or self.loading:
            return
        why = new.get("why")
        if why == "pause":
            self.notify("paused", who=who)
        elif why == "play" and not (old or {}).get("playing"):
            self.notify("played", who=who)
        elif why == "seek":
            self.notify("seeked", who=who, pos=clock_text(new.get("pos")))
        elif why == "buffer":
            self.notify("buffering", who=who)
        self._set(new)

    def _load(self, state):
        """Pustit u sebe titul skupiny; po startu (`_local_started`) naskočí na její pozici."""
        load = state.get("load") or {}
        if not load.get("lid") or not valid_replay(load.get("replay")):
            return
        self.loading = load["lid"]
        self.load_started = self.now()
        self.detached = False
        self.ready = ""
        self.notify("loading", title=load.get("title") or "")
        self.player.load(with_sw(load["replay"]))
        self._me()

    def rejoin(self):
        """Člen, který přehrávání zastavil nebo pustil něco jiného, se vrací do filmu skupiny."""
        if self.leader or self.loading or not self.state:
            return
        self._load(self.state)

    def _set(self, state):
        cur = self.player.position()
        if cur is None:
            return
        self._echo()
        if state.get("playing"):
            target = self.target(state)
            if abs(cur - target) > TOLERANCE:
                self.player.seek(target)
            if not self.player.playing():
                self.player.resume()
        else:
            if self.player.playing():
                self.player.pause()
            if abs(cur - float(state.get("pos") or 0)) > 0.5:
                self.player.seek(float(state.get("pos") or 0))

    # -- bufferování

    def _buffer_check(self, now, cur):
        if self.player.caching():
            if not self.cache_since:
                self.cache_since = now
            if (not self.buffering and now - self.cache_since >= BUFFER_AFTER
                    and (self.state or {}).get("playing")):
                self.buffering = True
                self._publish("buffer", False, cur)
                self._me()
            return
        self.cache_since = 0.0
        if not self.buffering:
            return
        self.buffering = False
        self._me()
        st = self.state or {}
        others = [m for m in self.members if m.get("online") and m.get("mid") != self.mid and m.get("buf")]
        if st.get("why") == "buffer" and not st.get("playing") and not others:
            self._echo()
            self.player.resume()
            self._publish("play", True, float(st.get("pos") or cur))

    def info(self):
        """Stav pro okna pluginu (vlastnost okna)."""
        st = self.state or {}
        load = st.get("load") or {}
        return {"members": [{"name": m.get("name") or "?", "leader": m.get("leader"), "you": m.get("mid") == self.mid,
                             "online": m.get("online"), "st": m.get("st")} for m in self.members],
                "title": load.get("title") or "", "phase": st.get("phase") or "",
                "playing": bool(st.get("playing")), "loaded": bool(load.get("lid")),
                "leader": self.leader, "expires": self.expires, "expires_at": self.expires_at, "locked": self.locked,
                "loading": bool(self.loading), "detached": self.detached}


def clock_text(seconds):
    s = int(float(seconds or 0))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


# --- běh na pozadí -------------------------------------------------------------------------

class Runtime(object):
    """Dvě vlákna: jedno čeká na serveru (long-poll), druhé všechno zpracovává
    po jednom — události přehrávače, odpovědi serveru a tik. Callbacky přehrávače
    v Kodi se tím nezdrží síťí a `Coordinator` nepotřebuje zámky."""

    def __init__(self, client, player, name, leader, notify=None, status=None, on_closed=None,
                 should_stop=None):
        self.client = client
        self.queue = queue.Queue()
        self.stopped = threading.Event()
        self.should_stop = should_stop or (lambda: False)
        self.on_closed = on_closed or (lambda reason: None)
        self.coord = Coordinator(player, name, leader, client.clock, self._publish, self._publish_me,
                                 notify=notify, status=status)
        self.coord.mid = client.mid
        self.threads = []

    def _publish(self, state):
        for attempt in (1, 2):
            try:
                return int(self.client.put_state(state).get("seq") or 0)
            except Closed as e:
                self._closed(e.reason)
                return 0
            except SyncWatchError:
                if attempt == 2:
                    return 0

    def _publish_me(self, me):
        try:
            self.client.put_me(me)
        except Closed as e:
            self._closed(e.reason)
        except SyncWatchError:
            pass

    def _closed(self, reason):
        if not self.stopped.is_set():
            self.stopped.set()
            self.on_closed(reason)

    def event(self, kind, **kw):
        """Z callbacku přehrávače — jen do fronty, nic nečeká."""
        self.queue.put(("local", kind, kw))

    def start(self):
        for target, name in ((self._poller, "nokturno-sw-poll"), (self._worker, "nokturno-sw")):
            t = threading.Thread(target=target, name=name)
            t.daemon = True
            t.start()
            self.threads.append(t)

    def stop(self, leave=False):
        self.stopped.set()
        self.queue.put(("stop", None, None))
        if leave:
            self.client.leave()

    def alive(self):
        return not self.stopped.is_set()

    def _poller(self):
        ver, errors = 0, 0
        while not self.stopped.is_set() and not self.should_stop():
            try:
                resp = self.client.poll(ver)
            except Closed as e:
                self._closed(e.reason)
                return
            except SyncWatchError:
                errors += 1
                self.stopped.wait(min(30, 2 * errors))
                continue
            errors = 0
            ver = resp["ver"]
            self.queue.put(("poll", resp, None))

    def _worker(self):
        while not self.stopped.is_set() and not self.should_stop():
            try:
                what, a, b = self.queue.get(timeout=1.0)
            except queue.Empty:
                what, a, b = "tick", None, None
            try:
                if what == "poll":
                    self.coord.on_poll(a)
                elif what == "local":
                    self.coord.on_local(a, **b)
                elif what == "tick":
                    self.coord.tick()
            except Exception:  # noqa: BLE001 – jedna vadná událost nesmí zastavit skupinu
                import logging
                logging.getLogger("nokturno").exception("syncwatch %s", what)
            if what != "tick" and self.queue.empty():
                self.coord.tick()
