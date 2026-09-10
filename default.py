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
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
import time
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))
from luna_api import LunaApi, LunaError, parse_base_url, parse_token  # noqa: E402
from sosac_api import SosacApi, SosacError, names_match, register as sosac_register  # noqa: E402
from sosac_api import is_sosac_id as _is_stremio_sosac_id  # noqa: E402
from sosac_direct import SosacDirect, is_direct_id  # noqa: E402
from enrich import enrich, enrich_one  # noqa: E402
from store import Store, migrate_profile  # noqa: E402
from streams import arrange, langs_from_name, parse_stream  # noqa: E402
from trakt_api import TraktApi, TraktError  # noqa: E402
from webshare_api import SORTS, WebshareApi, WebshareError, human_size  # noqa: E402

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ICON = ADDON.getAddonInfo("icon")
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
PAGE = 20
WS_PAGE = 40
CACHE_TTL = 600
SEARCH_TTL = 12 * 3600  # sjednocené s luna_api.SEARCH_TTL / webshare_api.SEARCH_TTL
SOSAC_TAG = "[COLOR FFE0A040]Sosáč[/COLOR]"
WS_TAG = "[COLOR FF60B0FF]WebShare[/COLOR]"
LUNA_TAG = "[COLOR FFB39DFF]Luna[/COLOR]"
SOURCE_TAGS = {"main": LUNA_TAG, "search": WS_TAG, "sosac": SOSAC_TAG, "ws": WS_TAG}
QUALITY_COLORS = {4: "FFFFC94D", 3: "FF7FE07F", 2: "FF7FC8FF", 1: "FFA0A0A0"}
LANG_COLORS = {"CZ": "FF7FE07F", "SK": "FF7FE07F", "EN": "FFE0E0E0"}
GREY = "FF9A9A9A"
PLAYING_PROP = "nokturno.playing"
VIEWED_PROP = "nokturno.viewed"   # služba si odsud bere „u titulu se zobrazily streamy“ pro statistiky
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
Errors = (LunaError, SosacError, WebshareError, TraktError)


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


def sosac_user_id():
    user_id = setting("sosac_user_id").strip()
    if "userId=" in user_id:
        user_id = user_id.split("userId=", 1)[1].split("&", 1)[0]
    return user_id


def get_sosac():
    if not on("sosac_enabled"):
        return None
    # napřímo: veřejné JSONy Sosáče + streamuj.tv s účtem Streamuj (bez Stremia, bez loginu k Sosáči)
    su, sp = setting("streamuj_username").strip(), setting("streamuj_password").strip()
    if su and sp:
        return SosacDirect(su, sp, cache=STORE, cache_ttl=CACHE_TTL, index_store=STORE)
    # starší režim přes Stremio doplněk Sosáče (userId)
    user_id = sosac_user_id()
    if not user_id and setting("sosac_username") and setting("sosac_password"):
        user_id = sosac_link_account(quiet=True)
    if user_id:
        return SosacApi(setting("sosac_url", "https://stremio.sosac.tv/cs"), user_id, cache=STORE, cache_ttl=CACHE_TTL)
    return None


def resolve_url(apis, url):
    """'streamuj:…' odkazy Sosáče napřímo se mění na finální mp4 až při přehrání."""
    if url and url.startswith("streamuj:"):
        api = apis.get("sosac")
        if not isinstance(api, SosacDirect):
            api = SosacDirect(setting("streamuj_username"), setting("streamuj_password"), cache=STORE)
        return api.resolve(url)
    return url


def sosac_link_account(quiet=False):
    """Přiváže účet Sosáče + Streamuj k novému userId a uloží ho do nastavení."""
    su, sp = setting("sosac_username").strip(), setting("sosac_password").strip()
    tu, tp = setting("streamuj_username").strip() or su, setting("streamuj_password").strip() or sp
    if not su or not sp:
        if not quiet:
            xbmcgui.Dialog().ok(L(30000), L(30125))
        return ""
    try:
        user_id = sosac_register(setting("sosac_url", "https://stremio.sosac.tv/cs"), su, sp, tu, tp)
    except SosacError as e:
        log_error(e)
        if not quiet:
            notify(L(30126), xbmcgui.NOTIFICATION_ERROR, 5000)
        return ""
    ADDON.setSetting("sosac_user_id", user_id)
    if not quiet:
        notify(L(30127))
    return user_id


