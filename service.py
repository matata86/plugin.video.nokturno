"""Služba na pozadí Nokturna.

1. Zhlédnuto / rozkoukáno: plugin před přehráním nastaví vlastnost okna
   `nokturno.playing` (JSON s klíčem titulu). Služba během přehrávání zapisuje
   pozici; při skončení nad 90 % označí titul jako zhlédnutý, jinak uloží pozici.
   Plugin se po `setResolvedUrl` ukončí, proto to nejde dělat v něm.
2. Trakt scrobble (start/stop) pro tituly s IMDb/TMDB id, je-li Trakt zapnutý.
3. Stahování: fronta v `downloads.json`, jeden soubor po druhém, průběh se
   zapisuje zpět, zrušení se pozná podle stavu „cancel“.
4. Anonymní statistiky: čítače přehrání a poslední použití (`stats.json`),
   jednou za čas odeslané na adresu z nastavení. Zapisuje je jen služba, plugin
   jí události předává vlastnostmi okna.
"""
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()


class _KodiLogHandler(logging.Handler):
    """Varování z knihovny (např. `store.py`: selhaný zápis souboru) do kodi.log —
    bez toho by je Python jen tiše pustil na stderr, kam se v Kodi nikdo nedívá."""
    def emit(self, record):
        level = xbmc.LOGERROR if record.levelno >= logging.ERROR else xbmc.LOGWARNING
        xbmc.log(f"[{ADDON.getAddonInfo('id')}/{record.name}] {self.format(record)}", level)


logging.getLogger().addHandler(_KodiLogHandler())
logging.getLogger().setLevel(logging.WARNING)
sys.path.insert(0, os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo("path")), "resources", "lib"))
from hellspy_api import HellspyApi  # noqa: E402
from sledujteto_api import SledujtetoApi  # noqa: E402
from fastshare_api import FastshareApi  # noqa: E402
from cztor_api import CztorApi  # noqa: E402
from prehrajto_api import PrehrajtoApi  # noqa: E402
from sosac_direct import SosacDirect  # noqa: E402
from stats import COLLECT_URL, Stats  # noqa: E402
from crash import CRASH_URL, CrashReporter  # noqa: E402
import accounts as accounts_lib  # noqa: E402
from storage_api import StorageApi, parse_ref  # noqa: E402
from store import Store, migrate_profile  # noqa: E402
from sync import sync_once  # noqa: E402
import syncbox  # noqa: E402
from trend_api import CATALOG_ID as TREND_CATALOG_ID  # noqa: E402
from tracks import SUBS_WHEN_NEEDED, pick_audio, pick_subtitle, track_lang  # noqa: E402
from trakt_api import TraktApi, TraktError  # noqa: E402
from webshare_api import WebshareApi  # noqa: E402
import kodi_marks  # noqa: E402 – vedle service.py, čte videodatabázi Kodi
import kodi_settings  # noqa: E402 – vedle service.py, most do settings.xml
import kodi_sources  # noqa: E402 – vedle service.py, sdílený výčet zdrojů do statistik
import setsync  # noqa: E402
import watch as watch_lib  # noqa: E402

PROP = "nokturno.playing"
VIEWED_PROP = "nokturno.viewed"
USED_PROP = "nokturno.used"
SYNC_PROP = "nokturno.sync"
CRASH_PROP = "nokturno.crash"   # plugin → služba: nové hlášení o pádu ve frontě (default.report_crash)
CRASH_EVERY = 30 * 60           # fronta hlášení bez nového pádu (neodeslané kvůli síti) — jednou za čas
FORCE_STATS_GAP = 600   # nucené odeslání po aktualizaci jen když poslední úspěšné bylo dřív
FORCE_STATS_PROP = "nokturno.force_stats"   # plugin → služba: aktualizace doplňku, nečekat na SEND_EVERY
SYNC_EVERY = 5 * 60   # výměna se střediskem; změny (dokoukáno, Můj seznam) ji vyvolají hned
SYNC_FIRST = 20       # první výměna po startu — ať je rozkoukanost z druhé TV hned na začátku
KODI_MARKS_EVERY = 60   # s – „Označit jako zhlédnuté“ ze skinu, viz KodiMarks
SUB_CHECK_EVERY = 12 * 3600   # jak často se ptát WebShare na stav předplatného
ACCOUNTS_DELAY = 240      # po startu Kodi napřed skin a widgety, teprve pak stav účtů
ACCOUNTS_EVERY = 6 * 3600     # pod `accounts.TTL` (12 h), ať v menu nestojí zastaralý stav
ACCOUNTS_RETRY = 20 * 60      # zdroj, na který se nešlo dostat, zkusit dřív — viz AccountsChecker.tick
ACCOUNTS_TRIGGER_PROP = "nokturno.accounts_trigger"   # stejný literál jako v default.py
WATCH_TRIGGER_PROP = "nokturno.watch_check"           # stejný literál jako v default.py
WATCH_DELAY = 6 * 60        # po startu napřed skin, widgety a stav zdrojů
WATCH_EVERY = 30 * 60       # jak často se podívat, jestli něco z Hlídaných nečeká na kontrolu
WATCH_DAILY = 24 * 3600     # seznam z Traktu (`watch_state.last_run`) — jinak se nepozná, že je co dělat
WATCH_NOTICE_EVERY = 60     # oznámení nových dílů, i těch, co přišly synchronizací
WATCH_NOTICE_MAX = 3        # víc toastů naráz je šum; zbytek je vidět v menu Hlídané
                                                      # (refresh_accounts_after_test) — obnovit hned
WATCHED_PCT = 0.90
MIN_RESUME = 90  # s – po takové době přehrávání patří titul do rozkoukaných
SAVE_EVERY = 30  # s – jak často se za běhu přepisuje pozice rozkoukaného
POLL = 5
MESSAGE_DELAY = 30       # po startu Kodi nechat doběhnout skin, než může přijít modální Dialog().ok()
WARM_DELAY = 180          # po startu Kodi nechat nejdřív doběhnout skin a widgety
WARM_EVERY = int(2.5 * 3600)   # pod TTL žebříčků Sosáče (3 h); s WARM_PROP se cache obnoví i před vypršením
WARM_PROP = "nokturno.warm"    # plugin při zahřívání cache API jen zapisuje, nečte (viz default.warming)
WARM_RETRY = 10 * 60      # když se zrovna přehrává, zahřívání počká
LANG_WARM_DELAY = 300     # živé ověřování zdrojů je dražší než ostatní zahřívání, ať nestartuje současně s ním
LANG_WARM_EVERY = 6 * 3600     # pod `LANG_CATALOG_TTL` (8 h) v default.py, ať uživatel nenarazí na živý přepočet
FORYOU_SEEN_KEY = "foryou_seen"      # stejný literál jako v default.py (note_foryou_open)
FORYOU_SEEN_DAYS = 14                # a stejná lhůta jako LANG_SEEN_DAYS níž
LANG_SEEN_KEY = "lang_catalog_seen"   # stejný literál jako v default.py (note_lang_catalog_open)
LANG_SEEN_DAYS = 14                  # a stejná lhůta — kdo seznam neotevřel, tomu se nezahřívá
LANG_TRIGGER_PROP = "nokturno.lang_trigger"  # stejný literál jako v default.py (lang_catalog_trigger) —
                                              # žádost o okamžitý přepočet, viz lang_trigger_watcher
LANG_TRIGGER_POLL = 2     # s – jak často se čeká na žádost z lang_catalog_trigger (klik uživatele)
QUIT_PROP = "nokturno.quitting"   # stejný literál jako v default.py — Kodi končí, viz ServiceMonitor
CHUNK = 1024 * 1024
PREF_LANGS = ("", "CZ", "SK", "EN", "HU")   # pořadí voleb `pref_lang` v settings.xml, stejné jako v default.py
TRACKS_DELAY = 1.0   # s po onAVStarted — externí titulky Kodi přidává až po otevření videa
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
TMDBH_PLAYER = "special://profile/addon_data/plugin.video.themoviedb.helper/players/nokturno.json"
_STARTED_AT = time.time()   # MESSAGE_DELAY se počítá odsud, ne od okamžiku, kdy dorazí FORCE_STATS_PROP


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f"[plugin.video.nokturno/service] {msg}", level)


def refresh_tmdbhelper_player():
    """Player pro TMDb Helper přidává jen tlačítko v nastavení (`default.tmdbhelper_player`).
    Když už nainstalovaný je, drží se po aktualizaci doplňku aktuální — jinak by v TMDb
    Helperu zůstal starý odkaz i po změně parametrů pluginu."""
    dest = xbmcvfs.translatePath(TMDBH_PLAYER)
    src = os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo("path")), "resources", "players", "nokturno.json")
    try:
        if not (os.path.exists(dest) and os.path.exists(src)):
            return
        with open(src, "rb") as new, open(dest, "rb") as old:
            if new.read() == old.read():
                return
        xbmcvfs.copy(src, dest)
        log("player pro TMDb Helper aktualizován")
    except OSError as e:
        log(f"player pro TMDb Helper: {e}", xbmc.LOGWARNING)


