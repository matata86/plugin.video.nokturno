"""Zvukové stopy a titulky při přehrávání — výběr podle preferovaného jazyka.

Čistý Python bez vazby na hostitele. Stopy se popisují slovníky ve tvaru, jaký
vrací JSON-RPC Kodi `Player.GetProperties` (`audiostreams`, `subtitles`):
`{"index", "language", "name", "channels", "isdefault", "isforced", "isimpaired"}`.
Hostitel si stopy načte, zeptá se `pick_audio()`/`pick_subtitle()` a výsledek
sám nastaví — tady se nic nepřehrává.

Druhá polovina modulu jsou titulky stažené ze zdroje (WebShare, Sosáč): z názvu
souboru jejich jazyk spolehlivě poznat nejde, proto `subtitle_lang()` čte text
a `decode_subtitle()` ho převede do UTF-8 (české titulky bývají ve windows-1250,
Kodi je pak ukazuje s rozsypanou diakritikou).
"""
import re
import unicodedata

# jazyk stopy v Kodi: ISO 639-2 z kontejneru („cze“/„ces“), výjimečně 639-1 nebo celé jméno
LANG_CODES = {
    "CZ": {"cze", "ces", "cs", "cz", "czech", "cesky", "cestina"},
    "SK": {"slo", "slk", "sk", "slovak", "slovensky", "slovencina"},
    "EN": {"eng", "en", "english", "anglicky", "anglictina"},
}
# ISO 639-2/B do názvu staženého souboru — Kodi z něj jazyk stopy vyčte samo
FILE_CODES = {"CZ": "cze", "SK": "slo", "EN": "eng"}
# titulky blízkého jazyka jsou lepší než žádné; zvuk se tak nezaměňuje (slovenský dabing
# místo originálu je věc vkusu, ne náhrada)
SUBTITLE_FALLBACK = {"CZ": ("CZ", "SK"), "SK": ("SK", "CZ"), "EN": ("EN",)}

# režimy titulků (nastavení `auto_subs`)
SUBS_KEEP = 0         # nechat na Kodi
SUBS_WHEN_NEEDED = 1  # zapnout, když chybí zvuk v preferovaném jazyce; jinak jen vynucené
SUBS_ALWAYS = 2       # vždy titulky v preferovaném jazyce

COMMENTARY_RE = re.compile(r"koment|comment|director|režis", re.IGNORECASE)
FORCED_RE = re.compile(r"forced|vynucen|vynúten|foreign|cizojazy", re.IGNORECASE)
WORD_SPLIT_RE = re.compile(r"[^0-9a-z]+")


def _fold(text):
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return text.lower()


def track_lang(track):
    """Kód jazyka stopy (`CZ`/`SK`/`EN`), nebo "" když ho stopa neprozradí.

    Rozhoduje pole `language`; název stopy („CZ Dabing“, „Czech 5.1“) jen tam,
    kde jazyk chybí nebo je `und` — kontejnery ho po přebalení často ztratí."""
    code = _fold(track.get("language")).strip()
    for lang, codes in LANG_CODES.items():
        if code in codes:
            return lang
    if code and code not in ("und", "unk", "unknown", "mis", "mul", "zxx"):
        return ""  # jazyk je známý, jen žádný z našich (ger, pol…)
    words = set(WORD_SPLIT_RE.split(_fold(track.get("name"))))
    words |= {w[:-3] for w in words if w.endswith("tit") and len(w) > 3}  # „CZtit“
    for lang, codes in LANG_CODES.items():
        if words & codes or (lang == "CZ" and words & {"dabing", "dab"}):
            return lang
    return ""


def is_forced(track):
    """Vynucené titulky (jen cizojazyčné pasáže). Příznak kontejneru chybí často —
    Office 2026-09-16: MKV se stopou „CZE forced“ a `isforced: false`, takže se
    brala jako plné titulky a u dabingu se vypnula."""
    return bool(track.get("isforced")) or bool(FORCED_RE.search(str(track.get("name") or "")))


def _channels(track):
    try:
        return int(track.get("channels") or 0)
    except (TypeError, ValueError):
        return 0


def pick_audio(tracks, current, pref_lang, stream_langs=()):
    """Index zvukové stopy, na kterou přepnout, a jestli preferovaný jazyk hraje.

    Vrací `(index nebo None, audio_ok)`. `audio_ok` je True, když po přepnutí hraje
    preferovaný jazyk, False, když v souboru není, a None, když se to z hlaviček
    nedá poznat (žádná stopa neuvádí jazyk) — pak rozhodne `stream_langs`, jazyky,
    které o streamu tvrdil zdroj; jedna neoznačená stopa v „CZ dabingu“ je česká.
    Stopa, která už hraje v preferovaném jazyce, se nemění — Kodi ji vybralo
    samo nebo si ji uživatel nastavil a přepínat by jen zbytečně cuklo zvukem.
    """
    if not pref_lang:
        return None, None
    current_index = (current or {}).get("index")
    matching = [t for t in tracks or [] if track_lang(t) == pref_lang]
    if not matching:
        if any(track_lang(t) or _fold(t.get("language")) not in ("", "und", "unk", "unknown")
               for t in tracks or []):
            return None, False
        langs = set(stream_langs or ())
        if not langs:
            return None, None
        return None, pref_lang in langs
    if any(t.get("index") == current_index for t in matching):
        return None, True
    best = sorted(matching, key=lambda t: (bool(COMMENTARY_RE.search(str(t.get("name") or ""))),
                                           -_channels(t), not t.get("isdefault"), t.get("index") or 0))[0]
    return best.get("index"), True


