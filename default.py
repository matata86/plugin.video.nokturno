"""Nokturno — video doplněk pro Kodi (Luna: Absolute Cinema + Sosáč + WebShare).

Zdroje (každý jde vypnout v nastavení):
- Luna: server v LAN, TMDB katalogy + streamy z WebShare přes Lunu
- Sosáč: vlastní katalogy a streamy (Stremio API, jen userId)
- WebShare přímo: hledání souborů a stream přes WebShare API (účet), bez Luny
Hledání prochází zapnuté zdroje; stejný titul z Luny a Sosáče je jen jednou
(Sosáč se přibalí jako `alt`), streamy se dohledají v druhém zdroji.
Do profilu doplňku se ukládá historie hledání, zhlédnuto/rozkoukáno (zapisuje
`service.py`), Můj seznam, snímky titulů pro Pokračovat a fronta stahování.
"""
import base64
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zlib

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))
from luna_api import (LunaApi, LunaError, diagnose as luna_diagnose,  # noqa: E402
                      discover as luna_discover, parse_base_url, parse_token)
from cinemeta_api import CinemetaApi, CinemetaError  # noqa: E402
from tmdb_api import TmdbApi, TmdbError  # noqa: E402
from trend_api import CATALOG_ID as TREND_CATALOG_ID, TrendApi  # noqa: E402
from dash_api import DashApi  # noqa: E402
from sosac_api import SosacError, is_sosac_id as _is_stremio_sosac_id  # noqa: E402
from sosac_direct import EXPORT as SOSAC_EXPORT, SosacDirect, is_direct_id  # noqa: E402
from enrich import enrich, enrich_one, shutdown_pool as release_enrich  # noqa: E402
from hellspy_api import HellspyApi, HellspyError  # noqa: E402
from sledujteto_api import SledujtetoApi, SledujtetoError  # noqa: E402
from fastshare_api import FastshareApi, FastshareError  # noqa: E402
from storage_api import SLOTS as STORAGE_SLOTS, StorageApi, StorageError, parse_ref  # noqa: E402
from store import Store, migrate_profile  # noqa: E402
from source_errors import describe_failure, summarize as summarize_failures  # noqa: E402
from sync import sync_once  # noqa: E402
from streams import estimate_rank, langs_from_name, parse_stream, subs_from_name  # noqa: E402
from qr import encode as qr_encode, to_png as qr_png  # noqa: E402
from remote_setup import SetupServer, parse_order  # noqa: E402
from tracks import FILE_CODES, SUBTITLE_FALLBACK, decode_subtitle, subtitle_format, subtitle_lang  # noqa: E402
from trakt_api import TraktApi, TraktError  # noqa: E402
from webshare_api import SORTS, WebshareApi, WebshareError, human_size  # noqa: E402
import kodi_marks  # noqa: E402 – vedle default.py, ne kopie jádra (čte videodatabázi Kodi)
from engine import AUDIO_PROBE_MAX, DEFAULT_RUNTIME_S, Engine, NokturnoError, runtime_minutes  # noqa: E402
from abort import Aborted  # noqa: E402
from crash import CrashReporter  # noqa: E402

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")


class _KodiLogHandler(logging.Handler):
    """Varování z knihovny (např. `store.py`: selhaný zápis souboru) do kodi.log —
    bez toho by je Python jen tiše pustil na stderr, kam se v Kodi nikdo nedívá."""
    def emit(self, record):
        level = xbmc.LOGERROR if record.levelno >= logging.ERROR else xbmc.LOGWARNING
        xbmc.log(f"[{ADDON_ID}/{record.name}] {self.format(record)}", level)


logging.getLogger().addHandler(_KodiLogHandler())
logging.getLogger().setLevel(logging.WARNING)

# Kodi při `Application.Quit` (nebo když uživatel opustí načítající se složku) čeká,
# až skript doběhne — a dlouhé smyčky jádra dřív běžely dál, dokud neskončily samy
# (Office 2026-09-16: vypnutí přes 2 minuty, zahřívání cache ze služby prohledávalo
# zdroje po titulech). Monitor vzniká hned při importu, ne až při prvním dotazu, ať
# nepropásne požadavek, který přišel dřív. `CANCEL` drží, že se už jednou zjistilo, že Kodi
# končí (modální zrušení hledání `cancelable_search` odstraněno v `5.2.14~beta2`).
MONITOR = xbmc.Monitor()
CANCEL = threading.Event()
QUIT_PROP = "nokturno.quitting"   # stejný literál jako v service.py — služba zachytila System.OnQuit
QUIT_POLL = 0.5                   # s – vlastnost okna se nečte při každé kontrole (vlákna zdrojů se ptají často)
_quit_checked = [0.0]


def should_stop():
    """`should_stop` pro jádro (`Engine`, `StorageApi`, `SosacDirect` — viz `lib/abort.py`):
    Kodi končí, nebo uživatel zrušil hledání dialogem. Jádro pak vyhodí `Aborted`,
    router ji chytí a handle zavře bez hlášky.

    `MONITOR.abortRequested()` sám nestačí: plugin spuštěný přes JSON-RPC (zahřívání
    ze služby, HA) ho při `Application.Quit` uvidí až po zastavení síťových služeb,
    a ty čekají právě na něj (Office 2026-09-16: 37 s, s HTTP dotazem 202 s). Služba
    proto na `System.OnQuit` nastaví `QUIT_PROP` (`service.ServiceMonitor`)."""
    if CANCEL.is_set():
        return True
    if MONITOR.abortRequested():
        CANCEL.set()
        return True
    now = time.time()
    if now - _quit_checked[0] >= QUIT_POLL:
        _quit_checked[0] = now
        if xbmcgui.Window(10000).getProperty(QUIT_PROP):
            CANCEL.set()
            return True
    return False
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ICON = ADDON.getAddonInfo("icon")
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo("path"))
PAGE = 20
WS_PAGE = 40
HS_PAGE = 40
CACHE_TTL = 600
# Luna: katalogy i meta se drží déle než obecných 10 min — zahřívání na pozadí běží
# každé 2,5 h a s 10min TTL zahřívalo do prázdna (menu bylo studené 10 min po každém warm-upu)
LUNA_TTL = 4 * 3600
WARM_PROP = "nokturno.warm"   # služba ji nastaví během zahřívání: API cache jen zapisují, nečtou
SEARCH_TTL = 12 * 3600  # sjednocené s luna_api.SEARCH_TTL / webshare_api.SEARCH_TTL
LANG_CATALOG_TARGET = 30   # kolik položek chceme v každém seznamu (dabing/titulky)
LANG_CATALOG_CAP = 60      # kolik kandidátů nejvýš prozkoumat, i kdyby se cíl nenaplnil
LANG_CATALOG_TTL = 8 * 3600  # rychlost ověřena (30/30 do minuty) — teď už se to smí cachovat
LANG_LOCK_PROP = "nokturno.lang_busy"  # zámek přes okno (sdílené mezi procesy) proti souběžnému přepočtu
LANG_PROGRESS_PROP = "nokturno.lang_progress"  # živý postup přepočtu (viz _build_lang_catalog), sdílený
                                                # stejně jako zámek — kdo na zámek čeká, si z něj přečte,
                                                # jak daleko je proces, co ho drží
LANG_TRIGGER_PROP = "nokturno.lang_trigger"  # žádost o okamžitý přepočet na pozadí (viz lang_catalog_trigger
                                              # tady a lang_trigger_watcher ve service.py) — stejný literál
                                              # v obou souborech, stejně jako WARM_PROP
LANG_LOCK_WAIT = 90    # s – radši počkat na cizí přepočet, než ho spustit podruhé souběžně
LANG_LOCK_POLL = 1     # s – jak často se během čekání kontroluje, jestli zámek zase zmizel
LANG_LOCK_STALE = 8 * 60  # s – nejdelší pozorovaný běh byl pod 4 min (60 kandidátů); zámek starší
                          # než tohle je nejspíš od procesu, co ho Kodi zabilo, ne od živého výpočtu
CZECH_LANGS = {"CZ", "SK"}
SOSAC_TAG = "[COLOR FFE0A040]Sosáč[/COLOR]"
HS_TAG = "[COLOR FFFF8A6B]HellSpy[/COLOR]"
ST_TAG = "[COLOR FF4DD0C0]Sledujteto[/COLOR]"
FS_TAG = "[COLOR FFF2C14E]FastShare[/COLOR]"
WS_TAG = "[COLOR FF60B0FF]WebShare[/COLOR]"
DAV_COLOR = "FFB0E57C"
DAV_TAG = f"[COLOR {DAV_COLOR}]Úložiště[/COLOR]"
LUNA_TAG = "[COLOR FFB39DFF]Luna[/COLOR]"
SOURCE_TAGS = {"main": LUNA_TAG, "search": WS_TAG, "sosac": SOSAC_TAG, "ws": WS_TAG, "hs": HS_TAG, "st": ST_TAG,
               "fs": FS_TAG, "dav": DAV_TAG}
QUALITY_COLORS = {4: "FFE06A60", 3: "FF6FD18A", 2: "FF6FB6F0", 1: "FFA0A0A0"}
# krátce, ať zbyde místo na zbytek řádku: „Full HD" se v úzkém sloupci nevyplatí
QUALITY_NAMES = {4: "4K", 3: "FHD", 2: "HD", 1: "SD"}
LANG_COLORS = {"CZ": "FF7FE07F", "SK": "FF7FE07F", "EN": "FF9A9A9A"}
# EN a jazyky bez vlastní barvy dostanou stejný odstín jako GREY (níž) —
# FFE0E0E0 (skoro bílá) na vybrané položce s bílým podkladem úplně mizelo
GREY = "FF9A9A9A"
WARN_COLOR = "FFE0A040"   # neověřená shoda z ručního fulltextu (viz stream_label)
# databáze filmů (Luna/Cinemeta) vrací žánry anglicky, Sosáč rovnou česky —
# do popisu titulu patří vždy česky, neznámý žánr necháme, jak přišel
GENRES_CS = {
    "Action": "Akční", "Adventure": "Dobrodružný", "Animation": "Animovaný", "Biography": "Životopisný",
    "Comedy": "Komedie", "Crime": "Krimi", "Documentary": "Dokument", "Drama": "Drama", "Family": "Rodinný",
    "Fantasy": "Fantasy", "History": "Historický", "Horror": "Horor", "Music": "Hudební", "Musical": "Muzikál",
    "Mystery": "Mysteriózní", "Romance": "Romantický", "Sci-Fi": "Sci-fi", "Short": "Krátkometrážní",
    "Sport": "Sportovní", "Thriller": "Thriller", "War": "Válečný", "Western": "Western",
}
PLAYING_PROP = "nokturno.playing"
VIEWED_PROP = "nokturno.viewed"   # služba si odsud bere „u titulu se zobrazily streamy“ pro statistiky
SYNC_PROP = "nokturno.sync"      # plugin → služba: synchronizuj hned, ne až za pět minut
FORCE_STATS_PROP = "nokturno.force_stats"   # plugin → služba: aktualizace doplňku, nečekat na SEND_EVERY
USED_PROP = "nokturno.used"    # služba si odsud bere „doplněk byl otevřen“ pro statistiky
PREF_LANGS = ("", "CZ", "SK", "EN", "HU")
STREAM_ORDERS = ("source", "quality", "size_desc", "size_asc")

# přechod z ID plugin.video.luna: data i nastavení ze starého profilu
_old_settings = migrate_profile(PROFILE)
if _old_settings:
    try:
        import xml.etree.ElementTree as _ET
        for el in _ET.parse(_old_settings).getroot().iter("setting"):
            if el.get("id") and (el.text or "") != "":
                ADDON.setSetting(el.get("id"), el.text)
    except Exception as _e:  # noqa: BLE001
        xbmc.log(f"[{ADDON_ID}] migrace nastavení: {_e}", xbmc.LOGWARNING)
STORE = Store(PROFILE)
Errors = (LunaError, CinemetaError, TmdbError, SosacError, WebshareError, HellspyError, SledujtetoError, FastshareError,
          TraktError, StorageError, NokturnoError)

# po aktualizaci doplňku (i downgradu) smazat cache API — jinak by staré verze
# odpovědí (chybějící pole, jiný tvar dat po změně kódu) přežily klidně týdny,
# než by je vytlačilo přirozené vypršení TTL
_ADDON_VERSION = ADDON.getAddonInfo("version")
if STORE.load("cache_version", "") != _ADDON_VERSION:
    STORE.clear_cache()
    STORE.save("cache_version", _ADDON_VERSION)
    # zároveň službě řekni, ať s dalším statistickým hlášením nečeká až SEND_EVERY (6 h) —
    # ať se případná zpráva z dashboardu (např. odpověď na nahlášený log) ukáže co nejdřív
    # po aktualizaci, ne až za pár hodin
    xbmcgui.Window(10000).setProperty(FORCE_STATS_PROP, "1")


def is_sosac_id(item_id):
    """Sosáč napřímo (`sosacd_…`) i starší Stremio režim (`sosac2_…`)."""
    return is_direct_id(item_id) or _is_stremio_sosac_id(item_id)


def L(sid, fallback=""):
    """Nové řetězce se z strings.po načtou až po restartu Kodi — proto záloha v kódu."""
    return ADDON.getLocalizedString(sid) or fallback


def Lf(sid, *args):
    """Lokalizovaný řetězec s %s; když překlad zástupný symbol nemá, jen připojí hodnoty."""
    text = L(sid)
    try:
        return text % args if "%" in text else " ".join([text] + [str(a) for a in args])
    except TypeError:
        return " ".join([text] + [str(a) for a in args])


def setting(key, default=""):
    return ADDON.getSetting(key) or default


def on(key, default="true"):
    return setting(key, default) != "false"


def build_url(**params):
    return BASE_URL + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


def runplugin(**params):
    return f"RunPlugin({build_url(**params)})"


# --- zdroje ---------------------------------------------------------------------

def warming():
    """Běží zahřívání cache ze služby? Pak se API cache jen zapisují, ne čtou — jinak
    zahřátí s TTL rovným intervalu jen ověřilo, že cache je čerstvá, a nic neobnovilo."""
    return xbmcgui.Window(10000).getProperty(WARM_PROP) == "1"


def get_luna():
    if not on("luna_enabled"):
        return None
    raw_token = setting("token")
    token = parse_token(raw_token)
    if not token:
        return None
    base = parse_base_url(raw_token, setting("luna_url", "http://192.168.1.10:7126"))
    return LunaApi(base, token, cache=STORE, cache_ttl=LUNA_TTL, fresh=warming())


def get_sosac():
    if not on("sosac_enabled"):
        return None
    # veřejné JSONy Sosáče + streamuj.tv s účtem Streamuj. Starší cesta přes
    # Stremio rozhraní Sosáče (userId, login k Sosáči) je pryč — katalogy jsou
    # ve veřejných exportech a k přehrání stačí Streamuj.
    su, sp = setting("streamuj_username").strip(), setting("streamuj_password").strip()
    if su and sp:
        return SosacDirect(su, sp, cache=STORE, cache_ttl=CACHE_TTL, index_store=STORE.index(), fresh=warming(),
                           should_stop=should_stop)
    return None


def get_sosac_db():
    """Veřejný katalog Sosáče (žádný účet, žádný přepínač) — vlastní databáze
    filmů a seriálů česky, funguje vždy. `apis["sosac"]` výš zůstává jen pro
    přihlášené přehrávání a stahování; katalog samotný účet nepotřebuje."""
    return SosacDirect(cache=STORE, cache_ttl=CACHE_TTL, index_store=STORE.index(), fresh=warming(),
                       should_stop=should_stop)


def expand_streams(apis, streams, meta, video):
    """„Zobrazit všechny streamy“: sloučené verze zvlášť. Schované verze nemají přečtené
    hlavičky — dočtou se (nejvýš `PROBE_DEADLINE`), ukazatel v rohu jako při hledání."""
    bar = xbmcgui.DialogProgressBG()
    bar.create(L(30000, "Nokturno"), L(30238, "Načítám streamy…"))
    try:
        return engine_of(apis).expand_streams(
            streams, (meta, video), on_audio_progress=lambda done, total: bar.update(int(done * 100 / max(total, 1))))
    finally:
        bar.close()


def alts_param(stream):
    """Náhradní odkazy sloučené verze (`_alts`) do adresy přehrání — viz `resolve_first`."""
    return "|".join(a["url"] for a in stream.get("_alts") or [] if a.get("url")) or None


def resolve_first(apis, urls):
    """Rozklíčovat první odkaz, který jde — sloučené verze jsou tentýž film jinde. Vrátí
    (reference, odkaz); když nejde žádný, vyhodí chybu toho prvního."""
    first = None
    for url in urls:
        try:
            return url, resolve_url(apis, url)
        except Errors as e:
            xbmc.log(f"[{ADDON_ID}] stream nejde přehrát, zkouším další verzi: {e}", xbmc.LOGINFO)
            first = first or e
    raise first or NokturnoError(L(30102))


def resolve_url(apis, url):
    """'streamuj:…' (Sosáč), 'ws:<ident>' (WebShare), 'hs:', 'st:' a 'dav:' se mění na
    finální odkaz až při přehrání — odkazy zdrojů vyprší po pár hodinách. Rozklíčování
    dělá jádro (`Engine.resolve`); tady navíc přežije token WebShare mezi voláními."""
    if url and url.startswith("streamuj:") and not isinstance(apis.get("sosac"), SosacDirect):
        # Sosáč v nastavení vypnutý, ale účet Streamuj vyplněný — rozkoukaný titul z dřívějška
        return SosacDirect(setting("streamuj_username"), setting("streamuj_password"), cache=STORE).resolve(url)
    engine = engine_of(apis)
    link = engine.resolve(url)
    if url and url.startswith("ws:"):
        remember_ws_token(engine.ws)
    return link


def get_webshare():
    if not on("ws_enabled", "false"):
        return None
    user, pw = setting("ws_username").strip(), setting("ws_password").strip()
    if not user or not pw:
        return None
    # token WebShare přežije mezi voláními pluginu – šetří login
    return WebshareApi(user, pw, token=xbmcgui.Window(10000).getProperty("nokturno.ws_token"), cache=STORE)


def get_hellspy():
    """HellSpy nemá účet ani token — stačí přepínač v nastavení."""
    if not on("hs_enabled", "false"):
        return None
    return HellspyApi(cache=STORE)


def get_sledujteto():
    """Sledujteto — přihlášený účet. Token si klient drží v úložišti doplňku,
    heslo se posílá jen při přihlášení. Přehrávat jde jen s Premium."""
    if not on("st_enabled", "false"):
        return None
    email, pw = setting("st_email").strip(), setting("st_password")
    if not email or not pw:
        return None
    return SledujtetoApi(email, pw, cache=STORE)


def get_fastshare():
    """FastShare — jméno a heslo. Hledá se bez přihlášení, k přehrání se přihlásí
    a soubor jde z kreditu účtu (nebo neomezeného tarifu)."""
    if not on("fs_enabled", "false"):
        return None
    user, pw = setting("fs_username").strip(), setting("fs_password")
    if not user or not pw:
        return None
    return FastshareApi(user, pw, cache=STORE)


def get_storages():
    """Vlastní úložiště z nastavení (až tři). Síť se tu nevolá — seznam souborů
    se načte až při prvním hledání a hodinu se pamatuje (`storage_api.INDEX_TTL`)."""
    out = []
    for slot in range(1, STORAGE_SLOTS + 1):
        url = setting(f"dav{slot}_url").strip()
        if not url:
            continue
        try:
            out.append(StorageApi(url, setting(f"dav{slot}_username"), setting(f"dav{slot}_password"),
                                  setting(f"dav{slot}_name"), slot=slot, cache=STORE, should_stop=should_stop))
        except StorageError as e:
            log_error(f"úložiště {slot}: {e}")
    return out


def storage_for(apis, url):
    """`dav:<slot>:<cesta>` → (úložiště, cesta); server se bere z nastavení, ne z odkazu."""
    slot, path = parse_ref(url)
    api = next((s for s in apis.get("dav") or [] if s.slot == slot), None)
    if api is None:
        raise StorageError(L(30104))
    return api, path


def remember_ws_token(api):
    if api and api.token:
        xbmcgui.Window(10000).setProperty("nokturno.ws_token", api.token)


def get_trakt():
    if not on("trakt_enabled", "false"):
        return None
    api = TraktApi(setting("trakt_client_id"), setting("trakt_client_secret"), tokens=STORE.trakt(),
                   on_tokens=STORE.set_trakt)
    return api


def get_cinemeta():
    """Vlastní databáze filmů a seriálů (Stremio/Cinemeta) — bez účtu, funguje vždy,
    i když Luna nebo Sosáč nejsou dostupné. Katalog i hledání se na ni spolehnou,
    kdykoli hlavní zdroj chybí (viz `_search_merge`, `main_menu`)."""
    return CinemetaApi(cache=STORE)


def get_trend():
    """Žebříček z vlastních statistik Nokturna (dashboard) — na rozdíl od ostatních
    zdrojů výš nepotřebuje účet ani klíč, funguje vždycky stejně jako Cinemeta."""
    return TrendApi(cache=STORE)


def get_dash():
    """Obsah řízený dashboardem (katalogy, podobné tituly, TV program) — bez účtu,
    s krátkým timeoutem a zálohou z cache, výpadek dashboardu menu nezdrží."""
    return DashApi(cache=STORE)


def get_tmdb():
    """Vlastní klíč uživatele (zdarma, viz nápověda v nastavení) — přednostní
    náhrada za veřejný katalog Sosáče/Cinemetu, když Luna neběží: umí česky
    i to, co ony ne (popis, obsazení). Bez klíče se prostě nepoužije."""
    key = setting("tmdb_api_key").strip()
    return TmdbApi(key, cache=STORE) if key else None


def engine_options():
    """Nastavení doplňku ve tvaru, kterému rozumí jádro: výběry z indexu na hodnotu,
    přepínače na bool, účty jen když je zdroj zapnutý (klienty ale staví `KodiEngine`
    z `get_*()` výš, tady jsou hlavně předvolby řazení a chování)."""
    return {
        "pref_lang": PREF_LANGS[int(setting("pref_lang", "0"))],
        "sort_streams": STREAM_ORDERS[int(setting("sort_streams", "0"))],
        "hide_sd": on("hide_sd", "false"),
        "pref_surround": on("pref_surround", "false"),
        "max_bitrate_mbps": setting("max_bitrate_mbps", "0"),
        "audio_probe": setting("audio_probe", str(AUDIO_PROBE_MAX)),
        "cross_search": on("cross_search"),
        "search_streams": on("search_streams"),
        "merge_streams": True,   # verze, mezi kterými se nevybírá, jako jeden řádek (`group_streams`)
        "probe_background": True,   # hlavičky nad limit a sloučených verzí dočíst na pozadí do cache
        "fresh": warming(),
        "luna_url": setting("luna_url", "http://192.168.1.10:7126"),
        "ws_username": setting("ws_username") if on("ws_enabled", "false") else "",
        "st_email": setting("st_email") if on("st_enabled", "false") else "",
        "fs_username": setting("fs_username") if on("fs_enabled", "false") else "",
        "hs_enabled": on("hs_enabled", "false"),
        "tmdb_api_key": setting("tmdb_api_key"),
    }


class KodiEngine(Engine):
    """Sdílené jádro nad nastavením doplňku.

    Klienty zdrojů staví z `get_luna()` a spol., ne z holých voleb jako HA a Stremio:
    Kodi má u každého zdroje přepínač, zahřívání na pozadí (`warming()` → cache jen
    zapisovat), delší TTL Luny a token WebShare v okně mezi voláními pluginu. Hledání
    streamů, párování duplicit, čtení hlaviček, dotazy pro fulltext i rozklíčování
    odkazů jsou už jen v jádru — dřív to samé leželo podruhé tady v `default.py`.
    """

    FACTORIES = {"luna": get_luna, "sosac": get_sosac, "sosac_db": get_sosac_db, "ws": get_webshare,
                 "hs": get_hellspy, "st": get_sledujteto, "fs": get_fastshare, "storages": get_storages, "tmdb": get_tmdb,
                 "cinemeta": get_cinemeta, "trend": get_trend, "dash": get_dash}

    def __init__(self):
        self._clients = {}
        super().__init__(engine_options(), PROFILE, store=STORE, should_stop=should_stop)

    def _client(self, name):
        if name not in self._clients:
            self._clients[name] = self.FACTORIES[name]()
        return self._clients[name]

    luna = property(lambda self: self._client("luna"))
    sosac = property(lambda self: self._client("sosac"))
    sosac_db = property(lambda self: self._client("sosac_db"))
    ws = property(lambda self: self._client("ws"))
    hs = property(lambda self: self._client("hs"))
    st = property(lambda self: self._client("st"))
    fs = property(lambda self: self._client("fs"))
    storages = property(lambda self: self._client("storages"))
    tmdb = property(lambda self: self._client("tmdb"))
    cinemeta = property(lambda self: self._client("cinemeta"))
    trend = property(lambda self: self._client("trend"))
    dash = property(lambda self: self._client("dash"))


def get_apis():
    """Klienty zdrojů pod jmény, na která je zvyklý zbytek doplňku, plus jádro pod `engine`."""
    engine = KodiEngine()
    return {"engine": engine, "luna": engine.luna, "sosac": engine.sosac, "ws": engine.ws, "hs": engine.hs,
            "st": engine.st, "fs": engine.fs, "dav": engine.storages, "cinemeta": engine.cinemeta, "sosac_db": engine.sosac_db,
            "tmdb": engine.tmdb, "trend": engine.trend, "dash": engine.dash}


def engine_of(apis):
    """Jádro k `apis` z `get_apis()`; holý slovník (testy, starší volání) dostane vlastní."""
    engine = apis.get("engine")
    if engine is None:
        engine = apis["engine"] = KodiEngine()
    return engine


def source_for(item_id):
    return "sosac" if is_sosac_id(item_id) else "luna"


def api_for(apis, item_id):
    api = apis[source_for(item_id)]
    if api is None:
        raise LunaError(L(30104))
    return api


def notify(msg, kind=xbmcgui.NOTIFICATION_INFO, ms=4000):
    xbmcgui.Dialog().notification(L(30000), msg, kind, ms)


def log_error(err):
    xbmc.log(f"[{ADDON_ID}] {err}", xbmc.LOGERROR)


SOURCE_LABELS = {
    LunaError: "Luna", CinemetaError: "Cinemeta", TmdbError: "TMDB",
    SosacError: "Sosáč", WebshareError: "WebShare", HellspyError: "HellSpy", SledujtetoError: "Sledujteto",
    FastshareError: "FastShare", StorageError: L(30405, "Úložiště"),
    TraktError: "Trakt.tv",
}


def describe_error(e):
    """Jméno zdroje před chybovou hláškou — ať je jasné, který přesně selhal
    (dřív se u víc-zdrojového hledání hlásilo natvrdo „Server Luna neodpovídá“
    i při chybě jinde, třeba na WebShare). Hlášky jádra už zdroj nesou samy."""
    if isinstance(e, NokturnoError):
        return str(e)
    return f"{error_label(e)}: {e}"


def error_label(e):
    if isinstance(e, NokturnoError):
        # „WebShare: …“, „Úložiště: …“ — zdroj je v hlášce, jinak je to obecná chyba jádra
        head, sep, _rest = str(e).partition(":")
        return head.strip() if sep and len(head) <= 20 else L(30000, "Nokturno")
    return getattr(e, "source_label", None) or next(
        (v for k, v in SOURCE_LABELS.items() if isinstance(e, k)), type(e).__name__)


class SourceFailure(Exception):
    """Neočekávaná chyba zdroje mimo jeho vlastní typ výjimky — nese jméno zdroje."""

    def __init__(self, label, err):
        super().__init__(str(err))
        self.source_label = label


def skipped_notice(errors):
    """Upozornění, že se zdroj přeskočil a výsledky jsou z ostatních — bez adres
    a tokenů, ty jsou jen v logu (viz `lib/source_errors.py`)."""
    lines = summarize_failures((error_label(e), e) for e in errors)
    return f"{'; '.join(lines)} — {L(30366, 'přeskočeno')}"


def describe_errors(errors):
    lines, seen = [], set()
    for e in errors:
        line = describe_error(e)
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines)


# --- položky ------------------------------------------------------------------

CONTENT_SORTS = {
    "movies": (xbmcplugin.SORT_METHOD_VIDEO_YEAR, xbmcplugin.SORT_METHOD_VIDEO_RATING),
    "tvshows": (xbmcplugin.SORT_METHOD_VIDEO_YEAR, xbmcplugin.SORT_METHOD_VIDEO_RATING),
    "episodes": (xbmcplugin.SORT_METHOD_EPISODE,),
}


LABEL2_MASKS = {
    xbmcplugin.SORT_METHOD_VIDEO_YEAR: "%Y",
    xbmcplugin.SORT_METHOD_VIDEO_RATING: "%R",
}
# druhý sloupec u výchozího řazení (a podle názvu) — rok u titulů, u ostatního Kodi výchozí
DEFAULT_LABEL2 = {"movies": "%Y", "tvshows": "%Y"}


def set_content(content):
    """Typ obsahu + nabídka řazení. Bez `addSortMethod` skiny ukazovaly „Řazení: žádné“
    a katalog nešel seřadit podle roku ani hodnocení, i když je `fill_info` plní.
    První je „jak přišlo“ — pořadí ze zdroje (žebříček, seřazené streamy) zůstává výchozí.

    Masky výslovně, Kodi (`ModuleXbmcplugin.cpp::addSortMethod`) jinak dosadí popisek `%T`
    a druhý sloupec `%D`:
    - `%T` ukázal místo našeho popisku titul z info tagu — katalog a hledání tak měly
      „Matrix“ bez roku, Můj seznam „Matrix (1999)“ (beta16),
    - `%D` = stopáž. Arctic Fuse (`Label_MediaList_Year`) kreslí vpravo Label2, a když je
      prázdný, rok — takže titul se stopáží (Můj seznam, snímek ji nese od bety 20) měl
      vpravo délku, titul bez ní (žebříček, `/trending` stopáž neposílá) rok. U filmů
      a seriálů proto vždy `%Y`, stejně všude (2026-09-16, nahlásil uživatel)."""
    xbmcplugin.setContent(HANDLE, content)
    label2 = DEFAULT_LABEL2.get(content, "")
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED, "%L", label2)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL_IGNORE_THE, "%L", label2)
    for method in CONTENT_SORTS.get(content, ()):
        xbmcplugin.addSortMethod(HANDLE, method, "%L", LABEL2_MASKS.get(method, label2))


