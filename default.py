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
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))
from luna_api import LunaApi, LunaError, parse_base_url, parse_token  # noqa: E402
from cinemeta_api import CinemetaApi, CinemetaError  # noqa: E402
from tmdb_api import TmdbApi, TmdbError  # noqa: E402
from trend_api import CATALOG_ID as TREND_CATALOG_ID, TrendApi  # noqa: E402
from sosac_api import SosacError, is_sosac_id as _is_stremio_sosac_id  # noqa: E402
from sosac_direct import EXPORT as SOSAC_EXPORT, SosacDirect, is_direct_id  # noqa: E402
from enrich import enrich, enrich_one  # noqa: E402
from hellspy_api import HellspyApi, HellspyError  # noqa: E402
from sledujteto_api import SledujtetoApi, SledujtetoError  # noqa: E402
from fastshare_api import FastshareApi, FastshareError  # noqa: E402
from storage_api import SLOTS as STORAGE_SLOTS, StorageApi, StorageError, parse_ref  # noqa: E402
from store import Store, migrate_profile  # noqa: E402
from source_errors import describe_failure, summarize as summarize_failures  # noqa: E402
from sync import sync_once  # noqa: E402
from streams import estimate_rank, langs_from_name, parse_stream, subs_from_name  # noqa: E402
from trakt_api import TraktApi, TraktError  # noqa: E402
from webshare_api import SORTS, WebshareApi, WebshareError, human_size  # noqa: E402
from engine import AUDIO_PROBE_MAX, DEFAULT_RUNTIME_S, Engine, NokturnoError, runtime_minutes  # noqa: E402

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
USED_PROP = "nokturno.used"    # služba si odsud bere „doplněk byl otevřen“ pro statistiky
PREF_LANGS = ("", "CZ", "SK", "EN")
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
        return SosacDirect(su, sp, cache=STORE, cache_ttl=CACHE_TTL, index_store=STORE.index(), fresh=warming())
    return None


def get_sosac_db():
    """Veřejný katalog Sosáče (žádný účet, žádný přepínač) — vlastní databáze
    filmů a seriálů česky, funguje vždy. `apis["sosac"]` výš zůstává jen pro
    přihlášené přehrávání a stahování; katalog samotný účet nepotřebuje."""
    return SosacDirect(cache=STORE, cache_ttl=CACHE_TTL, index_store=STORE.index(), fresh=warming())


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
                                  setting(f"dav{slot}_name"), slot=slot, cache=STORE))
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
                 "cinemeta": get_cinemeta, "trend": get_trend}

    def __init__(self):
        self._clients = {}
        super().__init__(engine_options(), PROFILE, store=STORE)

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


def get_apis():
    """Klienty zdrojů pod jmény, na která je zvyklý zbytek doplňku, plus jádro pod `engine`."""
    engine = KodiEngine()
    return {"engine": engine, "luna": engine.luna, "sosac": engine.sosac, "ws": engine.ws, "hs": engine.hs,
            "st": engine.st, "fs": engine.fs, "dav": engine.storages, "cinemeta": engine.cinemeta, "sosac_db": engine.sosac_db,
            "tmdb": engine.tmdb, "trend": engine.trend}


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

SORTS = {
    "movies": (xbmcplugin.SORT_METHOD_VIDEO_YEAR, xbmcplugin.SORT_METHOD_VIDEO_RATING),
    "tvshows": (xbmcplugin.SORT_METHOD_VIDEO_YEAR, xbmcplugin.SORT_METHOD_VIDEO_RATING),
    "episodes": (xbmcplugin.SORT_METHOD_EPISODE,),
}


def set_content(content):
    """Typ obsahu + nabídka řazení. Bez `addSortMethod` skiny ukazovaly „Řazení: žádné“
    a katalog nešel seřadit podle roku ani hodnocení, i když je `fill_info` plní.
    První je „jak přišlo“ — pořadí ze zdroje (žebříček, seřazené streamy) zůstává výchozí."""
    xbmcplugin.setContent(HANDLE, content)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_UNSORTED)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL_IGNORE_THE)
    for method in SORTS.get(content, ()):
        xbmcplugin.addSortMethod(HANDLE, method)


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
# hodnoty doslovný placeholder "Episode 3" – jako `or` fallback ho nic nechytí,
# je to neprázdný řetězec. Do statistik tak šlo "Episode 3" místo názvu seriálu.
_EPISODE_PLACEHOLDER_RE = re.compile(r"^episode\s+\d+$", re.IGNORECASE)


def episode_stats_title(video, meta):
    """Titul epizody do statistik – `video["title"]`, ale jen když není generický
    placeholder (viz výše); jinak název seriálu jako u filmu/seriálu bez epizody."""
    t = (video or {}).get("title")
    if t and not _EPISODE_PLACEHOLDER_RE.match(t):
        return t
    return bare_title(meta)


