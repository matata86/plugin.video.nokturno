"""Kód opsaný z obrazovky jako jediné tajemství — odvození klíčů a zapečetění dat.

Sdílený základ pro všechno, co si dvě zařízení posílají přes server, který do
obsahu nevidí: synchronizaci více Kodi (`syncbox.py`) i přenos nastavení
(`transfer.py`). Server dostane jen `ident` a neprůhlednou binárku.

    NKT-8G4M-2QX7-VB9K-TRWP      Crockford Base32, 4 znaky = 20 bitů

    root  = PBKDF2-HMAC-SHA256(kód, sůl, 200 000)
    ident = HMAC(root, "gid")   ← jediné, co vidí server
    enc   = HMAC(root, "enc")
    mac   = HMAC(root, "mac")

`ident` je adresa i přístupový token zároveň: kdo ho zná, smí na to místo psát
a číst z něj, ale bez kódu nic nedešifruje. Proto patří do hlavičky, nikdy do
URL — Tailscale i nginx logují cesty.

Každý účel má **vlastní sůl** (`salt`), takže z téhož kódu vyjdou jinde jiné
klíče a bloby jednoho účelu nejdou podstrčit druhému.

Šifrování je jen ze stdlib (`hashlib`, `hmac`, `os.urandom`), aby doplněk pro
Kodi nepotřeboval `script.module.pycryptodome`: instalace s vypnutým oficiálním
repem by si aktualizaci nestáhla. Nevymýšlí se šifra, skládají se standardní
primitiva — SHA-256 v counter módu jako proudová šifra a encrypt-then-MAC:

    blob  = nonce(16 B) || ciphertext || tag(32 B)
    proud = SHA-256(enc || nonce || counter_be64)    counter = 0, 1, 2, …
    ct    = gzip(json) XOR proud
    tag   = HMAC-SHA256(mac, nonce || ct)

Gzip před šifrováním není optimalizace, ale součást návrhu: plný stav 5000 titulů
je 511 kB JSON → 13 kB a zabalení trvá 8 ms místo 102 ms.
"""
import gzip
import hashlib
import hmac
import json
import os

# Crockford Base32 bez I, L, O a U — znaky, které se z obrazovky televize opisují
# špatně (jedničku od I a nulu od O nerozezná ani dobrý skin, U svádí na V).
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PREFIX = "NKT"
_CONFUSED = {"I": "1", "L": "1", "O": "0", "U": "V"}

ITERATIONS = 200000
NONCE = 16
TAG = 32


class SealError(Exception):
    """Chyba, kterou má smysl ukázat uživateli (špatný tvar kódu)."""


def new_code(length):
    """Nový kód. Náhoda jde z `os.urandom`, ne z `random`."""
    return format_code("".join(ALPHABET[b % len(ALPHABET)] for b in os.urandom(length)))


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


def valid_code(text, length):
    raw = normalize_code(text)
    return len(raw) == length and all(ch in ALPHABET for ch in raw)


class Keys(object):
    """Klíče odvozené z kódu. Drž je v paměti — PBKDF2 stojí na slabém Android
    boxu i půl sekundy a při každém použití by to bylo znát."""

    __slots__ = ("ident", "enc", "mac")

    def __init__(self, code, salt, length):
        if not valid_code(code, length):
            raise SealError("Kód nemá správný tvar")
        if not salt:
            raise SealError("Chybí sůl")
        root = hashlib.pbkdf2_hmac("sha256", normalize_code(code).encode("ascii"),
                                   salt if isinstance(salt, bytes) else salt.encode("ascii"),
                                   ITERATIONS)
        self.ident = hmac.new(root, b"gid", hashlib.sha256).hexdigest()[:32]
        self.enc = hmac.new(root, b"enc", hashlib.sha256).digest()
        self.mac = hmac.new(root, b"mac", hashlib.sha256).digest()


_KEYS = {}


def keys_for(code, salt, length):
    """Klíče s cache v paměti procesu. Bez ní by PBKDF2 (200 000 iterací, na
    slabém Android boxu i půl sekundy) běžel při každém použití znovu."""
    key = (normalize_code(code), salt if isinstance(salt, bytes) else str(salt), length)
    if key not in _KEYS:
        _KEYS[key] = Keys(code, salt, length)
    return _KEYS[key]


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
    téhož obsahu vypadají na serveru jinak."""
    raw = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), 6)
    nonce = os.urandom(NONCE)
    ct = _xor(raw, _keystream(keys.enc, nonce, len(raw)))
    tag = hmac.new(keys.mac, nonce + ct, hashlib.sha256).digest()
    return nonce + ct + tag


def unseal(keys, blob):
    """Blob → slovník, nebo `None`, když nesedí tag (cizí kód, jiná sůl, poškozený
    přenos, podvržený obsah). Tag se ověřuje **před** dešifrováním."""
    if not blob or len(blob) < NONCE + TAG:
        return None
    nonce, ct, tag = blob[:NONCE], blob[NONCE:-TAG], blob[-TAG:]
    if not hmac.compare_digest(tag, hmac.new(keys.mac, nonce + ct, hashlib.sha256).digest()):
        return None
    try:
        return json.loads(gzip.decompress(_xor(ct, _keystream(keys.enc, nonce, len(ct)))).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