# ikony, které smí poslat dashboard (`dash_api.ICONS`) → obrázky ze sady Kodi / doplňku
DASH_ICONS = {
    "": "DefaultVideoPlaylists.png", "movies": "DefaultMovies.png", "series": "DefaultTVShows.png",
    "star": "DefaultFavourites.png", "top": "DefaultMusicTop100.png", "new": "DefaultRecentlyAddedMovies.png",
    "family": "DefaultAddonVideo.png", "christmas": os.path.join(ADDON.getAddonInfo("path"), "resources", "media", "icon-vanoce.png"),
    "halloween": "DefaultAddonVideo.png", "calendar": "DefaultYear.png", "trophy": "DefaultMusicTop100.png",
}


def dash_catalog_items(apis, placement, ctype=None):
    """Katalogy zapnuté na dashboardu pro dané umístění — jako složky menu."""
    dash = apis.get("dash")
    if dash is None:
        return
    for e in dash.menu(placement=placement, ctype=ctype):
        dash_entry_item(e)


def dash_entry_item(entry):
    """Jedna položka menu z dashboardu: složka s podkategoriemi (`children`) vede na
    další výpis, obyčejný katalog rovnou na tituly."""
    icon = DASH_ICONS.get(entry["icon"], DASH_ICONS[""])
    if entry.get("children"):
        folder_item(entry["title"], build_url(action="dash_group", catalog=entry["slug"], type=entry["kind"]),
                    icon=icon)
    else:
        folder_item(entry["title"], build_url(action="catalog", type=entry["kind"], catalog=entry["slug"],
                                              src="dash"), icon=icon)


def list_dash_group(apis, slug, ctype):
    """Podkategorie složky z dashboardu. Když složka mezitím zmizela nebo podkategorie
    ztratila (server posílá jen neprázdné), otevře se rovnou jako katalog — server u
    složky vrátí slité položky potomků, takže uživatel neskončí v prázdném výpisu."""
    dash = apis.get("dash")
    children = dash.group(slug) if dash else []
    if not children:
        list_catalog(apis, ctype, slug, "dash")
        return
    for entry in children:
        dash_entry_item(entry)
    xbmcplugin.endOfDirectory(HANDLE)


IMDB_ID_RE = re.compile(r"^tt\d{5,10}$")


def similar_context(ctype, item_id):
    """„Podobné tituly“ v kontextovém menu — jen u titulů s IMDb id (TMDB je jinak nenajde)."""
    if not IMDB_ID_RE.match(str(item_id or "")):
        return []
    url = build_url(action="similar", type=ctype, id=item_id)
    # ve výpisu Nokturna jen přepnout obsah, z widgetu/domovské obrazovky otevřít okno Videa
    cmd = f"Container.Update({url})" if browsing_nokturno() else f"ActivateWindow(Videos,{url},return)"
    return [(L(30482, "Podobné tituly"), cmd)]


def folder_item(label, url, icon=None, context=None):
    li = xbmcgui.ListItem(label=label)
    li.setArt({"icon": icon or ICON, "thumb": icon or ICON})
    if context:
        li.addContextMenuItems(context)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)


def bare_title(meta):
    """Titul bez roku – u Sosáče jen titul bez jazyků a originálu."""
    if is_sosac_id(meta.get("id")):
        return meta.get("_title") or meta.get("name") or ""
    return meta.get("name") or meta.get("id") or ""


# Cinemeta/TMDB u epizod bez vlastního (přeloženého) názvu vrací místo prázdné
# hodnoty doslovný placeholder "Episode 3" (nebo v češtině "3. epizoda"/"3. díl") –
# jako `or` fallback ho nic nechytí, je to neprázdný řetězec. Do statistik tak šlo
# "3. epizoda" místo názvu seriálu – různé seriály se stejným generickým placeholderem
# u první epizody (`n:series:<rok>:1 epizoda`) se pak slily do jednoho falešného
# item_key (`db.canonical_key`) a dashboard/TMDB takový "titul" nedohledá (vypadne
# z veřejného žebříčku trendů, viz Dashboard/backend/stats.py::_resolve_for_client).
_EPISODE_PLACEHOLDER_RE = re.compile(r"^(episode\s+\d+|\d+\.?\s*(epizoda|díl))$", re.IGNORECASE)


def episode_stats_title(video, meta):
    """Titul epizody do statistik – `video["title"]`, ale jen když není generický
    placeholder (viz výše); jinak název seriálu jako u filmu/seriálu bez epizody."""
    t = (video or {}).get("title")
    if t and not _EPISODE_PLACEHOLDER_RE.match(t):
        return t
    return bare_title(meta)


def display_name(meta):
    """Název do seznamu — bez roku; u Sosáče jen titul bez jazyků a originálu.

    Do 5.2.7~beta21 „Matrix (1999)“. Rok ale od bety 16/20 kreslí každý seznam zvlášť
    (Label2 `%Y`, info tag), takže v názvu byl dvakrát (2026-09-16, přání uživatele)."""
    return bare_title(meta)


_TITLE_YEAR_RE = re.compile(r"\s*\((\d{4})\)$")


def strip_year(title, year=None):
    """Snímky uložené do bety 21 mají v názvu „ (1999)“ — při kreslení ho uřízne, jen když
    sedí s rokem snímku (nebo rok neznáme), ať nepřijde o závorku, která k názvu patří."""
    title = str(title or "")
    m = _TITLE_YEAR_RE.search(title)
    if m and (not str(year or "").isdigit() or m.group(1) == str(year)[:4]):
        return title[:m.start()]
    return title


def split_episode_id(item_id):
    """'tt0903747:1:2' / 'sosac2_21849:1:2' → (základ, sezóna, epizoda) nebo (id, None, None)."""
    parts = str(item_id).split(":")
    if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
        return ":".join(parts[:-2]), int(parts[-2]), int(parts[-1])
    return item_id, None, None


def snapshot(meta, ctype, video=None, series_id=None, alt=None):
    """Snímek titulu pro seznamy bez dotazu na API (Pokračovat, Můj seznam, Naposledy)."""
    return {
        "type": "series" if (video or ctype == "series") else "movie",
        "id": (video or {}).get("id") or meta.get("id"),
        "series": series_id or (meta.get("id") if video else None),
        "alt": alt,
        "title": (video or {}).get("title") or display_name(meta),
        "tvshow": (meta.get("_title") or meta.get("name")) if (video or ctype == "series") else "",
        "season": (video or {}).get("season"),
        "episode": (video or {}).get("episode"),
        "plot": (video or {}).get("overview") or meta.get("description") or "",
        "year": str(meta.get("year") or meta.get("releaseInfo") or "")[:4],
        "art": art_for(meta, video),
        # totéž, co kreslí `fill_info` v katalogu — bez toho měl Můj seznam/Pokračovat jen
        # název a popis, žádné hvězdičky, žánr ani stopáž (2026-09-16, nahlásil uživatel)
        "rating": meta.get("imdbRating") or "",
        "votes": meta.get("voteCount") or 0,
        "genres": [str(g) for g in (meta.get("genres") or [])],
        "runtime": (video or meta).get("runtime") or "",
        "mpaa": str(meta.get("mpaa") or ""),
    }


def thin_snapshot(info):
    """Snímek bez popisu i bez fotky — vznikl, když se titul přidal do Mého seznamu/
    stahování dřív, než pro něj doběhlo obohacení (TMDB, přepočet dabingu na pozadí
    u čerstvě přidaných titulů). Bez záchrany zůstane navždy prázdný, i když už
    mezitím data dorazila — `snapshot()` se z API znovu nevolá samo od sebe."""
    if info is None or info.get("type") in ("ws", "hs", "dav"):
        return False   # soubory bez meta — není z čeho dohledat
    if "rating" not in info:
        return True    # snímek ze starší verze bez hodnocení/žánrů/stopáže → jednou dohledat
    return not info.get("plot") and not (info.get("art") or {})


def apply_watched(li, key, context=None):
    """Zhlédnuto (fajfka) a bod pro pokračování z vlastní evidence.

    `context`: další položky kontextového menu. Kodi při každém `addContextMenuItems` přepisuje
    položky od indexu 0, proto se menu skládá tady najednou.
    """
    tag = li.getVideoInfoTag()
    count = STORE.playcount(key)
    # i nula výslovně: přehratelné položce (widget, Pokračovat s uloženým streamem) by
    # jinak Kodi dosadilo počet z vlastní videodatabáze, který Nokturno nevede
    tag.setPlaycount(count)
    resume, total = STORE.resume(key)
    try:
        if resume and not count:
            tag.setResumePoint(resume, total)
        else:
            # pozice 0 s nenulovou délkou: Kodi bere bod za nastavený (`CBookmark::IsSet`
            # = délka > 0), takže nedosadí starou záložku ze své databáze, a ukazatel
            # rozkoukání nekreslí (`IsPartWay` chce i pozici > 0)
            tag.setResumePoint(0, total or 1)
    except Exception:  # noqa: BLE001 – Kodi < 20
        pass
    li.addContextMenuItems(list(context or []) +
                           [(L(30044) if count else L(30043), runplugin(action="toggle_watched", id=key))])


def fav_context(key, ctype, series_id=None, alt=None):
    label = L(30062) if STORE.is_favourite(key) else L(30061)
    return (label, runplugin(action="toggle_fav", id=key, type=ctype, series=series_id, alt=alt))


def fill_info(li, meta, ctype="movie", video=None, tech=True):
    """`tech=False` vynechá stopáž a hodnocení.

    V seznamu streamů jsou to údaje o filmu, ne o streamu, ale skiny je vykreslují jako
    samostatné sloupce vpravo (Arctic Fuse) a ubírají tím šířku popisku streamu.
    """
    tag = li.getVideoInfoTag()
    tag.setMediaType("episode" if video else ("tvshow" if ctype == "series" else "movie"))
    title = (video or {}).get("title") or (meta.get("_title") if is_sosac_id(meta.get("id")) else None) \
        or meta.get("name") or ""
    tag.setTitle(title)
    if meta.get("_orig"):
        tag.setOriginalTitle(meta["_orig"])
    if ctype == "series":
        tag.setTvShowTitle(meta.get("_title") or meta.get("name") or "")
    plot = (video or {}).get("overview") or meta.get("description") or ""
    genres = ", ".join(genre_label(g) for g in (meta.get("genres") or []))
    if genres:
        # žánr na stejný řádek jako popis — s prázdným řádkem za ním zabral v panelu
        # skinu (Arctic Fuse, tři řádky) dva ze tří řádků a na popis zbyl jeden
        plot = f"[B]{genres}[/B] · {plot}" if plot else genres
    tag.setPlot(plot)
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    if tech and year.isdigit():
        tag.setYear(int(year))
    if meta.get("genres"):
        tag.setGenres([str(g) for g in meta["genres"]])
    try:
        if tech and meta.get("imdbRating"):
            rating = float(meta["imdbRating"])
            tag.setRating(rating)
            # skin kreslí z ratingu hvězdičky; procento posíláme zvlášť jako vlastnost
            li.setProperty("RatingPercent", f"{round(rating * 10)} %")
            if meta.get("voteCount"):  # jen TMDB — Luna/Cinemeta/Sosáč počet hlasů neznají
                tag.setVotes(int(meta["voteCount"]))
    except (TypeError, ValueError):
        pass
    if meta.get("mpaa"):  # věkový rating (jen TMDB, přednostně český)
        tag.setMpaa(str(meta["mpaa"]))
    if meta.get("trailerYoutubeId"):
        tag.setTrailer(f"plugin://plugin.video.youtube/play/?video_id={meta['trailerYoutubeId']}")
    # IMDb id: podle něj Kodi (OpenSubtitles apod.) hledá titulky
    if meta.get("imdb_id") or str(meta.get("id", "")).startswith("tt"):
        tag.setIMDBNumber(meta.get("imdb_id") or meta.get("id"))
    minutes = runtime_minutes((video or meta).get("runtime"))
    if tech and minutes:
        tag.setDuration(minutes * 60)
    if video:
        tag.setSeason(int(video.get("season") or 0))
        tag.setEpisode(int(video.get("episode") or 0))
        if video.get("released"):
            tag.setFirstAired(str(video["released"])[:10])
    cast = None
    extras = meta.get("app_extras") or {}
    if isinstance(extras, dict) and extras.get("cast"):
        cast = [(c.get("name", ""), c.get("character", ""), c.get("photo") or "") for c in extras["cast"][:15]]
    elif isinstance(meta.get("cast"), list) and meta["cast"] and isinstance(meta["cast"][0], dict):
        # TMDB — jméno, role a fotka rovnou ve tvaru API (viz tmdb_api.py:meta)
        cast = [(c.get("name", ""), c.get("character", ""), c.get("photo") or "") for c in meta["cast"][:15]]
    elif isinstance(meta.get("cast"), list):
        # Luna/Cinemeta (přes Sosáč) — jen jména, bez fotky
        cast = [(str(c), "", "") for c in meta["cast"][:15]]
    if cast:
        try:
            tag.setCast([xbmc.Actor(n, r, i, p) for i, (n, r, p) in enumerate(cast)])
        except Exception:  # noqa: BLE001 – starší API
            pass
    if isinstance(meta.get("director"), list) and meta["director"]:
        tag.setDirectors([str(d) for d in meta["director"]])
    if isinstance(meta.get("writer"), list) and meta["writer"]:
        tag.setWriters([str(w) for w in meta["writer"]])


# rozlišení, které se pošle skinu, když se kvalita jen odhadla z názvu
TIER_SIZE = {4: (3840, 2160), 3: (1920, 1080), 2: (1280, 720), 1: (720, 576)}
# „5.1" je šest kanálů — skin chce jejich počet, ne zápis se středem
CHANNEL_COUNT = {"1.0": 1, "2.0": 2, "2.1": 3, "5.1": 6, "6.1": 7, "7.1": 8}


def fill_streamdetails(li, s):
    """Technické údaje o stopách do položky.

    Odznak kvality si skin kreslí sám, jakmile o položce ví rozlišení — tak to
    dělají i jiné doplňky a vypadá to jako zbytek rozhraní, na rozdíl od obrázku
    přibaleného doplňkem. Ze stejných údajů skin bere i ikony zvuku a jazyků.

    Přesné rozlišení je z hlavičky souboru; když se číst nedalo, pošle se
    typické rozlišení odhadnuté třídy, ať odznak nechybí. Že jde o odhad, je
    poznat v popisku podle vlnovky.
    """
    info = s.get("_media") or {}
    width, height = info.get("width") or 0, info.get("height") or 0
    if not height:
        width, height = TIER_SIZE.get(s.get("quality_rank") or 0, (0, 0))
    tag = li.getVideoInfoTag()
    if height:
        tag.addVideoStream(xbmc.VideoStreamDetail(width=width, height=height))
    tracks = info.get("audio") or []
    if tracks:
        for t in tracks:
            tag.addAudioStream(xbmc.AudioStreamDetail(
                channels=CHANNEL_COUNT.get(t.get("channels") or "", 0),
                codec=(t.get("codec") or "").lower(),
                language=(t.get("lang") or "").lower()))
    else:
        for code, chans in (s.get("channels") or {}).items():
            tag.addAudioStream(xbmc.AudioStreamDetail(
                channels=CHANNEL_COUNT.get(f"{chans:.1f}", 0), language=str(code).lower()))
    for code in sorted(set(s.get("subs") or [])):
        tag.addSubtitleStream(xbmc.SubtitleStreamDetail(language=str(code).lower()))


def fill_info_snapshot(li, snap):
    """Info z uloženého snímku (bez API)."""
    tag = li.getVideoInfoTag()
    is_ep = snap.get("season") is not None
    tag.setMediaType("episode" if is_ep else ("tvshow" if snap.get("type") == "series" else "movie"))
    tag.setTitle(strip_year(snap.get("title"), snap.get("year")))
    if snap.get("tvshow"):
        tag.setTvShowTitle(snap["tvshow"])
    plot = snap.get("plot") or ""
    genres = ", ".join(genre_label(g) for g in (snap.get("genres") or []))
    if genres:
        plot = f"[B]{genres}[/B] · {plot}" if plot else genres
        tag.setGenres(list(snap["genres"]))
    tag.setPlot(plot)
    if str(snap.get("year") or "").isdigit():
        tag.setYear(int(snap["year"]))
    try:
        if snap.get("rating"):
            rating = float(snap["rating"])
            tag.setRating(rating)
            li.setProperty("RatingPercent", f"{round(rating * 10)} %")
            if snap.get("votes"):
                tag.setVotes(int(snap["votes"]))
    except (TypeError, ValueError):
        pass
    if snap.get("mpaa"):
        tag.setMpaa(str(snap["mpaa"]))
    minutes = runtime_minutes(snap.get("runtime"))
    if minutes:
        tag.setDuration(minutes * 60)
    if is_ep:
        tag.setSeason(int(snap.get("season") or 0))
        tag.setEpisode(int(snap.get("episode") or 0))
    base = split_episode_id(snap.get("id"))[0]
    if str(base).startswith("tt"):
        tag.setIMDBNumber(base)
    li.setArt(snap.get("art") or {})


def art_for(meta, video=None):
    art = {
        "poster": meta.get("poster") or "",
        "fanart": meta.get("background") or "",
        "landscape": meta.get("landscapePoster") or "",
        "clearlogo": meta.get("logo") or "",
        "thumb": (video or {}).get("thumbnail") or meta.get("poster") or "",
    }
    return {k: v for k, v in art.items() if v}


def add_meta_item(meta, ctype, alt=None, tag_source=False, label=None):
    """`alt` = id téhož titulu v Sosáči (sloučený výsledek hledání) → streamy z obou zdrojů.

    `tag_source`: ve smíšeném hledání označit tituly, které má jen Sosáč (v katalozích Sosáče je to zbytečné).
    `label`: vlastní popisek místo názvu (TV program: čas a stanice před názvem).
    """
    label = label or display_name(meta)
    if tag_source and is_sosac_id(meta.get("id")):
        label = f"{label}  [COLOR {GREY}]· Sosáč[/COLOR]"
    li = xbmcgui.ListItem(label=label)
    li.setArt(art_for(meta))
    fill_info(li, meta, ctype)
    fav = fav_context(meta["id"], ctype, alt=alt)
    similar = similar_context(ctype, meta["id"])
    if ctype == "series":
        li.addContextMenuItems([fav] + similar)
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="seasons", id=meta["id"], alt=alt), li, isFolder=True)
    else:
        apply_watched(li, meta["id"], [fav] + streams_context("movie", meta["id"], alt=alt) + similar)
        add_playable(li, "movie", meta["id"], alt=alt)


def add_playable(li, ctype, item_id, series_id=None, alt=None):
    """Film nebo díl — stream se vždy vybírá v dialogu na dva řádky (`choose_stream`).

    Ve výpisu Nokturna je položka `action=title` — ne-složka bez IsPlayable: klik ji Kodi spustí
    jako skript s handle −1 (`CGUIMediaWindow::OnClick` → `RunScriptWithParams`) → `pick_title()`
    najde streamy s ukazatelem, který jde zrušit, ukáže dialog a vybraný stream pustí přes
    `PlayMedia`. Zrušený dialog tak nic nehlásí. Přehrát v detailu (Estuary, Arctic Fuse přes
    TMDb Helper, tlačítko Play) tutéž položku rozklíčovává s normálním handle → `play(ask=1)`.
    Ve widgetu a na domovské obrazovce je položka přehratelná s `ask=1`. Nastavení „Výběr streamu“
    (automaticky / seznam / dialog) zrušeno v `5.2.14~beta2`, seznam streamů jako složka zůstal
    jen v kontextovém menu (stažení streamu, uvolněný fulltext).

    U epizod se předává i id seriálu — Sosáč dává epizodám vlastní id
    (`sosac2_1877:1:1`), ze kterého se meta seriálu nedá odvodit.
    """
    # rozkoukaný/dřív zhlédnutý titul má u sebe zapamatovanou vnitřní referenci streamu
    # (viz mark_playing, Player.save_resume) — ta na rozdíl od podepsaného odkazu zdroje
    # nevyprší, `play()` tak může přeskočit hledání napříč zdroji a rovnou pokračovat na
    # stejném streamu (dozná se, jestli mezitím zmizel ze zdroje, a spadne na hledání samo).
    resumed = STORE.resume_stream(item_id)
    stream_url, stream_subs = resumed if resumed else (None, None)
    if browsing_nokturno() and not stream_url:
        url = build_url(action="title", type=ctype, id=item_id, series=series_id, alt=alt)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
        return
    li.setProperty("IsPlayable", "true")
    # `ask=1` = dialog; bez `ask` (Up Next, HA) hraje zapamatovaný nebo nejlepší stream bez ptaní.
    # Uplatní se jen v plném hledání (`stream_url` prázdný, nebo se uložená reference nedala přehrát).
    url = build_url(action="play", type=ctype, id=item_id, series=series_id, alt=alt, ask="1",
                    url=stream_url, subs=stream_subs)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)


def streams_context(ctype, item_id, series_id=None, alt=None):
    """„Vybrat stream“ a „Stáhnout“ v kontextovém menu — oba otevřou dialog výběru (`pick_title`).
    Vybrat stream i u položky, která by jinak hrála rovnou (Pokračovat ve sledování s uloženým streamem),
    Stáhnout vybraný stream zařadí do fronty stahování místo přehrání. Výpis streamů jako složka od
    `5.2.14~beta4` není (na přání uživatele vše v modálním okně)."""
    params = dict(type=ctype, id=item_id, series=series_id, alt=alt)
    return [(L(30513, "Vybrat stream"), runplugin(action="title", **params)),
            (L(30070, "Stáhnout"), runplugin(action="title_download", **params))]


def add_snapshot_item(key, snap, extra_context=None):
    """Položka ze snímku (Pokračovat, Můj seznam, Naposledy)."""
    if snap.get("type") == "ws":
        f = {"ident": snap["id"][3:], "name": snap.get("title", ""), "img": (snap.get("art") or {}).get("thumb", ""),
             "size_h": snap.get("size_h", ""), "positive": 0, "negative": 0}
        add_ws_file(f, extra_context)
        return
    # HellSpy a vlastní úložiště mají snímek z `play_hs`/`play_dav` — bez těchhle větví šel
    # jejich klíč do `api_for` jako titul a Pokračovat skončilo chybou Luny (audit 2026-09-14)
    if snap.get("type") == "hs":
        _prefix, file_id, file_hash = snap["id"].split(":", 2)
        add_hs_file({"id": file_id, "hash": file_hash, "name": snap.get("title") or snap["id"],
                     "size_h": snap.get("size_h", "")}, extra_context)
        return
    if snap.get("type") == "dav":
        try:
            slot, path = parse_ref(snap["id"])
        except StorageError:
            return
        api = next((s for s in get_storages() if s.slot == slot), None)
        if api is None:
            return   # úložiště už není v nastavení
        add_dav_file(api, {"path": path, "name": snap.get("title") or path.rsplit("/", 1)[-1],
                           "size_h": snap.get("size_h", "")}, extra_context)
        return
    label = strip_year(snap.get("title"), snap.get("year")) or key
    if snap.get("tvshow") and snap.get("season") is not None:
        label = f"{snap['tvshow']} – {int(snap['season'])}x{int(snap['episode'] or 0):02d} {label}"
    li = xbmcgui.ListItem(label=label)
    fill_info_snapshot(li, snap)
    ctx = [fav_context(key, snap.get("type", "movie"), snap.get("series"), snap.get("alt"))] + (extra_context or [])
    if snap.get("type") == "series" and snap.get("season") is None:
        li.addContextMenuItems(ctx + similar_context("series", key))
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="seasons", id=key, alt=snap.get("alt")), li, isFolder=True)
        return
    kind = "series" if snap.get("season") is not None else "movie"
    similar = similar_context("movie", key) if kind == "movie" else []
    apply_watched(li, key, ctx + streams_context(kind, key, snap.get("series"), snap.get("alt")) + similar)
    add_playable(li, kind, key, series_id=snap.get("series"), alt=snap.get("alt"))


def add_ws_file(f, extra_context=None):
    key = "ws:" + f["ident"]
    votes = f"+{f['positive']}/-{f['negative']}" if (f.get("positive") or f.get("negative")) else ""
    label = f"{f['name']}  [COLOR FF9A9A9A]{f.get('size_h', '')} {votes}[/COLOR]"
    li = xbmcgui.ListItem(label=label)
    if f.get("img"):
        li.setArt({"thumb": f["img"], "icon": f["img"]})
    tag = li.getVideoInfoTag()
    tag.setMediaType("video")
    tag.setTitle(f["name"])
    tag.setPlot(f"{WS_TAG}  {f.get('size_h', '')}  {votes}")
    ctx = [(L(30070), runplugin(action="download_ws", ident=f["ident"], name=f["name"]))] + (extra_context or [])
    apply_watched(li, key, ctx)
    li.setProperty("IsPlayable", "true")
    xbmcplugin.addDirectoryItem(HANDLE, build_url(action="play_ws", ident=f["ident"], name=f["name"]), li, isFolder=False)


def add_dav_file(api, f, extra_context=None):
    """Soubor z vlastního úložiště ve výpisu hledání (a v Pokračovat/Naposledy přes `add_snapshot_item`)."""
    key = f"dav:{api.slot}:{f['path']}"
    folder = f["path"].rsplit("/", 1)[0] if "/" in f["path"] else ""
    label = f"{f['name']}  [COLOR FF9A9A9A]{f.get('size_h', '')}[/COLOR]"
    li = xbmcgui.ListItem(label=label)
    tag = li.getVideoInfoTag()
    tag.setMediaType("video")
    tag.setTitle(f["name"])
    tag.setPlot(f"[COLOR {DAV_COLOR}]{api.name}[/COLOR]  {f.get('size_h', '')}\n{folder}")
    apply_watched(li, key, list(extra_context or []))   # např. „Odebrat z Pokračovat“ z výpisu
    li.setProperty("IsPlayable", "true")
    xbmcplugin.addDirectoryItem(
        HANDLE, build_url(action="play_dav", slot=api.slot, path=f["path"], name=f["name"]), li, isFolder=False)


def add_hs_file(f, extra_context=None):
    """Soubor z HellSpy ve výpisu hledání. Odkaz se dohledává až při přehrání."""
    key = f"hs:{f['id']}:{f['hash']}"
    label = f"{f['name']}  [COLOR FF9A9A9A]{f.get('size_h', '')}[/COLOR]"
    li = xbmcgui.ListItem(label=label)
    tag = li.getVideoInfoTag()
    tag.setMediaType("video")
    tag.setTitle(f["name"])
    tag.setPlot(f"{HS_TAG}  {f.get('size_h', '')}")
    if f.get("duration"):
        tag.setDuration(int(f["duration"]))
    ctx = [(L(30070), runplugin(action="download_hs", id=f["id"], hash=f["hash"], name=f["name"]))]
    apply_watched(li, key, ctx + (extra_context or []))
    li.setProperty("IsPlayable", "true")
    xbmcplugin.addDirectoryItem(
        HANDLE, build_url(action="play_hs", id=f["id"], hash=f["hash"], name=f["name"]), li, isFolder=False)


# --- meta a streamy -------------------------------------------------------------

def meta_for(apis, meta_type, item_id):
    """Detail titulu — TMDB, Luna/Sosáč, veřejný katalog Sosáče, Cinemeta (viz `Engine._meta_for`)."""
    return engine_of(apis)._meta_for(meta_type, item_id)


def load_meta(apis, ctype, item_id, series_id=None):
    """Meta titulu (u epizody meta seriálu + konkrétní video) pro popis a OSD."""
    return engine_of(apis).meta(ctype, item_id, series_id)


def title_queries(apis, meta, video, ctype, alt=None, strict=True):
    """Dotazy pro fulltextové zdroje a filtr názvu souboru — viz `Engine._title_queries`.
    `strict=False` (ruční „Zkusit uvolněný fulltext") pustí soubor se slovy kdekoli v názvu."""
    return engine_of(apis)._title_queries(meta, video, ctype, alt, strict)


DIRECT_SOURCES = ("ws", "hs", "st", "fs")   # fulltextové zdroje, kde bývá tentýž soubor jako u Luny


def drop_duplicates(streams):
    """Soubor nalezený přes Lunu i napřímo je jeden soubor — párování podle názvu a
    velikosti dělá jádro (`Engine._merge_direct`); tady se jen označí přímé nálezy."""
    for s in streams:
        parse_stream(s)
        if s.get("source") in DIRECT_SOURCES:
            s["_direct"] = True
    return Engine._merge_direct(streams)


def load_meta_video(meta, item_id):
    """Díl z meta seriálu podle id epizody, u filmu None (pro hledání na WebShare)."""
    _base, season, episode = split_episode_id(item_id)
    if season is None:
        return None
    return next((v for v in meta.get("videos") or []
                 if int(v.get("season") or 0) == season and int(v.get("episode") or 0) == episode), None)


def collect_streams(apis, ctype, item_id, meta, alt=None, progress=None, strict=True, errors=None):
    """Streamy ze všech zdrojů, vyfiltrované a seřazené podle nastavení — `Engine.raw_streams`.

    Každý zdroj běží pod vlastní pojistkou jádra: když selže (vypnutý addon Luny,
    výpadek WebShare…), jeho chyba přijde do `errors` jako `SourceFailure` a hledá
    se dál v ostatních; co s tím udělat (upozornit), řeší volající. `progress`,
    je-li dán, dostává `set(done, total)` po každé fázi — čtení hlaviček je
    z nich zdaleka nejdelší —, `source(label, count)` po dokončení každého
    jednotlivého zdroje, ať je vidět odkud kolik streamů zatím přišlo, a
    `audio(done, total)` v průběhu čtení hlaviček, ať je vidět kolik je ověřeno.
    """
    errors = [] if errors is None else errors
    failures = []
    engine = engine_of(apis)
    try:
        streams = engine.raw_streams(ctype, item_id, alt, on_progress=progress.set if progress else None,
                                     failures=failures, strict=strict, meta_video=(meta, load_meta_video(meta, item_id)),
                                     on_source_done=progress.source if progress else None,
                                     on_audio_progress=progress.audio if progress else None)
    finally:
        errors.extend(SourceFailure(label, err) for label, err in failures)
        remember_ws_token(engine.ws)
    xbmc.log(f"[{ADDON_ID}] streamy {item_id}: {describe_timings(engine.last_timings)}", xbmc.LOGINFO)
    return streams