def display_name(meta):
    """Název s rokem – „Matrix (1999)“; u Sosáče jen titul bez jazyků a originálu."""
    title = bare_title(meta)
    if is_sosac_id(meta.get("id")):
        year = str(meta.get("year") or "")[:4]
    else:
        year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    return f"{title} ({year})" if year.isdigit() else title


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
    }


def apply_watched(li, key, context=None):
    """Zhlédnuto (fajfka) a bod pro pokračování z vlastní evidence.

    `context`: další položky kontextového menu. Kodi při každém `addContextMenuItems` přepisuje
    položky od indexu 0, proto se menu skládá tady najednou.
    """
    tag = li.getVideoInfoTag()
    count = STORE.playcount(key)
    if count:
        tag.setPlaycount(count)
    resume, total = STORE.resume(key)
    if resume and not count:
        try:
            tag.setResumePoint(resume, total)
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
    tag.setTitle(snap.get("title") or "")
    if snap.get("tvshow"):
        tag.setTvShowTitle(snap["tvshow"])
    tag.setPlot(snap.get("plot") or "")
    if str(snap.get("year") or "").isdigit():
        tag.setYear(int(snap["year"]))
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


def add_meta_item(meta, ctype, alt=None, tag_source=False):
    """`alt` = id téhož titulu v Sosáči (sloučený výsledek hledání) → streamy z obou zdrojů.

    `tag_source`: ve smíšeném hledání označit tituly, které má jen Sosáč (v katalozích Sosáče je to zbytečné).
    """
    label = display_name(meta)
    if tag_source and is_sosac_id(meta.get("id")):
        label = f"{label}  [COLOR {GREY}]· Sosáč[/COLOR]"
    li = xbmcgui.ListItem(label=label)
    li.setArt(art_for(meta))
    fill_info(li, meta, ctype)
    fav = fav_context(meta["id"], ctype, alt=alt)
    if ctype == "series":
        li.addContextMenuItems([fav])
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="seasons", id=meta["id"], alt=alt), li, isFolder=True)
    else:
        apply_watched(li, meta["id"], [fav, streams_context("movie", meta["id"], alt=alt)])
        add_playable(li, "movie", meta["id"], alt=alt)


def folder_mode():
    """Režim „Vybrat ze seznamu streamů“ ve výpisu Nokturna → film a díl jako složka."""
    return setting("stream_mode", "1") == "1" and browsing_nokturno()


def add_playable(li, ctype, item_id, series_id=None, alt=None):
    """Film nebo díl — ve výpisu Nokturna podle nastavení, jinde přehratelný.

    V režimu „Vybrat ze seznamu streamů“ je ve výpisu Nokturna složkou `action=streams` — klik
    otevře seznam nativně. Ve widgetu, na domovské obrazovce a v detailu otevřeném odtamtud je
    přehratelný s `ask=1`: skiny berou film podle DBType jako soubor a Přehrát v detailu (Arctic
    Fuse) volá `PlayMedia` na cestu položky. Složka tam nepřehrála nic, přehratelná položka ukáže
    dialog s filtrem (`choose_stream`). Přesměrovat z přehratelné položky ve výpisu na složku
    přes zrušené přehrání nešlo: Kodi hlásilo „položku se nepodařilo přehrát“ a seznam otevřený
    přes `Container.Update` ukazoval místo streamů název filmu (Office 2026-09-14).

    U epizod se předává i id seriálu — Sosáč dává epizodám vlastní id
    (`sosac2_1877:1:1`), ze kterého se meta seriálu nedá odvodit.
    """
    if folder_mode():
        url = build_url(action="streams", type=ctype, id=item_id, series=series_id, alt=alt)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
        return
    li.setProperty("IsPlayable", "true")
    # „1“ = výběr dialogem; „2“ se ptá sám v play(), „0“ pustí nejlepší. Bez `ask` (Up Next, HA)
    # se v režimu 1 hraje zapamatovaný nebo nejlepší stream bez ptaní.
    ask = "1" if setting("stream_mode", "1") == "1" else None
    url = build_url(action="play", type=ctype, id=item_id, series=series_id, alt=alt, ask=ask)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)


