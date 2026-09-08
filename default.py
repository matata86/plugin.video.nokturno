"""Nokturno — video doplněk pro Kodi (Luna: Absolute Cinema + Sosáč).

Dva zdroje: server Luna (TMDB katalogy + streamy z WebShare přes Lunu) a
Sosáč (vlastní katalogy a streamy ze streamuj.tv, Stremio API). Hledání
prohledává oba; u titulu z jednoho zdroje se streamy dohledají i v druhém.
Kodi se nikam nepřihlašuje — Luna běží jinde v LAN, Sosáč jde přes userId.
"""
import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

from resources.lib.luna_api import LunaApi, LunaError, parse_base_url, parse_token
from resources.lib.sosac_api import SosacApi, SosacError, is_sosac_id, names_match

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ICON = ADDON.getAddonInfo("icon")
PAGE = 20
SOSAC_TAG = "[COLOR FFE0A040]Sosáč[/COLOR]"


def L(sid):
    return ADDON.getLocalizedString(sid)


def setting(key, default=""):
    return ADDON.getSetting(key) or default


def build_url(**params):
    return BASE_URL + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


def get_luna():
    raw_token = setting("token")
    token = parse_token(raw_token)
    base = parse_base_url(raw_token, setting("luna_url", "http://192.168.1.10:7126"))
    if not token:
        xbmcgui.Dialog().ok(L(30000), L(30100))
        ADDON.openSettings()
        raise SystemExit
    return LunaApi(base, token)


def get_sosac():
    """Sosáč je volitelný — bez userId nebo vypnutý → None."""
    if setting("sosac_enabled", "true") == "false":
        return None
    user_id = setting("sosac_user_id").strip()
    if "userId=" in user_id:
        user_id = user_id.split("userId=", 1)[1].split("&", 1)[0]
    if not user_id:
        return None
    return SosacApi(setting("sosac_url", "https://stremio.sosac.tv/cs"), user_id)


def notify(msg, kind=xbmcgui.NOTIFICATION_INFO, ms=4000):
    xbmcgui.Dialog().notification(L(30000), msg, kind, ms)


def log_error(err):
    xbmc.log(f"[{ADDON_ID}] {err}", xbmc.LOGERROR)


# --- položky ------------------------------------------------------------------

def folder_item(label, url, icon=None):
    li = xbmcgui.ListItem(label=label)
    li.setArt({"icon": icon or ICON, "thumb": icon or ICON})
    xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)


def display_name(meta):
    """Sosáč má složené názvy – ukážeme jen titul (rok); Luna název tak, jak je."""
    if is_sosac_id(meta.get("id")):
        title = meta.get("_title") or meta.get("name") or ""
        year = meta.get("year")
        return f"{title} ({year})" if year else title
    return meta.get("name") or meta.get("id") or ""


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


def add_meta_item(meta, ctype):
    label = display_name(meta)
    if is_sosac_id(meta.get("id")):
        label = f"{label}  {SOSAC_TAG}"
    li = xbmcgui.ListItem(label=label)
    li.setArt(art_for(meta))
    fill_info(li, meta, ctype)
    if ctype == "series":
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="seasons", id=meta["id"]), li, isFolder=True)
    else:
        add_playable(li, "movie", meta["id"])


def add_playable(li, ctype, item_id, series_id=None):
    """Podle nastavení buď rovnou přehrát nejlepší stream, nebo otevřít výběr.

    U epizod se předává i id seriálu — Sosáč dává epizodám vlastní id
    (`sosac2_1877:1:1`), ze kterého se meta seriálu nedá odvodit.
    """
    if setting("stream_mode", "1") == "0":
        li.setProperty("IsPlayable", "true")
        url = build_url(action="play", type=ctype, id=item_id, series=series_id)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    else:
        url = build_url(action="streams", type=ctype, id=item_id, series=series_id)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=True)


# --- zdroje ---------------------------------------------------------------------

def source_for(item_id):
    return "sosac" if is_sosac_id(item_id) else "luna"