def get_webshare():
    if not on("ws_enabled", "false"):
        return None
    user, pw = setting("ws_username").strip(), setting("ws_password").strip()
    if not user or not pw:
        return None
    # token WebShare přežije mezi voláními pluginu – šetří login
    return WebshareApi(user, pw, token=xbmcgui.Window(10000).getProperty("nokturno.ws_token"), cache=STORE)


def remember_ws_token(api):
    if api and api.token:
        xbmcgui.Window(10000).setProperty("nokturno.ws_token", api.token)


def get_trakt():
    if not on("trakt_enabled", "false"):
        return None
    api = TraktApi(setting("trakt_client_id"), setting("trakt_client_secret"), tokens=STORE.trakt(),
                   on_tokens=STORE.set_trakt)
    return api


def get_apis():
    return {"luna": get_luna(), "sosac": get_sosac(), "ws": get_webshare()}


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
    tag.setPlot((video or {}).get("overview") or meta.get("description") or "")
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
    runtime = str((video or meta).get("runtime") or "")
    digits = "".join(ch for ch in runtime if ch.isdigit())
    if tech and digits:
        tag.setDuration(int(digits) * 60)
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
    if setting("stream_mode", "1") == "0":
        li.setProperty("IsPlayable", "true")
        url = build_url(action="play", type=ctype, id=item_id, series=series_id, alt=alt)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    else:
        url = build_url(action="streams", type=ctype, id=item_id, series=series_id, alt=alt)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)


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


# --- meta a streamy -------------------------------------------------------------

def load_meta(apis, ctype, item_id, series_id=None):
    """Meta titulu (u epizody meta seriálu + konkrétní video) pro popis a OSD."""
    base_id, season, episode = split_episode_id(item_id)
    if season is not None and series_id:
        base_id = series_id
    api = api_for(apis, base_id)
    meta_type = "series" if season is not None else ctype
    meta = api.meta(meta_type, base_id)
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


def all_streams(apis, ctype, item_id):
    api = api_for(apis, split_episode_id(item_id)[0])
    if isinstance(api, LunaApi):
        return api.streams(ctype, item_id, include_search=on("search_streams"))
    return api.streams(ctype, item_id)


def collect_streams(apis, ctype, item_id, meta, alt=None):
    """Streamy ze zdroje titulu + z druhého zdroje, vyfiltrované a seřazené podle nastavení."""
    streams = all_streams(apis, ctype, item_id) + cross_streams(apis, ctype, item_id, meta, alt)
    try:
        max_gb = float(setting("max_size_gb", "0").replace(",", ".") or 0)
    except ValueError:
        max_gb = 0.0
    return arrange(
        streams,
        pref_lang=PREF_LANGS[int(setting("pref_lang", "0"))],
        hide_sd=on("hide_sd", "false"),
        max_size_gb=max_gb,
        order=STREAM_ORDERS[int(setting("sort_streams", "0"))],
        pref_surround=on("pref_surround", "false"),
    )


