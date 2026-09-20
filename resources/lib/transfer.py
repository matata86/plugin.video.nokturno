"""Přenos nastavení do dalšího zařízení — kdo má čtyři Kodi, nastavuje jednou.

První zařízení zabalí své nastavení, zapečetí ho kódem, který nikdo jiný nezná,
a uloží na dashboard. Druhé zařízení kód opíše z obrazovky (nebo ho vyplní
na stránce z mobilu) a nastavení si vyzvedne:

    NKT-4F7K-2B9Q      8 znaků Crockford Base32, 40 bitů

Server je slepý — `ident` z kódu spočítat zpátky nejde a blob bez kódu nikdo
nerozluští (`sealbox.py`, tatáž kryptografie jako synchronizace). Blob žije
`TTL`, jedno vyzvednutí ho smaže. Krátký kód stačí, protože hádat se dá jen
přes server, a ten má limit na adresu.

Stejný blob se dá místo na server uložit do souboru (`export_bytes` /
`import_bytes`) — na USB, na síťový disk, kamkoli. Formát je týž, kód je pořád
potřeba.

**Co se přenáší.** Seznam se neudržuje ručně: bere se ze schématu formuláře
(`remote_setup.py`), které Kodi staví přímo ze `settings.xml`. Nové nastavení
se tak přenáší samo. Odečte se `DENY` — hodnoty vázané na konkrétní stroj,
u kterých by kopie škodila:

    download_dir                cesta, která na druhém stroji nemusí existovat
    sync_enabled/_url/_key      synchronizace má vlastní párování; sdílený klíč
                                by ze dvou zařízení udělal jedno

Mimo schéma (a tedy mimo přenos) zůstává všechno z profilu: `install_id`
(jinak by se dvě zařízení slila v statistikách i hlášeních o pádech), tokeny
CZtoru a Traktu, cache, oblíbené, rozkoukanost.

**Tokeny se nekopírují, ale nezapomínají.** CZtor mění obnovovací token při
každém použití — kopie by odhlásila původní zařízení. Trakt má token vázaný na
zařízení. Přenos proto nese jen `flags` („tady byl zapnutý CZtor"), aby cílové
zařízení po importu rovnou nabídlo párování PINem a autorizaci Traktu.
"""
import json
import time
import urllib.error
import urllib.request

from sealbox import SealError, format_code, keys_for, new_code as _new_code, normalize_code, seal, unseal, \
    valid_code as _valid_code
from stats import COLLECT_URL

TRANSFER_URL = COLLECT_URL.rsplit("/", 1)[0] + "/transfer"
FORMAT = 1
SALT = b"nokturno-transfer-v1"
CODE_LEN = 8
MAX_BLOB = 32 * 1024       # shoda s limitem serveru
TTL = 15 * 60              # jak dlouho blob na serveru žije (server rozhoduje, klient odpočítává)
TIMEOUT = 20

# prvky schématu bez hodnoty (podnadpis, návod, tlačítko) — do přenosu nic nedávají
NO_VALUE = ("heading", "info", "action")
DENY = frozenset({"download_dir", "sync_enabled", "sync_url", "sync_key", "install_id"})
# zapnutý zdroj, jehož přihlášení se přenést nedá — po importu se musí dořešit na místě
FLAG_SETTINGS = {"cz_enabled": "cztor", "trakt_enabled": "trakt"}


class TransferError(Exception):
    """Chyba, kterou má smysl ukázat uživateli (špatný kód, prošlý přenos, síť)."""


def new_code():
    return _new_code(CODE_LEN)


def valid_code(text):
    return _valid_code(text, CODE_LEN)


def keys(code):
    try:
        return keys_for(code, SALT, CODE_LEN)
    except SealError as e:
        raise TransferError(str(e))


def ident(code):
    """Adresa přenosu na serveru. Z kódu jednosměrně, zpátky to nejde."""
    return keys(code).ident


def exportable(schema):
    """Id nastavení, která se přenášejí — ze schématu formuláře, minus `DENY`."""
    out = []
    for section in schema or ():
        for field in section.get("fields") or ():
            name = field.get("id")
            if not name or field.get("type") in NO_VALUE or name in DENY:
                continue
            if name not in out:
                out.append(name)
    return out


def pack(schema, values, source="", extras=None, now=None):
    """Nastavení → obsah přenosu. `values` je `{id: hodnota}` tak, jak je čte
    hostitel; chybějící klíč se přeskočí, `DENY` se zahodí i kdyby v `values` byl."""
    settings = {}
    for name in exportable(schema):
        if name in (values or {}):
            settings[name] = "" if values[name] is None else str(values[name])
    flags = {}
    for name, flag in FLAG_SETTINGS.items():
        if str((values or {}).get(name, "")).lower() in ("true", "1"):
            flags[flag] = True
    for flag, on in (extras or {}).items():
        if on:
            flags[flag] = True
    return {"format": FORMAT, "created": int(now or time.time()), "source": str(source or ""),
            "settings": settings, "flags": flags}


