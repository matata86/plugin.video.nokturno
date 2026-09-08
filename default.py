"""Nokturno — video doplněk pro Kodi (Luna: Absolute Cinema + Sosáč + WebShare).

Zdroje (každý jde vypnout v nastavení):
- Luna: server v LAN, TMDB katalogy + streamy z WebShare přes Lunu
- Sosáč: vlastní katalogy a streamy (Stremio API, jen userId)
- WebShare přímo: hledání souborů a stream přes WebShare API (účet), bez Luny
Hledání prochází zapnuté zdroje; u titulu se streamy dohledají i v druhém
zdroji. Historie hledání a zhlédnuto/rozkoukáno se ukládají do profilu doplňku
(zhlédnutí zapisuje služba `service.py` podle skutečného přehrávání).
"""
import json
import os
import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "lib"))
from luna_api import LunaApi, LunaError, parse_base_url, parse_token  # noqa: E402
from sosac_api import SosacApi, SosacError, is_sosac_id, names_match  # noqa: E402
from store import Store  # noqa: E402
from webshare_api import SORTS, WebshareApi, WebshareError  # noqa: E402

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ICON = ADDON.getAddonInfo("icon")
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
PAGE = 20
WS_PAGE = 40
SOSAC_TAG = "[COLOR FFE0A040]Sosáč[/COLOR]"
WS_TAG = "[COLOR FF60B0FF]WebShare[/COLOR]"
PLAYING_PROP = "nokturno.playing"

STORE = Store(PROFILE)
Errors = (LunaError, SosacError, WebshareError)


def L(sid):
    return ADDON.getLocalizedString(sid)


def setting(key, default=""):
    return ADDON.getSetting(key) or default


def on(key, default="true"):
    return setting(key, default) != "false"


def build_url(**params):
    return BASE_URL + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


# --- zdroje ---------------------------------------------------------------------

def get_luna():
    if not on("luna_enabled"):
        return None
    raw_token = setting("token")
    token = parse_token(raw_token)
    if not token:
        return None
    return LunaApi(parse_base_url(raw_token, setting("luna_url", "http://192.168.1.10:7126")), token)


def get_sosac():
    if not on("sosac_enabled"):
        return None
    user_id = setting("sosac_user_id").strip()
    if "userId=" in user_id:
        user_id = user_id.split("userId=", 1)[1].split("&", 1)[0]
    if not user_id:
        return None
    return SosacApi(setting("sosac_url", "https://stremio.sosac.tv/cs"), user_id)


def get_webshare():
    if not on("ws_enabled", "false"):
        return None
    user, pw = setting("ws_username").strip(), setting("ws_password").strip()
    if not user or not pw:
        return None
    # token WebShare přežije mezi voláními pluginu – šetří login
    api = WebshareApi(user, pw, token=xbmcgui.Window(10000).getProperty("nokturno.ws_token"))
    return api


def remember_ws_token(api):
    if api and api.token:
        xbmcgui.Window(10000).setProperty("nokturno.ws_token", api.token)


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


def display_name(meta):
    """Sosáč má složené názvy – ukážeme jen titul (rok); Luna název tak, jak je."""
    if is_sosac_id(meta.get("id")):
        title = meta.get("_title") or meta.get("name") or ""
        year = meta.get("year")
        return f"{title} ({year})" if year else title
    return meta.get("name") or meta.get("id") or ""


def apply_watched(li, key):
    """Zhlédnuto (fajfka) a bod pro pokračování z vlastní evidence."""
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
    label = L(30044) if count else L(30043)
    li.addContextMenuItems([(label, f"RunPlugin({build_url(action='toggle_watched', id=key)})")])


def fill_info(li, meta, ctype="movie", video=None):
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
    if year.isdigit():
        tag.setYear(int(year))
    if meta.get("genres"):
        tag.setGenres([str(g) for g in meta["genres"]])
    try:
        if meta.get("imdbRating"):
            tag.setRating(float(meta["imdbRating"]))
    except (TypeError, ValueError):
        pass
    if meta.get("imdb_id") or str(meta.get("id", "")).startswith("tt"):
        tag.setIMDBNumber(meta.get("imdb_id") or meta.get("id"))
    runtime = str((video or meta).get("runtime") or "")
    digits = "".join(ch for ch in runtime if ch.isdigit())
    if digits:
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


def art_for(meta, video=None):
    art = {
        "poster": meta.get("poster") or "",
        "fanart": meta.get("background") or "",
        "landscape": meta.get("landscapePoster") or "",
        "clearlogo": meta.get("logo") or "",
        "thumb": (video or {}).get("thumbnail") or meta.get("poster") or "",
    }
    return {k: v for k, v in art.items() if v}


