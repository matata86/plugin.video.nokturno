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
sys.path.insert(0, os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo("path")), "resources", "lib"))
from stats import COLLECT_URL, Stats  # noqa: E402
from store import Store, migrate_profile  # noqa: E402
from sync import sync_once  # noqa: E402
from trakt_api import TraktApi, TraktError  # noqa: E402
from webshare_api import WebshareApi, WebshareError  # noqa: E402

PROP = "nokturno.playing"
VIEWED_PROP = "nokturno.viewed"
USED_PROP = "nokturno.used"
SYNC_PROP = "nokturno.sync"
SYNC_EVERY = 5 * 60   # výměna s HA; změny (dokoukáno, Můj seznam) ji vyvolají hned
SUB_CHECK_EVERY = 12 * 3600   # jak často se ptát WebShare na stav předplatného
WATCHED_PCT = 0.90
MIN_RESUME = 60  # s – kratší kousek nemá cenu pamatovat
POLL = 5
WARM_DELAY = 180          # po startu Kodi nechat nejdřív doběhnout skin a widgety
WARM_EVERY = 3 * 3600     # žebříčky Sosáče mají TTL 3 h, Luna 12 h
WARM_RETRY = 10 * 60      # když se zrovna přehrává, zahřívání počká
CHUNK = 1024 * 1024
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f"[plugin.video.nokturno/service] {msg}", level)


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


def L(sid):
    return ADDON.getLocalizedString(sid)


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
    def __init__(self, store, stats):
        super().__init__()
        self.store = store
        self.stats = stats
        self.item = None
        self.position = 0.0
        self.total = 0.0

    def onAVStarted(self):
        raw = xbmcgui.Window(10000).getProperty(PROP)
        if not raw:
            self.item = None
            return
        try:
            self.item = json.loads(raw)
        except ValueError:
            self.item = None
            return
        xbmcgui.Window(10000).clearProperty(PROP)
        self.position, self.total = 0.0, 0.0
        log(f"sleduji {self.item.get('id')}")
        self.trakt_scrobble("start", 0)

    def tick(self):
        if not self.item or not self.isPlayingVideo():
            return
        try:
            self.position = self.getTime()
            self.total = self.getTotalTime()
        except RuntimeError:
            pass

    def progress(self):
        return (self.position / self.total * 100) if self.total > 0 else 0

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
        if self.total > 0 and self.position / self.total >= WATCHED_PCT:
            self.store.set_watched(item_id, True)
            log(f"zhlédnuto {item_id}")
        elif self.position >= MIN_RESUME:
            self.store.set_resume(item_id, self.position, self.total)
            log(f"rozkoukáno {item_id} @ {int(self.position)} s")
        self.trakt_scrobble("stop", self.progress())
        if split_key(item_id)[1] is not None:
            prefetch_next_later()
        xbmcgui.Window(10000).setProperty(SYNC_PROP, "1")   # zhlédnuto/pozice → do HA hned
        self.item = None

    def onPlayBackStopped(self):
        self.finish()

    def onPlayBackEnded(self):
        # na konci videa getTime už nemusí jít – bereme celou délku
        if self.item and self.total > 0:
            self.position = self.total
        self.finish()

    def onPlayBackError(self):
        self.item = None


# --- stahování ----------------------------------------------------------------------

def safe_filename(name):
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name or "").strip() or "video"
    return name[:180]


