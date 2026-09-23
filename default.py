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
import random
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

# Pozor na `reuselanguageinvoker` v addon.xml: tělo tohohle souboru se spouští při
# každém kliknutí znovu, ale `sys.path` patří interpretu a ten zůstává. Bez téhle
# podmínky by cesta přibývala při každém kliknutí donekonečna.
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
from luna_api import (LunaApi, LunaError, diagnose as luna_diagnose,  # noqa: E402
                      discover as luna_discover, parse_base_url, parse_token)
from cinemeta_api import CinemetaApi, CinemetaError  # noqa: E402
from tmdb_api import TmdbApi, TmdbError  # noqa: E402
from trend_api import CATALOG_ID as TREND_CATALOG_ID, TrendApi  # noqa: E402
from dash_api import DashApi
from opensubtitles_api import OpenSubtitlesApi  # noqa: E402
from sosac_api import SosacError, is_sosac_id as _is_stremio_sosac_id  # noqa: E402
from sosac_direct import EXPORT as SOSAC_EXPORT, SosacDirect, is_direct_id  # noqa: E402
from enrich import enrich, enrich_one, shutdown_pool as release_enrich  # noqa: E402
import foryou  # noqa: E402
import servers  # noqa: E402
from hellspy_api import HellspyApi, HellspyError  # noqa: E402
from sledujteto_api import SledujtetoApi, SledujtetoError  # noqa: E402
from fastshare_api import FastshareApi, FastshareError  # noqa: E402
from cztor_api import CztorApi, CztorError  # noqa: E402
from prehrajto_api import PrehrajtoApi, PrehrajtoError  # noqa: E402
from storage_api import SLOTS as STORAGE_SLOTS, StorageApi, StorageError, parse_ref  # noqa: E402
from accounts import (FAIL as ACC_FAIL, OFF as ACC_OFF, OK as ACC_OK,  # noqa: E402
                      WARN as ACC_WARN, problems as accounts_problems,
                      PAUSE_CHOICES, pause as accounts_pause,
                      paused as accounts_paused, paused_for as accounts_paused_for)
from store import WATCHED_MAX, Store, migrate_profile  # noqa: E402
from source_errors import describe_failure, summarize as summarize_failures  # noqa: E402
from sync import sync_once  # noqa: E402
import kodi_settings  # noqa: E402
import setsync  # noqa: E402
import syncbox  # noqa: E402
from streams import estimate_rank, langs_from_name, parse_stream, subs_from_name  # noqa: E402
from tracks import FILE_CODES, SUBTITLE_FALLBACK, decode_subtitle, subtitle_format, subtitle_lang  # noqa: E402
from trakt_api import TraktApi, TraktError  # noqa: E402
from webshare_api import SORTS, WebshareApi, WebshareError, human_size  # noqa: E402
import kodi_marks  # noqa: E402 – vedle default.py, ne kopie jádra (čte videodatabázi Kodi)
import kodi_sources  # noqa: E402 – vedle default.py, sdílený výčet zdrojů do statistik
from engine import AUDIO_PROBE_MAX, DEFAULT_RUNTIME_S, Engine, NokturnoError, runtime_minutes  # noqa: E402
from abort import Aborted  # noqa: E402
from crash import CrashReporter  # noqa: E402

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")


# Líné importy. `remote_setup` táhne `http.server`, `qr` skládá PNG a `transfer`
# s `sealbox` kryptografii — dohromady jde o nejtěžší část importů a potřebují je jen
# tři obrazovky nastavení, ne menu, výpisy ani přehrání. `import` uvnitř funkce je po
# prvním volání jen vyhledání v `sys.modules`, takže se to nevyplatí obcházet cachí.
def _qr():
    import qr
    return qr


def _remote_setup():
    import remote_setup
    return remote_setup


def _transfer_core():
    # jmenuje se takhle, aby se `transfer_*` funkce níž nepotkaly jménem s modulem
    import transfer
    return transfer


class _KodiLogHandler(logging.Handler):
    """Varování z knihovny (např. `store.py`: selhaný zápis souboru) do kodi.log —
    bez toho by je Python jen tiše pustil na stderr, kam se v Kodi nikdo nedívá."""
    nokturno = True   # značka pro `_install_log_handler()`, `isinstance` tu nefunguje

    def emit(self, record):
        level = xbmc.LOGERROR if record.levelno >= logging.ERROR else xbmc.LOGWARNING
        xbmc.log(f"[{ADDON_ID}/{record.name}] {self.format(record)}", level)


def _install_log_handler():
    """Přidá handler jen jednou za život interpretu. S `reuselanguageinvoker` se tenhle
    soubor spouští znovu při každém kliknutí, ale kořenový logger je v modulu `logging`,
    který zůstává — bez téhle kontroly by se po deseti kliknutích každé varování
    zapsalo do kodi.log desetkrát.

    Poznávat handler přes `isinstance` tu NEJDE: každé spuštění vyrobí novou třídu
    `_KodiLogHandler`, takže handler z minula je instancí jiné třídy téhož jména.
    Proto značka atributem."""
    koren = logging.getLogger()
    if not any(getattr(h, "nokturno", False) for h in koren.handlers):
        koren.addHandler(_KodiLogHandler())
    koren.setLevel(logging.WARNING)


_install_log_handler()

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
LANG_CATALOG_WORKERS = 4   # souběžně zkoumaných kandidátů (víc riskuje 429 od HellSpy)
LANG_CATALOG_TTL = 8 * 3600  # rychlost ověřena (30/30 do minuty) — teď už se to smí cachovat
# Kdy se seznam „Nově přidané s CZ dabingem/titulky" naposledy otevřel. Zahřívání na
# pozadí je nejdražší práce, kterou doplněk dělá sám od sebe (až 60 kandidátů × všechny
# zapnuté zdroje, každých 6 h) — u instalace, která ten seznam nikdy neotevřela, je to
# čirý odpad a právě tahle cesta vyvolala blokaci HellSpy (audit 2026-09-19, nález 12).
LANG_SEEN_KEY = "lang_catalog_seen"
LANG_SEEN_DAYS = 14        # jak dlouho po otevření se seznam ještě zahřívá na pozadí
# O kolik dřív než vyprší cache ji zahřívání přepočítá. Musí být aspoň tak velké jako
# rozdíl mezi `LANG_CATALOG_TTL` a intervalem zahřívání (`service.LANG_WARM_EVERY`, 6 h),
# jinak kolo zahřívání jen přečte platnou cache a nechá ji vypršet mezi dvěma koly.
LANG_REFRESH_AFTER = LANG_CATALOG_TTL - 6 * 3600
LANG_LOCK_PROP = "nokturno.lang_busy"  # zámek přes okno (sdílené mezi procesy) proti souběžnému přepočtu
LANG_PROGRESS_PROP = "nokturno.lang_progress"  # živý postup přepočtu (viz _build_lang_catalog), sdílený
                                                # stejně jako zámek — kdo na zámek čeká, si z něj přečte,
                                                # jak daleko je proces, co ho drží
ACCOUNTS_TRIGGER_PROP = "nokturno.accounts_trigger"   # stejný literál jako v service.py — žádost o obnovu
                                                      # stavu zdrojů hned, viz refresh_accounts_after_test
LANG_TRIGGER_PROP = "nokturno.lang_trigger"  # žádost o okamžitý přepočet na pozadí (viz lang_catalog_trigger
                                              # tady a lang_trigger_watcher ve service.py) — stejný literál
                                              # v obou souborech, stejně jako WARM_PROP
LANG_LOCK_WAIT = 90    # s – radši počkat na cizí přepočet, než ho spustit podruhé souběžně
LANG_LOCK_POLL = 1     # s – jak často se během čekání kontroluje, jestli zámek zase zmizel
LANG_LOCK_STALE = 8 * 60  # s – nejdelší pozorovaný běh byl pod 4 min (60 kandidátů); zámek starší
                          # než tohle je nejspíš od procesu, co ho Kodi zabilo, ne od živého výpočtu
# „Pro tebe" — doporučení k naposledy zhlédnutým titulům (TMDB `recommendations`,
# bez vlastního klíče přes dashboard `/similar`). Je to řádově levnější než jazykové
# katalogy výš (žádné hledání ve zdrojích, jen TMDB, a detaily titulů má 30denní cache
# sdílená se všemi katalogy), ale platí tu totéž pravidlo: na pozadí se zahřívá jen typ,
# který uživatel za posledních `FORYOU_SEEN_DAYS` dní otevřel (audit 2026-09-19, nález 12).
FORYOU_TTL = 24 * 3600
FORYOU_SEEN_KEY = "foryou_seen"
FORYOU_SEEN_DAYS = 14
# O kolik dřív než vyprší cache ji zahřívání přepočítá — musí být aspoň interval
# zahřívání (`service.WARM_EVERY`, 2,5 h), jinak kolo jen přečte platnou cache a nechá
# ji vypršet mezi dvěma koly (tatáž past jako u `LANG_REFRESH_AFTER`, 6.2.2 nález 29).
FORYOU_REFRESH_AFTER = FORYOU_TTL - 3 * 3600
FORYOU_DOWN_KEY = "nokturno:foryou:down"   # značka výpadku, vzor `trend_api.DOWN_KEY`
FORYOU_DOWN_TTL = 300
FORYOU_STALE_TTL = 14 * 86400   # jak staré doporučení se ještě ukáže, když TMDB neodpoví
FORYOU_PAGES = 5   # z kolika stránek katalogu se losuje „Náhodný film" (víc = pestřejší)
RANDOM_CANDIDATES = 8   # kolik titulů se u „Náhodného filmu" ověřuje na preferovaný jazyk
RANDOM_BUDGET_S = 15.0  # na celé ověřování; pak se vezme cokoli (lepší titul bez CZ než čekání)
RANDOM_WORKERS = 4
SOSAC_TAG = "[COLOR FFE0A040]Sosáč[/COLOR]"
HS_TAG = "[COLOR FFFF8A6B]HellSpy[/COLOR]"
ST_TAG = "[COLOR FF4DD0C0]Sledujteto[/COLOR]"
FS_TAG = "[COLOR FFF2C14E]FastShare[/COLOR]"
PT_TAG = "[COLOR FF9AD5FF]Přehraj.to[/COLOR]"
CZ_TAG = "[COLOR FFFF6FB5]CZtor[/COLOR]"
WS_TAG = "[COLOR FF60B0FF]WebShare[/COLOR]"
DAV_COLOR = "FFB0E57C"
DAV_TAG = f"[COLOR {DAV_COLOR}]Úložiště[/COLOR]"
LUNA_TAG = "[COLOR FFB39DFF]Luna[/COLOR]"
SOURCE_TAGS = {"main": LUNA_TAG, "search": WS_TAG, "sosac": SOSAC_TAG, "ws": WS_TAG, "hs": HS_TAG, "st": ST_TAG,
               "fs": FS_TAG, "pt": PT_TAG, "cz": CZ_TAG, "dav": DAV_TAG}
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
Errors = (LunaError, CinemetaError, TmdbError, SosacError, WebshareError, HellspyError, SledujtetoError, FastshareError, CztorError,
          TraktError, StorageError, NokturnoError)

# Tvar dat v cache API (`Store.cached()`). Zvednout jen když se změní, co se do cache
# ukládá nebo jak se čte (nové pole v odpovědi, jiný klíč, jiná struktura) — staré záznamy
# by jinak přežily klidně týdny, než by je vytlačilo přirozené vypršení TTL. Do 6.1.4 se
# cache mazala při KAŽDÉ nové verzi doplňku: každá oprava textů tak celé instalaci
# vyhasila katalogy, streamy i metadata a první otevření po aktualizaci bylo studené
# (a zdroje včetně TMDB dostaly od všech instalací naráz vlnu dotazů).
CACHE_FORMAT = "6.1.4"
_ADDON_VERSION = ADDON.getAddonInfo("version")


def migrate_on_start():
    """Při startu pluginu: cache smazat jen při změně `CACHE_FORMAT`; při každé nové verzi
    doplňku (i downgradu) jen říct službě, ať s dalším statistickým hlášením nečeká až
    SEND_EVERY (6 h) — případná zpráva z dashboardu (odpověď na nahlášený log) se tak
    ukáže co nejdřív po aktualizaci, ne až za pár hodin."""
    if STORE.load("cache_version", "") != CACHE_FORMAT:
        STORE.clear_cache()
        STORE.save("cache_version", CACHE_FORMAT)
    if STORE.load("seen_version", "") != _ADDON_VERSION:
        STORE.save("seen_version", _ADDON_VERSION)
        xbmcgui.Window(10000).setProperty(FORCE_STATS_PROP, "1")


def info_install():
    """Tlačítko v kategorii Info: verze doplňku a anonymní id instalace.

    Kategorie Info nemá jediný obyčejný text: `<control type="label">` Kodi 21
    v nastavení doplňku odmítne (`error reading <control> tag`) a přeskočí celou
    kategorii, `title` se nevykreslí vůbec a zašedlý `edit` byl na TV nečitelný.
    Pevné hodnoty (web, fóra) proto nese rovnou popisek tlačítka; verze a id se
    do popisku napsat nedají, ukazuje je tenhle dialog."""
    xbmcgui.Dialog().textviewer(L(30432, "Info"),
                                "%s: %s[CR]%s: %s" % (L(30703, "Verze"), _ADDON_VERSION,
                                                      L(30695, "ID této instalace"), install_id() or "—"))


KOFI_URL = "ko-fi.com/matata86"
PAYPAL_URL = "paypal.me/matata86"
BITCOIN_ADDRESS = "bc1qhjwt8xxmuym0xsd50yfpvjph00386uz73gqwlc"
DONATE_URL = servers.BASE + "/#podpora"


def info_donate():
    """Tlačítko v kategorii Info: kam poslat příspěvek.

    Adresy se z televize opsat nedají, proto je vedle nich QR na sekci Podpora
    na domovské stránce (`DONATE_URL`) — z mobilu se odtud dá kliknout na PayPal
    i zkopírovat bitcoinová adresa. Okno je stejné jako u „Nastavit z mobilu“:
    obrázek se dá Kodi jen jako soubor."""
    qr_path = os.path.join(PROFILE, "donate-page.png")
    backdrop = os.path.join(PROFILE, "remote-setup-bg.png")
    try:
        if not os.path.exists(qr_path):
            with open(qr_path, "wb") as f:
                f.write(_qr().to_png(_qr().encode(DONATE_URL), scale=10, border=2))
        if not os.path.exists(backdrop):   # jednobarevná plocha, ztmaví se `colorDiffuse`
            with open(backdrop, "wb") as f:
                f.write(_qr().to_png([[False]], scale=1, border=0))
    except Exception as e:  # noqa: BLE001 – bez QR se ukáže aspoň adresa
        log_error(e)
        qr_path = ""
    window = DonateWindow(qr_path, backdrop)
    try:
        window.doModal()
    finally:
        del window


class DonateWindow(xbmcgui.WindowDialog):
    """Adresy na příspěvek a QR na bitcoinovou adresu. Zavře se čímkoli."""
    CLOSE_ACTIONS = (7, 9, 10, 13, 92, 100, 401)   # OK, Zpět, Stop, klik, ťuknutí

    def __init__(self, qr_path, backdrop_path=""):
        super().__init__()
        if backdrop_path:   # bez podkladu prosvítá dialog nastavení a text je nečitelný
            self.addControl(xbmcgui.ControlImage(0, 0, 1280, 720, backdrop_path, colorDiffuse="FF0D0B14"))
        self.addControl(xbmcgui.ControlLabel(90, 70, 1100, 50, "[B]%s[/B]" % L(30697, "Podpořit projekt"),
                                             font="font13", textColor="FFFFFFFF"))
        if qr_path:
            self.addControl(xbmcgui.ControlImage(90, 150, 380, 380, qr_path, aspectRatio=2))
        text = xbmcgui.ControlTextBox(500, 160, 700, 380, font="font12", textColor="FFE6E1F0")
        self.addControl(text)
        text.setText("%s[CR][CR][B]%s[/B][CR]%s[CR][CR][B]%s[/B][CR]%s[CR][CR][B]%s[/B][CR]%s[CR][CR]%s"
                     % (L(30706, "Naskenuj QR kód a otevře se stránka s možnostmi podpory."),
                        "Ko-fi", KOFI_URL, L(30704, "PayPal"), PAYPAL_URL, L(30699, "Bitcoin"), BITCOIN_ADDRESS,
                        L(30705, "Zavři tlačítkem Zpět.")))

    def onAction(self, action):
        if action.getId() in self.CLOSE_ACTIONS:
            self.close()


def install_id():
    """Anonymní id instalace ze statistik. Ukazuje se v Info, aby ho uživatel
    mohl uvést při hlášení problému — podle něj se v dashboardu najde jeho
    odeslaný log i hlášení o pádu.

    `Stats` se importuje až tady: na úrovni modulu v `default.py` není (drží se
    líně kvůli startu pluginu) a globální odkaz by tiše spadl na `NameError`
    — což se v betě 8 opravdu stalo a řádek v Info zůstal prázdný."""
    try:
        from stats import Stats
        return Stats(PROFILE).data["id"]
    except Exception:      # noqa: BLE001 – profil bez statistik nesmí shodit start
        return ""


# Zachyceno PŘED migrate_on_start(), který "seen_version" přepíše na aktuální verzi —
# nenulová a jiná než _ADDON_VERSION znamená, že tahle instalace už běžela dřív.
_PRIOR_SEEN_VERSION = STORE.load("seen_version", "")

migrate_on_start()

# Zvednout jen při věcné změně právního upozornění (ne u překlepu) — starší souhlas
# pak přestane platit a uživatel ho musí znovu potvrdit.
TERMS_VERSION = "2"

# Verze, se kterou právní upozornění vůbec poprvé vyšlo do stabilního vydání — jen ona
# se grandfatherí (viz `_existing_install`). Nikdy neměnit zpětně: i kdyby se text ještě
# před stabilním vydáním znovu upravil (další zvednutí TERMS_VERSION), tahle konstanta
# zůstává ukotvená na první verzi, aby ji grandfathering nesledoval navěky.
FIRST_TERMS_VERSION = "2"


# akce, které brána souhlasu pustí vždy: vedou k samotnému odsouhlasení
TERMS_FREE = ("info_terms", "settings")


def terms_accepted():
    """Souhlas drží **přepínač v nastavení** (kategorie Podmínky použití, `terms_ok`) — uživatel ho
    musí vidět a umět vzít zpět. Verze odsouhlaseného textu zůstává v profilu, protože
    v `settings.xml` by z ní bylo další pole navíc; zaškrtnutí platí vždy pro aktuální
    `TERMS_VERSION` a zapíše se při prvním přečtení."""
    if ADDON.getSetting("terms_ok") != "true":
        if STORE.load("terms_accepted", ""):
            STORE.save("terms_accepted", "")   # vzatý zpět; jinak by ho migrace zase zapnula
        return False
    if STORE.load("terms_accepted", "") != TERMS_VERSION:
        STORE.save("terms_accepted", TERMS_VERSION)
    return True


def migrate_terms():
    """Souhlas se z profilu (bety `7.6.0~beta1`–`~beta3`) překlopí do přepínače a starší
    instalace se odsouhlasí sama (`_existing_install`). Opačným směrem: po věcné změně
    textu (vyšší `TERMS_VERSION`) se přepínač vypne, aby ho uživatel potvrdil znovu.

    Běží **jednou za verzi textu** (značka `terms_migrated`). Bez toho by uložený souhlas
    v profilu zapínal přepínač po každém kliknutí, takže ruční vypnutí by nikdy nedrželo
    a doplněk by hledal dál."""
    if STORE.load("terms_migrated", "") == TERMS_VERSION:
        return
    STORE.save("terms_migrated", TERMS_VERSION)
    ulozena = STORE.load("terms_accepted", "")
    if ADDON.getSetting("terms_ok") == "true":
        if ulozena and ulozena != TERMS_VERSION:
            ADDON.setSetting("terms_ok", "false")
        return
    if ulozena == TERMS_VERSION or _existing_install():
        STORE.save("terms_accepted", TERMS_VERSION)
        ADDON.setSetting("terms_ok", "true")


def _existing_install():
    """Instalace, která běžela už před zavedením právního upozornění, se bere jako
    automaticky odsouhlasená — nikdo starý nemusí nic doklikávat. Platí jen pro
    `FIRST_TERMS_VERSION`: případná další věcná změna textu (zvednutí verze nad rámec
    prvního vydání) tenhle grandfathering neobchází, souhlas se pak musí znovu potvrdit
    i od existující instalace."""
    return TERMS_VERSION == FIRST_TERMS_VERSION and bool(_PRIOR_SEEN_VERSION) and _PRIOR_SEEN_VERSION != _ADDON_VERSION


