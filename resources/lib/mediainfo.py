"""Co se dá vyčíst z hlavičky souboru: zvuk, titulky a rozlišení.

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
    data, _total = fetch_sized(url, start, end, length, opener)
    return data


def fetch_sized(url, start=None, end=None, length=HEAD, opener=None):
    """Jako `fetch`, ale vrátí i celkovou velikost souboru, když ji server
    poslal v `Content-Range` — u zdrojů bez vlastního údaje o velikosti
    (Sosáč) je jinak jediný způsob, jak se k ní vůbec dostat."""
    if start is None:
        rng = f"bytes=0-{length - 1}"
    elif start < 0:
        rng = f"bytes=-{-start}"
    else:
        rng = f"bytes={start}-{end if end is not None else start + length - 1}"
    req = urllib.request.Request(url, headers={"Range": rng, "User-Agent": UA})
    opened = (opener or urllib.request).urlopen(req, timeout=TIMEOUT)
    with opened as resp:
        data = resp.read()
        content_range = resp.headers.get("Content-Range", "")
    tail = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
    return data, (int(tail) if tail.isdigit() else 0)


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


MKV_MASTER = {0x18538067, 0x1654AE6B, 0xAE, 0xE1, 0xE0}   # Segment, Tracks, TrackEntry, Audio, Video


def _mkv_info_fill(buf, i, end, info):
    """TimecodeScale a Duration ze Segment>Info — délka souboru v sekundách
    je `Duration * TimecodeScale / 1e9` (výchozí TimecodeScale je 1 ms)."""
    while i < end - 2:
        try:
            eid, j = _ebml_id(buf, i)
            size, k, width = _ebml_num(buf, j)
        except (IndexError, ValueError):
            return
        if size == (1 << (7 * width)) - 1:
            size = end - k
        stop = min(k + size, end)
        data = buf[k:stop]
        if eid == 0x2AD7B1 and data:
            info["scale"] = int.from_bytes(data, "big") or info["scale"]
        elif eid == 0x4489:
            if len(data) == 4:
                info["duration"] = struct.unpack(">f", data)[0]
            elif len(data) == 8:
                info["duration"] = struct.unpack(">d", data)[0]
        i = stop


def _mkv_walk(buf, i, end, out, info=None):
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
            _mkv_walk(buf, k, stop, out, info)
        elif eid == 0x1549A966 and info is not None:   # Info — mimo strom stop, netýká se aktuální track
            _mkv_info_fill(buf, k, stop, info)
        elif out:
            data, cur = buf[k:stop], out[-1]
            if eid == 0x83 and data:
                cur["type"] = data[-1]                       # 2 = zvuk, 17 = titulky
            elif eid == 0x86:
                cur["codec"] = data.split(b"\0")[0].decode("ascii", "ignore")
            elif eid == 0x22B59C:
                cur["lang"] = data.split(b"\0")[0].decode("ascii", "ignore")
            elif eid == 0x9F and data:
                cur["channels"] = int.from_bytes(data, "big")
            elif eid == 0xB0 and data:
                cur["width"] = int.from_bytes(data, "big")
            elif eid == 0xBA and data:
                cur["height"] = int.from_bytes(data, "big")
        i = stop
    return


def _from_mkv(head):
    tracks, info = [], {"scale": 1_000_000, "duration": 0.0}
    _mkv_walk(head, 0, len(head), tracks, info)
    return tracks, (info["duration"] * info["scale"] / 1_000_000_000 if info["duration"] else 0)


# --- AVI --------------------------------------------------------------------

def _from_avi(head):
    """RIFF: v `hdrl` je pro každou stopu `strh` (druh) a `strf` (formát),
    délka celého souboru v hlavním `avih` (počet snímků krát jejich délka).

    U obrazu je `strf` hlavička BITMAPINFOHEADER, kde za velikostí struktury
    leží šířka a výška; u zvuku WAVEFORMATEX s počtem kanálů.
    """
    tracks, i, end, duration = [], 12, len(head), 0
    while i + 8 <= end:
        tag, size = head[i:i + 4], struct.unpack("<I", head[i + 4:i + 8])[0]
        body = i + 8
        if tag == b"LIST":
            i = body + 4                       # do seznamu se vstupuje
            continue
        if tag == b"avih" and size >= 20:
            micro_per_frame, total_frames = struct.unpack("<II", head[body:body + 4] + head[body + 16:body + 20])
            if micro_per_frame and total_frames:
                duration = micro_per_frame * total_frames / 1_000_000
        elif tag == b"strh" and size >= 8:
            tracks.append({"type": 2 if head[body:body + 4] == b"auds" else 1})
        elif tag == b"strf" and tracks and size >= 4:
            if tracks[-1].get("type") == 2:
                tracks[-1]["channels"] = struct.unpack("<H", head[body + 2:body + 4])[0]
            elif size >= 12:
                tracks[-1]["width"] = struct.unpack("<i", head[body + 4:body + 8])[0]
                tracks[-1]["height"] = abs(struct.unpack("<i", head[body + 8:body + 12])[0])
        i = body + size + (size & 1)
    return tracks, duration


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


def _mp4_sample_entry(buf, start, end, track):
    """Záznam stopy v `stsd`. U zvuku počet kanálů, u obrazu rozlišení.

    Zvuk: počet kanálů leží 24 bajtů za hlavičkou bloku, ale u AC-3 tam bývá 2
    i u 5.1 — pravda je až v `dac3`/`dec3`. Obraz: šířka a výška na témže místě.
    """
    for kind, body, stop in _mp4_boxes(buf, start, end):
        track["codec"] = kind.decode("ascii", "ignore")
        if track.get("type") == 1:
            if body + 28 <= stop:
                track["width"] = struct.unpack(">H", buf[body + 24:body + 26])[0]
                track["height"] = struct.unpack(">H", buf[body + 26:body + 28])[0]
            break
        if body + 20 <= stop:
            track["channels"] = struct.unpack(">H", buf[body + 16:body + 18])[0]
        for sub, sbody, sstop in _mp4_boxes(buf, body + 28, stop):
            if sub in (b"dac3", b"dec3") and sbody < sstop:
                bits = int.from_bytes(buf[sbody:sbody + 3], "big")
                acmod, lfe = (bits >> 11) & 0x07, (bits >> 10) & 0x01
                track["channels"] = ACMOD.get(acmod, 2) + lfe
        break


def _mp4_mvhd(buf, body, stop):
    """`mvhd`: fullbox header (verze+příznaky), pak časy a jako poslední
    dvojice timescale/duration — u verze 1 jsou pole 8bajtová."""
    if body >= stop:
        return 0
    version = buf[body]
    try:
        if version == 0 and body + 20 <= stop:
            timescale, duration = struct.unpack(">II", buf[body + 12:body + 20])
        elif version == 1 and body + 32 <= stop:
            timescale = struct.unpack(">I", buf[body + 20:body + 24])[0]
            duration = struct.unpack(">Q", buf[body + 24:body + 32])[0]
        else:
            return 0
    except struct.error:
        return 0
    return duration / timescale if timescale else 0


def _from_mp4(buf):
    tracks, duration = [], [0]   # v seznamu, aby do něj šlo psát z vnořené walk()

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
                        handler = buf[b2 + 8:b2 + 12]
                        cur["type"] = {b"soun": 2, b"vide": 1}.get(handler, 17
                                       if handler in (b"sbtl", b"text", b"subt") else 0)
                    elif k2 == b"minf":
                        for k3, b3, s3 in _mp4_boxes(buf, b2, s2):
                            if k3 != b"stbl":
                                continue
                            for k4, b4, s4 in _mp4_boxes(buf, b3, s3):
                                if k4 == b"stsd" and b4 + 8 <= s4:
                                    _mp4_sample_entry(buf, b4 + 8, s4, cur)
        if cur:
            tracks.append(cur)

    def walk(start, end):
        for kind, body, stop in _mp4_boxes(buf, start, end):
            if kind == b"moov":
                walk(body, stop)
            elif kind == b"mvhd":
                duration[0] = _mp4_mvhd(buf, body, stop) or duration[0]
            elif kind == b"trak":
                trak(body, stop)

    walk(0, len(buf))
    return tracks, duration[0]


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
                return [], 0
            base = pos
            if len(buf) < 16:
                return [], 0
        i = pos - base
        size = struct.unpack(">I", buf[i:i + 4])[0]
        kind, header = buf[i + 4:i + 8], 8
        if size == 1:
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            header = 16
        if size < header:
            return [], 0
        if kind == b"moov":
            for window in MOOV_WINDOWS:
                want = min(size, window)
                try:
                    body = fetch(url, start=pos, end=pos + want - 1, opener=opener)
                except Exception:  # noqa: BLE001
                    return [], 0
                tracks, duration = _from_mp4(body)
                # mvhd bývá na začátku moov, takže délku má i první, nejmenší okno —
                # ta se tedy nezahazuje, i když se pro zvuk musí sáhnout po dalším
                if any(t.get("type") == 2 for t in tracks) or want >= size:
                    return tracks, duration
            return [], 0
        pos += size
    return [], 0


# --- dohromady --------------------------------------------------------------

CODECS = {
    "A_AC3": "AC-3", "AC-3": "AC-3", "ac-3": "AC-3",
    "A_EAC3": "EAC3", "ec-3": "EAC3",
    "A_DTS": "DTS", "A_DTS/EXPRESS": "DTS", "A_DTS/LOSSLESS": "DTS-HD",
    "A_TRUEHD": "TrueHD", "A_MLP": "TrueHD",
    "A_AAC": "AAC", "mp4a": "AAC", "A_OPUS": "Opus", "A_VORBIS": "Vorbis",
    "A_FLAC": "FLAC", "A_MP3": "MP3", "A_MPEG/L3": "MP3", "A_PCM/INT/LIT": "PCM",
}


def codec_name(raw):
    """Zkratka kodeku pro popisek. Neznámý se vrátí, jak přišel, jen bez prefixu."""
    raw = str(raw or "")
    if raw in CODECS:
        return CODECS[raw]
    return raw.split("/")[0].removeprefix("A_").removeprefix("V_") or ""


def probe(url, opener=None):
    """Co se o souboru dá zjistit z jeho hlavičky.

    Vrací `{"audio": [...], "subs": [jazyky], "height": int}`; prázdné hodnoty
    tam, kde se nic zjistit nedá. Nikdy nevyhodí výjimku — když to nejde, vrátí
    prázdno a doplněk se chová jako dřív.
    """
    empty = {"audio": [], "subs": [], "width": 0, "height": 0, "duration": 0, "size": 0}
    try:
        head, total_size = fetch_sized(url, length=HEAD, opener=opener)
    except Exception:  # noqa: BLE001 – síť, server bez Range, vypršelý odkaz
        return empty
    if not head:
        return empty
    try:
        if head[:4] == b"\x1a\x45\xdf\xa3":
            tracks, duration = _from_mkv(head)
        elif head[:4] == b"RIFF":
            tracks, duration = _from_avi(head)
        elif head[4:8] == b"ftyp":
            tracks, duration = _from_mp4(head)
            if not any(t.get("type") == 2 for t in tracks):
                tracks, duration = _mp4_remote(url, head, opener)
        else:
            return empty
    except Exception:  # noqa: BLE001 – poškozená nebo neúplná hlavička
        return empty
    audio = [{"lang": LANGS.get((t.get("lang") or "").lower(), ""),
              "channels": CHANNELS.get(int(t.get("channels") or 0), ""),
              "codec": codec_name(t.get("codec"))}
             for t in tracks if t.get("type") == 2]
    subs, seen = [], set()
    for t in tracks:
        code = LANGS.get((t.get("lang") or "").lower(), "")
        if t.get("type") == 17 and code and code not in seen:
            seen.add(code)
            subs.append(code)
    video = [t for t in tracks if t.get("type") == 1]
    width = max((int(t.get("width") or 0) for t in video), default=0)
    height = max((int(t.get("height") or 0) for t in video), default=0)
    return {"audio": audio, "subs": subs, "width": width, "height": height,
            "duration": duration, "size": total_size}


def quality_from_size(width, height=0):
    """Rozlišení z hlavičky na označení kvality, jaké používá zbytek doplňku.

    Rozhoduje šířka, ne výška: širokoúhlý film bývá 1920×800 a podle výšky by
    vyšel jako HD, i když je to plnohodnotné Full HD.
    """
    for limit, name in ((3000, "4K"), (1700, "Full HD"), (1200, "HD")):
        if width >= limit:
            return name
    if width:
        return "SD"
    for limit, name in ((1700, "4K"), (900, "Full HD"), (600, "HD")):
        if height >= limit:
            return name
    return "SD" if height else ""


def describe(info):
    """`Zvuk: CZ 5.1 EN 7.1 | Tit.: CZ` — tvar, ze kterého čte `streams.parse_stream`.

    Stopa bez rozpoznaného jazyka se neztrácí: připíše se jen jejím počtem
    kanálů. Originální zvuk bývá netagovaný a často je to zrovna ta nejlepší
    stopa v souboru, takže by bylo škoda ji zamlčet.
    """
    parts, seen, loose = [], set(), []
    for t in info.get("audio") or []:
        code, chans = t.get("lang"), t.get("channels")
        if code:
            if code in seen:
                continue
            seen.add(code)
            parts.append(f"{code} {chans}" if chans else code)
        elif chans and chans not in loose:
            loose.append(chans)
    parts += [c for c in loose if not any(c in p for p in parts)]
    out = []
    if parts:
        out.append("Zvuk: " + " ".join(parts))
    if info.get("subs"):
        out.append("Tit.: " + " ".join(info["subs"]))
    return " | ".join(out)