def stream_label(s):
    """Jeden řádek: zvuk · velikost · kvalita · zdroj · zbytek.

    Pořadí je dané šířkou: skiny s úzkým sloupcem seznamu (Arctic Fuse dává seznamu jen
    půl obrazovky) konec řádku oříznou, proto jde jazyk s počtem kanálů a velikost dopředu
    a zdroj až za kvalitu. 5.1 a víc je tučně, ať je surround vidět na první pohled.

    Luna = přesná shoda přes Lunu, WebShare = fulltext WebShare (přes Lunu nebo přímo), Sosáč = streamuj.
    """
    parse_stream(s)
    tag = SOURCE_TAGS.get(s.get("source"), "")
    raw = s["label"]
    for junk in ("(WS)", "Sosáč"):
        raw = raw.replace(junk, "")
    raw = raw.strip()
    # kvalita = tučně a barevně, zbytek názvu (HDR, DV, 60 %) normálně
    quality = {4: "4K", 3: "Full HD", 2: "HD", 1: "SD"}.get(s.get("quality_rank", 0), "")
    import re as _re
    rest = _re.sub(r"\b(4K|Full HD|UHD|FHD|HD|SD)\b", "", raw)   # \b → „HDR“ zůstane celé
    rest = " ".join(rest.replace(" - ", " ").split())
    if s.get("source") == "sosac":
        rest = ""   # u Sosáče je zbytek jen jazyk, ten je už ve zvuku

    parts = []
    channels = s.get("channels") or {}
    pref = PREF_LANGS[int(setting("pref_lang", "0"))]
    # metadata zdroje nemusí sedět na soubor („EN 5.1“ u souboru „…_cz_…“) — jazyk z názvu se přidá
    codes = set(s.get("langs") or []) | langs_from_name(raw)
    langs = []
    for code in sorted(codes, key=lambda c: (c != pref, c)):   # preferovaný jazyk první
        txt = f"[COLOR {LANG_COLORS.get(code, 'FFE0E0E0')}]{code}[/COLOR]"
        if code in channels:
            txt += f" [B]{channels[code]:g}[/B]" if channels[code] >= 5.1 else f" {channels[code]:g}"
        langs.append(txt)
    if langs:
        parts.append(" ".join(langs))
    if s.get("size_gb"):
        parts.append(f"{s['size_gb']:.1f} GB")
    parts.append(f"[COLOR {QUALITY_COLORS.get(s.get('quality_rank', 0), GREY)}][B]{quality or raw}[/B][/COLOR]")
    if tag:
        parts.append(tag)
    if rest and quality:
        parts.append(f"[COLOR {GREY}]{rest}[/COLOR]")
    if s.get("subs"):
        parts.append(f"[COLOR {GREY}]tit. {' '.join(sorted(s['subs']))}[/COLOR]")
    if s.get("bitrate"):
        parts.append(f"[COLOR {GREY}]{s['bitrate']:g} Mb/s[/COLOR]")
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


def stats_send():
    """Ruční odeslání statistik z nastavení – jinak je posílá služba na pozadí."""
    from stats import Stats
    ok, why = Stats(PROFILE).send(setting("stats_url", "").strip(),
                                  version=ADDON.getAddonInfo("version"))
    notify(L(30165) if ok else f"{L(30166)}: {why}",
           xbmcgui.NOTIFICATION_INFO if ok else xbmcgui.NOTIFICATION_ERROR, 5000)


# --- obrazovky --------------------------------------------------------------------

def main_menu(apis):
    mark_used()
    if not any(apis.values()):
        # bez modálního dialogu: ten by při volání z widgetu/JSON-RPC čekal na OK a zablokoval i vypínání Kodi
        notify(L(30104), xbmcgui.NOTIFICATION_WARNING, 6000)
        folder_item(L(30107), build_url(action="settings"))
        xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
        return
    # vlastní ikony místo jedné a té samé ikony doplňku u každé položky — jména
    # standardní sady Kodi (dodává je aktivní skin, žádný soubor navíc v doplňku)
    if STORE.in_progress() or STORE.recently_watched(1):
        folder_item(L(30063), build_url(action="continue"), icon="DefaultInProgressShows.png")
    if apis["luna"] or apis["sosac"]:
        # jedno hledání pro filmy i seriály — když dotaz najde obojí, nabídne se volba
        # v kontextovém menu (podržet/kliknout pravým) jde cache hledání vymazat i odsud,
        # ne jen z Nastavení — vynutí to čerstvá data, když se něco změnilo na zdroji
        folder_item(L(30150, "Hledat"), build_url(action="search", type="any"),
                   icon="DefaultAddonsSearch.png", context=[(L(30106), runplugin(action="clear_cache"))])
    if apis["ws"]:
        folder_item(L(30045), build_url(action="search", type="ws"), icon="DefaultAddonsSearch.png")
    if apis["luna"]:
        folder_item(L(30012), build_url(action="catalogs", type="movie", src="luna"), icon="DefaultMovies.png")
        folder_item(L(30013), build_url(action="catalogs", type="series", src="luna"), icon="DefaultTVShows.png")
    if apis["sosac"]:
        folder_item(L(30035), build_url(action="catalogs", type="movie", src="sosac"), icon="DefaultMovies.png")
        folder_item(L(30036), build_url(action="catalogs", type="series", src="sosac"), icon="DefaultTVShows.png")
    folder_item(L(30060), build_url(action="favourites"), icon="DefaultFavourites.png")
    folder_item(L(30064), build_url(action="recent"), icon="DefaultRecentlyAddedMovies.png")
    if setting("download_dir"):
        folder_item(L(30071), build_url(action="downloads"), icon="DefaultNetwork.png")
    folder_item(L(30106), build_url(action="clear_cache"), icon="DefaultAddonsUpdates.png")
    xbmcplugin.endOfDirectory(HANDLE)