def terms_text():
    return (L(30729, "Nokturno is primarily a player and manager for your own storage — content you upload "
                     "and expose yourself (e.g. via WebDAV) is played directly. As an optional add-on service, "
                     "you may connect some publicly available third-party search engines (WebShare, Sosáč, "
                     "HellSpy, Sledujteto, FastShare, Přehraj.to, CZtor, Luna, OpenSubtitles) — in that case "
                     "Nokturno is only a technical interface; it does not host, store, or provide any content "
                     "itself.\n\n"
                     "Use the add-on only for content you have a legal right, licence, or other legal title to "
                     "access. Searching for, accessing, or playing copyrighted content without the "
                     "rightsholders' consent is prohibited.\n\n"
                     "The add-on is provided \"as is\", with no warranty of functionality, availability, or "
                     "legality of third-party sources. Responsibility for how it is used lies solely with the "
                     "user. The operator reserves the right to restrict or terminate access at any time.")
            + "\n\n" +
            L(30734, "Where to report illegal content at each source:\n"
                     "WebShare: abuse@webshare.cz\n"
                     "Sosáč / Streamuj.tv: streamuj.tv/advertise (DMCA)\n"
                     "HellSpy: hellspy.to/contact\n"
                     "Sledujteto: sledujteto.cz/nahlasit-nelegalni-soubor\n"
                     "FastShare: fastshare.cz/abuse\n"
                     "Přehraj.to: prehrajto.cz/nahlasit-nelegalni-soubor\n"
                     "CZtor: contact via cztor.com/kontakt\n"
                     "OpenSubtitles: copyright@opensubtitles.org (DMCA)"))


def info_terms():
    """Tlačítko v kategorii Info: plný text právního upozornění, kdykoli k nahlédnutí."""
    xbmcgui.Dialog().textviewer(L(30728, "Legal notice"), terms_text())


def ensure_terms():
    """Musí proběhnout dřív, než plugin cokoli vyhledá nebo přehraje. Bez souhlasu se
    z UI nabídne otevření nastavení (přepínač je tam první kategorie), odjinud —
    z widgetu nebo JSON-RPC — jen oznámení: modál v cestě, kterou nikdo neklikl, by
    zasekl přehrávání i vypínání Kodi (viz CLAUDE.md). Souhlas tedy nejde dát jinde
    než v nastavení, takže ho nelze obejít spuštěním z widgetu."""
    if terms_accepted():
        return True
    if not browsing_nokturno():
        notify(L(30733, "First accept the legal notice in the Nokturno menu."), xbmcgui.NOTIFICATION_WARNING, 6000)
        return False
    dialog = xbmcgui.Dialog()
    dialog.textviewer(L(30728, "Legal notice"), terms_text())
    if not dialog.yesno(L(30728, "Legal notice"),
                        L(30739, "You have to agree to the terms of use first. Open the settings now?"),
                        yeslabel=L(30740, "Open the settings"), nolabel=L(30732, "I don't agree")):
        return False
    global ADDON
    ADDON.openSettings()            # přepínač je první kategorie, otevře se rovnou na něm
    ADDON = xbmcaddon.Addon()       # čerstvá instance, jinak by se četla hodnota z doby před dialogem
    return terms_accepted()


def note_lang_catalog_open(ctype):
    """Zapamatuje, že uživatel seznam s jazykem otevřel — podle toho se rozhoduje,
    jestli ho má služba zahřívat na pozadí (viz `service.lang_warm_urls`)."""
    with STORE.updating(LANG_SEEN_KEY, {}) as seen:
        seen[str(ctype)] = int(time.time())


def lang_catalog_wanted(ctype, days=LANG_SEEN_DAYS):
    """Otevřel uživatel tenhle seznam za posledních `days` dní?"""
    kdy = (STORE.load(LANG_SEEN_KEY, {}) or {}).get(str(ctype)) or 0
    return bool(kdy) and time.time() - kdy < days * 86400


def note_foryou_open(ctype):
    """Zapamatuje, že uživatel otevřel „Pro tebe" — podle toho se rozhoduje, jestli
    má služba seznam zahřívat na pozadí (viz `service.foryou_warm_urls`)."""
    with STORE.updating(FORYOU_SEEN_KEY, {}) as seen:
        seen[str(ctype)] = int(time.time())


def foryou_wanted(ctype, days=FORYOU_SEEN_DAYS):
    """Otevřel uživatel „Pro tebe" za posledních `days` dní?"""
    kdy = (STORE.load(FORYOU_SEEN_KEY, {}) or {}).get(str(ctype)) or 0
    return bool(kdy) and time.time() - kdy < days * 86400


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


# Kolik streamů se při přehrání zkusí, než to doplněk vzdá. Zdroj umí selhat celý —
# vyčerpaný kredit FastShare, vypršelý účet, výpadek — a do 7.6.1 tím spadlo celé
# přehrání, i když tentýž film ležel na dalších zdrojích (log uživatele 2026-09-22:
# 11 nalezených streamů, vybraný a jeho jediná sloučená kopie obě z FastShare
# s kreditem 0 MB → „FastShare: na soubor 7.6 GB nestačí kredit“ a konec). Strop je
# tu proto, že každý pokus je dotaz do zdroje: u opravdu mrtvého se jinak čeká na
# tolik timeoutů, kolik je streamů.
PLAY_FALLBACKS = 5


def play_candidates(chosen, streams, limit=PLAY_FALLBACKS):
    """Dvojice (odkaz, stream) k přehrání v pořadí: zvolený stream, jeho sloučené
    kopie, pak ostatní nálezy i s jejich kopiemi. Stream u odkazu je ten, ze kterého
    se po úspěchu berou titulky a jazyky — u sloučené kopie je to ona, ne rodič."""
    poradi, videne = [], set()

    def pridat(stream):
        for s in [stream] + list(stream.get("_alts") or []):
            url = s.get("url")
            if url and url not in videne:
                videne.add(url)
                poradi.append((url, s))

    pridat(chosen)
    for s in streams or []:
        if len(poradi) >= limit:
            break
        if s is not chosen:
            pridat(s)
    return poradi[:max(limit, 1)]


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


def get_prehrajto():
    """Přehraj.to — stačí přepínač. Účet je nepovinný: bez něj je vidět jen první
    strana hledání a hraje se překódovaný soubor, s Premium účtem původní."""
    if not on("pt_enabled", "false"):
        return None
    return PrehrajtoApi(setting("pt_email").strip(), setting("pt_password"), cache=STORE)


def cztor_client():
    """Klient CZtor i bez spárování — pro párování a stav účtu. Tokeny drží úložiště
    doplňku, takže je vidí plugin i služba (obnovovací token se použitím mění)."""
    name = xbmc.getInfoLabel("System.FriendlyName") or "Kodi"
    return CztorApi(STORE, device_name=f"Nokturno ({name})")


def get_cztor():
    """CZtor — jen zapnutý a spárovaný. Heslo účtu doplněk nikdy nevidí (párování PINem)."""
    if not on("cz_enabled", "false"):
        return None
    api = cztor_client()
    return api if api.paired() else None


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


# Vykoupení z věznice Shawshank: titulek k němu na OpenSubtitles je vždycky,
# takže prázdná odpověď znamená vadný klíč, ne titul bez titulků
OS_PROBE_ID = "tt0111161"


def get_opensubtitles():
    """Titulky z OpenSubtitles — záchrana pro tituly, které je ve zdrojích nemají.

    Klíč k API si uživatel nezakládá: rozdává ho dashboard (`/os-key`, cache týden),
    protože do repozitáře nesmí a proxovat dotazy přes server nejde — denní kvóta
    stahování se u OpenSubtitles počítá na IP toho, kdo stahuje. Bez klíče (dashboard
    nedostupný, nebo ho nemá nastavený) se zdroj tiše vynechá a titulky se berou jen
    ze souboru a z WebShare jako dřív.

    Jméno a heslo jsou dobrovolná — bez nich platí pět stažených titulků na IP za den,
    s účtem dvacet. Heslo jde jen do `/login`, ukládá se z něj jen vydaný token.
    """
    if not on("os_enabled"):
        return None
    key = get_dash().opensubtitles_key()
    if not key:
        return None
    return OpenSubtitlesApi(key, store=STORE, username=setting("os_username"),
                            password=setting("os_password"),
                            user_agent="Nokturno v%s" % _ADDON_VERSION)


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
        # Adresa má výchozí hodnotu, takže je vyplněná i u vypnuté Luny — bez
        # `luna_enabled` by stav zdrojů hlásil „běží, ale chybí token" každému,
        # kdo zdroj nikdy nezapnul.
        "luna_enabled": on("luna_enabled"),
        "luna_url": setting("luna_url", "http://192.168.1.10:7126"),
        # Kodi drží token Luny pod klíčem `token`, jádro ho zná jako `luna_token`.
        # Klienty si KodiEngine staví z `get_*()`, takže do 6.3.1 tenhle klíč jádro
        # vůbec nepotřebovalo — stav zdrojů podle něj ale pozná „Luna bez tokenu"
        # a bez něj hlásil chybějící token i u správně nastavené Luny.
        "luna_token": setting("token") if on("luna_enabled") else "",
        "cz_enabled": on("cz_enabled", "false"),
        "ws_username": setting("ws_username") if on("ws_enabled", "false") else "",
        "st_email": setting("st_email") if on("st_enabled", "false") else "",
        "fs_username": setting("fs_username") if on("fs_enabled", "false") else "",
        # jádro si podle e-mailu pozná účet (stránkování a původní soubor) — heslo
        # sem nepatří, klienta si staví `get_prehrajto()`
        "pt_enabled": on("pt_enabled", "false"),
        "pt_email": setting("pt_email").strip() if on("pt_enabled", "false") else "",
        "hs_enabled": on("hs_enabled", "false"),
        "tmdb_api_key": setting("tmdb_api_key"),
        # hlavičky souborů ze společné cache serveru (`Engine._media_hints`): dotaz
        # prozradí serveru identy otvíraných souborů, proto jen s povolenými statistikami
        "media_hints": on("stats_enabled", "false"),
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
                 "hs": get_hellspy, "st": get_sledujteto, "fs": get_fastshare, "pt": get_prehrajto, "cz": get_cztor, "storages": get_storages, "tmdb": get_tmdb,
                 "cinemeta": get_cinemeta, "trend": get_trend, "dash": get_dash,
                 "osub": get_opensubtitles}

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
    osub = property(lambda self: self._client("osub"))
    hs = property(lambda self: self._client("hs"))
    st = property(lambda self: self._client("st"))
    fs = property(lambda self: self._client("fs"))
    pt = property(lambda self: self._client("pt"))
    cz = property(lambda self: self._client("cz"))

    def cztor_client(self):
        return cztor_client()
    storages = property(lambda self: self._client("storages"))
    tmdb = property(lambda self: self._client("tmdb"))
    cinemeta = property(lambda self: self._client("cinemeta"))
    trend = property(lambda self: self._client("trend"))
    dash = property(lambda self: self._client("dash"))


def get_apis():
    """Klienty zdrojů pod jmény, na která je zvyklý zbytek doplňku, plus jádro pod `engine`."""
    engine = KodiEngine()
    return {"engine": engine, "luna": engine.luna, "sosac": engine.sosac, "ws": engine.ws, "hs": engine.hs,
            "st": engine.st, "fs": engine.fs, "pt": engine.pt, "cz": engine.cz, "dav": engine.storages, "cinemeta": engine.cinemeta, "sosac_db": engine.sosac_db,
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
    FastshareError: "FastShare", PrehrajtoError: "Přehraj.to", CztorError: "CZtor", StorageError: L(30405, "Úložiště"),
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


# ikony, které smí poslat dashboard (`dash_api.ICONS`) → naše sada
DASH_ICONS = {
    "": "DefaultVideoPlaylists.png", "movies": "DefaultMovies.png", "series": "DefaultTVShows.png",
    "star": "DefaultFavourites.png", "top": "DefaultMusicTop100.png", "new": "DefaultRecentlyAddedMovies.png",
    "halloween": "DefaultAddonVideo.png", "calendar": "DefaultYear.png", "trophy": "DefaultMusicTop100.png",
    "christmas": "DefaultYear.png", "fairytale": "DefaultMovies.png", "comedy": "DefaultMovies.png",
    "romance": "DefaultMovies.png", "family": "DefaultAddonVideo.png", "animation": "DefaultMovies.png",
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


CONCERTS_MENU_TTL = 3600
CONCERT_SOURCE_KEYS = (("ws", "webshare"), ("hs", "hellspy"), ("fs", "fastshare"))


def concert_sources(apis):
    """Zapnuté zdroje, které umí koncerty (server podle nich vybírá) — jméno tak, jak ho zná
    dashboard. Vypnutý zdroj je v `apis` None (`get_webshare` a spol.)."""
    return [name for key, name in CONCERT_SOURCE_KEYS if apis.get(key)]


def concerts_available(apis):
    """Má se položka Koncerty ukázat? Zdroj, který koncerty umí, a server, který je téhle
    instalaci vydává (zkušební provoz: dashboard má seznam instalací, ostatním vrací 404).
    Hodinu (`CONCERTS_MENU_TTL`) se drží jen kladná odpověď: záporná z chvíle, kdy Kodi
    po startu ještě nemá síť, schovávala Koncerty i po restartu (8.0.0). Opakovaný dotaz
    při výpadku nic nestojí, `DashApi` má vlastní pětiminutovou značku výpadku.
    Klíč `menu2`: 8.0.0 mohla na disku nechat zápornou odpověď pod starým klíčem."""
    dash = apis.get("dash")
    sources = concert_sources(apis)
    if not (dash and sources):
        return False
    try:
        stav = STORE.cached_if("nokturno:concerts:menu2", CONCERTS_MENU_TTL,
                               lambda: {"ok": bool(dash.concerts(sources, install=install_id()))},
                               ok=lambda s: bool(s and s.get("ok")))
    except Exception:      # noqa: BLE001 – výpadek serveru menu nezdrží
        return False
    return bool(stav and stav.get("ok"))


def list_concerts(apis, mode="", genre="", letter=""):
    """Katalog koncertů z dashboardu. Bez `mode` je to rozcestník (všichni / podle žánru /
    podle písmene) — dvě stě jmen v jednom výpisu se na ovladači neprochází. Koncert nemá
    IMDb id, jde tedy mimo běžné tituly: server drží hotové vnitřní odkazy a klient je
    jen přehraje (`play_ref`)."""
    dash = apis.get("dash")
    sources = concert_sources(apis)
    set_content("videos")
    if not dash or not sources:
        notify(L(30751, "Koncerty teď nejsou dostupné"), xbmcgui.NOTIFICATION_WARNING, 4000)
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    if not mode:
        list_concert_groups(dash, sources)
        return
    if mode == "recent":
        rows = dash.concert_recent(sources, install=install_id())
        xbmcplugin.setPluginCategory(HANDLE, L(30755, "Nově přidané"))
        if not rows:
            notify(L(30751, "Koncerty teď nejsou dostupné"), xbmcgui.NOTIFICATION_WARNING, 4000)
            xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
            return
        for c in rows:
            concert_item(c, c["artist"], with_artist=True)
        xbmcplugin.endOfDirectory(HANDLE)
        return
    if mode == "genres" or mode == "letters":
        skupiny = dash.concert_groups(sources, install=install_id()) or {}
        klic = "genres" if mode == "genres" else "letters"
        xbmcplugin.setPluginCategory(HANDLE, L(30752, "Podle žánru") if mode == "genres"
                                     else L(30753, "Podle abecedy"))
        for g in skupiny.get(klic) or []:
            li = xbmcgui.ListItem(label=f"{g['name']}  [COLOR {GREY}]{g['artists']}[/COLOR]")
            li.setArt({"icon": "DefaultMusicGenres.png" if mode == "genres" else "DefaultAddonsSearch.png"})
            url = build_url(action="concerts", mode="list",
                            **({"genre": g["name"]} if mode == "genres" else {"letter": g["name"]}))
            xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
        xbmcplugin.endOfDirectory(HANDLE)
        return

    rows = dash.concerts(sources, install=install_id(), genre=genre, letter=letter)
    xbmcplugin.setPluginCategory(HANDLE, genre or letter or L(30750, "Koncerty"))
    if not rows:
        notify(L(30751, "Koncerty teď nejsou dostupné"), xbmcgui.NOTIFICATION_WARNING, 4000)
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    for a in rows:
        # počet koncertů do labelu: label2 skiny u téhle položky nekreslí (viz 6.3.7)
        li = xbmcgui.ListItem(label=f"{a['name']}  [COLOR {GREY}]{a['concerts']}[/COLOR]",
                              label2=str(a["concerts"]))
        li.setArt({"icon": "DefaultMusicArtists.png"})
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="concert_artist", id=a["id"]), li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE)


def list_concert_groups(dash, sources):
    """Rozcestník nad katalogem koncertů. Když server skupiny neumí (starší verze, 404),
    zbude jediná položka se všemi interprety — katalog tím nepřestane fungovat."""
    skupiny = dash.concert_groups(sources, install=install_id()) or {}
    xbmcplugin.setPluginCategory(HANDLE, L(30750, "Koncerty"))
    # nově schválené koncerty nahoře; prázdný seznam (nic za 30 dní, starší server) položku schová
    if dash.concert_recent(sources, install=install_id()):
        folder_item(L(30755, "Nově přidané"), build_url(action="concerts", mode="recent"),
                    icon="DefaultRecentlyAddedMusicVideos.png")
    celkem = skupiny.get("artists") or 0
    vse = L(30754, "Všichni interpreti") + (f"  [COLOR {GREY}]{celkem}[/COLOR]" if celkem else "")
    folder_item(vse, build_url(action="concerts", mode="list"), icon="DefaultMusicArtists.png")
    if skupiny.get("genres"):
        folder_item(L(30752, "Podle žánru"), build_url(action="concerts", mode="genres"),
                    icon="DefaultMusicGenres.png")
    if skupiny.get("letters"):
        folder_item(L(30753, "Podle abecedy"), build_url(action="concerts", mode="letters"),
                    icon="DefaultAddonsSearch.png")
    xbmcplugin.endOfDirectory(HANDLE)


def list_concert_artist(apis, artist_id):
    """Koncerty jednoho interpreta. Položka = koncert, klik pustí největší soubor ze
    zapnutých zdrojů; ostatní soubory téhož koncertu jdou do `alts` a `play_ref` je
    zkusí, když první selže (tentýž vzorec jako sloučené verze u filmů)."""
    dash = apis.get("dash")
    data = dash.concert_artist(artist_id, concert_sources(apis), install=install_id()) if dash else None
    set_content("videos")
    if not data or not data["concerts"]:
        notify(L(30751, "Koncerty teď nejsou dostupné"), xbmcgui.NOTIFICATION_WARNING, 4000)
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    xbmcplugin.setPluginCategory(HANDLE, data["artist"] or L(30750, "Koncerty"))
    for c in data["concerts"]:
        concert_item(c, data["artist"])
    xbmcplugin.endOfDirectory(HANDLE)


def concert_item(c, artist, with_artist=False):
    """Řádek koncertu: klik pustí největší soubor ze zapnutých zdrojů, ostatní soubory téhož
    koncertu jdou do `alts`. `with_artist` = výpis napříč interprety (Nově přidané), kde
    jméno interpreta nenese nadpis obrazovky, takže musí do popisku."""
    files = c["files"]
    first = files[0]
    title = f"{artist} – {c['title']}" + (f" ({c['year']})" if c["year"] else "")
    # Do řádku patří i zdroj, velikost a délka — jinak není poznat, co klik pustí. Jméno
    # interpreta jen ve výpisu napříč interprety: jinak ho nese nadpis obrazovky a na řádek se
    # v Arctic Fuse vejde asi čtyřicet znaků, delší text si skin roluje pod rukama (6.3.7).
    # Label2 ani popis položky tenhle skin u ne-složky nekreslí, takže zbývá jen label.
    popis = (f"{artist} – " if with_artist else "") + c["title"] + (f" ({c['year']})" if c["year"] else "")
    udaje = [SOURCE_TAGS.get(first["ref"].split(":", 1)[0], ""), human_size(first["size"])]
    if first.get("height"):
        udaje.append(quality_name(first["height"]))
    if first["duration"]:
        udaje.append(f"{first['duration'] // 60} min")
    if len(files) > 1:
        udaje.append(f"×{len(files)}")
    popis += f"  [COLOR {GREY}]" + " · ".join(u for u in udaje if u) + "[/COLOR]"
    li = xbmcgui.ListItem(label=popis, label2=human_size(first["size"]) if first["size"] else "")
    # náhled dává sám zdroj (WebShare `img`, HellSpy `thumbs[0]`, FastShare `thumb`) —
    # koncert nemá IMDb id, takže obrázek odjinud vzít nejde; bez něj zbyde ikona
    nahled = next((f["img"] for f in files if f.get("img")), "")
    li.setArt({"icon": "DefaultMusicVideos.png",
               **({"thumb": nahled, "poster": nahled, "fanart": nahled} if nahled else {})})
    tag = li.getVideoInfoTag()
    tag.setTitle(title)
    if c["year"]:
        tag.setYear(c["year"])
    if first["duration"]:
        tag.setDuration(first["duration"])
    if first.get("width") and first.get("height"):
        tag.addVideoStream(xbmc.VideoStreamDetail(width=first["width"], height=first["height"]))
    # v popisu všechny soubory: zdroj, velikost a syrový název — ať jde poznat, co se pustí
    tag.setPlot("\n".join(f"{SOURCE_TAGS.get(f['ref'].split(':', 1)[0], '')} {human_size(f['size'])}  {f['name']}"
                          for f in files))
    li.setProperty("IsPlayable", "true")
    url = build_url(action="play_ref", ref=first["ref"], name=title,
                    alts="|".join(f["ref"] for f in files[1:]) or None)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)


