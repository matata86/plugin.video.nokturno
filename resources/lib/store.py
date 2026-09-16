"""Trvalá data doplňku — JSON soubory v profilu (bez závislosti na Kodi).

history.json     historie hledání {kind: [dotazy]}
watched.json     {klíč: {"playcount", "resume", "total", "ts"}}
items.json       snímky titulů podle klíče (název, plakát, typ, id…) pro Pokračovat / Můj seznam
sosac_index.json snímky z rejstříku Sosáče (`idx:` klíče) — zvlášť, viz `Index`
favourites.json  [klíče] – Můj seznam
downloads.json   fronta stahování [{"id", "url", "name", "dest", "status", "done", "size", "error", "ts"}]
trakt.json       tokeny Traktu
cache/           odpovědi API s TTL (manifesty, meta)

Kodi si u položek z pluginu zhlédnutí samo nepamatuje spolehlivě (cesta streamu
se mění), proto vlastní evidence.
"""
import hashlib
import json
import logging
import os
import threading
import time

_LOGGER = logging.getLogger(__name__)

OLD_ADDON_ID = "plugin.video.luna"  # do 1.3.0 se doplněk jmenoval takhle
DATA_FILES = ("history", "watched", "items", "favourites", "downloads", "trakt", "streampref", "favlog", "histlog",
              "streamfilter")
