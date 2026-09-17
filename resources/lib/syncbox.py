"""Synchronizace více klientů přes dashboard, který do obsahu nevidí.

`sync.py` umí totéž přes Home Assistant. Tady je varianta pro domácnosti bez HA:
střed dělá dashboard, ale jen jako **slepý relay** — ukládá neprůhledné bloby
a nedokáže je přečíst. Slévání zůstává na klientech (`collect_changes` /
`apply_changes` ze `sync.py`), protože last-write-wins je komutativní a nezáleží,
v jakém pořadí a od koho záznamy přijdou.

Skupina stojí na jediném tajemství — **kódu**, který vygeneruje první zařízení
(„master") a ostatní ho opíšou z obrazovky:

    NKT-8G4M-2QX7-VB9K-TRWP      16 znaků Crockford Base32, 80 bitů

Z kódu se odvodí adresa skupiny a klíče. Na server jde jen `group_id`, které
z kódu spočítat zpátky nejde:

    root     = PBKDF2-HMAC-SHA256(kód, "nokturno-sync-v1", 200 000)
    group_id = HMAC(root, "gid")   ← jediné, co vidí server
    enc_key  = HMAC(root, "enc")
    mac_key  = HMAC(root, "mac")

`group_id` slouží zároveň jako adresa i jako přístupový token relaye: kdo ho zná,
smí do skupiny psát a číst z ní, ale bez kódu nic nedešifruje. Proto patří do
hlavičky, nikdy do URL — Tailscale i nginx logují cesty.

Šifrování je jen ze stdlib (`hashlib`, `hmac`, `os.urandom`), aby doplněk pro Kodi
nepotřeboval `script.module.pycryptodome`: instalace s vypnutým oficiálním repem
by si aktualizaci nestáhla. Nevymýšlí se šifra, skládají se standardní primitiva
— SHA-256 v counter módu jako proudová šifra a encrypt-then-MAC:

    blob  = nonce(16 B) || ciphertext || tag(32 B)
    proud = SHA-256(enc_key || nonce || counter_be64)    counter = 0, 1, 2, …
    ct    = gzip(json) XOR proud
    tag   = HMAC-SHA256(mac_key, nonce || ct)

Gzip před šifrováním není optimalizace, ale součást návrhu: plný stav 5000 titulů
je 511 kB JSON → 13 kB a zabalení trvá 8 ms místo 102 ms.

Nahrává se **celý stav zařízení**, ne přírůstky — je tak malý, že fronta delt by
byla práce navíc: relay drží jeden přepisovaný řádek na zařízení, nový člen
skupiny dostane rovnou všechno a ztracený blob nic nerozbije.
"""
import gzip
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

from stats import COLLECT_URL
from sync import apply_changes, collect_changes

SYNC_URL = COLLECT_URL.rsplit("/", 1)[0] + "/sync"
STATE = "syncbox"          # syncbox.json v profilu
TIMEOUT = 20
MAX_BLOB = 128 * 1024      # shoda s limitem relaye

# Crockford Base32 bez I, L, O a U — znaky, které se z obrazovky televize opisují
# špatně (jedničku od I a nulu od O nerozezná ani dobrý skin, U svádí na V).
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 16
PREFIX = "NKT"
_CONFUSED = {"I": "1", "L": "1", "O": "0", "U": "V"}

# Okruhy: co z blobu se posílá a přijímá. Klíče odpovídají `collect_changes`.
CIRCLES = {
    "watched": ("watched", "next_hidden"),   # zhlédnuto, rozkoukanost, skryté další díly
    "favourites": ("favlog",),               # Můj seznam jako deník zapnuto/vypnuto
    "history": ("histlog",),                 # historie hledání
}
DEFAULT_CIRCLES = ("watched", "favourites", "history")
# Snímky titulů jdou vždy k tomu, co se posílá — bez nich by druhá strana
# neuměla položku vykreslit. `collect_changes` je omezuje na dotčené klíče.
SNAPSHOTS = "items"

FUTURE_SLACK = 6 * 3600    # `ts` víc než tohle v budoucnu = rozbité hodiny protějšku


class SyncError(Exception):
    """Chyba, kterou má smysl ukázat uživateli (špatný kód, plná skupina, síť)."""


def new_code():
    """Nový kód skupiny. Náhoda jde z `os.urandom`, ne z `random`."""
    raw = "".join(ALPHABET[b % len(ALPHABET)] for b in os.urandom(CODE_LEN))
    return format_code(raw)