def list_catalogs(apis, ctype, src):
    api = apis[src]
    if api is None:
        raise LunaError(L(30104))
    for c in api.catalogs(ctype):
        if c["search"]:
            continue
        action = "genres" if c["genres"] else "catalog"
        folder_item(c["name"], build_url(action=action, type=ctype, catalog=c["id"], src=src))
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
        folder_item(L(30020), build_url(action="catalog", type=ctype, catalog=cid, src=src))
    for g in cat["genres"]:
        folder_item(GENRE_LABELS.get(g, g),
                    build_url(action="catalog", type=ctype, catalog=cid, genre=g, src=src))
    xbmcplugin.endOfDirectory(HANDLE)


def list_catalog(apis, ctype, cid, src, genre=None, search=None, skip=0):
    xbmcplugin.setContent(HANDLE, "tvshows" if ctype == "series" else "movies")
    metas = apis[src].catalog(ctype, cid, genre=genre, search=search, skip=skip)
    if src == "sosac":
        # exporty Sosáče nemají popis → dotáhnout podle IMDb id (Luna / Cinemeta, cache)
        enrich(metas, apis["luna"], STORE, ctype)
    for m in metas:
        add_meta_item(m, ctype)
    # Luna vrací stránky po ~20, ale některé katalogy o pár položek méně
    if len(metas) >= PAGE // 2:
        folder_item(L(30021), build_url(action="catalog", type=ctype, catalog=cid, src=src, genre=genre,
                                        search=search, skip=skip + len(metas)))
    xbmcplugin.endOfDirectory(HANDLE)


# --- hledání + historie -----------------------------------------------------------

