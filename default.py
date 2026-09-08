"""Luna: Absolute Cinema — video doplněk pro Kodi.

Prochází katalogy (TMDB) a přehrává streamy ze serveru Luna přes jeho
Stremio HTTP API. Server Luna běží jinde v síti (např. jako addon Home
Assistantu); Kodi se k Webshare nepřihlašuje, streamuje přes Lunu.
"""
import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

from resources.lib.luna_api import LunaApi, LunaError, parse_base_url, parse_token

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ICON = ADDON.getAddonInfo("icon")
PAGE = 20


def L(sid):
    return ADDON.getLocalizedString(sid)


def build_url(**params):
    return BASE_URL + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


def get_api():
    raw_token = ADDON.getSetting("token")
    token = parse_token(raw_token)
    base = parse_base_url(raw_token, ADDON.getSetting("luna_url") or "http://192.168.1.10:7126")
    if not token:
        xbmcgui.Dialog().ok(L(30000), L(30100))
        ADDON.openSettings()
        raise SystemExit
    return LunaApi(base, token)


def notify_error(err):
    xbmc.log(f"[{ADDON_ID}] {err}", xbmc.LOGERROR)
    xbmcgui.Dialog().notification(L(30000), L(30101), xbmcgui.NOTIFICATION_ERROR, 5000)


# --- položky ------------------------------------------------------------------

def folder_item(label, url, icon=None, art=None, info=None):
    li = xbmcgui.ListItem(label=label)
    li.setArt(art or {"icon": icon or ICON, "thumb": icon or ICON})
    if info:
        fill_info(li, info)
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)


def fill_info(li, meta, ctype="movie", video=None):
    tag = li.getVideoInfoTag()
    tag.setMediaType("episode" if video else ("tvshow" if ctype == "series" else "movie"))
    tag.setTitle((video or meta).get("title") or meta.get("name") or "")
    if ctype == "series":
        tag.setTvShowTitle(meta.get("name") or "")
    plot = (video or {}).get("overview") or meta.get("description") or ""
    tag.setPlot(plot)
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    if year.isdigit():
        tag.setYear(int(year))
    if meta.get("genres"):
        tag.setGenres(list(meta["genres"]))
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
    extras = meta.get("app_extras") or {}
    if isinstance(extras, dict) and extras.get("cast"):
        try:
            tag.setCast([xbmc.Actor(c.get("name", ""), c.get("character", ""), i, c.get("photo") or "")
                         for i, c in enumerate(extras["cast"][:15])])
        except Exception:  # noqa: BLE001 – starší API
            pass


def art_for(meta, video=None):
    art = {
        "poster": meta.get("poster") or "",
        "fanart": meta.get("background") or "",
        "landscape": meta.get("landscapePoster") or "",
        "clearlogo": meta.get("logo") or "",
    }
    if video and video.get("thumbnail"):
        art["thumb"] = video["thumbnail"]
    else:
        art["thumb"] = meta.get("poster") or ""
    return {k: v for k, v in art.items() if v}


def add_meta_item(meta, ctype):
    label = meta.get("name") or meta.get("id")
    li = xbmcgui.ListItem(label=label)
    li.setArt(art_for(meta))
    fill_info(li, meta, ctype)
    if ctype == "series":
        url = build_url(action="seasons", id=meta["id"])
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)
    else:
        add_playable(li, "movie", meta["id"])


def add_playable(li, ctype, item_id, folder_label=None):
    """Podle nastavení buď rovnou přehrát nejlepší stream, nebo otevřít výběr."""
    if ADDON.getSetting("stream_mode") == "0":
        li.setProperty("IsPlayable", "true")
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="play", type=ctype, id=item_id), li, isFolder=False)
    else:
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="streams", type=ctype, id=item_id), li, isFolder=True)


# --- obrazovky --------------------------------------------------------------------

def main_menu():
    folder_item(L(30010), build_url(action="search", type="movie"))
    folder_item(L(30011), build_url(action="search", type="series"))
    folder_item(L(30012), build_url(action="catalogs", type="movie"))
    folder_item(L(30013), build_url(action="catalogs", type="series"))
    xbmcplugin.endOfDirectory(HANDLE)


def list_catalogs(api, ctype):
    for c in api.catalogs(ctype):
        if c["search"]:
            continue
        if c["genres"]:
            folder_item(c["name"], build_url(action="genres", type=ctype, catalog=c["id"]))
        else:
            folder_item(c["name"], build_url(action="catalog", type=ctype, catalog=c["id"]))
    xbmcplugin.endOfDirectory(HANDLE)