def streams_context(ctype, item_id, series_id=None, alt=None):
    """Druhá cesta ke streamům v kontextovém menu — ta, kterou nenabízí klik na položku.

    Složka (výpis Nokturna) → „Vybrat stream a přehrát“: dialog s filtrem, hodí se i v detailu,
    kde Přehrát na složce nefunguje. Přehratelná položka (widget) → „Seznam streamů“ jako složka;
    `ActivateWindow` jde i z domovské obrazovky (`Container.Update` jen ve Videích)."""
    if folder_mode():
        url = build_url(action="play", type=ctype, id=item_id, series=series_id, alt=alt, ask="1")
        return (L(30414, "Vybrat stream a přehrát"), f"PlayMedia({url})")
    url = build_url(action="streams", type=ctype, id=item_id, series=series_id, alt=alt)
    return (L(30201, "Seznam streamů"), f"ActivateWindow(Videos,{url},return)")


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
    label = snap.get("title") or key
    if snap.get("tvshow") and snap.get("season") is not None:
        label = f"{snap['tvshow']} – {int(snap['season'])}x{int(snap['episode'] or 0):02d} {label}"
    li = xbmcgui.ListItem(label=label)
    fill_info_snapshot(li, snap)
    ctx = [fav_context(key, snap.get("type", "movie"), snap.get("series"), snap.get("alt"))] + (extra_context or [])
    if snap.get("type") == "series" and snap.get("season") is None:
        li.addContextMenuItems(ctx)
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="seasons", id=key, alt=snap.get("alt")), li, isFolder=True)
        return
    kind = "series" if snap.get("season") is not None else "movie"
    apply_watched(li, key, ctx + [streams_context(kind, key, snap.get("series"), snap.get("alt"))])
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
    z nich zdaleka nejdelší.
    """
    errors = [] if errors is None else errors
    failures = []
    engine = engine_of(apis)
    try:
        streams = engine.raw_streams(ctype, item_id, alt, on_progress=progress.set if progress else None,
                                     failures=failures, strict=strict, meta_video=(meta, load_meta_video(meta, item_id)))
    finally:
        errors.extend(SourceFailure(label, err) for label, err in failures)
        remember_ws_token(engine.ws)
    return streams


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
    """Streamy, které vyhovují filtru z `streams_filter`. Prázdný filtr = beze změny.

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
    (`streams_filter`) i dialog výběru streamu (`choose_stream`)."""
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


def streams_filter(apis, ctype, item_id, series_id, alt, fq, flang, fch, fcodec, fsub, fsrc):
    """Dialog s nabídkou filtrů podle toho, co se u titulu doopravdy našlo.

    Nejde o samostatnou obrazovku — je to stejné volání GetDirectory jako
    „streams", jen se mezi nimi otevře dialog. Zrušení (Esc) nebo prázdný výběr
    beze změny vrátí předchozí filtr, potvrzení jede rovnou na `list_streams`
    s novým, žádný mezikrok navíc.
    """
    meta, video = load_meta(apis, ctype, item_id, series_id)
    streams = collect_streams(apis, ctype, item_id, meta, alt)
    active = {k: [x for x in v.split(",") if x] for k, v in zip(FILTER_KINDS, (fq, flang, fch, fcodec, fsub, fsrc))}
    new = filter_dialog(streams, active)
    if new is None:
        list_streams(apis, ctype, item_id, series_id, alt, fq, flang, fch, fcodec, fsub, fsrc)
        return
    list_streams(apis, ctype, item_id, series_id, alt, **filter_params(new))


def choose_stream(streams):
    """Výběr streamu v dialogu — z detailu filmu, widgetu nebo TMDb Helperu, odkud se do složky
    se seznamem přejít nedá (přehrání musí skončit `setResolvedUrl`). Nahoře stejné volby jako
    ve složce: Filtr streamů, Zrušit filtr, Použít poslední filtr. Vrací stream, nebo None."""
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
        idx = xbmcgui.Dialog().select(L(30024), [label for label, _v in entries] + [stream_label(st) for st in shown])
        if idx < 0:
            return None
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


def stream_label(s):
    """Popisek streamu na jeden řádek.

    Arctic Fuse v seznamu druhý řádek nevykreslí, takže všechno musí do jednoho
    a záleží na pořadí: co skin ořízne, je konec. Napřed tedy zvukové stopy
    a velikost, pak teprve datový tok, titulky, zdroj a název souboru.

    Jazyk bez vlnovky přišel od zdroje nebo z hlavičky souboru, s vlnovkou je
    jen odhad z názvu souboru — stejně jako „~4K" u odhadnuté kvality.
    """
    parse_stream(s)
    tag = SOURCE_TAGS.get(s.get("source"), "")
    if s.get("_storage"):
        tag = f"[COLOR {DAV_COLOR}]{s['_storage']}[/COLOR]"
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
    parts = []
    if s.get("_storage"):
        # vlastní soubor: štítek úložiště jako první, ne až za zvukem a velikostí na konci řádku
        parts.append(f"[COLOR {DAV_COLOR}][B]{s['_storage']}[/B][/COLOR]")
        tag = ""
    if s.get("_loose"):
        # z ručního „Zkusit fulltext" — přísný filtr ho zahodil jako podobný,
        # ale možná jiný titul; uživatel to musí posoudit sám podle názvu souboru
        parts.append(f"[COLOR {WARN_COLOR}]?[/COLOR]")
    parts.append(f"[COLOR {QUALITY_COLORS.get(s.get('quality_rank', 0), GREY)}][B]{quality or raw}[/B][/COLOR]")
    tracks = s.get("_tracks") or []
    if tracks:
        # přečteno z hlavičky souboru: každá stopa zvlášť i s kodekem
        for t in tracks:
            inside = " ".join(x for x in (t.get("codec"), t.get("channels"), t.get("lang")) if x)
            if inside:
                parts.append(f"[COLOR {LANG_COLORS.get(t.get('lang'), GREY)}][{inside}][/COLOR]")
    else:
        # zdroj o stopách mlčí — poskládá se z toho, co je po ruce
        channels = s.get("channels") or {}
        pref = PREF_LANGS[int(setting("pref_lang", "0"))]
        known = set(s.get("langs") or [])
        for code in sorted(known | langs_from_name(raw), key=lambda c: (c != pref, c)):
            mark = "" if code in known else "~"
            txt = f"{mark}{code}"
            if code in channels:
                txt += f" {channels[code]:.1f}"
            parts.append(f"[COLOR {LANG_COLORS.get(code, GREY)}][{txt}][/COLOR]")
    if s.get("size_gb") and on("show_size", "true"):
        parts.append(f"[COLOR {GREY}][B]{s['size_gb']:.1f} GB[/B][/COLOR]")
    if s.get("_length_s") and on("show_length", "true"):
        mark = "~" if s.get("_length_est") else ""
        parts.append(f"[COLOR {GREY}]{mark}{format_duration(s['_length_s'])}[/COLOR]")
    if s.get("bitrate") and on("show_bitrate", "true"):
        mark = "~" if s.get("_bitrate_est") else ""
        parts.append(f"[COLOR {GREY}]{mark}{s['bitrate']:g} Mb/s[/COLOR]")
    subs = set(s.get("subs") or []) | subs_from_name(raw)
    if subs and on("show_subs", "true"):
        parts.append(f"[COLOR {GREY}]Tit.: {' '.join(sorted(subs))}[/COLOR]")
    if tag and on("show_source", "true"):
        parts.append(tag)
    if rest and quality and on("show_file", "true"):
        parts.append(f"[COLOR {GREY}]{rest}[/COLOR]")
    return "  ".join(parts)


def mark_playing(key, title="", year=None, kind="movie"):
    xbmcgui.Window(10000).setProperty(PLAYING_PROP, json.dumps(
        {"id": key, "title": title, "year": year, "kind": kind}))


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
    if dialog.yesno(
        L(30336, "Vítej v Nokturnu"),
        L(30337, "Projdeme spolu základní nastavení zdrojů, zabere to necelou minutu.[CR]"
                 "Kdykoli to můžeš přeskočit a doplnit později v Nastavení doplňku."),
        yeslabel=L(30338, "Pojďme na to"), nolabel=L(30339, "Přeskočit"),
    ):
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
            addr = dialog.input(L(30350, "Adresa doplňku nebo token ze setu Luny"))
            if addr:
                ADDON.setSetting("token", addr)
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

    checks = {
        "Luna": (lambda: len((luna._get(luna._meta_url("manifest.json")) or {}).get("catalogs", []))) if luna else None,
        "Sosáč": (lambda: len(sosac._get(SOSAC_EXPORT + "souboryzanry.json", ttl=0) or {}))
        if isinstance(sosac, SosacDirect) else None,
        "WebShare": (lambda: bool(ws.login())) if ws else None,
        # mimo cache jako ostatní — HellspyApi bez úložiště se ptá vždy znovu
        "HellSpy": (lambda: len(HellspyApi().search("matrix", limit=5)[0])) if hs else None,
        "Sledujteto": check_sledujteto if st else None,
        "FastShare": check_fastshare if fs else None,
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
        # hodnota ve tvaru, jaký ukládá TMDb Helper sám (`<soubor> <režim>`, config/default.py)
        tmdbh = xbmcaddon.Addon(TMDBH_ID)
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
    běžná cesta (`list_streams`) započítala zobrazení do statistik.
    """
    if kind == "next":
        seen = set()
        for key, _entry in STORE.recently_watched(15):
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


