"""Rozbor, filtrování a řazení streamů (Luna i Sosáč) podle nastavení.

Luna popisuje stream textem („4K HDR“, „18 Mb/s · 2:42:02 · 19.8G“, „Zvuk: CZ 5.1 · GB 5.1“,
„Tit.: CZ“), Sosáč jen „CZ - HD“. Z toho se vytáhne kvalita, velikost, bitrate,
jazyky zvuku a titulků, aby šlo skrýt SD, omezit velikost, preferovat CZ a řadit.
"""
import re
import unicodedata

QUALITY_RANK = (("4K", 4), ("2160", 4), ("UHD", 4), ("FULL HD", 3), ("1080", 3), ("HD", 2), ("720", 2), ("SD", 1))
LANG_ALIASES = {"GB": "EN", "US": "EN", "UK": "EN", "CZ": "CZ", "SK": "SK", "EN": "EN"}
# „19.8G“ / „4.9 GB“, ale ne „18 Mb/s“ (bitrate)
SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([GMT])(?:B|iB)?(?![A-Za-z/])", re.I)
BITRATE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*Mb/s", re.I)
# délka streamu, jak ji posílá Luna: „2:42:02“ (h:mm:ss) nebo „42:02“ (mm:ss)
DURATION_RE = re.compile(r"(?<!\d)(?:(\d+):)?(\d{1,2}):(\d{2})(?!\d)")
LANG_RE = re.compile(r"\b([A-Z]{2})\b")
AUDIO_RE = re.compile(r"\b([A-Z]{2})\s+(\d(?:\.\d)?)\b")   # „CZ 5.1“, „GB 2.0“


# Uploadeři na HellSpy a WebShare lepí za název souboru znak z cizího písma, aby se
# jejich kopie téhož souboru lišily: „… UHD CZ  ᚠ", „(ሐ)", „ก", „(ア)", „(ㄅ)".
# Z názvu nic nevyčteš a škodí: klient Stremia kvůli němu přepne na záložní písmo
# a v celém popisu streamu přestane kreslit emoji (vlaječky jako písmena v rámečku)
# — a kopie téhož souboru se kvůli němu neslučovaly.
JUNK_SUFFIX_RE = re.compile(r"\s*[\(\[]?\s*([^\s()\[\]]{1,3})\s*[\)\]]?\s*$")


def _junk_token(token):
    """1–3 písmena z písma, které není latinka (runy, etiopské, thajské, kana…)."""
    for ch in token:
        if not unicodedata.category(ch).startswith("L"):
            return False
        if unicodedata.name(ch, "").startswith("LATIN"):
            return False
    return True


def clean_file_name(name):
    """Ořízne koncovku z cizího písma na konci názvu souboru. Víc slov cizím písmem
    (skutečný ruský nebo japonský název) nechá — bere jen 1–3 znaky za mezerou."""
    name = name or ""
    m = JUNK_SUFFIX_RE.search(name)
    if m and m.start() > 0 and name[:m.start()].strip() and _junk_token(m.group(1)) \
            and (name[m.start() - 1:m.start()].isspace() or name[m.start():m.start() + 1].isspace()
                 or name[m.start():].lstrip().startswith(("(", "["))):
        return name[:m.start()].rstrip()
    return name


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


def human_size(nbytes):
    """Velikost jako text pro popisek streamu — jedna podoba pro všechny zdroje („4.2 GB",
    „512 MB"). Dřív měl každý klient vlastní verzi s jiným zaokrouhlením (WebShare „4.5 GB",
    Sledujteto „4.50 GB", HellSpy „4.5 GB"), a `_merge_direct` páruje soubory právě podle velikosti."""
    try:
        n = int(nbytes)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    gb = n / 2 ** 30
    return f"{gb:.1f} GB" if gb >= 1 else f"{n / 2 ** 20:.0f} MB"


def fold(text):
    """Bez diakritiky, malá písmena — jedno místo pro porovnávání názvů souborů (dřív totéž
    v engine i storage_api)."""
    import unicodedata
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


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
    r"^(cz|cze|czech|dab|dabing|dabovano|sk|slo|slovak|slovensky|en|eng|english|hu|hun|hungarian|magyar)$",
    re.IGNORECASE)
NAME_SUB_RE = re.compile(r"^(cz|sk|en|hu)?(tit|titulky|sub|subs)$", re.IGNORECASE)
NAME_LANG_MAP = {"cz": "CZ", "cze": "CZ", "czech": "CZ", "dab": "CZ", "dabing": "CZ", "dabovano": "CZ",
                 "sk": "SK", "slo": "SK", "slovak": "SK", "slovensky": "SK",
                 "en": "EN", "eng": "EN", "english": "EN",
                 "hu": "HU", "hun": "HU", "hungarian": "HU", "magyar": "HU"}


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



ORIGIN_CZ = ("czech", "česk", "cesk", "czechoslovak", "československ")
ORIGIN_SK = ("slovak", "slovensk")


