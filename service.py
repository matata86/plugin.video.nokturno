"""Služba na pozadí: sleduje přehrávání položek Nokturna a ukládá zhlédnuto/rozkoukáno.

Plugin před přehráním nastaví vlastnost okna `nokturno.playing` (JSON s id
titulu). Služba pak během přehrávání každých pár sekund zapisuje pozici;
při skončení nad 90 % označí titul jako zhlédnutý, jinak uloží pozici pro
pokračování. Plugin se po `setResolvedUrl` ukončí, proto to nejde dělat v něm.
"""
import json
import os
import sys

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
sys.path.insert(0, os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo("path")), "resources", "lib"))
from store import Store  # noqa: E402

PROP = "nokturno.playing"
WATCHED_PCT = 0.90
MIN_RESUME = 60  # s – kratší kousek nemá cenu pamatovat
POLL = 5


def log(msg):
    xbmc.log(f"[plugin.video.luna/service] {msg}", xbmc.LOGINFO)


class Player(xbmc.Player):
    def __init__(self):
        super().__init__()
        self.item = None
        self.position = 0.0
        self.total = 0.0
        self.store = Store(xbmcvfs.translatePath(ADDON.getAddonInfo("profile")))

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

    def tick(self):
        if not self.item or not self.isPlayingVideo():
            return
        try:
            self.position = self.getTime()
            self.total = self.getTotalTime()
        except RuntimeError:
            pass

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


def main():
    monitor = xbmc.Monitor()
    player = Player()
    log("start")
    while not monitor.abortRequested():
        player.tick()
        if monitor.waitForAbort(POLL):
            break
    player.finish()


if __name__ == "__main__":
    main()
