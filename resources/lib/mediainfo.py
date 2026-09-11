"""Zvukové stopy přečtené z hlavičky souboru.

Zdroje často o zvuku nic neřeknou: HellSpy ho v rozhraní vůbec nemá a u souborů
z fulltextu bývá jen to, co si někdo napsal do názvu. Údaj ale leží přímo
v souboru a servery WebShare, Sosáče i HellSpy umí vydat jen jeho výřez
(`Range`), takže stačí stáhnout začátek a rozebrat hlavičku.

Umí tři kontejnery, jiné se tiše přeskočí:

* **Matroska** – stopy jsou hned na začátku, jeden dotaz stačí.
* **AVI** – totéž, jen se v RIFF hledá `strh`/`strf`.
* **MP4** – popis stop (`moov`) bývá až na konci souboru, pak se dotahuje
  druhým dotazem na jeho konec.

Výstup je řetězec ve tvaru, který čte `streams.parse_stream`, tedy
`Zvuk: CZ 5.1 EN 2.0`. Jazyky se překládají na dvoupísmenné kódy; co se přeložit
nedá, se zahodí, ať se do popisku nedostane šum.
"""
import struct
import urllib.request

HEAD = 128 * 1024      # začátek souboru: na Matrosku i AVI bohatě stačí
# `moov` u MP4 se dotahuje po oknech; skoro vždy stačí to první
MOOV_WINDOWS = (512 * 1024, 2 * 1024 * 1024, 6 * 1024 * 1024)
TIMEOUT = 20
UA = "Mozilla/5.0 (compatible; Nokturno/1.0)"

# ISO 639-2/B i /T na dvoupísmenné kódy, které používá zbytek doplňku
LANGS = {
    "cze": "CZ", "ces": "CZ", "cz": "CZ",
    "slo": "SK", "slk": "SK", "sk": "SK",
    "eng": "EN", "en": "EN",
    "ger": "DE", "deu": "DE", "de": "DE",
    "pol": "PL", "pl": "PL",
    "hun": "HU", "hu": "HU",
    "rus": "RU", "ru": "RU",
    "fre": "FR", "fra": "FR", "fr": "FR",
    "spa": "ES", "es": "ES",
    "ita": "IT", "it": "IT",
}
# počet kanálů → zápis, na který je zvyklý zbytek doplňku
CHANNELS = {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}
# AC-3: skutečné rozložení kanálů je v `dac3`, ne v hlavičce stopy (tam bývá 2)
ACMOD = {0: 2, 1: 1, 2: 2, 3: 3, 4: 3, 5: 4, 6: 4, 7: 5}


def fetch(url, start=None, end=None, length=HEAD, opener=None):
    """Výřez souboru. Bez `start` se bere začátek, se záporným `start` konec."""
    if start is None:
        rng = f"bytes=0-{length - 1}"
    elif start < 0:
        rng = f"bytes=-{-start}"
    else:
        rng = f"bytes={start}-{end if end is not None else start + length - 1}"
    req = urllib.request.Request(url, headers={"Range": rng, "User-Agent": UA})
    opened = (opener or urllib.request).urlopen(req, timeout=TIMEOUT)
    with opened as resp:
        return resp.read()


# --- Matroska ---------------------------------------------------------------

def _ebml_num(buf, i, strip_marker=True):
    b = buf[i]
    size, mask = 1, 0x80
    while size <= 8 and not (b & mask):
        mask >>= 1
        size += 1
    if size > 8:
        raise ValueError("špatná délka EBML")
    val = (b & (mask - 1)) if strip_marker else b
    for k in range(1, size):
        val = (val << 8) | buf[i + k]
    return val, i + size, size


def _ebml_id(buf, i):
    b = buf[i]
    size, mask = 1, 0x80
    while size <= 4 and not (b & mask):
        mask >>= 1
        size += 1
    val = 0
    for k in range(size):
        val = (val << 8) | buf[i + k]
    return val, i + size


MKV_MASTER = {0x18538067, 0x1654AE6B, 0xAE, 0xE1}   # Segment, Tracks, TrackEntry, Audio