def log_send():
    """Ruční odeslání Kodi logu z nastavení – poslední ~500 KB `kodi.log`, gzip.

    Instance id bere ze stejného `stats.json` jako statistiky, ať jde log
    v dashboardu spárovat s instalací. Vlastní endpoint (`/logs`, ne `/collect`)
    bere syrová gzip data v těle, ne JSON — soubor je řádově větší.
    """
    if not xbmcgui.Dialog().yesno(L(30000, "Nokturno"), L(30431, "Opravdu odeslat log?")):
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
            want, _, _ = target
            folder_item(label, build_url(action=action, type=ctype, want=want), icon=icon)
            continue
        src, cid, genre = target
        params = {"action": action, "type": ctype, "catalog": cid, "src": src}
        if genre:
            params["genre"] = genre
        folder_item(label, build_url(**params), icon=icon)
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
    set_content("tvshows" if ctype == "series" else "movies")
    metas = apis[src].catalog(ctype, cid, genre=genre, search=search, skip=skip)
    if src in ("sosac", "sosac_db", "cinemeta"):
        # exporty Sosáče a holé výpisy Cinemety nemají popis → dotáhnout podle IMDb id
        # (Luna, jinak Cinemeta sama — viz `_fetch()` v enrich.py — cache)
        enrich(metas, apis["luna"], STORE, ctype)
    for m in metas:
        add_meta_item(m, ctype)
    # Luna vrací stránky po ~20, ale některé katalogy o pár položek méně
    if len(metas) >= PAGE // 2:
        folder_item(L(30021), build_url(action="catalog", type=ctype, catalog=cid, src=src, genre=genre,
                                        search=search, skip=skip + len(metas)), icon="DefaultFolder.png")
    # widget a výpis v Nokturnu mívají stejnou adresu, položky se ale liší podle okna (add_playable)
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
    age = _lang_lock_age(win, prop)
    if age is not None and age < LANG_LOCK_STALE:
        _diag(f"{key}: zamčeno cizím výpočtem ({age:.0f}s), čekám")
        t0 = time.time()
        _wait_for_lang_catalog(win, prop)
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