def fresh_addon():
    """Doplněk s čerstvě načteným nastavením, nebo None.

    Při aktualizaci doplňku ho Kodi na chvíli odregistruje dřív, než se služba
    stihne ukončit — `xbmcaddon.Addon()` v tu chvíli hodí RuntimeError („Unknown
    addon id"), Kodi to ukáže jako „Chyba, více v protokolu" a teprve pak dopíše
    „Aktualizováno". Pro nás to není chyba, jen konec života téhle instance.
    """
    try:
        return xbmcaddon.Addon()
    except RuntimeError:
        return None


def L(sid, fallback=""):
    return ADDON.getLocalizedString(sid) or fallback


def Lf(sid, *args):
    text = L(sid)
    try:
        return text % args if "%" in text else " ".join([text] + [str(a) for a in args])
    except TypeError:
        return " ".join([text] + [str(a) for a in args])


def split_key(key):
    """'tt0903747:1:2' → ('tt0903747', 1, 2); 'tt0133093' → ('tt0133093', None, None)."""
    parts = str(key).split(":")
    if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
        return ":".join(parts[:-2]), int(parts[-2]), int(parts[-1])
    return key, None, None


# --- Trakt ------------------------------------------------------------------------

def get_trakt(store):
    addon = fresh_addon()
    if addon is None or addon.getSetting("trakt_enabled") != "true":
        return None
    api = TraktApi(addon.getSetting("trakt_client_id"), addon.getSetting("trakt_client_secret"),
                   tokens=store.trakt(), on_tokens=store.set_trakt)
    return api if api.logged_in() else None


# --- přehrávání ---------------------------------------------------------------------

class Player(xbmc.Player):
    """Rozkoukané a zhlédnuté se zapisují už za běhu, ne až po zastavení.

    Po `MIN_RESUME` s přehrávání jde titul hned do Pokračovat ve sledování a pozice
    se každých `SAVE_EVERY` s přepíše, od `WATCHED_PCT` je zhlédnutý a z rozkoukaných
    zmizí. Čekat na `onPlayBackStopped` nestačí: když se pustí jiný film bez
    zastavení toho prvního, Kodi pro ten první nic nepošle a jeho pozice by se ztratila.
    """

    def __init__(self, store, stats):
        super().__init__()
        self.store = store
        self.stats = stats
        self.sw = None   # SyncWatchManager — události přehrávače jdou i do skupiny
        self.reset()

    def reset(self):
        self.item = None
        self.position = 0.0
        self.total = 0.0
        self.started = 0.0
        self.saved = 0.0      # kdy se naposledy zapsala pozice, 0 = ještě ne
        self.saved_pos = -1.0  # jaká pozice se zapsala naposledy
        self.done = False     # už označené jako zhlédnuté
        self.removed = False  # uživatel titul během přehrávání odebral z Pokračovat

    def onAVStarted(self):
        # předchozí titul uzavřít dřív, než ho přepíše nový
        self.finish()
        raw = xbmcgui.Window(10000).getProperty(PROP)
        if not raw:
            self.sw_event("started", item={})
            return
        try:
            item = json.loads(raw)
        except ValueError:
            return
        xbmcgui.Window(10000).clearProperty(PROP)
        self.item, self.started = item, time.time()
        try:
            total = self.getTotalTime()
        except RuntimeError:
            total = 0
        self.sw_event("started", item={"replay": item.get("replay"), "title": item.get("title") or "",
                                       "total": total})
        log(f"sleduji {item.get('id')}")
        self.trakt_scrobble("start", 0)
        threading.Thread(target=self.apply_tracks, args=(item,), name="nokturno-tracks", daemon=True).start()

    def apply_tracks(self, item):
        """Zvuk a titulky podle preferovaného jazyka (Nastavení → Přehrávání).

        Kodi při startu vezme výchozí stopu kontejneru, u přebalených filmů často
        anglickou, i když soubor má český dabing. Tady se jednou po startu přepne
        na preferovaný jazyk; titulky se zapnou, jen když ten jazyk ve zvuku chybí
        (nebo vždy, podle nastavení), a když preferovaný jazyk hraje, vypnou se
        úplně, i vynucené. Rozhoduje `tracks.pick_audio`/`pick_subtitle`
        v jádru; co si pak uživatel přepne sám, už se nepřepisuje."""
        time.sleep(TRACKS_DELAY)
        if self.item is not item or not self.isPlayingVideo():
            return
        addon = fresh_addon()
        if addon is None:
            return
        try:
            pref = PREF_LANGS[int(addon.getSetting("pref_lang") or 0)]
            subs_mode = int(addon.getSetting("auto_subs") or SUBS_WHEN_NEEDED)
        except (ValueError, IndexError):
            return
        auto_audio = addon.getSetting("auto_audio") != "false"
        if not pref:
            return
        player = rpc("Player.GetActivePlayers") or []
        player_id = next((p.get("playerid") for p in player if p.get("type") == "video"), None)
        if player_id is None:
            return
        props = rpc("Player.GetProperties", playerid=player_id, properties=[
            "audiostreams", "currentaudiostream", "subtitles", "currentsubtitle", "subtitleenabled"]) or {}
        audio = props.get("audiostreams") or []
        index, audio_ok = pick_audio(audio, props.get("currentaudiostream"), pref, item.get("stream_langs"))
        if index is not None and not auto_audio:
            # zvuk nechat, ale titulky se musí rozhodovat podle toho, co doopravdy hraje
            index, audio_ok = None, track_lang(props.get("currentaudiostream") or {}) == pref
        if index is not None:
            rpc("Player.SetAudioStream", playerid=player_id, stream=index)
            log(f"zvuk → stopa {index} ({pref})")
        action, sub_index = pick_subtitle(props.get("subtitles") or [], pref, audio_ok, subs_mode)
        current = props.get("currentsubtitle") or {}
        enabled = bool(props.get("subtitleenabled"))
        if action == "on" and not (enabled and current.get("index") == sub_index):
            rpc("Player.SetSubtitle", playerid=player_id, subtitle=sub_index, enable=True)
            log(f"titulky → stopa {sub_index} (zvuk v {pref}: {audio_ok})")
        elif action == "off" and enabled:
            rpc("Player.SetSubtitle", playerid=player_id, subtitle="off")
            log(f"titulky vypnuty (zvuk v {pref})")

    def tick(self):
        if not self.item or not self.isPlayingVideo():
            return
        try:
            self.position = self.getTime()
            self.total = self.getTotalTime()
        except RuntimeError:
            return
        self.checkpoint()

    def progress(self):
        return (self.position / self.total * 100) if self.total > 0 else 0

    def watched_now(self):
        return self.total > 0 and self.position / self.total >= WATCHED_PCT

    def mark_watched(self):
        if self.done:
            return
        self.store.set_watched(self.item.get("id"), True)
        self.done = True
        log(f"zhlédnuto {self.item.get('id')}")
        xbmcgui.Window(10000).setProperty(SYNC_PROP, "1")   # zhlédnuto/pozice → do HA hned

    def save_resume(self):
        item_id = self.item.get("id")
        first = not self.saved
        entry = self.store.load("watched", {}).get(str(item_id)) or {}
        if not first and not float(entry.get("resume") or 0) and int(entry.get("ts") or 0) >= int(self.saved):
            # od posledního zápisu titul někdo odebral z Pokračovat ve sledování
            # (kontextové menu, karta HA, jiné Kodi přes sync) — ta volba platí,
            # dokud se titul nepustí znovu
            self.removed = True
            log(f"odebráno z rozkoukaných během přehrávání, dál nezapisuji {item_id}")
            return
        if first and entry.get("playcount"):
            # znovu puštěný zhlédnutý titul — rozkoukané ho se značkou zhlédnuto nevypíšou
            self.store.set_watched(item_id, False)
        # vnitřní reference streamu (viz mark_playing v default.py) — bere se jen při prvním
        # zápisu rozkoukanosti, další zápisy (pauza a pokračování ve stejném přehrávání) by
        # ji jen zbytečně přepisovaly stejnou hodnotou
        stream_url = self.item.get("stream_url") if first else None
        self.store.set_resume(item_id, self.position, self.total,
                              stream_url=stream_url, stream_subs=self.item.get("stream_subs"))
        self.saved = time.time()
        self.saved_pos = self.position
        if first:
            log(f"rozkoukáno {item_id} @ {int(self.position)} s")
            xbmcgui.Window(10000).setProperty(SYNC_PROP, "1")

    def checkpoint(self):
        if self.done:
            return
        if self.watched_now():
            self.mark_watched()
        elif (not self.removed and self.position >= MIN_RESUME and time.time() - self.saved >= SAVE_EVERY
              and abs(self.position - self.saved_pos) >= 1):
            # pozastavené video pozici nemění — zapisovat ji znovu by jen posunulo čas
            # záznamu, a ten pak přebil odebrání z Pokračovat (Spasitel 2026-09-13:
            # pauza v Kodi, odebrání se každých 30 s vrátilo a sync ho poslal do HA)
            self.save_resume()

    def trakt_scrobble(self, action, progress):
        trakt = get_trakt(self.store)
        if not trakt or not self.item:
            return
        base, season, episode = split_key(self.item.get("id"))
        try:
            trakt.scrobble(action, base, progress, season, episode)
        except TraktError as e:
            log(f"trakt {action}: {e}", xbmc.LOGWARNING)

    def finish(self):
        if not self.item:
            return
        item_id = self.item.get("id")
        if self.done or self.watched_now():
            self.mark_watched()
        elif self.position >= MIN_RESUME and not self.removed:
            self.save_resume()
        self.trakt_scrobble("stop", self.progress())
        if split_key(item_id)[1] is not None:
            prefetch_next_later()
        xbmcgui.Window(10000).setProperty(SYNC_PROP, "1")
        self.reset()

    def sw_event(self, kind, **kw):
        if self.sw is not None:
            self.sw.event(kind, **kw)

    def onPlayBackPaused(self):
        self.sw_event("paused")

    def onPlayBackResumed(self):
        self.sw_event("resumed")

    def onPlayBackSeek(self, time_ms, offset_ms):
        self.sw_event("seek", pos=time_ms / 1000.0)

    def onPlayBackSeekChapter(self, chapter):
        self.sw_event("seek")

    def onPlayBackStopped(self):
        self.sw_event("stopped")
        # opožděné zastavení předchozího souboru po startu nového nesmí ukončit sledování nového
        if self.item and time.time() - self.started < 5 and self.isPlayingVideo():
            return
        self.finish()

    def onPlayBackEnded(self):
        self.sw_event("stopped")
        # na konci videa getTime už nemusí jít – bereme celou délku
        if self.item and self.total > 0:
            self.position = self.total
        self.finish()

    def onPlayBackError(self):
        self.reset()


