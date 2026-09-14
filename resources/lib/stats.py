"""Anonymní statistiky používání — lokální čítače v profilu a jejich odesílání.

stats.json v profilu drží:
  id          náhodný identifikátor instalace (nic z něj nejde odvodit)
  installed   kdy se čítače založily
  last_used   poslední otevření doplňku nebo zobrazení streamů titulu
  last_sent   poslední úspěšné odeslání
  next_try    kdy má smysl zkusit odeslání znovu
  plays       {klíč titulu: {"t" název, "y" rok, "k" typ, "l" kdy naposledy}} — kdy se
              u titulu naposledy zobrazily streamy, ne kolikrát se skutečně přehrálo
              (spousta streamů nejde přehrát vůbec a to nic neříká o tom, jak je titul
              žádaný). Kolikrát za den se to stalo, klient neřeší — dedup „jednou
              denně" dělá server podle dne posledního zobrazení (`server/lib/collect.php`
              v Dashboardu), klient jen posílá čerstvý stav.

K hlášení se přidává (neukládá se, klient ho skládá při každém odeslání):
  sources     které zdroje má instalace v nastavení aktivní — klíče jako `Engine.sources()`
              („luna", „sosac", „webshare", „hellspy", „sledujteto", „torrent") plus
              „tmdb" a „trakt"; nic z účtů, jen jestli je zdroj zapnutý
  product     „kodi" / „ha" / „stremio" — dřív server odvozoval jen z platformy

Při vypnutých statistikách klient místo hlášení posílá jen `ping_payload()`:
náhodné id, produkt a verzi — aby bylo vidět, že instalace žije. Žádné tituly,
zdroje, platforma, jazyk ani časy použití (dashboard u pingu nic dalšího nepřepíše).

Posílá se kumulativní stav, ne přírůstky — server dělá upsert, takže výpadek
sítě ani ztracená odpověď nic nerozhodí. Zapisuje jen služba na pozadí
(`service.py`); plugin jí události předává přes vlastnost okna, aby dva procesy
nepsaly do stejného souboru.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

# Sběrný bod je natvrdo v kódu, ne v nastavení — je to detail implementace,
# ne něco, co by měl kdokoli přepínat. Změna adresy = nová verze.
# Od 2026-09-13 dashboard běží v LXC 124 přes Tailscale Funnel; stará adresa
# nokturno.full-net.cz/collect zatím hlášení přeposílá (Dashboard/thinline-presmerovani).
COLLECT_URL = "https://nokturno.tailf0014.ts.net/collect"

SEND_EVERY = 6 * 3600     # nejčastěji jednou za 6 hodin
RETRY_EVERY = 30 * 60     # po neúspěchu (server neběží, není síť) nezkoušet hned znovu
PLAYS_MAX = 300           # v souboru i v odeslané dávce jen tolik titulů
TIMEOUT = 10


class Stats:
    """Čítače v `stats.json`. Zámek: HA volá `note_play`/`note_use` z executoru souběžně
    a `json.dump` nad měněným slovníkem padal na „dictionary changed size during iteration"."""

    def __init__(self, directory):
        self.path = os.path.join(directory, "stats.json")
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not data.get("id"):
            data = {"id": uuid.uuid4().hex, "installed": int(time.time())}
        data.setdefault("plays", {})
        return data

    def _save(self):
        with self._lock:
            plays = self.data.get("plays") or {}
            if len(plays) > PLAYS_MAX:
                keep = sorted(plays.items(), key=lambda kv: kv[1].get("l") or 0, reverse=True)[:PLAYS_MAX]
                self.data["plays"] = dict(keep)
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"   # unikátní i pro dvě vlákna
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except OSError:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # --- sběr ---------------------------------------------------------------------

    def note_use(self, when=None):
        with self._lock:
            self.data["last_used"] = int(when or time.time())
            self._save()

    def note_play(self, key, title="", year=None, kind="movie"):
        """Titul, u kterého se právě zobrazily streamy — nezávisle na tom, jestli si
        uživatel nějaký pustí. Volat klidně při každém zobrazení, i vícekrát za den —
        server podle `l` (kdy naposledy) sám pozná, jestli jde o nový den, nebo jen
        o dohled nad tímtéž.
        """
        now = int(time.time())
        with self._lock:
            rec = self.data["plays"].setdefault(str(key), {})
            rec["l"] = now
            if title:
                rec["t"] = title[:150]
            if year:
                rec["y"] = int(year)
            rec["k"] = kind
            self.data["last_used"] = now
            self._save()

    # --- odesílání ----------------------------------------------------------------

    def due(self):
        return time.time() >= (self.data.get("next_try") or 0)

    def payload(self, version="", platform="", kodi="", lang="", sources=None, product=""):
        plays = sorted(self.data.get("plays", {}).items(), key=lambda kv: kv[1].get("l") or 0, reverse=True)
        out = {
            "id": self.data["id"],
            "version": version,
            "platform": platform,
            "kodi": kodi,
            "lang": lang,
            "installed": self.data.get("installed"),
            "last_used": self.data.get("last_used"),
            "plays": [dict(key=k, **v) for k, v in plays[:PLAYS_MAX]],
        }
        if sources is not None:
            out["sources"] = sorted({str(x) for x in sources if x})
        if product:
            out["product"] = product
        return out

    def ping_payload(self, version="", product=""):
        """Jen „instalace žije" — při vypnutých statistikách."""
        out = {"id": self.data["id"], "ping": True, "version": version}
        if product:
            out["product"] = product
        return out

    def send(self, url, version="", platform="", kodi="", lang="", agent="Kodi plugin.video.nokturno",
             sources=None, product="", ping=False):
        """Odešle stav. Vrací (True, "") nebo (False, důvod) — nikdy nevyhodí výjimku.

        `agent` odlišuje odesílatele v přístupovém logu serveru; tentýž modul
        používá i integrace pro Home Assistant (viz `lib/stats.py` v nokturno-ha).
        """
        if not url:
            return False, "chybí adresa"
        with self._lock:
            data = (self.ping_payload(version, product) if ping
                    else self.payload(version, platform, kodi, lang, sources, product))
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": f"{agent}/" + (version or "?"),
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                resp.read(1024)
                code = resp.getcode()
        except urllib.error.HTTPError as e:
            return self._failed(f"HTTP {e.code}")
        except Exception as e:  # noqa: BLE001 – síť, DNS, TLS; statistiky nesmí nic shodit
            return self._failed(str(e)[:120])
        if code and code >= 400:
            return self._failed(f"HTTP {code}")
        now = int(time.time())
        with self._lock:
            self.data["last_sent"] = now
            self.data["next_try"] = now + SEND_EVERY
            self._save()
        return True, ""

    def _failed(self, why):
        self.data["next_try"] = int(time.time()) + RETRY_EVERY
        self._save()
        return False, why
