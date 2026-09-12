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
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
import threading
import time
import urllib.parse
import urllib.request
import unicodedata

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))
from luna_api import LunaApi, LunaError, parse_base_url, parse_token  # noqa: E402
from cinemeta_api import CinemetaApi, CinemetaError  # noqa: E402
from tmdb_api import TmdbApi, TmdbError  # noqa: E402
from sosac_api import SosacError, is_sosac_id as _is_stremio_sosac_id, names_match  # noqa: E402
from sosac_direct import EXPORT as SOSAC_EXPORT, SosacDirect, is_direct_id  # noqa: E402
from enrich import enrich, enrich_one  # noqa: E402
from hellspy_api import HellspyApi, HellspyError  # noqa: E402
from mediainfo import describe as describe_media, probe as probe_media, quality_from_size  # noqa: E402
from store import Store, migrate_profile  # noqa: E402
from sync import sync_once  # noqa: E402
from streams import arrange, estimate_rank, langs_from_name, parse_stream, subs_from_name  # noqa: E402
from trakt_api import TraktApi, TraktError  # noqa: E402
from webshare_api import SORTS, WebshareApi, WebshareError, human_size  # noqa: E402

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ICON = ADDON.getAddonInfo("icon")
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo("path"))
PAGE = 20
WS_PAGE = 40
HS_PAGE = 40
CACHE_TTL = 600
SEARCH_TTL = 12 * 3600  # sjednocené s luna_api.SEARCH_TTL / webshare_api.SEARCH_TTL
SOSAC_TAG = "[COLOR FFE0A040]Sosáč[/COLOR]"
HS_TAG = "[COLOR FFFF8A6B]HellSpy[/COLOR]"
WS_TAG = "[COLOR FF60B0FF]WebShare[/COLOR]"
LUNA_TAG = "[COLOR FFB39DFF]Luna[/COLOR]"
SOURCE_TAGS = {"main": LUNA_TAG, "search": WS_TAG, "sosac": SOSAC_TAG, "ws": WS_TAG, "hs": HS_TAG}
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
Errors = (LunaError, CinemetaError, TmdbError, SosacError, WebshareError, HellspyError, TraktError)

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

def get_luna():
    if not on("luna_enabled"):
        return None
    raw_token = setting("token")
    token = parse_token(raw_token)
    if not token:
        return None
    base = parse_base_url(raw_token, setting("luna_url", "http://192.168.1.10:7126"))
    return LunaApi(base, token, cache=STORE, cache_ttl=CACHE_TTL)


def get_sosac():
    if not on("sosac_enabled"):
        return None
    # veřejné JSONy Sosáče + streamuj.tv s účtem Streamuj. Starší cesta přes
    # Stremio rozhraní Sosáče (userId, login k Sosáči) je pryč — katalogy jsou
    # ve veřejných exportech a k přehrání stačí Streamuj.
    su, sp = setting("streamuj_username").strip(), setting("streamuj_password").strip()
    if su and sp:
        return SosacDirect(su, sp, cache=STORE, cache_ttl=CACHE_TTL, index_store=STORE)
    return None


def get_sosac_db():
    """Veřejný katalog Sosáče (žádný účet, žádný přepínač) — vlastní databáze
    filmů a seriálů česky, funguje vždy. `apis["sosac"]` výš zůstává jen pro
    přihlášené přehrávání a stahování; katalog samotný účet nepotřebuje."""
    return SosacDirect(cache=STORE, cache_ttl=CACHE_TTL, index_store=STORE)


def resolve_url(apis, url):
    """'streamuj:…' (Sosáč) a 'ws:<ident>' (soubor přímo z WebShare) se mění na
    finální odkaz až při přehrání — odkazy WebShare vyprší po pár hodinách."""
    if url and url.startswith("ws:"):
        api = apis.get("ws")
        if api is None:
            raise WebshareError(L(30104))
        link = api.file_link(url[3:])
        remember_ws_token(api)
        return link
    if url and url.startswith("hs:"):
        api = apis.get("hs") or HellspyApi(cache=STORE)
        file_id, _sep, file_hash = url[3:].partition(":")
        return api.file_link(file_id, file_hash)
    if url and url.startswith("streamuj:"):
        api = apis.get("sosac")
        if not isinstance(api, SosacDirect):
            api = SosacDirect(setting("streamuj_username"), setting("streamuj_password"), cache=STORE)
        return api.resolve(url)
    return url


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


def get_tmdb():
    """Vlastní klíč uživatele (zdarma, viz nápověda v nastavení) — přednostní
    náhrada za veřejný katalog Sosáče/Cinemetu, když Luna neběží: umí česky
    i to, co ony ne (popis, obsazení). Bez klíče se prostě nepoužije."""
    key = setting("tmdb_api_key").strip()
    return TmdbApi(key, cache=STORE) if key else None


def get_apis():
    return {"luna": get_luna(), "sosac": get_sosac(), "ws": get_webshare(), "hs": get_hellspy(),
            "cinemeta": get_cinemeta(), "sosac_db": get_sosac_db(), "tmdb": get_tmdb()}


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
    SosacError: "Sosáč", WebshareError: "WebShare", HellspyError: "HellSpy",
    TraktError: "Trakt.tv",
}


def describe_error(e):
    """Jméno zdroje před chybovou hláškou — ať je jasné, který přesně selhal
    (dřív se u víc-zdrojového hledání hlásilo natvrdo „Server Luna neodpovídá“
    i při chybě jinde, třeba na WebShare)."""
    label = next((v for k, v in SOURCE_LABELS.items() if isinstance(e, k)), type(e).__name__)
    return f"{label}: {e}"


def describe_errors(errors):
    lines, seen = [], set()
    for e in errors:
        line = describe_error(e)
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines)


# --- položky ------------------------------------------------------------------

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
    genres = ", ".join(GENRES_CS.get(str(g), str(g)) for g in (meta.get("genres") or []))
    if genres:
        plot = f"[B]{genres}[/B]\n\n{plot}" if plot else genres
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
    except (TypeError, ValueError):
        pass
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
    elif isinstance(meta.get("cast"), list):
        cast = [(str(c), "", "") for c in meta["cast"][:15]]
    if cast:
        try:
            tag.setCast([xbmc.Actor(n, r, i, p) for i, (n, r, p) in enumerate(cast)])
        except Exception:  # noqa: BLE001 – starší API
            pass
    if isinstance(meta.get("director"), list) and meta["director"]:
        tag.setDirectors([str(d) for d in meta["director"]])


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
        apply_watched(li, meta["id"], [fav])
        add_playable(li, "movie", meta["id"], alt=alt)


def add_playable(li, ctype, item_id, series_id=None, alt=None):
    """Podle nastavení buď rovnou přehrát nejlepší stream, nebo otevřít výběr.

    U epizod se předává i id seriálu — Sosáč dává epizodám vlastní id
    (`sosac2_1877:1:1`), ze kterého se meta seriálu nedá odvodit.
    """
    if setting("stream_mode", "1") == "1":
        # jediný režim, kde má smysl vlastní složka: seznam streamů k proklikání
        url = build_url(action="streams", type=ctype, id=item_id, series=series_id, alt=alt)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    else:
        # „0“ (přehrát nejlepší) i „2“ (zeptat se dialogem) jdou rovnou na play() bez
        # url — teprve tam se podle stejného nastavení buď vezme streams[0], nebo
        # otevře Dialog().select(). Dřív oba tyhle režimy místo toho vedly do složky
        # se seznamem streamů, takže „Zeptat se v dialogu“ se nikdy neukázalo.
        li.setProperty("IsPlayable", "true")
        url = build_url(action="play", type=ctype, id=item_id, series=series_id, alt=alt)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)