def describe_timings(t):
    """Jeden řádek do logu: kolik která fáze hledání streamů trvala (`Engine.last_timings`)."""
    if t.get("cache"):
        return (f"z cache, celkem {t.get('celkem', 0)} s · hlavičky {t.get('hlavičky', 0)}"
                f" ({t.get('hlaviček', 0)}, nedočteno {t.get('hlaviček nedočteno', 0)}) · {t.get('streamů', 0)} streamů")
    zdroje = ", ".join(f"{k} {v}" for k, v in sorted((t.get("zdroje") or {}).items(), key=lambda kv: kv[1]))
    return (f"celkem {t.get('celkem', 0)} s · hlavní {t.get('hlavni', 0)} · souběžně {t.get('souběžně', 0)}"
            f" ({zdroje}){' · znovu česky' if t.get('znovu česky') else ''} · úložiště navíc {t.get('úložiště navíc', 0)}"
            f" · hlavičky {t.get('hlavičky', 0)} ({t.get('hlaviček', 0)}, nedočteno {t.get('hlaviček nedočteno', 0)},"
            f" na pozadí {t.get('hlavičky na pozadí', 0)}) · {t.get('streamů', 0)} streamů (sloučeno {t.get('sloučeno', 0)})")


def storage_first(streams):
    """Soubory z vlastního úložiště vždy na začátek, jinak ve stejném pořadí.

    Mezi desítkami streamů z WebShare a HellSpy se vlastní soubor ztrácel (u Bláznivé
    dovolené byl 5. z 61 a uživatel ho nenašel) — přitom je to ten, kvůli kterému
    úložiště má, a přehrává se bez závislosti na cizí službě."""
    return [s for s in streams if s.get("source") == "dav"] + [s for s in streams if s.get("source") != "dav"]


def stream_signature(s):
    """Co si z vybraného streamu pamatovat: zdroj, kvalita a jazyky zvuku."""
    parse_stream(s)
    return {"source": s.get("source") or "", "quality": int(s.get("quality_rank") or 0),
            "langs": sorted(s.get("langs") or [])}


def preferred_stream(streams, pref):
    """Stream odpovídající zapamatované volbě, nebo None (→ dialog / nejlepší).

    Nejdřív přesná shoda (zdroj, kvalita, všechny jazyky), pak aspoň zdroj a
    kvalita — u dalšího dílu bývá zvuk stejný, ale ne vždy jsou stejné značky.
    """
    if not pref:
        return None
    want = set(pref.get("langs") or [])
    for strict in (True, False):
        for st in streams:
            parse_stream(st)
            if (st.get("source") or "") != pref.get("source") or int(st.get("quality_rank") or 0) != int(pref.get("quality") or 0):
                continue
            if strict and not want <= set(st.get("langs") or []):
                continue
            return st
    return None


def pref_param(s):
    sig = stream_signature(s)
    return f"{sig['source']}|{sig['quality']}|{','.join(sig['langs'])}"


def pref_from_param(value):
    try:
        source, quality, langs = (value or "").split("|", 2)
    except ValueError:
        return None
    return {"source": source, "quality": int(quality or 0), "langs": [x for x in langs.split(",") if x]}


SOURCE_GROUP = {"main": "Luna", "search": "WebShare", "ws": "WebShare",
                "sosac": "Sosáč", "hs": "HellSpy", "st": "Sledujteto", "fs": "FastShare", "dav": L(30405, "Úložiště")}


def stream_tracks(s):
    """Zvukové stopy streamu jako (jazyk, kanály, kodek) DOHROMADY za stopu —
    aby šlo filtrovat kombinaci (např. CZ 5.1), a ne párovat jazyk a kanály
    nezávisle přes různé stopy (CZ 2.0 + EN 5.1 by jinak filtru „CZ a 5.1"
    vyhovělo taky, i když žádná stopa ve skutečnosti CZ 5.1 není).

    Kodek zná jen stream, u kterého se přečetla hlavička souboru (`_tracks`) —
    zdroje samy o kodeku nic neříkají, proto ho ostatní stopy mají `None`.
    """
    tracks = s.get("_tracks") or []
    if tracks:
        return [{"lang": t.get("lang"), "channels": t.get("channels"), "codec": t.get("codec")} for t in tracks]
    raw = s["label"]
    for junk in ("(WS)", "Sosáč"):
        raw = raw.replace(junk, "")
    raw = raw.strip()
    channels = s.get("channels") or {}
    langs = set(s.get("langs") or []) | langs_from_name(raw)
    return [{"lang": code, "channels": (f"{channels[code]:.1f}" if code in channels else None), "codec": None}
            for code in langs] or [{"lang": None, "channels": None, "codec": None}]


def stream_facets(s):
    """Kvalita, zvuk (jazyk/kanály/kodek), titulky a zdroj — přesně to, co vidí
    uživatel v `stream_label`, jen bez barev a řádkování. Používá to i filtr
    streamů, aby nabízel a pároval přesně to, co je v řádku vidět.
    """
    parse_stream(s)
    raw = s["label"]
    for junk in ("(WS)", "Sosáč"):
        raw = raw.replace(junk, "")
    raw = raw.strip()
    tracks = stream_tracks(s)
    subs = set(s.get("subs") or []) | subs_from_name(raw)
    return {
        "quality_rank": s.get("quality_rank") or 0,
        "langs": {t["lang"] for t in tracks if t["lang"]},
        "channels": {t["channels"] for t in tracks if t["channels"]},
        "codecs": {t["codec"] for t in tracks if t["codec"]},
        "subs": subs,
        "source": SOURCE_GROUP.get(s.get("source"), s.get("source") or ""),
        "tracks": tracks,
    }


def apply_stream_filter(streams, fq="", flang="", fch="", fcodec="", fsub="", fsrc=""):
    """Streamy, které vyhovují filtru z dialogu výběru (`filter_dialog`). Prázdný filtr = beze změny.

    Jazyk, kanály a kodek se ověřují na téže stopě (viz `stream_tracks`) — stream
    projde, jen když aspoň jedna jeho stopa vyhovuje všem třem najednou.
    """
    want_q = {x for x in fq.split(",") if x}
    want_lang = {x for x in flang.split(",") if x}
    want_ch = {x for x in fch.split(",") if x}
    want_codec = {x for x in fcodec.split(",") if x}
    want_sub = {x for x in fsub.split(",") if x}
    want_src = {x for x in fsrc.split(",") if x}
    if not (want_q or want_lang or want_ch or want_codec or want_sub or want_src):
        return list(streams)
    out = []
    for st in streams:
        f = stream_facets(st)
        if want_q and str(f["quality_rank"]) not in want_q:
            continue
        if want_sub and not (f["subs"] & want_sub):
            continue
        if want_src and f["source"] not in want_src:
            continue
        if (want_lang or want_ch or want_codec) and not any(
            (not want_lang or t["lang"] in want_lang)
            and (not want_ch or t["channels"] in want_ch)
            and (not want_codec or t["codec"] in want_codec)
            for t in f["tracks"]
        ):
            continue
        out.append(st)
    return out


FILTER_KINDS = ("q", "lang", "ch", "codec", "sub", "src")


def filter_params(filt):
    """{"q": [...], "lang": [...], …} → parametry `apply_stream_filter` (`fq`, `flang`, …)."""
    return {"f" + k: ",".join((filt or {}).get(k) or []) for k in FILTER_KINDS}


def filter_dialog(streams, active=None):
    """Výběr filtru podle toho, co se u titulu doopravdy našlo → {"q": [...], …}, nebo None
    (zrušeno, nebo není podle čeho filtrovat). Sdílí ho složka se seznamem streamů
    dialog výběru streamu (`choose_stream`)."""
    facets = [stream_facets(st) for st in streams]
    qualities = sorted({f["quality_rank"] for f in facets if f["quality_rank"]}, reverse=True)
    langs = sorted({c for f in facets for c in f["langs"]})
    # kanály sestupně (7.1 → 1.0), ne abecedně — "5.1" by jinak stálo za "7.1"
    channels = sorted({c for f in facets for c in f["channels"]}, key=lambda c: -float(c))
    codecs = sorted({c for f in facets for c in f["codecs"]})
    subs = sorted({c for f in facets for c in f["subs"]})
    sources = sorted({f["source"] for f in facets if f["source"]})

    options, kinds = [], []
    for q in qualities:
        options.append(f"{L(30208, 'Kvalita')}: {QUALITY_NAMES.get(q, '')}")
        kinds.append(("q", str(q)))
    for code in langs:
        options.append(f"{L(30209, 'Zvuk')}: {code}")
        kinds.append(("lang", code))
    for ch in channels:
        options.append(f"{L(30215, 'Kanály')}: {ch}")
        kinds.append(("ch", ch))
    for codec in codecs:
        options.append(f"{L(30216, 'Kodek')}: {codec}")
        kinds.append(("codec", codec))
    for code in subs:
        options.append(f"{L(30210, 'Titulky')}: {code}")
        kinds.append(("sub", code))
    for src in sources:
        options.append(f"{L(30211, 'Zdroj')}: {src}")
        kinds.append(("src", src))
    if not options:
        notify(L(30212, "Není podle čeho filtrovat"))
        return None

    active = active or {}
    preselect = [i for i, (kind, val) in enumerate(kinds) if val in (active.get(kind) or [])]
    chosen = xbmcgui.Dialog().multiselect(L(30213, "Filtr streamů"), options, preselect=preselect)
    if chosen is None:
        return None
    new = {k: [] for k in FILTER_KINDS}
    for i in chosen:
        kind, val = kinds[i]
        new[kind].append(val)
    if any(new.values()):
        # zapamatovat jen skutečný filtr, ne jeho úplné zrušení — viz „Použít poslední filtr“.
        # Soubor je v profilu tohohle Kodi, takže si ho každá instalace pamatuje sama za sebe.
        STORE.set_last_stream_filter(new)
    return new


FULLTEXT = "fulltext"


def choose_stream(streams, preferred=None, relax=False, expand=None):
    """Výběr streamu v dialogu na dva řádky — jediný způsob výběru od `5.2.14~beta2` (klik ve výpisu,
    Přehrát v detailu, widget, TMDb Helper), od `beta4` i jediné místo (výpis streamů jako složka zrušen).
    Nahoře Filtr streamů, Zrušit filtr, Použít poslední filtr. `preferred` (zapamatovaná volba
    u seriálu) je předvybraný. `relax=True` přidá dole „Zkusit uvolněný fulltext“ a jeho volba
    vrátí `FULLTEXT`. `expand()` vrátí seznam se sloučenými verzemi (`_alts`) každou zvlášť —
    nabízí se jako „Zobrazit všechny streamy“ před fulltextem, jen když je co rozbalit.
    Vrací stream, `FULLTEXT`, nebo None."""
    active = {}
    while True:
        shown = apply_stream_filter(streams, **filter_params(active))
        if not shown:
            notify(L(30214, "Filtr nic nenechal, zobrazeny všechny streamy"), xbmcgui.NOTIFICATION_WARNING)
            active, shown = {}, list(streams)
        entries = []   # (popisek, volba) nad seznamem streamů
        if len(streams) > 1:
            on = any(active.values())
            count = f"({len(shown)}/{len(streams)})" if on else f"({len(streams)})"
            entries.append((f"[B]{L(30213, 'Filtr streamů')}[/B]  {count}", "filter"))
            if on:
                entries.append((L(30363, "Zrušit filtr"), "clear"))
            last = STORE.last_stream_filter() or {}
            last = {k: list(last.get(k) or []) for k in FILTER_KINDS}
            if any(last.values()) and last != {k: list(active.get(k) or []) for k in FILTER_KINDS}:
                last_count = len(apply_stream_filter(streams, **filter_params(last)))
                if last_count:   # 0 shodných by bylo jen matoucí tlačítko do prázdna
                    entries.append((f"{L(30364, 'Použít poslední filtr')}  ({last_count})", "last"))
        rows = []
        for label, _v in entries:
            rows.append(xbmcgui.ListItem(label=label))
        for st in shown:
            top, bottom = stream_lines(st)
            if st.get("_alts"):
                top = f"{top}  [COLOR {GREY}]×{len(st['_alts']) + 1}[/COLOR]"
            row = xbmcgui.ListItem(label=top, label2=bottom)
            icon = quality_icon(st)
            if icon:
                row.setArt({"icon": icon, "thumb": icon})
            rows.append(row)
        tail = []   # volby pod seznamem streamů
        hidden = sum(len(st.get("_alts") or ()) for st in streams)
        if expand and hidden:
            tail.append("all")
            rows.append(xbmcgui.ListItem(label=f"{L(30558, 'Zobrazit všechny streamy')}  ({len(streams) + hidden})"))
        if relax:
            tail.append(FULLTEXT)
            rows.append(xbmcgui.ListItem(label=L(30335, "Zkusit uvolněný fulltext (WebShare, HellSpy, Sledujteto, FastShare)")))
        focus = next((i for i, st in enumerate(shown) if st is preferred), None)
        idx = xbmcgui.Dialog().select(L(30024), rows, useDetails=True,
                                      preselect=len(entries) + focus if focus is not None else -1)
        if idx < 0:
            return None
        if idx >= len(entries) + len(shown):
            if tail[idx - len(entries) - len(shown)] == FULLTEXT:
                return FULLTEXT
            streams = expand()
            continue
        if idx >= len(entries):
            return shown[idx - len(entries)]
        volba = entries[idx][1]
        if volba == "filter":
            new = filter_dialog(streams, active)
            if new is not None:
                active = new
        elif volba == "clear":
            active = {}
        else:
            active = last


def browsing_nokturno():
    """Běží plugin z výpisu Nokturna v okně Videa (klik na položku)? Z widgetu, domovské
    obrazovky nebo detailu otevřeného odtamtud je aktivní jiné okno a kontejner není náš —
    změřeno na Office 2026-09-14: klik ve výsledcích = okno 10025 + `plugin.video.nokturno`,
    widget/domovská obrazovka = 10000 + prázdný `Container.PluginName`."""
    return bool(xbmc.getCondVisibility("Window.IsMedia")) and xbmc.getInfoLabel("Container.PluginName") == ADDON_ID


def format_duration(seconds):
    """`9722` → `1:39` (h:mm) — kompaktní zápis stopáže do popisku streamu."""
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}:{m:02d}" if h else f"{m} min"


VIDEO_TAGS = (
    (r"\b(?:x|h)\.?265\b|\bhevc\b", "HEVC"),
    (r"\b(?:x|h)\.?264\b|\bavc\b", "H.264"),
    (r"\bav1\b", "AV1"),
    (r"\b(?:dv|dovi|dolby[ ._-]?vision)\b", "DV"),
    (r"\bhdr(?:10\+?)?\b", "HDR"),
    (r"\b10[ ._-]?bit\b", "10bit"),
)


def video_info(s, name):
    """Druhý řádek výběru streamu: rozlišení (jen přečtené z hlavičky souboru — z názvu
    se neví) a video kodek/HDR z názvu souboru (hlavička je nečte, zdroje ho neposílají)."""
    import re as _re
    parts = []
    media = s.get("_media") or {}
    if media.get("width") and media.get("height"):
        parts.append(f"{media['width']}×{media['height']}")
    low = (name or "").lower()
    for pattern, tag in VIDEO_TAGS:
        if _re.search(pattern, low) and tag not in parts:
            parts.append(tag)
    return " ".join(parts)


def stream_label_parts(s):
    """Díly popisku streamu (každý už obarvený) — skládá je `stream_label` do jednoho
    řádku pro výpis a `stream_list_item` do dvou řádků pro dialog výběru."""
    parse_stream(s)
    tag = SOURCE_TAGS.get(s.get("source"), "")
    raw = s["label"]
    for junk in ("(WS)", "Sosáč"):
        raw = raw.replace(junk, "")
    raw = raw.strip()
    quality = QUALITY_NAMES.get(s.get("quality_rank", 0), "")
    if quality and s.get("_estimated"):
        quality = "~" + quality   # jádro kvalitu odhadlo z velikosti (název ji neřekl)
    elif not quality and s.get("size_gb"):
        # soubor bez kvality v názvu (typicky přímo z WebShare): odhad podle velikosti, s vlnovkou
        quality = "~" + QUALITY_NAMES.get(estimate_rank(s["size_gb"]), "")
    import re as _re
    rest = _re.sub(r"\b(4K|Full HD|UHD|FHD|HD|SD)\b", "", raw)   # \b → „HDR“ zůstane celé
    rest = " ".join(rest.replace(" - ", " ").split())
    if s.get("source") == "sosac":
        rest = ""   # u Sosáče je zbytek jen jazyk, ten je už ve zvuku

    # Kvalita je první — na ni se v seznamu kouká nejdřív. Vše, co nemá vlastní
    # barvu (žádný [COLOR] okolo), Kodi vykreslí bílou textovou barvou skinu;
    # na vybrané položce s bílým podkladem to pak úplně zmizí. Proto má i
    # velikost výslovnou barvu (GREY se na bílém podkladu čte jako tmavý text).
    head = []
    if s.get("_storage"):
        # vlastní soubor: štítek úložiště jako první, ne až za zvukem a velikostí na konci řádku
        head.append(f"[COLOR {DAV_COLOR}][B]{s['_storage']}[/B][/COLOR]")
        tag = ""
    if s.get("_loose"):
        # z ručního „Zkusit fulltext" — přísný filtr ho zahodil jako podobný,
        # ale možná jiný titul; uživatel to musí posoudit sám podle názvu souboru
        head.append(f"[COLOR {WARN_COLOR}]?[/COLOR]")
    quality_text = f"[COLOR {QUALITY_COLORS.get(s.get('quality_rank', 0), GREY)}][B]{quality or raw}[/B][/COLOR]"
    audio, langs, seen_langs = [], [], set()
    tracks = s.get("_tracks") or []
    if tracks:
        # přečteno z hlavičky souboru: každá stopa zvlášť i s kodekem
        for t in tracks:
            inside = " ".join(x for x in (t.get("codec"), t.get("channels"), t.get("lang")) if x)
            if inside:
                audio.append(f"[COLOR {LANG_COLORS.get(t.get('lang'), GREY)}][{inside}][/COLOR]")
            code = t.get("lang")
            if code and code not in seen_langs:
                seen_langs.add(code)
                langs.append(f"[COLOR {LANG_COLORS.get(code, GREY)}][B]{code}[/B][/COLOR]")
    else:
        # zdroj o stopách mlčí — poskládá se z toho, co je po ruce
        channels = s.get("channels") or {}
        pref = PREF_LANGS[int(setting("pref_lang", "0"))]
        # jazyk odhadnutý z názvu souboru (`_langs_from_name` z jádra) leží přímo v `langs`,
        # ale ověřený není — vlnovka jako u odhadu, který se do `langs` nedostal
        guessed = set(s.get("langs") or []) if s.get("_langs_from_name") else set()
        known = set(s.get("langs") or []) - guessed
        for code in sorted(known | guessed | langs_from_name(raw), key=lambda c: (c != pref, c)):
            mark = "" if code in known else "~"
            txt = f"{mark}{code}"
            langs.append(f"[COLOR {LANG_COLORS.get(code, GREY)}][B]{txt}[/B][/COLOR]")
            if code in channels:
                txt += f" {channels[code]:.1f}"
            audio.append(f"[COLOR {LANG_COLORS.get(code, GREY)}][{txt}][/COLOR]")
    # klíče jako v nastavení „stream_layout“ (`STREAM_PARTS`); co uživatel v pořadí nemá, se nezobrazí
    parts = {"head": head, "quality": quality_text, "audio": "  ".join(audio), "langs": "  ".join(langs),
             "size": "", "length": "", "bitrate": "", "subs": "", "source": "", "file": "", "video": ""}
    if s.get("size_gb"):
        parts["size"] = f"[COLOR {GREY}][B]{s['size_gb']:.1f} GB[/B][/COLOR]"
    if s.get("_length_s"):
        mark = "~" if s.get("_length_est") else ""
        parts["length"] = f"[COLOR {GREY}]{mark}{format_duration(s['_length_s'])}[/COLOR]"
    if s.get("bitrate"):
        mark = "~" if s.get("_bitrate_est") else ""
        parts["bitrate"] = f"[COLOR {GREY}]{mark}{s['bitrate']:g} Mb/s[/COLOR]"
    subs = set(s.get("subs") or []) | subs_from_name(raw)
    if subs:
        parts["subs"] = f"[COLOR {GREY}]Tit.: {' '.join(sorted(subs))}[/COLOR]"
    if tag:
        parts["source"] = tag
    if rest and quality:
        parts["file"] = f"[COLOR {GREY}]{rest}[/COLOR]"
    video = video_info(s, s.get("_ws_name") or raw)
    if video:
        parts["video"] = f"[COLOR {GREY}]{video}[/COLOR]"
    return parts


# Co ukazovat u streamu a v jakém pořadí (nastavení „stream_layout“, od `5.2.14~beta4`): `a,b|c,d`,
# svislítko odděluje horní a dolní řádek dialogu. Nahradilo šest přepínačů show_size a spol.
STREAM_PARTS = ("langs", "size", "video", "audio", "bitrate", "length", "subs", "source", "file")
STREAM_LAYOUT_DEFAULT = "langs,size|video,audio,bitrate,length,subs,source,file"
_OLD_SHOW = {"size": "show_size", "length": "show_length", "source": "show_source", "file": "show_file",
             "subs": "show_subs", "bitrate": "show_bitrate"}


def stream_layout():
    """(horní řádek, dolní řádek) jako seznamy klíčů. Neplatná hodnota → výchozí pořadí.
    Výchozí hodnota u instalace, která dřív vypnula některý přepínač `show_*`, ho vynechá."""
    raw = setting("stream_layout", STREAM_LAYOUT_DEFAULT).strip()
    rows = parse_order(raw, set(STREAM_PARTS)) if raw else None
    if rows is None:
        rows = parse_order(STREAM_LAYOUT_DEFAULT, set(STREAM_PARTS))
    if raw in ("", STREAM_LAYOUT_DEFAULT):
        off = {key for key, old in _OLD_SHOW.items() if setting(old, "true") == "false"}
        rows = [[k for k in row if k not in off] for row in rows]
    return rows[0], rows[1]


def stream_layout_reset():
    ADDON.setSetting("stream_layout", STREAM_LAYOUT_DEFAULT)
    notify(L(30512, "Pořadí údajů u streamu vráceno na výchozí"))


QUALITY_ICON_DIR = os.path.join(ADDON.getAddonInfo("path"), "resources", "media", "quality")
QUALITY_ICON_KEYS = {4: "4k", 3: "fhd", 2: "hd", 1: "sd"}


def quality_icon(s):
    """Odznak kvality do dialogu výběru (UHD 4K, FHD 1080…, s HDR i DV) — cesta k PNG, nebo "".
    Obrázky kreslí `tools/make_quality_icons.py`."""
    parse_stream(s)
    key = QUALITY_ICON_KEYS.get(int(s.get("quality_rank") or 0))
    if not key:
        return ""
    tags = video_info({}, s.get("_ws_name") or s.get("label") or "").split()
    suffix = "-hdr" if ("HDR" in tags or "DV" in tags) else ""
    return os.path.join(QUALITY_ICON_DIR, f"{key}{suffix}.png")


def stream_lines(s):
    """Dva řádky streamu v dialogu výběru (`select(useDetails=True)`) — jednořádkový popisek ani
    výpis streamů jako složka od `5.2.14~beta4` nejsou. Co a v jakém pořadí, určuje `stream_layout()`;
    výchozí je nahoře jazyk a velikost, dole technika.

    Jazyk bez vlnovky přišel od zdroje nebo z hlavičky souboru, s vlnovkou je
    jen odhad z názvu souboru — stejně jako „~4K" u odhadnuté kvality."""
    p = stream_label_parts(s)
    # s odznakem kvality (`quality_icon`) je nápis zbytečný — zůstává jen odhad „~4K“ a neznámá kvalita
    shown_quality = [] if quality_icon(s) and not s.get("_estimated") else [p["quality"]]
    top_keys, bottom_keys = stream_layout()
    top = "  ".join(x for x in p["head"] + shown_quality + [p[k] for k in top_keys] if x)
    bottom = "  ".join(x for x in [p[k] for k in bottom_keys] if x)
    return top, bottom


def mark_playing(key, title="", year=None, kind="movie", stream_url=None, stream_subs=None, stream_langs=None):
    # stream_url/stream_subs: vnitřní reference zvoleného streamu (ne podepsaný odkaz zdroje,
    # ten vyprší) — služba (Player.save_resume) si je uloží k pozici, ať se dá „Pokračovat ve
    # sledování“ pustit rovnou bez nového hledání (viz add_playable/add_snapshot_item)
    # stream_langs: jazyky zvuku, které o streamu tvrdí zdroj — služba podle nich pozná češtinu
    # i u souboru, jehož stopy jazyk neuvádějí (`Player.apply_tracks`, `tracks.pick_audio`)
    xbmcgui.Window(10000).setProperty(PLAYING_PROP, json.dumps(
        {"id": key, "title": title, "year": year, "kind": kind,
         "stream_url": stream_url, "stream_subs": stream_subs, "stream_langs": stream_langs or []}))


SUBS_DIR = os.path.join(PROFILE, "subs")
SUBS_MAX_BYTES = 2 * 1024 * 1024   # titulky mají desítky kB; víc je omyl odkazu (video)
SUBS_KEEP_S = 2 * 86400
# na titulky se před přehráním čeká nejvýš tolik — WebShare u nedostupného souboru odpovídá
# „temporarily unavailable“ až po 5–13 s (Office 2026-09-16) a přehrání by stálo
SUBS_BUDGET_S = 4.0


def local_subtitles(apis, refs):
    """Titulky ze zdroje (`ws:<ident>`, odkaz Sosáče) stáhne do profilu a vrátí cesty.

    Kodi pozná jazyk externích titulků jen z názvu souboru a podepsaný odkaz zdroje
    ho neobsahuje, takže služba nevěděla, které titulky jsou české. Jazyk se tu
    určí z textu (`tracks.subtitle_lang`) a zapíše do názvu (`nokturno-….cze.srt`),
    text jde do UTF-8 s BOM — české titulky ve windows-1250 jinak Kodi ukazuje
    s rozsypanou diakritikou. Preferovaný jazyk jde v seznamu první.

    Rozklíčování i stažení běží souběžně a čeká se nejvýš `SUBS_BUDGET_S`; co do té
    doby nedoběhne, přehrání nezdrží (vlákno dožije se svým HTTP timeoutem). Token
    WebShare je v tu chvíli už platný — odkaz na samotný stream se rozklíčoval před
    titulky. Titulky, které se nepodaří rozklíčovat, se vynechají (dřív výjimka
    z `resolve_url` shodila celé přehrání), co se nepodaří stáhnout, jde odkazem."""
    refs = [r for r in refs if r]
    if not refs:
        return []
    try:
        xbmcvfs.mkdirs(SUBS_DIR)
        now = time.time()
        for name in os.listdir(SUBS_DIR):
            path = os.path.join(SUBS_DIR, name)
            if now - os.path.getmtime(path) > SUBS_KEEP_S:
                os.remove(path)
    except OSError as e:
        xbmc.log(f"[{ADDON_ID}] složka titulků: {e}", xbmc.LOGWARNING)

    import hashlib
    from concurrent.futures import wait as wait_futures

    def fetch(ref):
        try:
            link = resolve_url(apis, ref)
        except Errors as e:
            xbmc.log(f"[{ADDON_ID}] titulky {ref}: {e}", xbmc.LOGINFO)
            return None
        if not link:
            return None
        url, _sep, header_part = link.partition("|")
        headers = {"User-Agent": "Mozilla/5.0"}
        headers.update(dict(urllib.parse.parse_qsl(header_part)))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=8) as resp:
                raw = resp.read(SUBS_MAX_BYTES + 1)
            if not raw or len(raw) > SUBS_MAX_BYTES:
                return link, ""
            text = decode_subtitle(raw)
            lang = subtitle_lang(text)
            name = "nokturno-" + hashlib.sha1(ref.encode("utf-8")).hexdigest()[:10]
            if lang:
                name += "." + FILE_CODES[lang]
            path = os.path.join(SUBS_DIR, f"{name}.{subtitle_format(text)}")
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(text)
            return path, lang
        except Exception as e:  # noqa: BLE001 – síť, zápis; titulky nesmí shodit přehrání
            xbmc.log(f"[{ADDON_ID}] titulky {ref} se nestáhly, předám odkaz: {e}", xbmc.LOGINFO)
            return link, ""

    pool = ThreadPoolExecutor(max_workers=min(len(refs), 4))
    futures = [pool.submit(fetch, ref) for ref in refs]
    wait_futures(futures, timeout=SUBS_BUDGET_S)
    pool.shutdown(wait=False)
    fetched = []
    for ref, future in zip(refs, futures):
        if not future.done():
            xbmc.log(f"[{ADDON_ID}] titulky {ref}: nestihly se do {SUBS_BUDGET_S:g} s, přehrávám bez nich",
                     xbmc.LOGINFO)
            continue
        if future.result():
            fetched.append(future.result())
    pref = PREF_LANGS[int(setting("pref_lang", "0"))]
    rank = {lang: i for i, lang in enumerate(SUBTITLE_FALLBACK.get(pref, ()))}
    order = sorted(range(len(fetched)), key=lambda i: (rank.get(fetched[i][1], len(rank)), i))
    return [fetched[i][0] for i in order]


def mark_viewed(key, title="", year=None, kind="movie"):
    """Titul, u kterého se právě zobrazily streamy — nezávisle na tom, jestli si
    uživatel nějaký pustí. Vypovídá o zájmu líp než počítání přehrání: spousta
    streamů nejde přehrát vůbec (mrtvý odkaz, region, žádná titulková stopa)
    a to není chyba diváka. Kolikrát za den se to stane, neřeší ani tohle, ani
    služba — dedup „jednou denně" dělá server podle času posledního zobrazení."""
    xbmcgui.Window(10000).setProperty(VIEWED_PROP, json.dumps(
        {"id": key, "title": title, "year": year, "kind": kind}))


