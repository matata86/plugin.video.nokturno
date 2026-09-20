"""Synchronizace více klientů přes dashboard, který do obsahu nevidí.

`sync.py` umí totéž přes Home Assistant. Tady je varianta pro domácnosti bez HA:
střed dělá dashboard, ale jen jako **slepý relay** — ukládá neprůhledné bloby
a nedokáže je přečíst. Slévání zůstává na klientech (`collect_changes` /
`apply_changes` ze `sync.py`), protože last-write-wins je komutativní a nezáleží,
v jakém pořadí a od koho záznamy přijdou.

Skupina stojí na jediném tajemství — **kódu**, který vygeneruje první zařízení
(„master") a ostatní ho opíšou z obrazovky:

    NKT-8G4M-2QX7-VB9K-TRWP      16 znaků Crockford Base32, 80 bitů

Z kódu se odvodí adresa skupiny a klíče — dělá to sdílený `sealbox` (tentýž
modul zapečeťuje přenos nastavení mezi zařízeními) s vlastní solí
`nokturno-sync-v1`, takže týž opsaný kód vyjde v každém účelu na jiné klíče.
Na server jde jen `ident`, které z kódu spočítat zpátky nejde; slouží zároveň
jako adresa skupiny i jako přístupový token relaye: kdo ho zná, smí do skupiny
psát a číst z ní, ale bez kódu nic nedešifruje. Proto patří do hlavičky, nikdy
do URL — Tailscale i nginx logují cesty.

Nahrává se **celý stav zařízení**, ne přírůstky — je tak malý, že fronta delt by
byla práce navíc: relay drží jeden přepisovaný řádek na zařízení, nový člen
skupiny dostane rovnou všechno a ztracený blob nic nerozbije.
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from sealbox import SealError, format_code, keys_for as _keys_for, new_code as _new_code, \
    normalize_code, seal, unseal, valid_code as _valid_code
from stats import COLLECT_URL
from sync import CIRCLES, DEFAULT_CIRCLES, SNAPSHOTS, apply_changes, collect_changes, \
    filter_circles

SYNC_URL = COLLECT_URL.rsplit("/", 1)[0] + "/sync"
STATE = "syncbox"          # syncbox.json v profilu
TIMEOUT = 20
MAX_BLOB = 128 * 1024      # shoda s limitem relaye

# Kryptografii a kód skupiny dělá sdílený `sealbox` — stejnou konstrukci používá
# i přenos nastavení mezi zařízeními (`transfer.py`). Vlastní sůl a délka kódu
# dávají synchronizaci vlastní klíčový prostor: týž opsaný kód vyjde v každém
# účelu na jiné `ident` i jiné klíče, takže se jedno místo nedá zaměnit za druhé.
SALT = b"nokturno-sync-v1"
CODE_LEN = 16              # 80 bitů — kód se opisuje z obrazovky televize

# Okruhy (`CIRCLES`, `filter_circles`) jsou v `sync.py` — platí pro obě střediska stejně.
FUTURE_SLACK = 6 * 3600    # `ts` víc než tohle v budoucnu = rozbité hodiny protějšku


class SyncError(Exception):
    """Chyba, kterou má smysl ukázat uživateli (špatný kód, plná skupina, síť)."""


def new_code():
    """Nový kód skupiny, `NKT-XXXX-XXXX-XXXX-XXXX`."""
    return _new_code(CODE_LEN)


def valid_code(text):
    return _valid_code(text, CODE_LEN)


def keys_for(code):
    """Klíče skupiny (`ident`, `enc`, `mac`) s cache v paměti procesu."""
    try:
        return _keys_for(code, SALT, CODE_LEN)
    except SealError as e:
        raise SyncError(str(e) or "Kód skupiny nemá správný tvar")



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
        headers = {"X-Nokturno-Group": self.keys.ident, "X-Nokturno-Device": self.device_id}
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
                blobs.append(base64.b64decode(item.get("blob") or ""))
            except (ValueError, TypeError):   # binascii.Error je podtřída ValueError
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
    # `device_id()` zakládá id zařízení a ukládá ho do téhož souboru jako stav,
    # takže se musí volat PŘED načtením stavu — jinak by ho závěrečné uložení
    # (`dict(state, …)` nad starým obsahem) zase přepsalo pryč a zařízení by si
    # po každém kole vyrobilo novou identitu: v relayi by přibýval osiřelý blob
    # a skupina by se během pár kol zaplnila až na `MAX_DEVICES`.
    device = device_id(store)
    state = store.reload(STATE, {})
    try:
        keys = keys_for(code)
    except SyncError as e:
        return _fail(store, state, str(e))

    relay = Relay(keys, device, base_url)
    # Zapnutý okruh musí dostat i to, co přišlo, když byl vypnutý: cizí bloby
    # se stahují jen od `since`, takže bez resetu by se dorovnal až cizí změnou.
    znamka = ",".join(sorted(circles or ()))
    if state.get("circles") != znamka:
        state = dict(state, since=0, sent="")
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
                           circles=znamka, pushed=pushed, pulled=pulled))
    return True, pushed, pulled, ""


def _fail(store, state, why):
    store.save(STATE, dict(state, last_error=why, last_try=int(time.time())))
    return False, 0, 0, why