# --- SyncWatch: společné sledování -----------------------------------------------------

SW_PROP = "nokturno.sw"             # stejný literál jako v default.py — stav skupiny pro okna pluginu
SW_REJOIN_PROP = "nokturno.sw.rejoin"   # plugin: člen se chce vrátit do filmu skupiny
SW_WATCH = 1.0                      # s — jak často se správce dívá, jestli je zařízení ve skupině
# kód hlášky z jádra → řetězec (šablona s %(who)s a spol., záloha je český text z jádra)
SW_NOTICE_IDS = {
    "paused_all": 30840, "paused": 30841, "played": 30842, "seeked": 30843, "buffering": 30844,
    "stopped": 30845, "loading": 30846, "waiting_others": 30847, "started_without": 30848,
    "load_failed": 30849, "other_version": 30850, "detached": 30851, "not_shareable": 30852, "left": 30853,
}


class SwPlayer(object):
    """Přehrávač Kodi tak, jak ho chce `syncwatch.Coordinator`. `Player.pause()` v Kodi
    pauzu přepíná, proto se pauza i pokračování dělají jen ze správného stavu."""

    def __init__(self):
        self.p = xbmc.Player()

    def position(self):
        try:
            return self.p.getTime() if self.p.isPlayingVideo() else None
        except RuntimeError:
            return None

    def playing(self):
        return self.p.isPlayingVideo() and not xbmc.getCondVisibility("Player.Paused")

    def caching(self):
        return xbmc.getCondVisibility("Player.Caching")

    def pause(self):
        if self.playing():
            self.p.pause()

    def resume(self):
        if self.p.isPlayingVideo() and not self.playing():
            self.p.pause()

    def seek(self, pos):
        if self.p.isPlayingVideo():
            self.p.seekTime(max(0.0, float(pos)))

    def load(self, url):
        log("SyncWatch: spouštím stream od vedoucího")
        xbmc.executebuiltin("PlayMedia(%s)" % url)

    def stop(self):
        if self.p.isPlaying():
            self.p.stop()


class SyncWatchManager(threading.Thread):
    """Drží běh skupiny (`syncwatch.Runtime`), dokud je v profilu `syncwatch.json`
    s tokenem. Plugin ho jen zapisuje a maže; tady se podle něj skupina spustí,
    ukončí, nebo po restartu Kodi znovu naváže."""

    def __init__(self, store, monitor):
        super().__init__(name="nokturno-syncwatch", daemon=True)
        self.store = store
        self.monitor = monitor
        self.runtime = None
        self.token = ""

    def event(self, kind, **kw):
        """Z callbacků `Player` — jen když skupina běží."""
        rt = self.runtime
        if rt is not None and rt.alive():
            rt.event(kind, **kw)

    def run(self):
        while not self.monitor.abortRequested():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 – správce nesmí skončit kvůli jedné chybě
                log(f"SyncWatch: {e}", xbmc.LOGWARNING)
            if self.monitor.waitForAbort(SW_WATCH):
                break
        if self.runtime is not None:
            # bez `leave` — po restartu Kodi se do skupiny naváže znovu (vedoucího server
            # drží 10 minut, člena 5)
            self.runtime.stop()

    def tick(self):
        session = self.store.load("syncwatch", {}) or {}
        token = session.get("token") or ""
        if self.runtime is not None and (token != self.token or not self.runtime.alive()):
            self.runtime.stop()
            self.runtime = None
            if not token:
                xbmcgui.Window(10000).clearProperty(SW_PROP)
        if token and self.runtime is None and token == session.get("token"):
            self.join_group(session)
        home = xbmcgui.Window(10000)
        if home.getProperty(SW_REJOIN_PROP):
            home.clearProperty(SW_REJOIN_PROP)
            self.event("rejoin")

    def join_group(self, session):
        import syncwatch
        try:
            client = syncwatch.Client(session["code"], token=session["token"])
        except (syncwatch.SyncWatchError, KeyError) as e:
            log(f"SyncWatch: neplatná skupina v profilu ({e}), zahazuji", xbmc.LOGWARNING)
            self.store.save("syncwatch", {})
            return
        client.mid = int(session.get("mid") or 0)
        token = session["token"]

        def closed(reason):
            # jen když skupinu mezitím plugin nenahradil jinou
            if (self.store.load("syncwatch", {}) or {}).get("token") == token:
                self.store.save("syncwatch", {})
            xbmcgui.Window(10000).setProperty(SW_PROP, json.dumps({"closed": reason or L(30819, "SyncWatch ukončen")}))
            xbmcgui.Dialog().notification(L(30800, "SyncWatch"),
                                          reason or L(30819, "SyncWatch ukončen"), xbmcgui.NOTIFICATION_WARNING, 6000)
            log(f"SyncWatch: skupina skončila ({reason})")

        def notify(code, **kw):
            text = syncwatch.notice_text(code, L(SW_NOTICE_IDS.get(code, 0), "") or None, **kw)
            xbmcgui.Dialog().notification(L(30800, "SyncWatch"), text, ADDON.getAddonInfo("icon"), 3500)

        def status(info):
            xbmcgui.Window(10000).setProperty(SW_PROP, json.dumps(info))

        self.runtime = syncwatch.Runtime(client, SwPlayer(), session.get("name") or "Kodi",
                                         bool(session.get("leader")), notify=notify, status=status,
                                         on_closed=closed, should_stop=QUITTING.is_set)
        self.token = token
        self.runtime.start()
        log("SyncWatch: skupina běží (%s)" % ("vedoucí" if session.get("leader") else "člen"))


# --- stahování ----------------------------------------------------------------------

def safe_filename(name):
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name or "").strip() or "video"
    return name[:180]


def resolve_internal(url, store):
    """Vnitřní odkaz z fronty (`ws:`, `hs:`, `st:`, `fs:`, `cz:`, `pt:`, `streamuj:`, `dav:`) → (odkaz ke stažení, hlavičky).

    Rozklíčovává se až tady, ve chvíli stahování: podepsané odkazy zdrojů platí jen
    pár hodin a fronta je sekvenční — s hotovým odkazem uloženým při zařazení končil
    třetí soubor ve frontě nebo cokoli po restartu Kodi chybou. Nastavení se čte
    čerstvé (`fresh_addon`), účty jsou tytéž jako v pluginu."""
    addon = fresh_addon()
    s = (lambda key: (addon.getSetting(key) or "").strip()) if addon else (lambda key: "")
    if url.startswith("ws:"):
        api = WebshareApi(s("ws_username"), s("ws_password"),
                          token=xbmcgui.Window(10000).getProperty("nokturno.ws_token"), cache=store)
        link = api.file_link(url[3:])
        if api.token:
            xbmcgui.Window(10000).setProperty("nokturno.ws_token", api.token)
        return link, {}
    if url.startswith("hs:"):
        file_id, _sep, file_hash = url[3:].partition(":")
        return HellspyApi(cache=store).file_link(file_id, file_hash), {}
    if url.startswith("st:"):
        return SledujtetoApi(s("st_email"), addon.getSetting("st_password") if addon else "", cache=store).file_link(url[3:]), {}
    if url.startswith("fs:"):
        # soubor chce cookie z přihlášení — stahovač ji dostane v hlavičkách jako u úložiště
        return FastshareApi(s("fs_username"), addon.getSetting("fs_password") if addon else "", cache=store).request(url)
    if url.startswith("cz:"):
        # tokeny párování drží úložiště doplňku (sdílené s pluginem), `playback_url` hraje bez hlaviček
        return CztorApi(store, device_name=f"Nokturno ({xbmc.getInfoLabel('System.FriendlyName') or 'Kodi'})").resolve(url), {}
    if url.startswith("pt:"):
        # odkaz CDN je podepsaný a nedrží na IP, hlavičky nechce; s účtem Premium vyjde původní soubor
        return PrehrajtoApi(s("pt_email"), addon.getSetting("pt_password") if addon else "", cache=store).request(url)
    if url.startswith("streamuj:"):
        return SosacDirect(s("streamuj_username"), s("streamuj_password"), cache=store).resolve(url), {}
    if url.startswith("dav:"):
        slot, path = parse_ref(url)
        api = StorageApi(s(f"dav{slot}_url"), s(f"dav{slot}_username"), addon.getSetting(f"dav{slot}_password") if addon else "",
                         s(f"dav{slot}_name"), slot=slot, cache=store)
        return api.file_url(path), api.headers()
    return url, {}