def play_ref(apis, ref, name="", alts=""):
    """Přehrát vnitřní odkaz (`ws:`/`hs:`/`fs:`) bez titulu — koncerty. Když první nejde
    (smazaný soubor, vypršelý účet), zkusí se další kopie téhož koncertu z `alts`."""
    try:
        used, link = resolve_first(apis, [ref] + [a for a in (alts or "").split("|") if a])
    except Errors as e:
        notify(str(e) or L(30102), xbmcgui.NOTIFICATION_ERROR, 6000)
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    li = xbmcgui.ListItem(label=name or used, path=link)
    li.getVideoInfoTag().setTitle(name or used)
    kind = used.split(":", 1)[0]
    STORE.remember_item(used, {"type": kind, "id": used, "title": name or used, "art": {}})
    mark_playing(used, name, kind=kind, replay=sw_replay(action="play_ref", ref=used, name=name))
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


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
    li.setArt({"icon": icon or ICON})
    if context:
        li.addContextMenuItems(context)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)


def action_item(label, url, icon=None, context=None):
    """Položka, která jen spustí akci s dialogem (nastavení, novinky, průvodce). Není složka —
    Kodi ji po kliknutí spustí s handle −1, takže se nekreslí žádný výpis a do kodi.log
    nepadá `GetDirectory - Error getting …` (to hlásí každý `endOfDirectory(succeeded=False)`)."""
    li = xbmcgui.ListItem(label=label)
    li.setArt({"icon": icon or ICON})
    if context:
        li.addContextMenuItems(context)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)


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


def quality_name(height):
    """Výška obrazu → „4K“/„FHD“/„HD“/„SD“ (opak `TIER_SIZE`). Hranice jsou nižší než
    jmenovité rozlišení, protože kinoformát má obraz oříznutý — 1920×800 je pořád Full HD."""
    h = int(height or 0)
    for hranice, rank in ((1700, 4), (900, 3), (600, 2)):
        if h >= hranice:
            return QUALITY_NAMES[rank]
    return QUALITY_NAMES[1] if h else ""
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


DIRECT_SOURCES = ("ws", "hs", "st", "fs", "pt")   # fulltextové zdroje, kde bývá tentýž soubor jako u Luny


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
    def poradi(kv):
        """Zdroj, který nestihl rozpočet, má místo času značku „>20s“ (`str`) — řadí se
        nakonec. Bez tohohle rozlišení `sorted()` porovná `str` s `float` a spadne:
        `TypeError: '<' not supported between instances of 'str' and 'float'` (pád
        nahlášený z 6.2.7). Stačilo, aby se jeden zdroj opozdil a jiný odpověděl."""
        return (1, 0.0) if isinstance(kv[1], str) else (0, kv[1])
    zdroje = ", ".join(f"{k} {v}" for k, v in sorted((t.get("zdroje") or {}).items(), key=poradi))
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
                "sosac": "Sosáč", "hs": "HellSpy", "st": "Sledujteto", "fs": "FastShare", "pt": "Přehraj.to", "cz": "CZtor",
                "dav": L(30405, "Úložiště")}


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
    if setting("stream_filter_last") == "true":
        # „Automaticky použít poslední filtr“ — jen když u titulu něco nechá, jinak
        # by dialog začínal hláškou „Filtr nic nenechal“
        last = STORE.last_stream_filter() or {}
        last = {k: list(last.get(k) or []) for k in FILTER_KINDS}
        if any(last.values()) and apply_stream_filter(streams, **filter_params(last)):
            active = last
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
    rows = _remote_setup().parse_order(raw, set(STREAM_PARTS)) if raw else None
    if rows is None:
        rows = _remote_setup().parse_order(STREAM_LAYOUT_DEFAULT, set(STREAM_PARTS))
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


def mark_playing(key, title="", year=None, kind="movie", stream_url=None, stream_subs=None, stream_langs=None,
                 replay=None):
    # stream_url/stream_subs: vnitřní reference zvoleného streamu (ne podepsaný odkaz zdroje,
    # ten vyprší) — služba (Player.save_resume) si je uloží k pozici, ať se dá „Pokračovat ve
    # sledování“ pustit rovnou bez nového hledání (viz add_playable/add_snapshot_item)
    # stream_langs: jazyky zvuku, které o streamu tvrdí zdroj — služba podle nich pozná češtinu
    # i u souboru, jehož stopy jazyk neuvádějí (`Player.apply_tracks`, `tracks.pick_audio`)
    # replay: adresa pluginu, kterou totéž pustí ostatní ve skupině SyncWatch (služba ji
    # pošle, když je tohle Kodi vedoucí)
    xbmcgui.Window(10000).setProperty(PLAYING_PROP, json.dumps(
        {"id": key, "title": title, "year": year, "kind": kind,
         "stream_url": stream_url, "stream_subs": stream_subs, "stream_langs": stream_langs or [],
         "replay": replay}))


SUBS_DIR = os.path.join(PROFILE, "subs")
SUBS_MAX_BYTES = 2 * 1024 * 1024   # titulky mají desítky kB; víc je omyl odkazu (video)
SUBS_KEEP_S = 2 * 86400
# na titulky se před přehráním čeká nejvýš tolik — WebShare u nedostupného souboru odpovídá
# „temporarily unavailable“ až po 5–13 s (Office 2026-09-16) a přehrání by stálo
SUBS_BUDGET_S = 4.0


def subtitle_refs(apis, chosen, resolved_path, video=None, ctype="movie"):
    """Odkazy na titulky ke zvolenému streamu, případně zpřesněné otiskem souboru.

    Ve výpisu streamů se titulky z OpenSubtitles hledají podle IMDb id, takže sedí
    k titulu, ale ne nutně k tomuhle souboru (jiný sestřih, jiné fps). Otisk je páruje
    přímo se souborem, jenže stojí `Range` dotaz na jeho konec — proto se počítá až tady,
    u jednoho streamu, a jen když k němu nemáme lepší titulky: soubor s vlastními titulky
    ani nález z WebShare (ten je vybraný podle názvu releasu) se tím nezdržuje.
    """
    refs = list(chosen.get("subtitles") or [])
    if not refs or any(not r.startswith("os:") for r in refs):
        return refs
    try:
        presne = engine_of(apis).subtitles_by_hash(resolved_path, video, ctype)
    except Exception as e:  # noqa: BLE001 – titulky nesmí shodit přehrání
        xbmc.log(f"[{ADDON_ID}] titulky podle otisku: {e}", xbmc.LOGINFO)
        return refs
    if not presne:
        return refs
    xbmc.log(f"[{ADDON_ID}] titulky: otisk souboru sedí ({len(presne)})", xbmc.LOGINFO)
    return presne + [r for r in refs if r not in presne]


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


# Kde se Kodi potkávají (`sync_mode`): 0 = Home Assistant, 1 = dashboard (slepý relay).
#
# Třetí volba „HA i dashboard" existovala mezi `9.99.0~sync4` a `6.6.0~beta1`, kdy
# byl relay jediná cesta, jak mít dashboard a zároveň nepřijít o data v kartě HA.
# Od bety 1 chodí do skupiny i samotná integrace (pole „Kód skupiny z Kodi"), takže
# je to zbytečná druhá cesta k témuž — a dvě volby, které dělají totéž, se v UI
# nedají vysvětlit. Kdo má HA a chce dashboard, zadá týž kód i v integraci; HA je
# pak členem skupiny jako každé Kodi. Uložená „2" se při startu překlopí na relay
# (`migrate_sync_mode`).
SYNC_MODE_HA, SYNC_MODE_RELAY = "0", "1"
SYNC_MODE_BOTH_OLD = "2"          # jen pro migraci, do nastavení už se nedostane


def sync_via_relay():
    """Jede synchronizace přes dashboard, ne přes Home Assistant?"""
    return on("sync_enabled", "false") and setting("sync_mode") == SYNC_MODE_RELAY


def sync_via_ha():
    """Jede synchronizace přes Home Assistant?"""
    return on("sync_enabled", "false") and setting("sync_mode") == SYNC_MODE_HA


def migrate_sync_mode():
    """Uložená volba „HA i dashboard" (`2`) přejde na dashboard.

    Dashboard, ne Home Assistant: kdo si „obojí" vybral, chtěl dashboard a HA měl
    navíc. Kartu v HA dohoní tím, že týž kód skupiny zadá i v integraci — tam je
    od 6.6.0 pole „Kód skupiny z Kodi". Oznámení je jednorázové, ale samotné
    přepsání nastavení se dělá bezpodmínečně: volba `2` už v `settings.xml`
    neexistuje a Kodi by na ni spadlo zpátky na výchozí hodnotu.
    """
    # Číst se musí ze souboru, ne přes `getSetting`: volbu `2` už `settings.xml`
    # nezná, takže ji Kodi odmítne načíst (`failed to load value "2" for setting
    # sync_mode` při každém spuštění pluginu i služby) a `getSetting` vrátí výchozí
    # `0`. Podmínka nad ním by tedy nikdy nesedla, migrace by neproběhla a starý
    # zápis by v souboru zůstal napořád — včetně toho hluku v kodi.log.
    try:
        with open(os.path.join(PROFILE, "settings.xml"), encoding="utf-8") as f:
            ulozeno = f.read()
    except (IOError, OSError):
        return
    if not re.search(r'id="sync_mode"[^>]*>%s</setting>' % SYNC_MODE_BOTH_OLD, ulozeno):
        return
    ADDON.setSetting("sync_mode", SYNC_MODE_RELAY)
    notify(L(30688, "Synchronizace jede přes dashboard. S Home Assistantem zadej "
                    "týž kód skupiny i v nastavení integrace."), ms=8000)


def sync_settings():
    """(adresa HA, klíč) nebo None. Používá to i „Staženo v HA" a `list_ha_files()` —
    soubory stahuje a podepsané odkazy vydává HA, slepý relay žádné nemá."""
    if not sync_via_ha():
        return None
    url, key = setting("sync_url").strip(), setting("sync_key").strip()
    return (url, key) if url and key else None


def sync_targets():
    """Kam se tohle Kodi synchronizuje: `[("ha", (adresa, klíč))]` nebo
    `[("relay", kód)]`. Prázdný seznam = není kam. Seznam (a ne jedna hodnota)
    zůstal ze dvou středisek naráz — volajícím se tím nic nemění a kdyby se
    někdy vrátilo víc cílů, není co přepisovat."""
    if sync_settings():
        return [("ha", sync_settings())]
    if sync_via_relay() and setting("sync_code").strip():
        return [("relay", setting("sync_code").strip())]
    return []


# Okruhy nastavení a účtů umí jen relay — Home Assistant je střed pro `Store`,
# volby doplňku a přihlášení do jeho rozhraní nepatří (proto je `settings.xml`
# u obou přepínačů schovává, když je vybraný Home Assistant).
SYNC_CIRCLE_SETTINGS = {"watched": "sync_watched", "favourites": "sync_favourites",
                        "history": "sync_history", "settings": "sync_settings",
                        "accounts": "sync_accounts"}
SYNC_RELAY_ONLY = ("settings", "accounts")


def sync_circles(relay=None):
    """Okruhy zapnuté v nastavení. Prázdný výběr = neposílá se ani nepřijímá nic."""
    if relay is None:
        relay = setting("sync_mode") == SYNC_MODE_RELAY
    return tuple(okruh for okruh, klic in SYNC_CIRCLE_SETTINGS.items()
                 if on(klic, "false" if okruh in SYNC_RELAY_ONLY else "true")
                 and (relay or okruh not in SYNC_RELAY_ONLY))


def sync_values(relay=None):
    """Hodnoty nastavení pro okruhy `settings`/`accounts`, nebo None, když ani
    jeden neběží — pak jádro nastavení vůbec neřeší."""
    circles = sync_circles(relay)
    if not any(okruh in circles for okruh in SYNC_RELAY_ONLY):
        return None
    return kodi_settings.values(ADDON, ADDON_PATH)


def sync_write_settings(changes):
    """Zápis nastavení, které přišlo z druhého Kodi."""
    zapsano = kodi_settings.apply(ADDON, changes)
    if not zapsano:
        return
    if any(k.startswith("ws_") for k in zapsano):
        xbmcgui.Window(10000).clearProperty("nokturno.ws_token")   # jiný účet = nový login
    if any(setsync.circle_of(k) == setsync.CIRCLE_ACCOUNTS for k in zapsano):
        STORE.clear_cache()     # cache patří k účtům, které tu byly do teď
    xbmc.log(f"[{ADDON_ID}] synchronizace přepsala nastavení: {', '.join(sorted(zapsano))}",
             xbmc.LOGINFO)


def sync_relay_once(name=""):
    """Jedno kolo přes relay. Vrací totéž co `sync.sync_once`."""
    return syncbox.sync_once(STORE, setting("sync_code").strip(), circles=sync_circles(True),
                             name=name, settings=sync_values(True),
                             on_settings=sync_write_settings)


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
    """Ruční synchronizace — z hlavního menu i z nastavení.

    V režimu „obojí" se projdou obě střediska; výpadek jednoho nezastaví druhé
    a hlásí se, co se povedlo dohromady."""
    cile = sync_targets()
    jmeno = xbmc.getInfoLabel("System.FriendlyName")
    if not cile:
        notify(L(30186, "Synchronizace není zapnutá nebo chybí adresa a klíč"), xbmcgui.NOTIFICATION_WARNING, 5000)
    else:
        odeslano = prijato = 0
        chyby = []
        for kam, cfg in cile:
            if kam == "relay":
                ok, pushed, pulled, why = sync_relay_once(jmeno)
            else:
                ok, pushed, pulled, why = sync_once(STORE, cfg[0], cfg[1], jmeno,
                                                    circles=sync_circles(False))
            odeslano, prijato = odeslano + pushed, prijato + pulled
            if not ok:
                chyby.append(why)
        notify(f"{L(30188, 'Synchronizace selhala')}: {'; '.join(chyby)}" if chyby
               else (L(30187, "Synchronizováno: odesláno %d, přijato %d") % (odeslano, prijato)),
               xbmcgui.NOTIFICATION_ERROR if chyby else xbmcgui.NOTIFICATION_INFO, 5000)
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)


def _sync_apply(code):
    """Uloží kód skupiny a zapne synchronizaci přes relay."""
    ADDON.setSetting("sync_code", code)
    ADDON.setSetting("sync_mode", "1")
    ADDON.setSetting("sync_enabled", "true")


def sync_create():
    """Založí skupinu a ukáže kód, který se opíše na dalším Kodi.

    Kód je zároveň šifrovací klíč — server ho nikdy nevidí a bez něj data nikdo
    nepřečte ani neobnoví. Proto se ukazuje v `textviewer` (jde odrolovat a nechat
    na obrazovce), ne v mizející notifikaci."""
    code = setting("sync_code").strip()
    znovu = bool(code) and syncbox.valid_code(code)
    if not znovu:
        code = syncbox.new_code()
    try:
        syncbox.Relay(syncbox.keys_for(code), syncbox.device_id(STORE)).open_group()
    except syncbox.SyncError as e:
        xbmcgui.Dialog().ok(L(30180, "Synchronizace"), f"{L(30188, 'Synchronizace selhala')}: {e}")
        return
    _sync_apply(code)
    nadpis = L(30679, "Znovu otevřít připojení") if znovu else L(30671, "Skupina je založená. Na dalším Kodi zadej tento kód:")
    xbmcgui.Dialog().textviewer(L(30180, "Synchronizace"),
                                f"{nadpis}[CR][CR][B]{code}[/B][CR][CR]"
                                f"{L(30680, 'Připojení je otevřené 30 minut.')}[CR]"
                                f"{L(30672, 'Kód pustí nové zařízení dovnitř po 30 minut.')}")


def sync_join():
    """Připojí tohle Kodi ke skupině podle kódu z prvního Kodi."""
    code = xbmcgui.Dialog().input(L(30673, "Zadej kód skupiny z prvního Kodi"),
                                  defaultt=setting("sync_code").strip())
    if not code:
        return
    if not syncbox.valid_code(code):
        xbmcgui.Dialog().ok(L(30180, "Synchronizace"), L(30675, "Kód nemá správný tvar."))
        return
    code = syncbox.format_code(code)
    # `sync_circles(True)`: středisko se uloží až v `_sync_apply()` po úspěchu, ale
    # připojení je relay z definice — jinak by první kolo vynechalo nastavení a účty
    # právě u zařízení, které je potřebuje nejvíc
    ok, pushed, pulled, why = syncbox.sync_once(STORE, code, circles=sync_circles(True),
                                                name=xbmc.getInfoLabel("System.FriendlyName"),
                                                settings=sync_values(True),
                                                on_settings=sync_write_settings)
    if not ok:
        xbmcgui.Dialog().ok(L(30180, "Synchronizace"), f"{L(30188, 'Synchronizace selhala')}: {why}")
        return
    _sync_apply(code)
    xbmcgui.Dialog().ok(L(30180, "Synchronizace"),
                        f"{L(30674, 'Připojeno. Synchronizace běží na pozadí.')}[CR][CR]"
                        + (L(30187, "Synchronizováno: odesláno %d, přijato %d") % (pushed, pulled)))


def sync_leave():
    """Odhlásí tohle Kodi ze skupiny — na relayi smaže jeho blob. Uložená data
    zůstávají, jen se přestanou vyměňovat."""
    code = setting("sync_code").strip()
    if not code or not syncbox.valid_code(code):
        notify(L(30676, "Zatím žádná skupina"), xbmcgui.NOTIFICATION_WARNING, 5000)
        return
    if not xbmcgui.Dialog().yesno(L(30180, "Synchronizace"), L(30677, "Odejít ze skupiny?")):
        return
    try:
        syncbox.Relay(syncbox.keys_for(code), syncbox.device_id(STORE)).forget()
    except syncbox.SyncError as e:
        log_error(e)   # relay nedostupný: blob tam zůstane, retence ho po čase smaže
    ADDON.setSetting("sync_code", "")
    ADDON.setSetting("sync_enabled", "false")
    notify(L(30678, "Odešel jsi ze skupiny."), xbmcgui.NOTIFICATION_INFO, 5000)


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

# kategorie nastavení, které jdou vyplnit z mobilu; Pokročilé jsou jen tlačítka akcí,
# Stahování chce cestu vybranou v Kodi
REMOTE_SETUP_CATEGORIES = ("terms", "sources", "storage", "playback", "streamlist", "sync", "stats")
# kategorie, jejíž skupiny se na stránce z mobilu ukážou jako samostatné sekce (ne jako
# nadpisy uvnitř jedné) — "sources" slučuje deset dřívějších kategorií zdrojů/účtů do
# jedné kvůli stropu 20 kategorií v settings.xml, ale na mobilu má vypadat pořád jako
# deset rozbalovacích sekcí, ne jedna s 35 poli
REMOTE_SETUP_SPLIT = ("sources",)
REMOTE_SETUP_TIMEOUT = 1800
# tlačítka z settings.xml, která mají na stránce z mobilu vlastní akci: id → (akce, pole, která čte)
REMOTE_SETUP_ACTIONS = {"luna_find_action": ("luna_find", ["luna_url"]),
                        "luna_check_action": ("luna_check", ["luna_url", "token"]),
                        "terms_show_action": ("terms_show", [])}
STREAM_PART_LABELS = (("langs", 30501), ("size", 30502), ("video", 30503), ("audio", 30504), ("bitrate", 30505),
                      ("length", 30506), ("subs", 30507), ("source", 30508), ("file", 30509))
KODI_TAG_RE = re.compile(r"\[/?(?:B|I|CR|COLOR|UPPERCASE|LOWERCASE|CAPITALIZE|LIGHT)[^\]]*\]")


def _plain(text):
    return KODI_TAG_RE.sub(" ", text or "").strip()


def _group_fields(group):
    """Pole jedné skupiny `<group>` v `settings.xml`, ve tvaru pro formulář z mobilu."""
    fields = []
    if group.get("id") == "luna":
        fields.append({"type": "info", "label": L(30574, "Jak nastavit Lunu"),
                       "help": L(30575, "").replace("[CR]", "\n")})
    for node in group.findall("setting"):
        kind, control = node.get("type"), node.find("control")
        field = {"id": node.get("id")}
        if node.get("id") == "sync_code":
            # Skupina už je: kód se jen ukáže, aby se dal opsat na další Kodi —
            # je to zároveň šifrovací klíč a přepsat ho omylem by tohle Kodi
            # ze skupiny vyřadilo. Skupina ještě není: pole na opsání kódu
            # z prvního Kodi, služba se podle něj připojí do pěti minut.
            # Zakládání a opuštění zůstává na televizi (`sync_create`/`sync_leave`).
            kod = setting("sync_code").strip()
            if kod:
                fields.append({"type": "info", "label": _plain(L(30664, "Kód skupiny")), "help": kod})
                continue
            field["type"] = "text"
            field["default"] = ""
            field["label"] = _plain(L(30664, "Kód skupiny"))
            field["help"] = _plain(L(30707, "Kód z prvního Kodi, na kterém jsi skupinu založil. "
                                            "Zapni Synchronizaci, jako středisko zvol Dashboard "
                                            "Nokturna a ulož — toto Kodi se připojí do pěti minut."))
            field["enable"] = [("sync_enabled", "true"), ("sync_mode", "1")]
            fields.append(field)
            continue
        if node.get("id") in REMOTE_SETUP_ACTIONS:
            field["type"] = "action"
            field["action"], field["inputs"] = REMOTE_SETUP_ACTIONS[node.get("id")]
        elif node.get("id") == "stream_layout":
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
                                     "streamu, do Nezobrazovat přesuň, co se nemá ukazovat. Kvalita je "
                                     "vždy obrázek vlevo.")
        elif node.get("help"):
            field["help"] = _plain(L(int(node.get("help"))))
        dep = node.find("dependencies/dependency[@type='enable']")
        if dep is not None and dep.get("setting"):
            field["enable"] = (dep.get("setting"), (dep.text or "").strip())
        elif dep is not None:   # <and> s víc podmínkami (sekce Synchronizace)
            podminky = [(c.get("setting"), (c.text or "").strip())
                        for c in dep.findall("and/condition") if c.get("setting")]
            if podminky:
                field["enable"] = podminky
        fields.append(field)
    return fields