def add_snapshot_item(key, snap, extra_context=None):
    """Položka ze snímku (Pokračovat, Můj seznam, Naposledy)."""
    if snap.get("type") == "ws":
        f = {"ident": snap["id"][3:], "name": snap.get("title", ""), "img": (snap.get("art") or {}).get("thumb", ""),
             "size_h": snap.get("size_h", ""), "positive": 0, "negative": 0}
        add_ws_file(f, extra_context)
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
    apply_watched(li, key, ctx)
    add_playable(li, "series" if snap.get("season") is not None else "movie", key,
                 series_id=snap.get("series"), alt=snap.get("alt"))


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
    """Detail titulu — u tt… id má TMDB přednost i před Lunou, jakmile má uživatel
    vlastní klíč (stejná priorita jako v `_search_merge`); Luna zůstává zdroj
    streamů, ne metadat. Bez TMDB/Luny zaskočí veřejný katalog Sosáče (u
    sosac-native id), nebo Cinemeta (poslední záchrana, anglicky)."""
    if not is_sosac_id(item_id) and apis["tmdb"]:
        try:
            return apis["tmdb"].meta(meta_type, item_id)
        except TmdbError:
            pass
    try:
        return api_for(apis, item_id).meta(meta_type, item_id)
    except LunaError:
        if is_sosac_id(item_id) and apis["sosac_db"]:
            return apis["sosac_db"].meta(meta_type, item_id)
        if str(item_id).startswith("tt"):
            if apis["tmdb"]:
                try:
                    return apis["tmdb"].meta(meta_type, item_id)
                except TmdbError:
                    pass
            if apis["cinemeta"]:
                return apis["cinemeta"].meta(meta_type, item_id)
        raise


def load_meta(apis, ctype, item_id, series_id=None):
    """Meta titulu (u epizody meta seriálu + konkrétní video) pro popis a OSD."""
    base_id, season, episode = split_episode_id(item_id)
    if season is not None and series_id:
        base_id = series_id
    meta_type = "series" if season is not None else ctype
    meta = meta_for(apis, meta_type, base_id)
    if is_sosac_id(base_id):
        enrich_one(meta, apis["luna"], STORE, meta_type)
    video = None
    if season is not None:
        video = next((v for v in meta.get("videos") or []
                      if int(v.get("season") or 0) == season and int(v.get("episode") or 0) == episode), None)
    return meta, video


def cross_streams(apis, ctype, item_id, meta, alt=None):
    """Streamy z druhého zdroje pro stejný titul (Luna ↔ Sosáč).

    `alt` = id protějšku v Sosáči už známé z hledání (sloučený výsledek) – bez dohledávání.
    """
    if not on("cross_search"):
        return []
    base_id, season, episode = split_episode_id(item_id)
    if alt and apis["sosac"] and not is_sosac_id(base_id):
        try:
            target = alt if season is None else apis["sosac"].episode_id(alt, season, episode)
            return apis["sosac"].streams(ctype, target) if target else []
        except SosacError as e:
            log_error(f"cross-search (alt): {e}")
            return []
    title = meta.get("_title") or meta.get("name") or ""
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    orig = meta.get("_orig") or None
    meta_type = "series" if season is not None else ctype
    try:
        if is_sosac_id(base_id):
            luna = apis["luna"]
            if luna is None:
                return []
            for cand in luna.catalog(meta_type, "search.movie" if meta_type == "movie" else "search.series",
                                     search=title)[:10]:
                cand_year = str(cand.get("year") or cand.get("releaseInfo") or "")[:4]
                if not (names_match(cand.get("name"), title) or (orig and names_match(cand.get("name"), orig))):
                    continue
                if year and cand_year and abs(int(year) - int(cand_year)) > 1:
                    continue
                target = f"{cand['id']}:{season}:{episode}" if season is not None else cand["id"]
                return luna.streams(ctype, target, include_search=on("search_streams"))
        else:
            sosac = apis["sosac"]
            if sosac is None:
                return []
            match = sosac.find_match(meta_type, title, year or None, orig)
            if not match:
                return []
            target = match["id"]
            if season is not None:
                target = sosac.episode_id(match["id"], season, episode)
                if not target:
                    return []
            return sosac.streams(ctype, target)
    except Errors as e:
        log_error(f"cross-search: {e}")
    return []


WS_LIMIT = 25
HS_LIMIT = 25
# značka dílu v názvu souboru: „S01E03", „s1 e3", „1x03"
EPISODE_ANY_RE = re.compile(r"(?<![a-z0-9])s\d{1,2}\s?e\d{1,2}(?!\d)|(?<!\d)\d{1,2}x\d{2}(?!\d)", re.I)
AUDIO_PROBE_MAX = 24          # u kolika streamů se ještě vyplatí číst hlavičku souboru
AUDIO_TTL = 30 * 24 * 3600    # obsah souboru se nemění, stačí zjistit jednou
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


RUNTIME_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?", re.I)


def runtime_minutes(text):
    """Stopáž v minutách. Luna/Cinemeta posílají „2h42min“, epizody bývají
    holé číslo („42“) — bez rozlišení formátu by prosté vytažení číslic
    z „2h42min“ dalo „242“ a z dvouapůlhodinového filmu udělalo čtyřhodinový.
    """
    text = str(text or "")
    m = RUNTIME_RE.search(text)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