HISTORY_MAX = 10
WATCHED_MAX = 5000
ITEMS_MAX = 2000
INDEX_MAX = 3000
INDEX_REFRESH = 86400   # s – jak často obnovit razítko u nezměněného snímku v rejstříku
TMP_MAX_AGE = 3600   # s – starší rozepsané `*.tmp` po zabitém procesu se uklidí


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
        # Soubory sdílí víc procesů najednou (doplněk v Kodi = nový proces na každý
        # výpis, plus služba na pozadí). Načtený obsah se drží v paměti jen dokud se
        # soubor na disku nezmění (`_sigs`: mtime + velikost) — jinak by zápis
        # z procesu se starší kopií přepsal, co mezitím uložil jiný (přehrání
        # uložilo snímek titulu, výpis katalogu ho vzápětí smazal).
        self._cache = {}
        self._sigs = {}
        self._index = None
        self._clean_tmp()
        # Hledání pro film i seriál běží souběžně ve dvou vláknech (default.py
        # search_run) a obě sahají na tenhle jeden Store — bez zámku dvě vlákna
        # měnila stejný sdílený dict zároveň s tím, jak ho druhé zapisovalo
        # (json.dump) nebo prořezávalo (_trim), a Python na to spadl s
        # "dictionary changed size during iteration". RLock, ne Lock: metody
        # se volají navzájem (remember_item → load/save) ze stejného vlákna.
        self._lock = threading.RLock()

    # --- soubory ------------------------------------------------------------
    def _path(self, name):
        return os.path.join(self.dir, name + ".json")

    def _sig(self, path):
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _clean_tmp(self):
        try:
            for name in os.listdir(self.dir):
                path = os.path.join(self.dir, name)
                if name.endswith(".tmp") and time.time() - os.path.getmtime(path) > TMP_MAX_AGE:
                    os.remove(path)
        except OSError:
            pass

    def load(self, name, default):
        with self._lock:
            path = self._path(name)
            sig = self._sig(path)
            if name in self._cache and self._sigs.get(name) == sig:
                return self._cache[name]
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = default
            self._cache[name] = data
            self._sigs[name] = sig
            return data

    def _tmp(self, path):
        """Dočasný soubor musí být unikátní — plugin i služba (a víc instancí pluginu)
        zapisují souběžně a se společným jménem si ho navzájem přejmenují pod rukama
        (`FileNotFoundError: items.json.tmp -> items.json`)."""
        return f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"

    def save(self, name, data):
        with self._lock:
            self._cache[name] = data
            path = self._path(name)
            tmp = self._tmp(path)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, path)
                self._sigs[name] = self._sig(path)
            except OSError as err:  # zápis není kritický, ať kvůli němu nepadne výpis — ale ať je vidět
                _LOGGER.warning("zápis %s selhal: %s", path, err)
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def reload(self, name, default):
        """Načte znovu z disku. `load` si změnu z jiného procesu pozná sám, tohle
        zůstává pro volající, kteří to chtějí najisto (sync)."""
        with self._lock:
            self._cache.pop(name, None)
            self._sigs.pop(name, None)
            return self.load(name, default)

    def index(self):
        """Rejstřík Sosáče nad vlastním souborem, viz `Index`."""
        with self._lock:
            if self._index is None:
                self._index = Index(self)
            return self._index

    # --- historie hledání -------------------------------------------------------
    def history(self, kind):
        with self._lock:
            return list(self._hist_map().get(kind, []))

    def _hist_map(self):
        """Historie jako {kind: [dotazy]}. Starší HA verze ji ukládala jako plochý
        seznam — ten se bere jako přihrádka "any" (hlavní hledání, sdílené s doplňkem)."""
        h = self.load("history", {})
        return {"any": list(h)} if isinstance(h, list) else h

    # historie se vede i jako časovaný deník `histlog` (klíč "kind\tdotaz"),
    # aby šla synchronizovat mezi Kodi vč. mazání — zobrazený seznam `history`
    # se z něj přepočítá (novější dotaz výš), viz rebuild_history / sync.py
    @staticmethod
    def _hkey(kind, query):
        return f"{kind}\t{query.strip().lower()}"

    def _log_history(self, kind, query, on):
        log = self.load("histlog", {})
        log[self._hkey(kind, query)] = {"kind": kind, "q": query.strip(), "on": on, "ts": int(time.time())}
        self._trim(log, 500)
        self.save("histlog", log)

    def rebuild_history(self):
        """Zobrazený seznam `history` podle deníku: aktivní dotazy, novější výš, na kind."""
        log = self.load("histlog", {})
        by_kind = {}
        for rec in log.values():
            if rec.get("on") and rec.get("q"):
                by_kind.setdefault(rec.get("kind"), []).append(rec)
        data = {k: [r["q"] for r in sorted(v, key=lambda r: -(r.get("ts") or 0))][:HISTORY_MAX]
                for k, v in by_kind.items()}
        self.save("history", data)

    def add_history(self, kind, query):
        query = (query or "").strip()
        if not query:
            return
        with self._lock:
            data = self._hist_map()
            items = [q for q in data.get(kind, []) if q.lower() != query.lower()]
            data[kind] = ([query] + items)[:HISTORY_MAX]
            self.save("history", data)
            self._log_history(kind, query, True)

    def remove_history(self, kind, query):
        with self._lock:
            data = self._hist_map()
            data[kind] = [q for q in data.get(kind, []) if q != query]
            self.save("history", data)
            self._log_history(kind, query, False)

    def clear_history(self, kind):
        with self._lock:
            data = self._hist_map()
            data[kind] = []
            self.save("history", data)
            now = int(time.time())
            log = self.load("histlog", {})
            for key, rec in log.items():
                if rec.get("kind") == kind and rec.get("on"):
                    log[key] = {**rec, "on": False, "ts": now}
            self.save("histlog", log)

    # --- zhlédnuto / rozkoukáno ---------------------------------------------------
    def watched(self, item_id):
        with self._lock:
            return self.load("watched", {}).get(str(item_id))

    def playcount(self, item_id):
        return int((self.watched(item_id) or {}).get("playcount") or 0)

    def resume(self, item_id):
        w = self.watched(item_id) or {}
        return float(w.get("resume") or 0), float(w.get("total") or 0)

    def resume_stream(self, item_id):
        """Vnitřní reference streamu (`ws:…`/`hs:…:…`/…) a titulky, se kterými se titul
        naposledy hrál — na rozdíl od podepsaného odkazu zdroje nevyprší, jde ji tedy
        použít znovu při pokračování ve sledování a přeskočit tím nové hledání (`stream_url`
        z `set_resume`). `None`, když u záznamu není (starší záznam, nebo se nikdy nezapsal
        — např. položka z jiného zařízení přes sync)."""
        w = self.watched(item_id) or {}
        url = w.get("stream_url")
        if not url:
            return None
        return url, w.get("stream_subs") or ""

    def set_watched(self, item_id, watched=True):
        with self._lock:
            data = self.load("watched", {})
            entry = data.get(str(item_id)) or {}
            entry["playcount"] = 1 if watched else 0
            entry["resume"] = 0
            entry["ts"] = int(time.time())
            data[str(item_id)] = entry
            self._trim(data, WATCHED_MAX)
            self.save("watched", data)

    def set_resume(self, item_id, position, total, stream_url=None, stream_subs=None):
        """`stream_url`/`stream_subs`: jen když se pozice zapisuje za běhu přehrávání
        (`Player.save_resume`) — cross-device sync a ruční nastavení pozice žádný stream
        nezná, tam se předchozí zapamatovaná reference (pokud existuje) ponechá beze změny."""
        with self._lock:
            data = self.load("watched", {})
            entry = data.get(str(item_id)) or {}
            entry["resume"] = round(float(position), 1)
            entry["total"] = round(float(total), 1)
            entry["ts"] = int(time.time())
            if stream_url is not None:
                entry["stream_url"] = stream_url
                entry["stream_subs"] = stream_subs or ""
            data[str(item_id)] = entry
            self._trim(data, WATCHED_MAX)
            self.save("watched", data)

    # --- skrytý „Další díl“ -------------------------------------------------------
    # {id seriálu: {"ep": id dílu, "ts": čas}} — synchronizuje se mezi Kodi a HA
    # (viz sync.py), aby odebrání z Pokračovat ve sledování platilo i na Kodi,
    # které bylo zrovna vypnuté. Starší doplňky ukládaly jen {seriál: díl}.

    def hide_next(self, series_id, episode_id):
        with self._lock:
            data = self.load("next_hidden", {})
            data[str(series_id)] = {"ep": str(episode_id), "ts": int(time.time())}
            self.save("next_hidden", data)

    def next_hidden(self, series_id):
        """Id dílu, který uživatel u seriálu skryl, nebo prázdný řetězec."""
        rec = self.load("next_hidden", {}).get(str(series_id))
        if isinstance(rec, dict):
            return str(rec.get("ep") or "")
        return str(rec or "")

    def in_progress(self):
        """Rozkoukané: [(klíč, záznam)] od nejnovějšího."""
        with self._lock:
            data = self.load("watched", {})
            rows = [(k, v) for k, v in data.items() if float(v.get("resume") or 0) > 0 and not v.get("playcount")]
            return sorted(rows, key=lambda kv: -kv[1].get("ts", 0))

    def recently_watched(self, limit=50):
        with self._lock:
            data = self.load("watched", {})
            rows = [(k, v) for k, v in data.items() if v.get("playcount")]
            return sorted(rows, key=lambda kv: -kv[1].get("ts", 0))[:limit]

    @staticmethod
    def _trim(data, limit):
        if len(data) > limit:
            for key in sorted(data, key=lambda k: data[k].get("ts", 0))[: len(data) - limit]:
                del data[key]

    # --- zapamatovaná volba streamu u seriálu ----------------------------------------
    # {id seriálu: {"source", "quality", "langs", "ts"}} — jakou kombinaci si uživatel
    # vybral naposledy; další díly se pak pustí bez dialogu, když ji mají k dispozici.
    def stream_pref(self, series_id):
        with self._lock:
            return self.load("streampref", {}).get(str(series_id))

    def set_stream_pref(self, series_id, pref):
        with self._lock:
            data = self.load("streampref", {})
            data[str(series_id)] = dict(pref, ts=int(time.time()))
            self._trim(data, 300)
            self.save("streampref", data)

    # --- naposledy zvolený filtr streamů ---------------------------------------------
    # jeden společný filtr pro celý doplněk (ne na titul) — soubor je v profilu
    # tohoto Kodi, takže si ho každá instalace pamatuje sama za sebe, bez synchronizace
    def last_stream_filter(self):
        with self._lock:
            return self.load("streamfilter", {})

    def set_last_stream_filter(self, filt):
        with self._lock:
            self.save("streamfilter", dict(filt))

    # --- snímky titulů (pro seznamy bez dotazu na API) --------------------------------
    def remember_item(self, key, info):
        with self._lock:
            data = self.load("items", {})
            info = dict(info)
            info["ts"] = int(time.time())
            data[str(key)] = info
            self._trim(data, ITEMS_MAX)
            self.save("items", data)

    def item(self, key):
        with self._lock:
            return self.load("items", {}).get(str(key))

    # --- Můj seznam ---------------------------------------------------------------
    def favourites(self):
        with self._lock:
            return list(self.load("favourites", []))

    def is_favourite(self, key):
        with self._lock:
            return str(key) in self.load("favourites", [])

    def toggle_favourite(self, key, info=None):
        with self._lock:
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
            # deník pro synchronizaci (viz sync.py): samotný seznam neumí říct,
            # kdy z něj co ubylo, a bez času by se odebrání nedalo přenést jinam
            log = self.load("favlog", {})
            log[key] = {"on": added, "ts": int(time.time())}
            self._trim(log, 1000)
            self.save("favlog", log)
            return added

    # --- stahování ------------------------------------------------------------------
    def downloads(self):
        with self._lock:
            return list(self.reload("downloads", []))

    def add_download(self, entry):
        with self._lock:
            data = self.reload("downloads", [])
            if any(d.get("id") == entry.get("id") for d in data):
                return False
            entry = dict(entry, status="queued", done=0, size=0, error="", ts=int(time.time()))
            data.append(entry)
            self.save("downloads", data)
            return True

    def update_download(self, dl_id, **fields):
        with self._lock:
            data = self.reload("downloads", [])
            for d in data:
                if d.get("id") == dl_id:
                    d.update(fields)
            self.save("downloads", data)

    def remove_download(self, dl_id):
        with self._lock:
            data = self.reload("downloads", [])
            self.save("downloads", [d for d in data if d.get("id") != dl_id])

    # --- Trakt ------------------------------------------------------------------------
    def trakt(self):
        with self._lock:
            return self.load("trakt", {})

    def set_trakt(self, data):
        with self._lock:
            self.save("trakt", data or {})

    # --- cache odpovědí API -------------------------------------------------------------
    def cached(self, key, ttl, loader):
        return self.cached_if(key, ttl, loader)

    def cached_if(self, key, ttl, loader, ok=bool, fresh=False):
        """Jako `cached()`, ale na disk zapíše jen když `ok(data)` je pravda — pro
        věci, co se mají zapamatovat jen při úspěchu (např. nalezené streamy),
        ne prázdný/neúspěšný výsledek, který má jít zkusit znovu hned příště.
        `fresh=True` cache jen zapíše, nečte (zahřívání na pozadí obnoví, co už tam je)."""
        # nezávislé na self._cache (soubor per hash klíče) — nepotřebuje self._lock,
        # nejhorší případ při souběhu je zbytečné dvojí stažení, ne pád
        path = os.path.join(self.dir, "cache", hashlib.md5(key.encode("utf-8")).hexdigest() + ".json")
        try:
            if not fresh and time.time() - os.path.getmtime(path) < ttl:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, ValueError):
            pass
        data = loader()
        if not ok(data):
            return data
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

    def peek_cached(self, key, ttl):
        """Vrátí, co pro `key` uložil `cached()`/`cached_if()`, jen když je to ještě
        v `ttl` — beze spuštění loaderu. Pro rozhodnutí předem (bez placení ceny
        výpočtu), jestli je něco vůbec připravené, např. než se nabídne drahý
        přepočet uživateli ke schválení místo automatického spuštění."""
        path = os.path.join(self.dir, "cache", hashlib.md5(key.encode("utf-8")).hexdigest() + ".json")
        try:
            if time.time() - os.path.getmtime(path) < ttl:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, ValueError):
            pass
        return None

    def clear_cache(self):
        cdir = os.path.join(self.dir, "cache")
        for name in os.listdir(cdir):
            try:
                os.remove(os.path.join(cdir, name))
            except OSError:
                pass

    def prune_cache(self, max_age=72 * 3600):
        """Smaže soubory cache starší než `max_age` — TTL se hlídá jen při čtení, takže
        prošlé záznamy (hledání, streamy, katalogy) dřív ležely na disku navždy; v HA
        v `.storage`, tedy i v každé záloze. Vrací počet smazaných."""
        cdir = os.path.join(self.dir, "cache")
        hranice = time.time() - max_age
        smazano = 0
        try:
            names = os.listdir(cdir)
        except OSError:
            return 0
        for name in names:
            path = os.path.join(cdir, name)
            try:
                if os.path.getmtime(path) < hranice:
                    os.remove(path)
                    smazano += 1
            except OSError:
                pass
        return smazano