def add_meta_item(meta, ctype, alt=None):
    """`alt` = id téhož titulu v Sosáči (sloučený výsledek hledání) → streamy z obou zdrojů."""
    label = display_name(meta)
    if is_sosac_id(meta.get("id")):
        label = f"{label}  {SOSAC_TAG}"
    li = xbmcgui.ListItem(label=label)
    li.setArt(art_for(meta))
    fill_info(li, meta, ctype)
    if ctype == "series":
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="seasons", id=meta["id"], alt=alt), li, isFolder=True)
    else:
        apply_watched(li, meta["id"])
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


def add_ws_file(f):
    key = "ws:" + f["ident"]
    votes = f"+{f['positive']}/-{f['negative']}" if (f["positive"] or f["negative"]) else ""
    label = f"{f['name']}  [COLOR FF9A9A9A]{f['size_h']} {votes}[/COLOR]"
    li = xbmcgui.ListItem(label=label)
    if f.get("img"):
        li.setArt({"thumb": f["img"], "icon": f["img"]})
    tag = li.getVideoInfoTag()
    tag.setMediaType("video")
    tag.setTitle(f["name"])
    tag.setPlot(f"{WS_TAG}  {f['size_h']}  {votes}")
    apply_watched(li, key)
    li.setProperty("IsPlayable", "true")
    url = build_url(action="play_ws", ident=f["ident"], name=f["name"])
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)


# --- meta a streamy -------------------------------------------------------------

def split_episode_id(item_id):
    """'tt0903747:1:2' / 'sosac2_21849:1:2' → (základ, sezóna, epizoda) nebo (id, None, None)."""
    parts = str(item_id).split(":")
    if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
        return ":".join(parts[:-2]), int(parts[-2]), int(parts[-1])
    return item_id, None, None


def load_meta(apis, ctype, item_id, series_id=None):
    """Meta titulu (u epizody meta seriálu + konkrétní video) pro popis a OSD."""
    base_id, season, episode = split_episode_id(item_id)
    if season is not None and series_id:
        base_id = series_id
    api = api_for(apis, base_id)
    meta_type = "series" if season is not None else ctype
    meta = api.meta(meta_type, base_id)
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


def stream_label(s):
    label = s["label"]
    if s.get("source") == "sosac":
        label = f"{SOSAC_TAG} {label[len('Sosáč '):] if label.startswith('Sosáč ') else label}"
    return f"[B]{label}[/B]  {s['detail']}".strip()


def mark_playing(key, title=""):
    xbmcgui.Window(10000).setProperty(PLAYING_PROP, json.dumps({"id": key, "title": title}))


# --- obrazovky --------------------------------------------------------------------

def main_menu(apis):
    if not any(apis.values()):
        xbmcgui.Dialog().ok(L(30000), L(30104))
        ADDON.openSettings()
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    if apis["luna"] or apis["sosac"]:
        folder_item(L(30010), build_url(action="search", type="movie"))
        folder_item(L(30011), build_url(action="search", type="series"))
    if apis["ws"]:
        folder_item(L(30045), build_url(action="search", type="ws"))
    if apis["luna"]:
        folder_item(L(30012), build_url(action="catalogs", type="movie", src="luna"))
        folder_item(L(30013), build_url(action="catalogs", type="series", src="luna"))
    if apis["sosac"]:
        folder_item(L(30035), build_url(action="catalogs", type="movie", src="sosac"))
        folder_item(L(30036), build_url(action="catalogs", type="series", src="sosac"))
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


def list_genres(apis, ctype, cid, src):
    api = apis[src]
    cat = next((c for c in api.catalogs(ctype) if c["id"] == cid), None) if api else None
    if not cat:
        xbmcplugin.endOfDirectory(HANDLE)
        return
    if not cat["genre_required"]:
        folder_item(L(30020), build_url(action="catalog", type=ctype, catalog=cid, src=src))
    for g in cat["genres"]:
        folder_item(g, build_url(action="catalog", type=ctype, catalog=cid, genre=g, src=src))
    xbmcplugin.endOfDirectory(HANDLE)


def list_catalog(apis, ctype, cid, src, genre=None, search=None, skip=0):
    xbmcplugin.setContent(HANDLE, "tvshows" if ctype == "series" else "movies")
    metas = apis[src].catalog(ctype, cid, genre=genre, search=search, skip=skip)
    for m in metas:
        add_meta_item(m, ctype)
    # Luna vrací stránky po ~20, ale některé katalogy o pár položek méně
    if len(metas) >= PAGE // 2:
        folder_item(L(30021), build_url(action="catalog", type=ctype, catalog=cid, src=src, genre=genre,
                                        search=search, skip=skip + len(metas)))
    xbmcplugin.endOfDirectory(HANDLE)


# --- hledání + historie -----------------------------------------------------------

def search_title(kind):
    return {"movie": L(30010), "series": L(30011), "ws": L(30045)}.get(kind, L(30010))


