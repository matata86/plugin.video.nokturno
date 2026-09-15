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
from sosac_direct import SosacDirect  # noqa: E402
from stats import COLLECT_URL, Stats  # noqa: E402
from storage_api import StorageApi, parse_ref  # noqa: E402
from store import Store, migrate_profile  # noqa: E402
from sync import sync_once  # noqa: E402
from trend_api import CATALOG_ID as TREND_CATALOG_ID  # noqa: E402
from trakt_api import TraktApi, TraktError  # noqa: E402
from webshare_api import WebshareApi, WebshareError  # noqa: E402

PROP = "nokturno.playing"
VIEWED_PROP = "nokturno.viewed"
USED_PROP = "nokturno.used"
SYNC_PROP = "nokturno.sync"
SYNC_EVERY = 5 * 60   # výměna s HA; změny (dokoukáno, Můj seznam) ji vyvolají hned
SUB_CHECK_EVERY = 12 * 3600   # jak často se ptát WebShare na stav předplatného
WATCHED_PCT = 0.90
MIN_RESUME = 90  # s – po takové době přehrávání patří titul do rozkoukaných
SAVE_EVERY = 30  # s – jak často se za běhu přepisuje pozice rozkoukaného
POLL = 5
WARM_DELAY = 180          # po startu Kodi nechat nejdřív doběhnout skin a widgety
WARM_EVERY = int(2.5 * 3600)   # pod TTL žebříčků Sosáče (3 h); s WARM_PROP se cache obnoví i před vypršením
WARM_PROP = "nokturno.warm"    # plugin při zahřívání cache API jen zapisuje, nečte (viz default.warming)
WARM_RETRY = 10 * 60      # když se zrovna přehrává, zahřívání počká
CHUNK = 1024 * 1024
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
TMDBH_PLAYER = "special://profile/addon_data/plugin.video.themoviedb.helper/players/nokturno.json"


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
            return
        try:
            item = json.loads(raw)
        except ValueError:
            return
        xbmcgui.Window(10000).clearProperty(PROP)
        self.item, self.started = item, time.time()
        log(f"sleduji {item.get('id')}")
        self.trakt_scrobble("start", 0)

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
        self.store.set_resume(item_id, self.position, self.total)
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

    def onPlayBackStopped(self):
        # opožděné zastavení předchozího souboru po startu nového nesmí ukončit sledování nového
        if self.item and time.time() - self.started < 5 and self.isPlayingVideo():
            return
        self.finish()

    def onPlayBackEnded(self):
        # na konci videa getTime už nemusí jít – bereme celou délku
        if self.item and self.total > 0:
            self.position = self.total
        self.finish()

    def onPlayBackError(self):
        self.reset()


# --- stahování ----------------------------------------------------------------------

def safe_filename(name):
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name or "").strip() or "video"
    return name[:180]


def resolve_internal(url, store):
    """Vnitřní odkaz z fronty (`ws:`, `hs:`, `st:`, `streamuj:`, `dav:`) → (odkaz ke stažení, hlavičky).

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
            link, headers = resolve_internal(url, self.store)
            if not link:
                raise RuntimeError("zdroj odkaz nevydal")
            req = urllib.request.Request(link, headers={"User-Agent": "Kodi plugin.video.nokturno", **headers})
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
        elif addon.getSetting("luna_enabled") != "false":
            urls.append(base.format(src="luna", t=t, c=f"tmdb.top_{t}"))
            urls.append(base.format(src="luna", t=t, c=f"tmdb.top_rated_{t}"))
        # vlastní žebříček (dashboard) — bez ohledu na TMDB/Lunu, funguje vždycky stejně
        urls.append(base.format(src="trend", t=t, c=TREND_CATALOG_ID))
    for t, c in (("movie", "moviesrecentlyadded_dub"), ("movie", "moviesrecentlyadded_subs"),
                 ("series", "tvshowsrecentlyadded")):
        urls.append(base.format(src="sosac_db", t=t, c=c))
    return urls


def rpc_directory(url):
    xbmc.executeJSONRPC(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "Files.GetDirectory",
                                    "params": {"directory": url, "media": "video"}}))


def warm_caches(monitor, what="all"):
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


def prefetch_next_later():
    """Po dokoukání dílu předstáhnout streamy toho dalšího — Up Next se pak neptá sítě."""
    def run():
        xbmc.sleep(15000)
        warm_caches(xbmc.Monitor(), "next")
    threading.Thread(target=run, daemon=True).start()


# --- statistiky ---------------------------------------------------------------------

def stats_context(addon):
    """Verze doplňku, platforma, Kodi a aktivní zdroje – kontext k odeslaným čítačům."""
    platform = next((name for name, cond in (
        ("Android", "System.Platform.Android"), ("Linux", "System.Platform.Linux"),
        ("Windows", "System.Platform.Windows"), ("macOS", "System.Platform.OSX"),
        ("iOS", "System.Platform.IOS"), ("tvOS", "System.Platform.TVOS"),
    ) if xbmc.getCondVisibility(cond)), "?")

    def zapnuto(key, default="true"):
        return (addon.getSetting(key) or default) == "true"

    def vyplneno(key):
        return bool((addon.getSetting(key) or "").strip())

    # jen jestli je zdroj v nastavení aktivní — žádné účty, žádné adresy
    sources = [name for name, active in (
        ("luna", zapnuto("luna_enabled") and vyplneno("token")),
        ("sosac", zapnuto("sosac_enabled") and vyplneno("streamuj_username")),
        ("webshare", zapnuto("ws_enabled", "false") and vyplneno("ws_username")),
        ("hellspy", zapnuto("hs_enabled", "false")),
        ("sledujteto", zapnuto("st_enabled", "false") and vyplneno("st_email")),
        ("fastshare", zapnuto("fs_enabled", "false") and vyplneno("fs_username")),
        ("tmdb", vyplneno("tmdb_api_key")),
        ("trakt", zapnuto("trakt_enabled", "false")),
    ) if active]
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
    do `last_message`. `Dialog().ok()` je tady bezpečný: běží ze služby na pozadí,
    ne z cesty, kterou může spustit widget nebo JSON-RPC (viz pravidlo v CLAUDE.md)."""
    msg = stats.last_message
    if not msg:
        return
    xbmcgui.Dialog().ok(L(30000), msg.get("text") or "")
    stats.mark_message_seen(msg["id"])


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
    refresh_tmdbhelper_player()
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