def _fold(text):
    """Bez diakritiky, malá písmena — pro porovnávání názvů souborů."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def title_queries(apis, meta, video, ctype, alt=None, strict=True):
    """Dotazy pro fulltextové zdroje a filtr, který z výsledku nechá jen ten titul.

    Sdílí to WebShare i HellSpy — oba hledají v názvech souborů, takže potřebují
    totéž: víc variant názvu (originál, rok, u dílu značku sezóny) a pak zahodit
    všechno, co se jen podobá. Luna se WebShare ptá jedním dotazem a část souborů
    jí uteče, proto se tu hledá ve víc variantách, stejně jako v integraci pro HA.

    `strict=False` (ruční „Zkusit fulltext" ze seznamu streamů) vrací k poloze
    v názvu shovívavější filtr — stačí, aby soubor obsahoval všechna slova
    kdekoli. Používá se jen na výslovné vyžádání, kdy uživatel vidí i výsledky,
    které by přísný filtr zahodil (a počítá s tím, že mezi nimi může být omyl).
    """
    title = meta.get("_title") or meta.get("name") or ""
    origs = [o for o in [meta.get("_orig") or ""] if o]
    if alt and apis.get("sosac"):
        try:
            alt_meta = apis["sosac"].meta(ctype, alt)
            origs += [o for o in (alt_meta.get("_orig") or "", alt_meta.get("_title") or "") if o]
        except Errors:
            pass
    origs = [o for o in dict.fromkeys(origs) if _fold(o) != _fold(title)]
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    year = int(year) if year.isdigit() else None
    if video:
        se, ep = int(video.get("season") or 0), int(video.get("episode") or 0)
        tag = f"S{se:02d}E{ep:02d}"
        queries = [f"{title} {tag}"] + [f"{o} {tag}" for o in origs]
        episode_re = re.compile(rf"s{se:02d}e{ep:02d}|(?<!\d){se:02d}?x{ep:02d}(?!\d)|(?<!\d){se}x{ep:02d}(?!\d)")
        want_year = None
    else:
        queries = [f"{title} {year}" if year else title, title] + [f"{o} {year}" if year else o for o in origs]
        episode_re, want_year = None, year

    def words(text):
        return [w for w in re.split(r"[^a-z0-9]+", _fold(text)) if len(w) > 2]
    wanted = [w for w in [words(title)] + [words(o) for o in origs] if w]
    title_years = {int(y) for y in YEAR_RE.findall(_fold(title) + " " + " ".join(_fold(o) for o in origs))}

    def phrase_leads(tokens, group):
        """Slova názvu musí být v souboru za sebou a skoro na začátku.

        Pouhé „všechna slova někde v názvu" propustí i úplně jiný titul,
        který ta slova jen náhodou obsahuje — např. český idiom „Seber si
        svých pět švestek" vs. film „Pět švestek": obě slova tam jsou,
        ale patří k jiné větě. Skutečný název souboru na nich vždycky
        začíná (nejvýš za značkou webu/edicí v závorce), překódovaný
        balíček zdrojů (rok, kvalita, kodek…) přijde až za ním.
        """
        n = len(group)
        for i in range(len(tokens) - n + 1):
            if tokens[i:i + n] == group:
                return i <= 2
        return False

    def relevant(name):
        folded = _fold(name)
        if wanted:
            if strict:
                tokens = [t for t in re.split(r"[^a-z0-9]+", folded) if t]
                if not any(phrase_leads(tokens, group) for group in wanted):
                    return False
            elif not any(all(w in folded for w in group) for group in wanted):
                return False
        if not video and EPISODE_ANY_RE.search(folded):
            # u filmu nemá soubor se značkou dílu co dělat. Jednoslovný název
            # („Avatar") projde kontrolou slov a díly seriálu rok v názvu nemají,
            # takže by se do seznamu streamů filmu nasypal celý seriál.
            return False
        if want_year:
            years = {int(y) for y in YEAR_RE.findall(folded)} - (title_years - {want_year})
            if years and not any(abs(y - want_year) <= 1 for y in years):
                return False
        return not episode_re or bool(episode_re.search(folded))

    return [q.strip() for q in dict.fromkeys(queries) if q.strip()], relevant


def webshare_streams(apis, meta, video, ctype, alt=None, strict=True):
    """Tytéž soubory přímo z WebShare, navíc k tomu, co našla Luna."""
    ws = apis.get("ws")
    if ws is None:
        return []
    queries, relevant = title_queries(apis, meta, video, ctype, alt, strict)
    out, seen = [], set()
    for query in queries:
        try:
            files, _total = ws.search(query, limit=WS_LIMIT)
        except WebshareError as e:
            log_error(f"WebShare „{query}“: {e}")
            continue
        for f in files:
            if f["ident"] in seen or not relevant(f.get("name") or ""):
                continue
            seen.add(f["ident"])
            # velikost do `detail` — odtud ji parse_stream čte; stejné jednotky jako u Luny
            out.append({"url": "ws:" + f["ident"], "label": f.get("name") or "",
                        "detail": f.get("size_h") or human_size(f.get("size") or 0), "source": "ws",
                        "_loose": not strict})
    remember_ws_token(ws)
    return out


def hellspy_streams(apis, meta, video, ctype, alt=None, strict=True):
    """Tentýž titul na HellSpy. Nabízí se původní soubor, ne překódování, takže
    název i velikost popisují to, co se opravdu přehraje — viz `hellspy_api`."""
    hs = apis.get("hs")
    if hs is None:
        return []
    queries, relevant = title_queries(apis, meta, video, ctype, alt, strict)
    out, seen = [], set()
    for query in queries:
        try:
            files, _next = hs.search(query, limit=HS_LIMIT)
        except HellspyError as e:
            log_error(f"HellSpy „{query}“: {e}")
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
            out.append({"url": f"hs:{f['id']}:{f['hash']}", "label": name,
                        "detail": f.get("size_h") or "", "source": "hs", "_loose": not strict})
    return out


DIRECT_SOURCES = ("ws", "hs")   # fulltextové zdroje, kde bývá tentýž soubor jako u Luny


def media_from_file(apis, url):
    """Co se o souboru dá přečíst z jeho hlavičky. Prázdné, když to nejde."""
    def load():
        try:
            link = resolve_url(apis, url)
        except Errors as e:
            log_error(f"hlavička {url[:28]}: {e}")
            return {}
        return probe_media(link)
    return STORE.cached(f"media:{url}", AUDIO_TTL, load) or {}


def fill_audio(apis, streams, progress=None):
    """Doplní zvuk tam, kde ho zdroj neřekl, a ověří ho tam, kde řekl jen název souboru.

    HellSpy o zvuku nemá ve svém rozhraní vůbec nic a u souborů z fulltextu je
    jen to, co si někdo napsal do názvu. Údaj přitom leží v hlavičce souboru
    a servery umí vydat jen její výřez, takže se přečte pár desítek kB. Běží to
    souběžně a výsledek se pamatuje, takže se za soubor platí jednou.

    Tahle část trvá nejdýl ze všeho při načítání streamů, proto `progress`
    (je-li dán) dostane `tick()` za každý dočtený soubor, ne až na konci.
    """
    try:
        limit = int(setting("audio_probe", str(AUDIO_PROBE_MAX)) or AUDIO_PROBE_MAX)
    except ValueError:
        limit = AUDIO_PROBE_MAX
    if limit <= 0:
        return streams
    # streamy bez počtu kanálů v názvu jdou první — tam chybí úplně všechno.
    # Streamy, které už jazyk podle názvu mají („CZ Dabing"), se ale taky ověří:
    # uploader se může splést nebo zkopírovat popisek z jiného souboru, takže
    # název sám o sobě není důkaz — jen se čeká, až na ně dojde řada v limitu.
    candidates = [s for s in streams if not s.get("_tracks")
                  and str(s.get("url") or "").startswith(("hs:", "ws:", "streamuj:"))]
    todo = sorted(candidates, key=lambda s: bool(s.get("channels")))[:limit]
    if not todo:
        return streams
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(media_from_file, apis, s["url"]): s for s in todo}
        results = {}
        for future in as_completed(futures):
            results[id(futures[future])] = future.result()
            if progress:
                progress.tick()
    for stream, info in ((s, results[id(s)]) for s in todo):
        if not info:
            continue
        text = describe_media(info)
        if text:
            stream["detail"] = f"{stream['detail']} | {text}" if stream.get("detail") else text
        stream["_from_file"] = True
        stream["_tracks"] = info.get("audio") or []
        stream["_media"] = info
        if info.get("duration"):
            # z hlavičky je i skutečná délka streamu, ne jen titulu — přesnější
            # základ pro datový tok než odhad ze stopáže v `ensure_bitrate()`
            stream["_duration"] = info["duration"]
        # parse_stream je idempotentní podle `quality_rank`; po změně popisku
        # se musí přepočítat, jinak by jazyky a kanály zůstaly prázdné
        stream.pop("quality_rank", None)
        parse_stream(stream)
        # rozlišení ze souboru přebíjí název: ten u řady souborů slibuje „4k",
        # a přitom je uvnitř 1080p
        real = quality_from_size(info.get("width") or 0, info.get("height") or 0)
        if real:
            stream["quality_rank"] = {"4K": 4, "Full HD": 3, "HD": 2, "SD": 1}[real]
        if not stream.get("size_gb") and info.get("size"):
            # Sosáč velikost vůbec neříká — server ji ale poslal v hlavičce HTTP
            # odpovědi (Content-Range), když se sahalo pro zvuk. `parse_stream`
            # výše by nastavené GB zase přepsalo z (prázdného) popisku, proto
            # se to dopisuje až tady, po něm.
            stream["size_gb"] = info["size"] / 2 ** 30
    return streams


def drop_duplicates(streams):
    """Soubor nalezený přes Lunu i napřímo je jeden soubor — dvakrát ho neukazovat.
    Odkaz je pokaždé jiný (Luna vs. CDN), proto se dvojice pozná podle názvu a velikosti.
    Stejně se tak srovnají i WebShare s HellSpy mezi sebou, kde bývá táž nahrávka."""
    for s in streams:
        parse_stream(s)

    def key(s):
        return " ".join(_fold(s.get("label")).split()), round(s.get("size_gb") or 0, 1)

    known = {key(s) for s in streams if s.get("source") not in DIRECT_SOURCES}
    out = []
    # Lunino vlastní "Search" (source "search") umí tentýž soubor vrátit i víckrát —
    # všechny kopie mají generický popisek beze jména ("(WS) Full HD"), takže mají
    # stejný `key()` navzájem, ne jen proti přímému nálezu výše
    seen_search = set()
    for s in streams:
        if s.get("source") == "search":
            k = key(s)
            if k in seen_search:
                continue
            seen_search.add(k)
        if s.get("source") not in DIRECT_SOURCES:
            out.append(s)
            continue
        if key(s) in known:
            continue
        known.add(key(s))
        out.append(s)
    return out


def load_meta_video(meta, item_id):
    """Díl z meta seriálu podle id epizody, u filmu None (pro hledání na WebShare)."""
    _base, season, episode = split_episode_id(item_id)
    if season is None:
        return None
    return next((v for v in meta.get("videos") or []
                 if int(v.get("season") or 0) == season and int(v.get("episode") or 0) == episode), None)


def all_streams(apis, ctype, item_id):
    try:
        api = api_for(apis, split_episode_id(item_id)[0])
    except LunaError:
        # titul přišel z Cinemety (nebo Sosáč zrovna vypnutý) — chybí hlavní
        # zdroj úplně, ne že by selhal za běhu (to se hlásí dál jako dřív);
        # cross/WebShare/HellSpy v `collect_streams()` to samy doženou
        return []
    if isinstance(api, LunaApi):
        return api.streams(ctype, item_id, include_search=on("search_streams"))
    return api.streams(ctype, item_id)


def collect_streams(apis, ctype, item_id, meta, alt=None, progress=None, strict=True):
    """Streamy ze zdroje titulu + z druhého zdroje, vyfiltrované a seřazené podle nastavení.

    Oba dotazy běží souběžně — dřív šly za sebou a čas byl jejich součet. Chyba
    hlavního zdroje se hlásí dál jako dřív; dohledání v druhém zdroji si chyby
    jen zaloguje (viz `cross_streams`), takže výsledek druhého vlákna nikdy nechybí.

    `progress`, je-li dán, dostane jeden `tick()` za každý dokončený zdroj —
    vidět aspoň nějaký pohyb dřív, než začne (mnohem delší) čtení hlaviček
    v `fill_audio()`.
    """
    # WebShare se do streamů dohledává vždy, když je účet vyplněný (stejně jako HA) —
    # není to zdvojení Luny: Luna pošle jeden dotaz, tohle víc variant (rok, originál)
    direct = apis.get("ws") is not None
    video = load_meta_video(meta, item_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        main = pool.submit(all_streams, apis, ctype, item_id)
        cross = pool.submit(cross_streams, apis, ctype, item_id, meta, alt)
        ws = pool.submit(webshare_streams, apis, meta, video, ctype, alt, strict) if direct else None
        hs = pool.submit(hellspy_streams, apis, meta, video, ctype, alt, strict) if apis.get("hs") else None
        if progress:
            for _ in as_completed([f for f in (main, cross, ws, hs) if f is not None]):
                progress.tick()
        extra = cross.result() + (ws.result() if ws else []) + (hs.result() if hs else [])
        streams = drop_duplicates(main.result() + extra)
    max_gb = effective_max_gb(video or meta)

    def order(items):
        return arrange(
            items,
            pref_lang=PREF_LANGS[int(setting("pref_lang", "0"))],
            hide_sd=on("hide_sd", "false"),
            max_size_gb=max_gb,
            order=STREAM_ORDERS[int(setting("sort_streams", "0"))],
            pref_surround=on("pref_surround", "false"),
        )

    # Hlavičky se čtou až po seřazení. Kandidátů bývá víc, než se vyplatí číst,
    # a před seřazením se rozpočet utratil za řádky, které skončí dole; teď padne
    # na začátek seznamu, tedy na to, co má uživatel před očima. Po doplnění
    # kanálů se řadí znovu, protože 5.1 může pořadím pohnout.
    return order(ensure_bitrate(fill_audio(apis, order(streams), progress), video or meta))


def ensure_bitrate(streams, meta_or_video):
    """Datový tok a délka má mít úplně každý stream, ne jen ten, co je zdroj sám řekl.

    Přesnost podle toho, odkud se vzala délka. Nejlepší je ta, kterou přímo
    posílá zdroj (`duration`, z popisku Luny). Pak hlavička souboru
    (`_duration`, z `fill_audio`) — z obojího je datový tok stejně přesný
    jako velikost. Bez nich (mimo rozpočet čtení hlavičky, nebo zdroj bez
    vlastního údaje) se počítá se stopáží titulu — to je odhad, stejný,
    se kterým počítá i `effective_max_gb()`, proto se značí vlnovkou stejně
    jako ostatní odhadnuté věci v popisku.

    AVI hlavičky lžou často — `dwTotalFrames` v `avih` je jeden z nejčastěji
    poškozených nebo neaktualizovaných údajů po přebalení souboru. Soubor pak
    tvrdí, že devadesátiminutový film má 14 minut, a datový tok vyjde
    několikanásobně nadsazený. Když je titul znám, přečtená délka ze souboru
    se proto porovná s jeho stopáží — liší-li se o víc než polovinu,
    nedůvěřuje se jí a použije se odhad ze stopáže titulu.
    """
    minutes = runtime_minutes((meta_or_video or {}).get("runtime"))
    fallback_s = minutes * 60 if minutes else DEFAULT_RUNTIME_S
    for s in streams:
        duration = s.get("duration") or s.get("_duration") or 0
        if duration and minutes and not (0.5 <= duration / fallback_s <= 2.0):
            duration = 0   # hlavička zjevně lže (typicky poškozený avih u AVI)
        length = duration or fallback_s
        s["_length_s"] = length
        s["_length_est"] = not duration
        if not s.get("bitrate") and s.get("size_gb"):
            s["bitrate"] = round(s["size_gb"] * 2 ** 30 * 8 / length / 1_000_000, 1)
            s["_bitrate_est"] = not duration
    return streams


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
                "sosac": "Sosáč", "hs": "HellSpy"}


def stream_facets(s):
    """Kvalita, zvuk (jazyk/kanály/kodek), titulky a zdroj — přesně to, co vidí
    uživatel v `stream_label`, jen bez barev a řádkování. Používá to i filtr
    streamů, aby nabízel a pároval přesně to, co je v řádku vidět.

    Kodek zná jen stream, u kterého se přečetla hlavička souboru (`_tracks`) —
    zdroje samy o kodeku nic neříkají, proto se u ostatních prostě nenabídne.
    """
    parse_stream(s)
    raw = s["label"]
    for junk in ("(WS)", "Sosáč"):
        raw = raw.replace(junk, "")
    raw = raw.strip()
    tracks = s.get("_tracks") or []
    if tracks:
        langs = {t.get("lang") for t in tracks if t.get("lang")}
        channels = {t.get("channels") for t in tracks if t.get("channels")}
        codecs = {t.get("codec") for t in tracks if t.get("codec")}
    else:
        langs = set(s.get("langs") or []) | langs_from_name(raw)
        channels = {f"{v:.1f}" for v in (s.get("channels") or {}).values()}
        codecs = set()
    subs = set(s.get("subs") or []) | subs_from_name(raw)
    return {
        "quality_rank": s.get("quality_rank") or 0,
        "langs": langs,
        "channels": channels,
        "codecs": codecs,
        "subs": subs,
        "source": SOURCE_GROUP.get(s.get("source"), s.get("source") or ""),
    }


def apply_stream_filter(streams, fq="", flang="", fch="", fcodec="", fsub="", fsrc=""):
    """Streamy, které vyhovují filtru z `streams_filter`. Prázdný filtr = beze změny."""
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
        if want_lang and not (f["langs"] & want_lang):
            continue
        if want_ch and not (f["channels"] & want_ch):
            continue
        if want_codec and not (f["codecs"] & want_codec):
            continue
        if want_sub and not (f["subs"] & want_sub):
            continue
        if want_src and f["source"] not in want_src:
            continue
        out.append(st)
    return out


def streams_filter(apis, ctype, item_id, series_id, alt, fq, flang, fch, fcodec, fsub, fsrc):
    """Dialog s nabídkou filtrů podle toho, co se u titulu doopravdy našlo.

    Nejde o samostatnou obrazovku — je to stejné volání GetDirectory jako
    „streams", jen se mezi nimi otevře dialog. Zrušení (Esc) nebo prázdný výběr
    beze změny vrátí předchozí filtr, potvrzení jede rovnou na `list_streams`
    s novým, žádný mezikrok navíc.
    """
    meta, video = load_meta(apis, ctype, item_id, series_id)
    streams = collect_streams(apis, ctype, item_id, meta, alt)
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
        list_streams(apis, ctype, item_id, series_id, alt, fq, flang, fch, fcodec, fsub, fsrc)
        return

    active = {"q": set(x for x in fq.split(",") if x), "lang": set(x for x in flang.split(",") if x),
             "ch": set(x for x in fch.split(",") if x), "codec": set(x for x in fcodec.split(",") if x),
             "sub": set(x for x in fsub.split(",") if x), "src": set(x for x in fsrc.split(",") if x)}
    preselect = [i for i, (kind, val) in enumerate(kinds) if val in active[kind]]

    chosen = xbmcgui.Dialog().multiselect(L(30213, "Filtr streamů"), options, preselect=preselect)
    if chosen is None:
        list_streams(apis, ctype, item_id, series_id, alt, fq, flang, fch, fcodec, fsub, fsrc)
        return
    new = {"q": [], "lang": [], "ch": [], "codec": [], "sub": [], "src": []}
    for i in chosen:
        kind, val = kinds[i]
        new[kind].append(val)
    list_streams(apis, ctype, item_id, series_id, alt, fq=",".join(new["q"]), flang=",".join(new["lang"]),
                fch=",".join(new["ch"]), fcodec=",".join(new["codec"]),
                fsub=",".join(new["sub"]), fsrc=",".join(new["src"]))


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
    raw = s["label"]
    for junk in ("(WS)", "Sosáč"):
        raw = raw.replace(junk, "")
    raw = raw.strip()
    quality = QUALITY_NAMES.get(s.get("quality_rank", 0), "")
    if not quality and s.get("size_gb"):
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
    a to není chyba diváka."""
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
    xbmcplugin.setContent(HANDLE, "movies")
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
        # uživatel cokoli zapnuté, není to nová instalace a nemá se ho co ptát
        if any(on(k, "false") for k in ("ws_enabled", "sosac_enabled", "luna_enabled", "hs_enabled")) \
                or setting("tmdb_api_key").strip():
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

        dialog.ok(L(30357, "Nastavení uloženo"),
                  L(30358, "Hotovo! Cokoli z tohohle můžeš kdykoli změnit v Nastavení doplňku.[CR]"
                           "Bez zadaného zdroje budou katalog a hledání fungovat i tak, jen anglicky."))
    STORE.save("wizard_done", True)


def test_sources():
    """Tlačítko v nastavení: během pár vteřin řekne, který zdroj nefunguje a proč.

    Dřív se to poznalo až z prázdného seznamu streamů. Volá se mimo cache, aby
    zelená nebyla jen ozvěna včerejší odpovědi.
    """
    luna, sosac, ws = get_luna(), get_sosac(), get_webshare()
    checks = {
        "Luna": (lambda: len((luna._get(luna._meta_url("manifest.json")) or {}).get("catalogs", []))) if luna else None,
        "Sosáč": (lambda: len(sosac._get(SOSAC_EXPORT + "souboryzanry.json", ttl=0) or {}))
        if isinstance(sosac, SosacDirect) else None,
        "WebShare": (lambda: bool(ws.login())) if ws else None,
    }
    lines = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(fn) for name, fn in checks.items() if fn}
        for name in checks:
            if name not in futures:
                lines.append(f"{name}: {L(30169)}")
                continue
            try:
                result = futures[name].result(timeout=20)
                lines.append(f"{name}: {L(30168)}" + (f" ({result})" if isinstance(result, int) else ""))
            except Exception as e:  # noqa: BLE001 – přesně tohle chceme uživateli ukázat
                lines.append(f"{name}: {str(e)[:90]}")
    if ws:
        remember_ws_token(ws)
    xbmcgui.Dialog().ok(L(30170), "\n".join(lines))