class Index:
    """Snímky z rejstříku Sosáče (`idx:<id>`) ve vlastním souboru `sosac_index.json`.

    Dřív šly do `items.json` vedle snímků pro Pokračovat / Můj seznam. Jenže rejstřík
    zapisuje každý výpis katalogu Sosáče — stovky položek z procesu, který si soubor
    načetl při svém startu. Jeho zápis pak přepsal snímek, který mezitím uložilo
    přehrávání v jiném procesu, a rozkoukaný titul se v Pokračovat ve sledování
    neukázal (Office 2026-09-13: 1168 z 1192 záznamů v items.json byl rejstřík,
    „rozkoukáno tt38061210" bez snímku). Vlastní soubor drží `items.json` malý
    a psaný jen z uživatelských akcí; staré `idx:` záznamy se odsud jednou odstěhují.
    """

    def __init__(self, store, name="sosac_index"):
        # žádné čtení ani zápis souboru tady: `Engine.sources()` zakládá SosacDirect
        # (a tím rejstřík) i z atributů senzoru HA, tedy ve smyčce událostí, kde HA
        # blokující `open()` hlásí jako chybu. Stěhování proběhne až při prvním použití.
        self.store = store
        self.name = name
        self._migrated = False

    def _migrate(self):
        if self._migrated:
            return
        self._migrated = True
        with self.store._lock:
            items = self.store.load("items", {})
            old = [k for k in items if k.startswith("idx:")]
            if not old:
                return
            data = self.store.load(self.name, {})
            for k in old:
                data.setdefault(k, items.pop(k))
            self.store.save(self.name, data)
            self.store.save("items", items)

    def remember_item(self, key, info):
        self.remember_items({key: info})

    def remember_items(self, items):
        """Celý výpis jedním zápisem. Soubor má přes megabajt a zápis po položkách
        znamenal u seznamu 47 filmů 47× načíst a uložit celý rejstřík — na Office
        (32bit ARM) 45 s místo dvou (2026-09-14)."""
        if not items:
            return
        self._migrate()
        with self.store._lock:
            data = self.store.load(self.name, {})
            ts = int(time.time())
            changed = False
            for key, info in items.items():
                info = dict(info)
                old = data.get(str(key))
                # opakované otevření téhož seznamu nic nového nepřinese — bez zápisu;
                # razítko se obnoví jen jednou za den, aby `_trim` nevyhodil živé tituly
                if old and ts - old.get("ts", 0) < INDEX_REFRESH and \
                        {k: v for k, v in old.items() if k != "ts"} == json.loads(json.dumps(info)):
                    continue
                info["ts"] = ts
                data[str(key)] = info
                changed = True
            if not changed:
                return
            self.store._trim(data, INDEX_MAX)
            self.store.save(self.name, data)

    def item(self, key):
        """`idx:` klíče z rejstříku; ostatní (snímek přehraného titulu, ze kterého
        `SosacDirect.meta()` skládá meta neznámého filmu) z `items.json`."""
        self._migrate()
        with self.store._lock:
            snap = self.store.load(self.name, {}).get(str(key))
            if snap is None and not str(key).startswith("idx:"):
                snap = self.store.item(key)
            return snap