def origin_languages(country):
    """Jazyky původní tvorby podle země („Czech Republic“, „CZ, SK“) — jen CZ/SK, jinde prázdné."""
    text = str(country or "").lower()
    out = []
    if any(w in text for w in ORIGIN_CZ) or re.search(r"\bcz\b", text):
        out.append("CZ")
    text = re.sub(r"če?s?ko?slovensk\w*|czechoslovak\w*", "", text)   # Československo není Slovensko
    if any(w in text for w in ORIGIN_SK) or re.search(r"\bsk\b", text):
        out.append("SK")
    return out


def assume_origin_language(streams, country):
    """Český/slovenský titul s jedinou zvukovou stopou označenou „EN“ = špatně označený soubor.

    Kontejnery mívají jazyk zvuku vyplněný „eng“ jen ze zvyku muxeru (seriál Hospoda:
    „EN 1.9 GB“ u původně české tvorby). Hlavička to nerozliší, tak se jazyk odhadne podle
    země původu a stream se označí jako neověřený odhad (`_langs_from_name`, v popisku „~CZ“).
    Anglicky pojmenovaný soubor („…eng…“) se nemění, dvojjazyčný zvuk taky.
    """
    origin = origin_languages(country)
    if not origin:
        return streams
    for s in streams:
        if s.get("langs") != ["EN"] or s.get("_langs_from_name"):
            continue
        name = s.get("_ws_name") or s.get("label") or ""
        if "EN" in langs_from_name(name):
            continue
        s["langs"] = origin[:1]
        s["_langs_from_name"] = True
    return streams


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
    m = DURATION_RE.search(detail)
    s["duration"] = (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))) if m else 0
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
    # přepočet po přečtení hlavičky (`Engine._fill_audio` smaže `quality_rank`): příznak
    # odhadu z minula pryč, jinak soubor s „(CZ)“ v názvu zůstal i s ověřenou češtinou
    # za všemi ověřenými streamy — 1080p z FastShare pod 480p ze Sosáče (Čertí brko 2026-09-18)
    s.pop("_langs_from_name", None)
    # Bez „Zvuk:“/„Tit.“ v `detail` (probe_audio=False, nebo zdroj typu WebShare/HellSpy,
    # co o zvuku ve výpisu nic neřekne) zbývá jen odhad z názvu souboru — jinak `langs`/
    # `subs` zůstanou prázdné i u zjevně označeného „…_cz_dab_1080p.mp4“. Header z
    # `_fill_audio()` má vždycky přednost, tahle záloha se použije, jen když nic neřekl.
    # `_langs_from_name` značí, že `langs` je jen odhad z názvu, ne ověřený údaj —
    # `arrange()` podle toho v `lang_group()` drží ověřené streamy před pouhým odhadem.
    if not s["langs"]:
        guess = langs_from_name(label if s.get("_direct") else (s.get("_ws_name") or label))
        if guess:
            s["langs"] = guess
            s["_langs_from_name"] = True
    if not s["subs"]:
        s["subs"] = subs_from_name(label if s.get("_direct") else (s.get("_ws_name") or label))
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


HDR_RE = re.compile(r"(?<![A-Za-z0-9])(HDR10\+?|HDR|DV|DoVi|Dolby[ ._-]?Vision)(?![A-Za-z0-9])", re.IGNORECASE)
MERGE_SIZE_TOLERANCE = 0.10   # verze do ±10 % velikosti jsou pro výběr totéž (přání uživatele 2026-09-18)


def stream_hdr(s):
    """HDR / Dolby Vision podle popisku nebo názvu souboru — na televizi jiný obraz,
    na starší i chyba přehrání, takže se takové verze se SDR neslučují."""
    text = " ".join(str(s.get(k) or "") for k in ("label", "_ws_name", "name"))
    return bool(HDR_RE.search(text))


STEREO_3D_RE = re.compile(r"(?<![A-Za-z0-9])(3D|HSBS|H-SBS|H-?OU|Half[ ._-]?(?:SBS|OU|TAB)|MVC)(?![A-Za-z0-9])",
                          re.IGNORECASE)


def stream_3d(s):
    """3D verze (SBS, OU, MVC) podle hlavičky MKV (`StereoMode`), jinak podle popisku nebo
    názvu souboru — na běžné TV dva obrazy vedle sebe nebo nad sebou."""
    if (s.get("_media") or {}).get("stereo3d"):
        return True
    text = " ".join(str(s.get(k) or "") for k in ("label", "_ws_name", "name"))
    return bool(STEREO_3D_RE.search(text))


def merge_key(s):
    """Co uživatel při výběru streamu opravdu řeší: kvalita, jazyky zvuku a titulků,
    prostorový zvuk a HDR. Velikost se porovnává zvlášť, s tolerancí."""
    return (int(s.get("quality_rank") or 0), tuple(sorted(s.get("langs") or ())),
            tuple(sorted(s.get("subs") or ())), is_surround(s), stream_hdr(s), stream_3d(s))