def format_code(raw):
    """`8G4M2QX7VB9KTRWP` → `NKT-8G4M-2QX7-VB9K-TRWP` (jen pro zobrazení)."""
    raw = normalize_code(raw)
    groups = [raw[i:i + 4] for i in range(0, len(raw), 4)]
    return "-".join([PREFIX] + groups)


def normalize_code(text):
    """Kód tak, jak se z něj počítají klíče: bez pomlček, mezer a prefixu, velkými
    písmeny a se záměnami, které dělá člověk opisující z televize (O→0, I/L→1)."""
    raw = "".join((text or "").split()).replace("-", "").upper()
    if raw.startswith(PREFIX):
        raw = raw[len(PREFIX):]
    return "".join(_CONFUSED.get(ch, ch) for ch in raw)


def valid_code(text):
    raw = normalize_code(text)
    return len(raw) == CODE_LEN and all(ch in ALPHABET for ch in raw)


class Keys(object):
    """Klíče odvozené z kódu. Drž je v paměti — PBKDF2 stojí na slabém Android
    boxu i půl sekundy a při každém kole synchronizace by to bylo znát."""

    __slots__ = ("group_id", "enc", "mac")

    def __init__(self, code):
        if not valid_code(code):
            raise SyncError("Kód skupiny nemá správný tvar")
        root = hashlib.pbkdf2_hmac("sha256", normalize_code(code).encode("ascii"),
                                   b"nokturno-sync-v1", 200000)
        self.group_id = hmac.new(root, b"gid", hashlib.sha256).hexdigest()[:32]
        self.enc = hmac.new(root, b"enc", hashlib.sha256).digest()
        self.mac = hmac.new(root, b"mac", hashlib.sha256).digest()


_KEYS = {}


def keys_for(code):
    """Klíče s cache v paměti procesu. Bez ní by PBKDF2 (200 000 iterací, na
    slabém Android boxu i půl sekundy) běžel při každém kole synchronizace."""
    raw = normalize_code(code)
    if raw not in _KEYS:
        _KEYS[raw] = Keys(code)
    return _KEYS[raw]


def _keystream(key, nonce, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:length])


def _xor(data, pad):
    return bytes(a ^ b for a, b in zip(data, pad))


def seal(keys, payload):
    """Slovník → blob. Nonce je náhodná pro každé zabalení, takže dvě zabalení
    téhož stavu vypadají na serveru jinak."""
    raw = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), 6)
    nonce = os.urandom(16)
    ct = _xor(raw, _keystream(keys.enc, nonce, len(raw)))
    tag = hmac.new(keys.mac, nonce + ct, hashlib.sha256).digest()
    return nonce + ct + tag


