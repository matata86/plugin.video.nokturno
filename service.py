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
from stats import Stats  # noqa: E402
from store import Store, migrate_profile  # noqa: E402
from trakt_api import TraktApi, TraktError  # noqa: E402

PROP = "nokturno.playing"
USED_PROP = "nokturno.used"
WATCHED_PCT = 0.90
MIN_RESUME = 60  # s – kratší kousek nemá cenu pamatovat
POLL = 5
CHUNK = 1024 * 1024
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))


def log(msg, level=xbmc.LOGINFO):
    xbmc.log(f"[plugin.video.nokturno/service] {msg}", level)


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
    addon = xbmcaddon.Addon()  # čerstvá nastavení
    if addon.getSetting("trakt_enabled") != "true":
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
        self.stats.note_play(self.item.get("id"), self.item.get("title") or "",
                             self.item.get("year"), self.item.get("kind") or "movie")
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


# --- statistiky ---------------------------------------------------------------------

def stats_context():
    """Verze doplňku, platforma a Kodi – kontext k odeslaným čítačům."""
    addon = xbmcaddon.Addon()
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
    """Sebere „doplněk byl otevřen“ a jednou za čas odešle čítače."""
    used = xbmcgui.Window(10000).getProperty(USED_PROP)
    if used:
        xbmcgui.Window(10000).clearProperty(USED_PROP)
        stats.note_use(int(used) if used.isdigit() else None)
    addon = xbmcaddon.Addon()  # čerstvá nastavení
    if addon.getSetting("stats_enabled") != "true":
        return
    if not force and not stats.due():
        return
    ok, why = stats.send(addon.getSetting("stats_url").strip(), **stats_context())
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
    log("start")
    while not monitor.abortRequested():
        player.tick()
        stats_tick(stats)
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