# --- SyncWatch: společné sledování ------------------------------------------------------
#
# Plugin tu jen zakládá skupinu, připojuje se a ukazuje okna. Samotnou synchronizaci
# přehrávání dělá služba (`service.SyncWatchManager`) — běží celou dobu, co je zařízení
# ve skupině, i když plugin dávno skončil. Domlouvají se přes profil (`syncwatch.json`:
# kód a token) a vlastnost okna `SW_PROP` (stav skupiny, který služba zapisuje).

SW_PROP = "nokturno.sw"


def _syncwatch():
    import syncwatch
    return syncwatch


def sw_session():
    return STORE.load("syncwatch", {}) or {}


def sw_status():
    try:
        return json.loads(xbmcgui.Window(10000).getProperty(SW_PROP) or "{}")
    except ValueError:
        return {}


def sw_save(session):
    STORE.save("syncwatch", session)   # služba se sem dívá každou sekundu (`SyncWatchManager`)


def sw_device_name():
    return (xbmc.getInfoLabel("System.FriendlyName") or "Kodi").strip()[:40]


def _swf(sid, fallback, *args):
    """Lokalizovaný řetězec s `%` a zálohou v kódu (nové řetězce Kodi načte až po restartu)."""
    text = L(sid, fallback)
    try:
        return text % args
    except TypeError:
        return fallback % args


def sw_title():
    return L(30800, "SyncWatch")


def sw_entry():
    """Položka SyncWatch v menu: napoprvé (a dokud to uživatel nevypne) návod,
    pak složka skupiny."""
    if not STORE.load("sw_intro_hidden", False):
        if not sw_intro():
            return
    xbmc.executebuiltin("Container.Update(%s)" % build_url(action="sw_menu"))


def _sw_backdrop():
    path = os.path.join(PROFILE, "sw-bg.png")
    if not os.path.exists(path):
        xbmcvfs.mkdirs(PROFILE)
        with open(path, "wb") as f:
            f.write(_solid_rgba_png((13, 11, 20), 255))
    return path


def _sw_button_textures():
    paths = (os.path.join(PROFILE, "sw-btn.png"), os.path.join(PROFILE, "sw-btn-focus.png"))
    for path, rgba in zip(paths, (((92, 68, 150), 230), ((124, 92, 200), 255))):
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(_solid_rgba_png(*rgba))
    return paths


class SyncWatchIntroWindow(xbmcgui.WindowDialog):
    """Návod: jak SyncWatch funguje. Dvě tlačítka — „Rozumím" a „Rozumím,
    příště nezobrazovat"; Zpět okno zavře a do skupiny se nejde."""
    BACK = (9, 10, 13, 92)

    def __init__(self):
        super().__init__()
        self.choice = None
        self.addControl(xbmcgui.ControlImage(0, 0, 1280, 720, _sw_backdrop(), colorDiffuse="FF0D0B14"))
        self.addControl(xbmcgui.ControlLabel(90, 50, 1100, 50, "[B]%s[/B]" % sw_title(),
                                             font="font13", textColor="FFFFFFFF"))
        text = xbmcgui.ControlTextBox(90, 110, 1100, 470, font="font12", textColor="FFE6E1F0")
        self.addControl(text)
        text.setText(L(30801, SW_INTRO_TEXT))
        bg, bg_focus = _sw_button_textures()
        self.ok = xbmcgui.ControlButton(90, 610, 360, 56, L(30802, "Rozumím"), font="font13",
                                        textColor="FFE6E1F0", focusedColor="FFFFFFFF", alignment=6,
                                        noFocusTexture=bg, focusTexture=bg_focus)
        self.hide = xbmcgui.ControlButton(470, 610, 520, 56, L(30803, "Rozumím, příště nezobrazovat"),
                                          font="font13", textColor="FFE6E1F0", focusedColor="FFFFFFFF",
                                          alignment=6, noFocusTexture=bg, focusTexture=bg_focus)
        self.addControl(self.ok)
        self.addControl(self.hide)
        self.ok.controlRight(self.hide)
        self.hide.controlLeft(self.ok)
        self.setFocus(self.ok)

    def onControl(self, control):
        self.choice = "hide" if control.getId() == self.hide.getId() else "ok"
        self.close()

    def onAction(self, action):
        if action.getId() in self.BACK:
            self.close()


SW_INTRO_TEXT = (
    "Sledujte jeden film nebo díl společně na víc zařízeních — každý u sebe, ale ve stejnou chvíli.[CR][CR]"
    "[B]1.[/B] Jeden založí skupinu a stane se vedoucím. Dostane kód, třeba SW-7K2Q-9MFX.[CR]"
    "[B]2.[/B] Ostatní (nejvýš 4 další zařízení) zadají kód v SyncWatch → Připojit se. Čekají, až vedoucí něco pustí.[CR]"
    "[B]3.[/B] Vedoucí pustí film normálně v Nokturnu. Stejný stream se sám spustí i u ostatních — "
    "začne se, až se načte všem.[CR]"
    "[B]4.[/B] Pauza, play a přetáčení od kohokoli platí pro všechny. Když se někomu načítá, ostatní počkají.[CR][CR]"
    "Všichni pustí přesně tentýž stream — stejnou kvalitu, zvuk i délku — každý přes svůj vlastní účet "
    "u zdroje. Kdo účet u zdroje vedoucího nemá, stream se mu nespustí.[CR]"
    "Skupina zanikne, když se k ní 5 minut nikdo nepřipojí, nebo když ji vedoucí ukončí. "
    "Co sledujete, server nevidí — je to zašifrované kódem skupiny."
)


def sw_intro():
    """Návod. Vrací True, když chce uživatel pokračovat do skupiny."""
    window = SyncWatchIntroWindow()
    try:
        window.doModal()
        choice = window.choice
    finally:
        del window
    if choice == "hide":
        STORE.save("sw_intro_hidden", True)
    return choice is not None