SPEEDTEST_URL = "https://speed.cloudflare.com/__down?bytes=52428800"  # 50 MB, i na rychlém připojení stačí pár vteřin
SPEEDTEST_SECONDS = 8       # déle nemá smysl čekat, průměr se stejně ustálí dřív
SPEEDTEST_RESERVE = 0.25    # rezerva, aby přehrávání nezasekávalo při kolísání rychlosti
DEFAULT_RUNTIME_S = 7200    # dvouhodinový film — odhad stopáže, jen když ji titul sám neřekne


def effective_max_gb(meta_or_video):
    """Max. velikost streamu pro TENHLE titul, spočtená z nastaveného datového toku.

    Velikost souboru sama o sobě neříká, jestli přehrávání poteče plynule —
    rozhoduje datový tok, tedy velikost dělená stopáží. Pevné GB v Nastavení
    proto nedávaly smysl: devadesátiminutová pohádka a tříhodinový epos se
    stejným tokem vyjdou na docela jinou velikost. Když titul stopáž neřekne
    (typicky holé hledání na WebShare bez metadat), použije se dvouhodinový
    odhad — přesně to, s čím počítalo i samotné měření.
    """
    try:
        mbps = float(setting("max_bitrate_mbps", "0").replace(",", ".") or 0)
    except ValueError:
        mbps = 0.0
    if not mbps:
        return 0.0
    minutes = runtime_minutes((meta_or_video or {}).get("runtime"))
    seconds = minutes * 60 if minutes else DEFAULT_RUNTIME_S
    return mbps * 1_000_000 * seconds / 8 / 2 ** 30


