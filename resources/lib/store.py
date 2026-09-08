"""Trvalá data doplňku — JSON soubory v profilu (bez závislosti na Kodi).

history.json     historie hledání {kind: [dotazy]}
watched.json     {klíč: {"playcount", "resume", "total", "ts"}}
items.json       snímky titulů podle klíče (název, plakát, typ, id…) pro Pokračovat / Můj seznam
favourites.json  [klíče] – Můj seznam
downloads.json   fronta stahování [{"id", "url", "name", "dest", "status", "done", "size", "error", "ts"}]
trakt.json       tokeny Traktu
cache/           odpovědi API s TTL (manifesty, meta)

Kodi si u položek z pluginu zhlédnutí samo nepamatuje spolehlivě (cesta streamu
se mění), proto vlastní evidence.
"""
import hashlib
import json
import os
import threading
import time

OLD_ADDON_ID = "plugin.video.luna"  # do 1.3.0 se doplněk jmenoval takhle
DATA_FILES = ("history", "watched", "items", "favourites", "downloads", "trakt")
HISTORY_MAX = 30
WATCHED_MAX = 5000
ITEMS_MAX = 2000


def migrate_profile(new_dir):
    """Jednorázově přenese data z profilu starého ID (plugin.video.luna) do nového.

    Vrací cestu ke starému settings.xml (plugin z něj přebere nastavení), nebo None.
    """
    marker = os.path.join(new_dir, ".migrated")
    if os.path.exists(marker):
        return None
    old_dir = os.path.join(os.path.dirname(new_dir.rstrip("/\\")), OLD_ADDON_ID)
    os.makedirs(new_dir, exist_ok=True)
    old_settings = None
    if os.path.isdir(old_dir):
        import shutil
        for name in DATA_FILES:
            src = os.path.join(old_dir, name + ".json")
            dst = os.path.join(new_dir, name + ".json")
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy(src, dst)
        if os.path.exists(os.path.join(old_dir, "settings.xml")):
            old_settings = os.path.join(old_dir, "settings.xml")
            # soubor zkopírovat dřív, než si Kodi založí prázdný; plugin navíc hodnoty aplikuje přes setSetting
            if not os.path.exists(os.path.join(new_dir, "settings.xml")):
                shutil.copy(old_settings, os.path.join(new_dir, "settings.xml"))
    with open(marker, "w") as f:
        f.write("1")
    return old_settings