def search_menu(kind):
    """Složka hledání: nové hledání + historie dotazů."""
    folder_item(L(30040), build_url(action="search_new", type=kind))
    for q in STORE.history(kind):
        remove = f"RunPlugin({build_url(action='history_remove', type=kind, q=q)})"
        folder_item(q, build_url(action="search_run", type=kind, q=q), context=[(L(30042), remove)])
    if STORE.history(kind):
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
                alt, _ = sm["id"], used.add(sm["id"])
                break
        merged.append((lm, alt))
    merged.extend((sm, None) for sm in sosac_metas if sm["id"] not in used)
    return merged


def search_run(apis, kind, query, offset=0):
    STORE.add_history(kind, query)
    if kind == "ws":
        list_ws_results(apis, query, offset)
        return
    ctype = kind
    xbmcplugin.setContent(HANDLE, "tvshows" if ctype == "series" else "movies")
    errors, luna_metas, sosac_metas = [], [], []
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
    # stejný titul z obou zdrojů jen jednou – zdroj se ukáže až u streamů
    for meta, alt in merge_results(luna_metas, sosac_metas):
        add_meta_item(meta, ctype, alt=alt)
    # bez Luny nabídneme rovnou soubory z WebShare (jinak je má Luna: Search u titulu)
    if apis["ws"] and not apis["luna"]:
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


def toggle_watched(key):
    STORE.set_watched(key, not STORE.playcount(key))
    xbmc.executebuiltin("Container.Refresh")


# --- seriály ---------------------------------------------------------------------

def list_seasons(apis, series_id, alt=None):
    meta = api_for(apis, series_id).meta("series", series_id)
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
        apply_watched(li, ep_id)
        add_playable(li, "series", ep_id, series_id=series_id, alt=alt)
    xbmcplugin.endOfDirectory(HANDLE)


# --- přehrávání ------------------------------------------------------------------

def list_streams(apis, ctype, item_id, series_id=None, alt=None):
    meta, video = load_meta(apis, ctype, item_id, series_id)
    streams = all_streams(apis, ctype, item_id) + cross_streams(apis, ctype, item_id, meta, alt)
    if not streams:
        notify(L(30102))
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    for s in streams:
        li = xbmcgui.ListItem(label=stream_label(s))
        li.setArt(art_for(meta, video))
        # název titulu do InfoTagu → v OSD přehrávače je jméno filmu/epizody, ne popis streamu
        fill_info(li, meta, "series" if video else ctype, video=video)
        apply_watched(li, item_id)
        li.setProperty("IsPlayable", "true")
        # přehrání jde přes plugin (ne přímo URL), aby služba věděla, co se hraje
        url = build_url(action="play", type=ctype, id=item_id, series=series_id, url=s["url"])  # alt už netřeba
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    xbmcplugin.endOfDirectory(HANDLE)


def play(apis, ctype, item_id, series_id=None, url=None, alt=None):
    meta, video = load_meta(apis, ctype, item_id, series_id)
    if url:
        chosen = {"url": url}
    else:
        streams = all_streams(apis, ctype, item_id) + cross_streams(apis, ctype, item_id, meta, alt)
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
    li = xbmcgui.ListItem(label=title, path=chosen["url"])
    li.setArt(art_for(meta, video))
    fill_info(li, meta, "series" if video else ctype, video=video)
    mark_playing(item_id, title)
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
    mark_playing("ws:" + ident, name)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# --- router ---------------------------------------------------------------------

def router(query):
    p = dict(urllib.parse.parse_qsl(query.lstrip("?")))
    action = p.get("action")
    # akce bez seznamu (RunPlugin)
    if action == "history_remove":
        return history_remove(p["type"], p.get("q", ""))
    if action == "toggle_watched":
        return toggle_watched(p["id"])
    if action == "history_clear":
        return history_clear(p["type"])
    if action == "search":
        return search_menu(p["type"])

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
        elif action == "seasons":
            list_seasons(apis, p["id"], alt=p.get("alt"))
        elif action == "episodes":
            list_episodes(apis, p["id"], int(p.get("season") or 0), alt=p.get("alt"))
        elif action == "streams":
            list_streams(apis, p["type"], p["id"], p.get("series"), alt=p.get("alt"))
        elif action == "play":
            play(apis, p["type"], p["id"], p.get("series"), url=p.get("url"), alt=p.get("alt"))
        elif action == "play_ws":
            play_ws(apis, p["ident"], p.get("name", ""))
        else:
            main_menu(apis)
    except Errors as e:
        log_error(e)
        notify(L(30103) if isinstance(e, WebshareError) else L(30101), xbmcgui.NOTIFICATION_ERROR, 5000)
        if action in ("play", "play_ws"):
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        else:
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router(sys.argv[2] if len(sys.argv) > 2 else "")