class Downloader(threading.Thread):
    def __init__(self, store, monitor):
        super().__init__(daemon=True)
        self.store = store
        self.monitor = monitor

    def run(self):
        self.requeue_running()
        while not self.monitor.abortRequested():
            job = next((d for d in self.store.downloads() if d.get("status") == "queued"), None)
            if job and terms_ok():
                self.download(job)
            if self.monitor.waitForAbort(POLL):
                break

    def requeue_running(self):
        """Po startu služby: co je v seznamu „stahuje se", stahovat nemůže — ten proces
        už neběží. Zpátky do fronty, `.part` zůstane ležet a naváže se na něj."""
        for job in self.store.downloads():
            if job.get("status") == "running":
                self.store.update_download(job["id"], status="queued")
                log(f"stahování {job.get('name', '')} pokračuje po restartu")

    @staticmethod
    def part_size(tmp, expected):
        """Kolik už je staženo v `.part`, nebo 0 (nic k navázání).

        Hotový nebo delší soubor než očekávaný je podezřelý (jiná verze souboru pod
        stejným jménem, přerušený zápis) — raději od začátku než slepit dva různé."""
        try:
            mame = os.path.getsize(tmp)
        except OSError:
            return 0
        if mame <= 0 or (expected and mame >= expected):
            return 0
        return mame

    def download(self, job):
        dl_id, url, dest = job["id"], job["url"], job["dest"]
        self.store.update_download(dl_id, status="running")
        xbmcgui.Dialog().notification(L(30000), Lf(30073, job.get("name", "")), ADDON.getAddonInfo("icon"), 3000)
        tmp = dest + ".part"
        # Složka ze sítě (smb://, nfs://…) je cesta Kodi, ne souborového systému —
        # `open`/`os.*` s ní skončí „Read-only file system: 'smb:'" (do 7.9.5).
        vfs = "://" in dest
        try:
            if vfs:
                xbmcvfs.mkdirs(os.path.dirname(dest))
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
            link, headers = resolve_internal(url, self.store)
            if not link:
                raise RuntimeError("zdroj odkaz nevydal")
            # Navázání na rozdělané stahování: film mívá desítky GB a přerušení
            # (restart Kodi, výpadek sítě, vypnutý box) znamenalo do 6.2.2 začít
            # od nuly. `.part` proto po chybě zůstává ležet.
            # ponytail: `xbmcvfs.File` neumí zápis na konec, síťový cíl se po přerušení
            # stahuje znovu od nuly; navázání tam by chtělo `.part` lokálně a kopii na konci
            od = 0 if vfs else self.part_size(tmp, int(job.get("size") or 0))
            if od:
                headers = {**headers, "Range": f"bytes={od}-"}
            req = urllib.request.Request(link, headers={"User-Agent": "Kodi plugin.video.nokturno", **headers})
            with urllib.request.urlopen(req, timeout=60) as resp:
                # 206 = server navázání přijal; cokoli jiného (200, nebo zdroj Range
                # neumí) znamená, že posílá soubor od začátku — pak se přepisuje
                navazuje = od > 0 and resp.getcode() == 206
                size = int(resp.headers.get("Content-Length") or 0) + (od if navazuje else 0)
                done, last = (od if navazuje else 0), 0.0
                if od and not navazuje:
                    log(f"zdroj navázání neumí, stahuji {job.get('name', '')} znovu", xbmc.LOGWARNING)
                out = xbmcvfs.File(tmp, "w") if vfs else open(tmp, "ab" if navazuje else "wb")
                try:
                    # `finally` místo `with`: při chybě se soubor musí zavřít (a tím
                    # dopsat), ale NE smazat — na to, co je stažené, se příště naváže
                    self.store.update_download(dl_id, size=size, done=done)
                    while True:
                        if self.monitor.abortRequested():
                            raise RuntimeError("abort")
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        if out.write(chunk) is False:   # xbmcvfs chybu nehází, vrací False
                            raise OSError("zápis do cílové složky selhal")
                        done += len(chunk)
                        if time.time() - last > 2:
                            last = time.time()
                            current = next((d for d in self.store.downloads() if d.get("id") == dl_id), None)
                            if not current or current.get("status") == "cancel":
                                raise RuntimeError("cancel")
                            self.store.update_download(dl_id, done=done)
                finally:
                    out.close()
            if vfs:
                xbmcvfs.delete(dest)    # rename přes existující soubor na SMB neprojde
                if not xbmcvfs.rename(tmp, dest):
                    raise OSError("soubor nejde přejmenovat v cílové složce")
            else:
                os.replace(tmp, dest)
            self.store.update_download(dl_id, status="done", done=done, size=size or done)
            xbmcgui.Dialog().notification(L(30000), Lf(30074, job.get("name", "")), ADDON.getAddonInfo("icon"), 4000)
            log(f"staženo {dest}")
        except Exception as e:  # noqa: BLE001
            if str(e) == "cancel":
                xbmcvfs.delete(tmp)     # zrušeno uživatelem — rozdělané smazat
                self.store.remove_download(dl_id)
                log(f"stahování zrušeno {dest}")
            elif str(e) == "abort":
                # Kodi končí: `.part` zůstane, po startu se naváže (`requeue_running`)
                self.store.update_download(dl_id, status="running")
                log(f"stahování {job.get('name', '')} přerušeno vypnutím Kodi")
            else:
                # chyba sítě nebo zdroje — `.part` zůstane ležet, „Zkusit znovu" naváže
                self.store.update_download(dl_id, status="error", error=str(e)[:200])
                log(f"stahování selhalo {dest}: {e}", xbmc.LOGERROR)
                xbmcgui.Dialog().notification(L(30000), Lf(30075, job.get("name", "")), xbmcgui.NOTIFICATION_ERROR, 5000)


# --- synchronizace přes Home Assistant ----------------------------------------------

class AccountsChecker:
    """Obnoví stav účtů napříč zdroji a upozorní na končící předplatné WebShare.

    Obnova jde přes plugin (`action=accounts_refresh`) stejnou cestou jako zahřívání
    katalogů — služba vlastní jádro nemá a plugin už všechny klienty zdrojů má
    postavené. Výsledek si plugin uloží do `accounts.json`, odkud ho menu čte bez
    jediného dotazu na síť.

    Nahradila dřívější `SubscriptionChecker`, která se na totéž ptala WebShare sama:
    kontrola předplatného je teď jen jeden ze sedmi zdrojů v jedné obnově, takže
    dotazů na WebShare nepřibylo. Upozornění se ukáže nejvýš jednou za kalendářní
    den (`last_warned`), jinak by vyskakovalo při každém startu Kodi.
    """

    def __init__(self, store):
        self.store = store
        self.next = time.time() + ACCOUNTS_DELAY

    def tick(self):
        if self.requested():
            self.next = 0      # „Ověřit zdroje" v menu — obnovit hned, ne až za šest hodin
        if time.time() < self.next:
            return
        self.next = time.time() + ACCOUNTS_EVERY
        if xbmc.Player().isPlaying():
            self.next = time.time() + WARM_RETRY   # obnova zdrojů počká, až se dokouká
            return

        def run():
            try:
                rpc_directory("plugin://plugin.video.nokturno/?action=accounts_refresh")
            except Exception as e:  # noqa: BLE001 – stav účtů nesmí shodit službu
                log(f"obnova stavu zdrojů: {e}", xbmc.LOGWARNING)
                return
            if self.unreachable():
                # bez sítě (na mobilu typicky hned po startu Kodi) by v menu stálo
                # „neodpovídá" celých šest hodin, i kdyby se síť vrátila za minutu
                self.next = min(self.next, time.time() + ACCOUNTS_RETRY)
            self.warn_subscription()

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def requested():
        """Požádal plugin o obnovu hned? Vlastnost okna se přečte a smaže."""
        win = xbmcgui.Window(10000)
        if win.getProperty(ACCOUNTS_TRIGGER_PROP) != "1":
            return False
        win.clearProperty(ACCOUNTS_TRIGGER_PROP)
        return True

    def unreachable(self):
        """Selhal aspoň jeden zdroj tím, že se k němu nešlo dostat — nebo obnova
        skončila úplně bez sítě (jádro tehdy stav nechá a zapíše jen značku)?"""
        saved = self.store.reload(accounts_lib.STORE, {}) or {}
        if any((rec or {}).get("code") == "unreachable" for rec in saved.values()):
            return True
        znacka = (self.store.reload(accounts_lib.OFFLINE, {}) or {}).get("ts", 0)
        return bool(znacka) and time.time() - float(znacka) < accounts_lib.OFFLINE_TTL

    def warn_subscription(self):
        """Upozornění na končící nebo proběhlé předplatné WebShare — jediný stav,
        který si zaslouží vyskočit sám; zbytek uživatel najde v menu."""
        addon = fresh_addon()
        if addon is None or addon.getSetting("ws_enabled") != "true":
            return
        try:
            warn_days = int(addon.getSetting("sub_warn_days") or 5)
        except ValueError:
            warn_days = 5
        saved = self.store.reload(accounts_lib.STORE, {}) or {}
        rec = saved.get("webshare") or {}
        days = int((rec.get("detail") or {}).get("days") or 0)
        if rec.get("code") == "expires_soon" and days > warn_days:
            return      # obnova počítala s jiným prahem, než je teď v nastavení
        if rec.get("code") not in ("expires_soon", "expired", "free"):
            return
        today = time.strftime("%Y-%m-%d")
        state = self.store.reload(SUB_STATE, {})
        if state.get("last_warned") == today:
            return
        self.store.save(SUB_STATE, {"last_warned": today})
        msg = Lf(30236, days) if rec["code"] == "expires_soon" else L(30237, "WebShare předplatné vypršelo.")
        xbmcgui.Dialog().notification(L(30000), msg, xbmcgui.NOTIFICATION_WARNING, 8000)


