"""Most mezi `settings.xml` Kodi a okruhy `settings`/`accounts` v jádru.

Jádro (`setsync.py`) rozhoduje, co do kterého okruhu patří a která hodnota
vyhrává, ale do nastavení Kodi nevidí — čtení i zápis je tady. Používá to
plugin (`default.py`, ruční „Synchronizovat teď") i služba (`service.py`),
proto to nesedí ani v jednom z nich.

Seznam klíčů se neudržuje ručně: bere se přímo ze `settings.xml`, takže nové
nastavení se začne sdílet samo. Ven z něj odpadne, co `setsync.DENY` zakazuje
(`download_dir`, celá kategorie `sync_*`) a tlačítka, která hodnotu nemají.
"""
import os
import xml.etree.ElementTree as ET

from setsync import circle_of

_CACHE = {}        # cesta -> {id: výchozí hodnota}; settings.xml se za běhu nemění


def shared_defaults(settings_xml):
    """`{id: výchozí hodnota}` pro klíče, které se smí sdílet."""
    if settings_xml not in _CACHE:
        out = {}
        try:
            root = ET.parse(settings_xml).getroot()
        except (ET.ParseError, OSError):
            return {}
        for setting in root.iter("setting"):
            sid = setting.get("id")
            if not sid or setting.get("type") == "action" or circle_of(sid) is None:
                continue
            out[sid] = (setting.findtext("default") or "").strip()
        _CACHE[settings_xml] = out
    return _CACHE[settings_xml]


def values(addon, addon_path):
    """Aktuální hodnoty sdílených nastavení. Neuložená položka jde s výchozí
    hodnotou ze `settings.xml` — jinak by prázdno na jednom Kodi vypadalo jako
    změna proti výchozí hodnotě na druhém."""
    xml = os.path.join(addon_path, "resources", "settings.xml")
    out = {}
    for sid, default in shared_defaults(xml).items():
        try:
            out[sid] = addon.getSetting(sid) or default
        except Exception:      # noqa: BLE001 – neznámé id ve starším Kodi
            out[sid] = default
    return out


def apply(addon, changes):
    """Zapíše, co přišlo z druhého Kodi. Vrací id, která se opravdu změnila."""
    zapsano = []
    for sid, value in (changes or {}).items():
        if circle_of(sid) is None:
            continue           # pojistka: `setsync` to filtruje taky, zápis je nevratný
        try:
            addon.setSetting(sid, value)
            zapsano.append(sid)
        except Exception:      # noqa: BLE001 – id, které tohle Kodi nezná
            continue
    return zapsano
