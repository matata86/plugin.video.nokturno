"""Náhrada `xbmcaddon` — nastavení drží jeden slovník pro všechny instance (jako Kodi)."""
import os

settings = {}
info = {
    "id": "plugin.video.nokturno",
    "name": "Nokturno",
    "version": "0.0.0",
    "icon": "resources/media/icon2.png",
    "path": os.getcwd(),
    "profile": os.getcwd(),
}
opened_settings = []


class Addon:
    def __init__(self, addon_id=None):
        self.id = addon_id or info["id"]

    def getAddonInfo(self, key):
        return info.get(key, "")

    def getSetting(self, key):
        return settings.get(key, "")

    def getSettingBool(self, key):
        return settings.get(key, "") == "true"

    def getSettingString(self, key):
        return settings.get(key, "")

    def setSetting(self, key, value):
        settings[key] = str(value)

    def setSettingBool(self, key, value):
        settings[key] = "true" if value else "false"

    def getLocalizedString(self, sid):
        # záměrně prázdné: kód má u každého řetězce mít vlastní zálohu (viz `L()`),
        # skutečné překlady kontroluje test nad strings.po
        return ""

    def openSettings(self):
        opened_settings.append(self.id)


def reset():
    settings.clear()
    del opened_settings[:]