class Store:
    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        os.makedirs(os.path.join(directory, "cache"), exist_ok=True)
        self._cache = {}

    # --- soubory ------------------------------------------------------------
    def _path(self, name):
        return os.path.join(self.dir, name + ".json")

    def load(self, name, default):
        if name in self._cache:
            return self._cache[name]
        try:
            with open(self._path(name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = default
        self._cache[name] = data
        return data

    def _tmp(self, path):
        """Dočasný soubor musí být unikátní — plugin i služba (a víc instancí pluginu)
        zapisují souběžně a se společným jménem si ho navzájem přejmenují pod rukama
        (`FileNotFoundError: items.json.tmp -> items.json`)."""
        return f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"

    def save(self, name, data):
        self._cache[name] = data
        path = self._path(name)
        tmp = self._tmp(path)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:  # zápis cache není kritický, ať kvůli němu nepadne výpis
            try:
                os.remove(tmp)
            except OSError:
                pass

    def reload(self, name, default):
        """Načte znovu z disku – když soubor mění i služba na pozadí."""
        self._cache.pop(name, None)
        return self.load(name, default)

    # --- historie hledání -------------------------------------------------------
    def history(self, kind):
        return list(self.load("history", {}).get(kind, []))

    def add_history(self, kind, query):
        query = (query or "").strip()
        if not query:
            return
        data = self.load("history", {})
        items = [q for q in data.get(kind, []) if q.lower() != query.lower()]
        data[kind] = ([query] + items)[:HISTORY_MAX]
        self.save("history", data)

    def remove_history(self, kind, query):
        data = self.load("history", {})
        data[kind] = [q for q in data.get(kind, []) if q != query]
        self.save("history", data)

    def clear_history(self, kind):
        data = self.load("history", {})
        data[kind] = []
        self.save("history", data)

    # --- zhlédnuto / rozkoukáno ---------------------------------------------------
    def watched(self, item_id):
        return self.load("watched", {}).get(str(item_id))

    def playcount(self, item_id):
        return int((self.watched(item_id) or {}).get("playcount") or 0)

    def resume(self, item_id):
        w = self.watched(item_id) or {}
        return float(w.get("resume") or 0), float(w.get("total") or 0)

    def set_watched(self, item_id, watched=True):
        data = self.load("watched", {})
        entry = data.get(str(item_id)) or {}
        entry["playcount"] = 1 if watched else 0
        entry["resume"] = 0
        entry["ts"] = int(time.time())
        data[str(item_id)] = entry
        self._trim(data, WATCHED_MAX)
        self.save("watched", data)

    def set_resume(self, item_id, position, total):
        data = self.load("watched", {})
        entry = data.get(str(item_id)) or {}
        entry["resume"] = round(float(position), 1)
        entry["total"] = round(float(total), 1)
        entry["ts"] = int(time.time())
        data[str(item_id)] = entry
        self._trim(data, WATCHED_MAX)
        self.save("watched", data)

    def in_progress(self):
        """Rozkoukané: [(klíč, záznam)] od nejnovějšího."""
        data = self.load("watched", {})
        rows = [(k, v) for k, v in data.items() if float(v.get("resume") or 0) > 0 and not v.get("playcount")]
        return sorted(rows, key=lambda kv: -kv[1].get("ts", 0))

    def recently_watched(self, limit=50):
        data = self.load("watched", {})
        rows = [(k, v) for k, v in data.items() if v.get("playcount")]
        return sorted(rows, key=lambda kv: -kv[1].get("ts", 0))[:limit]

    @staticmethod
    def _trim(data, limit):
        if len(data) > limit:
            for key in sorted(data, key=lambda k: data[k].get("ts", 0))[: len(data) - limit]:
                del data[key]

    # --- snímky titulů (pro seznamy bez dotazu na API) --------------------------------
    def remember_item(self, key, info):
        data = self.load("items", {})
        info = dict(info)
        info["ts"] = int(time.time())
        data[str(key)] = info
        self._trim(data, ITEMS_MAX)
        self.save("items", data)

    def item(self, key):
        return self.load("items", {}).get(str(key))

    # --- Můj seznam ---------------------------------------------------------------
    def favourites(self):
        return list(self.load("favourites", []))

    def is_favourite(self, key):
        return str(key) in self.load("favourites", [])

    def toggle_favourite(self, key, info=None):
        favs = self.load("favourites", [])
        key = str(key)
        if key in favs:
            favs.remove(key)
            added = False
        else:
            favs.insert(0, key)
            added = True
            if info:
                self.remember_item(key, info)
        self.save("favourites", favs)
        return added

    # --- stahování ------------------------------------------------------------------
    def downloads(self):
        return list(self.reload("downloads", []))

    def add_download(self, entry):
        data = self.reload("downloads", [])
        if any(d.get("id") == entry.get("id") for d in data):
            return False
        entry = dict(entry, status="queued", done=0, size=0, error="", ts=int(time.time()))
        data.append(entry)
        self.save("downloads", data)
        return True

    def update_download(self, dl_id, **fields):
        data = self.reload("downloads", [])
        for d in data:
            if d.get("id") == dl_id:
                d.update(fields)
        self.save("downloads", data)

    def remove_download(self, dl_id):
        data = self.reload("downloads", [])
        self.save("downloads", [d for d in data if d.get("id") != dl_id])

    # --- Trakt ------------------------------------------------------------------------
    def trakt(self):
        return self.load("trakt", {})

    def set_trakt(self, data):
        self.save("trakt", data or {})

    # --- cache odpovědí API -------------------------------------------------------------
    def cached(self, key, ttl, loader):
        path = os.path.join(self.dir, "cache", hashlib.md5(key.encode("utf-8")).hexdigest() + ".json")
        try:
            if time.time() - os.path.getmtime(path) < ttl:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, ValueError):
            pass
        data = loader()
        tmp = self._tmp(path)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return data

    def clear_cache(self):
        cdir = os.path.join(self.dir, "cache")
        for name in os.listdir(cdir):
            try:
                os.remove(os.path.join(cdir, name))
            except OSError:
                pass
