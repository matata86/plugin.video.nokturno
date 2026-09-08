"""Historie hledání a stav zhlédnutí — JSON soubory v profilu doplňku.

Bez závislosti na Kodi (dostane jen adresář). Kodi si u položek z pluginu
zhlédnutí samo nepamatuje spolehlivě (cesta streamu se mění), proto vlastní
evidence: `watched.json` = {id: {"playcount": n, "resume": s, "total": s, "ts": t}}.
"""
import json
import os
import time

HISTORY_MAX = 30
WATCHED_MAX = 5000


class Store:
    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self._cache = {}

    # --- soubory ------------------------------------------------------------
    def _path(self, name):
        return os.path.join(self.dir, name + ".json")

    def _load(self, name, default):
        if name in self._cache:
            return self._cache[name]
        try:
            with open(self._path(name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = default
        self._cache[name] = data
        return data

    def _save(self, name, data):
        self._cache[name] = data
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self._path(name))

    # --- historie hledání -------------------------------------------------------
    def history(self, kind):
        return list(self._load("history", {}).get(kind, []))

    def add_history(self, kind, query):
        query = (query or "").strip()
        if not query:
            return
        data = self._load("history", {})
        items = [q for q in data.get(kind, []) if q.lower() != query.lower()]
        data[kind] = ([query] + items)[:HISTORY_MAX]
        self._save("history", data)

    def remove_history(self, kind, query):
        data = self._load("history", {})
        data[kind] = [q for q in data.get(kind, []) if q != query]
        self._save("history", data)

    def clear_history(self, kind):
        data = self._load("history", {})
        data[kind] = []
        self._save("history", data)

    # --- zhlédnuto / rozkoukáno ---------------------------------------------------
    def watched(self, item_id):
        return self._load("watched", {}).get(str(item_id))

    def playcount(self, item_id):
        return int((self.watched(item_id) or {}).get("playcount") or 0)

    def resume(self, item_id):
        w = self.watched(item_id) or {}
        return float(w.get("resume") or 0), float(w.get("total") or 0)

    def set_watched(self, item_id, watched=True):
        data = self._load("watched", {})
        entry = data.get(str(item_id)) or {}
        entry["playcount"] = 1 if watched else 0
        entry["resume"] = 0
        entry["ts"] = int(time.time())
        data[str(item_id)] = entry
        self._trim(data)
        self._save("watched", data)

    def set_resume(self, item_id, position, total):
        data = self._load("watched", {})
        entry = data.get(str(item_id)) or {}
        entry["resume"] = round(float(position), 1)
        entry["total"] = round(float(total), 1)
        entry["ts"] = int(time.time())
        data[str(item_id)] = entry
        self._trim(data)
        self._save("watched", data)

    def _trim(self, data):
        if len(data) > WATCHED_MAX:
            for key in sorted(data, key=lambda k: data[k].get("ts", 0))[: len(data) - WATCHED_MAX]:
                del data[key]