class Downloader(threading.Thread):
    def __init__(self, store, monitor):
        super().__init__(daemon=True)
        self.store = store
        self.monitor = monitor

    def run(self):
        while not self.monitor.abortRequested():
            job = next((d for d in self.store.downloads() if d.get("status") == "queued"), None)
            if job:
                self.download(job)
            if self.monitor.waitForAbort(POLL):
                break

    def download(self, job):
        dl_id, url, dest = job["id"], job["url"], job["dest"]
        self.store.update_download(dl_id, status="running")
        xbmcgui.Dialog().notification(L(30000), Lf(30073, job.get("name", "")), ADDON.getAddonInfo("icon"), 3000)
        tmp = dest + ".part"
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            req = urllib.request.Request(url, headers={"User-Agent": "Kodi plugin.video.nokturno"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                size = int(resp.headers.get("Content-Length") or 0)
                done, last = 0, 0.0
                self.store.update_download(dl_id, size=size)
                while True:
                    if self.monitor.abortRequested():
                        raise RuntimeError("abort")
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if time.time() - last > 2:
                        last = time.time()
                        current = next((d for d in self.store.downloads() if d.get("id") == dl_id), None)
                        if not current or current.get("status") == "cancel":
                            raise RuntimeError("cancel")
                        self.store.update_download(dl_id, done=done)
            os.replace(tmp, dest)
            self.store.update_download(dl_id, status="done", done=done, size=size or done)
            xbmcgui.Dialog().notification(L(30000), Lf(30074, job.get("name", "")), ADDON.getAddonInfo("icon"), 4000)
            log(f"staženo {dest}")
        except Exception as e:  # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            if str(e) == "cancel":
                self.store.remove_download(dl_id)
                log(f"stahování zrušeno {dest}")
            else:
                self.store.update_download(dl_id, status="error", error=str(e)[:200])
                log(f"stahování selhalo {dest}: {e}", xbmc.LOGERROR)
                xbmcgui.Dialog().notification(L(30000), Lf(30075, job.get("name", "")), xbmcgui.NOTIFICATION_ERROR, 5000)


# --- synchronizace přes Home Assistant ----------------------------------------------

class SubscriptionChecker:
    """Jednou za den upozorní, že WebShare předplatné brzy vyprší nebo už vypršelo.

    Kontroluje se přes síť jen `SUB_CHECK_EVERY`, ale upozornění samo se ukáže
    nejvýš jednou za kalendářní den (`last_warned`) — jinak by vyskakovalo při
    každém startu Kodi i uvnitř jednoho dne.
    """

    def __init__(self, store):
        self.store = store
        self.next = time.time() + 30

    def tick(self):
        if time.time() < self.next:
            return
        self.next = time.time() + SUB_CHECK_EVERY
        addon = fresh_addon()
        if addon is None or addon.getSetting("ws_enabled") != "true":
            return
        user, pw = addon.getSetting("ws_username").strip(), addon.getSetting("ws_password").strip()
        if not user or not pw:
            return
        try:
            warn_days = int(addon.getSetting("sub_warn_days") or 5)
        except ValueError:
            warn_days = 5

        def run():
            try:
                status = WebshareApi(user, pw).account_status()
            except WebshareError as e:
                log(f"stav předplatného WebShare: {e}", xbmc.LOGWARNING)
                return
            days = status["days"] if status["vip"] else 0
            if days > warn_days:
                return   # v pořádku, není co hlásit
            today = time.strftime("%Y-%m-%d")
            state = self.store.reload(SUB_STATE, {})
            if state.get("last_warned") == today:
                return
            self.store.save(SUB_STATE, {"last_warned": today})
            msg = Lf(30236, days) if status["vip"] else L(30237, "WebShare předplatné vypršelo.")
            xbmcgui.Dialog().notification(L(30000), msg, xbmcgui.NOTIFICATION_WARNING, 8000)
        threading.Thread(target=run, daemon=True).start()


SUB_STATE = "substate"   # substate.json v profilu: {"last_warned": "YYYY-MM-DD"}


class Syncer:
    """Jednou za SYNC_EVERY, nebo hned když si plugin/přehrávač řekne (vlastnost okna).
    Síť běží ve vlastním vlákně, aby nezdržela sledování pozice přehrávání."""

    def __init__(self, store):
        self.store = store
        self.next = time.time() + 60          # první výměna minutu po startu
        self.lock = threading.Lock()

    def tick(self, force=False):
        addon = fresh_addon()
        if addon is None or addon.getSetting("sync_enabled") != "true":
            return
        url, key = addon.getSetting("sync_url").strip(), addon.getSetting("sync_key").strip()
        if not url or not key:
            return
        asked = xbmcgui.Window(10000).getProperty(SYNC_PROP)
        if not force and not asked and time.time() < self.next:
            return
        xbmcgui.Window(10000).clearProperty(SYNC_PROP)
        self.next = time.time() + SYNC_EVERY
        if not self.lock.acquire(blocking=False):
            return

        def run():
            try:
                ok, pushed, pulled, why = sync_once(self.store, url, key, xbmc.getInfoLabel("System.FriendlyName"))
                log(f"sync: odesláno {pushed}, přijato {pulled}" if ok else f"sync neproběhl: {why}",
                    xbmc.LOGINFO if ok else xbmc.LOGWARNING)
            finally:
                self.lock.release()
        threading.Thread(target=run, daemon=True).start()


# --- zahřívání cache -----------------------------------------------------------------
# Domovská obrazovka (widgety) i hlavní menu čtou katalogy z cache doplňku. Když ji
# služba potichu obnoví dřív, než vyprší, uživatel čeká na síť jen výjimečně.
# Jde to přes běžný výpis pluginu (Files.GetDirectory), ne přes API napřímo — projde
# tím i doplnění popisů u Sosáče a nic se nepočítá do statistik (to dělá jen
# hlavní menu a seznam streamů). Streamy dalšího dílu má vlastní akci `prefetch`.

def warm_urls():
    """Katalogy, ze kterých žijí widgety a hlavní menu — jen pro zapnuté zdroje."""
    base = "plugin://plugin.video.nokturno/?action=catalog&src={src}&type={t}&catalog={c}"
    urls = []
    if ADDON.getSetting("luna_enabled") != "false":
        for t, c in (("movie", "tmdb.trending_movie"), ("series", "tmdb.trending_series")):
            urls.append(base.format(src="luna", t=t, c=c) + "&genre=Week")
        for t, c in (("movie", "tmdb.top_movie"), ("series", "tmdb.top_series")):
            urls.append(base.format(src="luna", t=t, c=c))
    if ADDON.getSetting("sosac_enabled") != "false":
        for t, c in (("movie", "moviesmostpopular"), ("movie", "moviesrecentlyadded"),
                     ("series", "tvshowsmostpopular"), ("series", "tvshowsrecentlyadded")):
            urls.append(base.format(src="sosac", t=t, c=c))
    return urls


def rpc_directory(url):
    xbmc.executeJSONRPC(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "Files.GetDirectory",
                                    "params": {"directory": url, "media": "video"}}))