def pick_subtitle(subtitles, pref_lang, audio_ok, mode=SUBS_WHEN_NEEDED):
    """Co udělat s titulky: `("on", index)`, `("off", None)` nebo `("keep", None)`.

    - zvuk v preferovaném jazyce hraje → jen vynucené titulky toho jazyka (překlad
      cizojazyčných pasáží), jinak titulky vypnout — ať u dabingu neběží přibalené
      titulky ze zdroje;
    - preferovaný zvuk chybí → plné titulky preferovaného jazyka, pak blízkého
      (CZ ↔ SK), přednost mají neoznačené pro neslyšící a výchozí;
    - nejde poznat, co hraje (`audio_ok is None`) → nic neměnit.
    """
    if not pref_lang or mode == SUBS_KEEP:
        return "keep", None
    subs = list(subtitles or [])

    def full(lang):
        found = [s for s in subs if track_lang(s) == lang and not is_forced(s)]
        found.sort(key=lambda s: (bool(s.get("isimpaired")), not s.get("isdefault"), s.get("index") or 0))
        return found

    forced = [s for s in subs if is_forced(s) and track_lang(s) == pref_lang]
    if mode == SUBS_ALWAYS:
        for lang in SUBTITLE_FALLBACK.get(pref_lang, (pref_lang,)):
            found = full(lang)
            if found:
                return "on", found[0].get("index")
        return ("on", forced[0].get("index")) if forced and audio_ok else ("keep", None)
    if audio_ok is None:
        return "keep", None
    if audio_ok:
        if forced:
            return "on", forced[0].get("index")
        return "off", None
    for lang in SUBTITLE_FALLBACK.get(pref_lang, (pref_lang,)):
        found = full(lang)
        if found:
            return "on", found[0].get("index")
    return "keep", None


# --- stažené titulky ----------------------------------------------------------------

TIMING_RE = re.compile(r"^\s*[\d:,.\->\s]+$|^\s*\d+\s*$|<[^>]+>|\{[^}]*\}")
CZ_ONLY = set("ěřůĚŘŮ")
SK_ONLY = set("ľĺŕôäĽĹŔÔÄ")
# polština a maďarština sdílí se slovenštinou slova i část diakritiky („nie“, „co“, á/é)
OTHER_ONLY = set("ąęłśćźńőűĄĘŁŚĆŹŃŐŰ")
EN_WORDS = {"the", "you", "and", "what", "that", "this", "is", "are", "don't", "it's", "i'm", "your"}
# „to“ ani „a“ ne — jsou i anglicky; bez diakritiky rozhodnou slova, která ta druhá řeč nemá
CZ_WORDS = {"jsem", "jsi", "jsme", "jste", "není", "neni", "proč", "proc", "taky", "tady", "můžu", "muzu", "že", "ze"}
SK_WORDS = {"som", "sme", "ste", "nie", "prečo", "preco", "tiež", "tiez", "sa", "môžem", "mozem", "ako", "čo"}
CZSK_WORDS = CZ_WORDS | SK_WORDS | {"je", "se", "na", "ale", "ne", "tak", "já", "ja", "co"}


def decode_subtitle(raw):
    """Bajty titulků → text. UTF-8 (i s BOM) a UTF-16 poznají podle BOM, jinak
    windows-1250 — tak ukládá titulky většina českých a slovenských programů."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1250", "replace")


def subtitle_format(text):
    """Přípona podle obsahu — odkaz zdroje ji v názvu mít nemusí."""
    head = text.lstrip()[:200]
    if head.startswith("WEBVTT"):
        return "vtt"
    if head.startswith("[Script Info]"):
        return "ass"
    return "srt"


def subtitle_lang(text):
    """Jazyk titulků podle textu: `CZ`, `SK`, `EN`, nebo "" když si není jistý.

    Čeština a slovenština se liší písmeny, která ta druhá nemá (ř/ě/ů proti
    ľ/ô/ä/ĺ/ŕ), angličtina nemá diakritiku a pozná se podle nejčastějších slov.
    Jiné jazyky (polština, maďarština…) vrátí prázdný řetězec, ne omylem CZ."""
    lines = [TIMING_RE.sub(" ", line) for line in text.splitlines()]
    body = " ".join(line for line in lines if line.strip())
    letters = [ch for ch in body if ch.isalpha()]
    if len(letters) < 200:
        return ""
    cz = sum(1 for ch in letters if ch in CZ_ONLY)
    sk = sum(1 for ch in letters if ch in SK_ONLY)
    words = re.findall(r"[a-zá-žA-ZÁ-Ž']+", body.lower())
    total = max(len(words), 1)
    czsk = sum(1 for w in words if w in CZSK_WORDS) / total
    en = sum(1 for w in words if w in EN_WORDS) / total
    per_mille = 1000.0 / len(letters)
    if sum(1 for ch in letters if ch in OTHER_ONLY) * per_mille >= 2:
        return ""
    if czsk >= 0.03 or (cz + sk) * per_mille >= 3:
        if cz * per_mille >= 2 and cz >= sk * 2:
            return "CZ"
        if sk * per_mille >= 1 and sk > cz:
            return "SK"
        if cz + sk == 0:
            # titulky bez diakritiky
            cz_words = sum(1 for w in words if w in CZ_WORDS)
            sk_words = sum(1 for w in words if w in SK_WORDS)
            if cz_words >= 5 and cz_words >= sk_words * 2:
                return "CZ"
            if sk_words >= 5 and sk_words >= cz_words * 2:
                return "SK"
        return ""
    if en >= 0.04:
        return "EN"
    return ""
