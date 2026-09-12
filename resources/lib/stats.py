"""Anonymní statistiky používání — lokální čítače v profilu a jejich odesílání.

stats.json v profilu drží:
  id          náhodný identifikátor instalace (nic z něj nejde odvodit)
  installed   kdy se čítače založily
  last_used   poslední otevření doplňku
  last_sent   poslední úspěšné odeslání
  next_try    kdy má smysl zkusit odeslání znovu

Posílá se kumulativní stav, ne přírůstky — server dělá upsert, takže výpadek
sítě ani ztracená odpověď nic nerozhodí. Zapisuje jen služba na pozadí
(`service.py`); plugin jí události předává přes vlastnost okna, aby dva procesy
nepsaly do stejného souboru.
"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

# Sběrný bod je natvrdo v kódu, ne v nastavení — je to detail implementace,
# ne něco, co by měl kdokoli přepínat. Změna adresy = nová verze.
COLLECT_URL = "https://nokturno.full-net.cz/collect"

SEND_EVERY = 6 * 3600     # nejčastěji jednou za 6 hodin
RETRY_EVERY = 30 * 60     # po neúspěchu (server neběží, není síť) nezkoušet hned znovu
TIMEOUT = 10


class Stats:
    def __init__(self, directory):
        self.path = os.path.join(directory, "stats.json")
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not data.get("id"):
            data = {"id": uuid.uuid4().hex, "installed": int(time.time())}
        return data

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # --- sběr ---------------------------------------------------------------------

    def note_use(self, when=None):
        self.data["last_used"] = int(when or time.time())
        self._save()

    # --- odesílání ----------------------------------------------------------------

    def due(self):
        return time.time() >= (self.data.get("next_try") or 0)

    def payload(self, version="", platform="", kodi="", lang=""):
        return {
            "id": self.data["id"],
            "version": version,
            "platform": platform,
            "kodi": kodi,
            "lang": lang,
            "installed": self.data.get("installed"),
            "last_used": self.data.get("last_used"),
        }

    def send(self, url, version="", platform="", kodi="", lang="", agent="Kodi plugin.video.nokturno"):
        """Odešle stav. Vrací (True, "") nebo (False, důvod) — nikdy nevyhodí výjimku.

        `agent` odlišuje odesílatele v přístupovém logu serveru; tentýž modul
        používá i integrace pro Home Assistant (viz `lib/stats.py` v nokturno-ha).
        """
        if not url:
            return False, "chybí adresa"
        body = json.dumps(self.payload(version, platform, kodi, lang)).encode("utf-8")
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
        self.data["last_sent"] = now
        self.data["next_try"] = now + SEND_EVERY
        self._save()
        return True, ""

    def _failed(self, why):
        self.data["next_try"] = int(time.time()) + RETRY_EVERY
        self._save()
        return False, why