class WatchChecker:
    """Hlídané: kontrola nových dílů a titulů bez streamu, a oznámení.

    Kontrolu dělá plugin (`action=watch_check`) stejnou cestou jako obnovu stavu zdrojů —
    služba vlastní jádro nemá. Budí se jen tehdy, když něčemu vypršel interval
    (`watch.anything_due`, bez sítě), a nikdy při přehrávání ani bez sítě: na mobilu
    Android utne Kodi na pozadí síť (viz 6.6.2) a kontrola by jen vyrobila chyby.

    Oznámení se počítají zvlášť (`watch.pending_notices`), takže se ozve i nový díl,
    který našel Home Assistant nebo jiné Kodi a přišel synchronizací.
    """

    def __init__(self, store):
        self.store = store
        self.next = time.time() + WATCH_DELAY
        self.next_notice = time.time() + WATCH_DELAY
        self.lock = threading.Lock()

    def tick(self):
        now = time.time()
        if now >= self.next_notice:
            self.next_notice = now + WATCH_NOTICE_EVERY
            self.announce()
        trigger = self.requested()
        if not trigger and now < self.next:
            return
        self.next = now + WATCH_EVERY
        if not trigger:
            if xbmc.Player().isPlaying() or self.offline():
                return
            last = int((self.store.reload("watch_state", {}) or {}).get("last_run") or 0)
            if not watch_lib.anything_due(self.store) and now - last < WATCH_DAILY:
                return
        if not self.lock.acquire(blocking=False):
            return

        def run():
            try:
                url = "plugin://plugin.video.nokturno/?action=watch_check"
                rpc_directory(url + ("&force=1" if trigger == "force" else ""))
            except Exception as e:  # noqa: BLE001 – kontrola nesmí shodit službu
                log(f"kontrola Hlídaných: {e}", xbmc.LOGWARNING)
            finally:
                self.lock.release()
            self.announce()

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def requested():
        """„Zkontrolovat teď“ nebo nově přidaný titul — vlastnost okna se přečte a smaže."""
        win = xbmcgui.Window(10000)
        value = win.getProperty(WATCH_TRIGGER_PROP)
        if value:
            win.clearProperty(WATCH_TRIGGER_PROP)
        return value

    def offline(self):
        znacka = (self.store.reload(accounts_lib.OFFLINE, {}) or {}).get("ts", 0)
        return bool(znacka) and time.time() - float(znacka) < accounts_lib.OFFLINE_TTL

    def announce(self):
        """Toasty, ne modál — služba nesmí nic blokovat. Při přehrávání počkají
        (nic se neoznačí), ať nevyskakují přes film."""
        if xbmc.Player().isPlaying():
            return
        try:
            notices = watch_lib.pending_notices(self.store)
        except Exception as e:  # noqa: BLE001
            log(f"oznámení Hlídaných: {e}", xbmc.LOGWARNING)
            return
        icon = ADDON.getAddonInfo("icon")
        for n in notices[:WATCH_NOTICE_MAX]:
            if n["kind"] == "episode":
                text = f"{n['title']} – {int(n['season'] or 0)}x{int(n['episode'] or 0):02d} {n['episode_title']}"
                xbmcgui.Dialog().notification(L(30913, "Nový díl ke sledování"), text.strip(), icon, 8000)
            else:
                year = f" ({n['year']})" if n.get("year") else ""
                sid = 30915 if n["kind"] == "more" else 30914
                xbmcgui.Dialog().notification(L(30000, "Nokturno"), Lf(sid, n["title"] + year), icon, 8000)
            time.sleep(0.2)


SUB_STATE = "substate"   # substate.json v profilu: {"last_warned": "YYYY-MM-DD"}


class Syncer:
    """Jednou za SYNC_EVERY, nebo hned když si plugin/přehrávač řekne (vlastnost okna).
    Síť běží ve vlastním vlákně, aby nezdržela sledování pozice přehrávání."""

    def __init__(self, store):
        self.store = store
        self.next = time.time() + SYNC_FIRST   # první výměna krátce po startu
        self.lock = threading.Lock()
        # poslední nahlášený důvod selhání pro každé středisko — viz `_log_vysledek`
        self.last_fail = {}

    # nastavení a účty umí jen relay (viz `default.sync_circles`) a jsou výchozím
    # stavem vypnuté — sdílení přihlášení má být vědomé rozhodnutí
    CIRCLES = {"watched": "sync_watched", "favourites": "sync_favourites",
               "history": "sync_history", "watchlist": "sync_watchlist",
               "settings": "sync_settings",
               "accounts": "sync_accounts"}
    RELAY_ONLY = ("settings", "accounts")

    @classmethod
    def _circles(cls, addon, relay=False):
        out = []
        for okruh, klic in cls.CIRCLES.items():
            if okruh in cls.RELAY_ONLY and (not relay or addon.getSetting(klic) != "true"):
                continue
            if okruh not in cls.RELAY_ONLY and addon.getSetting(klic) == "false":
                continue
            out.append(okruh)
        return tuple(out)

    @staticmethod
    def _settings_values(addon, circles):
        if not any(o in circles for o in Syncer.RELAY_ONLY):
            return None
        return kodi_settings.values(addon, xbmcvfs.translatePath(addon.getAddonInfo("path")))

    @staticmethod
    def _write_settings(addon, changes):
        zapsano = kodi_settings.apply(addon, changes)
        if not zapsano:
            return
        if any(k.startswith("ws_") for k in zapsano):
            xbmcgui.Window(10000).clearProperty("nokturno.ws_token")
        log("synchronizace přepsala nastavení: " + ", ".join(sorted(zapsano)), xbmc.LOGINFO)

    def _log_vysledek(self, kde, ok, pushed, pulled, why):
        """Warning jen při **změně** stavu, opakované stejné selhání do ladicího logu.

        Kolo běží po SYNC_EVERY, takže nedostupné středisko jinak zapíše warning
        pořád dokola (u jedné instalace 288 řádků `sync (HA) neproběhl: <urlopen
        error [Errno 7] No address associated with hostname>` denně, 2026-09-22).
        Informace, že něco nejde, je v logu potřeba jednou — ne v každém kole.
        """
        znacka = f"sync ({kde})" if kde else "sync"
        if ok:
            if self.last_fail.pop(kde, None):
                log(f"{znacka}: znovu funguje", xbmc.LOGINFO)
            log(f"{znacka}: odesláno {pushed}, přijato {pulled}", xbmc.LOGINFO)
            return
        nove = self.last_fail.get(kde) != why
        self.last_fail[kde] = why
        log(f"{znacka} neproběhl: {why}",
            xbmc.LOGWARNING if nove else xbmc.LOGDEBUG)

    def tick(self, force=False):
        addon = fresh_addon()
        if addon is None or addon.getSetting("sync_enabled") != "true":
            return
        # 0 = Home Assistant, 1 = dashboard (viz `default.sync_targets`)
        rezim = addon.getSetting("sync_mode")
        relay = rezim == "1"
        code = addon.getSetting("sync_code").strip() if relay else ""
        url, key = "", ""
        if not relay:
            url, key = addon.getSetting("sync_url").strip(), addon.getSetting("sync_key").strip()
        ha = bool(url and key)
        if not ha and not code:
            return
        circles = self._circles(addon, relay)   # zhlédnuto a spol. platí pro obě střediska
        asked = xbmcgui.Window(10000).getProperty(SYNC_PROP)
        if not force and not asked and time.time() < self.next:
            return
        xbmcgui.Window(10000).clearProperty(SYNC_PROP)
        self.next = time.time() + SYNC_EVERY
        if not self.lock.acquire(blocking=False):
            return

        def run():
            try:
                jmeno = xbmc.getInfoLabel("System.FriendlyName")
                if ha:
                    ok, pushed, pulled, why = sync_once(
                        self.store, url, key, jmeno,
                        circles=tuple(o for o in circles if o not in self.RELAY_ONLY))
                    self._log_vysledek("HA", ok, pushed, pulled, why)
                if code:
                    ok, pushed, pulled, why = syncbox.sync_once(
                        self.store, code, circles=circles, name=jmeno,
                        settings=self._settings_values(addon, circles),
                        on_settings=lambda zmeny: self._write_settings(addon, zmeny))
                    self._log_vysledek("", ok, pushed, pulled, why)
            finally:
                self.lock.release()
        threading.Thread(target=run, daemon=True).start()