def speedtest():
    """Tlačítko v nastavení: změří rychlost stahování a uloží ji jako
    dovolený datový tok s 25% rezervou — viz `effective_max_gb`, kde se
    teprve pro konkrétní titul a jeho stopáž mění na GB.
    """
    dialog = xbmcgui.DialogProgress()
    dialog.create(L(30000), L(30220, "Měřím rychlost stahování…"))
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


def stats_send():
    """Ruční odeslání statistik z nastavení – jinak je posílá služba na pozadí."""
    from stats import COLLECT_URL, Stats
    ok, why = Stats(PROFILE).send(COLLECT_URL, version=ADDON.getAddonInfo("version"))
    notify(L(30165) if ok else f"{L(30166)}: {why}",
           xbmcgui.NOTIFICATION_INFO if ok else xbmcgui.NOTIFICATION_ERROR, 5000)


# --- novinky ve verzi -------------------------------------------------------------

SEEN = "seen"   # seen.json v profilu: {"version": naposledy odbavená verze}


def _vkey(text):
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.\-+]", text))


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
    out = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(\d+(?:\.\d+)*)\s*[–-]\s*(.+)$", line)
        if not m:
            continue
        if since and _vkey(m.group(1)) <= _vkey(since):
            continue
        out.append((m.group(1), m.group(2)))
    return out