def _wait_for_lang_catalog(win, prop):
    """Čeká, dokud zámek drží živý (dost čerstvý) proces — vrátí se hned, jakmile
    zmizí (normální dokončení) nebo zestárne (mrtvý proces), jinak nejdéle `LANG_LOCK_WAIT`."""
    monitor = xbmc.Monitor()
    waited = 0
    while waited < LANG_LOCK_WAIT:
        age = _lang_lock_age(win, prop)
        if age is None or age >= LANG_LOCK_STALE:
            return
        if monitor.waitForAbort(LANG_LOCK_POLL):
            return
        waited += LANG_LOCK_POLL


def _build_lang_catalog(apis, ctype):
    """Živý výpočet pro `list_lang_catalog()` — samotné dohledání kandidátů se
    posílá do `Store.cached_if()`, proto je vytažené zvlášť. Progres se ukazuje
    jen při skutečném, na uživateli viditelném přepočtu — při zahřívání na
    pozadí (`warming()`) by jen zbytečně blikal.

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
            if bar:
                done = len(matched["dub"]) + len(matched["subs"])
                bar.update(int(done / (2 * LANG_CATALOG_TARGET) * 100),
                          message=f"{dub_label} {len(matched['dub'])}/{LANG_CATALOG_TARGET} · "
                                  f"{subs_label} {len(matched['subs'])}/{LANG_CATALOG_TARGET}")
    finally:
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

    def _show(self):
        self.bar.update(int(self.done / self.total * 100))

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

def toggle_watched(key):
    watched = not STORE.playcount(key)
    STORE.set_watched(key, watched)
    request_sync()
    trakt = get_trakt()
    if trakt and trakt.logged_in():
        base, season, episode = split_episode_id(key)
        try:
            (trakt.mark_watched if watched else trakt.unmark_watched)(base, season, episode)
        except TraktError as e:
            log_error(f"trakt: {e}")
    xbmc.executebuiltin("Container.Refresh")


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
    if not info and not str(key).startswith("ws:"):
        try:
            meta, video = load_meta(apis, ctype, key, series_id)
            info = snapshot(meta, ctype, video, series_id, alt)
        except Errors as e:
            log_error(e)
            info = {"type": ctype, "id": key, "title": key, "series": series_id, "alt": alt, "art": {}}
    added = STORE.toggle_favourite(key, info)
    notify(L(30065) if added else L(30066))
    request_sync()
    xbmc.executebuiltin("Container.Refresh")


def list_favourites():
    # „videos" je obecný typ a skiny k němu nabízejí jen základní seznam;
    # u konkrétního typu je na výběr celá sada zobrazení. Kodi si zobrazení
    # pamatuje podle typu obsahu, takže tyhle seznamy sdílejí nastavení s Filmy.
    set_content("movies")
    for key in STORE.favourites():
        snap = STORE.item(key)
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
    for key, _entry in STORE.recently_watched():
        snap = STORE.item(key)
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
    """Snímek pro titul, který má záznam o rozkoukání, ale v `items.json` chybí.

    Stávalo se to, když zápis snímku z přehrání přepsal jiný proces (rejstřík
    Sosáče, do 2.0.22) — a bez snímku výpis položku tiše vynechal, takže titul
    v Pokračovat ve sledování „nebyl". Dohledá se z meta a uloží, ať to příště
    nestojí dotaz na síť. Soubory WebShare/HellSpy meta nemají, u těch není z čeho.
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
        snap = STORE.item(key) or recover_snapshot(apis, key)
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
                                   runplugin(action="remove_progress", id=ep_id, series=snap["series"])),
                                  streams_context("series", ep_id, snap["series"], snap.get("alt"))])
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
        apply_watched(li, ep_id, [fav_context(ep_id, "series", series_id, alt),
                                  streams_context("series", ep_id, series_id, alt)])
        add_playable(li, "series", ep_id, series_id=series_id, alt=alt)
    # widget a výpis v Nokturnu mívají stejnou adresu, položky se ale liší podle okna (add_playable)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