class KodiMarks:
    """„Označit jako zhlédnuté“ ze skinu Kodi → evidence, HA a Trakt (viz `kodi_marks`).

    Plugin změnu převezme sám, když Kodi po označení obnoví výpis. Tohle je pojistka
    pro chvíle, kdy se výpis neobnoví (widget na domovské obrazovce, jiné okno) —
    jednou za `KODI_MARKS_EVERY` jeden dotaz do videodatabáze, bez sítě. Zároveň zrcadlí
    do Kodi změny, které přišly odjinud (menu Nokturna v jiném okně, synchronizace z HA)."""

    def __init__(self, store):
        self.store = store
        self.next = time.time() + KODI_MARKS_EVERY

    def tick(self):
        if time.time() < self.next:
            return
        self.next = time.time() + KODI_MARKS_EVERY
        try:
            kodi_marks.collect(self.store, xbmcvfs.translatePath("special://database/"), apply=self.apply,
                               write=kodi_marks.rpc_writer(xbmc.executeJSONRPC))
        except Exception as e:  # noqa: BLE001 – cizí databáze, cokoli neočekávaného jen do logu
            log(f"zhlédnuto z Kodi: {e}", xbmc.LOGWARNING)

    def apply(self, changes):
        changed = [(key, watched) for key, watched in changes.items()
                   if bool(self.store.playcount(key)) != watched]
        if not changed:
            return
        trakt = get_trakt(self.store)
        for key, watched in changed:
            log(f"zhlédnuto z Kodi: {key} → {'ano' if watched else 'ne'}")
            self.store.set_watched(key, watched)
            if trakt:
                base, season, episode = split_key(key)
                try:
                    (trakt.mark_watched if watched else trakt.unmark_watched)(base, season, episode)
                except TraktError as e:
                    log(f"trakt: {e}", xbmc.LOGWARNING)
        xbmcgui.Window(10000).setProperty(SYNC_PROP, "1")


# --- zahřívání cache -----------------------------------------------------------------
# Domovská obrazovka (widgety) i hlavní menu čtou katalogy z cache doplňku. Když ji
# služba potichu obnoví dřív, než vyprší, uživatel čeká na síť jen výjimečně.
# Jde to přes běžný výpis pluginu (Files.GetDirectory), ne přes API napřímo — projde
# tím i doplnění popisů u Sosáče a nic se nepočítá do statistik (to dělá jen
# hlavní menu a seznam streamů). Streamy dalšího dílu má vlastní akci `prefetch`.

def warm_urls():
    """Tytéž výpisy, které otevírá menu Filmy / Seriály (`browse_menu()` v default.py).

    Dřív se zahřívalo `src=sosac` a Luna, jenže menu od 3.1.6 bere seznamy z TMDB
    (je-li klíč) a z veřejného katalogu `sosac_db` — zahřívání tak minulo všechno,
    co uživatel otevírá, a první otevření nově přidaných trvalo na Office 12 s."""
    base = "plugin://plugin.video.nokturno/?action=catalog&src={src}&type={t}&catalog={c}"
    # čerstvá instance: modulový ADDON z doby startu služby nevidí změny nastavení —
    # po zadání klíče TMDB se dál zahřívala Luna
    addon = fresh_addon()
    if addon is None:
        return []
    urls = []
    for t in ("movie", "series"):
        if addon.getSetting("tmdb_api_key").strip():
            for c in ("popular", "top_rated"):
                urls.append(base.format(src="tmdb", t=t, c=c))
        # `luna_enabled` je ve výchozím stavu zapnuté i bez vyplněného tokenu — bez něj
        # ale `get_luna()` vrátí None a zahřívaný katalog skončí chybou „Není nastaven
        # žádný zdroj" (čtyři řádky v kodi.logu při každém warm-upu, nic zahřátého)
        elif addon.getSetting("luna_enabled") != "false" and addon.getSetting("token").strip():
            urls.append(base.format(src="luna", t=t, c=f"tmdb.top_{t}"))
            urls.append(base.format(src="luna", t=t, c=f"tmdb.top_rated_{t}"))
        # vlastní žebříček (dashboard) — bez ohledu na TMDB/Lunu, funguje vždycky stejně
        urls.append(base.format(src="trend", t=t, c=TREND_CATALOG_ID))
    return urls + foryou_warm_urls()


def foryou_warm_urls():
    """„Pro tebe" — doporučení k naposledy zhlédnutým (`default.list_foryou`).

    Patří do `warm_urls()` (2,5 h), ne mezi jazykové katalogy: je to jen TMDB, žádné
    hledání ve zdrojích. **Jen pro typ, který uživatel za posledních `FORYOU_SEEN_DAYS`
    dní otevřel** (`default.note_foryou_open`) — u instalace, která „Pro tebe" nikdy
    neotevřela, by šlo o dotazy na TMDB, které nikdo neuvidí. Totéž pravidlo jako
    u `lang_warm_urls()` níž (audit 2026-09-19, nález 12).

    Cache doporučení platí 24 h a přepočítá se, jen když by do dalšího kola zahřívání
    nevydržela (`default.FORYOU_REFRESH_AFTER`) nebo když uživatel mezitím něco
    dokoukal a změnily se vzory."""
    store = Store(PROFILE)
    seen = store.load(FORYOU_SEEN_KEY, {}) or {}
    ted = time.time()
    base = "plugin://plugin.video.nokturno/?action=foryou&type={t}"
    return [base.format(t=t) for t in ("movie", "series")
            if ted - (seen.get(t) or 0) < FORYOU_SEEN_DAYS * 86400]


def lang_warm_urls():
    """„Nově přidané s CZ dabingem/titulky" — od 2026-09-15 má vlastní 8h cache
    (`LANG_CATALOG_TTL` v default.py), zahřívá se proto zvlášť a řidčeji (viz
    `lang_warmer()`): živé ověřování streamů napříč zdroji je řádově dražší
    než ostatní katalogy v `warm_urls()`, běžet spolu s nimi by je zbytečně
    zdržovalo.

    Jen `want=dub` na typ (2026-09-15, druhé kolo): dabing i titulky se v jádru
    počítají v jednom průchodu se společnou cache (`lang_catalog:{ctype}`), takže
    zahřátí dabingu zadarmo zahřeje i titulky — druhá adresa by jen zbytečně
    čekala na zámek a přečetla to samé z cache.

    **Jen pro typ, který uživatel za posledních `LANG_SEEN_DAYS` dní otevřel**
    (`default.note_lang_catalog_open`). Tohle je nejdražší práce, kterou doplněk
    dělá sám od sebe — až 60 kandidátů krát všechny zapnuté zdroje, každých 6 h —
    a u instalace, která ten seznam nikdy neotevřela, je celá k ničemu. Právě tudy
    se doplněk dostal k blokaci HellSpy (audit 2026-09-19, nález 12). Kdo seznam
    otevře, dostane ho napoprvé přes „Klepni pro spuštění" a od té chvíle se
    zahřívá na pozadí jako dřív."""
    store = Store(PROFILE)
    seen = store.load(LANG_SEEN_KEY, {}) or {}
    ted = time.time()
    base = "plugin://plugin.video.nokturno/?action=lang_catalog&type={t}&want=dub"
    return [base.format(t=t) for t in ("movie", "series")
            if ted - (seen.get(t) or 0) < LANG_SEEN_DAYS * 86400]


def rpc(method, **params):
    """JSON-RPC Kodi zevnitř služby; výsledek, nebo None při chybě."""
    try:
        raw = xbmc.executeJSONRPC(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
        return json.loads(raw).get("result")
    except (ValueError, TypeError, RuntimeError) as e:
        log(f"{method}: {e}", xbmc.LOGWARNING)
        return None


def terms_ok():
    """Souhlas s podmínkami (přepínač `terms_ok`, první kategorie nastavení).

    Bez něj nesmí doplněk sáhnout na zdroje ani z pozadí. Plugin si brání sám
    (`ensure_terms()` v `main()`), ale z JSON-RPC by jen bliklo oznámení — zahřívání
    katalogů, prefetch a obnova stavu účtů by se o to pokoušely dál každých pár hodin.
    """
    addon = fresh_addon()
    return addon is not None and addon.getSetting("terms_ok") == "true"


def rpc_directory(url):
    """Jediná cesta služby do pluginu — proto tu sedí brána souhlasu."""
    if not terms_ok():
        return
    xbmc.executeJSONRPC(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "Files.GetDirectory",
                                    "params": {"directory": url, "media": "video"}}))


