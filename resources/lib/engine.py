"""Jádro integrace — hledání a streamy ve stejných zdrojích jako Kodi doplněk.

Logika odpovídá `default.py` doplňku (sloučení Luna ↔ Sosáč, cross-search, řazení),
ale bez Kodi: volání jsou synchronní a HA je pouští v executoru.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from abort import Aborted, check as check_stop, gather, never
import accounts as accounts_lib
from const import CONF_CZ_ENABLED, CONF_HS_ENABLED, DEFAULT_SORT, LANGS, SORT_ORDERS
from cinemeta_api import CinemetaApi, CinemetaError
from enrich import DEAD_IMAGES, _capped, _cinemeta, _fetch, _fetch_title, enrich, enrich_one
from luna_api import LunaApi, LunaError, clean_label, parse_base_url, parse_token
from tmdb_api import TmdbApi, TmdbError
from prowlarr import ProwlarrApi, ProwlarrError
from qbittorrent import QbitApi, QbitError
from sosac_api import SosacError, names_match
from sosac_api import is_sosac_id as _is_legacy_sosac_id
from sosac_direct import SosacDirect, is_direct_id
from store import Store
from streams import (arrange, assume_origin_language, estimate_rank, expand_groups, fold, group_streams,
                            langs_from_name, parse_stream, stream_hdr)
from tracks import SUBTITLE_FALLBACK
from hellspy_api import HellspyApi, HellspyError, HellspyRateLimited
from sledujteto_api import SledujtetoApi, SledujtetoError
from fastshare_api import FastshareApi, FastshareError, make_ref as fastshare_ref
from cztor_api import CztorApi, CztorError
from storage_api import StorageApi, StorageError, match_texts, parse_ref
from wikidata_api import local_titles
from mediainfo import describe as describe_media, fetch_sized, probe as probe_media, quality_from_size
from opensubtitles_api import BLOK as OSUB_BLOK, OpenSubtitlesApi, OpenSubtitlesError, otisk as osub_otisk
from webshare_api import WebshareApi, WebshareApiError, WebshareError, human_size

WS_LIMIT = 25    # kolik souborů brát z fulltextu WebShare
HS_LIMIT = 25    # totéž pro HellSpy
ST_LIMIT = 25    # totéž pro Sledujteto
FS_LIMIT = 25    # totéž pro FastShare
# značka dílu v názvu souboru: „S01E03", „s1 e3", „1x03"
EPISODE_ANY_RE = re.compile(r"(?<![a-z0-9])s\d{1,2}\s?e\d{1,2}(?!\d)|(?<!\d)\d{1,2}x\d{2}(?!\d)", re.I)
AUDIO_PROBE_MAX = 24          # u kolika streamů se ještě vyplatí číst hlavičku souboru
ENRICH_PROGRESS_ESTIMATE = 10  # počáteční odhad délky enrichu, než search() zjistí skutečný počet
AUDIO_TTL = 30 * 24 * 3600    # obsah souboru se nemění, stačí zjistit jednou
WS_RETRY_S = 60               # po selhání loginu WebShare zkusit znovu až za minutu
ORIG_MEMO_S = 300             # original_titles() se za jeden výpis počítá jednou, ne pětkrát
ORIG_MEMO_FAIL_S = 30         # po výpadku Wikidat jen tak dlouho, aby to pokrylo jeden výpis
SIZE_TOLERANCE = 0.25  # GB – Luna a WebShare zaokrouhlují velikost jinak
SUBS_MAX = 3
SEARCH_CACHE_TTL = 43200      # 12 h – seznam nalezených titulů podle dotazu (Luna, WebShare fulltext)
STREAMS_CACHE_TTL = 259200    # 72 h – seznam streamů k titulu, ale JEN když nějaké našel (viz `cached_if`)

_LOGGER = logging.getLogger(__name__)
# hlavičky souborů se čtou souběžně. 16 vláken místo 8 nic nezrychlilo (Office 2026-09-18,
# Bláznivá dovolená, 24 souborů): všechny začnou do ~1 s, konec drží pár pomalých souborů
# z HellSpy (stažení výřezu 2–3,7 s), ne počet vláken
PROBE_WORKERS = 8
# Na hlavičky se čeká nejvýš tolik sekund — pak se výběr streamu ukáže hned a pomalé
# soubory (typicky pár z HellSpy, 2–3,7 s) se dočtou na pozadí do cache (`media:`,
# 30 dní), takže při dalším otevření titulu už mají ověřený zvuk i rozlišení.
PROBE_DEADLINE = 3.0
SUBS_TASK = "Titulky"   # úloha v souběžném hledání streamů, ne zdroj (nehlásí se do průběhu ani výpadků)
OSUB_TASK = "Titulky OpenSubtitles"   # totéž, jen druhý zdroj titulků (`lib/opensubtitles_api.py`)
# obě úlohy se chovají stejně: nejdou do průběhu ani do výpadků a bez nich se streamy smí cachovat
SUBS_TASKS = (SUBS_TASK, OSUB_TASK)
# kolik titulků z OpenSubtitles se přibalí ke streamu — každé stažení jde z denní
# kvóty uživatele (5 na IP bez účtu, 20 s účtem), takže jen ten nejlepší
OSUB_MAX_NA_STREAM = 1
# Audit 2026-09-19: `kolo()` čekalo na všechny zdroje bez stropu, takže dialog stál na nejpomalejším
# (WebShare/Luna/Sosáč mají timeout 40 s na každý dotaz), i když ostatní dávno odpověděly. Po
# deadlinu se vezme, co je; opozdilec se počítá jako výpadek (výsledek se necachuje) a doběhne
# na pozadí — sám si výsledek do své cache uloží pro příště.
SOURCE_DEADLINE = 20.0
# `SOURCE_DEADLINE` je rozpočet pro *celé* hledání zdrojů, ne pro jedno kolo: `raw_streams()`
# volá `kolo()` podruhé, když hlavní zdroj nic nenašel a český název z TMDB se liší. Bez
# sdíleného rozpočtu se stropy sečtou (2× 20 s) a nikde to neusekne — naměřený nejdelší
# `/stream/` na produkci byl 31,4 s. Druhé kolo tedy dostane, co ze zbytku zbylo, nejméně
# ale `MIN_ROUND_DEADLINE`, ať se rychlému zdroji nezavřou dveře těsně před cílem.
MIN_ROUND_DEADLINE = 2.0
# kolik dalších názvů (originál, anglický, český/slovenský z Wikidat) jde do fulltextových dotazů;
# každý je u každého zdroje další HTTP dotaz (až 10 variant × 5 zdrojů = 45 dotazů na titul)
MAX_TITLE_VARIANTS = 3
# tvar částí odkazu `hs:<id>:<hash>` — id i hash jdou do cesty URL na api.hellspy.to
_HS_ID_RE = re.compile(r"^\d{1,20}$")
_HS_HASH_RE = re.compile(r"^[0-9A-Za-z]{1,64}$")

SOURCE_NAMES = {"main": "Luna", "search": "WebShare", "ws": "WebShare", "sosac": "Sosáč",
                "hs": "HellSpy", "st": "Sledujteto", "fs": "FastShare", "cz": "CZtor", "torrent": "Torrent", "dav": "Úložiště"}

# vlastní úložiště (WebDAV) — až tři, každé s adresou, jménem, heslem a názvem;
# klíče jsou vypsané celé, ať je najde kontrola nastavení v testech konzumentů
STORAGE_OPTIONS = (
    ("dav1_url", "dav1_username", "dav1_password", "dav1_name"),
    ("dav2_url", "dav2_username", "dav2_password", "dav2_name"),
    ("dav3_url", "dav3_username", "dav3_password", "dav3_name"),
)

QUALITY_NAMES = {4: "4K", 3: "Full HD", 2: "HD", 1: "SD", 0: ""}


RUNTIME_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?", re.I)
DEFAULT_RUNTIME_S = 7200    # dvouhodinový film — odhad stopáže, jen když ji titul sám neřekne


def runtime_minutes(text):
    """Stopáž v minutách. Luna/Cinemeta posílají „2h42min", epizody bývají
    holé číslo („42") — bez rozlišení formátu by prosté vytažení číslic
    z „2h42min" dalo „242" a z dvouapůlhodinového filmu udělalo čtyřhodinový.
    """
    text = str(text or "")
    m = RUNTIME_RE.search(text)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


_fold = fold   # HA a Kodi ho importují odsud


SUBTITLE_NAME_RE = {
    "CZ": re.compile(r"(^|[^a-z])(cz|cze|ces|czech|cesky|cestina|cs)([^a-z]|$)"),
    "SK": re.compile(r"(^|[^a-z])(sk|slo|slk|slovak|slovensky|slovencina)([^a-z]|$)"),
    "EN": re.compile(r"(^|[^a-z])(en|eng|english|anglicky)([^a-z]|$)"),
    "HU": re.compile(r"(^|[^a-z])(hu|hun|hungarian|magyar)([^a-z]|$)"),
}


def _subtitle_rank(folded, ranking):
    """Pořadí titulkového souboru podle jazyka ve jménu; menší číslo = dřív.

    `ranking` je pořadí jazyků podle předvolby (`SUBTITLE_FALLBACK`). Soubor bez
    jakékoli jazykové značky skončí hned za nimi — jazyk se z názvu poznat nedá
    a zahodit ho by znamenalo přijít i o správné titulky (pojmenované jen podle
    releasu). Označený cizí jazyk jde úplně dozadu. Bez předvolby jazyka se
    nerozlišuje nic a pořadí zůstane takové, v jakém soubory přišly z WebShare.
    """
    if not ranking:
        return 0
    for index, lang in enumerate(ranking):
        if SUBTITLE_NAME_RE[lang].search(folded):
            return index
    if not any(pattern.search(folded) for pattern in SUBTITLE_NAME_RE.values()):
        return len(ranking)
    return len(ranking) + 1


YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
# číslo dílu hned za názvem („Já, padouch 2", „How.to.Train.Your.Dragon.3") — ne
# zvuk 5.1/7.1, ten má za tečkou jedinou číslici; rok za tečkou („draka.2.2014") dílem je
SEQUEL_AFTER_RE = re.compile(r"[\s._\-:,(\[]*(?:[2-9]|ii|iii|iv|vi|vii|viii)(?![a-z0-9])(?![.,]\d(?!\d))")
# značky, které smí stát kolem krátkého názvu („To", „It") místo dalšího slova
RELEASE_TAGS = frozenset((
    "cz", "sk", "en", "eng", "cze", "czech", "cesky", "dabing", "dab", "dub", "titulky", "tit",
    "cztit", "sktit", "hu", "hun", "hungarian", "magyar", "hutit", "subs", "hd", "fullhd", "uhd",
    "bluray", "bdrip", "brrip", "webrip", "web", "webdl", "dl", "dvdrip", "hdtv", "remux", "hevc",
    "avc", "film", "movie", "mkv", "avi", "mp4",
))


def _years(folded):
    """Roky v názvu souboru. `\\b` mezi číslicí a podtržítkem hranici nevidí
    („Jak_vycvicit_draka_2025"), proto se podtržítka napřed mění na mezery."""
    text = folded.replace("_", " ")
    # rok slepený s „r“/„rok“ („Seber si svých pět švestek r1983“) \b nepozná
    glued = re.findall(r"(?<![a-z0-9])r(?:ok)?[ .-]?(19\d{2}|20\d{2})(?!\d)", text)
    return {int(y) for y in YEAR_RE.findall(text) + glued}


def _title_pattern(text):
    """(slova, ocas, krátký) jedné varianty názvu pro přísný filtr.

    Slova jsou ta delší než dva znaky („Harry Potter a Kámen mudrců" → bez „a"),
    ocas je všechno za posledním z nich („Toy Story 5" → „5"), aby se dalo poznat
    jiný díl. Název jen z krátkých slov („To", „It") by bez slov neměl žádný
    filtr, takže bere všechna a hlídá se zvlášť (viz `_title_leads`).
    """
    raw = re.findall(r"[a-z0-9]+", _fold(text))
    long_idx = [i for i, t in enumerate(raw) if len(t) > 2]
    if long_idx:
        return [raw[i] for i in long_idx], raw[long_idx[-1] + 1:], False
    return raw, [], True


def _prefix_ok(folded, spans, first):
    """Smí název titulu v souboru stát až za textem před ním? (viz `_title_leads`)"""
    before = [t for t, _e in spans[:first]]
    # krátká slova (≤ 2 znaky) patří k názvu — „S čerty nejsou žerty“ začíná krátkým „S“,
    # které filtr slov neřeší; bez téhle výjimky zmizely všechny jeho soubory
    if all(t in RELEASE_TAGS or t.isdigit() or len(t) <= 2 for t in before):
        return True
    start = spans[first][1] - len(spans[first][0])
    prefix = folded[:start].rstrip(" ._")
    return prefix.endswith(("-", "–", "|", ":", "]", ")"))


def _title_leads(folded, pattern, movie, variants=()):
    """Začíná název souboru tímhle názvem titulu? (přísný filtr)

    Slova názvu musí být v souboru za sebou a skoro na začátku. Pouhé „všechna
    slova někde v názvu" propustí i úplně jiný titul, který ta slova jen náhodou
    obsahuje — např. český idiom „Seber si svých pět švestek" vs. film „Pět
    švestek": obě slova tam jsou, ale patří k jiné větě. Skutečný název souboru
    na nich vždycky začíná (nejvýš za značkou webu/edicí v závorce), balíček
    zdrojů (rok, kvalita, kodek…) přijde až za ním.

    U filmu nesmí za názvem hned následovat číslo jiného dílu („Jak vycvičit
    draka 2" u jedničky). Krátký název („To") musí stát úplně na začátku a za
    ním smí být jen rok, kvalita nebo značka jazyka — jinak projde „What
    Happened to Monday" i „Někdo to rád horké". Výjimkou je jiná varianta
    téhož názvu hned za ním („To - It (2017)").
    """
    group, tail, short = pattern
    spans = [(m.group(), m.end()) for m in re.finditer(r"[a-z0-9]+", folded)]
    n = len(group)
    if short:
        start = 0
        while start < len(spans) and spans[start][0] in RELEASE_TAGS:
            start += 1
        if [t for t, _e in spans[start:start + n]] != group:
            return False
        after = spans[start + n:]
        for other in variants:
            if other != group and [t for t, _e in after[:len(other)]] == other:
                after = after[len(other):]
                break
        if after and after[0][0].isalpha() and after[0][0] not in RELEASE_TAGS:
            return False
        end = spans[start + n - 1][1]
    else:
        long_spans = [(i, t) for i, (t, _e) in enumerate(spans) if len(t) > 2]
        tokens = [t for _i, t in long_spans]
        for i in range(min(3, len(tokens) - n + 1)):
            if tokens[i:i + n] != group:
                continue
            first = long_spans[i][0]
            # Název smí začínat až za jiným textem jen tehdy, když je to značka (webu,
            # jazyka, kvality) nebo předpona oddělená závorkou či pomlčkou. Jinak prošel
            # český idiom „Seber si svých pět švestek“ u filmu „Pět švestek“ (2026).
            if first and not _prefix_ok(folded, spans, first):
                continue
            last = long_spans[i + n - 1][0]
            break
        else:
            return False
        after = spans[last + 1:]
        if tail and [t for t, _e in after[:len(tail)]] == tail:
            last += len(tail)
        end = spans[last][1]
    return not (movie and SEQUEL_AFTER_RE.match(folded, end))

# databáze filmů vrací žánry a země anglicky — do shrnutí patří česky
GENRES_CS = {
    "Action": "Akční", "Adventure": "Dobrodružný", "Animation": "Animovaný", "Biography": "Životopisný",
    "Comedy": "Komedie", "Crime": "Krimi", "Documentary": "Dokument", "Drama": "Drama", "Family": "Rodinný",
    "Fantasy": "Fantasy", "History": "Historický", "Horror": "Horor", "Music": "Hudební", "Musical": "Muzikál",
    "Mystery": "Mysteriózní", "Romance": "Romantický", "Sci-Fi": "Sci-fi", "Short": "Krátkometrážní",
    "Sport": "Sportovní", "Thriller": "Thriller", "War": "Válečný", "Western": "Western",
}
COUNTRIES_CS = {
    "Czech Republic": "Česko", "Czechia": "Česko", "Czechoslovakia": "Československo", "Slovakia": "Slovensko",
    "United States": "USA", "United States of America": "USA", "United Kingdom": "Velká Británie",
    "Germany": "Německo", "France": "Francie", "Italy": "Itálie", "Spain": "Španělsko", "Poland": "Polsko",
    "Austria": "Rakousko", "Hungary": "Maďarsko", "Canada": "Kanada", "Japan": "Japonsko", "Denmark": "Dánsko",
    "Sweden": "Švédsko", "Norway": "Norsko", "Netherlands": "Nizozemsko", "Belgium": "Belgie",
    "Switzerland": "Švýcarsko", "Australia": "Austrálie", "Russia": "Rusko", "Ireland": "Irsko",
}


class NokturnoError(Exception):
    """Chyba, kterou má smysl ukázat uživateli."""


def is_sosac_id(item_id):
    """Sosáč napřímo (`sosacd_`) i starší Stremio režim (`sosac2_`).

    Knihovní `sosac_api.is_sosac_id` zná jen ten starší tvar — tituly ze Sosáče by pak
    šly do Luny (žádné streamy) a nedostaly by poster z TMDB.
    """
    return is_direct_id(item_id) or _is_legacy_sosac_id(item_id)


def split_episode_id(item_id):
    """`id:S:E` → (id seriálu, sezóna, epizoda); u filmu (id, None, None)."""
    parts = str(item_id).split(":")
    if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
        return ":".join(parts[:-2]), int(parts[-2]), int(parts[-1])
    return str(item_id), None, None


def _server_odpovedel(err):
    """Odpověděl server, nebo se k němu vůbec nešlo dostat?

    Klienti zdrojů balí obojí do vlastní výjimky (`WebshareError`, `SledujtetoError`…),
    takže se typ nestačí — rozhoduje příčina v `__cause__`. `HTTPError` je odpověď
    serveru (a je podtřídou `URLError`, proto se testuje dřív), kdežto `URLError`
    a `OSError` znamenají DNS, timeout nebo vypnutou síť.
    """
    vidano = set()
    while err is not None and id(err) not in vidano:
        vidano.add(id(err))
        if isinstance(err, urllib.error.HTTPError):
            return True
        if isinstance(err, (urllib.error.URLError, OSError)):
            return False
        err = err.__cause__ or err.__context__
    return True


def _account_fail_code(err):
    """Selhání kontroly účtu → kód. Rozlišuje „účet doplněk odmítl" od „nešlo se zeptat",
    protože první chce zásah uživatele a druhé jen počkat.

    Bez rozlišení hlásil doplněk „nesedí jméno nebo heslo" pokaždé, když obnova na
    pozadí padla na vypnutou síť — typicky na mobilu hned po startu Kodi (nahlásil
    uživatel 2026-09-21: v menu čtyři zdroje v chybě, „Ověřit zdroje" hned nato
    všechny v pořádku).
    """
    if isinstance(err, WebshareApiError):      # WebShare odpověděl a účet odmítl
        return "bad_login"
    if isinstance(err, SledujtetoError) and getattr(err, "status", None):
        return "bad_login"
    if isinstance(err, StorageError):
        return "unreachable"
    if isinstance(err, (WebshareError, SledujtetoError, FastshareError, CztorError)):
        return "bad_login" if _server_odpovedel(err) else "unreachable"
    return "error"


class Engine:
    """Přístup ke třem zdrojům obsahu pod jedním rozhraním."""

    def __init__(self, options, storage_dir, opener=None, store=None, should_stop=None,
                 storage_limits=None):
        """`opener`: volitelný `urllib.request.OpenerDirector` pro vlastní úložiště —
        veřejná instance jím hlídá, kam se smí připojit (viz `StorageApi`).
        `storage_limits`: stropy průchodu cizím úložištěm pro veřejnou instanci —
        `{"crawl_deadline", "max_dirs", "timeout"}`, viz `storage_api.PUBLIC_*`.
        Bez nich se prochází, dokud je co (vlastní NAS v Kodi a HA).
        `store`: už otevřené úložiště hostitele (doplněk pro Kodi má jedno pro celý
        plugin) — jinak se otevře nové v `storage_dir`.
        `should_stop`: zavolatelné bez parametrů → True, když má jádro přestat
        (Kodi: `xbmc.Monitor().abortRequested` — Kodi při vypnutí čeká na doběhnutí
        skriptů, viz `lib/abort.py`). Dlouhé smyčky se ho ptají a vyhodí `Aborted`;
        bez něj (HA, Stremio) se nepřeruší nikdy.

        Volby nad rámec účtů a předvoleb (všechny nepovinné): `audio_probe` (kolika
        souborům číst hlavičku), `cross_search` a `search_streams` (dohledání v
        druhém zdroji, Lunino vlastní hledání), `fresh` (cache streamů jen zapisovat,
        ne číst — zahřívání na pozadí)."""
        self.options = dict(options)
        self.store = store or Store(storage_dir)
        self.opener = opener
        self.storage_limits = dict(storage_limits or {})
        self.should_stop = should_stop or never
        self.last_timings = {}   # časy fází posledního `raw_streams()` (s), viz tam
        self._luna = None
        self._sosac = None
        self._ws = None
        self._ws_ready = False
        self._ws_retry_after = 0.0   # po selhání loginu zkusit znovu až za chvíli, ne nikdy
        self.ws_error = None         # poslední chyba loginu WebShare (HA podle ní spouští reauth)
        self._orig_memo = {}         # original_titles() za jeden výpis: (klíč) → (čas, názvy)
        self._hs = None
        self._st = None
        self._fs = None
        self._cz = None
        self._cz_paired = self._cztor_paired()
        self._osub = None
        self._storages = None
        self._cinemeta = None
        self._sosac_db = None
        self._tmdb = None
        self._prowlarr = self._qbit = None
        self.sub_status = {}   # {"vip": bool, "days": int, "until": str} — plní check_subscription()
        self.stream_progress = {}   # {"id": item_id, "done": int, "total": int} — plní streams() přes on_progress
        self.search_progress = {}   # {"query": str, "done": int, "total": int} — plní search() přes on_progress

    # --- konfigurace --------------------------------------------------------

    def update_options(self, options):
        self.options = dict(options)
        self._luna = self._sosac = self._ws = None
        self._ws_ready = False
        self._ws_retry_after = 0.0
        self.ws_error = None
        self._orig_memo = {}
        self._hs = None
        self._st = None
        self._fs = None
        self._cz = None
        self._cz_paired = self._cztor_paired()
        self._osub = None
        self._storages = None
        self._tmdb = None
        self._prowlarr = self._qbit = None

    def _opt(self, key, default=""):
        value = self.options.get(key, default)
        return value if value is not None else default

    def _check_stop(self):
        """Vyhodí `Aborted`, když hostitel končí — volá se mezi kroky dlouhé práce."""
        check_stop(self.should_stop)

    @property
    def luna(self):
        if self._luna is None:
            token = parse_token(self._opt("luna_token"))
            if token:
                base = parse_base_url(self._opt("luna_url"), self._opt("luna_url"))
                self._luna = LunaApi(base, token, cache=self.store)
        return self._luna

    @property
    def sosac(self):
        if self._sosac is None:
            user = self._opt("streamuj_username").strip()
            if user:
                self._sosac = SosacDirect(user, self._opt("streamuj_password"),
                                          cache=self.store, index_store=self.store.index(),
                                          should_stop=self.should_stop)
        return self._sosac

    @property
    def osub(self):
        """OpenSubtitles, nebo None, když chybí klíč nebo je zdroj vypnutý.

        Klíč nemá uživatel vlastní — rozdává ho dashboard (`DashApi.opensubtitles_key()`),
        proto ho hostitel jen předá ve `options["os_key"]`. Jméno a heslo jsou
        dobrovolná: bez nich platí kvóta 5 stažených titulků na IP za den, s účtem 20.
        """
        if self._osub is None:
            if not self._opt("os_enabled", True):
                return None
            key = str(self._opt("os_key")).strip()
            if not key:
                return None
            self._osub = OpenSubtitlesApi(
                key, store=self.store,
                username=self._opt("os_username").strip(), password=self._opt("os_password"),
                user_agent=str(self._opt("os_user_agent") or "Nokturno v1.0"), opener=self.opener)
        return self._osub

    @property
    def ws(self):
        """Přihlášený WebShare, nebo None. Selhání loginu (výpadek sítě při startu HA)
        dřív zamklo zdroj do restartu — teď se zkusí znovu po `WS_RETRY_S`."""
        if not self._ws_ready and time.time() >= self._ws_retry_after:
            user = self._opt("ws_username").strip()
            if not user:
                self._ws_ready = True
            else:
                api = WebshareApi(user, self._opt("ws_password"))
                try:
                    api.login()
                    self._ws = api
                    self._ws_ready = True
                    self.ws_error = None
                except WebshareError as err:
                    self.ws_error = err
                    _LOGGER.warning("WebShare login selhal, další pokus za %d s: %s", WS_RETRY_S, err)
                    self._ws_retry_after = time.time() + WS_RETRY_S
                    # zadarmo a čerstvěji než obnova na pozadí: odmítnuté heslo je
                    # jiná věc než výpadek sítě, uživatel u první musí zasáhnout
                    self._note_account("webshare", {
                        "level": accounts_lib.FAIL,
                        "code": "bad_login" if isinstance(err, WebshareApiError) else "unreachable",
                        "detail": {"error": str(err)[:120]}})
        return self._ws

    def check_subscription(self):
        """Zjistí, kolik dní zbývá z předplatného WebShare. Volá se z HA periodicky
        (viz __init__.py) a výsledek si nechává v `sub_status` pro senzor i pro
        rozhodnutí, jestli poslat upozornění."""
        ws = self.ws
        if ws is None:
            self.sub_status = {}
            return self.sub_status
        try:
            self.sub_status = ws.account_status()
        except WebshareError as err:
            _LOGGER.debug("stav předplatného WebShare: %s", err)
        return self.sub_status

    @property
    def hs(self):
        """HellSpy nemá účet ani token — stačí přepínač v nastavení."""
        if self._hs is None and self._opt(CONF_HS_ENABLED, False):
            self._hs = HellspyApi(cache=self.store)
        return self._hs

    @property
    def st(self):
        """Sledujteto — účet (e-mail + heslo). Nepřihlašuje se tady, ale až první
        dotaz: `sources()` čte tuhle vlastnost i ze smyčky událostí HA. Token si
        klient drží v úložišti jádra, přehrávat jde jen s Premium účtem."""
        if self._st is None:
            email = str(self._opt("st_email") or "").strip()
            if email and self._opt("st_password"):
                self._st = SledujtetoApi(email, self._opt("st_password"), cache=self.store)
        return self._st

    @property
    def fs(self):
        """FastShare — jméno a heslo z fastshare.cz. Hledá se bez přihlášení, účet
        je potřeba až k přehrání (cookie z loginu) a stahuje se z kreditu, pokud
        účet nemá neomezený tarif."""
        if self._fs is None:
            user = str(self._opt("fs_username") or "").strip()
            if user and self._opt("fs_password"):
                self._fs = FastshareApi(user, self._opt("fs_password"), cache=self.store)
        return self._fs

    @property
    def cz(self):
        """CZtor — jen se zapnutým přepínačem a spárovaným zařízením (tokeny drží
        úložiště jádra, párování dělá hostitel přes `cztor_client()`)."""
        if self._cz is None and self._opt(CONF_CZ_ENABLED, False):
            client = self.cztor_client()
            if client.paired():
                self._cz = client
        return self._cz

    def _cztor_paired(self):
        """Spárovaný CZtor — zjišťuje se při založení enginu (v HA v executoru), protože
        `sources()` čte HA i ze smyčky událostí, kam čtení souboru s tokeny nepatří."""
        return bool(self._opt(CONF_CZ_ENABLED, False)) and self.cztor_client().paired()

    def cztor_client(self):
        """Klient CZtor i bez spárování — pro párování PINem a stav účtu v nastavení."""
        return CztorApi(self.store, device_name=str(self._opt("cz_device_name") or "Nokturno"))

    @property
    def storages(self):
        """Nastavená vlastní úložiště. Síť se tu nevolá — soubory se procházejí
        až při prvním hledání (`_storage_streams`), seznam se pak hodinu pamatuje."""
        if self._storages is None:
            self._storages = []
            for slot, (url, user, password, name) in enumerate(STORAGE_OPTIONS, start=1):
                if not str(self._opt(url) or "").strip():
                    continue
                try:
                    self._storages.append(StorageApi(self._opt(url), self._opt(user), self._opt(password),
                                                     self._opt(name), slot=slot, cache=self.store,
                                                     opener=self.opener, should_stop=self.should_stop,
                                                     **self.storage_limits))
                except StorageError as err:
                    _LOGGER.warning("úložiště %d: %s", slot, err)
        return self._storages

    def storage_for(self, url):
        """`dav:<slot>:<cesta>` → (úložiště, cesta). Adresa serveru se bere z nastavení, ne z odkazu."""
        try:
            slot, path = parse_ref(url)
        except StorageError as err:
            raise NokturnoError(f"Úložiště: {err}") from err
        api = next((s for s in self.storages if s.slot == slot), None)
        if api is None:
            raise NokturnoError("Tohle úložiště už není v nastavení.")
        return api, path

    def storage_request(self, url):
        """(adresa, hlavičky) souboru z vlastního úložiště — pro proxy doplňku pro Stremio."""
        api, path = self.storage_for(url)
        try:
            return api.request(path)
        except StorageError as err:
            raise NokturnoError(f"Úložiště: {err}") from err

    def _fastshare_api(self):
        if self.fs is None:
            raise NokturnoError("Účet FastShare není nastavený.")
        return self.fs

    def fastshare_request(self, url):
        """(adresa, hlavičky s cookie) souboru z FastShare — pro proxy (Stremio, HA mimo Kodi)."""
        try:
            return self._fastshare_api().request(url)
        except FastshareError as err:
            raise NokturnoError(f"FastShare: {err}") from err

    def file_request(self, url):
        """(adresa, hlavičky) pro proxy u odkazů, které bez hlaviček nehrají (`dav:`, `fs:`)."""
        return self.fastshare_request(url) if str(url).startswith("fs:") else self.storage_request(url)

    @property
    def cinemeta(self):
        """Vlastní databáze filmů a seriálů (Stremio/Cinemeta) — bez účtu, funguje
        vždy, i bez Luny a Sosáče. Poslední záchrana v `search()`/`meta()`, když ani
        TMDB, ani veřejný katalog Sosáče nic nenajdou (viz `sosac_db`, `tmdb`)."""
        if self._cinemeta is None:
            self._cinemeta = CinemetaApi(cache=self.store)
        return self._cinemeta

    @property
    def sosac_db(self):
        """Veřejný katalog Sosáče (žádný účet, žádný přepínač) — česká databáze
        filmů/seriálů, funguje vždy. `self.sosac` výš zůstává jen pro přihlášené
        přehrávání; katalog samotný účet nepotřebuje."""
        if self._sosac_db is None:
            self._sosac_db = SosacDirect(cache=self.store, index_store=self.store.index(),
                                         should_stop=self.should_stop)
        return self._sosac_db

    @property
    def tmdb(self):
        """Vlastní klíč uživatele (zdarma, viz nápověda u nastavení) — přednostní
        náhrada za veřejný katalog Sosáče/Cinemetu, když Luna neběží: umí česky
        i to, co ony ne (popis, obsazení). Bez klíče se prostě nepoužije."""
        if self._tmdb is None:
            key = self._opt("tmdb_api_key").strip()
            if key:
                self._tmdb = TmdbApi(key, cache=self.store)
        return self._tmdb

    @property
    def prowlarr(self):
        """Hledání na trackerech. Bez adresy i klíče se torrenty vůbec nenabídnou."""
        if self._prowlarr is None:
            url = self._opt("prowlarr_url").strip()
            key = self._opt("prowlarr_key").strip()
            if url and key:
                self._prowlarr = ProwlarrApi(url, key)
        return self._prowlarr

    @property
    def qbit(self):
        if self._qbit is None:
            url = self._opt("qbit_url").strip()
            if url:
                self._qbit = QbitApi(url, self._opt("qbit_username"), self._opt("qbit_password"))
        return self._qbit

    def sources(self):
        """Které zdroje jsou nastavené — pro diagnostiku a pro kartu.

        WebShare se hlásí podle vyplněných údajů, ne podle `ws`: ta se
        přihlašuje po síti a tohle se čte při počítání atributů senzoru, tedy
        ve smyčce událostí, kam blokující volání nepatří."""
        return {"luna": self.luna is not None, "sosac": self.sosac is not None,
                "webshare": bool(self._opt("ws_username").strip()),
                "hellspy": bool(self._opt(CONF_HS_ENABLED, False)),
                "sledujteto": bool(str(self._opt("st_email") or "").strip()),
                "fastshare": bool(str(self._opt("fs_username") or "").strip()),
                "cztor": self._cz_paired,
                "storage": bool(self.storages),
                "torrent": self.prowlarr is not None}

    # --- stav účtů ----------------------------------------------------------
    #
    # Dvě metody schválně: `accounts()` nesahá na síť (čte ji menu v Kodi při
    # každém otevření), `refresh_accounts()` ano a patří na pozadí. Detail
    # v `lib/accounts.py`.

    def accounts(self, ttl=accounts_lib.TTL):
        """Stav účtů napříč zdroji **bez jediného dotazu na síť** — z toho, co
        naposledy uložil `refresh_accounts()`, plus živá pauza HellSpy z disku.

        Vrací seznam v pořadí `accounts.SOURCES`; texty si skládá volající.
        """
        saved = dict(self.store.load(accounts_lib.STORE, {}) or {})
        if self._opt(CONF_HS_ENABLED, False):
            # pauza po 429 je levná a mění se po minutách — brát ji z dvanáctihodinového
            # záznamu by znamenalo hlásit blokaci dávno po jejím konci
            saved["hellspy"] = {**accounts_lib.hellspy(self.store), "ts": time.time()}
        return accounts_lib.compose(saved, self._account_sources(), ttl=ttl)

    def _luna_konfigurovana(self):
        """Má se Luna ve stavu zdrojů vůbec objevit?

        Vypnutý přepínač znamená ne, i když adresa v nastavení zůstala — v Kodi
        má výchozí hodnotu, takže bez téhle podmínky hlásil stav „běží, ale chybí
        token" každému, kdo Lunu nikdy nezapnul (nahlášeno na `6.6.0~beta11`).
        Větev, která přepínač neposílá (HA, Stremio), se řídí jen vyplněnými údaji
        jako dřív.
        """
        if not self._opt("luna_enabled", True):
            return False
        return bool(self._opt("luna_token").strip() or self._opt("luna_url").strip())

    def _account_sources(self):
        """Které zdroje se mají ve stavu vůbec objevit.

        Není to `sources()`: tam je zdroj „nastavený" teprve, když se s ním dá
        pracovat, kdežto tady jde právě o to pojmenovat rozdělanou práci.
        CZtor se zapnutým přepínačem a bez spárování je typické „nejde mi to";
        podle `sources()` by zmizel jako vypnutý a `not_paired` by se nikdy
        neukázalo. Totéž Luna s adresou a bez tokenu.
        """
        base = self.sources()
        base["cztor"] = bool(self._opt(CONF_CZ_ENABLED, False))
        base["luna"] = self._luna_konfigurovana()
        return base

    def account_problems(self, ttl=accounts_lib.TTL):
        """Jen zdroje, se kterými uživatel musí něco udělat (prázdné = vše v pořádku)."""
        return accounts_lib.problems(self.accounts(ttl=ttl))

    def _note_account(self, source, record):
        """Zapíše stav zdroje zjištěný mimochodem při běžné práci — selhaný login
        WebShare, 429 z HellSpy. Zadarmo a čerstvěji než obnova na pozadí."""
        try:
            with self.store.updating(accounts_lib.STORE, {}) as data:
                data[source] = {**record, "ts": time.time()}
        except Exception as err:  # noqa: BLE001 – stav účtů nesmí shodit hledání
            _LOGGER.debug("stav účtu %s se neuložil: %s", source, err)

    def _account_checks(self, only=None, warn_days=accounts_lib.WARN_DAYS, deep=True):
        """{jméno zdroje: funkce bez parametrů} — jen zdroje, které jsou nastavené.
        Klienti se zakládají tady (bez sítě), samotné dotazy dělá až `refresh_accounts`."""
        chce = (lambda name: True) if only is None else (lambda name: name in set(only))
        checks = {}
        if chce("luna") and self._luna_konfigurovana():
            url, token = self._opt("luna_url"), self._opt("luna_token")
            checks["luna"] = lambda: accounts_lib.luna(url, token, deep=deep)
        if chce("webshare") and self._opt("ws_username").strip():
            def ws_check():
                api = self.ws
                if api is None:
                    # původní výjimka, ne její text — `_account_fail_code` z ní pozná,
                    # jestli WebShare odmítl účet, nebo jen nebyla síť
                    raise self.ws_error or WebshareError("přihlášení se nepovedlo")
                return accounts_lib.webshare(api, warn_days=warn_days)
            checks["webshare"] = ws_check
        if chce("cztor") and self._opt(CONF_CZ_ENABLED, False):
            client = self.cztor_client()
            checks["cztor"] = lambda: accounts_lib.cztor(client, warn_days=warn_days, deep=deep)
        if chce("fastshare") and self.fs is not None:
            checks["fastshare"] = lambda: accounts_lib.fastshare(self.fs)
        if chce("sledujteto") and self.st is not None:
            checks["sledujteto"] = lambda: accounts_lib.sledujteto(self.st)
        if chce("hellspy") and self._opt(CONF_HS_ENABLED, False):
            checks["hellspy"] = lambda: accounts_lib.hellspy(self.store)   # nikdy se neptá po síti
        if chce("storage") and self.storages:
            prvni = self.storages[0]
            checks["storage"] = lambda: accounts_lib.storage(prvni)
        return checks

    def refresh_accounts(self, only=None, warn_days=accounts_lib.WARN_DAYS, deep=True,
                         timeout=25, workers=4):
        """Zjistí stav účtů po síti a uloží ho pro `accounts()`.

        Patří na pozadí — služba v Kodi, časovač v HA. Jeden dotaz na zdroj;
        HellSpy se neptá vůbec, jen si přečte vlastní pauzu. `only` omezí obnovu
        na vyjmenované zdroje, `deep=False` vynechá to, co stojí víc než jeden
        dotaz (ověření tokenu Luny dotazem na streamy, profil CZtoru).
        """
        checks = self._account_checks(only=only, warn_days=warn_days, deep=deep)
        if not checks:
            return self.accounts()
        vysledky = {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(checks)))) as pool:
            futures = {name: pool.submit(fn) for name, fn in checks.items()}
            for name, future in futures.items():
                try:
                    vysledky[name] = future.result(timeout=timeout)
                except Exception as err:  # noqa: BLE001 – přesně tohle chceme uživateli ukázat
                    _LOGGER.debug("stav účtu %s: %s", name, err)
                    vysledky[name] = {"level": accounts_lib.FAIL, "code": _account_fail_code(err),
                                      "detail": {"error": str(err)[:120]}}
        now = time.time()
        try:
            with self.store.updating(accounts_lib.STORE, {}) as data:
                for name, rec in vysledky.items():
                    data[name] = {**rec, "ts": now}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("stav účtů se neuložil: %s", err)
        return self.accounts()

    def _streams_cache_key(self, ctype, item_id, alt=None):
        """Klíč 72h cache streamů — nese i otisk zapnutých zdrojů a účtů. Bez něj měl titul po
        zapnutí nového zdroje (nebo změně účtu) 72 h stejný seznam bez něj a Kodi to obcházelo
        ručním `clear_cache()` jen u CZtoru (audit 2026-09-19). `streams5` = oprava filtru
        (krátké slovo na začátku názvu), `streams6` = otisk zdrojů."""
        podpis = {**self.sources(), "ws": self._opt("ws_username").strip(), "st": str(self._opt("st_email") or "").strip(),
                  "fs": str(self._opt("fs_username") or "").strip(),
                  "dav": [str(self._opt(f"dav{n}_url") or "").strip() for n in range(1, 4)],
                  "search": bool(self._opt("search_streams", True)), "cross": bool(self._opt("cross_search", True))}
        otisk = hashlib.sha1(json.dumps(podpis, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:10]
        return f"streams6:{ctype}:{item_id}:{alt or ''}:{otisk}"

    def api_for(self, item_id):
        api = self.sosac if is_sosac_id(item_id) else self.luna
        if api is None:
            raise NokturnoError("Zdroj tohoto titulu není nastavený (Luna / Sosáč).")
        return api

    # --- hledání ------------------------------------------------------------

    @staticmethod
    def split_year(query):
        """„Pět švestek 2026" → („Pět švestek", 2026). Zdroje hledají jen v názvu, rok
        v dotazu by je zmátl — odřízneme ho a použijeme na filtrování výsledků."""
        query = (query or "").strip()
        match = YEAR_RE.search(query)
        if not match:
            return query, None
        base = (query[: match.start()] + " " + query[match.end():]).strip()
        year = int(match.group(1))
        # „2012" nebo „Blade Runner 2049" — číslo je součást názvu, ne rok vydání
        if not base or year > datetime.now().year + 2:
            return query, None
        return base, year

    def _by_year(self, pairs, year):
        """Rok v dotazu je filtr: projdou jen tituly z toho roku (a ty, kde ho zdroj neuvádí).
        Když nezbude nic, karta nabídne hledání v databázi filmů — je to poctivější
        než ukázat stejnojmenný film o čtyřicet let starší."""
        if not year:
            return pairs
        return [p for p in pairs if self._year(p[0]) in (year, None)]

    @staticmethod
    def _year(meta):
        raw = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
        return int(raw) if raw.isdigit() else None

    def _same_title(self, luna_meta, sosac_meta):
        name = luna_meta.get("name") or ""
        if not (names_match(name, sosac_meta.get("_title")) or names_match(name, sosac_meta.get("_orig"))):
            return False
        y1, y2 = self._year(luna_meta), self._year(sosac_meta)
        return not (y1 and y2 and abs(y1 - y2) > 1)

    def _merge(self, luna_metas, sosac_metas):
        """[(meta, alt)] — titul z Luny s přibaleným id Sosáče, zbytek Sosáče zvlášť."""
        merged, used = [], set()
        for lm in luna_metas:
            alt = None
            for sm in sosac_metas:
                if sm["id"] not in used and self._same_title(lm, sm):
                    alt = sm["id"]
                    used.add(sm["id"])
                    break
            merged.append((lm, alt))
        for sm in sosac_metas:
            if sm["id"] in used:
                continue
            # Sosáč má některé filmy vloženy víckrát; k Luně se spáruje jen první,
            # ostatní kopie by se ve výsledcích objevily jako druhá dlaždice téhož titulu
            if any(self._same_title(lm, sm) for lm in luna_metas):
                continue
            merged.append((sm, None))
        return merged

    @staticmethod
    def _art(url, size=None):
        """Mrtvé náhledy Sosáče neposílat — v kartě je lepší podklad než rozbitý obrázek.

        Syrová data ze Sosáče (`art.fanart`/`art.landscape`) i Cinemety chodí v plné
        `original` velikosti TMDB obrázku — desítky MB na kus jako dekódovaná bitmapa
        v prohlížeči. `size` (např. „w500"/„w1280") to ořízne na rozumnou velikost."""
        if DEAD_IMAGES in (url or ""):
            return ""
        return _capped(url, size) if size else (url or "")

    def _item(self, meta, ctype, alt=None):
        return {
            "id": meta.get("id"),
            "type": ctype,
            "title": meta.get("_title") or meta.get("name") or "",
            "original_title": meta.get("_orig") or "",
            "year": self._year(meta),
            "poster": self._art(meta.get("poster"), "w500"),
            "background": self._art(meta.get("background"), "w1280"),
            "description": (meta.get("description") or "")[:4000],
            "rating": meta.get("imdbRating") or "",
            "source": meta.get("source") or ("sosac" if is_sosac_id(meta.get("id")) else "luna"),
            "alt": alt,
        }

    # kroků před enrichem (Luna, Sosáč) — enrich bývá nejdelší (síťové dotazy na
    # TMDB/Cinemetu položku po položce), na něm se dopočítá reálný zbytek do 100 %
    SEARCH_SOURCE_STEPS = 2

    def search_pairs(self, ctype, query, want_year=None, limit=None, on_tick=None, on_count=None,
                     failures=None, with_enrich=True, force=False):
        """Sloučené výsledky primárního zdroje metadat a přihlášeného Sosáče:
        `([(meta, alt)], mixed)`. Základ `search()` (HA, Stremio) i hledání v Kodi.

        Řetězec zdrojů metadat v pořadí priority — každý se zkusí, jen když předchozí
        nic nevrátil (chybí, nemá klíč, spadl, nebo prostě nic nenašel): TMDB má přednost
        i před Lunou, jakmile má uživatel vlastní klíč (jediný zdroj s českým popisem
        i obsazením bez Luny); Luna zůstává zdrojem streamů nezávisle na tom. Bez obojího
        zaskočí veřejný katalog Sosáče (česky, bez popisu), pak Cinemeta (anglicky,
        nejširší pokrytí). Přihlášený Sosáč se přidává vždycky navíc.

        `with_enrich=False` vrátí holý katalog bez popisů — Kodi z něj bere jen počty
        pro volbu Filmy/Seriály a nesmí na popisy čekat; holý a doplněný výsledek se
        proto cachují zvlášť (a doplněný staví na holém). `limit` ořízne PŘED enrichem
        (HA doplňuje jen to, co ukáže; Kodi vypisuje vše). `failures` dostane
        `(zdroj, chyba)` za každý výpadek; výsledek s výpadkem se necachuje (jako u
        streams). `mixed` = našel primární zdroj i přihlášený Sosáč — Kodi pak u položek
        jen ze Sosáče ukáže značku zdroje.
        """
        failures = [] if failures is None else failures
        errors = []   # jen za tenhle běh — rozhoduje o cachování

        def tick():
            if on_tick:
                on_tick()

        def _fetch_bare():
            luna_metas, sosac_metas = [], []
            del errors[:]
            self._check_stop()
            if self.tmdb:
                try:
                    luna_metas = self.tmdb.catalog(ctype, "popular", search=query)
                    for m in luna_metas:
                        m["source"] = "tmdb"
                except TmdbError as err:
                    errors.append(("TMDB", err))
            self._check_stop()
            if not luna_metas and self.luna:
                try:
                    cid = "search.movie" if ctype == "movie" else "search.series"
                    # LunaApi s `cache=self.store` si hledání pamatuje sama (SEARCH_TTL)
                    luna_metas = self.luna.catalog(ctype, cid, search=query)
                except LunaError as err:
                    errors.append(("Luna", err))
            if not luna_metas:
                try:
                    luna_metas = self.sosac_db.catalog(ctype, "top", search=query)
                    for m in luna_metas:
                        m["source"] = "sosac"
                except SosacError as err:
                    errors.append(("Sosáč", err))
            if not luna_metas:
                try:
                    luna_metas = self.cinemeta.catalog(ctype, "top", search=query)
                    for m in luna_metas:
                        m["source"] = "cinemeta"
                except CinemetaError as err:
                    errors.append(("Cinemeta", err))
            tick()
            self._check_stop()
            # přihlášený Sosáč vždycky navíc — najde i tituly, které TMDB/Luna/Cinemeta nemá
            if self.sosac:
                try:
                    sosac_metas = self.sosac.search(ctype, query)
                except SosacError as err:
                    errors.append(("Sosáč", err))
            tick()
            merged = self._by_year(self._merge(luna_metas, sosac_metas), want_year)
            if limit:
                merged = merged[: int(limit)]
            return {"pairs": [[m, alt] for m, alt in merged], "mixed": bool(luna_metas) and bool(sosac_metas)}

        ttl = 0 if force else SEARCH_CACHE_TTL
        tail = f"{ctype}:{query}:{int(limit) if limit else ''}:{want_year or ''}"
        ok = lambda data: bool(data and data.get("pairs")) and not errors  # noqa: E731
        bare = self.store.cached_if(f"searchbare:{tail}", ttl, _fetch_bare, ok=ok)
        if not with_enrich:
            failures.extend(errors)
            return [(m, alt) for m, alt in bare["pairs"]], bare["mixed"]

        def _fetch_full():
            # dřív jen položky Sosáče (ty jediné popis neměly) — bez Luny ho ale nemají ani
            # ty z Cinemety/veřejného Sosáče, `enrich()` si sama vybere, co doopravdy chybí.
            # Sosáč posílá vždy mrtvý náhled, takže by enrich bez vlastní cache běžel při
            # každém hledání znovu — proto se cachuje až výsledek PO enrichi.
            data = {"pairs": [[m, alt] for m, alt in bare["pairs"]], "mixed": bare["mixed"]}
            enrich([m for m, _alt in data["pairs"]], self.luna, self.store, ctype, on_tick=on_tick, on_count=on_count)
            return data
        full = self.store.cached_if(f"searchfull:{tail}", ttl, _fetch_full, ok=ok)
        failures.extend(errors)
        return [(m, alt) for m, alt in full["pairs"]], full["mixed"]

    def search(self, ctype="movie", query="", limit=20, on_progress=None):
        """Sloučené výsledky z Luny a Sosáče (stejný titul jen jednou), popsané pro HA/Stremio.

        Dotaz zakončený `*` obejde cache a vynutí čerstvá data (hvězdička se před
        hledáním odřízne) — výsledek se přesto zapíše do cache pro příští normální dotaz.

        `on_progress(done, total)`, je-li dán, se volá po každé fázi — stejný vzor
        jako `streams()` (viz tam i `__init__.py`, který ho na hass bezpečně napojuje).
        """
        query = (query or "").strip()
        force = query.endswith("*")
        if force:
            query = query[:-1].strip()
        query, want_year = self.split_year(query)
        if not query:
            raise NokturnoError("Prázdný dotaz.")

        total = self.SEARCH_SOURCE_STEPS + ENRICH_PROGRESS_ESTIMATE
        done = [0]

        def tick():
            if not on_progress:
                return
            done[0] = min(done[0] + 1, total)
            on_progress(done[0], total)

        def on_count(n):
            nonlocal total
            total = self.SEARCH_SOURCE_STEPS + n
            if on_progress:
                on_progress(min(done[0], total), total)

        failures = []
        pairs, _mixed = self.search_pairs(ctype, query, want_year, limit=int(limit or 20), on_tick=tick,
                                          on_count=on_count, failures=failures, force=force)
        if not pairs and failures:
            # jako dřív: když TMDB/Luna spadne a Sosáč něco najde, je to degradovaný seznam
            # (ukáže se, nepamatuje); bez jediného výsledku je to chyba
            raise NokturnoError("; ".join(f"{label}: {err}" for label, err in failures))
        found = [self._item(meta, ctype, alt) for meta, alt in pairs]
        if on_progress and done[0] < total:
            done[0] = total
            on_progress(done[0], total)
        return found

    # --- historie hledání -----------------------------------------------------

    # historie se sdílí s doplňkem pro Kodi pod typem "any" (hlavní hledání) —
    # zápis vede i deník `histlog`, takže se synchronizuje mezi kartou a všemi Kodi
    def history(self):
        return self.store.history("any")

    def add_history(self, query):
        self.store.add_history("any", query)

    def clear_history(self):
        self.store.clear_history("any")

    def search_catalog(self, ctype="movie", query="", limit=10):
        """Hledání v databázi filmů (Cinemeta = IMDb/TMDB) — najde i tituly, které zatím
        žádný ze zdrojů nemá, třeba chystané filmy. Slouží pro seznam „k zhlédnutí"."""
        query, want_year = self.split_year(query)
        if not query:
            raise NokturnoError("Prázdný dotaz.")
        # přes CinemetaApi (cache, UA, timeout jako všude) — dřív třetí vlastní klient Cinemety
        kind = "series" if ctype == "series" else "movie"
        try:
            metas = self.cinemeta.catalog(kind, "top", search=query) or []
        except Exception as err:  # noqa: BLE001
            raise NokturnoError(f"Databáze filmů neodpověděla: {err}") from err
        if want_year:
            metas = [m for m in metas
                     if str(m.get("releaseInfo") or m.get("year") or "")[:4] in (str(want_year), "")]
        out = []
        for meta in metas[: int(limit or 10)]:
            year = str(meta.get("releaseInfo") or meta.get("year") or "")[:4]
            out.append({
                "id": meta.get("id"),
                "type": ctype,
                "title": meta.get("name") or "",
                "year": int(year) if year.isdigit() else None,
                "poster": self._art(meta.get("poster"), "w500"),
                "background": self._art(meta.get("background"), "w1280"),
                "description": (meta.get("description") or "")[:4000],
                "source": "katalog",
                "alt": None,
            })
        return out

    def catalog_detail(self, ctype="movie", item_id=""):
        """Popis, plakát a hodnocení titulu z databáze filmů — katalog Cinemety je nemá."""
        if not item_id:
            raise NokturnoError("Chybí `id`.")
        kind = "series" if ctype == "series" else "movie"
        key = f"cinemeta:{kind}:{item_id}"
        try:
            meta = self.store.cached(key, 86400, lambda: _cinemeta(kind, item_id))
        except Exception as err:  # noqa: BLE001
            raise NokturnoError(f"Databáze filmů neodpověděla: {err}") from err
        # Cinemeta u čerstvých titulů popis nemá — TMDB (přes Lunu) ho většinou zná, a česky
        try:
            meta = {**meta, **{k: v for k, v in _fetch(self.luna, self.store, kind, item_id).items() if v}}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("detail %s: %s", item_id, err)
        year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
        year_num = int(year) if year.isdigit() else None
        if self.luna:  # TMDB podle IMDb id zná český název („Sunday League…“ → „Okresní přebor…“)
            try:
                by_id = self.store.cached(f"lunameta:{kind}:{item_id}", 30 * 86400,
                                          lambda: self.luna.meta(kind, item_id) or {})
                if by_id.get("name"):
                    meta = {**meta, **{k: v for k, v in by_id.items() if v}}
            except Exception as err:  # noqa: BLE001 – Luna nemusí běžet
                _LOGGER.debug("meta z Luny %s: %s", item_id, err)
        if not meta.get("description"):  # čerstvý film — zkusit TMDB ještě podle názvu a roku
            try:
                by_name = _fetch_title(self.luna, self.store, kind, meta.get("name") or "", year_num)
                meta = {**meta, **{k: v for k, v in by_name.items() if v}}
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("detail podle názvu %s: %s", item_id, err)
        return {
            "id": item_id,
            "type": kind,
            "title": self._local_title(kind, meta.get("name") or "", year_num) or meta.get("name") or "",
            "year": year_num,
            "poster": self._art(meta.get("poster"), "w500"),
            "background": self._art(meta.get("background"), "w1280"),
            "description": (meta.get("description") or self._summary(meta))[:4000],
            "rating": meta.get("imdbRating") or "",
            "genres": meta.get("genres") or [],
            "runtime": meta.get("runtime") or "",
            "cast": meta.get("cast") or [],
            "director": meta.get("director") or [],
            "source": "katalog",
        }

    def _local_title(self, kind, name, year):
        """Databáze filmů vede mezinárodní přepis („Pet svestek“) — český název zná TMDB."""
        if not self.luna or not name:
            return ""

        def load():
            cid = "search.movie" if kind == "movie" else "search.series"
            try:
                metas = self.luna.catalog(kind, cid, search=name)
            except Exception:  # noqa: BLE001 – Luna nemusí běžet
                return ""
            for meta in metas[:10]:
                found = meta.get("name") or ""
                if _fold(found) != _fold(name):
                    continue
                my = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
                if year and my.isdigit() and abs(int(my) - year) > 1:
                    continue
                return found
            return ""

        return self.store.cached(f"lname:{kind}:{_fold(name)}:{year or ''}", 30 * 86400, load)

    @staticmethod
    def _summary(meta):
        """Chystané filmy popis nemají nikde — složíme aspoň větu z toho, co je známo."""
        parts = []
        head = ", ".join(GENRES_CS.get(g, g) for g in (meta.get("genres") or []))
        if meta.get("country"):
            country = ", ".join(COUNTRIES_CS.get(c.strip(), c.strip())
                                for c in str(meta["country"]).split(","))
            head = f"{head} · {country}" if head else country
        if head:
            parts.append(head + ".")
        if meta.get("director"):
            parts.append("Režie " + ", ".join(meta["director"][:3]) + ".")
        if meta.get("cast"):
            parts.append("Hrají " + ", ".join(meta["cast"][:5]) + ".")
        return " ".join(parts)

    def search_webshare(self, query, limit=20):
        """Soubory přímo z WebShare (fulltext), bez metadat titulu.

        Dotaz zakončený `*` obejde cache a vynutí čerstvá data, stejně jako u `search()`.
        """
        if not self.ws:
            raise NokturnoError("WebShare účet není nastavený.")
        query = (query or "").strip()
        force = query.endswith("*")
        if force:
            query = query[:-1].strip()
        cache_key = f"webshare:search:{query}:{int(limit or 20)}"
        files, _total = self.store.cached_if(cache_key, 0 if force else SEARCH_CACHE_TTL,
                                              lambda: self.ws.search(query, limit=int(limit or 20)))
        return [{
            "id": "ws:" + f["ident"],
            "type": "file",
            "title": f.get("name") or "",
            "year": None,
            "poster": f.get("img") or "",
            "size": f.get("size_h") or human_size(int(f.get("size") or 0)),
            "source": "webshare",
            "alt": None,
        } for f in files]

    # --- detail -------------------------------------------------------------

    def _meta_for(self, meta_type, item_id):
        """Detail titulu — u tt… id má TMDB přednost i před Lunou, jakmile má uživatel
        vlastní klíč (stejná priorita jako v `search()`); Luna zůstává zdroj streamů,
        ne metadat. Bez TMDB/Luny zaskočí veřejný katalog Sosáče (u sosac-native id),
        nebo Cinemeta (poslední záchrana, anglicky)."""
        if not is_sosac_id(item_id) and self.tmdb:
            try:
                return self.tmdb.meta(meta_type, item_id)
            except TmdbError:
                pass
        try:
            return self.api_for(item_id).meta(meta_type, item_id)
        except NokturnoError:
            if is_sosac_id(item_id):
                return self.sosac_db.meta(meta_type, item_id)
            if str(item_id).startswith("tt"):
                if self.tmdb:
                    try:
                        return self.tmdb.meta(meta_type, item_id)
                    except TmdbError:
                        pass
                return self.cinemeta.meta(meta_type, item_id)
            raise

    def meta(self, ctype, item_id, series_id=None):
        base_id, season, episode = split_episode_id(item_id)
        if season is not None and series_id:
            base_id = series_id
        meta_type = "series" if season is not None else ctype
        meta = self._meta_for(meta_type, base_id)
        if is_sosac_id(base_id):
            enrich_one(meta, self.luna, self.store, meta_type)
        video = None
        if season is not None:
            video = next((v for v in meta.get("videos") or []
                          if int(v.get("season") or 0) == season and int(v.get("episode") or 0) == episode), None)
        return meta, video

    def episodes(self, series_id, season=None):
        """Epizody seriálu; bez `season` všechny."""
        meta = self._meta_for("series", series_id)
        out = []
        for video in meta.get("videos") or []:
            s = int(video.get("season") or 0)
            if season is not None and s != int(season):
                continue
            out.append({
                "id": video.get("id") or f"{series_id}:{s}:{int(video.get('episode') or 0)}",
                "season": s,
                "episode": int(video.get("episode") or 0),
                "title": video.get("title") or "",
                "thumbnail": video.get("thumbnail") or "",
                "released": video.get("released") or "",
                "description": (video.get("overview") or video.get("description") or "")[:4000],
            })
        out.sort(key=lambda v: (v["season"] == 0, v["season"], v["episode"]))
        return out

    # --- streamy ------------------------------------------------------------

    def _with_local_title(self, ctype, base_id, meta):
        """Doplní do metadat český název podle IMDb id (TMDB přes Lunu, jinak databáze filmů)."""
        kind = ctype if ctype in ("movie", "series") else "movie"
        try:
            title = self.catalog_detail(kind, base_id).get("title") or ""
        except NokturnoError as err:
            _LOGGER.debug("název podle %s: %s", base_id, err)
            return meta
        if not title or names_match(title, meta.get("name") or ""):
            return meta
        return {**meta, "name": title, "_title": title,
                "_orig": meta.get("_orig") or meta.get("name") or ""}

    def _cross_streams(self, ctype, item_id, meta, alt=None, failures=None):
        """Streamy z druhého zdroje pro stejný titul (Luna ↔ Sosáč).

        `failures`, je-li dán, dostane `(zdroj, chyba)` při výpadku — hledá se dál."""
        base_id, season, episode = split_episode_id(item_id)
        if alt and self.sosac and not is_sosac_id(base_id):
            try:
                target = alt if season is None else self.sosac.episode_id(alt, season, episode)
                return self.sosac.streams(ctype, target) if target else []
            except Exception as err:  # noqa: BLE001 – nedostupný Sosáč nesmí shodit výpis
                _LOGGER.warning("cross-search (alt): %s", err)
                if failures is not None:
                    failures.append(("Sosáč", err))
                return []
        title = meta.get("_title") or meta.get("name") or ""
        year = self._year(meta)
        orig = meta.get("_orig") or None
        meta_type = "series" if season is not None else ctype
        try:
            if is_sosac_id(base_id):
                if not self.luna:
                    return []
                cid = "search.movie" if meta_type == "movie" else "search.series"
                for cand in self.luna.catalog(meta_type, cid, search=title)[:10]:
                    if not (names_match(cand.get("name"), title) or (orig and names_match(cand.get("name"), orig))):
                        continue
                    cand_year = self._year(cand)
                    if year and cand_year and abs(year - cand_year) > 1:
                        continue
                    target = cand["id"] if season is None else f"{cand['id']}:{season}:{episode}"
                    return self.luna.streams(meta_type, target)
                return []
            if not self.sosac:
                return []
            match = self.sosac.find_match(meta_type, title, year, orig)
            if not match:
                return []
            # find_match vrací celé meta, ne id — do streams/episode_id patří match["id"]
            target = match["id"] if season is None else self.sosac.episode_id(match["id"], season, episode)
            return self.sosac.streams(ctype, target) if target else []
        except Exception as err:  # noqa: BLE001 – výpadek druhého zdroje nesmí shodit výpis
            _LOGGER.warning("cross-search: %s", err)
            if failures is not None:
                failures.append(("Luna" if is_sosac_id(base_id) else "Sosáč", err))
            return []

    def _describe(self, stream, index):
        """Stream do podoby vhodné pro HA (dashboard, hlasovka, automatizace)."""
        parse_stream(stream)
        # pevné pořadí: zdroj · kvalita · název souboru · zvuk · titulky · velikost
        full = clean_label(stream.get("label") if stream.get("_direct") else stream.get("_ws_name", ""))
        tracks = stream.get("_tracks") or []
        if tracks:
            # přečteno z hlavičky souboru — přebíjí název i metadata zdroje,
            # ty jen hádají (viz past níže o „EN 5.1“ u českého souboru)
            codes = sorted({t.get("lang") for t in tracks if t.get("lang")})
            channels = {t.get("lang"): t.get("channels") for t in tracks if t.get("lang") and t.get("channels")}
        else:
            channels = stream.get("channels") or {}
            # metadata zdroje nemusí sedět na soubor (Luna hlásila „EN 5.1“ u souboru „…_cz_…“),
            # takže jazyk z názvu souboru se přidá k tomu, co uvádí zdroj
            codes = list(stream.get("langs") or [])
            for code in sorted(langs_from_name(full)):
                if code not in codes:
                    codes.append(code)
        langs = [f"{code} {channels[code]:g}" if isinstance(channels.get(code), (int, float)) else
                 f"{code} {channels[code]}" if code in channels else code for code in codes]
        quality = QUALITY_NAMES.get(stream.get("quality_rank") or 0, "")
        if quality and stream.get("_estimated"):
            quality = "~" + quality  # odhad z velikosti, ne údaj ze zdroje
        source = stream.get("_storage") or SOURCE_NAMES.get(stream.get("source"), "")
        size = stream.get("size_gb") or 0
        name = full[:51] + "…" if len(full) > 52 else full
        length_min = round(stream["_length_s"] / 60) if stream.get("_length_s") else 0
        length_str = f"{'~' if stream.get('_length_est') else ''}{length_min // 60}:{length_min % 60:02d}" \
            if length_min >= 60 else (f"{'~' if stream.get('_length_est') else ''}{length_min} min" if length_min else "")
        bitrate_str = f"{'~' if stream.get('_bitrate_est') else ''}{stream['bitrate']:g} Mb/s" if stream.get("bitrate") else ""
        parts = [p for p in (
            source,
            quality,
            name,
            ("zvuk " + " ".join(langs)) if langs else "",
            ("tit. " + " ".join(stream.get("subs") or [])) if stream.get("subs") else "",
            f"{size:.1f} GB" if size else "",
            length_str,
            bitrate_str,
        ) if p]
        return {
            "index": index,
            # Sosáč streamuje z veřejného streamuj.tv, takže jeho odkazy hrají i mimo domácí síť
            "direct": bool(stream.get("_direct")) or bool(stream.get("_ws_url")) or stream.get("source") == "sosac",
            # odkaz, který funguje i mimo domácí síť (přímo z WebShare)
            "ws_url": stream.get("_ws_url", ""),
            "label": "  ·  ".join(parts) or clean_label(stream.get("label") or ""),
            "raw_label": clean_label(stream.get("label") or ""),
            "file": full,  # nezkrácený název souboru — karta ho dává do tooltipu
            "source": source,
            "quality": quality,
            "quality_rank": stream.get("quality_rank") or 0,
            "size_gb": round(size, 2) if size else None,
            "bitrate": stream.get("bitrate") or None,
            # odhad ze stopáže titulu, ne ze skutečné délky streamu (viz _ensure_bitrate)
            "bitrate_est": bool(stream.get("_bitrate_est")),
            "length_min": round(stream["_length_s"] / 60) if stream.get("_length_s") else None,
            "length_est": bool(stream.get("_length_est")),
            "langs": codes,
            "channels": channels,
            # stopy zvuku i s kodekem (z hlavičky souboru nebo z údajů zdroje) —
            # `channels` výš nese jen počet kanálů podle jazyka
            "audio": [{"lang": tr.get("lang") or "", "channels": tr.get("channels") or "",
                       "codec": tr.get("codec") or ""} for tr in tracks],
            "subs": stream.get("subs") or [],
            "url": stream.get("url") or "",
            "subtitles": stream.get("subtitles") or [],
        }

    def original_titles(self, meta, ctype, alt=None):
        """Další názvy titulu pro fulltext: originál ze Sosáče (`_orig`), anglický název
        z Cinemety a český/slovenský z Wikidat.

        Luna originál neposílá, přitom soubory na WebShare se často jmenují originálem
        („Outlander: Blood of My Blood“, „The Matrix“). A naopak bez Luny a TMDB (doplněk
        pro Stremio) je název titulu jen anglický, zatímco soubory jsou česky — bez českého
        názvu z Wikidat je přísný filtr zahazoval všechny („Harry Potter and the Goblet
        of Fire“ × „Harry Potter a Ohnivý pohár 2005 CZ dabing HD“).
        """
        title = meta.get("_title") or meta.get("name") or ""
        # volá se z každého fulltextového zdroje a z titulků — pětkrát za jeden výpis, a při
        # výpadku Wikidat (selhání se necachuje) pětkrát dva dotazy s 10s timeoutem
        memo_key = (ctype, meta.get("id"), alt, title)
        memo = self._orig_memo.get(memo_key)
        if memo and time.time() - memo[0] < memo[2]:
            return list(memo[1])
        wd_ok = True
        names = [meta.get("_orig") or ""]
        if alt and self.sosac:
            try:
                alt_meta = self.sosac.meta(ctype, alt)
                names += [alt_meta.get("_orig") or "", alt_meta.get("_title") or ""]
            except (SosacError, Exception) as err:  # noqa: BLE001 – jen doplňkový zdroj
                _LOGGER.debug("originál z alt %s: %s", alt, err)
        imdb = meta.get("imdb_id") or (meta.get("id") if str(meta.get("id", "")).startswith("tt") else "")
        if imdb:
            def load():
                try:
                    return {"name": _cinemeta(ctype, imdb).get("name") or ""}
                except Exception:  # noqa: BLE001
                    return {"name": ""}
            names.append((self.store.cached(f"cmname:{ctype}:{imdb}", 30 * 86400, load) or {}).get("name", ""))

            def load_local():
                try:
                    return {"ok": True, "names": local_titles(imdb)}
                except Exception as err:  # noqa: BLE001 – jen doplňkový zdroj názvů
                    _LOGGER.debug("Wikidata %s: %s", imdb, err)
                    return {"ok": False, "names": []}
            # výpadek Wikidat se necachuje, jinak by titul měsíc zůstal bez českého názvu
            local = self.store.cached_if(f"wdname:{imdb}", 30 * 86400, load_local, ok=lambda d: d.get("ok"))
            wd_ok = bool((local or {}).get("ok"))
            names += (local or {}).get("names") or []
        out, seen = [], {_fold(title)}
        for name in names:
            key = _fold(name)
            if name and key and key not in seen and not key.isdigit():
                seen.add(key)
                out.append(name)
        if len(self._orig_memo) > 200:
            self._orig_memo.clear()
        # výpadek Wikidat se pamatuje jen krátce — příští výpis to zkusí znovu, ale ten
        # právě běžící už nečeká pětkrát na timeout
        self._orig_memo[memo_key] = (time.time(), list(out), ORIG_MEMO_S if wd_ok else ORIG_MEMO_FAIL_S)
        return out

    def _title_queries(self, meta, video=None, ctype="movie", alt=None, strict=True):
        """Dotazy pro fulltextové zdroje a filtr, který z výsledku nechá jen ten titul.

        Sdílí to WebShare i HellSpy — oba hledají v názvech souborů, takže potřebují
        totéž: víc variant názvu a pak zahodit všechno, co se jen podobá.

        `strict=False` (ruční „Zkusit fulltext“ z karty) vrací k poloze v názvu
        shovívavější filtr — stačí, aby soubor obsahoval všechna slova kdekoli.
        Používá se jen na výslovné vyžádání, kdy uživatel vidí i výsledky, které
        by přísný filtr zahodil (a počítá s tím, že mezi nimi může být i omyl).
        """
        title = meta.get("_title") or meta.get("name") or ""
        origs = self.original_titles(meta, ctype, alt)
        # dotazy jen z prvních variant (originál, anglický, český…) — filtr níž bere všechny
        dotazy_z = origs[:MAX_TITLE_VARIANTS]
        if video:
            episode = f"S{int(video.get('season') or 0):02d}E{int(video.get('episode') or 0):02d}"
            queries = [f"{title} {episode}"] + [f"{o} {episode}" for o in dotazy_z]
        else:
            year = self._year(meta)
            queries = [f"{title} {year}" if year else title, title]
            queries += [f"{o} {year}" if year else o for o in dotazy_z]
        # fulltext WebShare vrací i soubory, které mají společné jen část slov („Krev mé krve" u
        # Hry o trůny i Cizinky) — bereme jen ty, co mají všechna slova názvu (nebo originálu)
        # a u epizody i její číslo (S02E01 / 2x01 / 02x01)
        wanted = [p for p in [_title_pattern(title)] + [_title_pattern(o) for o in origs] if p[0]]
        variants = [group for group, _tail, _short in wanted]
        episode_re = None
        if video:
            se, ep = int(video.get("season") or 0), int(video.get("episode") or 0)
            episode_re = re.compile(rf"s{se:02d}e{ep:02d}|(?<!\d){se:02d}?x{ep:02d}(?!\d)|(?<!\d){se}x{ep:02d}(?!\d)")

        # rok v názvu souboru rozliší stejnojmenné filmy („pět švestek“ 1983 vs. 2026);
        # roky, které patří k názvu titulu („Blade Runner 2049“), se ignorují
        want_year = None if video else self._year(meta)
        title_years = _years(_fold(title) + " " + " ".join(_fold(o) for o in origs))

        def year_ok(folded):
            if not want_year:
                return True
            years = _years(folded) - (title_years - {want_year})
            # soubor bez roku v názvu propustíme, soubor s jiným rokem ne
            return not years or any(abs(y - want_year) <= 1 for y in years)

        def relevant(name):
            folded = _fold(name)
            if wanted:
                if strict:
                    if not any(_title_leads(folded, pattern, not video, variants) for pattern in wanted):
                        return False
                elif not any(all(w in folded for w in group) for group, _tail, _short in wanted):
                    return False
            if not video and EPISODE_ANY_RE.search(folded):
                # u filmu nemá soubor se značkou dílu co dělat. Jednoslovný název
                # („Avatar") projde kontrolou slov a díly seriálu rok v názvu nemají,
                # takže by se do seznamu streamů filmu nasypal celý seriál.
                return False
            if not year_ok(folded):
                return False
            return not episode_re or bool(episode_re.search(folded))

        return [q.strip() for q in dict.fromkeys(queries) if q.strip()], relevant

    def _webshare_streams(self, meta, video=None, ctype="movie", alt=None, strict=True, failures=None):
        """Tytéž soubory přímo z WebShare — jejich odkazy fungují i mimo domácí síť.

        Streamy přes Lunu míří na její lokální adresu (`http://192.168.1.10:7126/…`),
        takže na mobilu mimo LAN nehrají. WebShare vrací odkaz na svoje CDN.
        Hledá se ve víc variantách (s rokem, bez roku, originální název), protože
        jeden dotaz vrátí jen část souborů a nespárované streamy pak zůstanou bez odkazu.
        """
        if not self.ws:
            # účet je, ale přihlášení selhalo (síť, 5xx): je to výpadek zdroje, ne „bez WebShare" —
            # jinak by se seznam bez hlavního zdroje uložil na 72 h (audit 2026-09-19)
            if failures is not None and self._opt("ws_username").strip():
                failures.append(("WebShare", self.ws_error or WebshareError("přihlášení selhalo")))
            return []
        queries, relevant = self._title_queries(meta, video, ctype, alt, strict)
        out, seen = [], set()
        for query in queries:
            self._check_stop()
            try:
                files, _total = self.ws.search(query, limit=WS_LIMIT)
            except WebshareError as err:
                _LOGGER.warning("WebShare hledání „%s“: %s", query, err)
                if failures is not None:
                    failures.append(("WebShare", err))
                continue
            for f in files:
                if f["ident"] in seen or not relevant(f.get("name") or ""):
                    continue
                seen.add(f["ident"])
                # velikost patří do `detail` — odtud ji `parse_stream` čte (v labelu ji nehledá).
                # `size_h` z WebShare je ve stejných jednotkách jako údaj Luny, takže se dvojice najdou.
                out.append({
                    "url": "ws:" + f["ident"],
                    "label": f.get("name") or "",
                    "detail": f.get("size_h") or (human_size(int(f["size"])) if f.get("size") else ""),
                    "source": "ws",
                    "_direct": True,
                })
        return out

    def _probe_in_background(self, urls):
        """Hlavičky do cache (`media:`) bez čekání — výsledek dostane až další výpis.

        Až po hlavním čtení, ať nebere linku tomu, na co se čeká. Hostitel na konci
        skriptu na vlákna počká (Kodi: plugin doběhne i během přehrávání); při vypínání
        se nezačaté přeskočí (`_media_from_file` se ptá `should_stop`)."""
        urls = list(dict.fromkeys(u for u in urls if u))
        if not urls or not self._opt("probe_background", False):
            return
        self.last_timings["hlavičky na pozadí"] = len(urls)
        pool = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
        for url in urls:
            pool.submit(self._media_from_file, url)
        pool.shutdown(wait=False)

    def _media_from_file(self, url):
        """Co se o souboru dá přečíst z jeho hlavičky. Prázdné, když to nejde."""
        def load():
            if self.should_stop():
                return {}   # doběh na pozadí po `PROBE_DEADLINE` — hostitel končí, nezačínat
            try:
                return probe_media(self.resolve(url))
            except Exception as err:  # noqa: BLE001 – čtení hlavičky je bonus, nikdy nesmí shodit výpis
                _LOGGER.debug("hlavička %s: %s", url[:28], err)
                return {}
        # `probe()` při selhání vrací slovník s nulami — ten se nesmí pamatovat 30 dní,
        # jinak stream po jednom timeoutu měsíc nemá zvuk ani rozlišení
        return self.store.cached_if(f"media:{url}", AUDIO_TTL, load,
                                    ok=lambda d: bool(d.get("audio") or d.get("height") or d.get("size"))) or {}

    def _fastshare_unlimited(self):
        """Má účet FastShare neomezené stahování? Přihlášení se pamatuje (`FastshareApi.account`),
        při chybě se bere „ne" — čtení hlavičky je bonus, kvůli němu se kredit riskovat nebude."""
        if self.fs is None:
            return False
        try:
            return bool(self.fs.account().get("unlimited"))
        except Exception as err:  # noqa: BLE001 – síť, špatné heslo
            _LOGGER.debug("FastShare účet: %s", err)
            return False

    def _fill_audio(self, streams, on_tick=None, on_count=None, on_audio_progress=None, background=()):
        """Doplní zvuk, titulky a rozlišení tam, kde je zdroj neřekl, a ověří je
        tam, kde je řekl jen název souboru.

        HellSpy o zvuku ve svém rozhraní nemá vůbec nic a u souborů z fulltextu
        je jen to, co si někdo napsal do názvu. Údaj přitom leží v hlavičce
        souboru a servery umí vydat jen její výřez, takže se čte pár desítek kB.
        Běží to souběžně a výsledek se pamatuje, takže se za soubor platí jednou.

        `on_tick`, je-li dán, se zavolá po každém dočteném souboru — tahle
        část bývá zdaleka nejdelší, takže se na ní zakládá ukazatel průběhu
        (viz `streams()` a `__init__.py`).
        """
        # streamy bez počtu kanálů v názvu jdou první — tam chybí úplně všechno.
        # Streamy, které už jazyk podle názvu mají („CZ Dabing"), se ale taky
        # ověří: uploader se může splést nebo zkopírovat popisek z jiného
        # souboru, takže název sám o sobě není důkaz — jen se čeká, až na ně
        # dojde řada v limitu.
        try:
            limit = int(self._opt("audio_probe", AUDIO_PROBE_MAX) or 0)
        except (TypeError, ValueError):
            limit = AUDIO_PROBE_MAX
        if limit <= 0:
            if on_count:
                on_count(0)
            return streams
        schemes = ("hs:", "ws:", "streamuj:", "dav:")
        # FastShare strhává kredit za přenesená data — hlavičky výpisu stojí desítky MB.
        # Kdo jede na kredit, zvuk nezjišťujeme; s neomezeným stahováním ano.
        if any(str(s.get("url") or "").startswith("fs:") for s in streams) and self._fastshare_unlimited():
            schemes += ("fs:",)
        candidates = [s for s in streams if not s.get("_tracks")
                      and str(s.get("url") or "").startswith(schemes)]
        ordered = sorted(candidates, key=lambda s: bool(s.get("channels")))
        todo = ordered[:limit]
        # `probe_background`: co se nečte teď (nad limit, sloučené verze v `background`),
        # se přečte na pozadí do cache — další otevření titulu i „Zobrazit všechny“
        # pak mají ověřené všechno, bez čekání
        rest = [s["url"] for s in ordered[limit:]] + [
            s["url"] for s in background if not s.get("_tracks") and str(s.get("url") or "").startswith(schemes)]
        # skutečný počet čtených hlaviček bývá výrazně nižší než limit —
        # ukazatel průběhu si podle něj dopočítá reálné 100 %, ne odhad
        if on_count:
            on_count(len(todo))
        if not todo:
            self._probe_in_background(rest)
            return streams
        total = len(todo)
        probed = 0
        if on_audio_progress:
            on_audio_progress(0, total)
        # ne `with ThreadPoolExecutor()`: jeho `__exit__` čeká na všechna vlákna, i když
        # hostitel končí — `gather()` se mezi tím ptá `should_stop()` a při přerušení
        # nezačaté hlavičky zruší (viz `lib/abort.py`)
        pool = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
        futures = {pool.submit(self._media_from_file, s["url"]): s for s in todo}
        results = {}

        def hotovo(future):
            nonlocal probed
            results[id(futures[future])] = future.result()
            if on_tick:
                on_tick()
            probed += 1
            if on_audio_progress:
                on_audio_progress(probed, total)
        gather(pool, list(futures), self.should_stop, on_done=hotovo, deadline=PROBE_DEADLINE)
        self.last_timings["hlaviček nedočteno"] = len(todo) - len(results)
        self._probe_in_background(rest)
        for stream, info in ((s, results.get(id(s))) for s in todo):
            if not info:
                continue
            text = describe_media(info)
            if text:
                stream["detail"] = f"{stream['detail']} | {text}" if stream.get("detail") else text
            stream["_tracks"] = info.get("audio") or []
            stream["_media"] = info
            if info.get("duration"):
                # z hlavičky je i skutečná délka streamu — přesnější základ pro
                # datový tok v `_ensure_bitrate()` než odhad ze stopáže titulu
                stream["_duration"] = info["duration"]
            # parse_stream je idempotentní podle `quality_rank`; po změně popisku
            # se musí přepočítat, jinak by jazyky a kanály zůstaly prázdné
            stream.pop("quality_rank", None)
            parse_stream(stream)
            # rozlišení ze souboru přebíjí název: ten u řady souborů slibuje
            # „4k", a přitom je uvnitř 1080p
            real = quality_from_size(info.get("width") or 0, info.get("height") or 0)
            if real:
                stream["quality_rank"] = {"4K": 4, "Full HD": 3, "HD": 2, "SD": 1}[real]
            if not stream.get("size_gb") and info.get("size"):
                # Sosáč velikost vůbec neříká, server ji ale poslal v Content-Range
                # při stejném dotazu na hlavičku, který se dělal pro zvuk
                stream["size_gb"] = info["size"] / 2 ** 30
        return streams

    WRONG_LENGTH_MIN_S = 80 * 60   # kratší soubor za film pod cizím názvem nepovažujeme
    WRONG_LENGTH_RATIO = 2.0

    def _drop_wrong_length(self, streams, video):
        """Vyřadí soubory, které se za epizodu jen vydávají: název říká `S04E01`,
        ale zdroj u nich hlásí délku filmu (HellSpy: 124 min / 19,7 GB pod názvem
        epizody Reachera, uvnitř Iron Man). Délku posílá zdroj už při hledání
        (`duration` / `_duration`), hlavičku číst netřeba. Bez známé stopáže epizody
        se nezahazuje nic."""
        minutes = runtime_minutes((video or {}).get("runtime"))
        if not minutes:
            return streams
        limit = max(self.WRONG_LENGTH_MIN_S, minutes * 60 * self.WRONG_LENGTH_RATIO)
        kept = [s for s in streams if (s.get("duration") or s.get("_duration") or 0) <= limit]
        if len(kept) != len(streams):
            self.last_timings["špatná délka"] = len(streams) - len(kept)
        return kept

    def _ensure_bitrate(self, streams, meta_or_video):
        """Datový tok a délka má mít úplně každý stream, ne jen ten, co je zdroj sám řekl.

        Přesnost podle toho, odkud se vzala délka. Nejlepší je ta, kterou přímo
        posílá zdroj (`duration`, z popisku Luny). Pak hlavička souboru
        (`_duration`, z `_fill_audio`) — z obojího je datový tok stejně přesný
        jako velikost. Bez nich se počítá se stopáží titulu — odhad, značí se
        vlnovkou stejně jako ostatní odhadnuté věci v popisku.

        AVI hlavičky lžou často — `dwTotalFrames` v `avih` je jeden z nejčastěji
        poškozených nebo neaktualizovaných údajů po přebalení souboru. Soubor
        pak tvrdí, že devadesátiminutový film má 14 minut, a datový tok vyjde
        několikanásobně nadsazený. Když je titul znám, přečtená délka ze
        souboru se proto porovná s jeho stopáží; liší-li se o víc než
        polovinu, nedůvěřuje se jí a použije se odhad ze stopáže titulu.
        """
        minutes = runtime_minutes((meta_or_video or {}).get("runtime"))
        fallback_s = minutes * 60 if minutes else DEFAULT_RUNTIME_S
        for stream in streams:
            duration = stream.get("duration") or stream.get("_duration") or 0
            if duration and minutes and not (0.5 <= duration / fallback_s <= 2.0):
                duration = 0
            length = duration or fallback_s
            stream["_length_s"] = length
            stream["_length_est"] = not duration
            if not stream.get("bitrate") and stream.get("size_gb"):
                stream["bitrate"] = round(stream["size_gb"] * 2 ** 30 * 8 / length / 1_000_000, 1)
                stream["_bitrate_est"] = not duration
        return streams

    def _hellspy_streams(self, meta, video=None, ctype="movie", alt=None, strict=True, failures=None):
        """Tentýž titul na HellSpy. Nabízí se původní soubor, ne překódování, takže
        název i velikost popisují to, co se opravdu přehraje — viz `lib/hellspy_api`."""
        if not self.hs:
            return []
        queries, relevant = self._title_queries(meta, video, ctype, alt, strict)
        out, seen = [], set()
        for query in queries:
            self._check_stop()
            try:
                files, _next = self.hs.search(query, limit=HS_LIMIT)
            except HellspyError as err:
                # pauza po dřívější 429 nic nového neříká — jen první odmítnutí se loguje nahlas
                _LOGGER.log(logging.DEBUG if getattr(err, "paused", False) else logging.WARNING,
                            "HellSpy hledání „%s“: %s", query, err)
                if failures is not None:
                    failures.append(("HellSpy", err))
                if isinstance(err, HellspyRateLimited):
                    break  # IP je omezená — další dotazy by jen prodloužily blokaci
                continue
            for f in files:
                name = f.get("name") or ""
                # Táž nahrávka bývá na HellSpy vícekrát pod prakticky stejným názvem.
                # _fold zahodí cizí písmo úplně, takže se dvě jinak shodná jména liší
                # jen zbylou mezerou — proto se mezery ještě srovnají.
                key = (" ".join(_fold(name).split()), f["size"])
                if f["hash"] in seen or key in seen or not relevant(name):
                    continue
                seen.add(f["hash"])
                seen.add(key)
                out.append({
                    "url": f"hs:{f['id']}:{f['hash']}",
                    "label": name,
                    "detail": f.get("size_h") or "",
                    "source": "hs",
                    "_duration": f.get("duration") or 0,
                    "_direct": True,
                })
        return out

    def _sledujteto_streams(self, meta, video=None, ctype="movie", alt=None, strict=True, failures=None):
        """Tentýž titul na Sledujteto — stejné dotazy i přísný filtr jako WebShare
        a HellSpy (`_title_queries`), jen přes přihlášený účet. Přehrávání chce
        Premium; hledání jde i bez něj, takže se streamy ukážou vždy a případné
        „vyžaduje Premium" se ozve až při přehrání (viz `resolve`)."""
        if not self.st:
            return []
        queries, relevant = self._title_queries(meta, video, ctype, alt, strict)
        out, seen = [], set()
        for query in queries:
            self._check_stop()
            try:
                files, _total = self.st.search(query, limit=ST_LIMIT)
                # diagnostika: kolik výsledků přišlo a kolik prošlo přísným filtrem názvu
                # (názvy jsou veřejné tituly videí, nic z účtu)
                odmitnute = [f.get("name") for f in files if not relevant(f.get("name") or "")]
                _LOGGER.debug("Sledujteto „%s“: %d výsledků, relevantních %d%s", query, len(files),
                              len(files) - len(odmitnute),
                              f", zahozeno např. {odmitnute[:3]}" if odmitnute else "")
            except SledujtetoError as err:
                _LOGGER.warning("Sledujteto hledání „%s“: %s", query, err)
                if failures is not None:
                    failures.append(("Sledujteto", err))
                if err.status in (401, 403):
                    break   # špatný účet — další dotazy by dopadly stejně
                continue
            for f in files:
                name = f.get("name") or ""
                if f["id"] in seen or not relevant(name):
                    continue
                seen.add(f["id"])
                info = f.get("media") or {}
                # technické údaje dává přímo API — jako přečtená hlavička, soubor se číst nemusí
                text = describe_media(info) if info.get("audio") else ""
                real = quality_from_size(info.get("width") or 0, info.get("height") or 0)
                stream = {
                    "url": f"st:{f['id']}",
                    "label": name,
                    "detail": " | ".join(x for x in (f.get("size_h") or "", text) if x),
                    "quality": real or f.get("quality") or "",
                    "source": "st",
                    "subtitles": list(f.get("subtitles") or []),
                    "_duration": f.get("duration") or 0,
                    "_direct": True,
                }
                if info.get("audio") or info.get("height"):
                    stream["_tracks"] = info.get("audio") or []
                    stream["_media"] = info
                out.append(stream)
        return out

    def _fastshare_streams(self, meta, video=None, ctype="movie", alt=None, strict=True, failures=None):
        """Tentýž titul na FastShare — stejné dotazy i přísný filtr jako ostatní fulltext.
        Rozlišení a stopáž posílá hledání; zvuk API neříká, dočte se z hlavičky
        souboru v `_fill_audio` — jen s neomezeným stahováním, na kredit ne (viz `lib/fastshare_api`)."""
        if not self.fs:
            return []
        queries, relevant = self._title_queries(meta, video, ctype, alt, strict)
        out, seen = [], set()
        for query in queries:
            self._check_stop()
            try:
                files, _total = self.fs.search(query, limit=FS_LIMIT)
            except FastshareError as err:
                _LOGGER.warning("FastShare hledání „%s“: %s", query, err)
                if failures is not None:
                    failures.append(("FastShare", err))
                continue
            for f in files:
                name = f.get("name") or ""
                if f["id"] in seen or not relevant(name):
                    continue
                seen.add(f["id"])
                info = f.get("media") or {}
                stream = {
                    "url": fastshare_ref(f),
                    "label": name,
                    "detail": f.get("size_h") or "",
                    "quality": quality_from_size(info.get("width") or 0, info.get("height") or 0) or "",
                    "source": "fs",
                    "_duration": f.get("duration") or 0,
                    "_direct": True,
                }
                out.append(stream)
        return out

    def _cztor_streams(self, meta, video=None, ctype="movie", alt=None, failures=None):
        """Tentýž titul v CZtor. Páruje se přes IMDb id, název a rok jen u titulů bez
        id (viz `lib/cztor_api`). Zvuk, titulky a rozlišení posílá API, takže stream
        je rovnou „ověřený" (`_tracks`, `_media`) a hlavička souboru se nečte."""
        if not self.cz:
            return []
        title = meta.get("_title") or meta.get("name") or ""
        titles = [title] + self.original_titles(meta, ctype, alt)
        imdb = meta.get("imdb_id") or (meta.get("id") if str(meta.get("id", "")).startswith("tt") else "")
        try:
            items = self.cz.find_titles("series" if video else ctype, titles, self._year(meta), imdb=imdb)
            found = []
            for item in items:
                self._check_stop()
                if video:
                    episode_id = self.cz.episode_id(item, video.get("season") or 0, video.get("episode") or 0)
                    if episode_id:
                        found += self.cz.streams("e", episode_id)
                else:
                    found += self.cz.streams("m", item["id"])
        except CztorError as err:
            _LOGGER.warning("CZtor „%s“: %s", title, err)
            if failures is not None:
                failures.append(("CZtor", err))
            return []
        out = []
        for f in found:
            info = f["media"]
            text = describe_media(info)
            label = f["name"]
            if (f.get("hdr") or f.get("dv")) and not stream_hdr({"label": label}):
                label = f"{label} [{'DV' if f.get('dv') else 'HDR'}]"
            out.append({
                "url": f["ref"],
                "label": label,
                "detail": " | ".join(p for p in (f.get("size_h") or "", text) if p),
                "quality": quality_from_size(info.get("width") or 0, info.get("height") or 0) or "",
                "source": "cz",
                "_duration": info.get("duration") or 0,
                "_tracks": info.get("audio") or [],
                "_media": info,
                "_direct": True,
            })
            # rozlišení z API přebíjí název, jako hlavička u ostatních zdrojů v `_fill_audio`:
            # „1080p.UHD.BluRay" by podle názvu vyšlo jako 4K
            parse_stream(out[-1])
            real = quality_from_size(info.get("width") or 0, info.get("height") or 0)
            if real:
                out[-1]["quality_rank"] = {"4K": 4, "Full HD": 3, "HD": 2, "SD": 1}[real]
        return out

    def _storage_streams(self, meta, video=None, ctype="movie", alt=None, strict=True, failures=None):
        """Tentýž titul ve vlastních úložištích — stejný přísný filtr jako fulltext,
        jen se nehledá po síti, ale v zapamatovaném seznamu souborů. Soubor se
        přiřadí i podle složky nad ním (`match_texts`)."""
        if not self.storages:
            return []
        _queries, relevant = self._title_queries(meta, video, ctype, alt, strict)
        out = []
        for api in self.storages:
            self._check_stop()
            try:
                files = api.files()
            except StorageError as err:
                _LOGGER.warning("úložiště %s: %s", api.name, err)
                if failures is not None:
                    failures.append((api.name, err))
                continue
            for f in files:
                if any(relevant(text) for text in match_texts(f["path"])):
                    out.append({
                        "url": f"dav:{api.slot}:{f['path']}",
                        "label": f["name"],
                        "detail": f.get("size_h") or "",
                        "source": "dav",
                        "_storage": api.name,
                        "_direct": True,
                    })
        return out

    def _webshare_subtitles(self, meta, video=None, ctype="movie", alt=None):
        """Titulky k titulu z WebShare (`.srt`), v preferovaném jazyce napřed — `ws:<ident>`.

        Číslo dílu v dotazu je pro fulltext WebShare jen nápověda, ne podmínka: na
        „Outlander: Blood of My Blood S02E01 srt" vrátil titulky k S01E04 a S01E05
        a ty se (2026-09-18) připnuly k prvnímu dílu druhé série, protože filtr
        hlídal jen slova názvu. U epizody proto musí být značka dílu i v názvu
        souboru, stejně jako u streamů (`_fulltext_plan`).
        """
        if not self.ws:
            return []
        title = meta.get("_title") or meta.get("name") or ""
        year = self._year(meta)
        names = [title] + self.original_titles(meta, ctype, alt)
        episode_re = None
        if video:
            se, ep = int(video.get("season") or 0), int(video.get("episode") or 0)
            suffix = f" S{se:02d}E{ep:02d} srt"
            episode_re = re.compile(rf"s{se:02d}e{ep:02d}|(?<!\d){se:02d}?x{ep:02d}(?!\d)|(?<!\d){se}x{ep:02d}(?!\d)")
        else:
            suffix = f" {year} srt" if year else " srt"
        groups = [[w for w in re.split(r"\W+", _fold(n)) if len(w) > 2] for n in names]
        # pořadí jazyků podle předvolby uživatele: CZ → SK, SK → CZ (jako u stop v souboru).
        # Jazyk se pozná jen ze značky v názvu; soubor bez značky patří za označené
        # v preferovaném jazyce, ale před ty ve zjevně cizím.
        pref = self._opt("pref_lang", "")
        ranking = SUBTITLE_FALLBACK.get(pref, ())
        found, seen = [], set()
        for query in [n + suffix for n in names]:
            try:
                root = self.ws._with_token("search", what=query, category="", sort="", limit=40, offset=0)
            except WebshareError as err:
                _LOGGER.debug("WebShare titulky: %s", err)
                continue
            for f in root.findall("file"):
                name, kind, ident = f.findtext("name") or "", (f.findtext("type") or "").lower(), f.findtext("ident")
                if kind != "srt" or ident in seen:
                    continue
                folded = _fold(name)
                if groups and not any(g and all(w in folded for w in g) for g in groups):
                    continue
                if episode_re and not episode_re.search(folded):
                    continue
                seen.add(ident)
                if year and not video and str(year) not in folded:
                    continue
                found.append((_subtitle_rank(folded, ranking), name, ident))
        found.sort()
        return ["ws:" + ident for _rank, _name, ident in found[:SUBS_MAX]]

    def _subtitle_langs(self):
        """Jazyky titulků podle předvolby zvuku, nejžádanější první.

        Bez předvolby se ptáme na češtinu a slovenštinu — doplněk je česko-slovenský
        a cokoli jiného by jen zabralo místo v seznamu (a u stažení kvótu)."""
        pref = self._opt("pref_lang", "")
        return list(SUBTITLE_FALLBACK.get(pref) or ("CZ", "SK"))

    def _opensubtitles_subtitles(self, meta, video=None, ctype="movie"):
        """Titulky z OpenSubtitles k titulu — `os:<file_id>`.

        Hledá se podle **IMDb id**, u seriálu podle rodičovského id a čísla sezóny
        a dílu zvlášť. To je ten podstatný rozdíl proti WebShare: tam je číslo dílu
        jen slovo ve fulltextu a titulky k jinému dílu se musí odfiltrovat regulárem
        (5.2.34), tady je server zná jako údaj a `_epizoda_sedi()` si je navíc ověří
        z `feature_details`. Titul bez IMDb id (Sosáč, vlastní úložiště) se přeskočí —
        podle názvu se tu nehledá schválně, právě aby nemohl přijít cizí díl.

        Vrací jen reference; soubor se stahuje až při přehrání, protože **stažení
        spotřebuje denní kvótu** (5 na IP bez účtu), kdežto hledání je zadarmo.
        """
        api = self.osub
        if api is None:
            return []
        imdb = str(meta.get("imdb_id") or meta.get("id") or "")
        if not imdb.startswith("tt"):
            return []
        season = episode = 0
        if video and ctype != "movie":
            season, episode = int(video.get("season") or 0), int(video.get("episode") or 0)
            if not (season and episode):
                return []   # seriál bez čísla dílu by vrátil titulky k celé sérii
        nalezeno = api.hledej_cachovane(imdb, self._subtitle_langs(), season, episode)
        return ["os:%d" % p["file_id"] for p in nalezeno[:SUBS_MAX] if p.get("file_id")]

    def subtitles_by_hash(self, url, video=None, ctype="movie"):
        """Titulky spárované s **tímhle konkrétním souborem** (otisk OpenSubtitles).

        Otisk je velikost souboru plus součet prvních a posledních 64 kB. Začátek
        čte kvůli hlavičce i `mediainfo.probe()`, takže navíc stojí jediný `Range`
        dotaz na konec. Proto se to nepočítá při výpisu streamů (to by byl dotaz na
        každý řádek), ale až u streamu, který si uživatel vybral — tam se vyplatí:
        titulky spárované otiskem sedí i časově, kdežto podle názvu titulu ne vždy.

        Nikdy nevyhodí výjimku — přehrání nesmí spadnout kvůli titulkům. U velkých
        souborů, které nikdo do OpenSubtitles nenahlásil (typicky české re-uploady
        a 4K remuxy z našich zdrojů), prostě nic nevrátí a zůstanou titulky podle id.
        """
        api = self.osub
        if api is None or not url:
            return []
        try:
            primy = self.resolve(url)
            zacatek, velikost = fetch_sized(primy, length=OSUB_BLOK, opener=self.opener)
            konec, velikost2 = fetch_sized(primy, start=-OSUB_BLOK, opener=self.opener)
            znak = osub_otisk(zacatek, konec, velikost or velikost2)
        except Exception as err:  # noqa: BLE001 – vypršelý odkaz, zdroj bez Range, síť
            _LOGGER.debug("otisk pro titulky %s: %s", url, err)
            return []
        if not znak:
            return []
        season = episode = 0
        if video and ctype != "movie":
            season, episode = int(video.get("season") or 0), int(video.get("episode") or 0)
        nalezeno = api.hledej(jazyky=self._subtitle_langs(), season=season, episode=episode,
                              moviehash=znak)
        return ["os:%d" % p["file_id"] for p in nalezeno if p.get("hash_match") and p.get("file_id")]

    @staticmethod
    def _merge_direct(streams):
        """Tentýž soubor přes Lunu i přímo z WebShare → jedna položka.

        Popis z Luny je bohatší (bitrate, jazyky, kanály), přímý odkaz WebShare zase
        funguje mimo domácí síť a jede z jejich CDN. Necháme tedy popis z Luny
        a přibalíme k němu `_ws_url`; osamocené soubory z WebShare zůstanou zvlášť.
        Velikosti se párují s tolerancí — Luna zaokrouhluje jinak než WebShare.

        Kvalita se u kandidáta na spárování bere jen jako vodítko, ne podmínka:
        WebShare/HellSpy fulltext ji hádá z názvu souboru ("HD"), zatímco Luna
        stejný soubor nezávisle klasifikuje jinak ("Full HD") — u WebShare se
        navíc kvalita opraví podle skutečného rozlišení až později, po tomhle
        párování. O jednu úroveň jinak odhadnutá kvalita proto nesmí párování
        zablokovat, jen se u ní vyžaduje mnohem těsnější shoda velikosti.
        """
        # CZtor je samostatný zdroj s vlastními údaji o souboru — k Luně se nepřibaluje
        direct = [s for s in streams if s.get("_direct") and s.get("source") != "cz" and (s.get("size_gb") or 0) > 0]
        used, out = set(), []
        for stream in streams:
            if stream.get("_direct"):
                continue
            size = stream.get("size_gb") or 0
            if size:
                best, closest = None, None
                for cand in direct:
                    if id(cand) in used:
                        continue
                    rank_diff = abs((cand.get("quality_rank") or 0) - (stream.get("quality_rank") or 0))
                    if rank_diff > 1:
                        continue
                    limit = SIZE_TOLERANCE if rank_diff == 0 else 0.05
                    delta = abs((cand.get("size_gb") or 0) - size)
                    if delta < limit and (closest is None or delta < closest):
                        best, closest = cand, delta
                if best is not None:
                    stream["_ws_url"] = best["url"]
                    # název souboru zná jen WebShare (Luna posílá jen popis) — přebalit do páru
                    stream["_ws_name"] = best.get("label") or ""
                    used.add(id(best))
            out.append(stream)
        # Lunin řádek (`main`) a výsledek Lunina vlastního hledání (`search`) bývají
        # tentýž soubor — Luna název neposílá, takže se poznají jen podle velikosti,
        # stejně jako přímé nálezy výš; každý `main` sloučí nejvýš jednu kopii
        mains = [s for s in out if s.get("source") == "main" and (s.get("size_gb") or 0) > 0]
        searches = [s for s in out if s.get("source") == "search" and (s.get("size_gb") or 0) > 0]
        drop = set()
        for stream in mains:
            best, closest = None, None
            for cand in searches:
                if id(cand) in drop:
                    continue
                rank_diff = abs((cand.get("quality_rank") or 0) - (stream.get("quality_rank") or 0))
                if rank_diff > 1:
                    continue
                limit = SIZE_TOLERANCE if rank_diff == 0 else 0.05
                delta = abs(cand["size_gb"] - stream["size_gb"])
                if delta < limit and (closest is None or delta < closest):
                    best, closest = cand, delta
            if best is not None:
                drop.add(id(best))
                if not stream.get("_ws_url") and best.get("_ws_url"):
                    stream["_ws_url"], stream["_ws_name"] = best["_ws_url"], best.get("_ws_name", "")
        out = [s for s in out if id(s) not in drop]
        solo = [s for s in streams if s.get("_direct") and id(s) not in used]
        solo.sort(key=lambda s: -(s.get("size_gb") or 0))
        # Bez ořezu: dřív tu byl strop 8 osamocených souborů (WebShare i HellSpy
        # dohromady), takže karta HA u titulu bez Luny ukázala zlomek toho, co
        # Kodi (Toy Story 5: 17 proti 64). Kodi žádný strop nemá, pořadí a filtr
        # stejně řeší až `arrange()` podle nastavení.
        merged = out + solo
        # Lunino vlastní "Search" (fulltext přes WebShare uvnitř Luny) umí tentýž
        # soubor vrátit i víckrát — všechny kopie mají stejnou velikost a kvalitu,
        # ale generický popisek bez jména ("(WS) Full HD"), protože Luna sama
        # název souboru neposílá. Spárovat s přímým nálezem výše jde jen jednu
        # (na druhou už nezbyl kandidát) — zbylé nerozeznatelné kopie sloučit do jedné.
        seen, deduped = set(), []
        for stream in merged:
            if stream.get("source") == "search" and not stream.get("_ws_name"):
                key = (stream.get("quality_rank") or 0, round(stream.get("size_gb") or 0, 1))
                if key in seen:
                    continue
                seen.add(key)
            deduped.append(stream)
        # WebShare umí tentýž soubor vrátit i dvakrát fulltextem samotným (jiný
        # dočasný "ws:" odkaz, stejný název i velikost) — sloučit i tohle, radši
        # necháme tu bohatší verzi (zná jazyk zvuku).
        by_name, final = {}, []
        for stream in deduped:
            name = stream.get("label") if stream.get("_direct") else stream.get("_ws_name", "")
            if not name:
                final.append(stream)
                continue
            key = (" ".join(_fold(name).split()), round(stream.get("size_gb") or 0, 1))
            prev = by_name.get(key)
            if prev is None:
                by_name[key] = len(final)
                final.append(stream)
            elif not final[prev].get("langs") and stream.get("langs"):
                final[prev] = stream
        return final

    def _torrent_streams(self, meta, video=None, ctype="movie"):
        """Torrenty z trackerů přes Prowlarr. Poslední možnost, když jinde nic není.

        Zkouší se český i původní název: české trackery pojmenovávají soubory
        obojím a fulltext na jednom z nich často nevrátí nic. U seriálu se
        sezóna a díl vybírají až z výsledků, aby na díl 2x01 nevyskočila
        první sezóna."""
        api = self.prowlarr
        if api is None:
            return []
        titles = []
        for name in (meta.get("_title"), meta.get("name")):
            name = (name or "").strip()
            if name and name not in titles:
                titles.append(name)
        if not titles:
            return []
        season = episode = None
        if video and video.get("season") is not None:
            season = int(video["season"])
            episode = int(video.get("episode") or 0) or None
        year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
        # U seriálu jde do dotazu jen název: trackery hledají fulltextem přes
        # název souboru a značka „S02E01“ v něm dotaz spolehlivě vynuluje
        # (ověřeno na Sk-CzTorrentu). Sezóna a díl se proto vybírají až
        # z výsledků. Rok u seriálu taky ne — v názvu bývá rok sezóny.
        queries = [t if season is not None or not year.isdigit() else f"{t} {year}" for t in titles]
        for query in queries:
            try:
                rows = api.search(query, "series" if season is not None else ctype,
                                  season=season, episode=episode)
            except ProwlarrError as err:  # noqa: BLE001 – výpadek trackerů není chyba titulu
                _LOGGER.warning("torrenty %s: %s", query, err)
                return []
            if rows:
                return rows
        return []

    @staticmethod
    def _describe_torrent(row, index):
        """Torrent do stejného tvaru jako stream, ale s vlastním druhem a bez přehrání."""
        size = row.get("size_gb") or 0
        name = row["title"][:51] + "…" if len(row["title"]) > 52 else row["title"]
        parts = [p for p in (
            "Torrent",
            row.get("quality") or "",
            name,
            f"{row['seeders']} seedů",
            f"{size:.1f} GB" if size else "",
        ) if p]
        return {
            "index": index,
            # torrent není odkaz na video — nedá se přehrát ani poslat do mobilu,
            # jde s ním jen jedno: zařadit do stahování
            "kind": "torrent",
            "direct": False,
            "ws_url": "",
            "label": "  ·  ".join(parts),
            "raw_label": row["title"],
            "file": row["title"],
            "source": "Torrent",
            "tracker": row.get("indexer") or "",
            "seeders": row.get("seeders") or 0,
            "leechers": row.get("leechers") or 0,
            "quality": row.get("quality") or "",
            "quality_rank": 0,
            "size_gb": size or None,
            "bitrate": None,
            "langs": [],
            "channels": {},
            "subs": [],
            "url": row.get("url") or "",
            "subtitles": [],
        }

    def torrents(self, ctype, item_id, series_id=None, offset=0):
        """Torrenty titulu. Hledají se až na vyžádání — trackery odpovídají
        v řádu sekund a u titulu, na který stream je, by to jen zdržovalo."""
        meta, video = self.meta(ctype, item_id, series_id)
        rows = self._torrent_streams(meta, video, ctype)
        return [self._describe_torrent(row, offset + i) for i, row in enumerate(rows)]

    def fulltext_streams(self, ctype, item_id, series_id=None, alt=None, sources=("ws", "hs", "st", "fs")):
        """Ruční, méně přísné hledání na WebShare/HellSpy/Sledujteto/FastShare — na vyžádání z karty.

        Běžné `_webshare_streams`/`_hellspy_streams` filtrují přísně (viz
        `_title_queries`, `strict=True`): jméno souboru musí mít slova názvu
        skoro na začátku, jinak to je jiný titul, který je jen náhodou obsahuje.
        To ale někdy zahodí i skutečnou shodu s neobvyklým názvem souboru.
        Tohle tlačítko pustí uvolněný filtr a nechá posouzení na uživateli —
        proto se výsledek značí `"_loose": True`, ať karta dá najevo, že
        nejde o automaticky ověřenou shodu.
        """
        meta, video = self.meta(ctype, item_id, series_id)
        found = []
        if "ws" in sources:
            found += self._webshare_streams(meta, video, ctype, alt, strict=False)
        if "hs" in sources:
            found += self._hellspy_streams(meta, video, ctype, alt, strict=False)
        if "st" in sources:
            found += self._sledujteto_streams(meta, video, ctype, alt, strict=False)
        if "fs" in sources:
            found += self._fastshare_streams(meta, video, ctype, alt, strict=False)
        for stream in found:
            parse_stream(stream)
        found = self._merge_direct(found)
        for stream in found:
            stream["_loose"] = True
        return found

    # stavy qBittorrentu → co z toho má karta ukázat
    QBIT_STATES = {
        "downloading": "running", "forcedDL": "running", "metaDL": "running",
        "forcedMetaDL": "running", "allocating": "running", "checkingDL": "running",
        "stalledDL": "queued", "queuedDL": "queued", "stoppedDL": "queued", "pausedDL": "queued",
        "error": "error", "missingFiles": "error",
    }

    def torrent_jobs(self):
        """Rozdělané torrenty jako položky fronty stahování.

        Tvar je stejný jako u vlastního stahování, aby je karta uměla vykreslit
        beze změny. Hotové torrenty se nevracejí — ty už leží ve složce a karta
        je ukáže mezi staženými soubory."""
        api = self.qbit
        if api is None:
            return []
        try:
            rows = api.torrents()
        except QbitError as err:  # noqa: BLE001 – klient nemusí běžet, to není chyba integrace
            _LOGGER.debug("qBittorrent: %s", err)
            return []
        out = []
        for t in rows:
            state = self.QBIT_STATES.get(t.get("state") or "")
            if state is None:      # uploading, stalledUP, checkingUP… = staženo
                continue
            size = t.get("size_gb") or 0
            out.append({
                "id": "qb:" + str(t.get("hash") or ""),
                "name": t.get("name") or "",
                "status": state,
                "percent": t.get("progress") or 0,
                "size": int(size * 1073741824),
                "done": int(size * 1073741824 * (t.get("progress") or 0) / 100),
                "speed": t.get("speed") or 0,
                "eta": t.get("eta") if (t.get("eta") or 0) < 8640000 else None,
                "path": t.get("path") or "",
                "error": "",
                "torrent": True,
            })
        return out

    def cancel_torrent(self, torrent_hash):
        api = self.qbit
        if api is None:
            raise NokturnoError("qBittorrent není nastavený.")
        try:
            api.delete(torrent_hash, with_files=True)
        except QbitError as err:
            raise NokturnoError(str(err)) from err
        return True

    def streams_or_torrents(self, ctype, item_id, alt=None, series_id=None):
        """Streamy titulu, a když žádné nejsou, aspoň torrenty.

        Pro hlídání dostupnosti (nové díly, seznam k zhlédnutí): torrent je až
        poslední možnost, ale titul, který leží jen na trackeru, k dispozici je.
        Trackery se ptají jen když streamy nic nevrátily — jinak by každá
        kontrola stála dotazy navíc."""
        found = self.streams(ctype, item_id, alt, series_id)
        if found or self.prowlarr is None:
            return found
        try:
            return self.torrents(ctype, item_id, series_id)
        except Exception as err:  # noqa: BLE001 – tracker nesmí shodit kontrolu
            _LOGGER.debug("torrenty %s: %s", item_id, err)
            return []

    def forget_torrent(self, path):
        """Odebere z qBittorrentu torrent, ze kterého vznikl daný soubor.

        Volá se při mazání staženého filmu. Data si maže integrace sama (i s
        titulky), klientovi torrent jen zmizí ze seznamu — jinak by soubor dál
        seedoval a po smazání hlásil chybějící data. Cesty se porovnávají podle
        názvu souboru srovnaného na jednu podobu: qBittorrent může běžet jinde
        a diakritiku vracet v jiné normalizaci Unicode."""
        api = self.qbit
        if api is None or not path:
            return False
        want = unicodedata.normalize("NFC", os.path.basename(path)).casefold()
        try:
            rows = api.torrents()
        except QbitError as err:
            _LOGGER.debug("qBittorrent: %s", err)
            return False
        for row in rows:
            name = unicodedata.normalize("NFC", os.path.basename(row.get("path") or "")).casefold()
            if name and name == want:
                try:
                    api.delete(row.get("hash"), with_files=False)
                except QbitError as err:
                    _LOGGER.warning("torrent %s nejde odebrat: %s", row.get("name"), err)
                    return False
                return True
        return False

    def download_torrent(self, url, name=""):
        """Předá torrent qBittorrentu. Stažený soubor skončí ve složce stahování."""
        api = self.qbit
        if api is None:
            raise NokturnoError("qBittorrent není nastavený.")
        try:
            if not api.add(url, save_path=self._opt("download_dir", "") or "", rename=name):
                raise NokturnoError("qBittorrent torrent nepřijal.")
        except QbitError as err:
            raise NokturnoError(str(err)) from err
        return True

    def _effective_max_gb(self, meta_or_video):
        """Max. velikost streamu pro TENHLE titul, spočtená z nastaveného
        datového toku (`max_bitrate_mbps`). Velikost souboru sama o sobě
        neříká, jestli přehrávání poteče plynule — rozhoduje datový tok, tedy
        velikost dělená stopáží. Pevné GB proto nedávaly smysl: devadesáti-
        minutová pohádka a tříhodinový epos se stejným tokem vyjdou na jinou
        velikost. Bez známé stopáže (typicky holý fulltext bez metadat) se
        počítá s dvouhodinovým filmem — stejný odhad jako v `_ensure_bitrate`.
        """
        try:
            mbps = float(str(self._opt("max_bitrate_mbps", 0)).replace(",", ".") or 0)
        except ValueError:
            mbps = 0.0
        if not mbps:
            return 0.0
        minutes = runtime_minutes((meta_or_video or {}).get("runtime"))
        seconds = minutes * 60 if minutes else DEFAULT_RUNTIME_S
        return mbps * 1_000_000 * seconds / 8 / 2 ** 30

    # kroků v _fetch_streams(), než začne (obvykle nejdelší) čtení hlaviček
    STREAM_SOURCE_STEPS = 8   # hlavní zdroj + šest dalších + titulky, viz `_fetch_streams`

    def streams(self, ctype, item_id, alt=None, series_id=None, on_progress=None, failures=None):
        """Seřazené streamy titulu ze všech dostupných zdrojů, popsané pro HA/Stremio
        (`_describe`). Doplněk pro Kodi bere surové řádky z `raw_streams()` a popisek
        si skládá sám (barvy, lokalizace)."""
        ordered = self.raw_streams(ctype, item_id, alt, series_id, on_progress, failures)
        return [self._describe(s, i) for i, s in enumerate(ordered)]

    def raw_streams(self, ctype, item_id, alt=None, series_id=None, on_progress=None, failures=None,
                    strict=True, meta_video=None, probe_audio=True, on_source_done=None,
                    on_audio_progress=None):
        """Seřazené streamy titulu ze všech dostupných zdrojů — surové slovníky.

        `probe_audio=False`: vynechá `_fill_audio()` (čtení hlaviček souborů) — pro
        případy, kdy stačí odhad jazyka z popisku/názvu (Sosáč, Luna a `langs_from_name`/
        `subs_from_name` u ostatních zdrojů), ne ověřená zvuková stopa. Používá se pro
        hromadnou klasifikaci (desítky titulů), kde by čtení hlaviček u každého bylo
        neúnosně pomalé. Nejde přes 72h cache (`cache_key` výš) — jinak by takhle
        odlehčený výsledek na 72 h zablokoval opravdové ověření hlaviček v dialogu
        streamů pro tentýž titul.

        Síťové dohledání streamů se cachuje 72 h, ale JEN když něco našlo (`cached_if`) —
        prázdný výsledek by mohl být jen dočasný výpadek zdroje, takže se zkusí znovu
        hned příště. Řazení/filtrování podle uživatelských preferencí (jazyk, velikost,
        pořadí) běží vždy nad čerstvě načtenými daty, aby se projevila okamžitě.

        `on_progress(done, total)`, je-li dán, se volá po každé fázi — synchronně,
        přímo z tohohle (executor) vlákna. Volající (`__init__.py`) si musí sám
        ošetřit bezpečný přechod zpátky na event loop, engine o hass/asyncio nic neví.

        Výpadek jednoho zdroje nezastaví ostatní. `failures`, je-li dán (seznam), dostane
        `(zdroj, chyba)` za každý přeskočený — volající z nich udělá upozornění přes
        `lib/source_errors.summarize`. Výsledek s výpadkem se **necachuje**: jinak by
        streamy vypnuté Luny chyběly 72 h i po jejím návratu.

        `strict=False` = ruční „zkusit uvolněný fulltext“: WebShare/HellSpy/Sledujteto
        s volnějším filtrem názvu (viz `_title_queries`), výsledek značený `_loose`
        a mimo cache. `meta_video`: (meta, video) už načtené volajícím, ať se nečtou dvakrát.

        `on_source_done(label, count)`, je-li dán, se volá po dokončení každého jednotlivého
        zdroje (na rozdíl od `on_progress` ví odkud a kolik) — jen při čerstvém hledání,
        cache hit ho vůbec nespustí. Volající si z toho může postavit průběžný přehled
        „WebShare: 12 · HellSpy: 3…“ místo pouhého procenta.

        `on_audio_progress(done, total)`, je-li dán, se volá při čtení hlaviček souborů
        (`_fill_audio` — zdaleka nejdelší fáze) po každém dočteném, i jednou předem s
        `done=0` a skutečným `total` (obvykle nižším než limit). Na rozdíl od `on_progress`
        (přepočtené na jediné souhrnné procento) jde jen o tuhle fázi — pro „ověřuji 5/12“.
        """
        failures = [] if failures is None else failures
        total = self.STREAM_SOURCE_STEPS + AUDIO_PROBE_MAX
        done = [0]
        # časy fází v sekundách od začátku — `last_timings`, volající je může zalogovat
        started = time.monotonic()
        timings = {"cache": True, "zdroje": {}}
        self.last_timings = timings

        def since(mark=None):
            return round(time.monotonic() - (started if mark is None else mark), 2)

        def tick():
            if not on_progress:
                return
            done[0] = min(done[0] + 1, total)
            on_progress(done[0], total)

        def on_count(n):
            # titul obvykle nemá zdaleka AUDIO_PROBE_MAX streamů k ověření —
            # bez přepočtu by ukazatel skončil vysoko pod 100 % ještě před koncem
            nonlocal total
            timings["hlaviček"] = n
            total = self.STREAM_SOURCE_STEPS + n
            if on_progress:
                on_progress(min(done[0], total), total)

        meta, video = meta_video if meta_video else self.meta(ctype, item_id, series_id)
        base_id = split_episode_id(item_id)[0]
        include_search = bool(self._opt("search_streams", True))

        def _fetch_streams():
            nonlocal meta
            timings["cache"] = False
            self._check_stop()
            primary_label = "Sosáč" if is_sosac_id(base_id) else "Luna"

            def primary():
                try:
                    api = self.api_for(base_id)
                    return api.streams(ctype, item_id, include_search=include_search) if isinstance(api, LunaApi) \
                        else api.streams(ctype, item_id)
                except Exception as err:  # noqa: BLE001 – výpadek zdroje = prázdno, ne chyba služby;
                                           # cross/WebShare/HellSpy to samy doženou
                    # chybějící zdroj (titul z Cinemety, Luna nenastavená) není výpadek — a ani
                    # zpráva do logu: katalog „Nově přidané s CZ dabingem" prochází desítky
                    # kandidátů naráz, takže bez Luny/Sosáče zaplnila tahle jedna hláška skoro
                    # půlku odeslaného kodi.logu a přebila v něm to, kvůli čemu se posílal
                    if isinstance(err, NokturnoError) and "není nastaven" in str(err).lower():
                        _LOGGER.debug("streamy %s: %s", item_id, err)
                    else:
                        _LOGGER.warning("streamy %s: %s", item_id, err)
                        failures.append((primary_label, err))
                    return []

            # Líné klienty (login WebShare) založit ještě tady, v hlavním vlákně, ať se
            # vlákna neperou o `_ws_ready`.
            self.ws, self.hs, self.st, self.fs, self.cz, self.sosac  # noqa: B018 – jen inicializace
            cross = self._cross_streams if self._opt("cross_search", True) else (lambda *a, **k: [])

            def ostatni(m):
                """Zdroje, které hledají podle názvu z `m`, a nakonec titulky z WebShare —
                ty jen s `probe_audio` (hromadná klasifikace katalogu je nikdy nepoužije,
                a byly to necachované dotazy za každou variantu názvu)."""
                ulohy = [
                    ("Luna" if is_sosac_id(base_id) else "Sosáč", lambda: cross(ctype, item_id, m, alt, failures)),
                    ("WebShare", lambda: self._webshare_streams(m, video, ctype, alt, strict, failures)),
                    ("HellSpy", lambda: self._hellspy_streams(m, video, ctype, alt, strict, failures)),
                    ("Sledujteto", lambda: self._sledujteto_streams(m, video, ctype, alt, strict, failures)),
                    ("FastShare", lambda: self._fastshare_streams(m, video, ctype, alt, strict, failures)),
                    ("CZtor", lambda: self._cztor_streams(m, video, ctype, alt, failures)),
                ]
                if probe_audio:
                    ulohy.append((SUBS_TASK, lambda: self._webshare_subtitles(m, video, ctype, alt)))
                    ulohy.append((OSUB_TASK, lambda: self._opensubtitles_subtitles(m, video, ctype)))
                return ulohy

            def bezpecne(label, fetch):
                try:
                    return fetch()
                except Exception as err:  # noqa: BLE001 – ani nečekaná chyba zdroje nesmí shodit ostatní
                    _LOGGER.warning("streamy %s (%s): %s", item_id, label, err)
                    if label not in SUBS_TASKS:   # bez titulků se streamy cachovat smí
                        failures.append((label, err))
                    return []

            def kolo(ulohy):
                """Jedna souběžná dávka: vrátí `{label: výsledek}`. Zdroje jsou nezávislé
                a každý má vlastní timeouty (15–40 s) — za sebou byl studený výpis 8–15
                sériových dotazů. `gather` čeká po vteřinách a ptá se `should_stop()`;
                po vypršení rozpočtu se dál nečeká — opozdilec se počítá jako výpadek
                (výsledek se necachuje) a doběhne si na pozadí.

                Rozpočet `SOURCE_DEADLINE` je společný pro všechna kola a měří se od
                `mark`, ne od začátku tohohle volání — druhé kolo tedy nezačíná znovu
                od nuly."""
                zbytek = SOURCE_DEADLINE - (time.monotonic() - mark)
                # minimum nesmí přerůst samotný rozpočet — jinak by malý `SOURCE_DEADLINE`
                # (test, nebo kdyby ho někdo stáhl) čekání naopak prodloužil
                zbytek = max(min(SOURCE_DEADLINE, MIN_ROUND_DEADLINE), zbytek)
                pool = ThreadPoolExecutor(max_workers=len(ulohy))
                futures = [pool.submit(bezpecne, label, fetch) for label, fetch in ulohy]
                label_by_future = dict(zip(futures, (label for label, _fetch in ulohy)))

                def hotovo(future):
                    label = label_by_future[future]
                    timings["zdroje"][label] = since(mark)
                    tick()
                    if on_source_done and label not in SUBS_TASKS:
                        on_source_done(label, len(future.result()))
                out = {}
                for future in gather(pool, futures, self.should_stop, on_done=hotovo, deadline=zbytek):
                    label = label_by_future[future]
                    if future.done():
                        out[label] = future.result()
                        continue
                    out[label] = []
                    timings["zdroje"][label] = f">{zbytek:.0f}s"
                    if label not in SUBS_TASKS:
                        _LOGGER.info("streamy %s: %s neodpověděl do %.0f s, bere se bez něj", item_id, label,
                                     zbytek)
                        failures.append((label, TimeoutError(f"neodpověděl do {zbytek:.0f} s")))
                return out

            def slozit(vysledky, ulohy):
                """Streamy v pořadí úloh (Luna/Sosáč napřed — na tom stojí párování v _merge_direct)."""
                found = []
                for label, _fetch in ulohy:
                    if label not in SUBS_TASKS:
                        found += vysledky.get(label) or []
                return found

            # Hlavní zdroj (Luna/Sosáč podle id) běží souběžně s ostatními — dřív se na něj
            # čekalo předem (studená Luna ~6 s, Office 2026-09-18), jen kvůli výjimce níž.
            mark = time.monotonic()
            ulohy = ostatni(meta)
            vysledky = kolo([(primary_label, primary)] + ulohy)
            found = vysledky.get(primary_label) or []
            timings["hlavni"] = timings["zdroje"].get(primary_label, 0)
            # titul otevřený jen podle IMDb id (z databáze filmů) má v metadatech mezinárodní
            # přepis („Sunday League…“), pod kterým Sosáč nic nenajde. Když hlavní zdroj nic
            # nevrátil a český název z TMDB se liší, ostatní zdroje hledají znovu s ním —
            # stejný výsledek jako dřív, kdy se na hlavní zdroj čekalo předem.
            if not found and not alt and not is_sosac_id(base_id) and str(base_id).startswith("tt"):
                local = self._with_local_title(ctype, base_id, meta)
                if local is not meta:
                    meta = local
                    timings["znovu česky"] = True
                    self._check_stop()
                    ulohy = ostatni(meta)
                    vysledky = kolo(ulohy)
            timings["souběžně"] = since(mark)
            subs = vysledky.get(SUBS_TASK) or []
            osubs = vysledky.get(OSUB_TASK) or []
            found = found + slozit(vysledky, ulohy)
            for stream in found:
                parse_stream(stream)
                # bez kvality v názvu („Matrix (1999).mkv") by soubor spadl na konec seznamu,
                # i když je podle velikosti zjevně 4K — odhadneme ji, ale přiznaně (~)
                if not stream.get("quality_rank"):
                    guess = estimate_rank(stream.get("size_gb"))
                    if guess:
                        stream["quality_rank"] = guess
                        stream["_estimated"] = True
                if not strict and stream.get("_direct"):
                    stream["_loose"] = True   # uvolněný filtr — uživatel posoudí podle názvu sám
            found = self._merge_direct(found)
            self._check_stop()
            # titulky z WebShare ke streamům, které žádné nemají (Sosáč si posílá svoje)
            if subs:
                for stream in found:
                    if not stream.get("subtitles"):
                        stream["subtitles"] = list(subs)
            # OpenSubtitles až jako poslední záchrana: stažení spotřebuje denní kvótu
            # (5 souborů na IP), takže se přibalí jen tam, kde nejsou ani titulky ze
            # zdroje, ani z WebShare, ani přímo v kontejneru souboru
            if osubs:
                chci = set(self._subtitle_langs())
                for stream in found:
                    if stream.get("subtitles") or (set(stream.get("subs") or []) & chci):
                        continue
                    stream["subtitles"] = list(osubs[:OSUB_MAX_NA_STREAM])
            # `parse_stream()` dává do langs/subs `set` — nejde ho serializovat do JSON
            # cache, tak se tu normalizuje na list (řazení navíc dělá cache stabilní)
            for stream in found:
                if isinstance(stream.get("langs"), set):
                    stream["langs"] = sorted(stream["langs"])
                if isinstance(stream.get("subs"), set):
                    stream["subs"] = sorted(stream["subs"])
            return found

        # „streams2“: seznamy uložené před doplněním českých názvů z Wikidat byly u titulů
        # bez Luny/TMDB ořezané přísným filtrem — nový klíč je jednorázově obnoví
        cache_key = self._streams_cache_key(ctype, item_id, alt)
        # vlastní úložiště mimo 72h cache streamů — nový soubor se má ukázat hned,
        # jak ho uvidí seznam úložiště (ten si drží vlastní hodinovou paměť). S
        # `probe_audio=False` (hromadná klasifikace) se přeskakuje úplně — cizí
        # úložiště titul ze Sosáčova katalogu stejně nerozhodne a při nedostupném
        # NAS/DAV to bez vlastní cache dusí každého jednoho kandidáta zvlášť.
        #
        # Běží souběžně s `_fetch_streams()`, ne až po něm — procházení úložiště
        # po síti (PROPFIND složka po složce, na cache miss) umí trvat déle než
        # všechny ostatní zdroje dohromady, a dřív se na něj čekalo navíc.
        def _run_storage():
            result = []
            if probe_audio:
                try:
                    result = self._storage_streams(meta, video, ctype, alt, failures=failures)
                except Exception as err:  # noqa: BLE001 – úložiště nesmí shodit ostatní zdroje
                    _LOGGER.warning("streamy %s (úložiště): %s", item_id, err)
                    failures.append(("Úložiště", err))
                    result = []
            if on_source_done:
                on_source_done("Vlastní úložiště", len(result))
            return result

        storage_pool = ThreadPoolExecutor(max_workers=1)
        storage_future = storage_pool.submit(_run_storage)
        try:
            if strict and probe_audio:
                found = self.store.cached_if(cache_key, STREAMS_CACHE_TTL, _fetch_streams,
                                             ok=lambda data: bool(data) and not failures,
                                             fresh=bool(self._opt("fresh", False)))
            else:
                found = _fetch_streams()
        except BaseException:
            # přerušení (`Aborted`) i chyba: na průchod úložiště se nečeká — to se
            # přeruší samo mezi vrstvami (`StorageApi._crawl`), vlákno doběhne bez nás
            storage_pool.shutdown(wait=False)
            raise
        # úložiště hlídá `should_stop` samo; tady se jen čeká, ať se dá přerušit i čekání
        mark = time.monotonic()
        local = gather(storage_pool, [storage_future], self.should_stop)[0].result()
        timings["úložiště navíc"] = since(mark)
        for stream in local:
            parse_stream(stream)
            if not stream.get("quality_rank"):
                guess = estimate_rank(stream.get("size_gb"))
                if guess:
                    stream["quality_rank"] = guess
                    stream["_estimated"] = True
            for key in ("langs", "subs"):
                if isinstance(stream.get(key), set):
                    stream[key] = sorted(stream[key])
        found = local + list(found or [])
        # z cache se vrátí rovnou, bez jediného tick() výše — doskočit na konec fáze zdrojů
        if on_progress and done[0] < self.STREAM_SOURCE_STEPS:
            done[0] = self.STREAM_SOURCE_STEPS
            on_progress(done[0], total)
        sort = self._sorter(video or meta)

        # Hlavičky se čtou až po seřazení. Kandidátů bývá víc, než se vyplatí číst,
        # a před seřazením se rozpočet utratil za řádky, které skončí dole; teď padne
        # na začátek seznamu, tedy na to, co má uživatel před očima. Po doplnění
        # kanálů se řadí znovu, protože 5.1 může pořadím pohnout.
        self._check_stop()
        if video:
            found = self._drop_wrong_length(found, video)
        ranked = sort(found)
        if self._opt("merge_streams", False):
            # verze, mezi kterými by uživatel nevybíral, jsou jeden řádek — a hlavičky
            # se pak čtou jen u zástupců (viz `lib/streams.group_streams`)
            ranked = group_streams(ranked)
            timings["sloučeno"] = len(found) - len(ranked)
        mark = time.monotonic()
        alts = [a for st in ranked for a in st.get("_alts") or ()]
        with_audio = self._fill_audio(ranked, tick, on_count, on_audio_progress, background=alts) \
            if probe_audio else ranked
        timings["hlavičky"] = since(mark)
        if probe_audio:
            assume_origin_language(with_audio, (meta or {}).get("country"))
        ordered = self._finish(sort, with_audio, video or meta)
        if on_progress and done[0] < total:
            done[0] = total
            on_progress(done[0], total)
        timings["streamů"] = len(ordered)
        timings["celkem"] = since()
        return ordered

    def _sorter(self, meta_or_video):
        """Řazení a filtr podle předvoleb (jazyk, velikost, pořadí) — funkce nad seznamem streamů."""
        max_gb = self._effective_max_gb(meta_or_video)
        lang = self._opt("pref_lang", "")
        order = self._opt("sort_streams", DEFAULT_SORT)

        def sort(items):
            return arrange(
                items,
                pref_lang=lang if lang in LANGS else "",
                hide_sd=bool(self.options.get("hide_sd")),
                max_size_gb=max_gb,
                order=order if order in SORT_ORDERS else DEFAULT_SORT,
                pref_surround=bool(self.options.get("pref_surround")),
            )
        return sort

    def _finish(self, sort, streams, meta_or_video):
        ordered = sort(self._ensure_bitrate(streams, meta_or_video))
        # vlastní úložiště vždy nahoru — mezi desítkami streamů zdrojů se jinak ztrácí
        return [s for s in ordered if s.get("source") == "dav"] + [s for s in ordered if s.get("source") != "dav"]

    def expand_streams(self, streams, meta_video, on_audio_progress=None):
        """„Zobrazit všechny streamy“: sloučené verze (`_alts`) zpátky každou zvlášť.

        Schované verze hlavičky ještě nemají — dočtou se teď (stejný strop jako při
        hledání, u zástupců jsou už v cache). `meta_video` = (meta, video) titulu."""
        meta, video = meta_video
        sort = self._sorter(video or meta)
        items = sort(expand_groups(list(streams)))
        return self._finish(sort, self._fill_audio(items, on_audio_progress=on_audio_progress), video or meta)

    # co WebShare vrací u nedostupných souborů — hlášky jsou anglické a nic neříkající
    WS_ERRORS = {
        "temporarily unavailable": "WebShare tenhle soubor teď nevydá (bývá to dočasné). "
                                   "Zkus jiný stream ze seznamu.",
        "file not found": "Soubor už na WebShare není. Zkus jiný stream ze seznamu.",
        "file password": "Soubor na WebShare je chráněný heslem.",
    }

    def webshare_link(self, ident):
        if not self.ws:
            raise NokturnoError("WebShare účet není nastavený.")
        try:
            link = self.ws.file_link(ident)
        except WebshareError as err:
            text = str(err).lower()
            for needle, message in self.WS_ERRORS.items():
                if needle in text:
                    raise NokturnoError(message) from err
            raise NokturnoError(f"WebShare: {err}") from err
        if not link:
            raise NokturnoError("WebShare nevrátil odkaz na soubor. Zkus jiný stream ze seznamu.")
        return link

    def external_url(self, url):
        """Odkaz na Lunu přepsaný na adresu dostupnou mimo domácí síť (Tailscale).

        Luna posílá svoji LAN adresu (`http://192.168.1.10:7126/…`), takže na mobilu
        mimo síť nehraje. Odkazy WebShare a Sosáče jsou veřejné a nechávají se být.
        """
        host = str(self._opt("external_host", "")).strip()
        if not host or not url.startswith("http"):
            return url
        luna = urllib.parse.urlsplit(self._opt("luna_url", ""))
        parts = urllib.parse.urlsplit(url)
        if not luna.hostname or parts.hostname != luna.hostname:
            return url
        netloc = host if ":" in host else (f"{host}:{parts.port}" if parts.port else host)
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def resolve(self, url, prefer_external=False):
        """Přímé HTTP URL pro externí přehrávač (Sosáč vrací `streamuj:` odkazy)."""
        if not url:
            raise NokturnoError("Chybí odkaz na stream.")
        if url.startswith("ws:"):
            return self.webshare_link(url[3:])
        if url.startswith("hs:"):
            api = self.hs or HellspyApi(cache=self.store)
            file_id, _sep, file_hash = url[3:].partition(":")
            # odkaz posílá i klient (Stremio `/play/`): bez kontroly by `../` nebo `?` v id
            # měnily cílový endpoint na api.hellspy.to (audit 2026-09-19)
            if not (_HS_ID_RE.match(file_id) and _HS_HASH_RE.match(file_hash)):
                raise NokturnoError("HellSpy: neplatný odkaz.")
            try:
                return api.file_link(file_id, file_hash)
            except HellspyError as err:
                raise NokturnoError(f"HellSpy: {err}") from err
        if url.startswith("os:"):
            # titulky z OpenSubtitles; tohle volání jako jediné spotřebuje denní kvótu,
            # proto se dělá až při přehrávání (`local_subtitles` v Kodi), ne ve výpisu
            api = self.osub
            if api is None:
                raise NokturnoError("OpenSubtitles nejsou nastavené.")
            if not url[3:].isdigit():
                raise NokturnoError("OpenSubtitles: neplatný odkaz.")
            try:
                return api.odkaz(int(url[3:]))
            except OpenSubtitlesError as err:
                raise NokturnoError("OpenSubtitles: %s" % err) from err
        if url.startswith("st:"):
            if self.st is None:
                raise NokturnoError("Účet Sledujteto není nastavený.")
            try:
                return self.st.file_link(url[3:])
            except SledujtetoError as err:
                raise NokturnoError(f"Sledujteto: {err}") from err
        if url.startswith("fs:"):
            # cookie za svislítkem, jako u úložiště — přehrávače mimo Kodi jdou přes `fastshare_request`
            try:
                return self._fastshare_api().kodi_url(url)
            except FastshareError as err:
                raise NokturnoError(f"FastShare: {err}") from err
        if url.startswith("cz:"):
            if self.cz is None:
                raise NokturnoError("CZtor není zapnutý nebo spárovaný.")
            try:
                return self.cz.resolve(url)
            except CztorError as err:
                raise NokturnoError(f"CZtor: {err}") from err
        if url.startswith("dav:"):
            api, path = self.storage_for(url)
            try:
                return api.kodi_url(path)
            except StorageError as err:
                raise NokturnoError(f"Úložiště: {err}") from err
        if url.startswith("streamuj:"):
            sosac = self.sosac
            if sosac is None:
                raise NokturnoError("Účet Streamuj není nastavený.")
            try:
                return sosac.resolve(url)
            except SosacError as err:
                # jako u ostatních zdrojů — volající chytají NokturnoError; syrová SosacError
                # z vypršelého odkazu dřív prošla `_fill_audio` a shodila celý výpis streamů
                raise NokturnoError(f"Sosáč: {err}") from err
        return self.external_url(url) if prefer_external else url

    def find_first(self, ctype, query):
        """První výsledek hledání — pro „pusť X" jedním krokem (hlasovka, skripty)."""
        results = self.search(ctype, query, limit=3)
        if not results:
            raise NokturnoError(f"„{query}“ jsem nenašel.")
        return results[0]

    def best_stream(self, ctype, item_id, alt=None, series_id=None):
        streams = self.streams(ctype, item_id, alt, series_id)
        if not streams:
            raise NokturnoError("Pro tento titul se nenašel žádný stream.")
        return streams[0]