# --- přehrávání ------------------------------------------------------------------

def fulltext_item(ctype, item_id, series_id, alt):
    """Odkaz na tuhle obrazovku znovu, ale s uvolněným filtrem fulltextových zdrojů
    (viz `Engine._title_queries`, `strict=False`) — pro případ, že přísný automatický
    filtr skutečnou shodu zahodil, protože název souboru je neobvyklý."""
    folder_item(L(30335, "Zkusit uvolněný fulltext (WebShare, HellSpy, Sledujteto, FastShare)"),
                build_url(action="streams", type=ctype, id=item_id, series=series_id, alt=alt, fulltext="1"),
                icon="DefaultAddonsSearch.png")


def list_streams(apis, ctype, item_id, series_id=None, alt=None, fq="", flang="", fch="", fcodec="", fsub="", fsrc="",
                 fulltext=""):
    meta, video = load_meta(apis, ctype, item_id, series_id)
    strict = fulltext != "1"
    has_fulltext_source = bool(apis.get("ws") or apis.get("hs") or apis.get("st") or apis.get("fs"))
    # ukazatel průběhu: pár kroků na dotazy zdrojům, pak (obvykle nejdelší část)
    # jeden na každý soubor, kterému jádro čte hlavičku — přesný počet si jádro upraví
    bar = xbmcgui.DialogProgressBG()
    bar.create(L(30000, "Nokturno"), L(30238, "Načítám streamy…"))
    bar.update(0)
    progress = SearchProgress(bar, Engine.STREAM_SOURCE_STEPS + AUDIO_PROBE_MAX)
    errors = []
    try:
        streams = collect_streams(apis, ctype, item_id, meta, alt, progress, strict, errors)
    finally:
        bar.close()
    if errors:
        # jen upozornění, ne dialog — výpis může spustit widget nebo JSON-RPC z HA
        notify(skipped_notice(errors), xbmcgui.NOTIFICATION_WARNING, 7000)
    if not streams:
        if strict and has_fulltext_source:
            # rovnou selhat by uživateli vzalo možnost zkusit to uvolněněji —
            # nabídne se aspoň ta jedna položka místo prázdné/chybové obrazovky
            set_content("episodes")
            fulltext_item(ctype, item_id, series_id, alt)
            xbmcplugin.endOfDirectory(HANDLE)
            return
        if not errors:
            notify(L(30102))
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    filtered = apply_stream_filter(streams, fq, flang, fch, fcodec, fsub, fsrc)
    if not filtered:
        # filtr nic nenechal — spíš zmatek než prázdný seznam, ukázat radši vše
        notify(L(30214, "Filtr nic nenechal, zobrazeny všechny streamy"), xbmcgui.NOTIFICATION_WARNING)
        filtered, fq, flang, fch, fcodec, fsub, fsrc = streams, "", "", "", "", "", ""
    # Bez tohohle skin nezná typ obsahu a nabídne jen holý „Seznam základní“
    # (jediné místo v doplňku, kde to chybělo). "videos" nestačí — bohatší
    # zobrazení (Seznam médií) skin nabízí jen pro konkrétní typy.
    #
    # Past: Kodi/skin si zvolené zobrazení pamatuje podle TYPU OBSAHU okna,
    # ne podle konkrétní obrazovky pluginu — použití "movies" tady (stejně
    # jako u výsledků hledání) svázalo obě obrazovky do jednoho nastavení,
    # takže změna zobrazení na jedné přepnula i tu druhou. "episodes" sdílí
    # identitu jen s obrazovkou Epizody, na kterou se z hledání chodí přes
    # mezikrok — kolize je tam mnohem méně nápadná než přímo s hledáním.
    set_content("episodes")
    title = (video or {}).get("title") or display_name(meta)
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    # do statistik jde titul bez roku — ten se posílá zvlášť polem `year`,
    # display_name() ho bafá přímo do řetězce a v dashboardu by se zdvojil
    stats_title = episode_stats_title(video, meta)
    mark_viewed(item_id, stats_title, year if year.isdigit() else None, "series" if video else ctype)
    if len(streams) > 1:
        active = bool(fq or flang or fch or fcodec or fsub or fsrc)
        # počet vždy — beze filtru aspoň řekne, z kolika streamů se vybírá,
        # s filtrem navíc kolik z nich filtru vyhovělo
        count = f"({len(filtered)}/{len(streams)})" if active else f"({len(streams)})"
        label = f"{L(30213, 'Filtr streamů')}  {count}"
        # aktivní filtr = zaškrtávací seznam kritérií, ne kolečko aktualizace —
        # to bylo matoucí, protože stejnou ikonu měly i „Vymazat mezipaměť"
        # a „Vymazat historii" (úplně jiná akce, teď mají křížek)
        folder_item(label, build_url(action="streams_filter", type=ctype, id=item_id, series=series_id, alt=alt,
                                     fq=fq, flang=flang, fch=fch, fcodec=fcodec, fsub=fsub, fsrc=fsrc),
                   icon="DefaultPlaylist.png" if active else "DefaultAddonsSearch.png")
        if active:
            folder_item(L(30363, "Zrušit filtr"),
                       build_url(action="streams", type=ctype, id=item_id, series=series_id, alt=alt,
                                 fulltext=fulltext),
                       icon="DefaultVideoDeleted.png")
        last = STORE.last_stream_filter()
        last_f = {k: ",".join(last.get(k) or []) for k in ("q", "lang", "ch", "codec", "sub", "src")}
        if any(last_f.values()) and (last_f["q"], last_f["lang"], last_f["ch"], last_f["codec"], last_f["sub"],
                                     last_f["src"]) != (fq, flang, fch, fcodec, fsub, fsrc):
            last_count = len(apply_stream_filter(streams, **{
                "fq": last_f["q"], "flang": last_f["lang"], "fch": last_f["ch"],
                "fcodec": last_f["codec"], "fsub": last_f["sub"], "fsrc": last_f["src"]}))
            # 0 shodných streamů by bylo jen matoucí tlačítko do prázdna — radši ho vůbec nenabízet
            if last_count:
                # hvězda jako u Můj seznam — „tvoje obvyklá volba", ne další lupa
                folder_item(f"{L(30364, 'Použít poslední filtr')}  ({last_count})",
                           build_url(action="streams", type=ctype, id=item_id, series=series_id, alt=alt,
                                     fulltext=fulltext, fq=last_f["q"], flang=last_f["lang"], fch=last_f["ch"],
                                     fcodec=last_f["codec"], fsub=last_f["sub"], fsrc=last_f["src"]),
                           icon="DefaultFavourites.png")
    for s in filtered:
        li = xbmcgui.ListItem(label=stream_label(s))
        li.setArt(art_for(meta, video))
        # popis a obrázky titulu do InfoTagu (panel s detailem); stopáž a hodnocení ne — skin by
        # z nich udělal sloupce a ukrojil šířku popisku streamu
        fill_info(li, meta, "series" if video else ctype, video=video, tech=False)
        # Title = popis streamu: část zobrazení Arctic Fuse kreslí v řádku `ListItem.Title`, ne popisek,
        # a pak byl celý seznam jen „Matrix“ pod sebou (Office 2026-09-14). Název filmu v OSD dává
        # přehrávaná položka z play(), ne tahle.
        li.getVideoInfoTag().setTitle(li.getLabel())
        fill_streamdetails(li, s)
        apply_watched(li, item_id, [(L(30070), runplugin(action="download", url=s["url"], name=f"{title} [{s['label']}]",
                                                         id=item_id, type=ctype, series=series_id, alt=alt))])
        li.setProperty("IsPlayable", "true")
        # přehrání jde přes plugin (ne přímo URL), aby služba věděla, co se hraje
        url = build_url(action="play", type=ctype, id=item_id, series=series_id, alt=alt, url=s["url"],
                        subs="|".join(s.get("subtitles") or []), pref=pref_param(s))
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    if strict and has_fulltext_source:
        # i mezi nalezenými streamy může být omyl (viz `phrase_leads`) — možnost
        # dohledat víc je dobré mít i tady, ne jen když se nenajde nic
        fulltext_item(ctype, item_id, series_id, alt)
    xbmcplugin.endOfDirectory(HANDLE)


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