def list_genres(api, ctype, cid):
    cat = next((c for c in api.catalogs(ctype) if c["id"] == cid), None)
    if not cat:
        xbmcplugin.endOfDirectory(HANDLE)
        return
    if not cat["genre_required"]:
        folder_item(L(30020), build_url(action="catalog", type=ctype, catalog=cid))
    for g in cat["genres"]:
        folder_item(g, build_url(action="catalog", type=ctype, catalog=cid, genre=g))
    xbmcplugin.endOfDirectory(HANDLE)


def list_catalog(api, ctype, cid, genre=None, search=None, skip=0):
    xbmcplugin.setContent(HANDLE, "tvshows" if ctype == "series" else "movies")
    metas = api.catalog(ctype, cid, genre=genre, search=search, skip=skip)
    for m in metas:
        add_meta_item(m, ctype)
    # Luna vrací stránky po ~20, ale některé katalogy o pár položek méně
    if len(metas) >= PAGE // 2:
        folder_item(L(30021), build_url(action="catalog", type=ctype, catalog=cid, genre=genre,
                                        search=search, skip=skip + len(metas)))
    xbmcplugin.endOfDirectory(HANDLE)


def do_search(api, ctype):
    query = xbmcgui.Dialog().input(L(30010) if ctype == "movie" else L(30011), type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    cid = "search.movie" if ctype == "movie" else "search.series"
    list_catalog(api, ctype, cid, search=query)


def list_seasons(api, series_id):
    meta = api.meta("series", series_id)
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
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="episodes", id=series_id, season=s), li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE)


def list_episodes(api, series_id, season):
    meta = api.meta("series", series_id)
    xbmcplugin.setContent(HANDLE, "episodes")
    videos = [v for v in meta.get("videos") or [] if int(v.get("season") or 0) == season]
    videos.sort(key=lambda v: int(v.get("episode") or 0))
    for v in videos:
        label = "%d. %s" % (int(v.get("episode") or 0), v.get("title") or "")
        li = xbmcgui.ListItem(label=label)
        li.setArt(art_for(meta, v))
        fill_info(li, meta, "series", video=v)
        add_playable(li, "series", v.get("id") or f"{series_id}:{season}:{v.get('episode')}")
    xbmcplugin.endOfDirectory(HANDLE)


def list_streams(api, ctype, item_id):
    streams = api.streams(ctype, item_id, include_search=ADDON.getSetting("search_streams") != "false")
    if not streams:
        xbmcgui.Dialog().notification(L(30000), L(30102), xbmcgui.NOTIFICATION_INFO, 4000)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    for s in streams:
        li = xbmcgui.ListItem(label="[B]%s[/B]  %s" % (s["label"], s["detail"]))
        li.setArt({"icon": ICON, "thumb": ICON})
        li.setProperty("IsPlayable", "true")
        li.getVideoInfoTag().setMediaType("video")
        xbmcplugin.addDirectoryItem(HANDLE, s["url"], li, isFolder=False)
    xbmcplugin.endOfDirectory(HANDLE)


def play(api, ctype, item_id):
    streams = api.streams(ctype, item_id, include_search=ADDON.getSetting("search_streams") != "false")
    if not streams:
        xbmcgui.Dialog().notification(L(30000), L(30102), xbmcgui.NOTIFICATION_INFO, 4000)
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return
    chosen = streams[0]
    if ADDON.getSetting("stream_mode") == "2":
        idx = xbmcgui.Dialog().select(L(30024), ["%s  %s" % (s["label"], s["detail"]) for s in streams])
        if idx < 0:
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return
        chosen = streams[idx]
    li = xbmcgui.ListItem(path=chosen["url"])
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# --- router ---------------------------------------------------------------------

def router(query):
    p = dict(urllib.parse.parse_qsl(query.lstrip("?")))
    action = p.get("action")
    if not action:
        main_menu()
        return
    api = get_api()
    try:
        if action == "catalogs":
            list_catalogs(api, p["type"])
        elif action == "genres":
            list_genres(api, p["type"], p["catalog"])
        elif action == "catalog":
            list_catalog(api, p["type"], p["catalog"], genre=p.get("genre"), search=p.get("search"),
                         skip=int(p.get("skip") or 0))
        elif action == "search":
            do_search(api, p["type"])
        elif action == "seasons":
            list_seasons(api, p["id"])
        elif action == "episodes":
            list_episodes(api, p["id"], int(p.get("season") or 0))
        elif action == "streams":
            list_streams(api, p["type"], p["id"])
        elif action == "play":
            play(api, p["type"], p["id"])
        else:
            main_menu()
    except LunaError as e:
        notify_error(e)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router(sys.argv[2] if len(sys.argv) > 2 else "")