def unseen_changelog():
    """Co uživatel po aktualizaci ještě neviděl. Při první instalaci nic —
    jinak by novinky vyskočily hned každému novému uživateli."""
    version = ADDON.getAddonInfo("version")
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
    STORE.save(SEEN, {"version": ADDON.getAddonInfo("version")})
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
    fresh = unseen_changelog()
    if fresh:
        folder_item(f"{L(30192, 'Novinky ve verzi')} {fresh[0][0]}",
                    build_url(action="whats_new"), icon="DefaultAddonsUpdates.png")
    # vlastní ikony místo jedné a té samé ikony doplňku u každé položky — jména
    # standardní sady Kodi (dodává je aktivní skin, žádný soubor navíc v doplňku)
    if STORE.in_progress() or STORE.recently_watched(1):
        folder_item(L(30063), build_url(action="continue"), icon="DefaultInProgressShows.png")
    if apis["luna"] or apis["sosac"] or apis["cinemeta"]:
        # jedno hledání pro filmy i seriály — když dotaz najde obojí, nabídne se volba
        # v kontextovém menu (podržet/kliknout pravým) jde cache hledání vymazat i odsud,
        # ne jen z Nastavení — vynutí to čerstvá data, když se něco změnilo na zdroji
        # Cinemeta funguje vždy (bez účtu), takže tahle položka teď zmizí jen ručním
        # zásahem — ale hledání samo se bez Luny/Sosáče vrátí jen s tím, co zná Cinemeta.
        folder_item(L(30150, "Hledat"), build_url(action="search", type="any"),
                   icon="DefaultAddonsSearch.png", context=[(L(30106), runplugin(action="clear_cache"))])
    if apis["luna"]:
        folder_item(L(30012), build_url(action="catalogs", type="movie", src="luna"), icon="DefaultMovies.png")
        folder_item(L(30013), build_url(action="catalogs", type="series", src="luna"), icon="DefaultTVShows.png")
    if apis["sosac"]:
        folder_item(L(30035), build_url(action="catalogs", type="movie", src="sosac"), icon="DefaultMovies.png")
        folder_item(L(30036), build_url(action="catalogs", type="series", src="sosac"), icon="DefaultTVShows.png")
    if not apis["luna"] and not apis["sosac"]:
        # dřív šlo procházení podle žánru/popularity jen přes Lunu (nebo přihlášený
        # Sosáč) — bez obojího teď zaskočí vlastní databáze: přednostně TMDB (má-li
        # uživatel vlastní klíč, viz get_tmdb — česky i s popisem), jinak veřejný
        # katalog Sosáče (get_sosac_db, česky, bez účtu, ale bez popisu)
        db_src = "tmdb" if apis["tmdb"] else "sosac_db"
        folder_item(L(30330, "Filmy (databáze)"), build_url(action="catalogs", type="movie", src=db_src),
                   icon="DefaultMovies.png")
        folder_item(L(30331, "Seriály (databáze)"), build_url(action="catalogs", type="series", src=db_src),
                   icon="DefaultTVShows.png")
    folder_item(L(30060), build_url(action="favourites"), icon="DefaultFavourites.png")
    folder_item(L(30064), build_url(action="recent"), icon="DefaultRecentlyAddedMovies.png")
    if setting("download_dir"):
        folder_item(L(30071), build_url(action="downloads"), icon="DefaultNetwork.png")
    folder_item(L(30106), build_url(action="clear_cache"), icon="DefaultAddonsUpdates.png")
    if sync_settings():
        folder_item(L(30190, "Staženo v HA"), build_url(action="ha_files"), icon="DefaultNetwork.png")
        folder_item(L(30184, "Synchronizovat teď"), build_url(action="sync_now"), icon="DefaultAddonService.png")
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
GENRE_LABELS = {"Day": "Za den", "Week": "Za týden"}


def list_genres(apis, ctype, cid, src):
    api = apis[src]
    cat = next((c for c in api.catalogs(ctype) if c["id"] == cid), None) if api else None
    if not cat:
        xbmcplugin.endOfDirectory(HANDLE)
        return
    if not cat["genre_required"]:
        folder_item(L(30020), build_url(action="catalog", type=ctype, catalog=cid, src=src),
                   icon="DefaultVideoPlaylists.png")
    for g in cat["genres"]:
        folder_item(GENRE_LABELS.get(g, GENRES_CS.get(g, g)),
                    build_url(action="catalog", type=ctype, catalog=cid, genre=g, src=src),
                    icon="DefaultGenre.png")
    xbmcplugin.endOfDirectory(HANDLE)


def list_catalog(apis, ctype, cid, src, genre=None, search=None, skip=0):
    xbmcplugin.setContent(HANDLE, "tvshows" if ctype == "series" else "movies")
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
    xbmcplugin.endOfDirectory(HANDLE)


# --- hledání + historie -----------------------------------------------------------