def play(apis, ctype, item_id, series_id=None, url=None, alt=None, subs="", pref="", ask=""):
    """`ask=1` = přehratelná položka Nokturna (widget, detail, kontextové menu) nebo player TMDb
    Helperu v režimu „Vybrat ze seznamu streamů“ → výběr dialogem s filtrem (`choose_stream`).
    Ve výpisu Nokturna je v tom režimu film složkou (`add_playable`), klik sem nevede.
    „Přehrát nejlepší automaticky“ hraje rovnou, „Zeptat se v dialogu“ se ptá vždy; bez `ask`
    (Up Next, HA) se v režimu 1 neptá."""
    mode = setting("stream_mode", "1")
    meta, video = load_meta(apis, ctype, item_id, series_id)
    # u seriálu si pamatujeme, jaký stream si uživatel vybral — další díl (Up Next,
    # Pokračovat, widget) pak jede stejně bez ptaní; klíč je seriál, ne díl
    pref_key = (series_id or split_episode_id(item_id)[0]) if video else None
    if url:
        chosen = {"url": url, "subtitles": [s for s in subs.split("|") if s]}
        if pref_key and pref_from_param(pref):
            STORE.set_stream_pref(pref_key, pref_from_param(pref))
    else:
        errors = []
        # z přehrání (widget, TMDb Helper) je jinak vidět jen točící se kolečko Kodi — streamy se
        # načítají i 15 s, tak aspoň stejný průběh jako nad seznamem streamů
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
        if remembered is None and (mode == "2" or (ask and mode == "1")):
            picked = choose_stream(streams)
            if picked is None:
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
                return
            chosen = picked
            if pref_key:
                STORE.set_stream_pref(pref_key, stream_signature(chosen))
    title = (video or {}).get("title") or display_name(meta)
    # label i InfoTag: při přímém otevření (JSON-RPC, widgety) nemá Kodi původní položku seznamu
    li = xbmcgui.ListItem(label=title, path=resolve_url(apis, chosen["url"]))
    li.setArt(art_for(meta, video))
    fill_info(li, meta, "series" if video else ctype, video=video)
    subtitles = [resolve_url(apis, s) for s in chosen.get("subtitles") or []]
    if subtitles:
        li.setSubtitles(subtitles)
    STORE.remember_item(item_id, snapshot(meta, ctype, video, series_id, alt))
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    # bez roku – ten se posílá zvlášť polem `year`, display_name() by ho zdvojil
    stats_title = episode_stats_title(video, meta)
    mark_playing(item_id, stats_title, year if year.isdigit() else None, "series" if video else ctype)
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
    if not STORE.item(key):
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
        ctx = [(L(30083) if status in ("queued", "running") else L(30084), runplugin(action="download_remove", id=d["id"]))]
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
        "setup_wizard": lambda: (setup_wizard(force=True),
                                 xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "sub_status": sub_status,
        "speedtest": speedtest,
        "update_repos": update_repos,
        "tmdbhelper_player": tmdbhelper_player,
        "sync_now": sync_now,
        "whats_new": whats_new,
        "ha_files": list_ha_files,
        "settings": lambda: (xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False), ADDON.openSettings()),
    }
    try:
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
        elif action == "genres":
            list_genres(apis, p["type"], p["catalog"], p.get("src", "luna"), show_all=not p.get("noall"))
        elif action == "catalog":
            list_catalog(apis, p["type"], p["catalog"], p.get("src", "luna"), genre=p.get("genre"),
                         search=p.get("search"), skip=int(p.get("skip") or 0))
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
        elif action == "streams":
            list_streams(apis, p["type"], p["id"], p.get("series"), alt=p.get("alt"),
                        fq=p.get("fq", ""), flang=p.get("flang", ""), fch=p.get("fch", ""),
                        fcodec=p.get("fcodec", ""), fsub=p.get("fsub", ""), fsrc=p.get("fsrc", ""),
                        fulltext=p.get("fulltext", ""))
        elif action == "streams_filter":
            streams_filter(apis, p["type"], p["id"], p.get("series"), p.get("alt"),
                          p.get("fq", ""), p.get("flang", ""), p.get("fch", ""), p.get("fcodec", ""),
                          p.get("fsub", ""), p.get("fsrc", ""))
        elif action == "play":
            play(apis, p["type"], p["id"], p.get("series"), url=p.get("url"), alt=p.get("alt"), subs=p.get("subs", ""),
                 pref=p.get("pref", ""), ask=p.get("ask", ""))
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
    except Errors as e:
        log_error(e)
        _fail(action, describe_error(e))
    except Exception as e:  # noqa: BLE001 – KeyError z chybějícího parametru, RuntimeError z Kodi API…
        # bez úklidu handle by Kodi u přehrání čekalo na timeout a hlásilo „Chyba skriptu"
        log_error(f"{action}: {traceback.format_exc()}")
        _fail(action, f"{type(e).__name__}: {e}")


def _fail(action, message):
    notify(message, xbmcgui.NOTIFICATION_ERROR, 5000)
    if action in ("play", "play_ws", "play_hs", "play_dav"):
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
    elif action not in ("download", "download_ws", "download_hs", "toggle_fav"):
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router(sys.argv[2] if len(sys.argv) > 2 else "")
