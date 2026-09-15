"""Jádro integrace — hledání a streamy ve stejných zdrojích jako Kodi doplněk.

Logika odpovídá `default.py` doplňku (sloučení Luna ↔ Sosáč, cross-search, řazení),
ale bez Kodi: volání jsou synchronní a HA je pouští v executoru.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from const import CONF_HS_ENABLED, DEFAULT_SORT, LANGS, SORT_ORDERS
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
from streams import arrange, estimate_rank, fold, langs_from_name, parse_stream
from hellspy_api import HellspyApi, HellspyError
from sledujteto_api import SledujtetoApi, SledujtetoError
from fastshare_api import FastshareApi, FastshareError, make_ref as fastshare_ref
from storage_api import StorageApi, StorageError, match_texts, parse_ref
from wikidata_api import local_titles
from mediainfo import describe as describe_media, probe as probe_media, quality_from_size
from webshare_api import WebshareApi, WebshareError, human_size

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

SOURCE_NAMES = {"main": "Luna", "search": "WebShare", "ws": "WebShare", "sosac": "Sosáč",
                "hs": "HellSpy", "st": "Sledujteto", "fs": "FastShare", "torrent": "Torrent", "dav": "Úložiště"}

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


YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
# číslo dílu hned za názvem („Já, padouch 2", „How.to.Train.Your.Dragon.3") — ne
# zvuk 5.1/7.1, ten má za tečkou jedinou číslici; rok za tečkou („draka.2.2014") dílem je
SEQUEL_AFTER_RE = re.compile(r"[\s._\-:,(\[]*(?:[2-9]|ii|iii|iv|vi|vii|viii)(?![a-z0-9])(?![.,]\d(?!\d))")
# značky, které smí stát kolem krátkého názvu („To", „It") místo dalšího slova
RELEASE_TAGS = frozenset((
    "cz", "sk", "en", "eng", "cze", "czech", "cesky", "dabing", "dab", "dub", "titulky", "tit",
    "cztit", "sktit", "subs", "hd", "fullhd", "uhd", "bluray", "bdrip", "brrip", "webrip", "web",
    "webdl", "dl", "dvdrip", "hdtv", "remux", "hevc", "avc", "film", "movie", "mkv", "avi", "mp4",
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


class Engine:
    """Přístup ke třem zdrojům obsahu pod jedním rozhraním."""

    def __init__(self, options, storage_dir, opener=None, store=None):
        """`opener`: volitelný `urllib.request.OpenerDirector` pro vlastní úložiště —
        veřejná instance jím hlídá, kam se smí připojit (viz `StorageApi`).
        `store`: už otevřené úložiště hostitele (doplněk pro Kodi má jedno pro celý
        plugin) — jinak se otevře nové v `storage_dir`.

        Volby nad rámec účtů a předvoleb (všechny nepovinné): `audio_probe` (kolika
        souborům číst hlavičku), `cross_search` a `search_streams` (dohledání v
        druhém zdroji, Lunino vlastní hledání), `fresh` (cache streamů jen zapisovat,
        ne číst — zahřívání na pozadí)."""
        self.options = dict(options)
        self.store = store or Store(storage_dir)
        self.opener = opener
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
        self._storages = None
        self._tmdb = None
        self._prowlarr = self._qbit = None

    def _opt(self, key, default=""):
        value = self.options.get(key, default)
        return value if value is not None else default

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
                                          cache=self.store, index_store=self.store.index())
        return self._sosac

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
                                                     opener=self.opener))
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
            self._sosac_db = SosacDirect(cache=self.store, index_store=self.store.index())
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
                "storage": bool(self.storages),
                "torrent": self.prowlarr is not None}

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
            if self.tmdb:
                try:
                    luna_metas = self.tmdb.catalog(ctype, "popular", search=query)
                    for m in luna_metas:
                        m["source"] = "tmdb"
                except TmdbError as err:
                    errors.append(("TMDB", err))
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
        if video:
            episode = f"S{int(video.get('season') or 0):02d}E{int(video.get('episode') or 0):02d}"
            queries = [f"{title} {episode}"] + [f"{o} {episode}" for o in origs]
        else:
            year = self._year(meta)
            queries = [f"{title} {year}" if year else title, title]
            queries += [f"{o} {year}" if year else o for o in origs]
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
            return []
        queries, relevant = self._title_queries(meta, video, ctype, alt, strict)
        out, seen = [], set()
        for query in queries:
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

    def _media_from_file(self, url):
        """Co se o souboru dá přečíst z jeho hlavičky. Prázdné, když to nejde."""
        def load():
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

    def _fill_audio(self, streams, on_tick=None, on_count=None):
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
        todo = sorted(candidates, key=lambda s: bool(s.get("channels")))[:limit]
        # skutečný počet čtených hlaviček bývá výrazně nižší než limit —
        # ukazatel průběhu si podle něj dopočítá reálné 100 %, ne odhad
        if on_count:
            on_count(len(todo))
        if not todo:
            return streams
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._media_from_file, s["url"]): s for s in todo}
            results = {}
            for future in as_completed(futures):
                results[id(futures[future])] = future.result()
                if on_tick:
                    on_tick()
        for stream, info in ((s, results[id(s)]) for s in todo):
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
            try:
                files, _next = self.hs.search(query, limit=HS_LIMIT)
            except HellspyError as err:
                _LOGGER.warning("HellSpy hledání „%s“: %s", query, err)
                if failures is not None:
                    failures.append(("HellSpy", err))
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

    def _storage_streams(self, meta, video=None, ctype="movie", alt=None, strict=True, failures=None):
        """Tentýž titul ve vlastních úložištích — stejný přísný filtr jako fulltext,
        jen se nehledá po síti, ale v zapamatovaném seznamu souborů. Soubor se
        přiřadí i podle složky nad ním (`match_texts`)."""
        if not self.storages:
            return []
        _queries, relevant = self._title_queries(meta, video, ctype, alt, strict)
        out = []
        for api in self.storages:
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
        """Titulky k titulu z WebShare (`.srt`), české napřed — `ws:<ident>` jako u streamů."""
        if not self.ws:
            return []
        title = meta.get("_title") or meta.get("name") or ""
        year = self._year(meta)
        names = [title] + self.original_titles(meta, ctype, alt)
        if video:
            suffix = f" S{int(video.get('season') or 0):02d}E{int(video.get('episode') or 0):02d} srt"
        else:
            suffix = f" {year} srt" if year else " srt"
        groups = [[w for w in re.split(r"\W+", _fold(n)) if len(w) > 2] for n in names]
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
                seen.add(ident)
                if year and not video and str(year) not in folded:
                    continue
                czech = bool(re.search(r"(^|[^a-z])(cz|cze|czech|cs)([^a-z]|$)", folded))
                found.append((0 if czech else 1, name, ident))
        found.sort()
        return ["ws:" + ident for _rank, _name, ident in found[:SUBS_MAX]]

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
        direct = [s for s in streams if s.get("_direct") and (s.get("size_gb") or 0) > 0]
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
    STREAM_SOURCE_STEPS = 6

    def streams(self, ctype, item_id, alt=None, series_id=None, on_progress=None, failures=None):
        """Seřazené streamy titulu ze všech dostupných zdrojů, popsané pro HA/Stremio
        (`_describe`). Doplněk pro Kodi bere surové řádky z `raw_streams()` a popisek
        si skládá sám (barvy, lokalizace)."""
        ordered = self.raw_streams(ctype, item_id, alt, series_id, on_progress, failures)
        return [self._describe(s, i) for i, s in enumerate(ordered)]

    def raw_streams(self, ctype, item_id, alt=None, series_id=None, on_progress=None, failures=None,
                    strict=True, meta_video=None, probe_audio=True):
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
        """
        failures = [] if failures is None else failures
        total = self.STREAM_SOURCE_STEPS + AUDIO_PROBE_MAX
        done = [0]

        def tick():
            if not on_progress:
                return
            done[0] = min(done[0] + 1, total)
            on_progress(done[0], total)

        def on_count(n):
            # titul obvykle nemá zdaleka AUDIO_PROBE_MAX streamů k ověření —
            # bez přepočtu by ukazatel skončil vysoko pod 100 % ještě před koncem
            nonlocal total
            total = self.STREAM_SOURCE_STEPS + n
            if on_progress:
                on_progress(min(done[0], total), total)

        meta, video = meta_video if meta_video else self.meta(ctype, item_id, series_id)
        base_id = split_episode_id(item_id)[0]
        include_search = bool(self._opt("search_streams", True))

        def _fetch_streams():
            nonlocal meta
            try:
                api = self.api_for(base_id)
                found = api.streams(ctype, item_id, include_search=include_search) if isinstance(api, LunaApi) \
                    else api.streams(ctype, item_id)
            except Exception as err:  # noqa: BLE001 – výpadek zdroje = prázdno, ne chyba služby;
                                       # cross/WebShare/HellSpy níž to samy doženou
                _LOGGER.warning("streamy %s: %s", item_id, err)
                # chybějící zdroj (titul z Cinemety, Luna nenastavená) není výpadek
                if not (isinstance(err, NokturnoError) and "není nastaven" in str(err).lower()):
                    failures.append(("Sosáč" if is_sosac_id(base_id) else "Luna", err))
                found = []
            tick()
            # titul otevřený jen podle IMDb id (z databáze filmů) má v metadatech mezinárodní přepis
            # („Sunday League…“), pod kterým Sosáč nic nenajde — podstrčíme mu český název z TMDB
            if not found and not alt and not is_sosac_id(base_id) and str(base_id).startswith("tt"):
                meta = self._with_local_title(ctype, base_id, meta)
            # zdroje jsou nezávislé a každý má vlastní timeouty (15–40 s) — za sebou byl studený
            # výpis 8–15 sériových dotazů. Líné klienty (login WebShare) založit ještě tady,
            # v hlavním vlákně, ať se čtyři vlákna neperou o `_ws_ready`.
            self.ws, self.hs, self.st, self.fs, self.sosac  # noqa: B018 – jen inicializace
            cross = self._cross_streams if self._opt("cross_search", True) else (lambda *a, **k: [])
            zdroje = (
                ("Sosáč/Luna", lambda: cross(ctype, item_id, meta, alt, failures)),
                ("WebShare", lambda: self._webshare_streams(meta, video, ctype, alt, strict, failures)),
                ("HellSpy", lambda: self._hellspy_streams(meta, video, ctype, alt, strict, failures)),
                ("Sledujteto", lambda: self._sledujteto_streams(meta, video, ctype, alt, strict, failures)),
                ("FastShare", lambda: self._fastshare_streams(meta, video, ctype, alt, strict, failures)),
            )

            def bezpecne(label, fetch):
                try:
                    return fetch()
                except Exception as err:  # noqa: BLE001 – ani nečekaná chyba zdroje nesmí shodit ostatní
                    _LOGGER.warning("streamy %s (%s): %s", item_id, label, err)
                    failures.append((label, err))
                    return []
            with ThreadPoolExecutor(max_workers=len(zdroje)) as pool:
                futures = [pool.submit(bezpecne, label, fetch) for label, fetch in zdroje]
                for future in as_completed(futures):
                    tick()
                # pořadí zdrojů drží (Luna/Sosáč napřed) — na něm stojí párování v _merge_direct
                for future in futures:
                    found += future.result()
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
            # titulky z WebShare ke streamům, které žádné nemají (Sosáč si posílá svoje)
            subs = self._webshare_subtitles(meta, video, ctype, alt)
            tick()
            if subs:
                for stream in found:
                    if not stream.get("subtitles"):
                        stream["subtitles"] = list(subs)
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
        cache_key = f"streams5:{ctype}:{item_id}:{alt or ''}"   # 5 = oprava filtru (krátké slovo na začátku názvu)
        if strict and probe_audio:
            found = self.store.cached_if(cache_key, STREAMS_CACHE_TTL, _fetch_streams,
                                         ok=lambda data: bool(data) and not failures,
                                         fresh=bool(self._opt("fresh", False)))
        else:
            found = _fetch_streams()
        # vlastní úložiště mimo 72h cache streamů — nový soubor se má ukázat hned,
        # jak ho uvidí seznam úložiště (ten si drží vlastní hodinovou paměť). S
        # `probe_audio=False` (hromadná klasifikace) se přeskakuje úplně — cizí
        # úložiště titul ze Sosáčova katalogu stejně nerozhodne a při nedostupném
        # NAS/DAV to bez vlastní cache dusí každého jednoho kandidáta zvlášť.
        local = []
        if probe_audio:
            try:
                local = self._storage_streams(meta, video, ctype, alt, failures=failures)
            except Exception as err:  # noqa: BLE001 – úložiště nesmí shodit ostatní zdroje
                _LOGGER.warning("streamy %s (úložiště): %s", item_id, err)
                failures.append(("Úložiště", err))
                local = []
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
        max_gb = self._effective_max_gb(video or meta)
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

        # Hlavičky se čtou až po seřazení. Kandidátů bývá víc, než se vyplatí číst,
        # a před seřazením se rozpočet utratil za řádky, které skončí dole; teď padne
        # na začátek seznamu, tedy na to, co má uživatel před očima. Po doplnění
        # kanálů se řadí znovu, protože 5.1 může pořadím pohnout.
        with_audio = self._fill_audio(sort(found), tick, on_count) if probe_audio else sort(found)
        ordered = sort(self._ensure_bitrate(with_audio, video or meta))
        # vlastní úložiště vždy nahoru — mezi desítkami streamů zdrojů se jinak ztrácí
        ordered = [s for s in ordered if s.get("source") == "dav"] + [s for s in ordered if s.get("source") != "dav"]
        if on_progress and done[0] < total:
            done[0] = total
            on_progress(done[0], total)
        return ordered

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
            try:
                return api.file_link(file_id, file_hash)
            except HellspyError as err:
                raise NokturnoError(f"HellSpy: {err}") from err
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