def warm_caches(monitor, what="all"):
    if not terms_ok():
        return
    try:
        if what in ("all", "catalogs"):
            # plugin během zahřívání cache API jen zapisuje: jinak by warm-up s TTL rovným
            # intervalu jen zjistil, že cache je ještě čerstvá, a nic neobnovil
            xbmcgui.Window(10000).setProperty(WARM_PROP, "1")
            try:
                for url in warm_urls():
                    if monitor.abortRequested():
                        return
                    rpc_directory(url)
            finally:
                xbmcgui.Window(10000).clearProperty(WARM_PROP)
        if what in ("all", "next"):
            rpc_directory("plugin://plugin.video.nokturno/?action=prefetch&kind=next")
        if what == "all":
            smazano = Store(PROFILE).prune_cache()   # prošlé soubory cache dřív ležely v profilu navždy
            if smazano:
                log(f"cache: smazáno {smazano} prošlých souborů")
        log(f"cache zahřáta ({what})")
    except Exception as e:  # noqa: BLE001 – zahřívání nesmí nikdy nic shodit
        log(f"zahřívání cache: {e}", xbmc.LOGWARNING)


def warmer(monitor):
    """Vlákno: první zahřátí chvíli po startu, pak každé tři hodiny; ne během přehrávání."""
    if monitor.waitForAbort(WARM_DELAY):
        return
    while not monitor.abortRequested():
        if xbmc.Player().isPlaying():
            if monitor.waitForAbort(WARM_RETRY):
                return
            continue
        warm_caches(monitor)
        if monitor.waitForAbort(WARM_EVERY):
            return


def warm_lang_caches(monitor):
    try:
        xbmcgui.Window(10000).setProperty(WARM_PROP, "1")
        try:
            for url in lang_warm_urls():
                if monitor.abortRequested():
                    return
                rpc_directory(url)
        finally:
            xbmcgui.Window(10000).clearProperty(WARM_PROP)
        log("cache dabingu/titulků zahřáta")
    except Exception as e:  # noqa: BLE001 – zahřívání nesmí nikdy nic shodit
        log(f"zahřívání cache dabingu/titulků: {e}", xbmc.LOGWARNING)


def lang_warmer(monitor):
    """Vlákno: „Nově přidané s CZ dabingem/titulky" má vlastní, delší interval
    (`LANG_WARM_EVERY`, pod `LANG_CATALOG_TTL`) — samostatně od `warmer()`, ať
    dražší živé ověřování streamů nezdržuje ostatní katalogy."""
    if monitor.waitForAbort(LANG_WARM_DELAY):
        return
    while not monitor.abortRequested():
        if xbmc.Player().isPlaying():
            if monitor.waitForAbort(WARM_RETRY):
                return
            continue
        warm_lang_caches(monitor)
        if monitor.waitForAbort(LANG_WARM_EVERY):
            return


def lang_trigger_watcher(monitor):
    """Vlákno: reaguje na klik uživatele na „Klepni pro spuštění" u „Nově přidané
    s CZ dabingem/titulky" (`lang_catalog_trigger()` v `default.py`), když data
    nejsou připravená a nic je zrovna nepočítá. Na rozdíl od `lang_warmer()`
    (pravidelně, ať uživatel nenarazí na živý přepočet) tohle čeká na výslovnou
    žádost a reaguje rychle (`LANG_TRIGGER_POLL`), ne až za `LANG_WARM_EVERY`.

    Samotný přepočet dělá stejná cesta jako zahřívání (`rpc_directory` na
    `action=lang_catalog`, `want=dub` stačí — dabing i titulky se počítají
    v jednom průchodu), jen pro ten jeden typ (film/seriál), co si uživatel
    vyžádal, ne pro oba. Po dokončení notifikace — uživatel mezitím odešel
    jinam, na místě nikdo nečeká."""
    win = xbmcgui.Window(10000)
    labels = {"movie": L(30012), "series": L(30013)}
    while not monitor.abortRequested():
        for ctype in ("movie", "series"):
            prop = f"{LANG_TRIGGER_PROP}:{ctype}"
            if win.getProperty(prop) != "1":
                continue
            win.clearProperty(prop)
            try:
                xbmcgui.Window(10000).setProperty(WARM_PROP, "1")
                try:
                    rpc_directory(f"plugin://plugin.video.nokturno/?action=lang_catalog&type={ctype}&want=dub")
                finally:
                    xbmcgui.Window(10000).clearProperty(WARM_PROP)
                xbmcgui.Dialog().notification(L(30000), Lf(30439, labels.get(ctype, ctype)),
                                              ADDON.getAddonInfo("icon"), 5000)
                log(f"lang_catalog:{ctype}: přepočet na žádost hotový")
            except Exception as e:  # noqa: BLE001 – žádost o přepočet nesmí shodit celou službu
                log(f"lang_catalog:{ctype}: přepočet na žádost selhal: {e}", xbmc.LOGWARNING)
        if monitor.waitForAbort(LANG_TRIGGER_POLL):
            return


class ServiceMonitor(xbmc.Monitor):
    """`xbmc.Monitor`, který o konci Kodi ví hned, ne až když se k němu Kodi dostane.

    Office 2026-09-16 (debug log): po `Application.Quit` Kodi nejdřív zastavuje síťové
    služby a čeká na rozběhnuté požadavky (i na `Files.GetDirectory` z JSON-RPC, který
    visí na pluginu), a teprve pak posílá skriptům stop. Plugin spuštěný zahříváním
    tak `abortRequested()` neuviděl vůbec (37 s, s čekajícím HTTP dotazem 202 s).
    Notifikace `System.OnQuit` ale chodí úplně na začátku vypínání — služba si ji
    zapamatuje (`QUITTING`) a nastaví vlastnost okna `QUIT_PROP`, kterou čte plugin
    (`default.should_stop`). Callbacky Monitoru běží ve vlákně, které ho vytvořilo, při
    jeho `waitForAbort` — tedy v hlavní smyčce `main()` (POLL), nejpozději do 100 ms."""

    def onNotification(self, sender, method, data):
        if method == "System.OnQuit":
            mark_quitting()

    def onScreensaverDeactivated(self):
        """Někdo si sedl k téhle TV — stáhnout, co mezitím přišlo z druhé.

        Periodické kolo běží po `SYNC_EVERY`, takže rozkoukanost z druhého Kodi
        by tu jinak mohla být i pět minut stará. Na CoreELEC navíc Kodi běží
        pořád (vypíná se jen televize), takže „start" nikdy nenastane a tohle je
        jediná chvíle, kdy se dá poznat, že se uživatel vrátil."""
        xbmcgui.Window(10000).setProperty(SYNC_PROP, "1")

    def onDPMSDeactivated(self):
        xbmcgui.Window(10000).setProperty(SYNC_PROP, "1")

    def abortRequested(self):
        return QUITTING.is_set() or super().abortRequested()

    def waitForAbort(self, timeout=None):
        """Jako `xbmc.Monitor.waitForAbort`, jen se po kouscích dívá i na `QUITTING` —
        ten nastaví callback, o kterém původní čekání v C neví."""
        end = None if timeout is None else time.time() + timeout
        while True:
            if QUITTING.is_set():
                return True
            left = QUIT_WAIT_STEP if end is None else min(QUIT_WAIT_STEP, end - time.time())
            if left <= 0:
                return False
            if super().waitForAbort(left):
                return True


QUITTING = threading.Event()
QUIT_WAIT_STEP = 0.5   # s – po jakých kouscích `ServiceMonitor.waitForAbort` kontroluje `QUITTING`


def mark_quitting():
    if QUITTING.is_set():
        return
    QUITTING.set()
    try:
        xbmcgui.Window(10000).setProperty(QUIT_PROP, str(int(time.time())))
    except Exception:  # noqa: BLE001 – při vypínání už GUI nemusí odpovídat, služba končí i tak
        pass
    log("Kodi končí (System.OnQuit), rozdělaná práce se přeruší")


def prefetch_next_later():
    """Po dokoukání dílu předstáhnout streamy toho dalšího — Up Next se pak neptá sítě."""
    def run():
        monitor = xbmc.Monitor()
        if monitor.waitForAbort(15) or QUITTING.is_set():
            return   # Kodi končí — nezačínat hledání, na které by pak čekalo
        warm_caches(monitor, "next")
    threading.Thread(target=run, daemon=True).start()


# --- statistiky ---------------------------------------------------------------------

def stats_context(addon):
    """Verze doplňku, platforma, Kodi a aktivní zdroje – kontext k odeslaným čítačům."""
    platform = next((name for name, cond in (
        ("Android", "System.Platform.Android"), ("Linux", "System.Platform.Linux"),
        ("Windows", "System.Platform.Windows"), ("macOS", "System.Platform.OSX"),
        ("iOS", "System.Platform.IOS"), ("tvOS", "System.Platform.TVOS"),
    ) if xbmc.getCondVisibility(cond)), "?")

    # jen jestli je zdroj v nastavení aktivní — žádné účty, žádné adresy
    sources = kodi_sources.stats_sources(addon.getSetting)
    return {
        "version": addon.getAddonInfo("version"),
        "platform": platform,
        "kodi": xbmc.getInfoLabel("System.BuildVersionShort"),
        "lang": xbmc.getLanguage(xbmc.ISO_639_1) or "",
        "sources": sources,
        "product": "kodi",
    }