def search_title(kind):
    return {"movie": L(30010), "series": L(30011), "ws": L(30045),
            "hs": L(30197, "Hledat na HellSpy"),
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
        folder_item(L(30041), build_url(action="history_clear", type=kind), icon="DefaultAddonsUpdates.png")
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def search_new(apis, kind):
    query = xbmcgui.Dialog().input(search_title(kind), type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)
        return
    search_run(apis, kind, query)


def same_title(luna_meta, sosac_meta):
    """Stejný film/seriál v obou zdrojích: shoda názvu nebo originálu + rok ±1 (je-li znám)."""
    name = luna_meta.get("name") or ""
    if not (names_match(name, sosac_meta.get("_title")) or names_match(name, sosac_meta.get("_orig"))):
        return False
    y1 = str(luna_meta.get("year") or luna_meta.get("releaseInfo") or "")[:4]
    y2 = str(sosac_meta.get("year") or "")[:4]
    if y1.isdigit() and y2.isdigit() and abs(int(y1) - int(y2)) > 1:
        return False
    return True


def merge_results(luna_metas, sosac_metas):
    """Vrátí [(meta, alt)] – titul z Luny s přibaleným id Sosáče, zbylé položky Sosáče zvlášť."""
    merged, used = [], set()
    for lm in luna_metas:
        alt = None
        for sm in sosac_metas:
            if sm["id"] not in used and same_title(lm, sm):
                alt = sm["id"]
                used.add(sm["id"])
                break
        merged.append((lm, alt))
    merged.extend((sm, None) for sm in sosac_metas if sm["id"] not in used)
    return merged


YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def split_year(query):
    """„Pět švestek 2026“ → („Pět švestek“, „2026“). Zdroje hledají jen v názvu,
    rok v dotazu je zmate — odřízneme ho a profiltrujeme jím výsledky."""
    query = (query or "").strip()
    m = YEAR_RE.search(query)
    if not m:
        return query, ""
    base = (query[: m.start()] + " " + query[m.end():]).strip()
    # „2012“ nebo „Blade Runner 2049“ — číslo je součást názvu, ne rok vydání
    if not base or int(m.group(1)) > time.localtime().tm_year + 2:
        return query, ""
    return base, m.group(1)


def filter_year(merged, year):
    """Rok v dotazu je filtr: projdou jen tituly z toho roku (a ty, kde ho zdroj neuvádí)."""
    if not year:
        return merged
    return [(m, a) for m, a in merged
            if str(m.get("year") or m.get("releaseInfo") or "")[:4] in (year, "")]


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


def _search_merge(apis, ctype, query, want_year, errors, tick=None):
    """Sloučené výsledky primárního zdroje a přihlášeného Sosáče pro jeden typ
    (film / seriál), BEZ popisů — stačí na počty pro volbu Filmy/Seriály.
    Kešuje se 5 minut.

    Primární zdroj: TMDB, jakmile má uživatel vlastní klíč — má přednost i před
    Lunou (umí česky i to, co Luna neřekne o titulech odjinud). Bez klíče je
    primární Luna, když je dostupná (beze změny). Bez obojího zaskočí veřejný
    katalog Sosáče (česky, bez účtu, ale bez popisu), teprve když ani ten nic
    nenajde, Cinemeta (anglicky, ale nejširší pokrytí). Přihlášený Sosáč se
    přidává vždycky navíc, nezávisle na tom, co je primární zdroj metadat."""
    def step():
        if tick:
            tick()

    def load():
        # řetězec zdrojů, v pořadí priority — každý se zkusí, jen když předchozí
        # nic nevrátil (chybí, spadl, nebo prostě nic nenašel)
        luna_metas, sosac_metas = [], []
        if apis["tmdb"]:
            try:
                luna_metas = apis["tmdb"].catalog(ctype, "popular", search=query)
            except TmdbError as e:
                errors.append(e)
        if not luna_metas and apis["luna"]:
            try:
                cid = "search.movie" if ctype == "movie" else "search.series"
                luna_metas = apis["luna"].catalog(ctype, cid, search=query)
            except LunaError as e:
                errors.append(e)
        if not luna_metas:
            try:
                luna_metas = apis["sosac_db"].catalog(ctype, "top", search=query)
            except SosacError as e:
                errors.append(e)
        if not luna_metas and apis["cinemeta"]:
            try:
                luna_metas = apis["cinemeta"].catalog(ctype, "top", search=query)
            except CinemetaError as e:
                errors.append(e)
        step()
        if apis["sosac"]:
            try:
                sosac_metas = apis["sosac"].search(ctype, query)
            except SosacError as e:
                errors.append(e)
            step()
        merged = filter_year(merge_results(luna_metas, sosac_metas), want_year)
        return merged, bool(luna_metas) and bool(sosac_metas)
    key = f"search:{ctype}:{query.strip().lower()}:{want_year or ''}"
    return STORE.cached(key, SEARCH_TTL, load)


def search_source(apis, ctype, query, want_year, errors):
    """`_search_merge()` + doplněné popisy — pro skutečné zobrazení seznamu.

    Enrich (u Sosáče dotažení popisu, fronta vláken, čekání na dokončení) se
    kešuje zvlášť od holého katalogu: volba Filmy/Seriály potřebuje jen počty,
    ne popisy, a nesmí na ně čekat — ty se dotáhnou až tady, těsně předtím,
    než se seznam skutečně vypisuje."""
    def load():
        merged, mixed = _search_merge(apis, ctype, query, want_year, errors)
        # dřív jen položky Sosáče (ty jediné popis neměly) — bez Luny ho ale
        # nemají ani ty z Cinemety, `enrich()` si sama vybere, co doopravdy chybí
        enrich([m for m, _alt in merged], apis["luna"], STORE, ctype)
        return merged, mixed
    key = f"searchfull:{ctype}:{query.strip().lower()}:{want_year or ''}"
    return STORE.cached(key, SEARCH_TTL, load)


def search_run(apis, kind, query, offset=0):
    if kind != "hs":
        # HellSpy se hledá jen jako odbočka z dotazu, který v katalozích nic
        # nenašel — do historie patří ten původní dotaz, ne tahle odbočka
        STORE.add_history(kind, query)
    if kind == "ws":
        list_ws_results(apis, query, offset)
        return
    if kind == "hs":
        list_hs_results(apis, query, offset)
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
        bar.create("Nokturno", L(30150, "Hledat"))
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
            xbmcplugin.setContent(HANDLE, "files")
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
    xbmcplugin.setContent(HANDLE, "tvshows" if ctype == "series" else "movies")
    # vždy přes search_source() — i po volbě z Filmy/Seriály; holý katalog výše
    # je z vlastní cache (_search_merge) skoro zadarmo, teprve tady se čeká na popisy
    merged, mixed = search_source(apis, ctype, query, want_year, errors)
    for meta, alt in merged:
        add_meta_item(meta, ctype, alt=alt, tag_source=mixed)
    # bez Luny (nebo když zrovna neodpovídá) nabídneme rovnou soubory z WebShare
    # (jinak je má Luna: Search u titulu — tam by šlo o duplicitu)
    luna_down = any(isinstance(e, LunaError) for e in errors)
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
    xbmcplugin.setContent(HANDLE, "movies")
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
    xbmcplugin.setContent(HANDLE, "movies")
    files, _next = api.search(query, limit=HS_PAGE, offset=offset)
    for f in files:
        add_hs_file(f)
    # HellSpy neposílá celkový počet, jen další offset; další strana se nabídne,
    # dokud chodí plná dávka
    if len(files) == HS_PAGE:
        folder_item(L(30021), build_url(action="search_run", type="hs", q=query, offset=offset + len(files)),
                   icon="DefaultFolder.png")
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
    xbmcplugin.setContent(HANDLE, "movies")
    for key in STORE.favourites():
        snap = STORE.item(key)
        if snap:
            add_snapshot_item(key, snap)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_recent():
    # „videos" je obecný typ a skiny k němu nabízejí jen základní seznam;
    # u konkrétního typu je na výběr celá sada zobrazení. Kodi si zobrazení
    # pamatuje podle typu obsahu, takže tyhle seznamy sdílejí nastavení s Filmy.
    xbmcplugin.setContent(HANDLE, "movies")
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


def list_continue(apis):
    """Rozkoukané tituly + další díly po naposledy zhlédnutých epizodách."""
    # „videos" je obecný typ a skiny k němu nabízejí jen základní seznam;
    # u konkrétního typu je na výběr celá sada zobrazení. Kodi si zobrazení
    # pamatuje podle typu obsahu, takže tyhle seznamy sdílejí nastavení s Filmy.
    xbmcplugin.setContent(HANDLE, "movies")
    for key, _entry in STORE.in_progress():
        snap = STORE.item(key)
        if snap:
            add_snapshot_item(key, snap)
    seen_series = set()
    for key, _entry in STORE.recently_watched(40):
        snap = STORE.item(key)
        if not snap or snap.get("season") is None or snap.get("series") in seen_series:
            continue
        seen_series.add(snap.get("series"))
        found = next_episode(apis, snap)
        if not found:
            continue
        video, meta = found
        ep_id = video.get("id") or f"{snap['series']}:{video.get('season')}:{video.get('episode')}"
        if STORE.playcount(ep_id):
            continue
        li = xbmcgui.ListItem(label=f"{L(30067)}: {meta.get('_title') or meta.get('name')} – "
                                    f"{int(video.get('season') or 0)}x{int(video.get('episode') or 0):02d} {video.get('title') or ''}")
        li.setArt(art_for(meta, video))
        fill_info(li, meta, "series", video=video)
        apply_watched(li, ep_id, [fav_context(ep_id, "series", snap["series"], snap.get("alt"))])
        add_playable(li, "series", ep_id, series_id=snap["series"], alt=snap.get("alt"))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


# --- seriály ---------------------------------------------------------------------

def list_seasons(apis, series_id, alt=None):
    meta = meta_for(apis, "series", series_id)
    if is_sosac_id(series_id):
        enrich_one(meta, apis["luna"], STORE, "series")
    videos = meta.get("videos") or []
    seasons = sorted({int(v.get("season") or 0) for v in videos}, key=lambda s: (s == 0, s))
    xbmcplugin.setContent(HANDLE, "seasons")
    for s in seasons:
        tpl = L(30023)
        label = L(30022) if s == 0 else (tpl % s if "%d" in tpl else f"{tpl} {s}")
        li = xbmcgui.ListItem(label=label)
        li.setArt(art_for(meta))
        fill_info(li, meta, "series")
        li.getVideoInfoTag().setMediaType("season")
        li.getVideoInfoTag().setSeason(s)
        # sezóna je zhlédnutá, když jsou zhlédnuté všechny její epizody
        eps = [v for v in videos if int(v.get("season") or 0) == s]
        if eps and all(STORE.playcount(v.get("id") or f"{series_id}:{s}:{v.get('episode')}") for v in eps):
            li.getVideoInfoTag().setPlaycount(1)
        url = build_url(action="episodes", id=series_id, season=s, alt=alt)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE)


def list_episodes(apis, series_id, season, alt=None):
    meta = meta_for(apis, "series", series_id)
    xbmcplugin.setContent(HANDLE, "episodes")
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
        apply_watched(li, ep_id, [fav_context(ep_id, "series", series_id, alt)])
        add_playable(li, "series", ep_id, series_id=series_id, alt=alt)
    xbmcplugin.endOfDirectory(HANDLE)


# --- přehrávání ------------------------------------------------------------------

def fulltext_item(ctype, item_id, series_id, alt):
    """Odkaz na tuhle obrazovku znovu, ale s uvolněným filtrem WebShare/HellSpy
    (viz `title_queries`, `strict=False`) — pro případ, že přísný automatický
    filtr skutečnou shodu zahodil, protože název souboru je neobvyklý."""
    folder_item(L(30335, "Zkusit fulltext na WebShare/HellSpy"),
                build_url(action="streams", type=ctype, id=item_id, series=series_id, alt=alt, fulltext="1"),
                icon="DefaultAddonsSearch.png")