def api_for(apis, item_id):
    api = apis[source_for(item_id)]
    if api is None:
        raise LunaError("Sosáč není nastaven")
    return api


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


def cross_streams(apis, ctype, item_id, meta):
    """Streamy z druhého zdroje pro stejný titul (Luna ↔ Sosáč)."""
    if setting("cross_search", "true") == "false":
        return []
    base_id, season, episode = split_episode_id(item_id)
    title = meta.get("_title") or meta.get("name") or ""
    year = str(meta.get("year") or meta.get("releaseInfo") or "")[:4]
    orig = meta.get("_orig") or None
    meta_type = "series" if season is not None else ctype
    try:
        if is_sosac_id(base_id):
            luna = apis["luna"]
            for cand in luna.catalog(meta_type, "search.movie" if meta_type == "movie" else "search.series",
                                     search=title)[:10]:
                cand_year = str(cand.get("year") or cand.get("releaseInfo") or "")[:4]
                if not (names_match(cand.get("name"), title) or (orig and names_match(cand.get("name"), orig))):
                    continue
                if year and cand_year and abs(int(year) - int(cand_year)) > 1:
                    continue
                target = f"{cand['id']}:{season}:{episode}" if season is not None else cand["id"]
                return luna.streams(ctype, target, include_search=setting("search_streams", "true") != "false")
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
    except (LunaError, SosacError) as e:
        log_error(f"cross-search: {e}")
    return []


def all_streams(apis, ctype, item_id):
    api = api_for(apis, split_episode_id(item_id)[0])
    if isinstance(api, LunaApi):
        streams = api.streams(ctype, item_id, include_search=setting("search_streams", "true") != "false")
    else:
        streams = api.streams(ctype, item_id)
    return streams


def stream_label(s):
    label = s["label"]
    if s.get("source") == "sosac":
        label = f"{SOSAC_TAG} {label[len('Sosáč '):] if label.startswith('Sosáč ') else label}"
    return f"[B]{label}[/B]  {s['detail']}".strip()


# --- obrazovky --------------------------------------------------------------------

def main_menu(apis):
    folder_item(L(30010), build_url(action="search", type="movie"))
    folder_item(L(30011), build_url(action="search", type="series"))
    folder_item(L(30012), build_url(action="catalogs", type="movie", src="luna"))
    folder_item(L(30013), build_url(action="catalogs", type="series", src="luna"))
    if apis["sosac"]:
        folder_item(L(30035), build_url(action="catalogs", type="movie", src="sosac"))
        folder_item(L(30036), build_url(action="catalogs", type="series", src="sosac"))
    xbmcplugin.endOfDirectory(HANDLE)


def list_catalogs(apis, ctype, src):
    api = apis[src]
    for c in api.catalogs(ctype):
        if c["search"]:
            continue
        action = "genres" if c["genres"] else "catalog"
        folder_item(c["name"], build_url(action=action, type=ctype, catalog=c["id"], src=src))
    xbmcplugin.endOfDirectory(HANDLE)


def list_genres(apis, ctype, cid, src):
    api = apis[src]
    cat = next((c for c in api.catalogs(ctype) if c["id"] == cid), None)
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


def do_search(apis, ctype):
    query = xbmcgui.Dialog().input(L(30010) if ctype == "movie" else L(30011), type=xbmcgui.INPUT_ALPHANUM)
    if not query:
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    xbmcplugin.setContent(HANDLE, "tvshows" if ctype == "series" else "movies")
    errors = []
    try:
        for m in apis["luna"].catalog(ctype, "search.movie" if ctype == "movie" else "search.series", search=query):
            add_meta_item(m, ctype)
    except LunaError as e:
        errors.append(e)
    if apis["sosac"]:
        try:
            for m in apis["sosac"].search(ctype, query):
                add_meta_item(m, ctype)
        except SosacError as e:
            errors.append(e)
    for e in errors:
        log_error(e)
    if errors:
        notify(L(30101), xbmcgui.NOTIFICATION_WARNING)
    xbmcplugin.endOfDirectory(HANDLE)