def warm_caches(monitor, what="all"):
    try:
        if what in ("all", "catalogs"):
            for url in warm_urls():
                if monitor.abortRequested():
                    return
                rpc_directory(url)
        if what in ("all", "next"):
            rpc_directory("plugin://plugin.video.nokturno/?action=prefetch&kind=next")
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


def prefetch_next_later():
    """Po dokoukání dílu předstáhnout streamy toho dalšího — Up Next se pak neptá sítě."""
    def run():
        xbmc.sleep(15000)
        warm_caches(xbmc.Monitor(), "next")
    threading.Thread(target=run, daemon=True).start()


# --- statistiky ---------------------------------------------------------------------

def stats_context(addon):
    """Verze doplňku, platforma a Kodi – kontext k odeslaným čítačům."""
    platform = next((name for name, cond in (
        ("Android", "System.Platform.Android"), ("Linux", "System.Platform.Linux"),
        ("Windows", "System.Platform.Windows"), ("macOS", "System.Platform.OSX"),
        ("iOS", "System.Platform.IOS"), ("tvOS", "System.Platform.TVOS"),
    ) if xbmc.getCondVisibility(cond)), "?")
    return {
        "version": addon.getAddonInfo("version"),
        "platform": platform,
        "kodi": xbmc.getInfoLabel("System.BuildVersionShort"),
        "lang": xbmc.getLanguage(xbmc.ISO_639_1) or "",
    }


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
    addon = fresh_addon()
    if addon is None or addon.getSetting("stats_enabled") != "true":
        return
    if not force and not stats.due():
        return
    ok, why = stats.send(COLLECT_URL, **stats_context(addon))
    log("statistiky odeslány" if ok else f"statistiky neodeslány: {why}",
        xbmc.LOGINFO if ok else xbmc.LOGWARNING)


# --- hlavní smyčka ------------------------------------------------------------------

def main():
    monitor = xbmc.Monitor()
    migrate_profile(PROFILE)
    store = Store(PROFILE)
    # rozdělané stahování z minula začít znovu
    for d in store.downloads():
        if d.get("status") in ("running", "cancel"):
            store.update_download(d["id"], status="queued", done=0)
    stats = Stats(PROFILE)
    player = Player(store, stats)
    Downloader(store, monitor).start()
    threading.Thread(target=warmer, args=(monitor,), daemon=True).start()
    syncer = Syncer(store)
    sub_checker = SubscriptionChecker(store)
    log("start")
    while not monitor.abortRequested():
        player.tick()
        stats_tick(stats)
        syncer.tick()
        sub_checker.tick()
        if monitor.waitForAbort(POLL):
            break
    player.finish()
    # Vypnutí Kodi, restart doplňku po změně nastavení nebo jeho zakázání v
    # nastavení Kodi — tohle spolehlivě proběhne, skutečná odinstalace (smazání
    # složky) ne. I tak zpřesní čas posledního vidění o hodiny až šest.
    stats_tick(stats, force=True)
    log("stop")


if __name__ == "__main__":
    main()