def search_title(kind):
    return {"movie": L(30010), "series": L(30011), "ws": L(30045),
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
    folder_item(L(30040), build_url(action="search_new", type=kind))
    history = search_history(kind)
    for q in history:
        folder_item(q, build_url(action="search_run", type=kind, q=q),
                    context=[(L(30042), runplugin(action="history_remove", type=kind, q=q))])
    if history:
        folder_item(L(30041), build_url(action="history_clear", type=kind))
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


def _search_merge(apis, ctype, query, want_year, errors):
    """Sloučené výsledky Luny a Sosáče pro jeden typ (film / seriál), BEZ popisů —
    stačí na počty pro volbu Filmy/Seriály. Kešuje se 5 minut."""
    def load():
        luna_metas, sosac_metas = [], []
        if apis["luna"]:
            try:
                cid = "search.movie" if ctype == "movie" else "search.series"
                luna_metas = apis["luna"].catalog(ctype, cid, search=query)
            except LunaError as e:
                errors.append(e)
        if apis["sosac"]:
            try:
                sosac_metas = apis["sosac"].search(ctype, query)
            except SosacError as e:
                errors.append(e)
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
        enrich([m for m, _alt in merged if is_sosac_id(m.get("id"))], apis["luna"], STORE, ctype)
        return merged, mixed
    key = f"searchfull:{ctype}:{query.strip().lower()}:{want_year or ''}"
    return STORE.cached(key, SEARCH_TTL, load)


def search_run(apis, kind, query, offset=0):
    STORE.add_history(kind, query)
    if kind == "ws":
        list_ws_results(apis, query, offset)
        return
    raw_query = query
    query, want_year = split_year(query)
    errors = []
    if kind == "any":
        # jedno hledání pro obojí; volba se nabídne, jen když dotaz sedí na filmy i seriály.
        # Oba dotazy běží souběžně — jinak by procházení čekalo na součet obou (7 s místo 4 s).
        # Jen holý katalog (_search_merge), bez popisů — na volbu Filmy/Seriály
        # stačí počty a čekání na enrich by ji zbytečně zdrželo.
        progress = xbmcgui.DialogProgressBG()
        progress.create("Nokturno", L(30150, "Hledat"))
        progress.update(0)
        results = {}
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(_search_merge, apis, "movie", query, want_year, errors): "movie",
                          pool.submit(_search_merge, apis, "series", query, want_year, errors): "series"}
                # aktualizace v pořadí, jak doopravdy dobíhají — zůstat na pevném
                # pořadí (nejdřív film) by procento drželo na 0 %, dokud nedoběhnou oba
                done = 0
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
                    done += 1
                    progress.update(int(done / len(futures) * 100))
        finally:
            progress.close()
        movies, _ = results["movie"]
        series, _ = results["series"]
        if movies and series:
            xbmcplugin.setContent(HANDLE, "files")
            folder_item(f"{L(30012)} ({len(movies)})", build_url(action="search_run", type="movie", q=raw_query))
            folder_item(f"{L(30013)} ({len(series)})", build_url(action="search_run", type="series", q=raw_query))
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
    for e in errors:
        log_error(e)
    if errors:
        notify(L(30101), xbmcgui.NOTIFICATION_WARNING)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_ws_results(apis, query, offset=0):
    api = apis["ws"]
    if api is None:
        raise WebshareError(L(30104))
    xbmcplugin.setContent(HANDLE, "videos")
    files, total = api.search(query, sort=SORTS[int(setting("ws_sort", "0"))], limit=WS_PAGE, offset=offset)
    remember_ws_token(api)
    for f in files:
        add_ws_file(f)
    if offset + len(files) < total and files:
        folder_item(L(30021), build_url(action="search_run", type="ws", q=query, offset=offset + len(files)))
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def history_remove(kind, query):
    STORE.remove_history(kind, query)
    xbmc.executebuiltin("Container.Refresh")


def history_clear(kind):
    STORE.clear_history(kind)
    xbmc.executebuiltin("Container.Refresh")
    xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)


# --- zhlédnuto, Můj seznam, Pokračovat ----------------------------------------------

def toggle_watched(key):
    watched = not STORE.playcount(key)
    STORE.set_watched(key, watched)
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
    xbmc.executebuiltin("Container.Refresh")


def list_favourites():
    xbmcplugin.setContent(HANDLE, "videos")
    for key in STORE.favourites():
        snap = STORE.item(key)
        if snap:
            add_snapshot_item(key, snap)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)


def list_recent():
    xbmcplugin.setContent(HANDLE, "videos")
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
    xbmcplugin.setContent(HANDLE, "videos")
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
    meta = api_for(apis, series_id).meta("series", series_id)
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
    meta = api_for(apis, series_id).meta("series", series_id)
    xbmcplugin.setContent(HANDLE, "episodes")
    videos = [v for v in meta.get("videos") or [] if int(v.get("season") or 0) == season]
    videos.sort(key=lambda v: int(v.get("episode") or 0))
    for v in videos:
        label = "%d. %s" % (int(v.get("episode") or 0), v.get("title") or "")
        li = xbmcgui.ListItem(label=label)
        li.setArt(art_for(meta, v))
        fill_info(li, meta, "series", video=v)
        ep_id = v.get("id") or f"{series_id}:{season}:{v.get('episode')}"
        apply_watched(li, ep_id, [fav_context(ep_id, "series", series_id, alt)])
        add_playable(li, "series", ep_id, series_id=series_id, alt=alt)
    xbmcplugin.endOfDirectory(HANDLE)


# --- přehrávání ------------------------------------------------------------------