def list_streams(apis, ctype, item_id, series_id=None, alt=None, fq="", flang="", fch="", fcodec="", fsub="", fsrc="",
                 fulltext=""):
    meta, video = load_meta(apis, ctype, item_id, series_id)
    strict = fulltext != "1"
    has_fulltext_source = bool(apis.get("ws") or apis.get("hs"))
    # ukazatel průběhu: pár kroků na dotazy zdrojům, pak (obvykle nejdelší část)
    # jeden na každý soubor, kterému se čte hlavička v `fill_audio()`
    try:
        probe_limit = int(setting("audio_probe", str(AUDIO_PROBE_MAX)) or AUDIO_PROBE_MAX)
    except ValueError:
        probe_limit = AUDIO_PROBE_MAX
    num_sources = 2 + (1 if apis.get("ws") else 0) + (1 if apis.get("hs") else 0)
    bar = xbmcgui.DialogProgressBG()
    bar.create("Nokturno", L(30238, "Načítám streamy…"))
    bar.update(0)
    progress = SearchProgress(bar, num_sources + max(probe_limit, 0))
    try:
        streams = collect_streams(apis, ctype, item_id, meta, alt, progress, strict)
    finally:
        bar.close()
    if not streams:
        if strict and has_fulltext_source:
            # rovnou selhat by uživateli vzalo možnost zkusit to uvolněněji —
            # nabídne se aspoň ta jedna položka místo prázdné/chybové obrazovky
            xbmcplugin.setContent(HANDLE, "episodes")
            fulltext_item(ctype, item_id, series_id, alt)
            xbmcplugin.endOfDirectory(HANDLE)
            return
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
    xbmcplugin.setContent(HANDLE, "episodes")
    title = (video or {}).get("title") or display_name(meta)
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    # do statistik jde titul bez roku — ten se posílá zvlášť polem `year`,
    # display_name() ho baká přímo do řetězce a v dashboardu by se zdvojil
    stats_title = (video or {}).get("title") or bare_title(meta)
    mark_viewed(item_id, stats_title, year if year.isdigit() else None, "series" if video else ctype)
    if len(streams) > 1:
        active = bool(fq or flang or fch or fcodec or fsub or fsrc)
        # počet vždy — beze filtru aspoň řekne, z kolika streamů se vybírá,
        # s filtrem navíc kolik z nich filtru vyhovělo
        count = f"({len(filtered)}/{len(streams)})" if active else f"({len(streams)})"
        label = f"{L(30213, 'Filtr streamů')}  {count}"
        folder_item(label, build_url(action="streams_filter", type=ctype, id=item_id, series=series_id, alt=alt,
                                     fq=fq, flang=flang, fch=fch, fcodec=fcodec, fsub=fsub, fsrc=fsrc),
                   icon="DefaultAddonsUpdates.png" if active else "DefaultAddonsSearch.png")
    for s in filtered:
        li = xbmcgui.ListItem(label=stream_label(s))
        li.setArt(art_for(meta, video))
        # název titulu do InfoTagu → v OSD přehrávače je jméno filmu/epizody, ne popis streamu;
        # stopáž a hodnocení ne — skin by z nich udělal sloupce a ukrojil šířku popisku streamu
        fill_info(li, meta, "series" if video else ctype, video=video, tech=False)
        fill_streamdetails(li, s)
        apply_watched(li, item_id, [(L(30070), runplugin(action="download", url=s["url"], name=f"{title} [{s['label']}]",
                                                         id=item_id, type=ctype, series=series_id, alt=alt))])
        li.setProperty("IsPlayable", "true")
        # přehrání jde přes plugin (ne přímo URL), aby služba věděla, co se hraje
        url = build_url(action="play", type=ctype, id=item_id, series=series_id, url=s["url"],
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


def play(apis, ctype, item_id, series_id=None, url=None, alt=None, subs="", pref=""):
    meta, video = load_meta(apis, ctype, item_id, series_id)
    # u seriálu si pamatujeme, jaký stream si uživatel vybral — další díl (Up Next,
    # Pokračovat, widget) pak jede stejně bez ptaní; klíč je seriál, ne díl
    pref_key = (series_id or split_episode_id(item_id)[0]) if video else None
    if url:
        chosen = {"url": url, "subtitles": [s for s in subs.split("|") if s]}
        if pref_key and pref_from_param(pref):
            STORE.set_stream_pref(pref_key, pref_from_param(pref))
    else:
        streams = collect_streams(apis, ctype, item_id, meta, alt)
        if not streams:
            notify(L(30102))
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return
        remembered = preferred_stream(streams, STORE.stream_pref(pref_key)) if pref_key else None
        chosen = remembered or streams[0]
        if setting("stream_mode", "1") == "2" and remembered is None:
            idx = xbmcgui.Dialog().select(L(30024), [stream_label(st) for st in streams])
            if idx < 0:
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
                return
            chosen = streams[idx]
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
    # do statistik titul bez roku, viz komentář u mark_viewed v list_streams
    stats_title = (video or {}).get("title") or bare_title(meta)
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


def guess_ext(url, name):
    for ext in (".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm", ".wmv"):
        if name.lower().endswith(ext):
            return ""
        if url.lower().split("?")[0].endswith(ext):
            return ext
    return ".mkv"


def enqueue_download(url, name, key, dest_name=None):
    d = download_dir()
    if not d:
        return
    base = safe_filename(dest_name or name)
    dest = os.path.join(d, base + guess_ext(url, base))
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
    enqueue_download(resolve_url(apis, url), name, f"dl:{key}:{abs(hash(url)) % 10**8}", dest_name=name)


def download_ws(apis, ident, name):
    api = apis["ws"]
    if api is None:
        raise WebshareError(L(30104))
    link = api.file_link(ident)
    remember_ws_token(api)
    if not link:
        notify(L(30102))
        return
    enqueue_download(link, name, f"dl:ws:{ident}", dest_name=name)


def download_hs(apis, file_id, file_hash, name):
    api = apis.get("hs") or HellspyApi(cache=STORE)
    link = api.file_link(file_id, file_hash)
    if not link:
        notify(L(30102))
        return
    enqueue_download(link, name, f"dl:hs:{file_id}", dest_name=name)


def list_downloads():
    xbmcplugin.setContent(HANDLE, "videos")
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
        "search": lambda: search_menu(p["type"]),
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
        "test_sources": test_sources,
        "setup_wizard": lambda: (setup_wizard(force=True),
                                 xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "sub_status": sub_status,
        "speedtest": speedtest,
        "sync_now": sync_now,
        "whats_new": whats_new,
        "ha_files": list_ha_files,
        "settings": lambda: (xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False), ADDON.openSettings()),
    }
    if action in simple:
        return simple[action]()

    apis = get_apis()
    try:
        if not action:
            if not STORE.load("wizard_done", False):
                setup_wizard()
                apis = get_apis()  # nastavení se mohlo změnit, načíst zdroje znovu
            main_menu(apis)
        elif action == "catalogs":
            list_catalogs(apis, p["type"], p.get("src", "luna"))
        elif action == "genres":
            list_genres(apis, p["type"], p["catalog"], p.get("src", "luna"))
        elif action == "catalog":
            list_catalog(apis, p["type"], p["catalog"], p.get("src", "luna"), genre=p.get("genre"),
                         search=p.get("search"), skip=int(p.get("skip") or 0))
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
                 pref=p.get("pref", ""))
        elif action == "play_ws":
            play_ws(apis, p["ident"], p.get("name", ""))
        elif action == "play_hs":
            play_hs(apis, p["id"], p["hash"], p.get("name", ""))
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
        notify(describe_error(e), xbmcgui.NOTIFICATION_ERROR, 5000)
        if action in ("play", "play_ws", "play_hs"):
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        elif action not in ("download", "download_ws", "download_hs", "toggle_fav"):
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router(sys.argv[2] if len(sys.argv) > 2 else "")