def _show_pending_message(stats):
    """Zpráva napsaná v dashboardu (obrazovka Zprávy) — `stats.send()` ji zachytil
    do `last_message`. `textviewer()` (ne `.ok()`, ten zprávu delší než pár řádků
    prostě ořízne bez posouvání — nahlásil uživatel 2026-09-18) je tady bezpečný:
    běží ze služby na pozadí, ne z cesty, kterou může spustit widget nebo JSON-RPC
    (viz pravidlo v CLAUDE.md).

    Ve vlastním vlákně: modál čeká na OK klidně hodiny (televize běží, nikdo u ní
    nesedí) a do 6.1.4 po tu dobu stála celá smyčka služby — sledování přehrávače,
    synchronizace s HA, titulky, hlášení o pádech. Během přehrávání se neukazuje
    (přerušil by film) a při vypínání Kodi taky ne; zpráva přijde znovu s dalším
    hlášením, dokud ji uživatel nezavře (`msg_seen` se posílá až po zavření)."""
    msg = stats.last_message
    if not msg or QUITTING.is_set() or xbmc.Player().isPlaying():
        return
    if _MESSAGE_SHOWING.locked():
        return   # předchozí zpráva pořád otevřená

    def ukaz():
        with _MESSAGE_SHOWING:
            xbmcgui.Dialog().textviewer(L(30000), msg.get("text") or "")
            if not QUITTING.is_set():
                stats.mark_message_seen(msg["id"])

    threading.Thread(target=ukaz, name="nokturno-message", daemon=True).start()


_MESSAGE_SHOWING = threading.Lock()


def stats_tick(stats, force=False):
    """Sebere „doplněk byl otevřen“ a „u titulu se zobrazily streamy“, jednou za čas odešle čítače."""
    used = xbmcgui.Window(10000).getProperty(USED_PROP)
    if used:
        xbmcgui.Window(10000).clearProperty(USED_PROP)
        stats.note_use(int(used) if used.isdigit() else None)
    raw_viewed = xbmcgui.Window(10000).getProperty(VIEWED_PROP)
    if raw_viewed:
        xbmcgui.Window(10000).clearProperty(VIEWED_PROP)
        try:
            viewed = json.loads(raw_viewed)
        except ValueError:
            viewed = None
        if viewed and viewed.get("id"):
            stats.note_play(viewed["id"], viewed.get("title") or "",
                            viewed.get("year"), viewed.get("kind") or "movie")
    if xbmcgui.Window(10000).getProperty(FORCE_STATS_PROP) and time.time() - _STARTED_AT >= MESSAGE_DELAY:
        # doplněk se právě aktualizoval (viz default.py) — nečekat až SEND_EVERY (6 h),
        # ať případná zpráva z dashboardu (odpověď na nahlášený log, oznámení chyby)
        # dorazí co nejdřív po instalaci nové verze, ne až s dalším pravidelným hlášením.
        # MESSAGE_DELAY: aktualizace typicky přijde hned po restartu Kodi/služby (nová
        # verze se stáhne a nastartuje) — modální Dialog().ok() volaný dřív, než doběhne
        # start skinu, by nikdo nezaznamenal (viz vlastnost do `getProperty` necháváme
        # nastavenou, dokud grace neuplyne, aby se to nezahodilo)
        xbmcgui.Window(10000).clearProperty(FORCE_STATS_PROP)
        # start služby po aktualizaci už statistiky odeslal (a zprávu z dashboardu vyzvedl) —
        # druhé odeslání do minut dashboard omezí (HTTP 429)
        force = time.time() - float(stats.data.get("last_sent") or 0) > FORCE_STATS_GAP
    addon = fresh_addon()
    if addon is None:
        return
    if not force and not stats.due():
        return
    if addon.getSetting("stats_enabled") != "true":
        # vypnuté statistiky: jen „instalace žije" — id, produkt a verze, žádné tituly ani zdroje
        # (zpráva z dashboardu se pošle i tak — viz _show_pending_message níže)
        ok, why = stats.send(COLLECT_URL, version=addon.getAddonInfo("version"), product="kodi", ping=True)
        log("ping instalace odeslán" if ok else f"ping instalace neodeslán: {why}",
            xbmc.LOGINFO if ok else xbmc.LOGWARNING)
        _show_pending_message(stats)
        return
    ok, why = stats.send(COLLECT_URL, **stats_context(addon))
    log("statistiky odeslány" if ok else f"statistiky neodeslány: {why}",
        xbmc.LOGINFO if ok else xbmc.LOGWARNING)
    _show_pending_message(stats)


# --- hlášení o pádech -----------------------------------------------------------------

def crash_reports_on(addon):
    return addon.getSetting("stats_enabled") == "true" and addon.getSetting("crash_reports") != "false"


class CrashSender:
    """Odešle frontu hlášení o pádech (plugin i služba ji jen plní). Síť ve vlastním vlákně,
    ať hlavní smyčka nečeká; při vypnutém přepínači se fronta smaže — souhlas platí i zpětně."""

    def __init__(self, reporter):
        self.reporter = reporter
        self.next_try = time.time() + 60   # po startu Kodi nechat doběhnout síť
        self._busy = threading.Lock()

    def tick(self):
        win = xbmcgui.Window(10000)
        nudged = bool(win.getProperty(CRASH_PROP))
        if not nudged and time.time() < self.next_try:
            return
        win.clearProperty(CRASH_PROP)
        self.next_try = time.time() + CRASH_EVERY
        addon = fresh_addon()
        if addon is None:
            return
        if not crash_reports_on(addon):
            for path in self.reporter.pending():
                try:
                    os.remove(path)
                except OSError:
                    pass
            return
        if not self.reporter.pending() or not self._busy.acquire(blocking=False):
            return

        def run():
            try:
                sent, left = self.reporter.flush(CRASH_URL, agent="Kodi plugin.video.nokturno/"
                                                 + addon.getAddonInfo("version"),
                                                 should_stop=QUITTING.is_set)
                if sent or left:
                    log(f"hlášení o pádech: odesláno {sent}, zbývá {left}")
            finally:
                self._busy.release()

        threading.Thread(target=run, daemon=True, name="nokturno-crash").start()


def capture_service_crash(where, exc):
    """Neočekávaná výjimka ve službě (hlavní smyčka nebo vlákno) → fronta hlášení."""
    try:
        addon = fresh_addon()
        if addon is None or not crash_reports_on(addon):
            return
        platform = stats_context(addon)["platform"]
        CrashReporter(PROFILE).capture(exc, Stats(PROFILE).data["id"], "kodi", addon.getAddonInfo("version"),
                                       platform=platform, kodi=xbmc.getInfoLabel("System.BuildVersionShort"),
                                       action=where)
        xbmcgui.Window(10000).setProperty(CRASH_PROP, "1")
    except Exception as e:  # noqa: BLE001 – hlášení nesmí nic shodit
        log(f"hlášení o pádu služby nezařazeno: {e}", xbmc.LOGWARNING)


def install_thread_hook():
    """Pád ve vlákně služby (stahování, zahřívání, sync) jinak skončí jen v logu."""
    previous = threading.excepthook

    def hook(args):
        if args.exc_value is not None and not isinstance(args.exc_value, SystemExit):
            capture_service_crash(f"service:{getattr(args.thread, 'name', '?')}", args.exc_value)
        previous(args)

    threading.excepthook = hook


# --- hlavní smyčka ------------------------------------------------------------------

def main():
    # vlastnost z minulého běhu (restart služby po aktualizaci doplňku v témže procesu Kodi)
    xbmcgui.Window(10000).clearProperty(QUIT_PROP)
    monitor = ServiceMonitor()
    migrate_profile(PROFILE)
    store = Store(PROFILE)
    # rozdělané stahování z minula začít znovu
    for d in store.downloads():
        if d.get("status") in ("running", "cancel"):
            store.update_download(d["id"], status="queued", done=0)
    install_thread_hook()
    stats = Stats(PROFILE)
    player = Player(store, stats)
    player.sw = SyncWatchManager(store, monitor)
    player.sw.start()
    Downloader(store, monitor).start()
    threading.Thread(target=warmer, args=(monitor,), daemon=True).start()
    threading.Thread(target=lang_warmer, args=(monitor,), daemon=True).start()
    threading.Thread(target=lang_trigger_watcher, args=(monitor,), daemon=True).start()
    syncer = Syncer(store)
    accounts_checker = AccountsChecker(store)
    watch_checker = WatchChecker(store)
    marks = KodiMarks(store)
    crash_sender = CrashSender(CrashReporter(PROFILE))
    log("start")
    refresh_tmdbhelper_player()
    try:
        while not monitor.abortRequested():
            player.tick()
            stats_tick(stats)
            syncer.tick()
            accounts_checker.tick()
            watch_checker.tick()
            marks.tick()
            crash_sender.tick()
            if monitor.waitForAbort(POLL):
                break
    except Exception as e:  # noqa: BLE001 – zaznamenat a nechat spadnout jako dřív
        capture_service_crash("service", e)
        raise
    player.finish()
    # Vypnutí Kodi, restart doplňku po změně nastavení nebo jeho zakázání v
    # nastavení Kodi — tohle spolehlivě proběhne, skutečná odinstalace (smazání
    # složky) ne. I tak zpřesní čas posledního vidění o hodiny až šest.
    stats_tick(stats, force=True)
    log("stop")


if __name__ == "__main__":
    main()
