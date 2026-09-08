"""Rozbor, filtrování a řazení streamů (Luna i Sosáč) podle nastavení.

Luna popisuje stream textem („4K HDR“, „18 Mb/s · 2:42:02 · 19.8G“, „Zvuk: CZ 5.1 · GB 5.1“,
„Tit.: CZ“), Sosáč jen „CZ - HD“. Z toho se vytáhne kvalita, velikost, bitrate,
jazyky zvuku a titulků, aby šlo skrýt SD, omezit velikost, preferovat CZ a řadit.
"""
import re

QUALITY_RANK = (("4K", 4), ("2160", 4), ("UHD", 4), ("FULL HD", 3), ("1080", 3), ("HD", 2), ("720", 2), ("SD", 1))
LANG_ALIASES = {"GB": "EN", "US": "EN", "UK": "EN", "CZ": "CZ", "SK": "SK", "EN": "EN"}
# „19.8G“ / „4.9 GB“, ale ne „18 Mb/s“ (bitrate)
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([GMT])(?:B|iB)?(?![A-Za-z/])", re.I)
BITRATE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*Mb/s", re.I)
LANG_RE = re.compile(r"\b([A-Z]{2})\b")


def quality_rank(text):
    up = (text or "").upper()
    for key, rank in QUALITY_RANK:
        if key in up:
            return rank
    return 0


def parse_size_gb(text):
    m = SIZE_RE.search(text or "")
    if not m:
        return 0.0
    val = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    return val * 1024 if unit == "T" else (val / 1024 if unit == "M" else val)


def parse_langs(segment):
    return {LANG_ALIASES.get(code, code) for code in LANG_RE.findall(segment or "")} - {"HD", "SD", "DV"}


def parse_stream(s):
    """Doplní do streamu klíče quality, size_gb, bitrate, langs, subs (idempotentní)."""
    if "quality_rank" in s:
        return s
    label, detail = s.get("label") or "", s.get("detail") or ""
    s["quality_rank"] = quality_rank(label) or quality_rank(s.get("quality") or "")
    s["size_gb"] = parse_size_gb(detail)
    m = BITRATE_RE.search(detail)
    s["bitrate"] = float(m.group(1).replace(",", ".")) if m else 0.0
    audio, subs = "", ""
    for part in detail.split("|"):
        part = part.strip()
        if part.startswith("Zvuk:"):
            audio = part[5:]
        elif part.startswith("Tit."):
            subs = part.split(":", 1)[-1]
    if s.get("source") == "sosac":
        audio = label  # „Sosáč CZ - HD“
    s["langs"] = parse_langs(audio)
    s["subs"] = parse_langs(subs)
    return s


def arrange(streams, pref_lang="", hide_sd=False, max_size_gb=0.0, order="source"):
    """Vyfiltruje a seřadí streamy; když by filtr nic nenechal, vrátí původní pořadí.

    order: source (jak přišly) | quality (nejlepší první) | size_desc | size_asc
    """
    for s in streams:
        parse_stream(s)
    kept = []
    for s in streams:
        if hide_sd and s["quality_rank"] and s["quality_rank"] <= 1:
            continue
        if max_size_gb and s["size_gb"] and s["size_gb"] > max_size_gb:
            continue
        kept.append(s)
    if not kept:
        kept = list(streams)
    keyed = list(enumerate(kept))
    if order == "quality":
        keyed.sort(key=lambda p: (-p[1]["quality_rank"], -p[1]["bitrate"], p[0]))
    elif order == "size_desc":
        keyed.sort(key=lambda p: (-p[1]["size_gb"], p[0]))
    elif order == "size_asc":
        keyed.sort(key=lambda p: (p[1]["size_gb"] or 1e9, p[0]))
    if pref_lang:
        # preferovaný jazyk zvuku dopředu, pořadí uvnitř skupin zachovat
        keyed.sort(key=lambda p: 0 if pref_lang in p[1]["langs"] else 1)
    return [s for _, s in keyed]


if __name__ == "__main__":
    demo = [
        {"label": "4K HDR", "detail": "18 Mb/s · 2:42:02 · 19.8G | Zvuk: CZ 5.1 · GB 5.1 | Tit.: CZ", "source": "main"},
        {"label": "(WS) SD", "detail": "1 Mb/s · 2:10:47 · 0.7G", "source": "search"},
        {"label": "(WS) Full HD", "detail": "5 Mb/s · 2:16:18 · 4.9G | Zvuk: GB 5.1", "source": "search"},
        {"label": "Sosáč CZ - HD", "detail": "", "source": "sosac"},
    ]
    for s in arrange(demo, pref_lang="CZ", hide_sd=True, max_size_gb=10, order="quality"):
        print(s["label"], s["quality_rank"], s["size_gb"], s["langs"], s["subs"])