def _mkv_walk(buf, i, end, out):
    while i < end - 2:
        try:
            eid, j = _ebml_id(buf, i)
            size, k, width = _ebml_num(buf, j)
        except (IndexError, ValueError):
            return
        if size == (1 << (7 * width)) - 1:        # neznámá délka
            size = end - k
        stop = min(k + size, end)
        if eid in MKV_MASTER:
            if eid == 0xAE:
                out.append({})
            _mkv_walk(buf, k, stop, out)
        elif out:
            data, cur = buf[k:stop], out[-1]
            if eid == 0x83 and data:
                cur["type"] = data[-1]                       # 2 = zvuk, 17 = titulky
            elif eid == 0x22B59C:
                cur["lang"] = data.split(b"\0")[0].decode("ascii", "ignore")
            elif eid == 0x9F and data:
                cur["channels"] = int.from_bytes(data, "big")
        i = stop
    return


def _from_mkv(head):
    tracks = []
    _mkv_walk(head, 0, len(head), tracks)
    return tracks


# --- AVI --------------------------------------------------------------------

def _from_avi(head):
    """RIFF: v `hdrl` je pro každou stopu `strh` (druh) a `strf` (formát)."""
    tracks, i, end = [], 12, len(head)
    stack = [end]
    while i + 8 <= end:
        tag, size = head[i:i + 4], struct.unpack("<I", head[i + 4:i + 8])[0]
        body = i + 8
        if tag == b"LIST":
            i = body + 4                       # do seznamu se vstupuje
            continue
        if tag == b"strh" and size >= 8:
            tracks.append({"type": 2 if head[body:body + 4] == b"auds" else 1})
        elif tag == b"strf" and tracks and tracks[-1].get("type") == 2 and size >= 4:
            tracks[-1]["channels"] = struct.unpack("<H", head[body + 2:body + 4])[0]
        i = body + size + (size & 1)
    del stack
    return tracks


# --- MP4 --------------------------------------------------------------------

def _mp4_boxes(buf, i, end):
    while i + 8 <= end:
        size = struct.unpack(">I", buf[i:i + 4])[0]
        kind = buf[i + 4:i + 8]
        if size == 1 and i + 16 <= end:        # 64bitová délka
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            body = i + 16
        else:
            body = i + 8
        if size == 0:
            size = end - i
        if size < 8:
            return
        yield kind, body, min(i + size, end)
        i += size


def _mp4_lang(packed):
    """mdhd: tři pětibitová písmena posunutá o 0x60."""
    return "".join(chr(((packed >> shift) & 0x1F) + 0x60) for shift in (10, 5, 0))


def _mp4_audio_entry(buf, start, end, track):
    """Zvukový záznam v `stsd`. U AC-3 je pravda až v `dac3`/`dec3`."""
    for kind, body, stop in _mp4_boxes(buf, start, end):
        track["codec"] = kind.decode("ascii", "ignore")
        if body + 20 <= stop:
            track["channels"] = struct.unpack(">H", buf[body + 16:body + 18])[0]
        for sub, sbody, sstop in _mp4_boxes(buf, body + 28, stop):
            if sub in (b"dac3", b"dec3") and sbody < sstop:
                bits = int.from_bytes(buf[sbody:sbody + 3], "big")
                acmod, lfe = (bits >> 11) & 0x07, (bits >> 10) & 0x01
                track["channels"] = ACMOD.get(acmod, 2) + lfe
        break


def _from_mp4(buf):
    tracks = []

    def trak(start, end):
        cur = {}
        for kind, body, stop in _mp4_boxes(buf, start, end):
            if kind == b"mdia":
                for k2, b2, s2 in _mp4_boxes(buf, body, stop):
                    if k2 == b"mdhd" and b2 + 24 <= s2:
                        off = b2 + (20 if buf[b2] == 0 else 32)
                        if off + 2 <= s2:
                            cur["lang"] = _mp4_lang(struct.unpack(">H", buf[off:off + 2])[0])
                    elif k2 == b"hdlr" and b2 + 12 <= s2:
                        cur["type"] = 2 if buf[b2 + 8:b2 + 12] == b"soun" else 1
                    elif k2 == b"minf":
                        for k3, b3, s3 in _mp4_boxes(buf, b2, s2):
                            if k3 != b"stbl":
                                continue
                            for k4, b4, s4 in _mp4_boxes(buf, b3, s3):
                                if k4 == b"stsd" and b4 + 8 <= s4:
                                    _mp4_audio_entry(buf, b4 + 8, s4, cur)
        if cur:
            tracks.append(cur)

    def walk(start, end):
        for kind, body, stop in _mp4_boxes(buf, start, end):
            if kind == b"moov":
                walk(body, stop)
            elif kind == b"trak":
                trak(body, stop)

    walk(0, len(buf))
    return tracks


