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
AUDIO_RE = re.compile(r"\b([A-Z]{2})\s+(\d(?:\.\d)?)\b")   # „CZ 5.1“, „GB 2.0“


def quality_rank(text):
    up = (text or "").upper()
    for key, rank in QUALITY_RANK:
        if key in up:
            return rank
    return 0


# hrubý odhad kvality podle velikosti — pro soubory, které kvalitu nemají v názvu
SIZE_RANKS = ((14.0, 4), (5.5, 3), (1.6, 2))


def estimate_rank(size_gb):
    """4K / Full HD / HD podle velikosti souboru; 0 když velikost neznáme."""
    if not size_gb:
        return 0
    for limit, rank in SIZE_RANKS:
        if size_gb >= limit:
            return rank
    return 1


def parse_size_gb(text):
    m = SIZE_RE.search(text or "")
    if not m:
        return 0.0
    val = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    return val * 1024 if unit == "T" else (val / 1024 if unit == "M" else val)


def parse_langs(segment):
    return {LANG_ALIASES.get(code, code) for code in LANG_RE.findall(segment or "")} - {"HD", "SD", "DV"}


# jazykové značky v názvech souborů — „cztit“ je titulek, ne zvuk, proto se vylučuje.
# Hledá se po rozdělení názvu na slova, jinak by „Číslo“ dalo „slo“ (= SK).
NAME_SPLIT_RE = re.compile(r"[^0-9A-Za-zÀ-ž]+")
NAME_LANG_RE = re.compile(
    r"^(cz|cze|czech|dab|dabing|dabovano|sk|slo|slovak|slovensky|en|eng|english)$", re.IGNORECASE)
NAME_SUB_RE = re.compile(r"^(cz|sk|en)?(tit|titulky|sub|subs)$", re.IGNORECASE)
NAME_LANG_MAP = {"cz": "CZ", "cze": "CZ", "czech": "CZ", "dab": "CZ", "dabing": "CZ", "dabovano": "CZ",
                 "sk": "SK", "slo": "SK", "slovak": "SK", "slovensky": "SK",
                 "en": "EN", "eng": "EN", "english": "EN"}


def langs_from_name(name):
    """Jazyky zvuku podle názvu souboru („…_cz_1080p.mp4“ → CZ). Titulkové značky se vynechají."""
    words = [w for w in NAME_SPLIT_RE.split(name or "") if w]
    out = set()
    for index, word in enumerate(words):
        if not NAME_LANG_RE.match(word):
            continue
        following = NAME_SUB_RE.match(words[index + 1]) if index + 1 < len(words) else None
        # „cz tit“ je titulek k tomuhle jazyku; „eng cztit“ naopak znamená anglický zvuk
        if following and not following.group(1):
            continue
        out.add(NAME_LANG_MAP[word.lower()])
    return out


def subs_from_name(name):
    """Jazyk titulků podle názvu souboru — „…_CZtit_…“ (jedno slovo) i „…_cz_tit_…“
    (rozdělené podpomlčkou/tečkou) → CZ. Doplňuje langs_from_name, která tahle
    slova z audio jazyků naopak vylučuje."""
    words = [w for w in NAME_SPLIT_RE.split(name or "") if w]
    out = set()
    for index, word in enumerate(words):
        m = NAME_SUB_RE.match(word)
        if not m:
            continue
        if m.group(1):
            out.add(NAME_LANG_MAP[m.group(1).lower()])
        elif index > 0 and NAME_LANG_RE.match(words[index - 1]):
            out.add(NAME_LANG_MAP[words[index - 1].lower()])
    return out


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
    # kanály zvuku podle jazyka: {"CZ": 5.1, "EN": 7.1}; Sosáč/Luna bez údaje → prázdné
    channels = {}
    for code, ch in AUDIO_RE.findall(audio):
        try:
            channels[LANG_ALIASES.get(code, code)] = float(ch)
        except ValueError:
            pass
    s["channels"] = channels
    return s


def is_surround(s, pref_lang=""):
    """5.1 a víc – v preferovaném jazyce, když je nastaven, jinak v kterémkoli."""
    ch = s.get("channels") or {}
    if pref_lang and pref_lang in ch:
        return ch[pref_lang] >= 5.1
    return any(v >= 5.1 for v in ch.values())


def arrange(streams, pref_lang="", hide_sd=False, max_size_gb=0.0, order="source", pref_surround=False):
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

    def lang_key(s):
        lang = 0 if (pref_lang and pref_lang in s["langs"]) else 1
        surround = 0 if (pref_surround and is_surround(s, pref_lang)) else 1
        return (lang, surround)

    # řazení podle kvality/velikosti je hlavní klíč, preferovaný jazyk jen rozhoduje remízy
    # (dřív jazyk přebíjel kvalitu → za HD Sosáčem v češtině se objevilo 4K v angličtině)
    if order == "quality":
        keyed.sort(key=lambda p: (-p[1]["quality_rank"], lang_key(p[1]), -p[1]["bitrate"], p[0]))
    elif order == "size_desc":
        keyed.sort(key=lambda p: (-p[1]["size_gb"], lang_key(p[1]), p[0]))
    elif order == "size_asc":
        keyed.sort(key=lambda p: (p[1]["size_gb"] or 1e9, lang_key(p[1]), p[0]))
    elif pref_lang or pref_surround:
        # bez řazení: jen preferovaný jazyk / 5.1 dopředu, pořadí uvnitř skupin zachovat
        keyed.sort(key=lambda p: lang_key(p[1]))
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