def mark_used():
    """Otevření doplňku – službě to stačí pro „naposledy použito“ ve statistikách."""
    xbmcgui.Window(10000).setProperty(USED_PROP, str(int(time.time())))


def sync_settings():
    """(adresa HA, klíč) nebo None, když synchronizace není zapnutá či vyplněná."""
    if not on("sync_enabled", "false"):
        return None
    url, key = setting("sync_url").strip(), setting("sync_key").strip()
    return (url, key) if url and key else None


def request_sync():
    xbmcgui.Window(10000).setProperty(SYNC_PROP, "1")


def list_ha_files():
    """Soubory stažené integrací do HA — přehratelné z tohohle Kodi přes adresu HA
    (může být i Nabu Casa, pak hraje i mimo síť). Podepsané odkazy dává HA."""
    cfg = sync_settings()
    if not cfg:
        notify(L(30186, "Synchronizace není zapnutá nebo chybí adresa a klíč"), xbmcgui.NOTIFICATION_WARNING, 5000)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        return
    base, key = cfg
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/nokturno/files", headers={"X-Nokturno-Key": key})
        with urllib.request.urlopen(req, timeout=20) as resp:
            files = json.loads(resp.read().decode("utf-8")).get("files") or []
    except Exception as e:  # noqa: BLE001 – HA nedostupná, špatný klíč
        notify(f"{L(30188, 'Synchronizace selhala')}: {str(e)[:80]}", xbmcgui.NOTIFICATION_ERROR, 5000)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        return
    set_content("movies")
    for f in files:
        size = human_size(f.get("size") or 0)
        subs = f"  [COLOR {GREY}]tit.[/COLOR]" if f.get("subtitles") else ""
        li = xbmcgui.ListItem(label=f"{f.get('name', '')}  [COLOR {GREY}]{size}[/COLOR]{subs}")
        li.setProperty("IsPlayable", "true")
        li.getVideoInfoTag().setTitle(f.get("name", ""))
        url = base.rstrip("/") + f.get("path", "")   # podepsaná relativní cesta z HA
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    xbmcplugin.endOfDirectory(HANDLE)


def sync_now():
    """Ruční synchronizace — z hlavního menu i z nastavení."""
    cfg = sync_settings()
    if not cfg:
        notify(L(30186, "Synchronizace není zapnutá nebo chybí adresa a klíč"), xbmcgui.NOTIFICATION_WARNING, 5000)
    else:
        ok, pushed, pulled, why = sync_once(STORE, cfg[0], cfg[1], xbmc.getInfoLabel("System.FriendlyName"))
        notify((L(30187, "Synchronizováno: odesláno %d, přijato %d") % (pushed, pulled)) if ok
               else f"{L(30188, 'Synchronizace selhala')}: {why}",
               xbmcgui.NOTIFICATION_INFO if ok else xbmcgui.NOTIFICATION_ERROR, 5000)
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)


def sub_status():
    """Tlačítko v nastavení: kolik dní zbývá z předplatného WebShare."""
    ws = get_webshare()
    if ws is None:
        xbmcgui.Dialog().ok(L(30000), L(30230, "WebShare není zapnutý nebo nemá vyplněný účet."))
        return
    try:
        st = ws.account_status()
    except WebshareError as e:
        xbmcgui.Dialog().ok(L(30000), f"WebShare: {e}")
        return
    if st["vip"]:
        msg = Lf(30231, st["days"], st["until"][:10])
    else:
        msg = L(30232, "VIP není aktivní.")
    xbmcgui.Dialog().ok(L(30000), msg)


def accounts_set():
    """Má uživatel vyplněný aspoň jeden účet nebo zdroj, který účet nepotřebuje nastavit?"""
    return any(setting(k).strip() for k in ("token", "streamuj_username", "ws_username", "st_email", "fs_username",
                                            "tmdb_api_key", "dav1_url", "dav2_url", "dav3_url"))


# --- Nastavit z mobilu -----------------------------------------------------------------

# kategorie nastavení, které jdou vyplnit z mobilu; Pokročilé a Info jsou jen tlačítka akcí,
# Stahování chce cestu vybranou v Kodi
REMOTE_SETUP_CATEGORIES = ("ws", "sosac", "hs", "st", "fs", "luna", "storage", "database", "playback",
                           "streamlist", "trakt", "sync", "stats")
REMOTE_SETUP_TIMEOUT = 1800
STREAM_PART_LABELS = (("langs", 30501), ("size", 30502), ("video", 30503), ("audio", 30504), ("bitrate", 30505),
                      ("length", 30506), ("subs", 30507), ("source", 30508), ("file", 30509))
KODI_TAG_RE = re.compile(r"\[/?(?:B|I|CR|COLOR|UPPERCASE|LOWERCASE|CAPITALIZE|LIGHT)[^\]]*\]")


def _plain(text):
    return KODI_TAG_RE.sub(" ", text or "").strip()


def remote_setup_schema(section=None):
    """Formulář pro mobil přímo ze `settings.xml` — nová položka nastavení se na stránce
    objeví sama. Popisky a nápověda jdou z `strings.po` v jazyce Kodi. `section` = jen jedna
    kategorie (tlačítko Nastavit z mobilu přímo v ní, např. Výběr streamu)."""
    import xml.etree.ElementTree as ET
    root = ET.parse(os.path.join(ADDON_PATH, "resources", "settings.xml")).getroot()
    sections = []
    for category in root.iter("category"):
        if category.get("id") not in REMOTE_SETUP_CATEGORIES or section and category.get("id") != section:
            continue
        groups = category.findall("group")
        fields = []
        for group in groups:
            if group.get("label") and len(groups) > 1:
                fallback = f"Úložiště {group.get('id')}"
                fields.append({"type": "heading", "label": _plain(L(int(group.get("label")), fallback))})
            for node in group.findall("setting"):
                kind, control = node.get("type"), node.find("control")
                field = {"id": node.get("id")}
                if node.get("id") == "stream_layout":
                    field["type"] = "order"
                    field["items"] = [(key, L(label, key)) for key, label in STREAM_PART_LABELS]
                elif kind == "boolean":
                    field["type"] = "bool"
                elif kind == "string":
                    field["type"] = ("password" if control is not None and control.find("hidden") is not None
                                     else "text")
                elif kind == "integer" and node.find("constraints/options") is not None:
                    field["type"] = "choice"
                    field["options"] = [(opt.text, _plain(L(int(opt.get("label")), opt.text)) if opt.get("label")
                                         else opt.text) for opt in node.find("constraints/options")]
                elif kind == "integer" and node.find("constraints/maximum") is not None:
                    low = int(node.findtext("constraints/minimum") or 0)
                    step = int(node.findtext("constraints/step") or 1)
                    high = int(node.findtext("constraints/maximum"))
                    field["type"] = "choice"
                    field["options"] = [(str(v), str(v)) for v in range(low, high + 1, step)]
                else:
                    continue
                field["default"] = (node.findtext("default") or "").strip()
                field["label"] = (_plain(L(int(node.get("label")), node.get("id"))) if node.get("label")
                                  else node.get("id"))
                if field["type"] == "order":   # nápověda z Kodi popisuje zápis `a,b|c`, tady jsou šipky
                    field["help"] = L(30514, "Šipkami přesuň údaje mezi horním a dolním řádkem dialogu výběru "
                                             "streamu, do Nezobrazovat dej, co nechceš vidět. Kvalita je vždy "
                                             "obrázek vlevo.")
                elif node.get("help"):
                    field["help"] = _plain(L(int(node.get("help"))))
                dep = node.find("dependencies/dependency[@type='enable']")
                if dep is not None and dep.get("setting"):
                    field["enable"] = (dep.get("setting"), (dep.text or "").strip())
                fields.append(field)
        if fields:
            sections.append({"id": category.get("id"), "label": _plain(L(int(category.get("label")))),
                             "fields": fields, "open": not sections})
    return sections


def _solid_rgba_png(rgb, alpha):
    """PNG 1×1 RGBA jedné barvy — Kodi ji roztáhne na velikost kontroly.

    Podklad tlačítka s adresou v `RemoteSetupWindow`. Výchozí textura skinu (bez vlastní)
    se na telefonu kreslila užší než tlačítko a posunutá doprava, text přes ni přetékal
    (5.2.21~beta4/5). Jednobarevná pilulka je stejná v každém skinu."""
    r, g, b = rgb
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    raw = bytes([0, r, g, b, alpha])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class RemoteSetupWindow(xbmcgui.WindowDialog):
    """Okno s QR kódem. Neblokuje — `remote_setup()` mezitím čeká na mobil; Zpět zruší.

    Na Androidu je adresa klikatelná — OK/klik ji otevře v systémovém prohlížeči přímo na
    tomhle zařízení (`StartAndroidActivity`, nahlásil uživatel 2026-09-18: díval se na QR
    z mobilu a chtěl adresu rovnou otevřít, ne ji přepisovat ručně). Jinde (CoreELEC/Linux,
    Windows…) ten builtin nic nedělá, takže tam adresa zůstává jen čitelný text jako dřív.

    Klik na adresu (5.2.21~beta1–4, telefon uživatele): ťuknutí prstem měnilo jen fokusový
    vzhled tlačítka, `onControl` nikdy nepřišel — s prázdnou, poloprůhlednou i neprůhlednou
    vlastní texturou stejně. Kodi na ťuknutí pošle nejdřív `ACTION_MOUSE_MOVE` (ten tlačítko
    zaostří, proto ta barva) a pak akci z touch keymapy; kde se cestou k `GUI_MSG_CLICKED`
    ztrácí, se z logu nezjistilo. Proto se klik bere ze dvou míst: z `onControl` (řádná
    cesta, srovnání přes `getId()`, ne `==` — Kodi může do callbacku dát jiný Python obal
    téhož ovládacího prvku) a z `onAction` pro každou klikací akci (OK, levé tlačítko myši,
    ťuknutí) ve chvíli, kdy má adresa fokus — `Window.onAction` dostává i akce myši/dotyku,
    jakmile je nějaký prvek zaostřený. `open_link()` obě cesty sloučí (jeden klik = jedno
    otevření). Podklad tlačítka je vlastní jednobarevná pilulka (`_solid_rgba_png`) — ani
    prázdná textura, ani výchozí tlačítko skinu nevypadaly na telefonu dobře."""
    CANCEL_ACTIONS = (9, 10, 13, 92)   # PARENT_DIR, PREVIOUS_MENU, STOP, NAV_BACK
    # SELECT_ITEM, MOUSE_LEFT_CLICK, MOUSE_DOUBLE_CLICK, MOUSE_LONG_CLICK, TOUCH_TAP
    CLICK_ACTIONS = (7, 100, 103, 108, 401)
    MOUSE_MOVE = 107

    def __init__(self, qr_path, backdrop_path, url, link_bg_path, link_bg_focus_path):
        super().__init__()
        self.cancelled = False
        self.url = url
        self.link = None
        self._opened_at = 0.0
        self.addControl(xbmcgui.ControlImage(0, 0, 1280, 720, backdrop_path, colorDiffuse="F20D0B14"))
        self.addControl(xbmcgui.ControlLabel(90, 70, 1100, 50, "[B]%s[/B]" % L(30447, "Nastavit z mobilu"),
                                             font="font13", textColor="FFFFFFFF"))
        self.addControl(xbmcgui.ControlImage(90, 150, 400, 400, qr_path, aspectRatio=2))
        steps = xbmcgui.ControlTextBox(540, 160, 660, 220, font="font13", textColor="FFE6E1F0")
        self.addControl(steps)
        steps.setText(L(30452, "1. Připoj mobil ke stejné Wi-Fi jako tenhle přístroj.[CR]"
                               "2. Naskenuj QR kód fotoaparátem, nebo otevři v prohlížeči adresu:"))
        footer = L(30453, "Zpět zruší · adresa platí 10 minut a pro jedno uložení")
        if xbmc.getCondVisibility("System.Platform.Android"):
            self.link = xbmcgui.ControlButton(540, 400, 700, 60, "[B]%s[/B]" % url, font="font13",
                                              textColor="FFC4B5FD", focusedColor="FFFFFFFF",
                                              noFocusTexture=link_bg_path, focusTexture=link_bg_focus_path)
            self.addControl(self.link)
            self.setFocus(self.link)
            footer += " · " + L(30517, "OK adresu otevře v prohlížeči")
        else:
            self.addControl(xbmcgui.ControlLabel(540, 400, 700, 60, "[B]%s[/B]" % url, font="font13",
                                                 textColor="FFC4B5FD"))
        self.addControl(xbmcgui.ControlLabel(90, 610, 1100, 40, footer, font="font13", textColor="FF9B95AD"))

    def onAction(self, action):
        aid = action.getId()
        if aid in self.CANCEL_ACTIONS:
            self.cancelled = True
            self.close()
            return
        if self.link is None or aid == self.MOUSE_MOVE:
            return
        focus = self._focus_id()
        xbmc.log(f"[{ADDON_ID}] Nastavit z mobilu: akce {aid}, fokus {focus}, adresa {self.link.getId()}",
                 xbmc.LOGINFO)
        if aid in self.CLICK_ACTIONS and focus == self.link.getId():
            self.open_link("onAction %d" % aid)

    def onControl(self, control):
        if self.link is not None and control.getId() == self.link.getId():
            self.open_link("onControl")

    def _focus_id(self):
        try:
            return self.getFocusId()
        except (RuntimeError, SystemError):
            return -1

    def open_link(self, via):
        now = time.time()
        if now - self._opened_at < 1.5:
            xbmc.log(f"[{ADDON_ID}] Nastavit z mobilu: {via} — tentýž klik, už otevřeno", xbmc.LOGINFO)
            return
        self._opened_at = now
        xbmc.log(f"[{ADDON_ID}] Nastavit z mobilu: {via} — otevírám {self.url}", xbmc.LOGINFO)
        xbmcgui.Dialog().notification(L(30447, "Nastavit z mobilu"), L(30518, "Otvírám v prohlížeči…"),
                                      xbmcgui.NOTIFICATION_INFO, 2000)
        xbmc.executebuiltin('StartAndroidActivity("", "android.intent.action.VIEW", "", "%s")' % self.url)


def remote_setup(section=None):
    """„Nastavit z mobilu“ (Nastavení → Pokročilé, průvodce): QR s místní adresou, mobil ve
    stejné Wi-Fi vyplní formulář, Kodi uloží změny. Server (`lib/remote_setup.py`) běží jen
    po dobu dialogu. Vrací počet uložených položek, nebo None při zrušení/chybě.

    Otevřený dialog nastavení doplňku se nejdřív zavře: drží vlastní kopii hodnot a při
    zavření by změny z mobilu přepsal."""
    if xbmc.getCondVisibility("Window.IsVisible(addonsettings)"):
        xbmc.executebuiltin("Dialog.Close(addonsettings,true)")
        for _ in range(30):
            if not xbmc.getCondVisibility("Window.IsVisible(addonsettings)") or MONITOR.waitForAbort(0.1):
                break
    ip = xbmc.getIPAddress()
    if not ip or ip.startswith("127.") or ip == "0.0.0.0":
        xbmcgui.Dialog().ok(L(30447, "Nastavit z mobilu"),
                            L(30454, "Tenhle přístroj nemá adresu v místní síti. Připoj ho k Wi-Fi nebo kabelem "
                                     "a zkus to znovu."))
        return None
    schema = remote_setup_schema(section)
    # neuložená položka: výchozí hodnota ze settings.xml — jinak by prohlížeč u výběru poslal
    # první volbu a u přepínače „vypnuto“ a uložilo by se, co uživatel neměnil
    values = {f["id"]: ADDON.getSetting(f["id"]) or f["default"] for section in schema for f in section["fields"]
              if f.get("type") != "heading"}
    texts = {
        "title": "Nokturno — " + L(30447, "Nastavit z mobilu"),
        "intro": L(30458, "Vyplň, co chceš změnit, a ulož. Nastavení se hned propíše do Kodi."),
        "save": L(30459, "Uložit do Kodi"),
        "saved": L(30460, "Uloženo. Nastavení je v Kodi, stránku můžeš zavřít."),
        "password_set": L(30461, "vyplněno — nech prázdné beze změny"),
        "expired": L(30462, "Tahle adresa už neplatí. Na TV spusť Nastavit z mobilu znovu."),
        "invalid": L(30463, "Neplatná hodnota: %s").replace("%s", "{}"),
        "order_rows": L(30499, "Horní řádek|Dolní řádek"),
        "order_hidden": L(30500, "Nezobrazovat"),
        "order_up": L(30510, "Nahoru"),
        "order_down": L(30511, "Dolů"),
    }
    server = SetupServer(schema, values, texts)
    try:
        server.start()
    except OSError as e:
        xbmcgui.Dialog().ok(L(30447, "Nastavit z mobilu"), Lf(30455, e) if L(30455) else str(e))
        return None
    url = server.url(ip)
    qr_path = os.path.join(PROFILE, "remote-setup-%s.png" % server.token[:8])
    backdrop = os.path.join(PROFILE, "remote-setup-bg.png")
    link_bg = os.path.join(PROFILE, "remote-setup-link-bg.png")
    link_bg_focus = os.path.join(PROFILE, "remote-setup-link-bg-focus.png")
    changes, window = None, None
    try:
        xbmcvfs.mkdirs(PROFILE)
        with open(qr_path, "wb") as f:
            f.write(qr_png(qr_encode(url), scale=12, border=2))
        with open(backdrop, "wb") as f:
            f.write(qr_png([[False]], scale=1, border=0))
        with open(link_bg, "wb") as f:
            f.write(_solid_rgba_png((92, 68, 150), 230))
        with open(link_bg_focus, "wb") as f:
            f.write(_solid_rgba_png((124, 92, 200), 255))
        xbmc.log(f"[{ADDON_ID}] nastavení z mobilu: server na portu {server.port}", xbmc.LOGINFO)
        window = RemoteSetupWindow(qr_path, backdrop, url, link_bg, link_bg_focus)
        window.show()
        deadline = time.time() + REMOTE_SETUP_TIMEOUT
        while time.time() < deadline and not window.cancelled and not should_stop():
            # Kodi doručí `onAction` (Zpět) oknu skriptu jen během volání svého API — čekání
            # čistě v Pythonu (`Event.wait`) ho nepustí a Zpět nic nezavřelo (Office, beta 3).
            # Návratovou hodnotu hlídá `should_stop()` v podmínce smyčky.
            MONITOR.waitForAbort(0.2)
            changes = server.wait_result(0.05)
            if changes is not None or server.finished:
                break
    finally:
        if window is not None:
            window.close()
            del window
        server.stop()
        try:
            os.remove(qr_path)
        except OSError:
            pass
    if changes is None:
        return None
    for key, value in changes.items():
        ADDON.setSetting(key, value)
    if any(k.startswith("ws_") for k in changes):
        xbmcgui.Window(10000).clearProperty("nokturno.ws_token")   # nový účet = nový login
    xbmc.log(f"[{ADDON_ID}] nastavení z mobilu uloženo: {', '.join(sorted(changes))}", xbmc.LOGINFO)
    if changes:
        notify(Lf(30456, len(changes)) if L(30456) else "Nastavení z mobilu uloženo (%d)" % len(changes))
    else:
        notify(L(30457, "Z mobilu nepřišla žádná změna"))
    return len(changes)


def remote_setup_action(section=None):
    """Tlačítko v nastavení (RunPlugin, bez výpisu) — po uložení otevře nastavení znovu.
    `section` = stránka jen s jednou kategorií (`remote_setup_schema`)."""
    saved = remote_setup(section if section in REMOTE_SETUP_CATEGORIES else None)
    if HANDLE >= 0:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
    if saved:
        ADDON.openSettings()


def _wizard_accounts(dialog):
    """Průvodce ovladačem: účty a zdroje otázku po otázce."""
    if dialog.yesno(L(30340, "WebShare"), L(30341, "Máš účet WebShare?")):
        user = dialog.input(L(30342, "WebShare — e-mail"))
        if user:
            pwd = dialog.input(L(30343, "WebShare — heslo"), option=xbmcgui.ALPHANUM_HIDE_INPUT)
            if pwd:
                ADDON.setSetting("ws_username", user)
                ADDON.setSetting("ws_password", pwd)
                ADDON.setSetting("ws_enabled", "true")

    if dialog.yesno(L(30344, "Sosáč"),
                     L(30345, "Máš účet Streamuj.tv (přehrávač Sosáče)?[CR]"
                              "Katalogy Sosáče fungují i bez účtu, jen pro přehrávání je potřeba.")):
        user = dialog.input(L(30346, "Streamuj.tv — uživatel"))
        if user:
            pwd = dialog.input(L(30347, "Streamuj.tv — heslo"), option=xbmcgui.ALPHANUM_HIDE_INPUT)
            if pwd:
                ADDON.setSetting("streamuj_username", user)
                ADDON.setSetting("streamuj_password", pwd)
        ADDON.setSetting("sosac_enabled", "true")

    if dialog.yesno(L(30348, "Luna: Absolute Cinema"),
                     L(30349, "Máš v síti spuštěný server Luna: Absolute Cinema?")):
        # adresu Luny uživatel při prvním spuštění zpravidla nezná — najdeme ji za něj
        # (sken podsítě trvá vteřiny) a rovnou mu řekneme, kam si má jít pro token
        try:
            found = luna_discover(should_stop=should_stop)
        except Exception:  # noqa: BLE001 – bez sítě, bez IPv4 adresy
            found = []
        if found:
            ADDON.setSetting("luna_url", found[0]["url"])
            heading = Lf(30542, found[0]["url"])
        else:
            heading = L(30350, "Adresa doplňku nebo token ze setu Luny")
        addr = dialog.input(heading)
        if addr:
            ADDON.setSetting("token", addr)
        if addr or found:
            ADDON.setSetting("luna_enabled", "true")

    if dialog.yesno(L(30351, "HellSpy"), L(30352, "Zapnout HellSpy? Je zdarma a nepotřebuje žádný účet.")):
        ADDON.setSetting("hs_enabled", "true")

    if dialog.yesno(L(30367, "Sledujteto"),
                     L(30388, "Máš účet Sledujteto?[CR]"
                              "Hledá se přes tvůj účet, přehrávat jde jen s Premium.")):
        email = dialog.input(L(30389, "Sledujteto — e-mail"))
        if email:
            pwd = dialog.input(L(30390, "Sledujteto — heslo"), option=xbmcgui.ALPHANUM_HIDE_INPUT)
            if pwd:
                ADDON.setSetting("st_email", email)
                ADDON.setSetting("st_password", pwd)
                ADDON.setSetting("st_enabled", "true")

    if dialog.yesno(L(30417, "FastShare"),
                     L(30424, "Máš účet FastShare?[CR]"
                              "Hledá se i bez něj, přehrání jde z tvého kreditu nebo neomezeného tarifu.")):
        user = dialog.input(L(30420, "FastShare — uživatel"))
        if user:
            pwd = dialog.input(L(30422, "FastShare — heslo"), option=xbmcgui.ALPHANUM_HIDE_INPUT)
            if pwd:
                ADDON.setSetting("fs_username", user)
                ADDON.setSetting("fs_password", pwd)
                ADDON.setSetting("fs_enabled", "true")

    if dialog.yesno(L(30353, "Vlastní databáze filmů a seriálů"),
                     L(30354, "Chceš zadat zdarma klíč TMDB, aby popisy a obsazení filmů byly česky? (nepovinné)")):
        dialog.ok(L(30353, "Vlastní databáze filmů a seriálů"),
                  L(30355, "Klíč se zakládá zdarma na themoviedb.org -> ikona profilu -> Nastavení -> API -> "
                           "Request an API Key -> Developer -> zkopírovat \"API Key (v3 auth)\".[CR]"
                           "Podrobný návod je i v nápovědě u tohoto nastavení."))
        key = dialog.input(L(30356, "API klíč TMDB"))
        if key:
            ADDON.setSetting("tmdb_api_key", key)


def setup_wizard(force=False):
    """Průvodce prvním nastavením — nabídne se sám při prvním otevření doplňku,
    ať uživatel nemusí sám hledat, co a kde v nastavení vyplnit. Jde přeskočit
    (`Přeskočit` na úvodní obrazovce), nebo si ho kdykoli znovu pustit ručně
    z Nastavení → Pokročilé (`force=True`, běží bez ohledu na to, že už proběhl).
    """
    if not force:
        if STORE.load("wizard_done", False):
            return
        # už existující instalace (aktualizace z verze bez průvodce) — má-li
        # uživatel vyplněný účet, není to nová instalace a nemá se ho co ptát.
        # Přepínače zdrojů se hlídat nesmí: sosac/luna/hs mají v settings.xml
        # výchozí true a Kodi ho vrací i bez uloženého nastavení, takže průvodce
        # se na čisté instalaci nikdy nespustil (audit 2026-09-14)
        if accounts_set():
            STORE.save("wizard_done", True)
            return
    dialog = xbmcgui.Dialog()
    while True:
        # úvodní volba (přání uživatele 2026-09-16): z mobilu, průvodce ovladačem, nebo přeskočit
        choice = dialog.yesnocustom(
            L(30336, "Vítej v Nokturnu"),
            L(30449, "Účty a zdroje můžeš vyplnit v mobilu — na TV se ukáže QR kód, stačí mobil ve stejné "
                     "Wi-Fi a hesla nepíšeš ovladačem. Nebo projdi krátkého průvodce ovladačem.[CR]"
                     "Kdykoli to můžeš přeskočit a doplnit později v Nastavení doplňku."),
            customlabel=L(30450, "Z mobilu"), nolabel=L(30339, "Přeskočit"), yeslabel=L(30451, "Průvodce ovladačem"),
        )
        if choice == 2:
            if remote_setup() is None:
                continue   # zrušeno nebo bez sítě → zpátky na volbu
            break
        if choice == 1:
            _wizard_accounts(dialog)
            break
        STORE.save("wizard_done", True)
        return

    if dialog.yesno(L(30361, "Rychlost internetu"),
                     L(30362, "Chceš teď změřit rychlost internetu a podle ní nastavit nejvyšší dovolený "
                              "datový tok streamů? Zabrání to sekání při přehrávání příliš velkého souboru.[CR]"
                              "Zabere necelou minutu, jde udělat i později v Nastavení → Přehrávání.")):
        speedtest()

    # jen když TMDb Helper je — jinak by otázka nedávala smysl (bez něj Přehrát v detailu
    # filmu z widgetů Nokturna funguje samo, player je jen pro detail z TMDb Helperu)
    if tmdbhelper_installed() and dialog.yesno(
            L(30415, "Přehrát z detailu filmu"),
            L(30416, "Máš doplněk TMDb Helper (detail filmu v Arctic Fuse a dalších skinech).[CR]"
                     "Nastavit Nokturno jako jeho přehrávač? Tlačítko Přehrát v detailu pak hledá "
                     "streamy v Nokturnu.")):
        if not install_tmdbhelper_player(set_default=True):
            notify(L(30413, "Přidání do TMDb Helperu selhalo"), xbmcgui.NOTIFICATION_ERROR)

    dialog.ok(L(30357, "Nastavení uloženo"),
              L(30358, "Hotovo! Cokoli z tohohle můžeš kdykoli změnit v Nastavení doplňku.[CR]"
                       "Bez zadaného zdroje budou katalog a hledání fungovat i tak, jen anglicky."))
    STORE.save("wizard_done", True)


def test_sources():
    """Tlačítko v nastavení: během pár vteřin řekne, který zdroj nefunguje a proč.

    Dřív se to poznalo až z prázdného seznamu streamů. Volá se mimo cache, aby
    zelená nebyla jen ozvěna včerejší odpovědi.
    """
    luna, sosac, ws, hs, st = get_luna(), get_sosac(), get_webshare(), get_hellspy(), get_sledujteto()
    fs = get_fastshare()
    storages = get_storages()
    tmdb = get_tmdb()

    def check_sledujteto():
        # přihlášení samo nestačí — bez Premium Sledujteto odkaz na přehrání nevydá
        user = st.me()
        return "Premium" if user.get("is_premium") else L(30406, "bez Premium — přehrávání nepůjde")

    def check_fastshare():
        # přihlášení a kolik zbývá — soubor se odečítá z kreditu, pokud účet nemá neomezený tarif
        account = fs.login()
        if account.get("unlimited"):
            return L(30425, "neomezené stahování")
        return f"{L(30426, 'kredit')} {account.get('credit_mb', 0) / 1024:.1f} GB"

    def check_luna():
        # manifest Luna vydá i pro neplatný token (jen s výchozím nastavením), takže
        # „přišel manifest" nic neznamená — viz `luna_api.diagnose`
        result = luna_diagnose(setting("luna_url"), setting("token"))
        if result["level"] == "ok":
            return result["version"]
        sid, fallback = LUNA_DIAG_SHORT.get(result["code"], LUNA_DIAG_SHORT["unreachable"])
        raise LunaError(L(sid, fallback))

    checks = {
        "Luna": check_luna if luna else None,
        "Sosáč": (lambda: len(sosac._get(SOSAC_EXPORT + "souboryzanry.json", ttl=0) or {}))
        if isinstance(sosac, SosacDirect) else None,
        "WebShare": (lambda: bool(ws.login())) if ws else None,
        # mimo cache jako ostatní — HellspyApi bez úložiště se ptá vždy znovu
        "HellSpy": (lambda: len(HellspyApi().search("matrix", limit=5)[0])) if hs else None,
        "Sledujteto": check_sledujteto if st else None,
        "FastShare": check_fastshare if fs else None,
        # jen ověření klíče, mimo cache — 401 se překládá na "neplatný TMDB API klíč" v tmdb_api._get
        "TMDB": (lambda: tmdb._get("/configuration") and None) if tmdb else None,
        # jen kořen složky — ověří adresu i heslo, celý strom se prochází až při hledání
        **{api.name: (lambda api=api: api.check()) for api in storages},
    }
    lines = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {name: pool.submit(fn) for name, fn in checks.items() if fn}
        for name in checks:
            if name not in futures:
                lines.append(f"{name}: {L(30169)}")
                continue
            try:
                result = futures[name].result(timeout=20)
                lines.append(f"{name}: {L(30168)}" + (f" ({result})" if isinstance(result, (int, str)) else ""))
            except Exception as e:  # noqa: BLE001 – přesně tohle chceme uživateli ukázat
                # „<zdroj> neodpovídá" místo technického výpisu (TLS, spojení odmítnuto…)
                lines.append(describe_failure(name, e))
    if ws:
        remember_ws_token(ws)
    xbmcgui.Dialog().ok(L(30170), "\n".join(lines))