def sw_menu():
    """Složka SyncWatch: mimo skupinu Založit / Připojit, ve skupině stav a odchod."""
    session = sw_session()
    xbmcplugin.setPluginCategory(HANDLE, sw_title())
    if session.get("token"):
        status = sw_status()
        members = status.get("members") or []
        online = sum(1 for m in members if m.get("online")) or 1
        role = L(30804, "vedoucí") if session.get("leader") else L(30805, "člen")
        if not session.get("leader") and status.get("detached") and status.get("loaded"):
            # zastavil omylem nebo pustil něco jiného — skupina sleduje dál
            action_item(_swf(30835, "Vrátit se do filmu: %s", status.get("title") or ""),
                        build_url(action="sw_rejoin"), icon="DefaultVideo.png")
        action_item("%s · %s · %s" % (session.get("code", ""), role, _swf(30806, "připojeno: %d", online)),
                    build_url(action="sw_window"), icon="DefaultNetwork.png")
        if session.get("leader"):
            action_item(L(30807, "Zavřít skupinu pro další zařízení") if not status.get("locked")
                        else L(30808, "Otevřít skupinu pro další zařízení"),
                        build_url(action="sw_lock"), icon="DefaultAddonService.png")
            action_item(L(30809, "Ukončit sledování pro všechny"), build_url(action="sw_leave"),
                        icon="DefaultIconError.png")
        else:
            action_item(L(30810, "Opustit skupinu"), build_url(action="sw_leave"), icon="DefaultIconError.png")
    else:
        action_item(L(30811, "Založit skupinu (budu vedoucí)"), build_url(action="sw_create"),
                    icon="DefaultAddSource.png")
        action_item(L(30812, "Připojit se kódem"), build_url(action="sw_join"), icon="DefaultNetwork.png")
    action_item(L(30813, "Jak SyncWatch funguje"), build_url(action="sw_intro"), icon="DefaultIconInfo.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def _sw_fail(err):
    xbmcgui.Dialog().ok(sw_title(), str(err) or L(30814, "Server SyncWatch neodpovídá, zkus to za chvíli."))


def sw_create():
    sw = _syncwatch()
    if sw_session().get("token"):
        return sw_window()
    code = sw.new_code()
    client = sw.Client(code)
    try:
        client.create({"name": sw_device_name(), "st": "ok"})
    except sw.SyncWatchError as e:
        return _sw_fail(e)
    sw_save({"code": code, "token": client.token, "mid": client.mid, "leader": True,
             "name": sw_device_name(), "since": int(time.time())})
    xbmc.log(f"[{ADDON_ID}] SyncWatch: založena skupina", xbmc.LOGINFO)
    sw_window()
    xbmc.executebuiltin("Container.Refresh")


def sw_join():
    sw = _syncwatch()
    if sw_session().get("token"):
        return sw_window()
    code = xbmcgui.Dialog().input(L(30815, "Kód skupiny (SW-XXXX-XXXX)"))
    if not code:
        return
    if not sw.valid_code(code):
        return _sw_fail(L(30816, "Kód nemá správný tvar. Má osm znaků, třeba SW-7K2Q-9MFX."))
    code = sw.pretty_code(code)
    client = sw.Client(code)
    try:
        client.join({"name": sw_device_name(), "st": "ok"})
    except sw.SyncWatchError as e:
        return _sw_fail(e)
    sw_save({"code": code, "token": client.token, "mid": client.mid, "leader": False,
             "name": sw_device_name(), "since": int(time.time())})
    xbmc.log(f"[{ADDON_ID}] SyncWatch: připojeno ke skupině", xbmc.LOGINFO)
    sw_window()
    xbmc.executebuiltin("Container.Refresh")


def sw_leave():
    sw = _syncwatch()
    session = sw_session()
    if not session.get("token"):
        return
    question = (L(30817, "Ukončit sledování? Skupina skončí i všem ostatním.") if session.get("leader")
                else L(30818, "Opustit skupinu? Ostatní budou sledovat dál."))
    if not xbmcgui.Dialog().yesno(sw_title(), question):
        return
    try:
        sw.Client(session["code"], token=session["token"]).leave()
    except sw.SyncWatchError as e:
        log_error(e)
    sw_save({})
    notify(L(30819, "SyncWatch ukončen"))
    xbmc.executebuiltin("Container.Refresh")


def sw_rejoin():
    """Služba (`SyncWatchManager`) pustí film skupiny znovu a naskočí na její pozici."""
    if sw_session().get("token"):
        xbmcgui.Window(10000).setProperty("nokturno.sw.rejoin", str(time.time()))


def sw_lock():
    sw = _syncwatch()
    session = sw_session()
    if not session.get("leader"):
        return
    locked = not sw_status().get("locked")
    try:
        sw.Client(session["code"], token=session["token"]).lock(locked)
    except sw.SyncWatchError as e:
        return _sw_fail(e)
    notify(L(30820, "Skupina je zavřená, nikdo další se nepřipojí") if locked
           else L(30821, "Skupina je otevřená pro další zařízení"))
    xbmc.executebuiltin("Container.Refresh")


class SyncWatchWindow(xbmcgui.WindowDialog):
    """Okno skupiny. U vedoucího kód velkým písmem a kdo se připojil, u člena
    „Čekám na vysílání…". Neblokuje: smyčka v `sw_window()` ho obnovuje ze stavu,
    který píše služba. Zpět okno zavře, ve skupině se zůstává."""
    BACK = (9, 10, 13, 92)

    def __init__(self, leader, code):
        super().__init__()
        self.closed_by_user = False
        self.addControl(xbmcgui.ControlImage(0, 0, 1280, 720, _sw_backdrop(), colorDiffuse="FF0D0B14"))
        self.addControl(xbmcgui.ControlLabel(90, 50, 1100, 50, "[B]%s[/B]" % sw_title(),
                                             font="font13", textColor="FFFFFFFF"))
        if leader:
            self.addControl(xbmcgui.ControlLabel(90, 120, 1100, 40, L(30822, "Kód skupiny"),
                                                 font="font13", textColor="FF9B95AD"))
            self.addControl(xbmcgui.ControlLabel(90, 160, 1100, 110, "[B]%s[/B]" % code,
                                                 font="font45", textColor="FFC4B5FD"))
            hint = L(30823, "Na dalším Kodi: Nokturno → SyncWatch → Připojit se kódem.")
        else:
            self.addControl(xbmcgui.ControlLabel(90, 140, 1100, 60, "[B]%s[/B]" % L(30824, "Čekám na vysílání…"),
                                                 font="font13", textColor="FFC4B5FD"))
            hint = _swf(30825, "Skupina %s — až vedoucí něco pustí, spustí se to i tady.", code)
        self.addControl(xbmcgui.ControlLabel(90, 280, 1100, 40, hint, font="font13", textColor="FFE6E1F0"))
        self.status = xbmcgui.ControlLabel(90, 330, 1100, 40, "", font="font13", textColor="FFFFB86C")
        self.addControl(self.status)
        self.members = xbmcgui.ControlTextBox(90, 390, 1100, 200, font="font13", textColor="FFE6E1F0")
        self.addControl(self.members)
        footer = (L(30826, "Zpět zavře okno. Pak pusť film v Nokturnu — spustí se všem.") if leader
                  else L(30827, "Zpět zavře okno, ve skupině zůstaneš. Odejít jde v menu SyncWatch."))
        self.addControl(xbmcgui.ControlLabel(90, 620, 1100, 40, footer, font="font13", textColor="FF9B95AD"))

    def update(self, status, leader):
        rows = []
        for m in status.get("members") or []:
            dot = "[COLOR FF50FA7B]●[/COLOR]" if m.get("online") else "[COLOR FF6D6A7C]○[/COLOR]"
            extra = []
            if m.get("leader"):
                extra.append(L(30804, "vedoucí"))
            if m.get("you"):
                extra.append(L(30828, "toto zařízení"))
            if not m.get("online"):
                extra.append(L(30829, "odpojené"))
            rows.append("%s %s%s" % (dot, m.get("name") or "?", (" (%s)" % ", ".join(extra)) if extra else ""))
        self.members.setText("[CR]".join(rows) or L(30830, "Připojuji…"))
        line = ""
        if status.get("expires") is not None and leader:
            left = max(0, int(status["expires_at"] - time.time())) if status.get("expires_at") else int(status["expires"])
            line = _swf(30831, "Zatím se nikdo nepřipojil — kód zanikne za %s", "%d:%02d" % divmod(left, 60))
        elif status.get("title") and status.get("loaded"):
            line = "%s: %s" % (L(30832, "Hraje se"), status["title"])
        self.status.setLabel(line)

    def onAction(self, action):
        if action.getId() in self.BACK:
            self.closed_by_user = True
            self.close()


def sw_window():
    """Okno skupiny, dokud ho uživatel nezavře, skupina neskončí, nebo (u člena)
    se nezačne přehrávat stream od vedoucího."""
    session = sw_session()
    if not session.get("token"):
        return
    leader = bool(session.get("leader"))
    window = SyncWatchWindow(leader, session.get("code", ""))
    window.show()
    try:
        while not window.closed_by_user and not should_stop():
            # jen volání API Kodi pustí `onAction` (Zpět) k oknu — viz `remote_setup()`
            MONITOR.waitForAbort(0.4)
            if not sw_session().get("token"):
                closed = sw_status().get("closed") or ""
                notify(_swf(30833, "SyncWatch skončil: %s", closed) if closed else L(30819, "SyncWatch ukončen"),
                       xbmcgui.NOTIFICATION_WARNING, 6000)
                break
            status = sw_status()
            window.update(status, leader)
            if not leader and (status.get("loading") or xbmc.Player().isPlayingVideo()):
                break   # stream od vedoucího se spouští — okno by překrylo video
    finally:
        window.close()
        del window


def sw_replay(**params):
    """Adresa, kterou ostatní ve skupině pustí totéž (`syncwatch.valid_replay`)."""
    return build_url(**params)


def remote_setup_schema(section=None):
    """Formulář pro mobil přímo ze `settings.xml` — nová položka nastavení se na stránce
    objeví sama. Popisky a nápověda jdou z `strings.po` v jazyce Kodi. `section` = jen jedna
    kategorie (tlačítko Nastavit z mobilu přímo v ní, např. Výběr streamu).

    `REMOTE_SETUP_SPLIT` kategorie (dnes jen „sources“, sloučené kvůli stropu 20 kategorií
    v settings.xml) rozpadne na jednu sekci stránky za skupinu, aby na mobilu zůstalo vidět
    deset rozbalovacích bloků zdrojů, ne jeden s 35 poli. Ostatní kategorie mají skupiny dál
    jako nadpisy uvnitř jedné sekce."""
    import xml.etree.ElementTree as ET
    root = ET.parse(os.path.join(ADDON_PATH, "resources", "settings.xml")).getroot()
    sections = []
    for category in root.iter("category"):
        cat_id = category.get("id")
        if cat_id not in REMOTE_SETUP_CATEGORIES or section and cat_id != section:
            continue
        groups = category.findall("group")
        if cat_id in REMOTE_SETUP_SPLIT:
            for group in groups:
                fields = _group_fields(group)
                if not fields:
                    continue
                label = (_plain(L(int(group.get("label")), group.get("id"))) if group.get("label")
                         else group.get("id"))
                sections.append({"id": group.get("id"), "label": label, "fields": fields,
                                 "open": not sections})
            continue
        fields = []
        for group in groups:
            group_fields = _group_fields(group)
            if not group_fields:
                continue
            if group.get("label") and len(groups) > 1:
                fallback = f"Úložiště {group.get('id')}"
                fields.append({"type": "heading", "label": _plain(L(int(group.get("label")), fallback))})
            fields.extend(group_fields)
        if fields:
            sections.append({"id": cat_id, "label": _plain(L(int(category.get("label")))),
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
        steps.setText(L(30452, "1. Připoj mobil ke stejné Wi-Fi jako toto zařízení.[CR]"
                               "2. Naskenuj QR kód fotoaparátem, nebo otevři v prohlížeči adresu:"))
        footer = L(30453, "Zpět zruší · adresa platí 30 minut a pro jedno uložení")
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
                            L(30454, "Toto zařízení nemá adresu v místní síti. Připoj ho k Wi-Fi nebo kabelem "
                                     "a zkus to znovu."))
        return None
    schema = remote_setup_schema(section)
    # neuložená položka: výchozí hodnota ze settings.xml — jinak by prohlížeč u výběru poslal
    # první volbu a u přepínače „vypnuto“ a uložilo by se, co uživatel neměnil
    values = {f["id"]: ADDON.getSetting(f["id"]) or f["default"] for section in schema for f in section["fields"]
              if f.get("type") not in ("heading", "info", "action")}
    texts = {
        "title": "Nokturno — " + L(30447, "Nastavit z mobilu"),
        "intro": L(30458, "Vyplň, co chceš změnit, a ulož. Nastavení se hned propíše do Kodi."),
        "save": L(30459, "Uložit do Kodi"),
        "saved": L(30460, "Uloženo. Nastavení je v Kodi, stránku můžeš zavřít."),
        "password_set": L(30461, "vyplněno — prázdné pole ponechá původní hodnotu"),
        "expired": L(30462, "Tato adresa už neplatí. Na TV spusť Nastavit z mobilu znovu."),
        "invalid": L(30463, "Neplatná hodnota: %s").replace("%s", "{}"),
        "order_rows": L(30499, "Horní řádek|Dolní řádek"),
        "order_hidden": L(30500, "Nezobrazovat"),
        "order_up": L(30510, "Nahoru"),
        "order_down": L(30511, "Dolů"),
        "action_failed": L(30580, "Spojení s televizí se přerušilo — na TV spusť Nastavit z mobilu znovu."),
    }
    server = _remote_setup().SetupServer(schema, values, texts, actions={"luna_find": luna_find_remote,
                                                        "luna_check": luna_check_remote,
                                                        "terms_show": terms_show_remote})
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
            f.write(_qr().to_png(_qr().encode(url), scale=12, border=2))
        with open(backdrop, "wb") as f:
            f.write(_qr().to_png([[False]], scale=1, border=0))
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


# Kam se uloží kopie nastavení před tím, než ho přepíše přenos z jiného zařízení.
TRANSFER_BACKUP = "settings-pred-prenosem.xml"
TRANSFER_EXT = ".nokturno"


def _close_settings():
    """Zavře otevřený dialog nastavení. Drží vlastní kopii hodnot, takže by při
    zavření přepsal, co mezitím zapsal přenos — a při čtení by vydal starý stav."""
    if not xbmc.getCondVisibility("Window.IsVisible(addonsettings)"):
        return
    xbmc.executebuiltin("Dialog.Close(addonsettings,true)")
    for _ in range(30):
        if not xbmc.getCondVisibility("Window.IsVisible(addonsettings)") or MONITOR.waitForAbort(0.1):
            break


def transfer_values(schema):
    """`{id: hodnota}` pro přenos. Neuložená položka jde s výchozí hodnotou ze
    `settings.xml` — stejně jako u stránky z mobilu."""
    return {f["id"]: ADDON.getSetting(f["id"]) or f["default"]
            for section in schema for f in section["fields"]
            if f.get("type") not in ("heading", "info", "action")}


def transfer_payload():
    """Obsah přenosu z aktuálního nastavení.

    CZtor a Trakt ve schématu formuláře nejsou (párují se na místě, ne vyplněním
    pole), takže se jejich stav přidá zvlášť — a jen jako příznak, nikdy token."""
    schema = remote_setup_schema()
    extras = {"cztor": ADDON.getSetting("cz_enabled") == "true", "trakt": bool(STORE.trakt())}
    return _transfer_core().pack(schema, transfer_values(schema),
                              source="Kodi %s" % ADDON.getAddonInfo("version"), extras=extras)


def transfer_send():
    """Odeslat nastavení do jiného Kodi: kód na obrazovce, obsah zašifrovaný.

    Dialog s kódem zůstane otevřený, dokud ho uživatel nezavře — má čas dojít
    k druhé televizi a kód opsat. Na server jde jen neprůhledná binárka."""
    _close_settings()
    try:
        code, ttl = _transfer_core().send_payload(transfer_payload())
    except _transfer_core().TransferError as e:
        xbmcgui.Dialog().ok(L(30587, "Přenos nastavení"), Lf(30595, e))
        return
    xbmc.log(f"[{ADDON_ID}] přenos nastavení odeslán, platí {ttl} s", xbmc.LOGINFO)
    xbmcgui.Dialog().ok(L(30587, "Přenos nastavení"), Lf(30593, code, max(1, ttl // 60)))


def transfer_receive(code=None):
    """Načíst nastavení z jiného Kodi podle opsaného kódu."""
    _close_settings()
    dialog = xbmcgui.Dialog()
    if not code:
        code = dialog.input(L(30594, "Kód z druhého Kodi"), defaultt="NKT-")
        if not code:
            return
    try:
        payload = _transfer_core().receive(code)
    except _transfer_core().TransferError as e:
        dialog.ok(L(30587, "Přenos nastavení"), Lf(30595, e))
        return
    transfer_apply(payload)


def transfer_file_save():
    """Týž přenos, jen místo serveru soubor — na USB nebo síťový disk. Kód je
    potřeba pořád: bez něj soubor nikdo nepřečte."""
    _close_settings()
    dialog = xbmcgui.Dialog()
    folder = dialog.browseSingle(3, L(30591, "Uložit do souboru"), "files")
    if not folder:
        return
    code = _transfer_core().new_code()
    name = "nokturno-%s%s" % (time.strftime("%Y-%m-%d"), TRANSFER_EXT)
    path = os.path.join(folder, name) if "://" not in folder else folder.rstrip("/") + "/" + name
    try:
        blob = _transfer_core().export_bytes(code, transfer_payload())
        handle = xbmcvfs.File(path, "w")
        try:
            if not handle.write(bytearray(blob)):
                raise OSError("zápis se nepovedl")
        finally:
            handle.close()
    except (_transfer_core().TransferError, OSError) as e:
        dialog.ok(L(30587, "Přenos nastavení"), Lf(30603, e))
        return
    dialog.ok(L(30587, "Přenos nastavení"), Lf(30599, path, code))


def transfer_file_load():
    _close_settings()
    dialog = xbmcgui.Dialog()
    path = dialog.browseSingle(1, L(30592, "Načíst ze souboru"), "files", TRANSFER_EXT)
    if not path or path.endswith("/"):
        return
    code = dialog.input(L(30594, "Kód z druhého Kodi"), defaultt="NKT-")
    if not code:
        return
    try:
        handle = xbmcvfs.File(path)
        try:
            blob = bytes(handle.readBytes())
        finally:
            handle.close()
    except OSError as e:
        dialog.ok(L(30587, "Přenos nastavení"), Lf(30604, e))
        return
    try:
        payload = _transfer_core().import_bytes(code, blob)
    except _transfer_core().TransferError as e:
        dialog.ok(L(30587, "Přenos nastavení"), Lf(30595, e))
        return
    transfer_apply(payload)


def transfer_backup_settings():
    """Kopie `settings.xml` vedle něj. Import je přepis — bez zálohy by se nešlo
    vrátit k tomu, co na zařízení bylo."""
    source = os.path.join(PROFILE, "settings.xml")
    target = os.path.join(PROFILE, TRANSFER_BACKUP)
    if not xbmcvfs.exists(source):
        return ""
    xbmcvfs.delete(target)
    return target if xbmcvfs.copy(source, target) else ""


def transfer_apply(payload):
    """Náhled, potvrzení, zápis. Uživatel vidí, co se změní, ještě než se to stane."""
    schema = remote_setup_schema()
    plan = _transfer_core().plan(payload, transfer_values(schema), _transfer_core().exportable(schema))
    dialog = xbmcgui.Dialog()
    if plan.empty():
        dialog.ok(L(30587, "Přenos nastavení"), L(30598, "Z přenosu nepřišla žádná změna."))
        return
    if not dialog.yesno(L(30587, "Přenos nastavení"),
                        Lf(30596, len(plan), plan.source or "Kodi", len(plan.same),
                           len(plan.unknown) + len(plan.blocked), TRANSFER_BACKUP)):
        return

    backup = transfer_backup_settings()
    for key, value in plan.changes.items():
        ADDON.setSetting(key, value)
    if any(k.startswith("ws_") for k in plan.changes):
        xbmcgui.Window(10000).clearProperty("nokturno.ws_token")   # nový účet = nový login
    STORE.clear_cache()     # cache patří k účtům, které tu byly do teď
    xbmc.log(f"[{ADDON_ID}] přenos nastavení: zapsáno {len(plan)} položek "
             f"({', '.join(sorted(plan.changes))}), záloha {backup or 'žádná'}", xbmc.LOGINFO)
    notify(Lf(30597, len(plan)))

    # Tokeny se nepřenášejí schválně: obnovovací token CZtoru se použitím mění, takže by
    # kopie odhlásila původní zařízení, a token Traktu je vázaný na zařízení. Přenos nese
    # jen příznak „tady to bylo zapnuté“ — přihlásit se musí každé zařízení samo.
    if "cztor" in plan.flags and not cztor_client().paired() \
            and dialog.yesno(L(30560, "CZtor"), L(30600, "Účet CZtoru se nepřenáší. Spárovat teď?")):
        cztor_pair()
    if "trakt" in plan.flags and not STORE.trakt() \
            and dialog.yesno(L(30090, "Trakt"), L(30601, "Účet Traktu se nepřenáší. Přihlásit teď?")):
        trakt_auth()


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

    if dialog.yesno(L(30560, "CZtor"), L(30573, "Máš předplatné CZtor (cztor.com)?[CR]"
                                                "Zařízení se spáruje PINem, heslo není potřeba.")):
        ADDON.setSetting("cz_enabled", "true")
        cztor_pair()

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
    """Průvodce prvním nastavením — na čisté instalaci se ukáže jako první položka
    kořenového menu (`main_menu()`), ať uživatel nemusí sám hledat, co a kde
    v nastavení vyplnit. Samo od sebe (bez kliknutí) se nespouští — modální dialog
    v cestě, kterou otevírají widgety a JSON-RPC, by blokoval i vypínání Kodi
    (audit 2026-09-14, viz `CLAUDE.md`). Jde přeskočit, nebo si ho kdykoli znovu
    pustit ručně z Nastavení → Pokročilé (`force=True`, běží bez ohledu na to,
    že už proběhl).
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
    if not terms_accepted():
        # dnes už negde jinam projít — router (`main()`) žádnou akci mimo `TERMS_FREE`
        # nepustí bez `ensure_terms()`, takže se sem dostane jen s odsouhlaseným textem.
        # Vlastní krok tu zůstává jako pojistka, kdyby se to spuštění průvodce jednou
        # obešlo (přání uživatele 2026-09-22: souhlas patří i do samotného průvodce).
        dialog.textviewer(L(30728, "Legal notice"), terms_text())
        if not dialog.yesno(L(30728, "Legal notice"),
                            L(30749, "Do you agree to the terms of use above?"),
                            yeslabel=L(30748, "I agree"), nolabel=L(30732, "I don't agree")):
            return
        ADDON.setSetting("terms_ok", "true")
        if not terms_accepted():   # zápis se nepovedl (souběžná změna nastavení) — nepokračovat naslepo
            return
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
              L(30358, "Hotovo. Vše se dá kdykoli změnit v Nastavení doplňku.[CR]"
                       "Bez zadaného zdroje budou katalog a hledání fungovat i tak, jen anglicky."))
    STORE.save("wizard_done", True)


def test_sources():
    """Tlačítko v nastavení: během pár vteřin řekne, který zdroj nefunguje a proč.

    Dřív se to poznalo až z prázdného seznamu streamů. Volá se mimo cache, aby
    zelená nebyla jen ozvěna včerejší odpovědi.
    """
    luna, sosac, ws, hs, st = get_luna(), get_sosac(), get_webshare(), get_hellspy(), get_sledujteto()
    fs = get_fastshare()
    pt = get_prehrajto()
    cz = get_cztor()
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

    def check_prehrajto():
        # bez účtu zdroj funguje taky, jen s méně výsledky — ověří se tedy hledáním,
        # a s účtem navíc Premium (bez něj se hraje jen překódovaný soubor)
        found, _total = pt.search("matrix", limit=5)
        if not (pt.email and pt.password):
            return L(30721, "bez účtu, nalezeno %s") % len(found)
        user = pt.me()
        if not user.get("premium"):
            return L(30722, "bez Premium — méně výsledků a jen 1080p")
        return L(30723, "Premium, zbývá %s dní") % user.get("days", 0)

    def check_cztor():
        # párování se ověří dotazem na účet; bez aktivního předplatného se katalog neotevře
        account = cz.profile()
        if not account.get("active"):
            raise CztorError(L(30572, "Předplatné CZtor není aktivní."))
        return f"{account.get('plan')} → {account.get('valid_until')}"

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
        "Přehraj.to": check_prehrajto if pt else None,
        "CZtor": check_cztor if cz else None,
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
    refresh_accounts_after_test()
    xbmcgui.Dialog().ok(L(30170), "\n".join(lines))


ACCOUNTS_RETRY_AGE = 120   # s; „neodpovídá" mladší než tohle se ještě nezkouší znovu


def request_accounts_retry(engine, rows):
    """Otevřené menu = Kodi v popředí = síť je. Když uložený stav říká „neodpovídá",
    nebo poslední obnova skončila úplně bez sítě, požádat službu o obnovu hned.

    Na mobilu (Samsung, Android 16) Android utne Kodi na pozadí síť: obnova po
    startu (`ACCOUNTS_DELAY`) i další po 20 minutách běží přesně ve chvíli, kdy
    uživatel telefon odložil, a všechny skončí `unreachable` — ověřeno z logu
    2026-09-21 (DNS `Errno 7` pokaždé 3–4 min po startu služby, v popředí ani
    jednou). Jediná chvíle, kdy se dá spolehnout na síť, je tahle.
    """
    try:
        opakovat = any(r.get("code") == "unreachable"
                       and (r.get("age") is None or r["age"] > ACCOUNTS_RETRY_AGE) for r in rows)
        if not opakovat and engine.offline_recently():
            stari = [r["age"] for r in rows if r.get("age") is not None]
            opakovat = not stari or min(stari) > ACCOUNTS_RETRY_AGE
        if opakovat:
            xbmcgui.Window(10000).setProperty(ACCOUNTS_TRIGGER_PROP, "1")
    except Exception as e:  # noqa: BLE001 – menu nesmí spadnout kvůli stavu zdrojů
        log_error(e)


def refresh_accounts_after_test():
    """Po „Ověřit zdroje" požádat službu, ať přepíše i uložený stav pro položku
    Stav zdrojů v menu.

    Jsou to dvě různé cesty: test se ptá zdrojů živě, kdežto menu čte záznam
    z `accounts.json`, který platí dvanáct hodin. Když obnova na pozadí padla
    na vypnutou síť (na mobilu hned po startu Kodi), ukazovalo menu půl dne
    chyby, zatímco test hned vedle hlásil všechno v pořádku.

    Obnova **nesmí běžet tady**: nedostupný zdroj se v ní odbaví až timeoutem
    (Luna 20 s, úložiště taky), takže plugin po testu půl minuty jen točil
    kolečkem — nahlásil uživatel na `6.6.0~beta10`. Stejný vzor jako u
    přepočtu jazykových katalogů: plugin zapíše vlastnost okna, službu to
    probudí do několika sekund a čeká se na pozadí."""
    try:
        xbmcgui.Window(10000).setProperty(ACCOUNTS_TRIGGER_PROP, "1")
    except Exception as e:  # noqa: BLE001 – výsledek testu se musí ukázat i tak
        log_error(e)


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
    "bad_token": (30527, "Luna %s běží, ale tento token nepřijala.[CR][CR]Vygeneruj si adresu doplňku "
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


def luna_diag_text(result, base=""):
    """Věta a rada k výsledku `luna_diagnose` (Kodi značky `[CR]` zůstávají)."""
    sid, fallback = LUNA_DIAG_TEXTS.get(result["code"], LUNA_DIAG_TEXTS["unreachable"])
    hodnoty = {"version": result.get("version") or "?", "base": result.get("base") or base}
    try:
        return L(sid, fallback) % tuple(hodnoty[k] for k in LUNA_DIAG_ARGS.get(result["code"], ()))
    except TypeError:   # překlad se zástupnými symboly nesouhlasí — radši holý text než pád
        return L(sid, fallback)


# --- stav zdrojů ------------------------------------------------------------
#
# „Prostě mi to nejde" má několik různých příčin, které od sebe uživatel u televize
# nerozezná: vypršené předplatné, účet bez VIP, pauza HellSpy po 429, Luna, která
# neběží. Do teď to šlo poznat jen z prázdného seznamu streamů, nebo tlačítkem
# „Ověřit zdroje" schovaným v nastavení. Teď je stav první položkou menu — ale jen
# když je co hlásit, ať se menu nezaplevelí.
#
# Data přicházejí z `accounts.py` v jádru jako kód příčiny a čísla; věta se skládá
# až tady, protože HA jich potřebuje jinou podobu. Stav se **čte z uloženého
# záznamu**, po síti ho obnovuje služba na pozadí (`AccountsChecker` v service.py) —
# otevření menu se tím nesmí zdržet ani o milisekundu.

# Stav slovem v barvě zadané hexem. Emoji a ✔/✘ kreslí fonty skinů jako prázdný
# proužek a pojmenované barvy (`green`) závisí na skinu — viz `5.2.30~beta2`.
ACCOUNT_COLORS = {ACC_FAIL: "FFFF6B6B", ACC_WARN: "FFFFC14E", ACC_OK: "FF6FD18A", ACC_OFF: "FF9A9A9A"}

# Jméno zdroje tak, jak ho uživatel zná ze seznamu streamů (obarvené stejně).
ACCOUNT_TAGS = {"luna": LUNA_TAG, "sosac": SOSAC_TAG, "webshare": WS_TAG, "cztor": CZ_TAG,
                "fastshare": FS_TAG, "sledujteto": ST_TAG, "prehrajto": PT_TAG, "hellspy": HS_TAG,
                "storage": DAV_TAG}

# (zdroj, kód) → (id řetězce, český fallback). `%s` se dosazuje z `detail`, viz
# `ACCOUNT_ARGS`. Luna má vlastní sadu už od 5.2.30 (`LUNA_DIAG_SHORT`), tady se
# jen použije. Kódy `off`/`unknown` se nehlásí vůbec — zdroj je vypnutý, nebo se
# ho doplněk ještě nestihl zeptat.
ACCOUNT_TEXTS = {
    ("webshare", "vip"): (30631, "předplatné do %s"),
    ("webshare", "expires_soon"): (30632, "předplatné končí za %s dní"),
    ("webshare", "expired"): (30633, "předplatné vypršelo"),
    ("webshare", "free"): (30634, "účet bez VIP — stahování pár kB/s"),
    ("fastshare", "unlimited"): (30635, "neomezené stahování"),
    ("fastshare", "credit"): (30636, "zbývá %s GB kreditu"),
    ("fastshare", "no_credit"): (30637, "došel kredit"),
    ("cztor", "ok"): (30638, "%s do %s"),
    ("cztor", "expires_soon"): (30639, "předplatné končí za %s dní"),
    ("cztor", "expired"): (30640, "předplatné vypršelo"),
    ("cztor", "not_paired"): (30641, "zařízení není spárované"),
    ("cztor", "unknown"): (30642, "stav účtu neznámý"),
    ("sledujteto", "premium"): (30643, "Premium"),
    ("sledujteto", "no_premium"): (30644, "účet bez Premium — přehrávání nepůjde"),
    ("prehrajto", "premium"): (30715, "Premium, zbývá %s dní"),
    ("prehrajto", "expires_soon"): (30716, "předplatné končí za %s dní"),
    ("prehrajto", "no_premium"): (30717, "účet bez Premium — méně výsledků a jen 1080p"),
    ("prehrajto", "anonymous"): (30718, "bez účtu — jen první strana výsledků"),
    ("prehrajto", "paused"): (30719, "pozastaveno na %s min (HTTP 429)"),
    # Sosáč se na nic neptá, řádek je tu kvůli uspání zdroje — text sdílí s HellSpy
    ("sosac", "ok"): (30645, "v pořádku"),
    ("hellspy", "ok"): (30645, "v pořádku"),
    # 429 od HellSpy = blokace sítě uživatele, ne limit dotazů (viz `source_errors`)
    ("hellspy", "paused"): (30646, "odmítá tuto síť (HTTP 429) — VPN nebo mobilní data? Zkusí se za %s min"),
    ("storage", "ok"): (30647, "odpovídá"),
}

# Totéž zkrácené pro štítek položky v menu. Skin má na řádek zhruba čtyřicet znaků
# a delší text si roluje pod rukama — na Office bylo z „Stav zdrojů: Luna: server
# neodpovídá · WebShare: předplatné vypršelo · +2" vidět jen prostředek věty a
# uživatel nepoznal, co čte. Ve výpisu pod tím má každý zdroj vlastní řádek, takže
# tam zůstává plný text z `ACCOUNT_TEXTS`.
ACCOUNT_SHORT = {
    ("webshare", "expires_soon"): (30651, "končí za %s dní"),
    ("webshare", "free"): (30652, "účet bez VIP"),
    ("fastshare", "credit"): (30653, "zbývá %s GB"),
    ("cztor", "expires_soon"): (30651, "končí za %s dní"),
    ("cztor", "not_paired"): (30654, "není spárováno"),
    ("sledujteto", "no_premium"): (30655, "účet bez Premium"),
    ("prehrajto", "expires_soon"): (30651, "končí za %s dní"),
    ("prehrajto", "no_premium"): (30720, "účet bez Premium"),
    ("prehrajto", "paused"): (30656, "pauza %s min"),
    ("hellspy", "paused"): (30725, "odmítá síť"),
}

ACCOUNT_COMMON_SHORT = {
    "bad_login": (30657, "nesedí přihlášení"),
    "error": (30658, "stav neznámý"),
}

# co platí pro každý zdroj stejně (selhání kontroly, viz `engine._account_fail_code`)
ACCOUNT_COMMON = {
    "bad_login": (30648, "nesedí jméno nebo heslo"),
    "unreachable": (30649, "neodpovídá"),
    "error": (30650, "nepodařilo se zjistit stav"),
}

# co se v které hlášce dosazuje za %s (pořadí podle textu)
ACCOUNT_ARGS = {
    ("webshare", "vip"): ("until",),
    ("webshare", "expires_soon"): ("days",),
    ("fastshare", "credit"): ("gb",),
    ("cztor", "ok"): ("plan", "until"),
    ("cztor", "expires_soon"): ("days",),
    ("hellspy", "paused"): ("minutes",),
    ("prehrajto", "premium"): ("days",),
    ("prehrajto", "expires_soon"): ("days",),
    ("prehrajto", "paused"): ("minutes",),
}


def account_text(row, short=False):
    """Stav jednoho zdroje jednou větou, bez jeho jména — to dodává volající.
    `short` vrátí variantu do štítku menu, kde je místo jen na pár slov."""
    source, code = row["source"], row["code"]
    if source == "luna":
        sid, fallback = LUNA_DIAG_SHORT.get(code, (30168, "v pořádku") if code == "ok" else
                                            LUNA_DIAG_SHORT["unreachable"])
    elif short:
        sid, fallback = (ACCOUNT_SHORT.get((source, code)) or ACCOUNT_COMMON_SHORT.get(code)
                         or ACCOUNT_TEXTS.get((source, code)) or ACCOUNT_COMMON.get(code)
                         or ACCOUNT_COMMON_SHORT["error"])
    else:
        sid, fallback = ACCOUNT_TEXTS.get((source, code)) or ACCOUNT_COMMON.get(code) or ACCOUNT_COMMON["error"]
    detail = row.get("detail") or {}
    args = tuple(str(detail.get(k, "?")) for k in ACCOUNT_ARGS.get((source, code), ()))
    try:
        return L(sid, fallback) % args if args else L(sid, fallback)
    except TypeError:   # překlad se zástupnými symboly nesouhlasí — radši holý text než pád
        return L(sid, fallback)


def account_line(row, color=True, short=False):
    """„WebShare: předplatné končí za 3 dny" — jméno zdroje a stav, stav v barvě."""
    tag = ACCOUNT_TAGS.get(row["source"]) or row["source"]
    text = account_text(row, short=short)
    if color:
        text = f"[COLOR {ACCOUNT_COLORS.get(row['level'], ACCOUNT_COLORS[ACC_OK])}]{text}[/COLOR]"
    return f"{tag}: {text}"


# kam vede klik na řádek — rovnou tam, kde se to opravuje
ACCOUNT_ACTIONS = {
    "luna": {"": "luna_check"},
    "cztor": {"not_paired": "cztor_pair", "": "cztor_status"},
    "webshare": {"expires_soon": "sub_status", "expired": "sub_status", "": "settings"},
}


def account_action(row):
    podle_zdroje = ACCOUNT_ACTIONS.get(row["source"]) or {}
    return podle_zdroje.get(row["code"]) or podle_zdroje.get("") or "settings"


# Kolik zdrojů se vejde do štítku v menu. Jeden, a ještě zkráceně: skin má na řádek
# zhruba čtyřicet znaků a delší text si sám roluje, takže z něj uživatel vidí
# v každém okamžiku jen výsek. Ověřeno na Office se dvěma zdroji a „+2" — ze štítku
# bylo vidět „server neodpovídá · WebShare: předplatné" bez začátku i konce.
# Zbytek patří do výpisu pod položkou, kde má každý zdroj vlastní řádek.
SUMMARY_LIMIT = 1


def account_summary(rows, limit=SUMMARY_LIMIT):
    """Štítek položky v menu: „WebShare: končí za 3 dní · +2"."""
    bad = accounts_problems(rows)
    if not bad:
        return ""
    texty = [account_line(r, short=True) for r in bad[:limit]]
    if len(bad) > limit:
        texty.append(f"[COLOR {GREY}]+{len(bad) - limit}[/COLOR]")
    return " · ".join(texty)


def accounts_refresh(apis):
    """Obnova stavu účtů po síti — volá ji služba na pozadí přes JSON-RPC, stejnou
    cestou jako zahřívání katalogů (služba vlastní jádro nemá).

    Jeden dotaz na zdroj; HellSpy se neptá vůbec, jen si přečte svou pauzu po 429 —
    právě opakovanými dotazy si doplněk blokaci dvakrát přivodil (6.0.2, 6.0.4).
    """
    try:
        warn_days = int(setting("sub_warn_days") or 5)
    except ValueError:
        warn_days = 5
    try:
        engine_of(apis).refresh_accounts(warn_days=warn_days)
    except Exception as e:  # noqa: BLE001 – obnova na pozadí nesmí nikdy nic shodit
        log_error(f"obnova stavu zdrojů: {e}")
    # jako u `prefetch()`: prázdný, ale úspěšně zavřený adresář, ať služba nedělá
    # v každém kole řádek `error <general>: GetDirectory` v kodi.log
    xbmcplugin.endOfDirectory(HANDLE, succeeded=True, cacheToDisc=False)


def list_accounts(apis):
    """Výpis stavu všech nastavených zdrojů — žádný modál, ten by z widgetu nebo
    JSON-RPC neměl kdo zavřít a zablokoval by i vypínání Kodi."""
    engine = engine_of(apis)
    rows = engine.accounts()
    # nadpis obrazovky — 30630 se uvolnilo ze štítku položky v menu (viz main_menu)
    xbmcplugin.setPluginCategory(HANDLE, L(30630, "Stav zdrojů"))
    paused = accounts_paused(STORE)
    for row in rows:
        if row["level"] == ACC_OFF and row["code"] == "off":
            continue    # zdroj je vypnutý schválně, není co hlásit
        label = account_line(row)
        zbyva = paused.get(row["source"])
        if zbyva:
            label += f" [COLOR {GREY}]({int(zbyva // 60) + 1}m)[/COLOR]"
        ctx = [(L(30742, "Uspat zdroj"), runplugin(action="source_pause", source=row["source"]))]
        action_item(label, build_url(action=account_action(row)),
                    icon="DefaultAddonService.png", context=ctx)
    # ne-složka jako Nastavení pod ní: jako složka by Kodi po kliknutí čekal na
    # výpis adresáře, který `test_sources` nikdy nezavře — po OK v dialogu se
    # točilo kolečko navěky (nahlášeno z mobilu na `6.6.0~beta11`)
    action_item(L(30167, "Ověřit zdroje"), build_url(action="test_sources"), icon="DefaultAddonProgram.png")
    action_item(L(30392, "Nastavení"), build_url(action="settings"), icon="DefaultAddonProgram.png")
    # bez cache na disk — stav se mění, zpět do menu by jinak ukázalo starý výpis
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def luna_find_remote(values):
    """Najít Lunu ze stránky „Nastavit z mobilu“: běží ve vlákně serveru, jen vrátí adresu
    stránce — do nastavení se uloží až tlačítkem Uložit."""
    found = luna_discover()
    if not found:
        return {"level": "fail", "text": _stranka(L(30531, "V této síti se Luna nenašla."))}
    return {"level": "ok", "text": _stranka(L(30576, "Luna nalezena: %s") % ", ".join(f["url"] for f in found)),
            "set": {"luna_url": found[0]["url"]}, "link": _luna_setup_link(found[0]["url"])}


def luna_check_remote(values):
    """Ověřit Lunu ze stránky „Nastavit z mobilu“ s tím, co je právě ve formuláři."""
    base = values.get("luna_url", "")
    # token je teď skryté pole (heslo se na stránku nikdy neposílá, viz remote_setup.py) —
    # prázdné odeslání neznamená „smaž token“, ale „nikdo ho nepřepsal“
    token = values.get("token") or setting("token")
    try:
        result = luna_diagnose(base, token)
    except Exception as e:  # noqa: BLE001 – ať akce nikdy nespadne bez vysvětlení
        result = {"level": "fail", "code": "unreachable", "base": base, "token": "", "version": "",
                  "detail": str(e)}
    out = {"level": result["level"], "text": _stranka(luna_diag_text(result, base)), "set": {}}
    if result.get("base") and result["base"] != base:
        out["set"]["luna_url"] = result["base"]
    if result.get("token") and result["token"] != token:   # celá adresa ze /setup se rozdělí
        out["set"]["token"] = result["token"]
    if result["code"] in ("no_token", "bad_token", "bad_token_format", "main_empty", "no_streams"):
        out["link"] = _luna_setup_link(result.get("base") or base)   # token se bere právě tam
    return out


def _luna_setup_link(base):
    """Odkaz na stránku /setup Luny — tam se vezme adresa doplňku s tokenem."""
    return {"url": base.rstrip("/") + "/setup", "label": L(30581, "Otevřít nastavení Luny (/setup)")}


def _stranka(text):
    """Text z `strings.po` pro webovou stránku: `[CR]` na nový řádek, ostatní Kodi značky pryč."""
    return _plain(text.replace("[CR]", "\n"))


def terms_show_remote(values):
    """Tlačítko „Přečíst podmínky“ ve Stavu zdrojů z mobilu — stejný text jako `info_terms()`
    na TV, jen na stránce místo v modálu."""
    return {"level": "ok", "text": _stranka(terms_text())}


def os_check():
    """Tlačítko „Vyzkoušet OpenSubtitles“ v nastavení.

    Odpovídá na tři různé otázky, které z hlášky „nefunguje mi to“ nejdou rozeznat:
    jestli je zdroj vůbec zapnutý, jestli doplněk dostal ze serveru klíč, a jestli
    se (u vyplněného účtu) podařilo přihlásit. Hledá se zkušební titul, ne stahuje —
    stažení by ukrojilo z denní kvóty právě toho, kdo si jen ověřuje nastavení.
    """
    from opensubtitles_api import OpenSubtitlesError   # líný import, viz reuse invoker
    if not on("os_enabled"):
        xbmcgui.Dialog().ok(L(30611, "OpenSubtitles"), L(30622, "OpenSubtitles jsou v nastavení vypnuté."))
        return
    api = get_opensubtitles()
    if api is None:
        xbmcgui.Dialog().ok(L(30611, "OpenSubtitles"),
                    L(30623, "Klíč se nepodařilo získat ze serveru — zkus to později."))
        return
    radky = []
    try:
        ucet = api.ucet()
    except OpenSubtitlesError as err:
        xbmc.log(f"[{ADDON_ID}] OpenSubtitles účet: {err}", xbmc.LOGINFO)
        xbmcgui.Dialog().ok(L(30611, "OpenSubtitles"),
                    L(30624, "Přihlášení se nepovedlo — zkontroluj jméno a heslo."))
        return
    if ucet:
        radky.append(L(30627, "Přihlášen jako %s") % ucet["user"])
        radky.append(L(30626, "Zbývá dnes stažení: %s") % ucet["zbyva"])
    else:
        radky.append(L(30628, "Bez přihlášení — 5 stažení denně pro tuto IP adresu."))
    nalez = api.hledej(OS_PROBE_ID, ("CZ", "SK"))
    radky.insert(0, L(30621, "OpenSubtitles funguje.") if nalez
                 else L(30623, "Klíč se nepodařilo získat ze serveru — zkus to později."))
    radky.append(L(30625, "Titulků ke zkušebnímu titulu: %s") % len(nalez))
    xbmcgui.Dialog().ok(L(30611, "OpenSubtitles"), "\n".join(radky))


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
                            L(30531, "V této síti se Luna nenašla.[CR][CR]Běží na některém počítači "
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
    text = luna_diag_text(result, setting("luna_url"))
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
        L(30534, "Ověření Luny"), f"{mark}{text}[CR][CR]" + L(30557, "Ověřuje se uložené nastavení — hodnota "
                                                                    "rozepsaná v políčku se započítá až po OK. "
                                                                    "Jinou adresu lze zadat rovnou zde."),
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
        notify(L(30222, "Stáhlo se příliš málo dat, zkus to znovu"), xbmcgui.NOTIFICATION_WARNING, 5000)
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


# Zahřívání streamů dalšího dílu: jen seriály sledované za posledních PREFETCH_DAYS dní,
# nejvýš PREFETCH_SERIES z nich (každý = hledání napříč všemi zdroji + čtení hlaviček).
# Do 6.1.4 se braly poslední 15 zhlédnuté položky bez ohledu na stáří — u instalace,
# která seriály dva měsíce neotevřela, se tak po každém startu Kodi hledaly díly
# dávno opuštěných seriálů.
PREFETCH_DAYS = 14
PREFETCH_SERIES = 5
PREFETCH_SCAN = 30


def prefetch(apis, kind):
    """Zahřátí cache — volá služba na pozadí, nic se nevypisuje ani nepočítá.

    Katalogy zahřívá služba přes běžný výpis (Files.GetDirectory), protože ten
    projde i doplněním popisů; tohle je jen pro streamy dalších dílů, kde by
    běžná cesta (`pick_title`) započítala zobrazení do statistik.
    """
    if kind == "next":
        seen = set()
        fresh_since = time.time() - PREFETCH_DAYS * 86400
        for key, entry in STORE.recently_watched(PREFETCH_SCAN):
            if should_stop():
                break   # Kodi končí — každý titul je vlastní hledání napříč zdroji, tady se nic necachuje napůl
            if (entry.get("ts") or 0) < fresh_since or len(seen) >= PREFETCH_SERIES:
                break   # seřazené od nejnovějšího: starší zhlédnutí už nikoho nezajímá
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
    # Služba sem chodí přes Files.GetDirectory (JSON-RPC), a `succeeded=False` Kodi hlásí
    # jako `GetDirectory - Error getting plugin://…?action=prefetch` — v každém kole
    # zahřívání jeden řádek `error` v kodi.log, který uživatelé posílají na dashboard.
    # Prázdný, ale úspěšně zavřený adresář je totéž bez té hlášky (nic se nekreslí,
    # `cacheToDisc=False` drží Kodi od zapamatování prázdného výpisu).
    xbmcplugin.endOfDirectory(HANDLE, succeeded=True, cacheToDisc=False)


def stats_sources():
    """Které zdroje jsou v nastavení aktivní — do statistik, bez účtů a adres.
    Výčet je v `kodi_sources`, protože ho stejně potřebuje i služba."""
    return kodi_sources.stats_sources(ADDON.getSetting)


def stats_send():
    """Ruční odeslání statistik z nastavení – jinak je posílá služba na pozadí."""
    from stats import COLLECT_URL, Stats
    ok, why = Stats(PROFILE).send(COLLECT_URL, version=ADDON.getAddonInfo("version"),
                                  sources=stats_sources(), product="kodi")
    notify(L(30165) if ok else f"{L(30166)}: {why}",
           xbmcgui.NOTIFICATION_INFO if ok else xbmcgui.NOTIFICATION_ERROR, 5000)


LOG_TAIL_BYTES = 500 * 1024   # celý xbmc.log bývá desítky MB, server bere jen ~500 KB


def scrub_log(raw):
    """Konec `kodi.log` bez tajemství — každý řádek projde `crash.scrub()` (tokeny, hesla,
    e-maily, IP, jména v cestách, celé adresy). Kodi samo do logu píše i adresy streamů
    s podpisem účtu a hlášky jiných doplňků s klíči API; hlášení o pádu se čistí stejně,
    ruční log do 6.1.4 odcházel syrový."""
    from crash import scrub
    text = raw.decode("utf-8", errors="replace")
    return "\n".join(scrub(line) for line in text.split("\n")).encode("utf-8")


def log_send(ask=True):
    """Ruční odeslání Kodi logu z nastavení – poslední ~500 KB `kodi.log`, gzip, bez tajemství
    (`scrub_log`).

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

    body = gzip.compress(scrub_log(raw))
    instalace = install_id()
    version = ADDON.getAddonInfo("version")
    logs_url = COLLECT_URL.rsplit("/", 1)[0] + "/logs"
    req = urllib.request.Request(
        f"{logs_url}?id={instalace}&version={urllib.parse.quote(version)}",
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
    if HANDLE >= 0:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
    xbmcgui.Dialog().textviewer(L(30191, "Novinky"), body)
    xbmc.executebuiltin("Container.Refresh")


# --- obrazovky --------------------------------------------------------------------

def main_menu(apis):
    mark_used()
    if not any(apis.values()):
        # bez modálního dialogu: ten by při volání z widgetu/JSON-RPC čekal na OK a zablokoval i vypínání Kodi
        notify(L(30104), xbmcgui.NOTIFICATION_WARNING, 6000)
        # bez zdroje je průvodce jediné, co dává smysl — dřív tu byla jen položka Nastavení
        # a kdo průvodce jednou přeskočil, neměl jak zjistit, že existuje
        action_item(L(30359, "Průvodce nastavením"), build_url(action="setup_wizard"),
                    icon="DefaultAddonProgram.png")
        action_item(L(30107), build_url(action="settings"), icon="DefaultAddonProgram.png")
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    # Stav zdrojů úplně nahoře, ale jen když je co hlásit — jinak by tu u všech
    # bez problému jen zabíral místo. Čte se z uloženého záznamu (obnovu dělá
    # služba na pozadí), takže menu nezdrží. Souhrn je dlouhý, proto jde i do
    # popisku položky, kde ho skin ukáže celý.
    engine = engine_of(apis)
    rows = engine.accounts()
    request_accounts_retry(engine, rows)
    souhrn = account_summary(rows)
    if souhrn:
        # bez prefixu „Stav zdrojů: “ (13 znaků) — Arctic Fuse má na řádek zhruba
        # čtyřicet znaků a delší text si roluje pod rukama, takže z věty je vidět
        # vždycky jen výsek; popis složky ten skin nekreslí vůbec, takže záloha
        # neexistuje. Význam „něco je špatně“ nese ikona. Stejná bitva jako v 6.3.7.
        li = xbmcgui.ListItem(label=souhrn)
        li.setArt({"icon": "DefaultIconWarning.png"})
        tag = li.getVideoInfoTag()
        # v popisu všechno a na vlastních řádcích — do štítku se vejdou jen dva zdroje
        tag.setPlot("\n".join(account_line(r) for r in accounts_problems(rows)))
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="accounts"), li, isFolder=True)
    # čistá instalace: průvodce nahoře jako položka. Spouštět ho z kořene sám od sebe
    # nejde — modální dialog v cestě, kterou otevírají widgety a JSON-RPC, blokuje
    # i vypínání Kodi (viz pravidlo v CLAUDE.md)
    if not accounts_set():   # i po přeskočení: bez účtu nemá doplněk co ukázat
        action_item(L(30359, "Průvodce nastavením"), build_url(action="setup_wizard"),
                    icon="DefaultAddonProgram.png")
    fresh = unseen_changelog()
    if fresh:
        action_item(f"{L(30192, 'Novinky ve verzi')} {fresh[0][0]}",
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
    # koncerty jsou jen ve fulltextových zdrojích (WebShare, HellSpy, FastShare) — bez nich
    # by server neměl co vrátit, tak položka bez nich ani není
    if concerts_available(apis):
        folder_item(L(30750, "Koncerty"), build_url(action="concerts"), icon="DefaultMusicVideos.png")
    # jako Pokračovat výš: na čisté instalaci nevede do prázdna. Podmínka musí pokrýt
    # obojí, co je uvnitř — Můj seznam i Naposledy zhlédnuté (to je schované až tam).
    # První přidaný titul řádek rozsvítí hned, `toggle_fav()` volá Container.Refresh.
    if STORE.favourites() or STORE.recently_watched(1):
        folder_item(L(30060), build_url(action="favourites"), icon="DefaultFavourites.png")
    # společné sledování; ve skupině ukazuje i kód, ať je vidět, že běží
    sw_code = sw_session().get("code")
    action_item("%s · %s" % (sw_title(), sw_code) if sw_code else sw_title(),
                build_url(action="syncwatch"), icon="DefaultNetwork.png")
    if apis.get("dav"):
        folder_item(L(30387, "Moje úložiště"), build_url(action="dav_browse"), icon="DefaultHardDisk.png")
    if setting("download_dir") or sync_targets():
        folder_item(L(30391, "Stažené"), build_url(action="downloads"), icon="DefaultHardDisk.png")
    action_item(L(30392, "Nastavení"), build_url(action="settings"), icon="DefaultAddonProgram.png")
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
    # sezónní a tematické katalogy z dashboardu nahoře, pořadí mezi nimi řídí `position`
    dash_catalog_items(apis, "browse", kind)

    def pick(tmdb_cid, luna_cid, cinemeta_cid):
        if tmdb and tmdb_cid:
            return "tmdb", tmdb_cid, None
        if luna and luna_cid:
            return "luna", luna_cid, None
        if cinemeta and cinemeta_cid:
            return "cinemeta", cinemeta_cid, None
        return None

    # „Pro tebe" jen když je z čeho doporučovat — prázdný seznam by jen mátl. Seznam
    # vzorů se čte z profilu (`watched`), žádná síť, takže kreslení menu to nezdrží.
    if foryou_seeds(apis, ctype, limit=1):
        folder_item(L(30605, "Pro Tebe"), build_url(action="foryou", type=ctype),
                    icon="DefaultAddonsRecentlyUpdated.png")
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
    # „Náhodný film/seriál" je ne-složka: klik ji Kodi spustí jako skript s handle −1
    # a `random_title()` rovnou otevře dialog výběru streamu — ve výpisu se tedy žádný
    # modál neotevře a widget ani JSON-RPC se sem nedostanou (vzor `tv_pick`).
    nahodny = xbmcgui.ListItem(label=L(30609, "Náhodný seriál") if ctype == "series"
                               else L(30608, "Náhodný film"))
    nahodny.setArt({"icon": "DefaultAddonsUpdates.png"})
    xbmcplugin.addDirectoryItem(HANDLE, build_url(action="random", type=ctype), nahodny, isFolder=False)
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
    note_lang_catalog_open(ctype)
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
        folder_item(L(30437, "Data aren't ready — checking dubbing/subtitles across the enabled sources can take "
                              "a few minutes. Tap to start."),
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
    note_lang_catalog_open(ctype)
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
    přes `Engine.classify_langs()` napříč VŠEMI zdroji, které má uživatel zapnuté
    (jeden zdroj tvrdí anglicky s anglickými titulky neznamená, že jiný zdroj nemá
    dabing). Sosáč/Luna mají jazyk přímo v popisku (levné), WebShare/HellSpy/
    Sledujteto/FastShare ho jádro odhadne z názvu (taky levné, jen text) —
    `probe_audio=False` ale vynechá poslední, nejdražší krok, čtení hlaviček
    souboru přes síť (`_fill_audio`), který by u desítek titulů byl neúnosně
    pomalý (odhad z popisku/názvu stačí na klasifikaci ano/ne, nemusí být ověřený
    jako ve skutečném dialogu streamů). Kolo zdrojů navíc skončí hned, jak některý
    nabídne CZ/SK dabing (přednost před titulky) — Přehraj.to bez účtu se do
    téhle hromadné klasifikace vůbec nezapojuje (viz `ostatni()` v jádru) a
    zařazení kandidáta se drží 24 h, takže zahřívání po 6 h většinou vůbec
    nesáhne na síť.

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
    # Měření, kde „Nově přidané s CZ dabingem/titulky" ztrácí čas (2026-09-15).
    # LOGDEBUG, ne LOGWARNING: do 60 kandidátů = desítky řádků na každé zahřátí
    # (každých 6 h), a logy uživatelů z dashboardu pak vypadají jako porucha —
    # stejná třída šumu jako 6.2.2 a 6.2.7. Kdo měří, zapne ladicí log Kodi.
    xbmc.log(f"[{ADDON_ID}/DIAG] {msg}", xbmc.LOGDEBUG)


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
        # NE `fresh=warming()` bez podmínky: u ostatních katalogů (levný dotaz) dává
        # smysl při zahřívání vždy přepsat, tady je přepočet o řády dražší (desítky
        # sekund až minuty) — bezpodmínečné `fresh=True` by nutilo počítat znovu,
        # i když cache má sotva pár minut (2026-09-15: nahlásil uživatel — druhé
        # otevření nešlo z cache).
        # Zahřívání ale musí přepočítat, když cache do příštího kola nevydrží: běží
        # po `LANG_WARM_EVERY` (6 h) a cache platí `LANG_CATALOG_TTL` (8 h), takže
        # bez tohohle kolo v 6. hodině jen přečetlo platnou cache a nic nepřepsalo —
        # mezi 8. a 12. hodinou pak menu hlásilo „Data nejsou připravená" (nález 29).
        stara = STORE.peek_cached(key, LANG_REFRESH_AFTER) is None
        return STORE.cached_if(key, LANG_CATALOG_TTL, lambda: _build_lang_catalog(apis, ctype),
                               fresh=warming() and stara)
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
                                                    "the enabled sources can take a few minutes…"))
        bar.update(0)
    matched = {"dub": [], "subs": []}
    try:
        # po dávkách LANG_CATALOG_WORKERS kandidátů souběžně: jeden kandidát stojí 2–6 s
        # (dotazy na všechny zdroje) a 60 kandidátů za sebou dělalo přes 170 s. Výsledky se
        # čtou v původním pořadí, takže seznam vypadá stejně jako při sériovém průchodu.
        # Souběžnost je záměrně malá — HellSpy po 429 blokuje celý proces na 10 minut.
        cands = candidates[:LANG_CATALOG_CAP]

        def _probe(cand):
            t_item = time.time()
            try:
                return raw_streams_for(cand), time.time() - t_item, None
            except Exception as err:  # noqa: BLE001 – výpadek u jednoho kandidáta nesmí shodit celý seznam
                return None, time.time() - t_item, err

        def raw_streams_for(cand):
            # `classify_langs()` v jádru: kolo zdrojů skončí hned, jak některý nabídne
            # CZ/SK dabing (dabing má přednost), a zařazení se drží 24 h — zahřívání
            # po 6 h tak většinou vůbec nesáhne na síť.
            return engine.classify_langs(cand.get("type") or "movie", cand["id"])

        with ThreadPoolExecutor(max_workers=LANG_CATALOG_WORKERS) as pool:
            for start in range(0, len(cands), LANG_CATALOG_WORKERS):
                if len(matched["dub"]) >= LANG_CATALOG_TARGET and len(matched["subs"]) >= LANG_CATALOG_TARGET:
                    break
                if should_stop():
                    # Kodi končí (zahřívání ze služby běží v tomhle skriptu a Kodi na něj
                    # čeká) — výjimka, ne `break`: nedokončený seznam se nesmí zapsat do
                    # 8h cache v `STORE.cached_if()`, a zámek uklidí `finally` výš
                    _diag(f"{ctype}: přerušeno po {start} kandidátech, Kodi končí")
                    raise Aborted()
                batch = cands[start:start + LANG_CATALOG_WORKERS]
                for j, (cand, (res, took, err)) in enumerate(zip(batch, pool.map(_probe, batch))):
                    i = start + j
                    if err is not None:
                        _diag(f"  [{i}] {cand.get('id')}: chyba za {took:.1f} s ({err})")
                        continue
                    _diag(f"  [{i}] {cand.get('id')}: {res['n']} streamů, {res['k'] or '–'} za {took:.1f} s")
                    if res["k"] == "dub" and len(matched["dub"]) < LANG_CATALOG_TARGET:
                        matched["dub"].append(cand)
                    elif res["k"] == "subs" and len(matched["subs"]) < LANG_CATALOG_TARGET:
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
        # ne-složka: vymazání není výpis, Kodi ji spustí s handle −1 (viz action_item)
        action_item(L(30041), build_url(action="history_clear", type=kind), icon="DefaultVideoDeleted.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def search_new(apis, kind):
    query = xbmcgui.Dialog().input(search_title(kind), type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        # zrušený dialog = Kodi zůstane v menu hledání; `succeeded=True` by ho navedlo
        # do prázdné složky. Řádek `GetDirectory - Error getting …` v logu je cena za to.
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
        # blokující dialog, ne jen toast — ať si uživatel opravdu všimne, že nějaký zdroj
        # neodpověděl, a ví přesně který; výsledky ze zbylých zdrojů se zobrazí hned po OK
        # (endOfDirectory běží až za tímhle). Jen po kliku ve výpisu Nokturna: výsledky
        # hledání jdou i do widgetu / přes JSON-RPC (HA) a tam by modál čekal na OK,
        # které nikdo nedá, a blokoval i vypínání Kodi (pravidlo v CLAUDE.md)
        if browsing_nokturno():
            xbmcgui.Dialog().ok(L(30360, "Zdroj neodpověděl"), describe_errors(errors))
        else:
            notify(skipped_notice(errors), xbmcgui.NOTIFICATION_WARNING, 7000)
            li = xbmcgui.ListItem(label=f"[COLOR {WARN_COLOR}]{L(30360, 'Zdroj neodpověděl')}[/COLOR]: "
                                  + describe_errors(errors).replace("\n", " · "))
            li.setArt({"icon": "DefaultIconError.png"})
            xbmcplugin.addDirectoryItem(HANDLE, build_url(action="settings"), li, isFolder=False)
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
    # adresář nezavírá — položka je ne-složka (handle −1) jako u `history_remove`;
    # ze staré oblíbené položky s handle ≥ 0 ho zavře `_tlacitko` v routeru
    for k in (("any", "movie", "series") if kind == "any" else (kind,)):
        STORE.clear_history(k)
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
    if sync_targets():
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
        # zhlédnuté z jiného zařízení (synchronizace) přijdou bez snímku — bez
        # dohledání by je výpis tiše vynechal a „Naposledy" na druhém Kodi zůstalo prázdné
        if not snap or thin_snapshot(snap):
            apis = apis or get_apis()
            snap = recover_snapshot(apis, key) or snap
        if snap:
            add_snapshot_item(key, snap)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def aired_videos(videos, today):
    """Díly, které už vyšly — bez speciálů, setříděné podle sezóny a čísla.

    Datum vydání (`released`, u TMDB `air_date`) se do 6.3.3 nečetlo vůbec: po dílu
    číslo 1 se prostě vzal první s vyšším číslem, i kdyby vycházel za tři měsíce.
    Vadily tím obě cesty, kterými se další díl používá:

    - `prefetch(kind="next")` na něj každé kolo zahřívání pustí `collect_streams()`,
      tedy hledání napříč všemi zdroji. Kolo běží po `WARM_EVERY` (2,5 h) a po každém
      dokoukání dílu, nejvýš pro `PREFETCH_SERIES` seriálů — u seriálu, který běží
      týdně, to je až několik desítek marných hledání denně. Ukládá se jen nález
      (`cached_if`), takže se prázdný výsledek nikdy neodloží a opakuje se pořád dokola.
      Právě opakovanými dotazy na zdroje si doplněk už dvakrát řekl o blokaci od
      HellSpy (6.0.2 a 6.0.4).
    - Řádek „Další díl" v Pokračovat ve sledování (`list_continue`, synchronizuje se
      i do karty v Home Assistantu) nabízel díl, který ještě nešlo nikde sehnat.

    Seriály, kterým TMDB data nedává vůbec, se řídí dál starým pravidlem: nemá-li
    datum ani jeden díl, projde celý seznam. Jinak by u nich další díl přestal
    fungovat úplně.

    Díl bez data je něco jiného než díl, který ve zdrojích chybí — chybějící díl se
    hledat má, nevydaný ne.
    """
    known = [v for v in videos if int(v.get("season") or 0) > 0]
    if any(v.get("released") for v in known):
        known = [v for v in known if v.get("released") and str(v["released"])[:10] <= today]
    return sorted(known, key=lambda v: (int(v.get("season") or 0), int(v.get("episode") or 0)))


def next_episode(apis, snap):
    """Další už vydaná epizoda po zhlédnuté (podle meta seriálu), nebo None."""
    series_id = snap.get("series")
    if not series_id or snap.get("season") is None:
        return None
    try:
        meta = api_for(apis, series_id).meta("series", series_id)
    except Errors:
        return None
    videos = aired_videos(meta.get("videos") or [], time.strftime("%Y-%m-%d"))
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


def foryou_seeds(apis, ctype, limit=foryou.SEEDS):
    """Tituly, ze kterých se doporučuje — naposledy zhlédnuté daného typu."""
    return foryou.seed_ids(STORE.recently_watched(), ctype, limit=limit)


def foryou_seed_names(seeds):
    """`{id vzoru: název}` ze snímků titulů v profilu (`items.json`) — žádný dotaz
    na síť. Co snímek nemá, do „Protože jsi viděl …" prostě nepůjde."""
    hledane, out = set(seeds), {}
    for key, _entry in STORE.recently_watched():
        base, season, _episode = split_episode_id(key)
        if base not in hledane or base in out:
            continue
        snap = STORE.item(key) or {}
        name = strip_year(snap.get("tvshow") if season is not None else snap.get("title"), snap.get("year"))
        if name:
            out[base] = name
    return out


def foryou_build(apis, ctype, seeds):
    """Doporučení ke vzorům: s vlastním klíčem přímo z TMDB, bez něj (nebo při jeho
    chybě) z dashboardu, který se na TMDB zeptá za doplněk — stejná dvojice jako
    u „Podobné tituly" (`list_similar`)."""
    tmdb, dash = apis.get("tmdb"), apis.get("dash")

    def similar(item_id):
        if should_stop():
            # pět vzorů = až patnáct dotazů na TMDB za sebou; při vypínání Kodi
            # se smyčka nesmí dotáhnout do konce (CLAUDE.md, „Přerušení při vypnutí")
            raise Aborted()
        if tmdb is not None:
            try:
                return tmdb.similar(ctype, item_id, limit=foryou.PER_SEED)
            except TmdbError as e:
                log_error(f"pro tebe {item_id}: {e}")
        if dash is not None:
            return dash.similar(ctype, item_id)
        return []

    skip = foryou.known_ids(STORE.recently_watched(limit=WATCHED_MAX), STORE.in_progress())
    return foryou.recommend(seeds, similar, skip=skip)


def foryou_items(apis, ctype, seeds):
    """Seznam „Pro tebe" z cache; přepočítá se, jen když je stará nebo se změnily vzory.

    Klíč je jeden na typ a seznam vzorů se ukládá dovnitř — po zhlédnutí nového titulu
    se doporučení přepočítají hned (jinak by týden stála na místě), ale při výpadku TMDB
    i dashboardu je pořád z čeho ukázat poslední známý stav místo prázdna
    (`FORYOU_STALE_TTL`). Značka výpadku `FORYOU_DOWN_KEY` drží doplněk od toho, aby
    při každém otevření znovu čekal na timeout — přesně jako `nokturno:trend:down`
    v `trend_api.py` (6.2.2, nález 30)."""
    key = f"nokturno:foryou:{ctype}"
    rec = STORE.peek_cached(key, FORYOU_TTL) or {}
    stara = STORE.peek_cached(key, FORYOU_REFRESH_AFTER) is None
    if rec.get("items") and rec.get("seeds") == seeds and not (warming() and stara):
        return rec["items"]
    zaloha = (STORE.peek_cached(key, FORYOU_STALE_TTL) or {}).get("items") or []
    if STORE.peek_cached(FORYOU_DOWN_KEY, FORYOU_DOWN_TTL) is not None:
        return zaloha
    items = foryou_build(apis, ctype, seeds)
    if not items:
        # buď zdroj neodpověděl, nebo TMDB k těmhle vzorům nic nezná — tak jako tak
        # se to nemá zkoušet znovu při každém otevření seznamu
        STORE.cached_if(FORYOU_DOWN_KEY, FORYOU_DOWN_TTL, lambda: {"t": int(time.time())}, fresh=True)
        return zaloha
    STORE.cached_if(key, FORYOU_TTL, lambda: {"seeds": seeds, "items": items}, fresh=True)
    return items


def list_foryou(apis, ctype):
    """„Pro tebe" — doporučení k tomu, co uživatel dokoukal naposledy.

    Kreslení menu tohle nezdrží: položka v `browse_menu()` je obyčejná složka a
    počítá se až tady, po vstupu do ní."""
    note_foryou_open(ctype)
    set_content("tvshows" if ctype == "series" else "movies")
    seeds = foryou_seeds(apis, ctype)
    items = foryou_items(apis, ctype, seeds) if seeds else []
    names = foryou_seed_names(seeds) if items else {}
    for m in items:
        because = names.get(m.get("_because") or "")
        if because:
            popis = Lf(30606, because)
            m = dict(m, description=f"{popis}\n\n{m.get('description') or ''}".strip())
        add_meta_item(m, ctype)
    if not items:
        # notifikace, ne modál — sem se dá dostat i z widgetu a z JSON-RPC
        notify(L(30607, "Zatím není z čeho doporučovat — seznam vzniká z naposledy zhlédnutých titulů."),
               xbmcgui.NOTIFICATION_INFO, 4000)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def random_source(apis, ctype):
    """Odkud losovat: TMDB s vlastním klíčem, jinak Luna, jinak Cinemeta — stejné
    pořadí jako v `browse_menu()`."""
    kind = "series" if ctype == "series" else "movie"
    if apis.get("tmdb") is not None:
        return "tmdb", "popular"
    if apis.get("luna") is not None:
        return "luna", f"tmdb.top_{kind}"
    if apis.get("cinemeta") is not None:
        return "cinemeta", "top"
    return None, None


def random_candidates(apis, ctype):
    """Kandidáti na náhodný titul a žánr, ve kterém se losovalo (prázdný = losovalo se ze všeho).

    Žánr je vážený tím, co uživatel viděl (`foryou.pick_genre`) — kdo kouká hlavně
    komedie, dostane spíš komedii, ale ne pokaždé. Bere se jen ze žánrů, které zdroj
    sám nabízí: snímky mají žánry tak, jak je vrátil zdroj (TMDB česky, Luna
    a Cinemeta anglicky), a cizí slovo by katalog nenašel.

    Stránka se losuje taky (`FORYOU_PAGES`), jinak by se pořád vybíralo z týchž
    dvaceti nejpopulárnějších."""
    src, cid = random_source(apis, ctype)
    if not src:
        return [], ""
    api = apis[src]
    nabidka = set(next((c.get("genres") or [] for c in api.catalogs(ctype) if c["id"] == cid), []))
    snaps = [STORE.item(key) for key, _entry in STORE.recently_watched()]
    counts = {g: n for g, n in foryou.genre_counts(snaps).items() if g in nabidka}
    genre = foryou.pick_genre(counts)
    skip = PAGE * random.randrange(FORYOU_PAGES)
    metas = api.catalog(ctype, cid, genre=genre, skip=skip) or []
    if not metas and (genre or skip):
        genre, metas = "", (api.catalog(ctype, cid) or [])
    videne = foryou.known_ids(STORE.recently_watched(limit=WATCHED_MAX))
    volba = [m for m in metas if m.get("id") not in videne] or metas
    return volba, (genre or "")


def has_pref_lang(streams, pref):
    """Má některý stream preferovaný jazyk ve zvuku, nebo titulky v něm (či v náhradním
    jazyce, CZ ↔ SK — `SUBTITLE_FALLBACK`)? Jazyk odhadnutý z názvu souboru se počítá."""
    subs_ok = set(SUBTITLE_FALLBACK.get(pref, (pref,)))
    for s in streams:
        if pref in (s.get("langs") or []) or subs_ok & set(s.get("subs") or []):
            return True
    return False


def random_by_language(apis, ctype, candidates, pref):
    """První z náhodně vybraných kandidátů, který má `pref` ve zvuku nebo titulcích.

    Kandidáti se ověřují souběžně stejným lehkým hledáním jako jazykové katalogy
    (`raw_streams(probe_audio=False)`, bez čtení hlaviček) a výsledek zůstává v cache
    streamů, takže dialog vybraného titulu naběhne hned. Celé ověřování má strop
    `RANDOM_BUDGET_S`; nikdo nevyhověl (nebo nestihl) = `(None, False)`."""
    engine = engine_of(apis)
    cands = random.sample(candidates, min(RANDOM_CANDIDATES, len(candidates)))

    t0 = time.time()

    def check(cand):
        try:
            streams = engine.raw_streams(cand.get("type") or ctype, cand["id"], strict=True, probe_audio=False)
            ok = has_pref_lang(streams, pref)
            xbmc.log(f"[{ADDON_ID}] náhodný titul: {cand['id']} {'má' if ok else 'nemá'} {pref} "
                     f"({len(streams)} streamů, {time.time() - t0:.1f} s)", xbmc.LOGINFO)
            return ok
        except Exception as e:  # noqa: BLE001 – výpadek u jednoho kandidáta nesmí shodit losování
            log_error(f"náhodný titul, {cand.get('id')}: {e}")
            return False

    from concurrent.futures import FIRST_COMPLETED, wait as wait_futures
    pool = ThreadPoolExecutor(max_workers=RANDOM_WORKERS)
    futures = {pool.submit(check, c): c for c in cands}
    pending = set(futures)
    deadline = time.time() + RANDOM_BUDGET_S
    # neblokující ukazatel, ne modál — sem se dá dostat i z widgetu a z JSON-RPC
    bar = xbmcgui.DialogProgressBG()
    bar.create(L(30000, "Nokturno"), L(30660, "Looking for a title in your preferred language…"))
    try:
        while pending:
            if should_stop():
                raise Aborted()
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            done, pending = wait_futures(pending, timeout=min(remaining, 0.5), return_when=FIRST_COMPLETED)
            for f in done:
                if f.result():
                    return futures[f], True
        xbmc.log(f"[{ADDON_ID}] náhodný titul: žádný z {len(cands)} kandidátů nevyhověl", xbmc.LOGINFO)
    finally:
        bar.close()
        for f in pending:
            f.cancel()
        pool.shutdown(wait=False)
    return None, False


def random_pick(apis, ctype):
    """Náhodný titul (s preferovaným jazykem, když se ho podaří najít) a žánr; viz `random_choose`."""
    meta, genre, _ok = random_choose(apis, ctype)
    return meta, genre


def random_choose(apis, ctype):
    """`(titul, žánr, má_jazyk)`. Bez preferovaného jazyka (nastavení „Žádný") se neověřuje
    a `má_jazyk` je True; jinak se hledá titul s `pref_lang` a když žádný nevyhoví, vezme se
    prostě první vylosovaný a `má_jazyk` je False."""
    volba, genre = random_candidates(apis, ctype)
    if not volba:
        return None, genre, True
    pref = PREF_LANGS[int(setting("pref_lang", "0"))]
    if pref:
        meta, ok = random_by_language(apis, ctype, volba, pref)
        if ok:
            return meta, genre, True
        return random.choice(volba), genre, False
    return random.choice(volba), genre, True


def random_title(apis, ctype):
    """„Náhodný film" / „Náhodný seriál": vylosuje titul a otevře nad ním výběr streamu.

    Položka je ve výpisu ne-složka, takže klik ji Kodi spustí jako skript s handle −1
    (dialog), kdežto Přehrát ze skinu ji rozklíčovává s handle ≥ 0 (`setResolvedUrl`) —
    stejné rozdělení jako u titulu v režimu seznamu (5.2.7~beta20). Výpis se tu nikdy
    nekreslí, takže widget ani JSON-RPC žádný dialog neotevřou."""
    try:
        meta, genre, has_lang = random_choose(apis, ctype)
    except Errors as e:
        log_error(f"náhodný titul: {e}")
        meta, genre, has_lang = None, "", True
    if meta is None:
        notify(L(30610, "Není z čeho losovat"), xbmcgui.NOTIFICATION_WARNING, 4000)
        if HANDLE >= 0:
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    jmeno = display_name(meta)
    if genre:
        jmeno = f"{jmeno} · {genre}"
    if not has_lang:
        jmeno += " · " + L(30659, "no preferred language")
    notify(jmeno, xbmcgui.NOTIFICATION_INFO, 4000)
    if HANDLE < 0:
        pick_title(apis, ctype, meta["id"])
    else:
        play(apis, ctype, meta["id"], ask="1")


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
    if day == data.get("today"):   # dnes: přepínač skončených pořadů úplně první
        state = L(30585, "ukázané") if on("tv_show_ended", "false") else L(30586, "skryté")
        picks = (("ended", f"{L(30584, 'Skončené pořady')}: {state}", "DefaultInProgressShows.png"),) + picks
    for field, label, icon in picks:
        li = xbmcgui.ListItem(label=f"[B]{label}[/B]")
        li.setArt({"icon": icon})
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="tv_pick", field=field, date=day or None,
                                                      kind=kind or None, channel=channel or None),
                                    li, isFolder=False)
    now = time.time()
    shown = 0
    for it in data["items"]:
        if day == data.get("today") and it["stop"] < now and not on("tv_show_ended", "false"):
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
        li = xbmcgui.ListItem(label=f"[COLOR {GREY}]{L(30494, 'V tomto výběru nejsou žádné pořady')}[/COLOR]")
        xbmcplugin.addDirectoryItem(HANDLE, tv_url(day, kind, channel), li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def tv_pick(apis, field, day="", kind="", channel=""):
    """Klik na volbu nad TV programem (handle −1, jen z výpisu Nokturna) → dialog → přepnout výpis."""
    data = apis["dash"].tv_program(day or None, kind or None, channel or None) or {}
    if field == "ended":
        ADDON.setSetting("tv_show_ended", "false" if on("tv_show_ended", "false") else "true")
    elif field == "day":
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

    def collect(strict, meta=meta):
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
        # nic nenašel ani uvolněný filtr: nabídnout hledání pod jiným názvem (fulltext zdrojů hledá
        # v názvech souborů, ty mívají jiný název než TMDB — u seriálu doplní SxxEyy engine sám)
        if has_fulltext_source and xbmcgui.Dialog().yesno(
                L(30577, "Žádný stream nenalezen"),
                L(30578, "Zkusit hledat pod jiným názvem?[CR]Zadaný název se hledá fulltextem ve zdrojích "
                         "(u dílu seriálu se přidá číslo série a dílu).")):
            typed = xbmcgui.Dialog().input(L(30579, "Název pro hledání"), defaultt=str(
                meta.get("_title") or meta.get("name") or "")).strip()
            if typed:
                streams, errors = collect(False, dict(meta, _title=typed, name=typed))
        if not streams:
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


def play(apis, ctype, item_id, series_id=None, url=None, alt=None, subs="", pref="", ask="", alts="", sw=""):
    """`ask=1` = Přehrát nad položkou Nokturna (detail, widget) nebo player TMDb Helperu → výběr
    v dialogu (`choose_stream`, zapamatovaný stream předvybraný). Bez `ask` (Up Next, HA) hraje
    zapamatovaný nebo nejlepší stream bez ptaní."""
    meta, video = load_meta(apis, ctype, item_id, series_id)
    # u seriálu si pamatujeme, jaký stream si uživatel vybral — další díl (Up Next,
    # Pokračovat, widget) pak jede stejně bez ptaní; klíč je seriál, ne díl
    pref_key = (series_id or split_episode_id(item_id)[0]) if video else None
    resolved_path = None
    streams = []
    if url:
        try:
            # sloučené verze (`alts`) jsou tentýž film jinde — zkusí se, než se hledá znovu
            # u SyncWatch jen tentýž soubor, `alts` se ignorují
            url, resolved_path = resolve_first(apis, [url] + [a for a in (alts or "").split("|") if a and not sw])
        except Errors as e:
            if sw:
                # SyncWatch: jen přesně tentýž stream jako vedoucí (stejná kvalita, zvuk i délka),
                # jiný soubor by se rozjel — žádné hledání ani náhrada z jiného zdroje
                xbmc.log(f"[{ADDON_ID}] SyncWatch: stream vedoucího nejde přehrát: {e}", xbmc.LOGWARNING)
                notify(Lf(30834, str(e) or L(30102)), xbmcgui.NOTIFICATION_ERROR, 8000)
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
                return
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
        dead = engine_of(apis).last_timings.get("nedostupné")
        if errors:
            text = skipped_notice(errors)
            if dead:
                text = f"{text} · {Lf(30741, dead)}"
            notify(text, xbmcgui.NOTIFICATION_WARNING, 7000)
        elif dead:
            notify(Lf(30741, dead), xbmcgui.NOTIFICATION_WARNING, 7000)
        if not streams:
            if not errors:
                notify(L(30102))
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            # modál tu nesmí (přehrávací kontext), tak ho otevře samostatný skript až po neúspěšném
            # přehrání: dialog „hledat pod jiným názvem“ / uvolněné hledání (`pick_title`).
            # Jen po kliku ve výpisu Nokturna — přehrání z widgetu Pokračovat, z karty HA nebo
            # z TMDb Helperu by jinak vyhodilo dialog, na který nikdo neodpoví (a zamklo vypínání)
            if browsing_nokturno():
                xbmc.executebuiltin("RunPlugin(%s)" % build_url(action="title", type=ctype, id=item_id,
                                                                series=series_id, alt=alt, fulltext="1"))
            return
        remembered = preferred_stream(streams, STORE.stream_pref(pref_key)) if pref_key else None
        chosen = remembered or streams[0]
        if ask and not sw:
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
        # nejen zvolený stream a jeho sloučené kopie, ale i další nálezy — viz PLAY_FALLBACKS
        poradi = play_candidates(chosen, streams)
        used, resolved_path = resolve_first(apis, [u for u, _ in poradi])
        if used != chosen["url"]:
            chosen = next(s for u, s in poradi if u == used)
    li = xbmcgui.ListItem(label=title, path=resolved_path)
    li.setArt(art_for(meta, video))
    fill_info(li, meta, "series" if video else ctype, video=video)
    # bez tohohle Kodi u přímého přehrání (HA karta, widget, Up Next) nevědělo o rozkoukanosti
    # a vždycky pustilo od začátku — resume point se jinak nastavuje jen v seznamech (`apply_watched`)
    tag = li.getVideoInfoTag()
    count = STORE.playcount(item_id)
    if count:
        tag.setPlaycount(count)
    # SyncWatch: pozici určuje skupina, ne historie tohohle zařízení
    resume, total = STORE.resume(item_id) if not sw else (0, 0)
    if resume and not count:
        try:
            tag.setResumePoint(resume, total)
        except Exception:  # noqa: BLE001 – Kodi < 20
            pass
    subtitles = local_subtitles(apis, subtitle_refs(apis, chosen, resolved_path, video, ctype))
    if subtitles:
        li.setSubtitles(subtitles)
    STORE.remember_item(item_id, snapshot(meta, ctype, video, series_id, alt))
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    # bez roku – ten se posílá zvlášť polem `year`, display_name() by ho zdvojil
    stats_title = episode_stats_title(video, meta)
    # SyncWatch: ostatním jen přesně tentýž stream — stejná kvalita, zvuk i délka; jiná verze
    # (ani sloučená kopie) by se rozjela, proto žádné `alts`
    replay = sw_replay(action="play", type=ctype, id=item_id, series=series_id, alt=alt, url=chosen.get("url"),
                       subs="|".join(chosen.get("subtitles") or []))
    mark_playing(item_id, stats_title, year if year.isdigit() else None, "series" if video else ctype,
                stream_url=chosen.get("url"), stream_subs="|".join(chosen.get("subtitles") or []),
                stream_langs=list(chosen.get("langs") or []), replay=replay)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)
    if video and not sw:
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
    mark_playing("ws:" + ident, name, kind="ws", replay=sw_replay(action="play_ws", ident=ident, name=name))
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def play_dav(apis, slot, path, name=""):
    key = f"dav:{slot}:{path}"
    replay = sw_replay(action="play_dav", slot=slot, path=path, name=name)
    api, path = storage_for(apis, key)
    li = xbmcgui.ListItem(label=name or path, path=api.kodi_url(path))
    li.getVideoInfoTag().setTitle(name or path)
    STORE.remember_item(key, {"type": "dav", "id": key, "title": name or path, "art": {}})
    mark_playing(key, name, kind="dav", replay=replay)
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
    mark_playing(key, name, kind="hs", replay=sw_replay(action="play_hs", id=file_id, hash=file_hash, name=name))
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


def strip_inner_ext(name):
    """Popisek „Díl [soubor.mkv]“ (CZtor a spol. mají název souboru v hranaté závorce) —
    přípona uvnitř závorky pryč, jinak cílový soubor dostal `….mkv].mkv`."""
    low = name.lower()
    for ext in VIDEO_EXTS:
        if low.endswith(ext + "]"):
            return name[:-len(ext) - 1] + "]"
    return name


def guess_ext(url, name):
    for ext in VIDEO_EXTS:
        if name.lower().endswith(ext):
            return ""
        if url.lower().split("?")[0].endswith(ext):
            return ext
    return ".mkv"


def enqueue_download(url, name, key, dest_name=None, link=None):
    """Do fronty jde VNITŘNÍ odkaz (`ws:`, `hs:`, `st:`, `fs:`, `cz:`, `pt:`, `streamuj:`, `dav:`) — služba ho
    rozklíčuje až ve chvíli stahování (`service.resolve_internal`). Hotový odkaz WebShare
    vyprší za pár hodin: třetí soubor ve frontě nebo cokoli po restartu Kodi dřív končilo
    chybou. `link` je volitelný už rozklíčovaný odkaz jen kvůli odhadu přípony."""
    d = download_dir()
    if not d:
        return
    base = safe_filename(strip_inner_ext(dest_name or name))
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
    # jen režim Home Assistant — soubory stahuje a podepsané odkazy vydává HA,
    # slepý relay žádné nemá (a „adresa“ v jeho nastavení není server, jen značka)
    if sync_settings() and not sync_via_relay():
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
            xbmcvfs.delete(d["dest"])     # xbmcvfs umí i síťovou složku (smb://)
        else:
            xbmcvfs.delete(d["dest"] + ".part")   # rozdělané už nikdo nedostahuje
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


def cztor_pair():
    """Spáruje zařízení s účtem CZtor: PIN se potvrdí na webu, doplněk se jen ptá."""
    api = cztor_client()
    try:
        pin = api.start_pin()
    except CztorError as e:
        log_error(e)
        xbmcgui.Dialog().ok(L(30560, "CZtor"), f"{L(30569, 'Spárování s CZtor se nepovedlo.')}[CR]{e}")
        return
    dialog = xbmcgui.DialogProgress()
    text = L(30567, "Na telefonu nebo počítači otevři[CR][B]%s[/B][CR]přihlas se a zadej PIN [B]%s[/B]")
    dialog.create(L(30560, "CZtor"), text % (pin["url"], pin["pin"]))
    total = max(30, pin["expires"] - time.time())
    paired, error = False, None
    while time.time() < pin["expires"] and not dialog.iscanceled() and not MONITOR.abortRequested():
        dialog.update(int(100 - (pin["expires"] - time.time()) / total * 100))
        if MONITOR.waitForAbort(pin["interval"]) or dialog.iscanceled():
            break
        try:
            paired = api.poll_pin(pin["poll_token"])
        except CztorError as e:
            log_error(e)
            error = e
            break
        if paired:
            break
    canceled = dialog.iscanceled()
    dialog.close()
    if not paired:
        if error or not canceled:
            notify(L(30569, "Spárování s CZtor se nepovedlo."), xbmcgui.NOTIFICATION_ERROR)
        return
    ADDON.setSetting("cz_enabled", "true")
    STORE.clear_cache()   # seznamy streamů bez CZtor jinak drží 72 h
    cztor_status()


def cztor_status():
    api = cztor_client()
    if not api.paired():
        xbmcgui.Dialog().ok(L(30560, "CZtor"), L(30571, "CZtor není spárovaný — použij Spárovat PINem."))
        return
    try:
        account = api.profile()
    except CztorError as e:
        log_error(e)
        xbmcgui.Dialog().ok(L(30560, "CZtor"), str(e))
        return
    text = L(30568, "CZtor: %s, předplatné %s do %s") % (
        account.get("name") or "?", account.get("plan") or "?", account.get("valid_until") or "?")
    if not account.get("active"):
        text += "[CR]" + L(30572, "Předplatné CZtor není aktivní.")
    xbmcgui.Dialog().ok(L(30560, "CZtor"), text)


def cztor_logout():
    cztor_client().logout()
    notify(L(30570, "CZtor odhlášen."))


def trakt_logout():
    STORE.set_trakt({})
    notify(L(30098))


# volby dialogu „Uspat zdroj": (sekundy, id řetězce, český fallback)
SOURCE_PAUSE_CHOICES = list(zip(PAUSE_CHOICES, (30743, 30744, 30745), ("10 minut", "1 hodina", "12 hodin")))


def source_pause(source):
    """Dialog „Uspat zdroj" nad řádkem ve Stavu zdrojů — zapíše pauzu do `accounts.pause()`,
    kterou jádro čte v `raw_streams()` (`accounts.paused_for`), ať se nedostupný zdroj
    přestane hledat, dokud pauza neskončí."""
    labels = [L(sid, fallback) for _s, sid, fallback in SOURCE_PAUSE_CHOICES]
    if accounts_paused_for(STORE, source) > 0:
        labels.append(L(30746, "Zrušit uspání"))
    choice = xbmcgui.Dialog().select(L(30742, "Uspat zdroj"), labels)
    if choice < 0:
        return
    if choice < len(SOURCE_PAUSE_CHOICES):
        seconds, sid, fallback = SOURCE_PAUSE_CHOICES[choice]
        accounts_pause(STORE, source, seconds)
        notify(Lf(30747, ACCOUNT_TAGS.get(source, source), L(sid, fallback)))
    else:
        accounts_pause(STORE, source, 0)
        notify(L(30746, "Zrušit uspání"))


# --- router ---------------------------------------------------------------------

def router(query):
    p = dict(urllib.parse.parse_qsl(query.lstrip("?")))
    action = p.get("action")
    # akce bez seznamu (RunPlugin) a bez API
    simple = {
        "history_remove": lambda: history_remove(p["type"], p.get("q", "")),
        "history_clear": lambda: _tlacitko(lambda: history_clear(p["type"])),
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
        # akce z nastavení, žádný výpis — succeeded=False jako u "clear_cache"
        "cztor_pair": lambda: (cztor_pair(), xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "sync_create": lambda: _tlacitko(sync_create),
        # SyncWatch: tlačítka s okny (handle −1); složka skupiny je `sw_menu`
        "syncwatch": lambda: _tlacitko(sw_entry),
        "sw_menu": sw_menu,
        "sw_intro": lambda: _tlacitko(sw_intro),
        "sw_create": lambda: _tlacitko(sw_create),
        "sw_join": lambda: _tlacitko(sw_join),
        "sw_window": lambda: _tlacitko(sw_window),
        "sw_leave": lambda: _tlacitko(sw_leave),
        "sw_rejoin": lambda: _tlacitko(sw_rejoin),
        "sw_lock": lambda: _tlacitko(sw_lock),
        "sync_join": lambda: _tlacitko(sync_join),
        "sync_leave": lambda: _tlacitko(sync_leave),
        "cztor_status": lambda: (cztor_status(), xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "cztor_logout": lambda: (cztor_logout(), xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        # succeeded=False jako u "settings" — jinak by Kodi navigoval do prázdné složky
        # a musel by se dát Zpět, i když jde jen o akci, ne o výpis
        "clear_cache": lambda: (STORE.clear_cache(), notify(L(30099)),
                                xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "stats_send": stats_send,
        "log_send": log_send,
        "test_sources": lambda: _tlacitko(test_sources),
        "source_pause": lambda: _tlacitko(lambda: source_pause(p["source"])),
        "remote_setup": lambda: remote_setup_action(p.get("section")),
        "transfer_send": lambda: (transfer_send(),
                                  xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "transfer_receive": lambda: (transfer_receive(),
                                     xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "transfer_file_save": lambda: (transfer_file_save(),
                                       xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "transfer_file_load": lambda: (transfer_file_load(),
                                       xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "stream_layout_reset": lambda: (stream_layout_reset(),
                                        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "setup_wizard": lambda: _tlacitko(lambda: setup_wizard(force=True)),
        "sub_status": sub_status,
        # tlačítka s dialogem, žádný výpis: z nastavení (RunPlugin, handle −1) je to jedno, ale z
        # výpisu (Stav zdrojů → Luna) má Kodi handle ≥ 0 a bez endOfDirectory nahlásí GetDirectory
        # failed a čeká na timeout
        "luna_check": lambda: _tlacitko(lambda: luna_check(ask=True)),
        "os_check": lambda: _tlacitko(os_check),
        "luna_find": lambda: _tlacitko(luna_find),
        "speedtest": lambda: _tlacitko(speedtest),
        "update_repos": lambda: _tlacitko(update_repos),
        "tmdbhelper_player": lambda: _tlacitko(tmdbhelper_player),
        "sync_now": sync_now,
        "info_install": lambda: _tlacitko(info_install),
        "info_donate": lambda: _tlacitko(info_donate),
        "info_terms": lambda: _tlacitko(info_terms),
        "whats_new": whats_new,
        "ha_files": list_ha_files,
        "settings": lambda: (xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False) if HANDLE >= 0 else None,
                             ADDON.openSettings()),
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
            pick_title(get_apis(), p.get("type", "movie"), p["id"], p.get("series"), alt=p.get("alt"),
                       fulltext=bool(p.get("fulltext")))
            return
        if action in ("streams", "streams_filter"):
            # výpis streamů jako složka zrušen v `5.2.14~beta4` — starý odkaz (oblíbené, widget) nic
            # nevypíše, z okna Nokturna otevře dialog; z widgetu/JSON-RPC nikdy modál
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
            if browsing_nokturno():
                xbmc.executebuiltin(runplugin(action="title", type=p.get("type", "movie"), id=p.get("id"),
                                              series=p.get("series"), alt=p.get("alt")))
            return
        if action == "random":
            # ne-složka jako titul v režimu seznamu: klik (handle −1) i Přehrát ze skinu
            # (handle ≥ 0) vedou na tutéž cestu, výpis se nekreslí nikdy
            random_title(get_apis(), p.get("type", "movie"))
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
        elif action == "accounts":
            list_accounts(apis)
        elif action == "accounts_refresh":
            accounts_refresh(apis)
        elif action == "browse":
            browse_menu(apis, p.get("type", "movie"))
        elif action == "foryou":
            list_foryou(apis, p.get("type", "movie"))
        elif action == "similar":
            list_similar(apis, p.get("type", "movie"), p.get("id", ""))
        elif action == "tv":
            list_tv(apis, p.get("date", ""), p.get("kind", ""), p.get("channel", ""))
        elif action == "concerts":
            list_concerts(apis, p.get("mode", ""), p.get("genre", ""), p.get("letter", ""))
        elif action == "concert_artist":
            list_concert_artist(apis, int(p.get("id") or 0))
        elif action == "play_ref":
            play_ref(apis, p.get("ref", ""), p.get("name", ""), p.get("alts", ""))
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
                 pref=p.get("pref", ""), ask=p.get("ask", ""), alts=p.get("alts", ""), sw=p.get("sw", ""))
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


# Akce, u kterých se před výpisem nevyplatí číst `MyVideos*.db` (`adopt_kodi_marks`): nic
# se nekreslí, takže zhlédnuto ze skinu nemá kde chybět — plugin ho převezme při příštím
# výpisu titulů a služba jednou za minutu. Přehrání (widget Pokračovat, HA, Up Next) a
# zahřívání cache jinak platily čtení celé databáze (stovky řádků u velké knihovny) navíc.
MARKS_SKIP = frozenset((
    # přehrání a streamy
    "play", "play_ws", "play_hs", "play_dav", "title", "title_download", "prefetch",
    "download", "download_ws", "download_hs", "toggle_fav", "streams", "streams_filter",
    "dav_browse", "tv_pick", "lang_catalog_trigger",
    # akce bez výpisu titulů (tlačítka v nastavení, hledání, stahování, Trakt, CZtor…)
    "history_remove", "history_clear", "remove_progress", "search", "search_new", "downloads",
    "download_remove", "download_retry", "trakt_auth", "trakt_logout", "cztor_pair",
    "cztor_status", "cztor_logout", "clear_cache", "stats_send", "log_send", "website_info",
    "test_sources", "source_pause", "remote_setup", "stream_layout_reset", "setup_wizard", "sub_status",
    "luna_check", "luna_find", "os_check", "speedtest", "update_repos", "tmdbhelper_player", "sync_now",
    "sync_create", "sync_join", "sync_leave",
    "whats_new", "ha_files", "settings", "transfer_send", "transfer_receive",
    "transfer_file_save", "transfer_file_load",
))


def _tlacitko(fn):
    """Spustí akci s dialogem a výpis zavře jako neúspěšný (nic se nekreslí, Kodi nikam nenavigne)."""
    try:
        fn()
    finally:
        if HANDLE >= 0:
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)


def main(query):
    try:
        # zrušená volba střediska „HA i dashboard"; patří sem, ne do `migrate_on_start`
        # — ta běží na úrovni modulu, tedy dřív, než je tahle funkce definovaná
        migrate_sync_mode()
        migrate_terms()
        action = dict(urllib.parse.parse_qsl(query.lstrip("?"))).get("action") or ""
        # do nastavení a k textu podmínek se uživatel musí dostat i bez souhlasu — jinak
        # by neměl kde ho dát (přepínač je první kategorie nastavení)
        if action not in TERMS_FREE and not ensure_terms():
            _close(action)
            return
        if action not in MARKS_SKIP:
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