def _mp4_remote(url, head, opener=None):
    """Stopy z MP4, kde `moov` leží až za daty.

    Výřez z konce souboru nestačí: `moov` má u dlouhého filmu klidně pár
    megabajtů, takže by v něm byl jen jeho ocas. Pozice se proto počítá z řetězu
    bloků — hlavička dá délku `mdat` a podle ní se skáče dál. Samotný `moov` se
    pak dotahuje po oknech a přestane se, jakmile se najde zvuk; popis stop leží
    na jeho začátku, obsáhlé tabulky vzorků až za ním.
    """
    pos, buf, base = 0, head, 0
    for _hop in range(16):
        if not (base <= pos and pos + 16 <= base + len(buf)):
            try:
                buf = fetch(url, start=pos, length=4096, opener=opener)
            except Exception:  # noqa: BLE001
                return []
            base = pos
            if len(buf) < 16:
                return []
        i = pos - base
        size = struct.unpack(">I", buf[i:i + 4])[0]
        kind, header = buf[i + 4:i + 8], 8
        if size == 1:
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            header = 16
        if size < header:
            return []
        if kind == b"moov":
            for window in MOOV_WINDOWS:
                want = min(size, window)
                try:
                    body = fetch(url, start=pos, end=pos + want - 1, opener=opener)
                except Exception:  # noqa: BLE001
                    return []
                tracks = [t for t in _from_mp4(body) if t.get("type") == 2]
                if tracks or want >= size:
                    return tracks
            return []
        pos += size
    return []


# --- dohromady --------------------------------------------------------------

def audio_tracks(url, opener=None):
    """Zvukové stopy souboru. Prázdný seznam, když se nedají zjistit."""
    try:
        head = fetch(url, length=HEAD, opener=opener)
    except Exception:  # noqa: BLE001 – síť, server bez Range, vypršelý odkaz
        return []
    if not head:
        return []
    try:
        if head[:4] == b"\x1a\x45\xdf\xa3":
            tracks = _from_mkv(head)
        elif head[:4] == b"RIFF":
            tracks = _from_avi(head)
        elif head[4:8] == b"ftyp":
            tracks = _from_mp4(head)
            if not any(t.get("type") == 2 for t in tracks):
                tracks = _mp4_remote(url, head, opener)
        else:
            return []
    except Exception:  # noqa: BLE001 – poškozená nebo neúplná hlavička
        return []
    return [t for t in tracks if t.get("type") == 2]


def describe(tracks):
    """`Zvuk: CZ 5.1 EN 2.0` — tvar, ze kterého čte `streams.parse_stream`.

    Stopa bez rozpoznaného jazyka se neztrácí: připíše se jen jejím počtem
    kanálů. Originální zvuk bývá netagovaný a často je to zrovna ta nejlepší
    stopa v souboru, takže by bylo škoda ji zamlčet. `parse_stream` čte jazyky
    i kanály dvojicí „XX 5.1", takže osamocené číslo jen zobrazí a nic nerozbije.
    """
    parts, seen, loose = [], set(), []
    for t in tracks:
        code = LANGS.get((t.get("lang") or "").lower())
        chans = CHANNELS.get(int(t.get("channels") or 0))
        if code:
            if code in seen:
                continue
            seen.add(code)
            parts.append(f"{code} {chans}" if chans else code)
        elif chans and chans not in loose:
            loose.append(chans)
    parts += [c for c in loose if not any(c in p for p in parts)]
    return "Zvuk: " + " ".join(parts) if parts else ""