# --- Luna: najít v síti a ověřit -------------------------------------------
#
# Luna je ze všech zdrojů jediná, která se instaluje mimo doplněk (server někde
# v síti), a „nefunguje mi to" o ní chodí nejčastěji. Test zdrojů výš na ni
# nestačí: manifest Luna vrátí i pro naprosto neplatný token, takže se tvářila
# zeleně i ve chvíli, kdy z ní nikdy nemohl přijít jediný stream.

# kód z `luna_api.diagnose` → (id řetězce, český fallback); %s je verze Luny a její adresa
LUNA_DIAG_TEXTS = {
    "ok": (30520, "Luna %s odpovídá a vrací streamy. Nastavení je v pořádku."),
    "no_url": (30521, "Není vyplněná adresa Luny ani token.[CR]Použij „Najít Lunu v síti“, nebo vlož "
                      "do pole Token celou adresu doplňku ze stránky /setup Luny."),
    "bad_url": (30522, "Adrese %s nerozumím.[CR]Čekám tvar jako 192.168.1.10:7126."),
    "unreachable": (30523, "Na adrese %s se nikdo neozval.[CR][CR]Běží počítač, kde je Luna spuštěná? "
                           "Je ve stejné síti jako tahle televize? Zkus „Najít Lunu v síti“."),
    "not_luna": (30524, "Na adrese %s něco odpovídá, ale není to Luna.[CR]Zkontroluj port — Luna má "
                        "výchozí 7126."),
    "no_token": (30525, "Luna %s běží, ale chybí token.[CR][CR]Otevři v prohlížeči %s/setup, zkopíruj "
                        "adresu doplňku a vlož ji celou do pole Token — adresu i token z ní doplněk "
                        "vytáhne sám."),
    "bad_token_format": (30526, "V poli Token není token.[CR][CR]Token začíná „e1.“ a je dlouhý. Otevři "
                                "%s/setup a zkopíruj celou adresu doplňku."),
    "bad_token": (30527, "Luna %s běží, ale tenhle token nepřijala.[CR][CR]Vygeneruj si adresu doplňku "
                         "znovu na %s/setup a vlož ji celou do pole Token."),
    "main_empty": (30528, "Luna %s odpovídá a hledání na WebShare funguje, ale její hlavní zdroj nic "
                          "nevrací.[CR][CR]Zkontroluj na %s/setup účet WebShare a jestli je token "
                          "opravdu z téhle Luny."),
    "no_streams": (30529, "Luna %s běží, ale nenašla streamy ani u známých filmů.[CR][CR]Nejčastěji "
                          "chybí účet WebShare v samotné Luně — otevři %s/setup a doplň ho."),
}


# totéž na jeden řádek — „Ověřit zdroje" má pro každý zdroj jen řádek, ne odstavec
LUNA_DIAG_SHORT = {
    "no_url": (30543, "chybí adresa i token"),
    "bad_url": (30544, "adresa nedává smysl"),
    "unreachable": (30545, "server neodpovídá"),
    "not_luna": (30546, "na té adrese neběží Luna"),
    "no_token": (30547, "běží, ale chybí token"),
    "bad_token_format": (30548, "v poli Token není token"),
    "bad_token": (30549, "token Luna nepřijala"),
    "main_empty": (30550, "hlavní zdroj nic nevrací — účet WebShare v Luně?"),
    "no_streams": (30551, "nenašla žádné streamy — účet WebShare v Luně?"),
}

# co se v které hlášce dosazuje za %s (pořadí podle textu)
LUNA_DIAG_ARGS = {
    "ok": ("version",),
    "bad_url": ("base",),
    "unreachable": ("base",),
    "not_luna": ("base",),
    "no_token": ("version", "base"),
    "bad_token_format": ("base",),
    "bad_token": ("version", "base"),
    "main_empty": ("version", "base"),
    "no_streams": ("version", "base"),
}


def luna_find():
    """Tlačítko v nastavení: projde vlastní podsíť a najde server Luny.

    Adresa je první, co lidem nesedí — opisují ji z návodu, ne ze své sítě.
    Sken je levný (TCP klepnutí na 7126, celá podsíť do pár vteřin) a za Lunu
    se prohlásí jen to, co se k ní přizná v manifestu.
    """
    dialog = xbmcgui.DialogProgress()
    dialog.create(L(30000, "Nokturno"), L(30530, "Hledám Lunu v místní síti…"))
    try:
        found = luna_discover(
            on_progress=lambda done, total: dialog.update(int(done * 100 / max(total, 1))),
            should_stop=lambda: dialog.iscanceled() or should_stop())
    except Exception as e:  # noqa: BLE001 – bez sítě, bez IPv4 adresy
        found = []
        xbmc.log(f"[Nokturno] hledání Luny selhalo: {e}", xbmc.LOGWARNING)
    finally:
        dialog.close()

    if not found:
        xbmcgui.Dialog().ok(L(30000, "Nokturno"),
                            L(30531, "V téhle síti jsem Lunu nenašel.[CR][CR]Běží na některém počítači "
                                     "v domácnosti? Má výchozí port 7126? Pokud běží jinde nebo na jiném "
                                     "portu, vyplň adresu ručně."))
        return
    pick = 0 if len(found) == 1 else xbmcgui.Dialog().select(
        L(30532, "Nalezené servery Luny"), [f"{f['url']}   ({f['name']} {f['version']})" for f in found])
    if pick < 0:
        return
    ADDON.setSetting("luna_url", found[pick]["url"])
    ADDON.setSetting("luna_enabled", "true")
    # rovnou navážeme ověřením — samotná adresa bez tokenu ještě nic nepřehraje.
    # Adresu předáme přímo: `getSetting` by při otevřeném dialogu nastavení vrátil
    # ještě tu starou (prázdnou) a ověření by hlásilo „adrese nerozumím“
    luna_check(found[pick]["url"])


def luna_check(base=None, token=None, kolo=0, ask=False):
    """Tlačítko v nastavení: řekne, na kterém článku řetězu to stojí.

    Vrací jednu větu a k ní radu, co s tím — ne technický výpis. Když to
    nevyjde, nabídne zadat adresu rovnou tady a poslat log, aby nebylo nutné
    popisovat problém slovy („nejde mi to“ se nedá opravit).

    `base`/`token` se předávají **přímo**, ne přes nastavení: dokud je otevřený
    dialog nastavení, Kodi v něm rozepsanou hodnotu drží zvlášť — `getSetting()`
    vrátí ještě tu uloženou, i když `setSetting()` už novou do políčka zapsal
    (`luna_find` → `luna_check` na to doplatilo hláškou „adrese nerozumím“ nad
    adresou, kterou právě samo našlo a vyplnilo).
    """
    base = setting("luna_url") if base is None else base
    token = setting("token") if token is None else token
    if ask:
        # Kodi nedá akci to, co má uživatel rozepsané v políčku (uloží se až na OK),
        # takže se na adresu ptáme rovnou tady — nastavení se kvůli ověření nemusí
        # ukládat vůbec. Předvyplněná je ta uložená, takže „OK" stačí beze změny.
        zadano = xbmcgui.Dialog().input(L(30556, "Adresa Luny (nebo celá adresa doplňku ze /setup)"),
                                        defaultt=base)
        if not zadano:
            return
        base, token = zadano, parse_token(token) or token
    dialog = xbmcgui.DialogProgress()
    dialog.create(L(30000, "Nokturno"), L(30533, "Ověřuji Lunu…"))
    try:
        result = luna_diagnose(base, token)
    except Exception as e:  # noqa: BLE001 – ať tlačítko nikdy nespadne bez vysvětlení
        result = {"level": "fail", "code": "unreachable", "base": base, "token": "",
                  "version": "", "detail": str(e)}
    finally:
        dialog.close()

    # co jde spravit za uživatele, spravíme rovnou: celá adresa vložená do pole
    # tokenu (nejčastější vložení ze /setup) se rozdělí na adresu a token
    if result.get("base") and result["base"] != setting("luna_url"):
        ADDON.setSetting("luna_url", result["base"])
    if result.get("token") and result["token"] != setting("token"):
        ADDON.setSetting("token", result["token"])

    sid, fallback = LUNA_DIAG_TEXTS.get(result["code"], LUNA_DIAG_TEXTS["unreachable"])
    hodnoty = {"version": result.get("version") or "?", "base": result.get("base") or setting("luna_url")}
    try:
        text = L(sid, fallback) % tuple(hodnoty[k] for k in LUNA_DIAG_ARGS.get(result["code"], ()))
    except TypeError:   # překlad se zástupnými symboly nesouhlasí — radši holý text než pád
        text = L(sid, fallback)
    # stav slovem, ne symbolem: fonty skinů Kodi znaky jako ✔/✘ většinou nemají a
    # nakreslí místo nich prázdný proužek (totéž řeší EMOJI_MAP v jádru u popisků Luny).
    # Barvy hexem jako ostatní štítky — pojmenované („green“) závisí na skinu.
    mark = {"ok": f"[COLOR FF7BC96F]{L(30552, 'V pořádku')}[/COLOR][CR]",
            "warn": f"[COLOR FFF2C14E]{L(30553, 'Pozor')}[/COLOR][CR]"}.get(
        result["level"], f"[COLOR FFFF8A6B]{L(30554, 'Nepovedlo se')}[/COLOR][CR]")
    if result.get("detail") and result["level"] != "ok":
        xbmc.log(f"[Nokturno] diagnostika Luny: {result['code']} – {result['detail']}", xbmc.LOGINFO)

    if result["level"] == "ok":
        xbmcgui.Dialog().ok(L(30534, "Ověření Luny"), f"{mark}{text}")
        return
    # Zadat adresu je první volba schválně: ověřuje se uložené nastavení, takže
    # hodnota právě přepsaná v políčku (bez OK) se sem jinak nedostane
    volba = xbmcgui.Dialog().yesnocustom(
        L(30534, "Ověření Luny"), f"{mark}{text}[CR][CR]" + L(30557, "Ověřuje se uložené nastavení — co jsi "
                                                                    "právě přepsal v políčku, se počítá až po OK. "
                                                                    "Jinou adresu můžeš zadat rovnou tady."),
        customlabel=L(30536, "Poslat log"), nolabel=L(30537, "Zavřít"), yeslabel=L(30555, "Zadat adresu"))
    if volba == 1 and kolo < 3:      # Zadat adresu → zkusit znovu s ní
        nova = xbmcgui.Dialog().input(L(30556, "Adresa Luny (nebo celá adresa doplňku ze /setup)"),
                                      defaultt=result.get("base") or base)
        if nova:
            # token nechat čistý: kdyby v něm zůstala stará celá adresa, přebila by
            # tuhle zadanou (`diagnose` bere adresu přednostně z tokenu)
            luna_check(nova, parse_token(token) or token, kolo + 1)
    elif volba == 2:                 # Poslat log
        log_send(ask=False)


SPEEDTEST_URL = "https://speed.cloudflare.com/__down?bytes=52428800"  # 50 MB, i na rychlém připojení stačí pár vteřin
SPEEDTEST_SECONDS = 8       # déle nemá smysl čekat, průměr se stejně ustálí dřív
SPEEDTEST_RESERVE = 0.25    # rezerva, aby přehrávání nezasekávalo při kolísání rychlosti


def speedtest():
    """Tlačítko v nastavení: změří rychlost stahování a uloží ji jako
    dovolený datový tok s 25% rezervou — viz `Engine._effective_max_gb`, kde
    se teprve pro konkrétní titul a jeho stopáž mění na GB.
    """
    dialog = xbmcgui.DialogProgress()
    dialog.create(L(30000), L(30402, "Měřím rychlost stahování…"))
    got, t0 = 0, time.time()
    try:
        req = urllib.request.Request(SPEEDTEST_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            while True:
                if dialog.iscanceled():
                    dialog.close()
                    return
                chunk = resp.read(262144)
                if not chunk:
                    break
                got += len(chunk)
                elapsed = time.time() - t0
                dialog.update(min(99, int(elapsed / SPEEDTEST_SECONDS * 100)), f"{got / 2**20:.0f} MB")
                if elapsed >= SPEEDTEST_SECONDS:
                    break
    except Exception as e:  # noqa: BLE001 – síť, DNS, vypršelý čas
        dialog.close()
        notify(f"{L(30221, 'Měření rychlosti selhalo')}: {str(e)[:80]}", xbmcgui.NOTIFICATION_ERROR, 5000)
        return
    dialog.close()
    elapsed = max(time.time() - t0, 0.5)
    if got < 512 * 1024:
        # míň než půl megabajtu je jen šum (pomalé DNS, krátké přerušení) — s tím se nepočítá
        notify(L(30222, "Stáhlo se moc málo dat, zkus to znovu"), xbmcgui.NOTIFICATION_WARNING, 5000)
        return
    mbps = got * 8 / elapsed / 1_000_000
    allowed_mbps = round(mbps * (1 - SPEEDTEST_RESERVE), 1)
    ADDON.setSetting("max_bitrate_mbps", str(allowed_mbps))
    example_gb = allowed_mbps * 1_000_000 * DEFAULT_RUNTIME_S / 8 / 2 ** 30
    notify(Lf(30223, f"{mbps:.0f}", f"{allowed_mbps:g}", f"{example_gb:.1f}"), xbmcgui.NOTIFICATION_INFO, 7000)


def update_repos():
    """Tlačítko v nastavení: kontrola repozitářů hned, ne až podle plánu Kodi — po
    vydání (hlavně bety) se jinak čeká klidně den, než Kodi aktualizaci nabídne."""
    xbmc.executebuiltin("UpdateAddonRepos")
    notify(L(30408, "Kontroluji aktualizace doplňků…"))


TMDBH_ID = "plugin.video.themoviedb.helper"
# složka vlastních playerů TMDb Helperu (`PLAYERS_BASEDIR_USER` v jeho lib/addon/consts.py)
TMDBH_PLAYER = f"special://profile/addon_data/{TMDBH_ID}/players/nokturno.json"
TMDBH_PLAYER_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "players", "nokturno.json")


def tmdbhelper_installed():
    return bool(xbmc.getCondVisibility(f"System.HasAddon({TMDBH_ID})"))


def install_tmdbhelper_player(set_default=True):
    """Uloží player Nokturna do TMDb Helperu a volitelně ho nastaví jako výchozí přehrávač
    filmů i dílů. False, když TMDb Helper chybí nebo se soubor nepodařilo zapsat.
    Sdílí ho tlačítko v nastavení (`tmdbhelper_player`) i průvodce prvním nastavením."""
    if not tmdbhelper_installed():
        return False
    dest = xbmcvfs.translatePath(TMDBH_PLAYER)
    xbmcvfs.mkdirs(os.path.dirname(dest))
    if not xbmcvfs.copy(TMDBH_PLAYER_SRC, dest):
        return False
    if set_default:
        # hodnota ve tvaru, jaký ukládá TMDb Helper sám (`<soubor> <režim>`, config/default.py).
        # `System.HasAddon` výš hlásí i vypnutý doplněk, ale `xbmcaddon.Addon()` na vypnutý
        # doplněk spadne — RuntimeError bereme stejně jako "TMDb Helper tu není".
        try:
            tmdbh = xbmcaddon.Addon(TMDBH_ID)
        except RuntimeError:
            return False
        tmdbh.setSetting("default_player_movies", "nokturno.json play_movie")
        tmdbh.setSetting("default_player_episodes", "nokturno.json play_episode")
    return True


def tmdbhelper_player():
    """Tlačítko v nastavení: „Přehrát“ v detailu filmu nebo dílu z TMDb Helperu (Arctic Fuse
    a další skiny ho používají pro info stránky) pustí hledání streamů v Nokturnu.

    TMDb Helper přehrává přes JSON „playery“ ve své složce — bez nich Přehrát v detailu nic
    z Nokturna nevyvolá. Player jde podle IMDb id (u dílu id seriálu + sezóna a díl), takže
    nehledá podle názvu. Nainstalovaný soubor pak po aktualizacích drží aktuální služba."""
    if not tmdbhelper_installed():
        notify(L(30410, "TMDb Helper není nainstalovaný"), xbmcgui.NOTIFICATION_WARNING)
        return
    set_default = xbmcgui.Dialog().yesno(L(30000, "Nokturno"), L(30411, "Nastavit Nokturno jako výchozí přehrávač v "
                                                                        "TMDb Helperu? Přehrát v detailu filmu nebo "
                                                                        "dílu pak rovnou hledá streamy v Nokturnu."))
    if not install_tmdbhelper_player(set_default):
        notify(L(30413, "Přidání do TMDb Helperu selhalo"), xbmcgui.NOTIFICATION_ERROR)
        return
    notify(L(30412, "Nokturno je v TMDb Helperu"))


def prefetch(apis, kind):
    """Zahřátí cache — volá služba na pozadí, nic se nevypisuje ani nepočítá.

    Katalogy zahřívá služba přes běžný výpis (Files.GetDirectory), protože ten
    projde i doplněním popisů; tohle je jen pro streamy dalších dílů, kde by
    běžná cesta (`pick_title`) započítala zobrazení do statistik.
    """
    if kind == "next":
        seen = set()
        for key, _entry in STORE.recently_watched(15):
            if should_stop():
                break   # Kodi končí — každý titul je vlastní hledání napříč zdroji, tady se nic necachuje napůl
            snap = STORE.item(key)
            if not snap or snap.get("season") is None or snap.get("series") in seen:
                continue
            seen.add(snap.get("series"))
            found = next_episode(apis, snap)
            if not found:
                continue
            video, meta = found
            ep_id = video.get("id") or f"{snap['series']}:{video.get('season')}:{video.get('episode')}"
            if STORE.playcount(ep_id):
                continue
            try:
                collect_streams(apis, "series", ep_id, meta, snap.get("alt"))
            except Errors as e:
                log_error(f"prefetch {ep_id}: {e}")
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)


def stats_sources():
    """Které zdroje jsou v nastavení aktivní — do statistik, bez účtů a adres.
    Tentýž výčet skládá služba (`service.stats_context`), tohle je ruční odeslání."""
    return [name for name, active in (
        ("luna", on("luna_enabled") and bool(setting("token").strip())),
        ("sosac", on("sosac_enabled") and bool(setting("streamuj_username").strip())),
        ("webshare", on("ws_enabled", "false") and bool(setting("ws_username").strip())),
        ("hellspy", on("hs_enabled", "false")),
        ("sledujteto", on("st_enabled", "false") and bool(setting("st_email").strip())),
        ("fastshare", on("fs_enabled", "false") and bool(setting("fs_username").strip())),
        ("tmdb", bool(setting("tmdb_api_key").strip())),
        ("trakt", on("trakt_enabled", "false")),
    ) if active]


def stats_send():
    """Ruční odeslání statistik z nastavení – jinak je posílá služba na pozadí."""
    from stats import COLLECT_URL, Stats
    ok, why = Stats(PROFILE).send(COLLECT_URL, version=ADDON.getAddonInfo("version"),
                                  sources=stats_sources(), product="kodi")
    notify(L(30165) if ok else f"{L(30166)}: {why}",
           xbmcgui.NOTIFICATION_INFO if ok else xbmcgui.NOTIFICATION_ERROR, 5000)


LOG_TAIL_BYTES = 500 * 1024   # celý xbmc.log bývá desítky MB, server bere jen ~500 KB