def _same_size(a, b):
    if not a or not b:
        return not a and not b   # neznámá velikost se slučuje jen s neznámou
    return abs(a - b) <= MERGE_SIZE_TOLERANCE * max(a, b)


def group_streams(streams):
    """Sloučí verze, mezi kterými by uživatel nevybíral (`merge_key` + velikost ±10 %).

    Vstup musí být seřazený — zástupcem skupiny je první, tedy nejlepší podle řazení.
    Ostatní se schovají do jeho `_alts` (náhradní odkazy při selhání, „Zobrazit všechny
    streamy“). Vlastní úložiště a výsledky uvolněného fulltextu se neslučují nikdy —
    úložiště má být vždy vidět a uvolněný fulltext posuzuje uživatel podle názvu.

    Slučuje se před čtením hlaviček, takže jazyk může být jen odhad z názvu souboru;
    hlavičky se pak čtou jen u zástupců (rychlejší výběr)."""
    out, reps = [], {}
    for s in streams:
        s.pop("_alts", None)
        if s.get("source") == "dav" or s.get("_loose"):
            out.append(s)
            continue
        key = merge_key(s)
        size = s.get("size_gb") or 0
        rep = next((r for r in reps.get(key, ()) if _same_size(r.get("size_gb") or 0, size)), None)
        if rep is None:
            reps.setdefault(key, []).append(s)
            out.append(s)
        else:
            rep.setdefault("_alts", []).append(s)
    return out


def expand_groups(streams):
    """Opak `group_streams`: zástupci i schované verze, každá zvlášť."""
    out = []
    for s in streams:
        alts = s.pop("_alts", None) or []
        out.append(s)
        out.extend(alts)
    return out


def arrange(streams, pref_lang="", hide_sd=False, max_size_gb=0.0, order="source", pref_surround=False,
            hide_3d=False):
    """Vyfiltruje a seřadí streamy; když by filtr nic nenechal, vrátí původní pořadí.

    order: source (jak přišly) | quality (nejlepší první) | size_desc | size_asc
    """
    for s in streams:
        parse_stream(s)
    if hide_3d:
        # na rozdíl od ostatních filtrů bez pádu na původní seznam: 3D se neukáže nikdy
        # (přání uživatele 2026-09-26), ani mezi sloučenými verzemi
        streams = [s for s in streams if not stream_3d(s)]
        for s in streams:
            if s.get("_alts"):
                s["_alts"] = [a for a in s["_alts"] if not stream_3d(a)]
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

    def verified(s):
        """Ověřené napřed, odhadnuté až za nimi.

        Jazyk ve `langs` přišel od zdroje nebo z hlavičky souboru; stream, který
        ho má jen odhadnutý z názvu (`parse_stream()` nastaví `_langs_from_name`,
        když se do „Zvuk:“ dostat nedalo), patří níž. Je to jen remízový klíč —
        odhadnuté 4K nemá spadnout pod ověřené SD.
        """
        return 0 if s.get("langs") and not s.get("_langs_from_name") else 1

    def lang_group(s):
        """Preferovaný jazyk je hlavní klíč: nejdřív streamy s ním, pak všechno ostatní
        včetně neznámého jazyka (přání uživatele 2026-09-16 — kdo chce češtinu, nemá ji
        hledat mezi desítkami anglických streamů).

        Čeština odhadnutá z názvu souboru (`_langs_from_name`) patří do stejné skupiny
        jako ověřená — ověření rozhoduje až remízu (`verified`). Dřív byla o patro níž
        a 4K z FastShare s „CZ“ v názvu tak skončilo pod 480p ze Sosáče (Matrix
        2026-09-18): se stropem na čtení hlaviček zůstane neověřených víc a dobré
        soubory by propadaly na konec. Hlavička, která jazyk vyvrátí, ho z `langs`
        vyřadí, takže takový soubor spadne dolů sám."""
        if not pref_lang:
            return 0
        return 0 if pref_lang in s["langs"] else 1

    def surround_key(s):
        return 0 if (pref_surround and is_surround(s, pref_lang)) else 1

    if order == "quality":
        keyed.sort(key=lambda p: (lang_group(p[1]), -p[1]["quality_rank"], verified(p[1]), surround_key(p[1]),
                                  -p[1]["bitrate"], p[0]))
    elif order == "size_desc":
        keyed.sort(key=lambda p: (lang_group(p[1]), -p[1]["size_gb"], verified(p[1]), surround_key(p[1]), p[0]))
    elif order == "size_asc":
        keyed.sort(key=lambda p: (lang_group(p[1]), p[1]["size_gb"] or 1e9, verified(p[1]), surround_key(p[1]),
                                  p[0]))
    elif pref_lang or pref_surround:
        # bez řazení: jen preferovaný jazyk / 5.1 dopředu, pořadí uvnitř skupin zachovat
        keyed.sort(key=lambda p: (lang_group(p[1]), verified(p[1]), surround_key(p[1])))
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