def list_streams(apis, ctype, item_id, series_id=None, alt=None):
    meta, video = load_meta(apis, ctype, item_id, series_id)
    streams = collect_streams(apis, ctype, item_id, meta, alt)
    if not streams:
        notify(L(30102))
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    # bez tohohle skin nezná typ obsahu a nabídne jen holý „Seznam základní“
    # místo bohatších zobrazení (plakát, popis) — jediné místo v doplňku,
    # kde to chybělo
    xbmcplugin.setContent(HANDLE, "videos")
    title = (video or {}).get("title") or display_name(meta)
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    # do statistik jde titul bez roku — ten se posílá zvlášť polem `year`,
    # display_name() ho baká přímo do řetězce a v dashboardu by se zdvojil
    stats_title = (video or {}).get("title") or bare_title(meta)
    mark_viewed(item_id, stats_title, year if year.isdigit() else None, "series" if video else ctype)
    for s in streams:
        li = xbmcgui.ListItem(label=stream_label(s))
        li.setArt(art_for(meta, video))
        # název titulu do InfoTagu → v OSD přehrávače je jméno filmu/epizody, ne popis streamu;
        # stopáž a hodnocení ne — skin by z nich udělal sloupce a ukrojil šířku popisku streamu
        fill_info(li, meta, "series" if video else ctype, video=video, tech=False)
        apply_watched(li, item_id, [(L(30070), runplugin(action="download", url=s["url"], name=f"{title} [{s['label']}]",
                                                         id=item_id, type=ctype, series=series_id, alt=alt))])
        li.setProperty("IsPlayable", "true")
        # přehrání jde přes plugin (ne přímo URL), aby služba věděla, co se hraje
        url = build_url(action="play", type=ctype, id=item_id, series=series_id, url=s["url"],
                        subs="|".join(s.get("subtitles") or []))
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    xbmcplugin.endOfDirectory(HANDLE)


def play(apis, ctype, item_id, series_id=None, url=None, alt=None, subs=""):
    meta, video = load_meta(apis, ctype, item_id, series_id)
    if url:
        chosen = {"url": url, "subtitles": [s for s in subs.split("|") if s]}
    else:
        streams = collect_streams(apis, ctype, item_id, meta, alt)
        if not streams:
            notify(L(30102))
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return
        chosen = streams[0]
        if setting("stream_mode", "1") == "2":
            idx = xbmcgui.Dialog().select(L(30024), [stream_label(s) for s in streams])
            if idx < 0:
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
                return
            chosen = streams[idx]
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
        "sosac_link": sosac_link_account,
        "trakt_logout": trakt_logout,
        # succeeded=False jako u "settings" — jinak by Kodi navigoval do prázdné složky
        # a musel by se dát Zpět, i když jde jen o akci, ne o výpis
        "clear_cache": lambda: (STORE.clear_cache(), notify(L(30099)),
                                xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False)),
        "stats_send": stats_send,
        "settings": lambda: (xbmcplugin.endOfDirectory(HANDLE, succeeded=False, cacheToDisc=False), ADDON.openSettings()),
    }
    if action in simple:
        return simple[action]()

    apis = get_apis()
    try:
        if not action:
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
        elif action == "toggle_fav":
            toggle_fav(apis, p["id"], p.get("type", "movie"), p.get("series"), p.get("alt"))
        elif action == "seasons":
            list_seasons(apis, p["id"], alt=p.get("alt"))
        elif action == "episodes":
            list_episodes(apis, p["id"], int(p.get("season") or 0), alt=p.get("alt"))
        elif action == "streams":
            list_streams(apis, p["type"], p["id"], p.get("series"), alt=p.get("alt"))
        elif action == "play":
            play(apis, p["type"], p["id"], p.get("series"), url=p.get("url"), alt=p.get("alt"), subs=p.get("subs", ""))
        elif action == "play_ws":
            play_ws(apis, p["ident"], p.get("name", ""))
        elif action == "download":
            download_stream(apis, p["url"], p.get("name", ""), p["id"], p.get("type", "movie"), p.get("series"), p.get("alt"))
        elif action == "download_ws":
            download_ws(apis, p["ident"], p.get("name", ""))
        else:
            main_menu(apis)
    except Errors as e:
        log_error(e)
        notify(L(30103) if isinstance(e, WebshareError) else L(30101), xbmcgui.NOTIFICATION_ERROR, 5000)
        if action in ("play", "play_ws"):
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        elif action not in ("download", "download_ws", "toggle_fav"):
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router(sys.argv[2] if len(sys.argv) > 2 else "")