def log_send(ask=True):
    """Ruční odeslání Kodi logu z nastavení – poslední ~500 KB `kodi.log`, gzip.

    Instance id bere ze stejného `stats.json` jako statistiky, ať jde log
    v dashboardu spárovat s instalací. Vlastní endpoint (`/logs`, ne `/collect`)
    bere syrová gzip data v těle, ne JSON — soubor je řádově větší.
    """
    if ask and not xbmcgui.Dialog().yesno(L(30000, "Nokturno"), L(30431, "Opravdu odeslat log?")):
        return

    import gzip
    from stats import COLLECT_URL, Stats

    log_path = xbmcvfs.translatePath("special://logpath/kodi.log")
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            if size > LOG_TAIL_BYTES:
                f.seek(size - LOG_TAIL_BYTES)
            raw = f.read()
    except OSError as e:
        notify(f"{L(30430)}: {e}", xbmcgui.NOTIFICATION_ERROR, 5000)
        return

    body = gzip.compress(raw)
    install_id = Stats(PROFILE).data["id"]
    version = ADDON.getAddonInfo("version")
    logs_url = COLLECT_URL.rsplit("/", 1)[0] + "/logs"
    req = urllib.request.Request(
        f"{logs_url}?id={install_id}&version={urllib.parse.quote(version)}",
        data=body, method="POST",
        headers={"Content-Type": "application/gzip", "User-Agent": f"Kodi plugin.video.nokturno/{version}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read(1024)
        notify(L(30429), xbmcgui.NOTIFICATION_INFO, 5000)
    except Exception as e:  # noqa: BLE001 – HTTPError, URLError, timeout… vše skončí stejně
        notify(f"{L(30430)}: {e}", xbmcgui.NOTIFICATION_ERROR, 5000)


CRASH_PROP = "nokturno.crash"     # plugin → služba: ve frontě je nové hlášení o pádu, poslat hned
CRASH_LOG_TAIL = 256 * 1024          # kolik z konce kodi.log projít kvůli řádkům Nokturna


def crash_reports_on(addon=None):
    """Hlášení o pádech jen se zapnutými statistikami a nevypnutým přepínačem (výchozí zapnuto)."""
    addon = addon or ADDON
    return addon.getSetting("stats_enabled") == "true" and addon.getSetting("crash_reports") != "false"


def kodi_platform():
    return next((name for name, cond in (
        ("Android", "System.Platform.Android"), ("Linux", "System.Platform.Linux"),
        ("Windows", "System.Platform.Windows"), ("macOS", "System.Platform.OSX"),
        ("iOS", "System.Platform.IOS"), ("tvOS", "System.Platform.TVOS"),
    ) if xbmc.getCondVisibility(cond)), "?")


def addon_log_lines():
    """Poslední řádky kodi.log od Nokturna — kontext k pádu. Cizí doplňky do hlášení nepatří."""
    try:
        path = xbmcvfs.translatePath("special://logpath/kodi.log")
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > CRASH_LOG_TAIL:
                f.seek(size - CRASH_LOG_TAIL)
            raw = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return []
    return [line for line in raw.splitlines() if ADDON_ID in line]


def report_crash(action, exc):
    """Neočekávaná výjimka z routeru → fronta hlášení (`crash.py`); odešle ji služba.
    Nikdy nevyhodí výjimku a nečeká na síť — plugin už handle zavřel."""
    try:
        if not crash_reports_on():
            return
        from stats import Stats
        queued = CrashReporter(PROFILE).capture(
            exc, Stats(PROFILE).data["id"], "kodi", ADDON.getAddonInfo("version"),
            platform=kodi_platform(), kodi=xbmc.getInfoLabel("System.BuildVersionShort"),
            action=action or "", log_lines=addon_log_lines())
        if queued:
            xbmcgui.Window(10000).setProperty(CRASH_PROP, "1")
    except Exception as e:  # noqa: BLE001 – hlášení o pádu nesmí shodit úklid po pádu
        xbmc.log(f"[{ADDON_ID}] hlášení o pádu nezařazeno: {e}", xbmc.LOGWARNING)


def website_info():
    """Zobrazí odkaz na web rodiny Nokturno (podpora, Stremio, Home Assistant)."""
    xbmcgui.Dialog().ok(L(30432, "Info"), f"https://nokturno.tailf0014.ts.net/\n\n{L(30434)}")


# --- novinky ve verzi -------------------------------------------------------------

SEEN = "seen"   # seen.json v profilu: {"version": naposledy odbavená verze}


def _vkey(text):
    """Pořadí verzí jako v Kodi (`CAddonVersion`) a v `tools/build_repo.version_key`:
    část za „~“ řadí před stejnou verzi bez ní — „3.2.0~beta1“ < „3.2.0“."""
    main, _, tag = str(text or "").partition("~")
    nums = tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.\-+]", main))
    tag_key = tuple(int(p) if p.isdigit() else p for p in re.findall(r"\d+|\D+", tag))
    return nums, (0, tag_key) if tag else (1, ())


def parse_news(text, since=None):
    """Řádky „verze – text“ z <news>, od nejnovější; se `since` jen novější než ta verze.
    Beta verze (`3.2.0~beta1 – …`) se počítají taky — dřív je regex vynechal a beta
    uživatelé Novinky neviděli."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        m = re.match(r"^(\d+(?:\.\d+)*(?:~[a-z]+\d*)?)\s*[–-]\s*(.+)$", line)
        if not m:
            continue
        if since and _vkey(m.group(1)) <= _vkey(since):
            continue
        out.append((m.group(1), m.group(2)))
    return out


def changelog_lines(since=None):
    """Řádky <news> z addon.xml novější než `since`, od nejnovější.

    Changelog se nedrží zvlášť — <news> v addon.xml je ten, který Kodi ukazuje
    v informacích o doplňku, takže druhý seznam by se rozešel. Každý řádek
    začíná verzí, podle ní se i filtruje.
    """
    try:
        import xml.etree.ElementTree as ET
        news = ET.parse(os.path.join(ADDON_PATH, "addon.xml")).getroot().find(".//news")
        text = (news.text or "") if news is not None else ""
    except Exception:  # noqa: BLE001 – bez changelogu se nic neděje
        return []
    return parse_news(text, since)


def current_version():
    """Verze z addon.xml na disku — ze stejného souboru, ze kterého se čte changelog.

    `ADDON.getAddonInfo("version")` vrací verzi, kterou si Kodi načetlo při startu.
    Když se addon.xml změní bez restartu (ruční nahrání na box), obě čísla se
    rozejdou: „přečteno" se uložilo jako starší verze a řádek novější verze
    v changelogu pořád prošel filtrem — položka „Novinky ve verzi" po zavření
    nezmizela, dokud se Kodi nerestartovalo.
    """
    try:
        import xml.etree.ElementTree as ET
        version = ET.parse(os.path.join(ADDON_PATH, "addon.xml")).getroot().get("version")
        if version:
            return version
    except Exception:  # noqa: BLE001 – bez addon.xml aspoň to, co ví Kodi
        pass
    return ADDON.getAddonInfo("version")


def unseen_changelog():
    """Co uživatel po aktualizaci ještě neviděl. Při první instalaci nic —
    jinak by novinky vyskočily hned každému novému uživateli."""
    version = current_version()
    seen = (STORE.load(SEEN, {}) or {}).get("version")
    if not seen:
        STORE.save(SEEN, {"version": version})
        return []
    return changelog_lines(seen)


def whats_new():
    """Stručný changelog po aktualizaci. Modální okno je tu v pořádku — jde
    o reakci na kliknutí, ne o něco, co se otevře samo (to by při volání
    z widgetu nebo JSON-RPC čekalo na OK a zablokovalo i vypínání Kodi)."""
    lines = unseen_changelog() or changelog_lines()[:5]
    groups = []
    for v, text in lines:
        if groups and groups[-1][0] == v:
            groups[-1][1].append(text)
        else:
            groups.append((v, [text]))
    body = "\n\n".join(f"[B]{v}[/B]\n" + "\n".join(f"• {t}" for t in texts) for v, texts in groups) \
        or L(30193, "Žádné novinky")
    STORE.save(SEEN, {"version": current_version()})
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
    xbmcgui.Dialog().textviewer(L(30191, "Novinky"), body)
    xbmc.executebuiltin("Container.Refresh")


# --- obrazovky --------------------------------------------------------------------

def main_menu(apis):
    mark_used()
    if not any(apis.values()):
        # bez modálního dialogu: ten by při volání z widgetu/JSON-RPC čekal na OK a zablokoval i vypínání Kodi
        notify(L(30104), xbmcgui.NOTIFICATION_WARNING, 6000)
        folder_item(L(30107), build_url(action="settings"), icon="DefaultAddonProgram.png")
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    # čistá instalace: průvodce nahoře jako položka. Spouštět ho z kořene sám od sebe
    # nejde — modální dialog v cestě, kterou otevírají widgety a JSON-RPC, blokuje
    # i vypínání Kodi (viz pravidlo v CLAUDE.md)
    if not STORE.load("wizard_done", False) and not accounts_set():
        folder_item(L(30359, "Průvodce nastavením"), build_url(action="setup_wizard"),
                    icon="DefaultAddonProgram.png")
    fresh = unseen_changelog()
    if fresh:
        folder_item(f"{L(30192, 'Novinky ve verzi')} {fresh[0][0]}",
                    build_url(action="whats_new"), icon="DefaultAddonRepository.png")
    # vlastní ikony místo jedné a té samé ikony doplňku u každé položky — jména
    # standardní sady Kodi (dodává je aktivní skin, žádný soubor navíc v doplňku)
    # Jedno hledání, jedny Filmy a jedny Seriály — dřív tu byly Filmy/Seriály zvlášť za
    # každý zdroj katalogu (Luna, Sosáč, databáze), každé s vlastními podkategoriemi.
    # Který zdroj stojí za kterým seznamem, rozhoduje až `browse_menu`.
    folder_item(L(30150, "Hledat"), build_url(action="search", type="any"),
               icon="DefaultAddonsSearch.png", context=[(L(30106), runplugin(action="clear_cache"))])
    if STORE.in_progress() or STORE.recently_watched(1):
        folder_item(L(30063), build_url(action="continue"), icon="DefaultInProgressShows.png")
    folder_item(L(30012), build_url(action="browse", type="movie"), icon="DefaultMovies.png")
    folder_item(L(30013), build_url(action="browse", type="series"), icon="DefaultTVShows.png")
    # sezónní a tematické katalogy zapnuté na dashboardu (bez vydání nové verze)
    dash_catalog_items(apis, "root")
    folder_item(L(30483, "TV program"), build_url(action="tv"), icon="DefaultAddonPVRClient.png")
    folder_item(L(30060), build_url(action="favourites"), icon="DefaultFavourites.png")
    if apis.get("dav"):
        folder_item(L(30387, "Moje úložiště"), build_url(action="dav_browse"), icon="DefaultHardDisk.png")
    if setting("download_dir") or sync_settings():
        folder_item(L(30391, "Stažené"), build_url(action="downloads"), icon="DefaultHardDisk.png")
    folder_item(L(30392, "Nastavení"), build_url(action="settings"), icon="DefaultAddonProgram.png")
    # bez cache na disk — položky se mění podle stavu (Novinky, Pokračovat), zpět do
    # menu z podsložky by jinak Kodi ukázalo starý výpis i s už přečtenými Novinkami
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def browse_menu(apis, ctype):
    """Filmy / Seriály: seznamy bez ohledu na zdroj. Zdroj vybírá doplněk sám —
    TMDB (vlastní klíč), jinak Luna, jinak Cinemeta; „s CZ dabingem“ je z veřejného
    katalogu Sosáče, protože český dabing pozná jen on. „Podle písmene“ vypadlo
    (2026-09-14) — 100 položek bez popisů, pomalé a nikdo ho neprocházel.

    „Populární na TMDB“ a „Nejlépe hodnocené“ jdou přes `action="genres"`
    (2026-09-15) — obě mají u zdroje (TMDB/Luna/Cinemeta) seznam žánrů, `list_genres()`
    nabídne „Vše“ i jednotlivé žánry, teprve pak se sáhne na `list_catalog()` se
    zvoleným `genre=`. „Nejsledovanější tento týden“ (dashboard) a „Nově přidané
    s CZ dabingem/titulky“ (živá kontrola ze Sosáče) žánr u položek nemají, zůstávají
    tedy jako přímý `action="catalog"`/`"lang_catalog"`."""
    kind = "series" if ctype == "series" else "movie"
    tmdb, luna, cinemeta, sosac = apis.get("tmdb"), apis.get("luna"), apis.get("cinemeta"), apis.get("sosac_db")

    def pick(tmdb_cid, luna_cid, cinemeta_cid):
        if tmdb and tmdb_cid:
            return "tmdb", tmdb_cid, None
        if luna and luna_cid:
            return "luna", luna_cid, None
        if cinemeta and cinemeta_cid:
            return "cinemeta", cinemeta_cid, None
        return None

    rows = [
        (L(30398, "Populární na TMDB"), "genres", pick("popular", f"tmdb.top_{kind}", "top"), "DefaultMovies.png"),
        (L(30393, "Nejsledovanější tento týden"), "catalog", ("trend", TREND_CATALOG_ID, None),
         "DefaultFavourites.png"),
        (L(30399, "Nejlépe hodnocené"), "genres", pick("top_rated", f"tmdb.top_rated_{kind}", "imdbRating"),
         "DefaultMusicTop100.png"),
        (L(30394, "Nově přidané s CZ dabingem"), "lang_catalog", ("dub", None, None)
         if sosac else None, "DefaultRecentlyAddedMovies.png"),
        (L(30401, "Nově přidané s CZ titulky"), "lang_catalog", ("subs", None, None)
         if sosac else None, "DefaultRecentlyAddedMovies.png"),
    ]
    for label, action, target, icon in rows:
        if not target:
            continue
        if action == "lang_catalog":
            # `lang_catalog_menu` (ne rovnou `lang_catalog`) — z menu nechceme spustit
            # drahý živý přepočet automaticky, viz `lang_catalog_menu()` níž
            want, _, _ = target
            folder_item(label, build_url(action="lang_catalog_menu", type=ctype, want=want), icon=icon)
            continue
        src, cid, genre = target
        params = {"action": action, "type": ctype, "catalog": cid, "src": src}
        if genre:
            params["genre"] = genre
        folder_item(label, build_url(**params), icon=icon)
    dash_catalog_items(apis, "browse", kind)
    xbmcplugin.endOfDirectory(HANDLE)


def list_catalogs(apis, ctype, src):
    api = apis[src]
    if api is None:
        raise LunaError(L(30104))
    for c in api.catalogs(ctype):
        # Luna má hledání jako vlastní katalog `search.movie`/`search.series` — ten
        # do procházení nepatří. Cinemeta narozdíl od Luny umí `search=` extra i na
        # běžných katalozích (`top`, `imdbRating`), takže se pozná jen podle id.
        if c["id"].startswith("search"):
            continue
        action = "genres" if c["genres"] else "catalog"
        folder_item(c["name"], build_url(action=action, type=ctype, catalog=c["id"], src=src),
                   icon="DefaultVideoPlaylists.png")
    xbmcplugin.endOfDirectory(HANDLE)


# u „Trendy" nejsou v roli žánru žánry, ale časové okno TMDB — hodnota musí zůstat anglicky
GENRE_LABELS = {"Day": L(30403, "Za den"), "Week": L(30404, "Za týden")}
# žánry česky jen pro češtinu a slovenštinu — anglické Kodi dřív dostávalo české názvy natvrdo
GENRES_LOCAL = xbmc.getLanguage(xbmc.ISO_639_1) in ("cs", "sk")


def genre_label(g):
    return GENRES_CS.get(str(g), str(g)) if GENRES_LOCAL else str(g)


def list_genres(apis, ctype, cid, src, show_all=True):
    api = apis[src]
    cat = next((c for c in api.catalogs(ctype) if c["id"] == cid), None) if api else None
    if not cat:
        xbmcplugin.endOfDirectory(HANDLE)
        return
    if show_all and not cat["genre_required"]:
        folder_item(L(30020), build_url(action="catalog", type=ctype, catalog=cid, src=src),
                   icon="DefaultVideoPlaylists.png")
    for g in cat["genres"]:
        folder_item(GENRE_LABELS.get(g, genre_label(g)),
                    build_url(action="catalog", type=ctype, catalog=cid, genre=g, src=src),
                    icon="DefaultGenre.png")
    xbmcplugin.endOfDirectory(HANDLE)


def list_catalog(apis, ctype, cid, src, genre=None, search=None, skip=0):
    api = apis[src]
    if api is None:
        raise LunaError(L(30104))
    set_content("tvshows" if ctype == "series" else "movies")
    metas = api.catalog(ctype, cid, genre=genre, search=search, skip=skip)
    if src in ("sosac", "sosac_db", "cinemeta"):
        # exporty Sosáče a holé výpisy Cinemety nemají popis → dotáhnout podle IMDb id
        # (Luna, jinak Cinemeta sama — viz `_fetch()` v enrich.py — cache)
        enrich(metas, apis["luna"], STORE, ctype)
    for m in metas:
        add_meta_item(m, ctype)
    # Luna vrací stránky po ~20, ale některé katalogy o pár položek méně; žebříček a katalogy
    # z dashboardu přijdou celé najednou, „Další“ by vedlo do prázdné složky
    if len(metas) >= PAGE // 2 and src not in ("trend", "dash"):
        folder_item(L(30021), build_url(action="catalog", type=ctype, catalog=cid, src=src, genre=genre,
                                        search=search, skip=skip + len(metas)), icon="DefaultFolder.png")
    # widget a výpis v Nokturnu mívají stejnou adresu, položky se ale liší podle okna (add_playable)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def lang_catalog_menu(apis, ctype, want):
    """Vstupní bod z menu (`browse_menu()`) pro „Nově přidané s CZ dabingem/titulky" —
    NE rovnou `list_lang_catalog()`. Ten dřív spouštěl živý přepočet (klidně několik
    minut) automaticky, jen s bublinou postupu — uživatel se ale sám ptal, ať to
    potvrdí, ne aby to vidí až za pochodu (2026-09-15).

    Modální ano/ne dialog nejde použít (viz pravidlo v CLAUDE.md — cesta jde
    spustit i z widgetu/JSON-RPC, `warming()` pozná jen náš vlastní zahřívač na
    pozadí, ne cizí volání), takže se místo otázky nabídne obyčejná položka
    seznamu — klik na ni požádá `service.py` o přepočet na pozadí a nechá
    uživatele jít dál (`lang_catalog_trigger()`, s notifikací až bude hotovo),
    místo aby ho nutil čekat na místě.

    Cache hotová → rovnou seznam. Nic jiného ale NEjde přes `list_lang_catalog()`
    (jak to bylo dřív, „ať si počká na cizí zámek") — `LANG_LOCK_WAIT` (90 s) bývá
    kratší než reálná doba běhu (až ~4 min u 60 kandidátů, viz `LANG_LOCK_STALE`),
    takže tohle čekání skoro vždycky vypršelo dřív, než cizí výpočet doběhl, a
    `_lang_catalog_locked()` pak vrátil tichý prázdný seznam (2026-09-15, nahlásil
    uživatel — „zahřívání nových dílů a filmů", „prázdné seznam"). Cizí výpočet
    (zahřívač, nebo dřívější `lang_catalog_trigger()`) proto jen ohlásíme stejnou
    zprávou jako čerstvě zadanou žádost — doběhne sám, notifikace přijde, až bude
    hotovo, tady se na nic nečeká."""
    key = f"lang_catalog:{ctype}"
    win = xbmcgui.Window(10000)
    age = _lang_lock_age(win, f"{LANG_LOCK_PROP}:{key}")
    if STORE.peek_cached(key, LANG_CATALOG_TTL) is not None:
        list_lang_catalog(apis, ctype, want)
        return
    set_content("tvshows" if ctype == "series" else "movies")
    if age is not None and age < LANG_LOCK_STALE:
        label = L(30438, "Started in the background — you'll get a notification when it's ready.")
        progress = _lang_progress_text(win.getProperty(f"{LANG_PROGRESS_PROP}:{key}"))
        if progress:
            label = f"{label} ({progress})"
        folder_item(label, build_url(action="lang_catalog_menu", type=ctype, want=want),
                    icon="DefaultAddonsSearch.png")
    else:
        folder_item(L(30437, "Data aren't ready — checking dubbing/subtitles across your sources can take a "
                              "few minutes. Tap to start."),
                    build_url(action="lang_catalog_trigger", type=ctype, want=want), icon="DefaultAddonsSearch.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def lang_catalog_trigger(apis, ctype, want):
    """Klik na položku „Klepni pro spuštění" z `lang_catalog_menu()` — sám nic
    nepočítá (to by uživatele nechalo čekat přesně tomu, čemu se chtěl vyhnout),
    jen požádá `service.py` o přepočet na pozadí zápisem do `LANG_TRIGGER_PROP`
    (`lang_trigger_watcher()` tam ji čte a hned reaguje, ne až za `LANG_WARM_EVERY`)
    a hned se vrátí. Až bude hotovo, přijde `xbmcgui.Dialog().notification()`
    (nemodální, bezpečná odkudkoli i z pozadí).

    NE `list_lang_catalog()`, i kdyby cache mezitím zůstala prázdná a zámek byl
    cizí — to je přesně ta blokující cesta s tichým prázdným seznamem, co řeší
    `lang_catalog_menu()` (viz tam, `LANG_LOCK_WAIT` vs. reálná doba běhu)."""
    key = f"lang_catalog:{ctype}"
    win = xbmcgui.Window(10000)
    age = _lang_lock_age(win, f"{LANG_LOCK_PROP}:{key}")
    if STORE.peek_cached(key, LANG_CATALOG_TTL) is not None:
        list_lang_catalog(apis, ctype, want)
        return
    if age is None or age >= LANG_LOCK_STALE:
        # nikdo to zrovna nepočítá (zahřívač ani dřívější žádost) — teprve teď o to požádat
        win.setProperty(f"{LANG_TRIGGER_PROP}:{ctype}", "1")
    set_content("tvshows" if ctype == "series" else "movies")
    label = L(30438, "Started in the background — you'll get a notification when it's ready.")
    progress = _lang_progress_text(win.getProperty(f"{LANG_PROGRESS_PROP}:{key}"))
    if progress:
        label = f"{label} ({progress})"
    folder_item(label, build_url(action="lang_catalog_menu", type=ctype, want=want),
                icon="DefaultAddonsSearch.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_lang_catalog(apis, ctype, want):
    """Nově přidané s CZ dabingem / titulky — na rozdíl od Sosáčova vlastního exportu
    (ten u filmů jazyk rozliší jen napůl spolehlivě, viz cache staleness; u seriálů
    vůbec) se jazyk ověřuje živě: kandidáti jsou surové „nově přidané" ze Sosáče,
    ale co je doopravdy dabing/titulky se zjišťuje stejně jako v detailu titulu —
    přes `Engine.raw_streams()` napříč VŠEMI zdroji, které má uživatel zapnuté (jeden
    zdroj tvrdí anglicky s anglickými titulky neznamená, že jiný zdroj nemá dabing).
    Sosáč/Luna mají jazyk přímo v popisku (levné), WebShare/HellSpy/Sledujteto/
    FastShare ho jádro odhadne z názvu (taky levné, jen text) — `probe_audio=False`
    ale vynechá poslední, nejdražší krok, čtení hlaviček souboru přes síť
    (`_fill_audio`), který by u desítek titulů byl neúnosně pomalý (odhad z popisku/
    názvu stačí na klasifikaci ano/ne, nemusí být ověřený jako ve skutečném dialogu
    streamů).

    Rychlost ověřena na Office (30/30 do minuty s `probe_audio=False`) — teď už se
    seznam cachuje na `LANG_CATALOG_TTL` (8 h, stejně jako ostatní katalogy) a
    zahřívá na pozadí (`lang_warmer()` ve `service.py`, každých 6 h — kratší než
    TTL, aby uživatel na živý přepočet nikdy nenarazil). `LANG_CATALOG_CAP`
    omezuje cenu i když se cíl nenaplní.

    Každé otevření pluginu je vlastní Python proces, takže se dvě souběžná
    vyvolání (uživatelův klik + zahřívání na pozadí, nebo dva kliky za sebou)
    nepoznají přes obyčejnou proměnnou — `_lang_catalog_locked()` proto řeší
    vzájemné vyloučení přes vlastnost okna (viz `LANG_LOCK_PROP`), sdílenou
    napříč procesy stejně jako `WARM_PROP`. Bez toho si dvě souběžná živá
    ověřování šlapala na zdroje navzájem (2026-09-15, uživatel narazil na
    zaseklý průběh přesně ve chvíli, kdy jsem zrovna testoval totéž JSON-RPC
    voláním).

    Dabing i titulky se počítají v JEDNOM průchodu kandidáty (`_build_lang_catalog()`,
    2026-09-15) — `raw_streams()` u kandidáta stejně řekne obojí najednou (`langs`
    i `subs`), počítat je zvlášť by jen dvakrát zaplatilo tu samou síťovou práci.
    Cache/zámek jsou proto společné pro celý `ctype` (`lang_catalog:{ctype}`), ne
    zvlášť pro dabing a titulky — otevření druhého seznamu hned po prvním je pak
    už jen čtení z cache, i když se předtím nikdy samostatně nepočítal."""
    set_content("tvshows" if ctype == "series" else "movies")
    key = f"lang_catalog:{ctype}"
    combined = _lang_catalog_locked(apis, ctype, key)
    matched = combined.get(want) or []
    t0 = time.time()
    filled = enrich(matched, apis.get("luna"), STORE, "movie")
    _diag(f"{key}: enrich {filled}/{len(matched)} za {time.time() - t0:.1f} s "
          f"(bez hodnocení: {sum(1 for m in matched if not m.get('imdbRating'))})")
    for m in matched:
        # `bare_title()` u Sosáčových id zobrazuje `_title` (ne `name`) — u filmů
        # jsou stejné, ale u dílů seriálu (`episode_meta()` v jádru) `_title` je
        # záměrně jen holý název seriálu (potřebuje ho hledání napříč zdroji výš,
        # v `_build_lang_catalog()`), zatímco `name` nese i sezónu/díl. Bez tohohle
        # se pod Seriály zobrazovalo desetkrát za sebou jen „Dogu“ bez rozlišení
        # (2026-09-15). Hledání už proběhlo, přepsat `_title` teď je bezpečné.
        m["_title"] = m.get("name") or m.get("_title")
        add_meta_item(m, m.get("type") or "movie")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def _diag(msg):
    # DOČASNÉ (2026-09-15) – měření, kde přesně „Nově přidané s CZ dabingem/titulky"
    # ztrácí čas; smazat, až bude jasné, co je pomalé.
    xbmc.log(f"[{ADDON_ID}/DIAG] {msg}", xbmc.LOGWARNING)


def _lang_lock_age(win, prop):
    """Stáří zámku v sekundách, nebo `None`, když není zamčeno.

    Hodnota zámku je čas jeho vzniku (`time.time()`), ne jen „1" — Kodi umí
    neuposlechnuvší skript po 5 s natvrdo zabít (`script didn't stop in 5
    seconds`, typicky když uživatel odejde ze seznamu dřív, než doběhne) a
    takový proces se přes `finally` nikdy nedostane, aby zámek uklidil. Bez
    stáří by zámek zůstal navěky a každé další otevření by jen marně čekalo
    (2026-09-15, přesně tohle nahlásil uživatel: „úplně se to seklo")."""
    holder = win.getProperty(prop)
    if not holder:
        return None
    try:
        return time.time() - float(holder)
    except ValueError:
        return LANG_LOCK_STALE + 1  # neznámý formát → raději rovnou jako mrtvý zámek


def _lang_catalog_locked(apis, ctype, key):
    win = xbmcgui.Window(10000)
    prop = f"{LANG_LOCK_PROP}:{key}"
    progress_prop = f"{LANG_PROGRESS_PROP}:{key}"
    age = _lang_lock_age(win, prop)
    if age is not None and age < LANG_LOCK_STALE:
        _diag(f"{key}: zamčeno cizím výpočtem ({age:.0f}s), čekám")
        t0 = time.time()
        # bublina s postupem jen když čekání vidí uživatel — zahřívání na pozadí
        # (`warming()`) na výsledek nikdy nečeká vizuálně, jen ať se cache naplní
        _wait_for_lang_catalog(win, prop, progress_prop, show_bar=not warming())
        _diag(f"{key}: čekání skončilo po {time.time() - t0:.1f} s")
        age = _lang_lock_age(win, prop)
        if age is not None and age < LANG_LOCK_STALE:
            # pořád zamčeno živým výpočtem (čekání vypršelo dřív, než skončil) —
            # radši starý/prázdný výsledek než počítat znovu souběžně s ním
            return STORE.cached_if(key, float("inf"), lambda: {"dub": [], "subs": []})
        # jinak zámek mezitím zmizel (normální dokončení) nebo zestárl (mrtvý
        # proces) — v obou případech to zkusíme níž sami
    elif age is not None:
        _diag(f"{key}: zámek starý {age:.0f}s (zabitý/spadlý proces?), přebírám")
    win.setProperty(prop, str(time.time()))
    _diag(f"{key}: zamčeno mnou, warming={warming()}")
    t0 = time.time()
    try:
        # NE `fresh=warming()`: u ostatních katalogů (levný dotaz) dává smysl při
        # zahřívání vždy přepsat, tady je přepočet o řády dražší (desítky sekund
        # až minuty) — `fresh=True` by nutilo počítat znovu, i když cache má
        # sotva pár minut, přesně opak toho, proč tu 8h cache vůbec máme
        # (2026-09-15: nahlásil uživatel — druhé otevření nešlo z cache).
        return STORE.cached_if(key, LANG_CATALOG_TTL, lambda: _build_lang_catalog(apis, ctype))
    finally:
        win.clearProperty(prop)
        _diag(f"{key}: _lang_catalog_locked() hotovo za {time.time() - t0:.1f} s")


def _wait_for_lang_catalog(win, prop, progress_prop, show_bar):
    """Čeká, dokud zámek drží živý (dost čerstvý) proces — vrátí se hned, jakmile
    zmizí (normální dokončení) nebo zestárne (mrtvý proces), jinak nejdéle `LANG_LOCK_WAIT`.

    `show_bar=True` k tomu navíc ukazuje nemodální bublinu s postupem cizího
    výpočtu — ten svůj postup průběžně píše do `progress_prop` (`_build_lang_catalog()`),
    tady se jen čte a promítá, ať uživatel neschne u prázdné obrazovky beze zprávy."""
    monitor = xbmc.Monitor()
    bar = None
    if show_bar:
        bar = xbmcgui.DialogProgressBG()
        bar.create(L(30000, "Nokturno"), L(30436, "The list is being built in the background right now, "
                                                    "following the progress…"))
        bar.update(0)
    try:
        waited = 0
        while waited < LANG_LOCK_WAIT:
            age = _lang_lock_age(win, prop)
            if age is None or age >= LANG_LOCK_STALE:
                return
            if bar:
                _update_lang_bar(bar, win.getProperty(progress_prop))
            if monitor.waitForAbort(LANG_LOCK_POLL):
                return
            waited += LANG_LOCK_POLL
    finally:
        if bar:
            bar.close()


def _lang_progress_parts(raw):
    """`raw` = `"{dab}/{cíl}|{titulky}/{cíl}"`, jak ho píše `_build_lang_catalog()`.
    `None`, když je prázdný/nerozpoznatelný (výpočet ještě nezapsal první hodnotu,
    nebo mezitím zmizel) — volající pak nechá poslední známý stav beze změny."""
    if not raw:
        return None
    try:
        dub_part, subs_part = raw.split("|")
        dub_done, target = (int(x) for x in dub_part.split("/"))
        subs_done, _ = (int(x) for x in subs_part.split("/"))
    except (TypeError, ValueError):
        return None
    return dub_done, subs_done, target


def _lang_progress_text(raw):
    """Krátký text s aktuálním postupem („dabing 5/30 · titulky 2/30“) pro položku
    seznamu (`lang_catalog_menu()`/`lang_catalog_trigger()`) — `None`, když se nedá
    přečíst (viz `_lang_progress_parts()`)."""
    parts = _lang_progress_parts(raw)
    if not parts:
        return None
    dub_done, subs_done, target = parts
    dub_label = L(30394, "Nově přidané s CZ dabingem")
    subs_label = L(30401, "Nově přidané s CZ titulky")
    return f"{dub_label} {dub_done}/{target} · {subs_label} {subs_done}/{target}"


def _update_lang_bar(bar, raw):
    parts = _lang_progress_parts(raw)
    if not parts:
        return
    dub_done, subs_done, target = parts
    percent = int((dub_done + subs_done) / (2 * target) * 100) if target else 0
    bar.update(min(percent, 100), message=_lang_progress_text(raw))


def _build_lang_catalog(apis, ctype):
    """Živý výpočet pro `list_lang_catalog()` — samotné dohledání kandidátů se
    posílá do `Store.cached_if()`, proto je vytažené zvlášť. Bublina s postupem se
    ukazuje jen při skutečném, na uživateli viditelném přepočtu — při zahřívání na
    pozadí (`warming()`) by jen zbytečně blikala. I tehdy ale postup píšeme do
    `LANG_PROGRESS_PROP` (`win.setProperty` níž) — kdyby přesně v tu chvíli kliknul
    uživatel a čekal na zámek (`_wait_for_lang_catalog()`), potřebuje odkud číst.

    Dabing i titulky v JEDNOM průchodu (2026-09-15) — `raw_streams()` u kandidáta
    řekne obojí (`langs` i `subs`) za stejnou cenu, dělit to na dva běhy by jen
    zdvojnásobilo síťovou práci pro tutéž dvojici seznamů (dabing má navíc
    přednost před titulky, stejný titul nepatří do obou)."""
    t_start = time.time()
    engine = engine_of(apis)
    sosac = apis.get("sosac_db")
    raw_cid = "tvshowsrecentlyadded" if ctype == "series" else "moviesrecentlyadded"
    candidates = sosac.catalog(ctype, raw_cid, skip=0, page=LANG_CATALOG_CAP) if sosac else []
    _diag(f"{ctype}: {len(candidates)} kandidátů za {time.time() - t_start:.1f} s")
    dub_label = L(30394, "Nově přidané s CZ dabingem")
    subs_label = L(30401, "Nově přidané s CZ titulky")
    win = xbmcgui.Window(10000)
    progress_prop = f"{LANG_PROGRESS_PROP}:lang_catalog:{ctype}"
    bar = None if warming() else xbmcgui.DialogProgressBG()
    if bar:
        bar.create(L(30000, "Nokturno"), L(30435, "This list is normally built in the background, but the "
                                                    "data isn't ready yet. Checking dubbing/subtitles across "
                                                    "your sources, this can take a few minutes…"))
        bar.update(0)
    matched = {"dub": [], "subs": []}
    try:
        for i, cand in enumerate(candidates[:LANG_CATALOG_CAP]):
            if len(matched["dub"]) >= LANG_CATALOG_TARGET and len(matched["subs"]) >= LANG_CATALOG_TARGET:
                break
            if should_stop():
                # Kodi končí (zahřívání ze služby běží v tomhle skriptu a Kodi na něj
                # čeká) — výjimka, ne `break`: nedokončený seznam se nesmí zapsat do
                # 8h cache v `STORE.cached_if()`, a zámek uklidí `finally` výš
                _diag(f"{ctype}: přerušeno po {i} kandidátech, Kodi končí")
                raise Aborted()
            item_ctype = cand.get("type") or "movie"
            t_item = time.time()
            try:
                streams = engine.raw_streams(item_ctype, cand["id"], strict=True, probe_audio=False)
            except Exception as err:  # noqa: BLE001 – výpadek u jednoho kandidáta nesmí shodit celý seznam
                _diag(f"  [{i}] {cand.get('id')}: chyba za {time.time() - t_item:.1f} s ({err})")
                continue
            _diag(f"  [{i}] {cand.get('id')}: {len(streams)} streamů za {time.time() - t_item:.1f} s")
            langs, subs = set(), set()
            for s in streams:
                langs.update(s.get("langs") or [])
                subs.update(s.get("subs") or [])
            has_dub = bool(langs & CZECH_LANGS)
            has_subs = bool(subs & CZECH_LANGS)
            if has_dub and len(matched["dub"]) < LANG_CATALOG_TARGET:
                matched["dub"].append(cand)
            if has_subs and not has_dub and len(matched["subs"]) < LANG_CATALOG_TARGET:
                matched["subs"].append(cand)
            win.setProperty(progress_prop, f"{len(matched['dub'])}/{LANG_CATALOG_TARGET}|"
                                            f"{len(matched['subs'])}/{LANG_CATALOG_TARGET}")
            if bar:
                done = len(matched["dub"]) + len(matched["subs"])
                bar.update(int(done / (2 * LANG_CATALOG_TARGET) * 100),
                          message=f"{dub_label} {len(matched['dub'])}/{LANG_CATALOG_TARGET} · "
                                  f"{subs_label} {len(matched['subs'])}/{LANG_CATALOG_TARGET}")
    finally:
        win.clearProperty(progress_prop)
        if bar:
            bar.close()
    _diag(f"{ctype}: hotovo, dab {len(matched['dub'])} / tit {len(matched['subs'])} "
          f"za {time.time() - t_start:.1f} s")
    return matched


# --- hledání + historie -----------------------------------------------------------

def search_title(kind):
    return {"movie": L(30010), "series": L(30011), "ws": L(30045),
            "hs": L(30197, "Hledat na HellSpy"), "dav": L(30386, "Hledat ve vlastním úložišti"),
            "any": L(30150, "Hledat")}.get(kind, L(30150, "Hledat"))


def search_history(kind):
    """Pro sjednocené hledání i dřívější dotazy z časů oddělených složek."""
    if kind != "any":
        return STORE.history(kind)
    seen, out = set(), []
    for key in ("any", "movie", "series"):
        for q in STORE.history(key):
            if q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
    return out


def search_menu(kind):
    """Složka hledání: nové hledání + historie dotazů."""
    folder_item(L(30040), build_url(action="search_new", type=kind), icon="DefaultAddonsSearch.png")
    history = search_history(kind)
    for q in history:
        folder_item(q, build_url(action="search_run", type=kind, q=q), icon="DefaultAddonsSearch.png",
                    context=[(L(30042), runplugin(action="history_remove", type=kind, q=q))])
    if history:
        folder_item(L(30041), build_url(action="history_clear", type=kind), icon="DefaultVideoDeleted.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def search_new(apis, kind):
    query = xbmcgui.Dialog().input(search_title(kind), type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        return
    search_run(apis, kind, query)


# pomocné metody jádra bez stavu (porovnání názvů, sloučení, rok) — nepotřebují klienty ani úložiště
_JADRO = Engine.__new__(Engine)


def same_title(luna_meta, sosac_meta):
    """Stejný film/seriál v obou zdrojích (viz `Engine._same_title`)."""
    return _JADRO._same_title(luna_meta, sosac_meta)


def merge_results(luna_metas, sosac_metas):
    """[(meta, alt)] – titul z Luny s přibaleným id Sosáče, zbylé položky Sosáče zvlášť (jádro)."""
    return _JADRO._merge(luna_metas, sosac_metas)


def split_year(query):
    """„Pět švestek 2026“ → („Pět švestek“, „2026“) — jako `Engine.split_year`, rok jako text
    (jde do odkazů a klíčů cache)."""
    base, year = Engine.split_year(query)
    return base, str(year) if year else ""


def filter_year(merged, year):
    """Rok v dotazu je filtr: projdou jen tituly z toho roku (a ty, kde ho zdroj neuvádí)."""
    return _JADRO._by_year(merged, int(year) if str(year).isdigit() else None)


class SearchProgress:
    """Ukazatel průběhu hledání.

    Kroků je tolik, kolik je doopravdy dotazů na zdroje, tedy dva typy krát
    zapnuté zdroje. Dokud se počítaly jen typy, uměl ukazatel skočit z nuly na
    polovinu a rovnou na konec. Ze dvou vláken se do něj sahá zároveň, proto zámek.
    """

    def __init__(self, bar, total):
        self.bar, self.total, self.done = bar, max(1, total), 0
        self.lock = threading.Lock()
        self.sources = []   # [(label, count), ...] v pořadí, jak zdroje dorazily
        self.audio_done = self.audio_total = 0   # čtení hlaviček (ověření zvuku) — poslední fáze

    def _show(self):
        """„Streamy: 35“ (součet ze zdrojů, průběžně přibývá) je vidět pořád, během ověřování
        k němu přibude „Meta: 3/12“ (zkrácené 2026-09-17, přání uživatele). Dřív se vypisoval každý zdroj
        zvlášť („Luna: 8 · WebShare: 12 · …“) — na TV nečitelné (2026-09-16, přání uživatele)."""
        percent = int(self.done / self.total * 100)
        parts = []
        if self.sources:
            parts.append(L(30240, "Streamy: {count}").format(count=sum(n for _label, n in self.sources)))
        if self.audio_total:
            parts.append(L(30239, "Meta: {done}/{total}").format(
                done=self.audio_done, total=self.audio_total))
        if parts:
            self.bar.update(percent, " · ".join(parts))
        else:
            # hledání titulu (search_run) source()/audio() nehlásí — nechává si text z create()
            self.bar.update(percent)

    def tick(self):
        with self.lock:
            self.done = min(self.done + 1, self.total)
            self._show()

    def reach(self, value):
        """Dorovná ukazatel — z cache se výsledek vrátí rovnou a kroky neproběhnou."""
        with self.lock:
            if value > self.done:
                self.done = min(value, self.total)
                self._show()

    def set(self, done, total):
        """`on_progress` jádra: to si počet kroků upravuje za běhu (kolik hlaviček se čte)."""
        with self.lock:
            self.total = max(1, total)
            self.done = min(done, self.total)
            self._show()

    def source(self, label, count):
        """`on_source_done` jádra: doplní přehled, odkud kolik streamů zatím přišlo."""
        with self.lock:
            self.sources.append((label, count))
            self._show()

    def audio(self, done, total):
        """`on_audio_progress` jádra: kolik hlaviček souborů je ověřeno z kolika se čte —
        poslední a nejdelší fáze, kdy už `source()` dál nepřibývá."""
        with self.lock:
            self.audio_done, self.audio_total = done, total
            self._show()


def _search_merge(apis, ctype, query, want_year, errors, tick=None):
    """Sloučené výsledky primárního zdroje a přihlášeného Sosáče pro jeden typ
    (film / seriál), BEZ popisů — stačí na počty pro volbu Filmy/Seriály.
    Řetězec zdrojů (TMDB → Luna → veřejný Sosáč → Cinemeta, + přihlášený Sosáč)
    a cache jsou v jádru, viz `Engine.search_pairs`."""
    failures = []
    try:
        return engine_of(apis).search_pairs(ctype, query, int(want_year) if want_year else None, on_tick=tick,
                                            failures=failures, with_enrich=False)
    finally:
        errors.extend(SourceFailure(label, err) for label, err in failures)


def search_source(apis, ctype, query, want_year, errors):
    """`_search_merge()` + doplněné popisy — pro skutečné zobrazení seznamu (jádro
    cachuje holý katalog a doplněný výsledek zvlášť, volba Filmy/Seriály na popisy nečeká)."""
    failures = []
    try:
        return engine_of(apis).search_pairs(ctype, query, int(want_year) if want_year else None,
                                            failures=failures)
    finally:
        errors.extend(SourceFailure(label, err) for label, err in failures)


def search_run(apis, kind, query, offset=0):
    # `search_new` prázdný dotaz nepustí dál, ale sem se dá dostat i přímo (crafted plugin://
    # URL, widget) — prázdné `q=` u Sosáče/WebSharu/HellSpy spadne na nerozparsovatelné odpovědi
    if not str(query or "").strip():
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        return
    if kind not in ("hs", "dav"):
        # HellSpy se hledá jen jako odbočka z dotazu, který v katalozích nic
        # nenašel — do historie patří ten původní dotaz, ne tahle odbočka
        STORE.add_history(kind, query)
    if kind == "ws":
        list_ws_results(apis, query, offset)
        return
    if kind == "hs":
        list_hs_results(apis, query, offset)
        return
    if kind == "dav":
        list_dav_results(apis, query, offset)
        return
    raw_query = query
    query, want_year = split_year(query)
    errors = []
    if kind == "any":
        # jedno hledání pro obojí; volba se nabídne, jen když dotaz sedí na filmy i seriály.
        # Oba dotazy běží souběžně — jinak by procházení čekalo na součet obou (7 s místo 4 s).
        # Jen holý katalog (_search_merge), bez popisů — na volbu Filmy/Seriály
        # stačí počty a čekání na enrich by ji zbytečně zdrželo.
        bar = xbmcgui.DialogProgressBG()
        bar.create(L(30000, "Nokturno"), L(30150, "Hledat"))
        bar.update(0)
        # primární zdroj je vždy jeden krok (TMDB/Luna, nebo za ně zaskočí sosac_db/Cinemeta
        # — viz _search_merge); přihlášený Sosáč se sčítá zvlášť, běží nezávisle na primárním zdroji
        sources = 1 + (1 if apis.get("sosac") else 0)
        progress = SearchProgress(bar, 2 * sources)
        results = {}
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(_search_merge, apis, "movie", query, want_year, errors,
                                       progress.tick): "movie",
                          pool.submit(_search_merge, apis, "series", query, want_year, errors,
                                      progress.tick): "series"}
                # aktualizace v pořadí, jak doopravdy dobíhají — zůstat na pevném
                # pořadí (nejdřív film) by procento drželo na 0 %, dokud nedoběhnou oba
                finished = 0
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
                    finished += 1
                    progress.reach(finished * sources)
        finally:
            bar.close()
        movies, _ = results["movie"]
        series, _ = results["series"]
        if movies and series:
            set_content("files")
            folder_item(f"{L(30012)} ({len(movies)})", build_url(action="search_run", type="movie", q=raw_query),
                       icon="DefaultMovies.png")
            folder_item(f"{L(30013)} ({len(series)})", build_url(action="search_run", type="series", q=raw_query),
                       icon="DefaultTVShows.png")
            for e in errors:
                log_error(e)
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
            return
        kind = "series" if series else "movie"
    ctype = kind
    set_content("tvshows" if ctype == "series" else "movies")
    # vždy přes search_source() — i po volbě z Filmy/Seriály; holý katalog výše
    # je z vlastní cache (_search_merge) skoro zadarmo, teprve tady se čeká na popisy
    merged, mixed = search_source(apis, ctype, query, want_year, errors)
    for meta, alt in merged:
        add_meta_item(meta, ctype, alt=alt, tag_source=mixed)
    # bez Luny (nebo když zrovna neodpovídá) nabídneme rovnou soubory z WebShare
    # (jinak je má Luna: Search u titulu — tam by šlo o duplicitu)
    luna_down = any(error_label(e) == "Luna" for e in errors)
    if apis["ws"] and (not apis["luna"] or luna_down):
        try:
            files, _total = apis["ws"].search(query, sort=SORTS[int(setting("ws_sort", "0"))], limit=WS_PAGE)
            remember_ws_token(apis["ws"])
            for f in files:
                add_ws_file(f)
        except WebshareError as e:
            errors.append(e)
    if not merged:
        # V katalozích nic. WebShare ani HellSpy tituly neznají, jsou to úložiště
        # souborů — soubor pojmenovaný stejně tam ale být může a starší nebo
        # okrajové věci bývají jen tam. Nabídne se to až tady, do rozcestníku
        # fulltext nepatří a běžné hledání by jen zdržoval.
        if apis.get("ws"):
            folder_item(L(30045), build_url(action="search_run", type="ws", q=query),
                       icon="DefaultAddonsSearch.png")
        if apis.get("hs"):
            folder_item(L(30197, "Hledat na HellSpy"), build_url(action="search_run", type="hs", q=query),
                       icon="DefaultAddonsSearch.png")
    for e in errors:
        log_error(e)
    if errors:
        # blokující dialog, ne jen toast — ať si uživatel opravdu všimne, že
        # nějaký zdroj neodpověděl, a ví přesně který; výsledky ze zbylých
        # zdrojů se zobrazí hned po OK (endOfDirectory běží až za tímhle)
        xbmcgui.Dialog().ok(L(30360, "Zdroj neodpověděl"), describe_errors(errors))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_ws_results(apis, query, offset=0):
    api = apis["ws"]
    if api is None:
        raise WebshareError(L(30104))
    # „videos" je obecný typ a skiny k němu nabízejí jen základní seznam;
    # u konkrétního typu je na výběr celá sada zobrazení. Kodi si zobrazení
    # pamatuje podle typu obsahu, takže tyhle seznamy sdílejí nastavení s Filmy.
    set_content("movies")
    files, total = api.search(query, sort=SORTS[int(setting("ws_sort", "0"))], limit=WS_PAGE, offset=offset)
    remember_ws_token(api)
    for f in files:
        add_ws_file(f)
    if offset + len(files) < total and files:
        folder_item(L(30021), build_url(action="search_run", type="ws", q=query, offset=offset + len(files)),
                   icon="DefaultFolder.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_hs_results(apis, query, offset=0):
    api = apis.get("hs")
    if api is None:
        raise HellspyError(L(30104))
    set_content("movies")
    files, _next = api.search(query, limit=HS_PAGE, offset=offset)
    for f in files:
        add_hs_file(f)
    # HellSpy neposílá celkový počet, jen další offset; další strana se nabídne,
    # dokud chodí plná dávka
    if len(files) == HS_PAGE:
        folder_item(L(30021), build_url(action="search_run", type="hs", q=query, offset=offset + len(files)),
                   icon="DefaultFolder.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def _storage_ok(api, errors):
    """Jde úložiště přečíst? Chyba se připíše k ostatním a úložiště se přeskočí."""
    try:
        api.files()
        return True
    except StorageError as e:
        errors.append(SourceFailure(api.name, e))
        return False


def list_dav_browse(apis, slot=0, path=""):
    """Moje úložiště — procházení po složkách.

    Složky se neptají serveru po jedné: strom se skládá ze zapamatovaného seznamu
    souborů (`StorageApi.files`), takže přechod do podsložky je okamžitý. Ukazují se
    jen složky, ve kterých je aspoň jedno video — prázdné by jen překážely. Napřed
    se vždy vybírá úložiště (i jen jedno nastavené, 2026-09-15) — uživatel tak vidí
    jeho jméno a ví, do čeho se dívá, než se napojí na jeho data."""
    storages = apis.get("dav") or []
    if not storages:
        raise StorageError(L(30104))
    if not slot:
        for api in storages:
            folder_item(api.name, build_url(action="dav_browse", slot=api.slot), icon="DefaultHardDisk.png")
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    api = next((s for s in storages if s.slot == slot), storages[0])
    prefix = f"{path.strip('/')}/" if path.strip("/") else ""
    folders, files = {}, []
    for f in api.files():
        if not f["path"].startswith(prefix):
            continue
        rest = f["path"][len(prefix):]
        if "/" in rest:
            name = rest.split("/", 1)[0]
            folders[name] = folders.get(name, 0) + 1
        else:
            files.append(f)
    for name in sorted(folders, key=str.casefold):
        count = folders[name]
        folder_item(f"{name}  [COLOR FF9A9A9A]{count}[/COLOR]",
                    build_url(action="dav_browse", slot=api.slot, path=prefix + name), icon="DefaultFolder.png")
    if files:
        set_content("movies")
    for f in sorted(files, key=lambda f: f["name"].casefold()):
        add_dav_file(api, f)
    if not folders and not files:
        notify(L(30102))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_dav_results(apis, query, offset=0):
    storages = apis.get("dav") or []
    if not storages:
        raise StorageError(L(30104))
    set_content("movies")
    errors, rows = [], []
    for api in storages:
        if _storage_ok(api, errors):
            rows += [(api, f) for f in api.search(query, limit=10 ** 6)[0]]
    for api, f in rows[offset:offset + WS_PAGE]:
        add_dav_file(api, f)
    if offset + WS_PAGE < len(rows):
        folder_item(L(30021), build_url(action="search_run", type="dav", q=query, offset=offset + WS_PAGE),
                   icon="DefaultFolder.png")
    if errors:
        notify(skipped_notice(errors), xbmcgui.NOTIFICATION_WARNING, 7000)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def history_remove(kind, query):
    # sjednocené hledání ("any") zobrazuje i historii z "movie"/"series"
    # (viz search_history) — dotaz je proto potřeba smazat ze všech tří
    for k in (("any", "movie", "series") if kind == "any" else (kind,)):
        STORE.remove_history(k, query)
    xbmc.executebuiltin("Container.Refresh")


def history_clear(kind):
    for k in (("any", "movie", "series") if kind == "any" else (kind,)):
        STORE.clear_history(k)
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
    xbmc.executebuiltin("Container.Refresh")


# --- zhlédnuto, Můj seznam, Pokračovat ----------------------------------------------

def set_watched(key, watched):
    """Zhlédnuto/nezhlédnuto do evidence, do HA (synchronizace) i do Traktu."""
    STORE.set_watched(key, watched)
    request_sync()
    trakt = get_trakt()
    if trakt and trakt.logged_in():
        base, season, episode = split_episode_id(key)
        try:
            (trakt.mark_watched if watched else trakt.unmark_watched)(base, season, episode)
        except TraktError as e:
            log_error(f"trakt: {e}")


def toggle_watched(key):
    set_watched(key, not STORE.playcount(key))
    xbmc.executebuiltin("Container.Refresh")


def adopt_kodi_marks():
    """„Označit jako zhlédnuté“ ze skinu Kodi → evidence Nokturna (viz `kodi_marks`).

    Kodi po označení výpis hned obnoví, takže tohle běží dřív, než se položky
    nakreslí — a nakreslí se už se správnou fajfkou. Chyba nesmí shodit výpis."""
    def apply(changes):
        for key, watched in changes.items():
            if bool(STORE.playcount(key)) != watched:
                xbmc.log(f"[{ADDON_ID}] zhlédnuto z Kodi: {key} → {'ano' if watched else 'ne'}", xbmc.LOGINFO)
                set_watched(key, watched)
    try:
        kodi_marks.collect(STORE, xbmcvfs.translatePath("special://database/"), apply=apply,
                           write=kodi_marks.rpc_writer(xbmc.executeJSONRPC))
    except Exception as e:  # noqa: BLE001 – cizí databáze, cokoli neočekávaného jen do logu
        log_error(f"zhlédnuto z Kodi: {e}")


def remove_progress(key, series=None):
    """Odebrání z Pokračovat ve sledování — vynuluje rozkoukanost (`resume`/`total`),
    ne zhlédnutí. Nový `ts` (nastaví ho `set_resume`) je to, co odebrání přenese
    i na ostatní synchronizovaná Kodi — bez novějšího času by ho starší rozkoukaný
    záznam odjinud zase přepsal zpátky (viz sync.py, vyhrává vždy novější).

    Volá se z kontextového menu (RunPlugin), ale i z integrace pro Home Assistant
    přes `Files.GetDirectory` (`ExecuteAddon` neumí RunPlugin akce spustit) — proto
    `succeeded=False` jako u „Vymazat mezipaměť": bez něj by na tenhle typ volání
    Kodi čekalo na výpis složky, který nikdy nepřijde."""
    STORE.set_resume(key, 0, 0)
    if series:
        # „Další díl“ není rozkoukaný — dopočítává se z posledního zhlédnutého dílu,
        # takže vynulované resume ho nezmění a položka se hned vrátila (Hospoda 1x02
        # v kartě HA). Pamatuje se proto skrytý díl: jakmile se zhlédne další a na řadě
        # je jiný, nabídne se zase.
        STORE.hide_next(series, key)   # synchronizuje se na ostatní Kodi a do HA
    request_sync()
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
    xbmc.executebuiltin("Container.Refresh")


def toggle_fav(apis, key, ctype, series_id=None, alt=None):
    info = STORE.item(key)
    if (not info or thin_snapshot(info)) and not str(key).startswith("ws:"):
        try:
            meta, video = load_meta(apis, ctype, key, series_id)
            info = snapshot(meta, ctype, video, series_id, alt)
        except Errors as e:
            log_error(e)
            info = info or {"type": ctype, "id": key, "title": key, "series": series_id, "alt": alt, "art": {}}
    added = STORE.toggle_favourite(key, info)
    notify(L(30065) if added else L(30066))
    request_sync()
    xbmc.executebuiltin("Container.Refresh")


def list_favourites():
    # „videos" je obecný typ a skiny k němu nabízejí jen základní seznam;
    # u konkrétního typu je na výběr celá sada zobrazení. Kodi si zobrazení
    # pamatuje podle typu obsahu, takže tyhle seznamy sdílejí nastavení s Filmy.
    set_content("movies")
    apis = None
    for key in STORE.favourites():
        snap = STORE.item(key)
        if snap and thin_snapshot(snap):
            apis = apis or get_apis()
            snap = recover_snapshot(apis, key) or snap
        if snap:
            add_snapshot_item(key, snap)
    # z hlavního menu sem — patří k „mým“ titulům a synchronizuje se s nimi
    folder_item(L(30064), build_url(action="recent"), icon="DefaultRecentlyAddedMovies.png")
    if sync_settings():
        folder_item(L(30184, "Synchronizovat teď"), build_url(action="sync_now"), icon="DefaultAddonsUpdates.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_recent():
    # „videos" je obecný typ a skiny k němu nabízejí jen základní seznam;
    # u konkrétního typu je na výběr celá sada zobrazení. Kodi si zobrazení
    # pamatuje podle typu obsahu, takže tyhle seznamy sdílejí nastavení s Filmy.
    set_content("movies")
    apis = None
    for key, _entry in STORE.recently_watched():
        snap = STORE.item(key)
        if snap and thin_snapshot(snap):
            apis = apis or get_apis()
            snap = recover_snapshot(apis, key) or snap
        if snap:
            add_snapshot_item(key, snap)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def next_episode(apis, snap):
    """Další epizoda po zhlédnuté (podle meta seriálu), nebo None."""
    series_id = snap.get("series")
    if not series_id or snap.get("season") is None:
        return None
    try:
        meta = api_for(apis, series_id).meta("series", series_id)
    except Errors:
        return None
    videos = sorted((v for v in meta.get("videos") or [] if int(v.get("season") or 0) > 0),
                    key=lambda v: (int(v.get("season") or 0), int(v.get("episode") or 0)))
    cur = (int(snap.get("season") or 0), int(snap.get("episode") or 0))
    for v in videos:
        if (int(v.get("season") or 0), int(v.get("episode") or 0)) > cur:
            return v, meta


def recover_snapshot(apis, key):
    """Snímek pro titul, který má záznam o rozkoukání, ale v `items.json` chybí,
    nebo je „hubený" (`thin_snapshot` — bez popisu i fotky, viz volající).

    Chybějící snímek: stávalo se, když zápis z přehrání přepsal jiný proces
    (rejstřík Sosáče, do 2.0.22) — a bez snímku výpis položku tiše vynechal,
    takže titul v Pokračovat ve sledování „nebyl". Hubený snímek: titul se
    přidal do Mého seznamu/Pokračovat dřív, než pro něj doběhlo obohacení
    (TMDB, přepočet na pozadí) — `snapshot()` pak nemá co dát do popisu ani
    fotky, a bez týhle opravy zůstane prázdný navždy, i když data mezitím
    dorazila. Dohledá se z meta a uloží, ať to příště nestojí dotaz na síť.
    Soubory WebShare/HellSpy meta nemají, u těch není z čeho.
    """
    if key.startswith(("ws:", "hs:", "dav:", "dl:")):
        return None
    base, season, _episode = split_episode_id(key)
    ctype = "series" if season is not None else "movie"
    try:
        meta, video = load_meta(apis, ctype, key)
        snap = snapshot(meta, ctype, video, base if video else None, None)
    except Errors as e:
        log_error(f"snímek {key}: {e}")
        return None
    STORE.remember_item(key, snap)
    return snap


def list_continue(apis):
    """Rozkoukané tituly + další díly po naposledy zhlédnutých epizodách."""
    # „videos" je obecný typ a skiny k němu nabízejí jen základní seznam;
    # u konkrétního typu je na výběr celá sada zobrazení. Kodi si zobrazení
    # pamatuje podle typu obsahu, takže tyhle seznamy sdílejí nastavení s Filmy.
    set_content("movies")
    for key, _entry in STORE.in_progress():
        snap = STORE.item(key)
        if not snap or thin_snapshot(snap):
            snap = recover_snapshot(apis, key) or snap
        if snap:
            add_snapshot_item(key, snap, [(L(30365, "Odebrat z Pokračovat ve sledování"),
                                          runplugin(action="remove_progress", id=key))])
    seen_series = set()
    snaps = []
    for key, _entry in STORE.recently_watched(40):
        snap = STORE.item(key)
        if not snap or snap.get("season") is None or snap.get("series") in seen_series:
            continue
        seen_series.add(snap.get("series"))
        snaps.append(snap)
    # meta každého seriálu je dotaz na síť (Luna 10 min, TMDB 30 dní v cache) — souběžně,
    # dřív se Pokračovat s deseti seriály otevíralo deset dotazů za sebou
    with ThreadPoolExecutor(max_workers=6) as pool:
        nalezeno = list(pool.map(lambda s: next_episode(apis, s), snaps))
    for snap, found in zip(snaps, nalezeno):
        if not found:
            continue
        video, meta = found
        ep_id = video.get("id") or f"{snap['series']}:{video.get('season')}:{video.get('episode')}"
        if STORE.playcount(ep_id) or STORE.next_hidden(snap["series"]) == ep_id:
            continue
        li = xbmcgui.ListItem(label=f"{L(30067)}: {meta.get('_title') or meta.get('name')} – "
                                    f"{int(video.get('season') or 0)}x{int(video.get('episode') or 0):02d} {video.get('title') or ''}")
        li.setArt(art_for(meta, video))
        fill_info(li, meta, "series", video=video)
        apply_watched(li, ep_id, [fav_context(ep_id, "series", snap["series"], snap.get("alt")),
                                  (L(30365, "Odebrat z Pokračovat ve sledování"),
                                   runplugin(action="remove_progress", id=ep_id, series=snap["series"]))]
                      + streams_context("series", ep_id, snap["series"], snap.get("alt")))
        add_playable(li, "series", ep_id, series_id=snap["series"], alt=snap.get("alt"))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


# --- seriály ---------------------------------------------------------------------

def list_seasons(apis, series_id, alt=None):
    meta = meta_for(apis, "series", series_id)
    if is_sosac_id(series_id):
        enrich_one(meta, apis["luna"], STORE, "series")
    videos = meta.get("videos") or []
    seasons = sorted({int(v.get("season") or 0) for v in videos}, key=lambda s: (s == 0, s))
    set_content("seasons")
    for s in seasons:
        tpl = L(30023)
        label = L(30022) if s == 0 else (tpl % s if "%d" in tpl else f"{tpl} {s}")
        li = xbmcgui.ListItem(label=label)
        li.setArt(art_for(meta))
        fill_info(li, meta, "series")
        tag = li.getVideoInfoTag()
        tag.setMediaType("season")
        tag.setSeason(s)
        # `fill_info()` nastaví Title na název seriálu (správně pro epizody/film) —
        # tady jde o výběr sezóny, skin (řádek sezón) kreslí `ListItem.Title`, ne
        # `ListItem.Label`, takže bez přepsání byly všechny položky pojmenované
        # stejně jako seriál místo „1. série“/„2. série“ (2026-09-15, nahlásil
        # uživatel screenshotem: „Lupin Lupin Lupin“ místo čísel sérií).
        tag.setTitle(label)
        # sezóna je zhlédnutá, když jsou zhlédnuté všechny její epizody
        eps = [v for v in videos if int(v.get("season") or 0) == s]
        if eps and all(STORE.playcount(v.get("id") or f"{series_id}:{s}:{v.get('episode')}") for v in eps):
            li.getVideoInfoTag().setPlaycount(1)
        url = build_url(action="episodes", id=series_id, season=s, alt=alt)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    similar_item("series", series_id)
    xbmcplugin.endOfDirectory(HANDLE)


def list_episodes(apis, series_id, season, alt=None):
    meta = meta_for(apis, "series", series_id)
    set_content("episodes")
    videos = [v for v in meta.get("videos") or [] if int(v.get("season") or 0) == season]
    videos.sort(key=lambda v: int(v.get("episode") or 0))
    ep_ids = [v.get("id") or f"{series_id}:{season}:{v.get('episode')}" for v in videos]
    # „další na řadě" = první nezhlédnutý díl za posledním zhlédnutým v téhle sezóně;
    # dokud v ní nic zhlédnuté není, neoznačuje se nic (nemá co navazovat)
    watched = [bool(STORE.playcount(ep_id)) for ep_id in ep_ids]
    next_up = next((i for i in range(max((i for i, w in enumerate(watched) if w), default=-1) + 1, len(videos))
                    if not watched[i]), None) if any(watched) else None
    for i, v in enumerate(videos):
        label = "%d. %s" % (int(v.get("episode") or 0), v.get("title") or "")
        if i == next_up:
            label = f"[COLOR {LANG_COLORS.get('CZ', 'FFFFC94D')}]»[/COLOR] {label}"
        li = xbmcgui.ListItem(label=label)
        if i == next_up:
            li.setProperty("nokturno.next", "true")
        li.setArt(art_for(meta, v))
        fill_info(li, meta, "series", video=v)
        ep_id = ep_ids[i]
        apply_watched(li, ep_id, [fav_context(ep_id, "series", series_id, alt)]
                      + streams_context("series", ep_id, series_id, alt))
        add_playable(li, "series", ep_id, series_id=series_id, alt=alt)
    # widget a výpis v Nokturnu mívají stejnou adresu, položky se ale liší podle okna (add_playable)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def similar_item(ctype, item_id):
    """Složka „Podobné tituly“ na konci seznamu streamů filmu a sezón seriálu."""
    if IMDB_ID_RE.match(str(item_id or "")):
        folder_item(L(30482, "Podobné tituly"), build_url(action="similar", type=ctype, id=item_id),
                    icon="DefaultVideoPlaylists.png")


def list_similar(apis, ctype, item_id):
    """Podobné tituly: s vlastním TMDB klíčem přímo z TMDB, bez něj (nebo při jeho chybě)
    z dashboardu, který se na TMDB ptá za doplněk."""
    ctype = "series" if ctype == "series" else "movie"
    items = []
    tmdb = apis.get("tmdb")
    if tmdb is not None:
        try:
            items = tmdb.similar(ctype, item_id)
        except TmdbError as e:
            log_error(e)
            items = []
    if not items and apis.get("dash") is not None:
        items = apis["dash"].similar(ctype, item_id)
    set_content("tvshows" if ctype == "series" else "movies")
    if not items:
        notify(L(30493, "Žádné podobné tituly"), xbmcgui.NOTIFICATION_INFO, 3000)
    for m in items:
        add_meta_item(m, ctype)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


TV_KINDS = ("", "movie", "series")


def tv_day_label(day, today):
    """`2026-09-18` → „Zítra (pá 18. 9.)“ — dny v týdnu z řetězce 30491 (lokalizace bez locale)."""
    import datetime
    try:
        d = datetime.date(*map(int, day.split("-")))
        t = datetime.date(*map(int, (today or day).split("-")))
    except (ValueError, AttributeError):
        return day or ""
    names = L(30491, "Mon,Tue,Wed,Thu,Fri,Sat,Sun").split(",")
    short = f"{names[d.weekday()] if len(names) == 7 else ''} {d.day}. {d.month}.".strip()
    rel = {0: L(30489, "Dnes"), 1: L(30490, "Zítra")}.get((d - t).days)
    return f"{rel} ({short})" if rel else short


def tv_url(day="", kind="", channel=""):
    return build_url(action="tv", date=day or None, kind=kind or None, channel=channel or None)


def list_tv(apis, day="", kind="", channel=""):
    """TV program: filmy a seriály, které dnes (nebo jiný den) dávají v české a slovenské
    televizi a které server spároval s TMDB. Nahoře tři volby (den, stanice, typ) jako
    ne-složky — klik je spustí se handle −1 (`action=tv_pick`, výběr v dialogu), takže
    výpis sám, widget ani JSON-RPC žádný dialog neotevře."""
    dash = apis.get("dash")
    data = dash.tv_program(day or None, kind or None, channel or None) if dash else None
    set_content("movies")
    if not data:
        notify(L(30492, "TV program teď není dostupný"), xbmcgui.NOTIFICATION_WARNING, 4000)
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    day = data.get("date") or day
    channel_name = next((c["name"] for c in data["channels"] if c["slug"] == channel), "") or L(30487, "Všechny stanice")
    kind_name = {"": L(30488, "Filmy i seriály"), "movie": L(30012), "series": L(30013)}.get(kind, "")
    picks = (
        ("day", f"{L(30484, 'Den')}: {tv_day_label(day, data.get('today'))}", "DefaultYear.png"),
        ("channel", f"{L(30485, 'Stanice')}: {channel_name}", "DefaultAddonPVRClient.png"),
        ("kind", f"{L(30486, 'Typ')}: {kind_name}", "DefaultGenre.png"),
    )
    for field, label, icon in picks:
        li = xbmcgui.ListItem(label=f"[B]{label}[/B]")
        li.setArt({"icon": icon, "thumb": icon})
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="tv_pick", field=field, date=day or None,
                                                      kind=kind or None, channel=channel or None),
                                    li, isFolder=False)
    now = time.time()
    shown = 0
    for it in data["items"]:
        if day == data.get("today") and it["stop"] < now:
            continue   # dnes už skončené pořady jen zabírají místo
        start = time.strftime("%H:%M", time.localtime(it["start"]))
        live = it["start"] <= now < it["stop"]
        clock = f"[COLOR {LANG_COLORS.get('CZ', 'FFFFC94D')}]{start}[/COLOR]" if live else start
        name = display_name(it["meta"]) if it["kind"] == "movie" else (it["meta"].get("name") or it["title"])
        if it["kind"] == "series" and it.get("season") is not None and it.get("episode") is not None:
            name = f"{name} {int(it['season'])}x{int(it['episode']):02d}"
        if it.get("episode_title"):
            name = f"{name} {it['episode_title']}"
        label = f"{clock}  [COLOR {GREY}]{it['channel_name']}[/COLOR]  {name}"
        add_meta_item(it["meta"], it["kind"], label=label)
        shown += 1
    if not shown:
        li = xbmcgui.ListItem(label=f"[COLOR {GREY}]{L(30494, 'V tomhle výběru nic nedávají')}[/COLOR]")
        xbmcplugin.addDirectoryItem(HANDLE, tv_url(day, kind, channel), li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def tv_pick(apis, field, day="", kind="", channel=""):
    """Klik na volbu nad TV programem (handle −1, jen z výpisu Nokturna) → dialog → přepnout výpis."""
    data = apis["dash"].tv_program(day or None, kind or None, channel or None) or {}
    if field == "day":
        dates = data.get("dates") or []
        if not dates:
            return
        idx = xbmcgui.Dialog().select(L(30484, "Den"), [tv_day_label(d, data.get("today")) for d in dates],
                                      preselect=dates.index(day) if day in dates else 0)
        if idx < 0:
            return
        day = dates[idx]
    elif field == "channel":
        channels = [{"slug": "", "name": L(30487, "Všechny stanice")}] + (data.get("channels") or [])
        slugs = [c["slug"] for c in channels]
        idx = xbmcgui.Dialog().select(L(30485, "Stanice"), [c["name"] for c in channels],
                                      preselect=slugs.index(channel) if channel in slugs else 0)
        if idx < 0:
            return
        channel = slugs[idx]
    else:
        names = [L(30488, "Filmy i seriály"), L(30012), L(30013)]
        idx = xbmcgui.Dialog().select(L(30486, "Typ"), names,
                                      preselect=TV_KINDS.index(kind) if kind in TV_KINDS else 0)
        if idx < 0:
            return
        kind = TV_KINDS[idx]
    xbmc.executebuiltin(f"Container.Update({tv_url(day, kind, channel)},replace)")


# --- přehrávání ------------------------------------------------------------------

def upnext_episode(meta, v, series_id):
    """Popis dílu ve tvaru, jaký čeká služba Up Next (service.upnext)."""
    art = art_for(meta, v)
    ep_id = v.get("id") or f"{series_id}:{int(v.get('season') or 0)}:{int(v.get('episode') or 0)}"
    runtime = runtime_minutes(v.get("runtime") or meta.get("runtime"))
    return {
        "episodeid": ep_id,
        "tvshowid": series_id,
        "title": v.get("title") or "",
        "art": {
            "thumb": art.get("thumb", ""),
            "tvshow.poster": art.get("poster", ""),
            "tvshow.fanart": art.get("fanart", ""),
            "tvshow.landscape": art.get("landscape", ""),
            "tvshow.clearlogo": art.get("clearlogo", ""),
            "tvshow.clearart": "",
        },
        "season": int(v.get("season") or 0),
        "episode": int(v.get("episode") or 0),
        "showtitle": meta.get("_title") or meta.get("name") or "",
        "plot": v.get("overview") or "",
        "playcount": 1 if STORE.playcount(ep_id) else 0,
        "rating": str(meta.get("imdbRating") or ""),
        "firstaired": str(v.get("released") or "")[:10],
        "runtime": runtime * 60,
    }, ep_id


def upnext_notify(meta, video, series_id, alt=None):
    """Oznámí službě Up Next (je-li nainstalovaná) následující díl, aby ke konci
    epizody nabídla „Další díl“ a uměla ho pustit zase přes tenhle plugin.

    Up Next čeká signál `upnext_data` (JSONRPC.NotifyAll, payload base64 JSON) —
    stejný formát posílá i modul AddonSignals, ten ale kvůli jednomu volání netaháme.
    """
    if not xbmc.getCondVisibility("System.HasAddon(service.upnext)"):
        return
    videos = [v for v in meta.get("videos") or [] if v.get("episode") is not None]
    # speciály (sezóna 0) až na konec, jinak podle sezóny a čísla dílu
    videos.sort(key=lambda v: (int(v.get("season") or 0) == 0, int(v.get("season") or 0), int(v.get("episode") or 0)))
    cur = (int(video.get("season") or 0), int(video.get("episode") or 0))
    idx = next((i for i, v in enumerate(videos)
                if (int(v.get("season") or 0), int(v.get("episode") or 0)) == cur), None)
    if idx is None or idx + 1 >= len(videos):
        return
    current, _ = upnext_episode(meta, video, series_id)
    following, next_id = upnext_episode(meta, videos[idx + 1], series_id)
    payload = {
        "current_episode": current,
        "next_episode": following,
        "play_url": build_url(action="play", type="series", id=next_id, series=series_id, alt=alt),
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    xbmc.executeJSONRPC(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "JSONRPC.NotifyAll", "params": {
        "sender": f"{ADDON_ID}.SIGNAL", "message": "upnext_data", "data": [encoded]}}))


def pick_title(apis, ctype, item_id, series_id=None, alt=None, fulltext=False, download=False):
    """Klik na titul ve výpisu Nokturna (handle −1, viz `add_playable`) i „Vybrat stream“ v kontextovém
    menu: najít streamy s ukazatelem průběhu v rohu (`DialogProgressBG`), ukázat dialog a vybraný stream
    pustit přes `PlayMedia` s jeho referencí — `play()` ji jen rozklíčuje a zapamatuje volbu u seriálu.
    Modál je tu v pořádku: tuhle cestu spouští jen klik uživatele, widget ani JSON-RPC položku
    `action=title` nerozklíčují jako skript.

    Přísný filtr fulltextových zdrojů (`Engine._title_queries`) může skutečnou shodu zahodit kvůli
    neobvyklému názvu souboru: bez výsledku se rovnou zkusí uvolněný, s výsledky ho nabídne dialog.

    `download=True` („Stáhnout“ v kontextovém menu): stejný dialog, vybraný stream jde do fronty
    stahování (`download_stream`) místo přehrání. Bez složky pro stahování se nic nehledá."""
    if download and not download_dir():
        return
    meta, video = load_meta(apis, ctype, item_id, series_id)
    has_fulltext_source = bool(apis.get("ws") or apis.get("hs") or apis.get("st") or apis.get("fs"))

    def collect(strict):
        bar = xbmcgui.DialogProgressBG()   # načítání jen ukazatelem v rohu, modální je až výběr streamu
        bar.create(L(30000, "Nokturno"), L(30238, "Načítám streamy…"))
        errors = []
        try:
            found = collect_streams(apis, ctype, item_id, meta, alt,
                                    SearchProgress(bar, Engine.STREAM_SOURCE_STEPS + AUDIO_PROBE_MAX), strict, errors)
        finally:
            bar.close()
        if errors:
            notify(skipped_notice(errors), xbmcgui.NOTIFICATION_WARNING, 7000)
        return found, errors

    strict = not fulltext
    streams, errors = collect(strict)
    if not fulltext:
        year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
        mark_viewed(item_id, bare_title(meta), year if year.isdigit() else None, "series" if video else ctype)
    if not streams and strict and has_fulltext_source:
        strict = False
        streams, errors = collect(strict)
    if not streams:
        if not errors:
            notify(L(30102))
        return
    pref_key = (series_id or split_episode_id(item_id)[0]) if video else None
    remembered = preferred_stream(streams, STORE.stream_pref(pref_key)) if pref_key else None
    picked = choose_stream(streams, remembered, relax=strict and has_fulltext_source,
                           expand=lambda: expand_streams(apis, streams, meta, video))
    if picked == FULLTEXT:
        pick_title(apis, ctype, item_id, series_id, alt, fulltext=True, download=download)
        return
    if picked is None:
        return
    if download:
        title = (video or {}).get("title") or display_name(meta)
        download_stream(apis, picked["url"], f"{title} [{picked['label']}]", item_id, ctype, series_id, alt)
        return
    xbmc.executebuiltin("PlayMedia(%s)" % build_url(
        action="play", type=ctype, id=item_id, series=series_id, alt=alt, url=picked["url"],
        subs="|".join(picked.get("subtitles") or []), pref=pref_param(picked), alts=alts_param(picked)))


def play(apis, ctype, item_id, series_id=None, url=None, alt=None, subs="", pref="", ask="", alts=""):
    """`ask=1` = Přehrát nad položkou Nokturna (detail, widget) nebo player TMDb Helperu → výběr
    v dialogu (`choose_stream`, zapamatovaný stream předvybraný). Bez `ask` (Up Next, HA) hraje
    zapamatovaný nebo nejlepší stream bez ptaní."""
    meta, video = load_meta(apis, ctype, item_id, series_id)
    # u seriálu si pamatujeme, jaký stream si uživatel vybral — další díl (Up Next,
    # Pokračovat, widget) pak jede stejně bez ptaní; klíč je seriál, ne díl
    pref_key = (series_id or split_episode_id(item_id)[0]) if video else None
    resolved_path = None
    if url:
        try:
            # sloučené verze (`alts`) jsou tentýž film jinde — zkusí se, než se hledá znovu
            url, resolved_path = resolve_first(apis, [url] + [a for a in (alts or "").split("|") if a])
        except Errors as e:
            # uložená reference streamu (Pokračovat ve sledování, viz add_playable) nebo dřív
            # vybraný stream ze seznamu mezitím zmizely ze zdroje — vzít to jako by url vůbec
            # nepřišla a normálně prohledat všechny zdroje znovu, ne rovnou ukázat chybu
            xbmc.log(f"[{ADDON_ID}] uložený stream nejde přehrát, hledám znovu: {e}", xbmc.LOGINFO)
            url = None
        else:
            chosen = {"url": url, "subtitles": [s for s in subs.split("|") if s]}
            if pref_key and pref_from_param(pref):
                STORE.set_stream_pref(pref_key, pref_from_param(pref))
    if not url:
        errors = []
        # z přehrání (widget, TMDb Helper) je jinak vidět jen točící se kolečko Kodi — streamy se
        # načítají i 15 s, tak aspoň stejný průběh jako nad seznamem streamů. NE modální
        # DialogProgress — tahle cesta se spouští z widgetu, TMDb Helperu i JSON-RPC (CLAUDE.md:
        # „nikdy modální dialog v cestě, kterou může spustit widget nebo JSON-RPC“), a modální
        # dialog v tomhle přehrávacím kontextu přehrání buď spadlo, nebo se nic nezobrazilo
        # (Office 2026-09-16, nahlásil uživatel po zavedení cancelable_search v betě 7).
        # Zpět tu tedy hledání nezruší.
        bar = xbmcgui.DialogProgressBG()
        bar.create(L(30000, "Nokturno"), L(30238, "Načítám streamy…"))
        try:
            streams = collect_streams(apis, ctype, item_id, meta, alt,
                                      SearchProgress(bar, Engine.STREAM_SOURCE_STEPS + AUDIO_PROBE_MAX), errors=errors)
        finally:
            bar.close()
        if errors:
            notify(skipped_notice(errors), xbmcgui.NOTIFICATION_WARNING, 7000)
        if not streams:
            if not errors:
                notify(L(30102))
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return
        remembered = preferred_stream(streams, STORE.stream_pref(pref_key)) if pref_key else None
        chosen = remembered or streams[0]
        if ask:
            picked = choose_stream(streams, remembered, expand=lambda: expand_streams(apis, streams, meta, video))
            if picked is None:
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
                return
            chosen = picked
            if pref_key:
                STORE.set_stream_pref(pref_key, stream_signature(chosen))
    title = (video or {}).get("title") or display_name(meta)
    # label i InfoTag: při přímém otevření (JSON-RPC, widgety) nemá Kodi původní položku seznamu
    if not resolved_path:
        used, resolved_path = resolve_first(apis, [chosen["url"]] + [a["url"] for a in chosen.get("_alts") or []])
        if used != chosen["url"]:
            chosen = next(a for a in chosen["_alts"] if a["url"] == used)
    li = xbmcgui.ListItem(label=title, path=resolved_path)
    li.setArt(art_for(meta, video))
    fill_info(li, meta, "series" if video else ctype, video=video)
    # bez tohohle Kodi u přímého přehrání (HA karta, widget, Up Next) nevědělo o rozkoukanosti
    # a vždycky pustilo od začátku — resume point se jinak nastavuje jen v seznamech (`apply_watched`)
    tag = li.getVideoInfoTag()
    count = STORE.playcount(item_id)
    if count:
        tag.setPlaycount(count)
    resume, total = STORE.resume(item_id)
    if resume and not count:
        try:
            tag.setResumePoint(resume, total)
        except Exception:  # noqa: BLE001 – Kodi < 20
            pass
    subtitles = local_subtitles(apis, chosen.get("subtitles") or [])
    if subtitles:
        li.setSubtitles(subtitles)
    STORE.remember_item(item_id, snapshot(meta, ctype, video, series_id, alt))
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    # bez roku – ten se posílá zvlášť polem `year`, display_name() by ho zdvojil
    stats_title = episode_stats_title(video, meta)
    mark_playing(item_id, stats_title, year if year.isdigit() else None, "series" if video else ctype,
                stream_url=chosen.get("url"), stream_subs="|".join(chosen.get("subtitles") or []),
                stream_langs=list(chosen.get("langs") or []))
    xbmcplugin.setResolvedUrl(HANDLE, True, li)
    if video:
        upnext_notify(meta, video, series_id or split_episode_id(item_id)[0], alt)


def play_ws(apis, ident, name=""):
    api = apis["ws"]
    if api is None:
        raise WebshareError(L(30104))
    link = api.file_link(ident)
    remember_ws_token(api)
    if not link:
        notify(L(30102))
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    li = xbmcgui.ListItem(label=name or ident, path=link)
    li.getVideoInfoTag().setTitle(name or ident)
    STORE.remember_item("ws:" + ident, {"type": "ws", "id": "ws:" + ident, "title": name or ident, "art": {}})
    mark_playing("ws:" + ident, name, kind="ws")
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def play_dav(apis, slot, path, name=""):
    key = f"dav:{slot}:{path}"
    api, path = storage_for(apis, key)
    li = xbmcgui.ListItem(label=name or path, path=api.kodi_url(path))
    li.getVideoInfoTag().setTitle(name or path)
    STORE.remember_item(key, {"type": "dav", "id": key, "title": name or path, "art": {}})
    mark_playing(key, name, kind="dav")
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def play_hs(apis, file_id, file_hash, name=""):
    api = apis.get("hs") or HellspyApi(cache=STORE)
    key = f"hs:{file_id}:{file_hash}"
    link = api.file_link(file_id, file_hash)
    if not link:
        notify(L(30102))
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    li = xbmcgui.ListItem(label=name or key, path=link)
    li.getVideoInfoTag().setTitle(name or key)
    STORE.remember_item(key, {"type": "hs", "id": key, "title": name or key, "art": {}})
    mark_playing(key, name, kind="hs")
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# --- stahování ---------------------------------------------------------------------

def download_dir():
    d = setting("download_dir")
    if not d:
        notify(L(30076), xbmcgui.NOTIFICATION_WARNING, 6000)
        return None
    return xbmcvfs.translatePath(d)


def safe_filename(name):
    import re
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name or "").strip(" _.") or "video"
    return name[:150]


VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm", ".wmv")


def guess_ext(url, name):
    for ext in VIDEO_EXTS:
        if name.lower().endswith(ext):
            return ""
        if url.lower().split("?")[0].endswith(ext):
            return ext
    return ".mkv"


def enqueue_download(url, name, key, dest_name=None, link=None):
    """Do fronty jde VNITŘNÍ odkaz (`ws:`, `hs:`, `st:`, `streamuj:`, `dav:`) — služba ho
    rozklíčuje až ve chvíli stahování (`service.resolve_internal`). Hotový odkaz WebShare
    vyprší za pár hodin: třetí soubor ve frontě nebo cokoli po restartu Kodi dřív končilo
    chybou. `link` je volitelný už rozklíčovaný odkaz jen kvůli odhadu přípony."""
    d = download_dir()
    if not d:
        return
    base = safe_filename(dest_name or name)
    dest = os.path.join(d, base + guess_ext(link or url, base))
    entry = {"id": key, "url": url, "name": name, "dest": dest}
    if STORE.add_download(entry):
        notify(Lf(30072, name))
    else:
        notify(L(30077))


def download_stream(apis, url, name, key, ctype, series_id=None, alt=None):
    if thin_snapshot(STORE.item(key)) or not STORE.item(key):
        try:
            meta, video = load_meta(apis, ctype, key, series_id)
            STORE.remember_item(key, snapshot(meta, ctype, video, series_id, alt))
        except Errors:
            pass
    # id z hashlib: `hash(str)` je náhodně seedovaný per proces, takže tentýž soubor
    # dostal po restartu Kodi jiné id a dedup ve frontě ho stáhl podruhé
    import hashlib
    dl_id = f"dl:{key}:{hashlib.sha1(url.encode('utf-8')).hexdigest()[:8]}"
    # rozklíčovat jen kvůli příponě, když ji název nemá (Luna/Sosáč mají místo názvu popisek)
    link = None if name.lower().endswith(VIDEO_EXTS) else resolve_url(apis, url)
    enqueue_download(url, name, dl_id, dest_name=name, link=link)


def download_ws(apis, ident, name):
    api = apis["ws"]
    if api is None:
        raise WebshareError(L(30104))
    link = api.file_link(ident)   # ověření, že soubor existuje; stahuje se až z fronty
    remember_ws_token(api)
    if not link:
        notify(L(30102))
        return
    enqueue_download("ws:" + ident, name, f"dl:ws:{ident}", dest_name=name, link=link)


def download_hs(apis, file_id, file_hash, name):
    api = apis.get("hs") or HellspyApi(cache=STORE)
    link = api.file_link(file_id, file_hash)
    if not link:
        notify(L(30102))
        return
    enqueue_download(f"hs:{file_id}:{file_hash}", name, f"dl:hs:{file_id}", dest_name=name, link=link)


def list_downloads():
    if sync_settings():
        folder_item(L(30190, "Staženo v HA"), build_url(action="ha_files"), icon="DefaultNetwork.png")
    set_content("videos")
    status_labels = {"queued": L(30078), "running": L(30079), "done": L(30080), "error": L(30081), "cancel": L(30082)}
    for d in STORE.downloads():
        status = d.get("status")
        pct = int(d.get("done", 0) * 100 / d["size"]) if d.get("size") else 0
        detail = f"{status_labels.get(status, status)}"
        if status == "running":
            detail += f" {pct} % · {human_size(d.get('done', 0))} / {human_size(d.get('size', 0))}"
        elif status == "done":
            detail += f" · {human_size(d.get('size', 0))}"
        elif status == "error":
            detail += f" · {d.get('error', '')[:60]}"
        li = xbmcgui.ListItem(label=f"{d.get('name', '')}  [COLOR FF9A9A9A]{detail}[/COLOR]")
        tag = li.getVideoInfoTag()
        tag.setMediaType("video")
        tag.setTitle(d.get("name", ""))
        tag.setPlot(d.get("dest", ""))
        # "done" fakticky maže soubor z disku (po potvrzení, viz download_remove) — "Odebrat ze
        # seznamu" by tam matlo, že zůstane ležet na kartě (nahlásil uživatel 2026-09-18)
        if status in ("queued", "running"):
            remove_label = L(30083)
        elif status == "done":
            remove_label = L(30516, "Delete")
        else:
            remove_label = L(30084)
        ctx = [(remove_label, runplugin(action="download_remove", id=d["id"]))]
        if status == "error":
            ctx.append((L(30085), runplugin(action="download_retry", id=d["id"])))
        li.addContextMenuItems(ctx)
        if status == "done" and os.path.exists(d.get("dest", "")):
            li.setProperty("IsPlayable", "true")
            xbmcplugin.addDirectoryItem(HANDLE, d["dest"], li, isFolder=False)
        else:
            xbmcplugin.addDirectoryItem(HANDLE, build_url(action="downloads"), li, isFolder=False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def download_remove(dl_id):
    d = next((x for x in STORE.downloads() if x.get("id") == dl_id), None)
    if not d:
        return
    if d.get("status") == "running":
        STORE.update_download(dl_id, status="cancel")  # služba smaže .part i záznam
    else:
        if d.get("status") == "done" and xbmcgui.Dialog().yesno(L(30000), Lf(30086, d.get("name", ""))):
            try:
                os.remove(d["dest"])
            except OSError:
                pass
        STORE.remove_download(dl_id)
    xbmc.executebuiltin("Container.Refresh")


def download_retry(dl_id):
    STORE.update_download(dl_id, status="queued", done=0, error="")
    xbmc.executebuiltin("Container.Refresh")


# --- Trakt -------------------------------------------------------------------------

def trakt_auth():
    trakt = get_trakt()
    if not trakt or not trakt.client_id or not trakt.client_secret:
        xbmcgui.Dialog().ok(L(30000), L(30094))
        return
    try:
        code = trakt.device_code()
    except TraktError as e:
        log_error(e)
        notify(L(30096), xbmcgui.NOTIFICATION_ERROR)
        return
    dialog = xbmcgui.DialogProgress()
    dialog.create(L(30090), Lf(30095, code.get("verification_url", ""), code.get("user_code", "")))
    interval = int(code.get("interval") or 5)
    deadline = time.time() + int(code.get("expires_in") or 600)
    tokens = None
    while time.time() < deadline and not dialog.iscanceled():
        dialog.update(int(100 - (deadline - time.time()) / int(code.get("expires_in") or 600) * 100))
        xbmc.sleep(interval * 1000)
        try:
            tokens = trakt.poll_token(code["device_code"])
        except TraktError as e:
            log_error(e)
            break
        if tokens:
            break
    dialog.close()
    notify(L(30097) if tokens else L(30096), xbmcgui.NOTIFICATION_INFO if tokens else xbmcgui.NOTIFICATION_ERROR)


def trakt_logout():
    STORE.set_trakt({})
    notify(L(30098))


# --- router ---------------------------------------------------------------------

def router(query):
    p = dict(urllib.parse.parse_qsl(query.lstrip("?")))
    action = p.get("action")
    # akce bez seznamu (RunPlugin) a bez API
    simple = {
        "history_remove": lambda: history_remove(p["type"], p.get("q", "")),
        "history_clear": lambda: history_clear(p["type"]),
        "toggle_watched": lambda: toggle_watched(p["id"]),
        "remove_progress": lambda: remove_progress(p["id"], p.get("series")),
        "search": lambda: search_menu(p.get("type") or p.get("kind") or "any"),
        "favourites": list_favourites,
        "recent": list_recent,
        "downloads": list_downloads,
        "download_remove": lambda: download_remove(p["id"]),
        "download_retry": lambda: download_retry(p["id"]),
        "trakt_auth": trakt_auth,
        "trakt_logout": trakt_logout,
        # succeeded=False jako u "settings" — jinak by Kodi navigoval do prázdné složky
        # a musel by se dát Zpět, i když jde jen o akci, ne o výpis
        "clear_cache": lambda: (STORE.clear_cache(), notify(L(30099)),
                                xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "stats_send": stats_send,
        "log_send": log_send,
        "website_info": website_info,
        "test_sources": test_sources,
        "remote_setup": lambda: remote_setup_action(p.get("section")),
        "stream_layout_reset": lambda: (stream_layout_reset(),
                                        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "setup_wizard": lambda: (setup_wizard(force=True),
                                 xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "sub_status": sub_status,
        "luna_check": lambda: luna_check(ask=True),
        "luna_find": luna_find,
        "speedtest": speedtest,
        "update_repos": update_repos,
        "tmdbhelper_player": tmdbhelper_player,
        "sync_now": sync_now,
        "whats_new": whats_new,
        "ha_files": list_ha_files,
        "settings": lambda: (xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False), ADDON.openSettings()),
    }
    try:
        if action == "title_download":
            # „Stáhnout“ v kontextovém menu (RunPlugin, handle −1) — z widgetu/JSON-RPC s handle nic
            if HANDLE < 0:
                pick_title(get_apis(), p.get("type", "movie"), p["id"], p.get("series"), alt=p.get("alt"),
                           download=True)
            else:
                xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
            return
        if action in ("title", "streams") and HANDLE < 0:
            # klik na titul ve výpisu Nokturna: Kodi ne-přehratelnou položku spustí jako skript
            # bez handle → streamy v dialogu na dva řádky (viz add_playable, pick_title)
            pick_title(get_apis(), p.get("type", "movie"), p["id"], p.get("series"), alt=p.get("alt"))
            return
        if action in ("streams", "streams_filter"):
            # výpis streamů jako složka zrušen v `5.2.14~beta4` — starý odkaz (oblíbené, widget) nic
            # nevypíše, z okna Nokturna otevře dialog; z widgetu/JSON-RPC nikdy modál
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
            if browsing_nokturno():
                xbmc.executebuiltin(runplugin(action="title", type=p.get("type", "movie"), id=p.get("id"),
                                              series=p.get("series"), alt=p.get("alt")))
            return
        if action == "tv_pick":
            # volba nad TV programem: dialog jen po kliku ve výpisu (handle −1), jinak nic
            if HANDLE < 0:
                tv_pick(get_apis(), p.get("field", ""), p.get("date", ""), p.get("kind", ""), p.get("channel", ""))
            else:
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return
        if action in simple:
            # stejná pojistka jako u výpisů níž: akce bez `type` (starý odkaz z widgetu, ruční
            # URL z HA) dřív vyletěla KeyError mimo `_fail`, Kodi nechalo neuzavřený handle
            # a při souběhu s dalším dialogem se celé ukončilo („two concurrent busydialogs")
            simple[action]()
            return
        apis = get_apis()
        if not action:
            main_menu(apis)
        elif action == "catalogs":
            list_catalogs(apis, p["type"], p.get("src", "luna"))
        elif action == "browse":
            browse_menu(apis, p.get("type", "movie"))
        elif action == "similar":
            list_similar(apis, p.get("type", "movie"), p.get("id", ""))
        elif action == "tv":
            list_tv(apis, p.get("date", ""), p.get("kind", ""), p.get("channel", ""))
        elif action == "genres":
            list_genres(apis, p["type"], p["catalog"], p.get("src", "luna"), show_all=not p.get("noall"))
        elif action == "dash_group":
            list_dash_group(apis, p["catalog"], p.get("type", "movie"))
        elif action == "catalog":
            list_catalog(apis, p["type"], p["catalog"], p.get("src", "luna"), genre=p.get("genre"),
                         search=p.get("search"), skip=int(p.get("skip") or 0))
        elif action == "lang_catalog_menu":
            lang_catalog_menu(apis, p.get("type", "movie"), p.get("want", "dub"))
        elif action == "lang_catalog_trigger":
            lang_catalog_trigger(apis, p.get("type", "movie"), p.get("want", "dub"))
        elif action == "lang_catalog":
            list_lang_catalog(apis, p.get("type", "movie"), p.get("want", "dub"))
        elif action == "search_new":
            search_new(apis, p["type"])
        elif action == "search_run":
            search_run(apis, p["type"], p.get("q", ""), offset=int(p.get("offset") or 0))
        elif action == "continue":
            list_continue(apis)
        elif action == "prefetch":
            prefetch(apis, p.get("kind", "next"))
        elif action == "toggle_fav":
            toggle_fav(apis, p["id"], p.get("type", "movie"), p.get("series"), p.get("alt"))
        elif action == "seasons":
            list_seasons(apis, p["id"], alt=p.get("alt"))
        elif action == "episodes":
            list_episodes(apis, p["id"], int(p.get("season") or 0), alt=p.get("alt"))
        elif action == "title":
            # Přehrát nad titulem v režimu seznamu (Estuary/Arctic Fuse/TMDb Helper/tlačítko Play):
            # Kodi položku rozklíčovává a čeká setResolvedUrl → dialog výběru streamu
            play(apis, p.get("type", "movie"), p["id"], p.get("series"), alt=p.get("alt"), ask="1")
        elif action == "play":
            play(apis, p["type"], p["id"], p.get("series"), url=p.get("url"), alt=p.get("alt"), subs=p.get("subs", ""),
                 pref=p.get("pref", ""), ask=p.get("ask", ""), alts=p.get("alts", ""))
        elif action == "play_ws":
            play_ws(apis, p["ident"], p.get("name", ""))
        elif action == "play_hs":
            play_hs(apis, p["id"], p["hash"], p.get("name", ""))
        elif action == "dav_browse":
            list_dav_browse(apis, int(p.get("slot") or 0), p.get("path", ""))
        elif action == "play_dav":
            play_dav(apis, int(p.get("slot") or 0), p.get("path", ""), p.get("name", ""))
        elif action == "download":
            download_stream(apis, p["url"], p.get("name", ""), p["id"], p.get("type", "movie"), p.get("series"), p.get("alt"))
        elif action == "download_ws":
            download_ws(apis, p["ident"], p.get("name", ""))
        elif action == "download_hs":
            download_hs(apis, p["id"], p["hash"], p.get("name", ""))
        else:
            main_menu(apis)
    except Aborted:
        # Kodi končí, nebo uživatel zrušil — žádná hláška, jen zavřít handle, ať Kodi
        # nečeká na timeout (viz `should_stop`; přerušené hledání se nikde necachuje)
        xbmc.log(f"[{ADDON_ID}] {action}: přerušeno (Kodi končí nebo zrušeno uživatelem)", xbmc.LOGINFO)
        _close(action)
    except Errors as e:
        log_error(e)
        _fail(action, describe_error(e))
    except Exception as e:  # noqa: BLE001 – KeyError z chybějícího parametru, RuntimeError z Kodi API…
        # bez úklidu handle by Kodi u přehrání čekalo na timeout a hlásilo „Chyba skriptu"
        log_error(f"{action}: {traceback.format_exc()}")
        _fail(action, f"{type(e).__name__}: {e}")
        report_crash(action, e)


def _fail(action, message):
    notify(message, xbmcgui.NOTIFICATION_ERROR, 5000)
    _close(action)


def _close(action):
    """Zavře handle Kodi jako neúspěšný — bez toho by Kodi u přehrání čekalo na timeout."""
    # "title" s handle ≥ 0 = Přehrát nad titulem (Kodi čeká setResolvedUrl, viz router);
    # s handle < 0 se sem nedostane, router tu větev ukončí dřív
    if action in ("play", "play_ws", "play_hs", "play_dav", "title"):
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
    elif action not in ("download", "download_ws", "download_hs", "toggle_fav"):
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


def main(query):
    try:
        adopt_kodi_marks()
        router(query)
    finally:
        # sdílený executor popisů (`enrich`) nechává po sobě nečinná vlákna, na která Kodi
        # po doběhnutí pluginu čeká navždy — i při vypínání (Office 2026-09-16: widget
        # „Nově přidané" zablokoval Application.Quit natrvalo). Rozběhnuté dotazy doběhnou
        # do cache; když Kodi končí, nezačaté se zruší.
        release_enrich(cancel=should_stop())


if __name__ == "__main__":
    main(sys.argv[2] if len(sys.argv) > 2 else "")