def unseal(keys, blob):
    """Blob → slovník, nebo `None`, když nesedí tag (cizí skupina, poškozený
    přenos, podvržený obsah). Tag se ověřuje **před** dešifrováním."""
    if not blob or len(blob) < 16 + 32:
        return None
    nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    if not hmac.compare_digest(tag, hmac.new(keys.mac, nonce + ct, hashlib.sha256).digest()):
        return None
    try:
        return json.loads(gzip.decompress(_xor(ct, _keystream(keys.enc, nonce, len(ct)))).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def filter_circles(changes, circles):
    """Ze stavu nechá jen zapnuté okruhy; snímky titulů jdou vždy s tím, co zbyde."""
    allowed = set()
    for name in circles or ():
        allowed.update(CIRCLES.get(name, ()))
    out = {k: v for k, v in (changes or {}).items() if k in allowed}
    if out and (changes or {}).get(SNAPSHOTS):
        out[SNAPSHOTS] = changes[SNAPSHOTS]
    return out


def sanitize(changes, now=None):
    """Zahodí záznamy s časem z budoucnosti. Android box po výpadku napíše `ts`
    o roky dopředu a last-write-wins by ten záznam zafixovalo napořád — relay
    to pohlídat nemůže, protože do blobu nevidí, takže to dělá příjemce."""
    limit = int(now or time.time()) + FUTURE_SLACK
    out = {}
    for section, records in (changes or {}).items():
        if section == SNAPSHOTS or not isinstance(records, dict):
            out[section] = records
            continue
        out[section] = {k: v for k, v in records.items()
                        if not isinstance(v, dict) or int(v.get("ts") or 0) <= limit}
    return out


class Relay(object):
    """Klient slepého relaye. Nikdy neposílá kód ani nic nešifrovaného."""

    def __init__(self, keys, device_id, base_url=SYNC_URL, timeout=TIMEOUT):
        self.keys = keys
        self.device_id = device_id
        self.base = (base_url or SYNC_URL).rstrip("/")
        self.timeout = timeout

    def _request(self, method, url, body=None, kind="application/octet-stream"):
        headers = {"X-Nokturno-Group": self.keys.group_id, "X-Nokturno-Device": self.device_id}
        if body is not None:
            headers["Content-Type"] = kind
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise SyncError({
                403: "Skupina nepřijímá nová zařízení — otevři připojení na prvním Kodi",
                404: "Skupina neexistuje nebo vypršela",
                409: "Skupina je plná",
                413: "Data jsou příliš velká",
                429: "Moc častá synchronizace, zkus to za chvíli",
            }.get(e.code, "Server odpověděl %s" % e.code))
        except Exception as e:  # noqa: BLE001 – síť, DNS, špatná adresa
            raise SyncError(str(e)[:120])

    def open_group(self):
        """Založí skupinu, nebo u existující otevře okno pro připojení dalšího
        zařízení. Jediná operace, kterou dělá výhradně master."""
        self._request("POST", self.base + "/open", b"", "application/json")

    def push(self, blob):
        if len(blob) > MAX_BLOB:
            raise SyncError("Stav je příliš velký (%d kB)" % (len(blob) // 1024))
        self._request("PUT", self.base, blob)

    def pull(self, since):
        """Bloby ostatních zařízení novější než `since`. Vrací (revize, [bloby])."""
        raw = self._request("GET", "%s?since=%d" % (self.base, int(since or 0)))
        try:
            answer = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise SyncError("Nesrozumitelná odpověď serveru")
        blobs = []
        for item in answer.get("devices") or []:
            try:
                blobs.append(bytes.fromhex(item.get("blob") or ""))
            except ValueError:
                continue
        return int(answer.get("rev") or 0), blobs

    def forget(self):
        """Odhlásí tohle zařízení ze skupiny (smaže jeho blob na serveru)."""
        self._request("DELETE", self.base)


def device_id(store):
    """Náhodné id zařízení, uložené v profilu. Se statistikami nemá nic
    společného — kdyby šlo `install_id` spárovat se skupinou, anonymita
    hlášení padne."""
    state = store.reload(STATE, {})
    if not state.get("device"):
        state["device"] = os.urandom(8).hex()
        store.save(STATE, state)
    return state["device"]


def sync_once(store, code, circles=DEFAULT_CIRCLES, base_url=SYNC_URL, name="", extra=None):
    """Jedno kolo s relayem. Vrací (ok, odesláno, přijato, důvod) — nikdy
    nevyhodí výjimku, stejně jako `sync.sync_once`."""
    state = store.reload(STATE, {})
    try:
        keys = keys_for(code)
    except SyncError as e:
        return _fail(store, state, str(e))

    relay = Relay(keys, device_id(store), base_url)
    payload = filter_circles(collect_changes(store, 0), circles)
    if extra:
        payload["extra"] = extra
    payload["device"] = name or ""
    blob = seal(keys, payload)

    # Otisk stavu, ne blobu: nonce je pokaždé jiná, takže by se nahrávalo
    # každé kolo, i když se nic nezměnilo.
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    pushed = 0
    try:
        if fingerprint != state.get("sent"):
            relay.push(blob)
            pushed = sum(len(v) for v in payload.values() if isinstance(v, dict))
            state["sent"] = fingerprint
        rev, blobs = relay.pull(state.get("since") or 0)
    except SyncError as e:
        return _fail(store, state, str(e))

    pulled = 0
    for foreign in blobs:
        data = unseal(keys, foreign)
        if data is None:
            continue          # cizí skupina nebo poškozený blob — tiše dál
        pulled += apply_changes(store, sanitize(filter_circles(data, circles)))
    now = int(time.time())
    store.save(STATE, dict(state, since=rev, last_ok=now, last_error="",
                           pushed=pushed, pulled=pulled))
    return True, pushed, pulled, ""


def _fail(store, state, why):
    store.save(STATE, dict(state, last_error=why, last_try=int(time.time())))
    return False, 0, 0, why