def list_seasons(apis, series_id):
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
        xbmcplugin.addDirectoryItem(HANDLE, build_url(action="episodes", id=series_id, season=s), li, isFolder=True)
    xbmcplugin.endOfDirectory(HANDLE)


def list_episodes(apis, series_id, season):
    meta = api_for(apis, series_id).meta("series", series_id)
    xbmcplugin.setContent(HANDLE, "episodes")
    videos = [v for v in meta.get("videos") or [] if int(v.get("season") or 0) == season]
    videos.sort(key=lambda v: int(v.get("episode") or 0))
    for v in videos:
        label = "%d. %s" % (int(v.get("episode") or 0), v.get("title") or "")
        li = xbmcgui.ListItem(label=label)
        li.setArt(art_for(meta, v))
        fill_info(li, meta, "series", video=v)
        add_playable(li, "series", v.get("id") or f"{series_id}:{season}:{v.get('episode')}", series_id=series_id)
    xbmcplugin.endOfDirectory(HANDLE)


def list_streams(apis, ctype, item_id, series_id=None):
    meta, video = load_meta(apis, ctype, item_id, series_id)
    streams = all_streams(apis, ctype, item_id) + cross_streams(apis, ctype, item_id, meta)
    if not streams:
        notify(L(30102))
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    for s in streams:
        li = xbmcgui.ListItem(label=stream_label(s))
        li.setArt(art_for(meta, video))
        # název titulu do InfoTagu → v OSD přehrávače je jméno filmu/epizody, ne popis streamu
        fill_info(li, meta, "series" if video else ctype, video=video)
        li.setProperty("IsPlayable", "true")
        xbmcplugin.addDirectoryItem(HANDLE, s["url"], li, isFolder=False)
    xbmcplugin.endOfDirectory(HANDLE)


def play(apis, ctype, item_id, series_id=None):
    meta, video = load_meta(apis, ctype, item_id, series_id)
    streams = all_streams(apis, ctype, item_id) + cross_streams(apis, ctype, item_id, meta)
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
    # label i InfoTag: při přímém otevření (JSON-RPC, widgety) nemá Kodi původní položku seznamu
    li = xbmcgui.ListItem(label=(video or {}).get("title") or display_name(meta), path=chosen["url"])
    li.setArt(art_for(meta, video))
    fill_info(li, meta, "series" if video else ctype, video=video)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


# --- router ---------------------------------------------------------------------

def router(query):
    p = dict(urllib.parse.parse_qsl(query.lstrip("?")))
    action = p.get("action")
    apis = {"luna": None, "sosac": None}
    try:
        apis["luna"] = get_luna()
        apis["sosac"] = get_sosac()
        if not action:
            main_menu(apis)
        elif action == "catalogs":
            list_catalogs(apis, p["type"], p.get("src", "luna"))
        elif action == "genres":
            list_genres(apis, p["type"], p["catalog"], p.get("src", "luna"))
        elif action == "catalog":
            list_catalog(apis, p["type"], p["catalog"], p.get("src", "luna"), genre=p.get("genre"),
                         search=p.get("search"), skip=int(p.get("skip") or 0))
        elif action == "search":
            do_search(apis, p["type"])
        elif action == "seasons":
            list_seasons(apis, p["id"])
        elif action == "episodes":
            list_episodes(apis, p["id"], int(p.get("season") or 0))
        elif action == "streams":
            list_streams(apis, p["type"], p["id"], p.get("series"))
        elif action == "play":
            play(apis, p["type"], p["id"], p.get("series"))
        else:
            main_menu(apis)
    except (LunaError, SosacError) as e:
        log_error(e)
        notify(L(30101), xbmcgui.NOTIFICATION_ERROR, 5000)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)


if __name__ == "__main__":
    router(sys.argv[2] if len(sys.argv) > 2 else "")