def export_bytes(code, payload):
    """Zapečetěný blob. Větší než `MAX_BLOB` neprojde ani na server, ani do souboru."""
    blob = seal(keys(code), payload)
    if len(blob) > MAX_BLOB:
        raise TransferError("Nastavení je příliš velké (%d kB)" % (len(blob) // 1024))
    return blob


def import_bytes(code, blob):
    """Blob → obsah přenosu. Špatný kód i poškozený blob končí stejně — tag
    se ověřuje před dešifrováním, takže se nic neparsuje naslepo."""
    payload = unseal(keys(code), blob)
    if payload is None:
        raise TransferError("Kód nesedí, nebo jsou data poškozená")
    if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
        raise TransferError("Nesrozumitelný obsah přenosu")
    if int(payload.get("format") or 0) > FORMAT:
        raise TransferError("Přenos je z novějšího Nokturna — nejdřív aktualizuj tohle zařízení")
    return payload


class Plan(object):
    """Co se z přenosu stane, než se na to uživatel podívá a potvrdí to.

    `changes` se zapíše, `same` už sedí, `unknown` tohle zařízení nezná (starší
    verze doplňku, jiná větev rodiny), `blocked` je `DENY`."""

    __slots__ = ("changes", "same", "unknown", "blocked", "flags", "source", "created")

    def __init__(self, changes, same, unknown, blocked, flags, source="", created=0):
        self.changes, self.same, self.unknown, self.blocked = changes, same, unknown, blocked
        self.flags, self.source, self.created = flags, source, created

    def __len__(self):
        return len(self.changes)

    def empty(self):
        return not self.changes


def plan(payload, current, known=None):
    """Porovná přenos s tím, co na zařízení je. `known` = id, která tohle
    zařízení zná (typicky `exportable(vlastní schéma)`); bez něj se bere
    `current`."""
    allowed = set(known) if known is not None else set(current or {})
    changes, same, unknown, blocked = {}, [], [], []
    for name, value in sorted((payload.get("settings") or {}).items()):
        value = "" if value is None else str(value)
        if name in DENY:
            blocked.append(name)
        elif name not in allowed:
            unknown.append(name)
        elif str((current or {}).get(name, "")) == value:
            same.append(name)
        else:
            changes[name] = value
    flags = sorted(f for f, on in (payload.get("flags") or {}).items() if on)
    return Plan(changes, same, unknown, blocked, flags,
                str(payload.get("source") or ""), int(payload.get("created") or 0))


class Relay(object):
    """Klient serveru. Nikdy neposílá kód ani nic nešifrovaného — jen `ident`
    v hlavičce (cesty se logují) a neprůhlednou binárku v těle."""

    def __init__(self, code, base_url=TRANSFER_URL, timeout=TIMEOUT):
        self.ident = ident(code)
        self.base = (base_url or TRANSFER_URL).rstrip("/")
        self.timeout = timeout

    def _request(self, method, body=None):
        headers = {"X-Nokturno-Transfer": self.ident}
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(self.base, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise TransferError({
                404: "Přenos neexistuje, vypršel, nebo už byl jednou vyzvednutý",
                409: "Pod tímhle kódem už přenos leží",
                413: "Nastavení je příliš velké",
                429: "Moc častých pokusů, zkus to za chvíli",
            }.get(e.code, "Server odpověděl %s" % e.code))
        except Exception as e:  # noqa: BLE001 – síť, DNS, špatná adresa
            raise TransferError(str(e)[:120])

    def push(self, blob):
        """Uloží přenos. Odpověď nese, jak dlouho bude platit."""
        raw = self._request("POST", blob)
        try:
            return int(json.loads(raw.decode("utf-8")).get("ttl") or TTL)
        except (ValueError, AttributeError, UnicodeDecodeError):
            return TTL

    def pull(self):
        """Vyzvedne přenos. Server ho tím zahodí — druhý pokus vrátí 404."""
        blob = self._request("GET")
        if not blob:
            raise TransferError("Server vrátil prázdný přenos")
        return blob


def send_payload(payload, base_url=TRANSFER_URL):
    """Zapečetí hotový obsah novým kódem a pošle ho. Vrací `(kód k opsání, jak
    dlouho platí)`."""
    code = new_code()
    ttl = Relay(code, base_url).push(export_bytes(code, payload))
    return code, ttl


def send(schema, values, source="", extras=None, base_url=TRANSFER_URL):
    """Zabalí, zapečetí, pošle."""
    return send_payload(pack(schema, values, source, extras), base_url)


def receive(code, base_url=TRANSFER_URL):
    """Vyzvedne a rozbalí přenos podle opsaného kódu."""
    if not valid_code(code):
        raise TransferError("Kód nemá správný tvar (NKT-XXXX-XXXX)")
    return import_bytes(code, Relay(code, base_url).pull())


def pretty_code(code):
    return format_code(normalize_code(code))
