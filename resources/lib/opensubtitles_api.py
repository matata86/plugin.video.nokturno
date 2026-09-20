"""Klient OpenSubtitles.com (REST API v1) — titulky k titulům, které je nemají ze zdroje.

Proč vůbec: měřeno na ostré cache Stremia (2026-09-20, 3 363 výpisů streamů) nemá
**36,7 %** titulů žádné české ani slovenské titulky a **14,5 %** ani dabing, ani
titulky. Dosavadní záloha — hledání `.srt` ve fulltextu WebShare (`engine
._webshare_subtitles`) — zachrání 6,9 procentního bodu; zbytek je díra, kterou
zavírá právě tenhle zdroj (v aktuálním žebříčku má CZ nebo SK titulky 83 % filmů
a 92 % seriálů, a jde skoro výhradně o lidské překlady, ne strojové).

Jak se to liší od WebShare: tam se hledá jménem souboru ve volném fulltextu a
číslo dílu je jen nápověda, tady jde dotaz na **IMDb id** a u seriálu na sezónu
a díl zvlášť, takže se titulky k jinému dílu nemůžou přichytit (chyba z 5.2.34) —
a `_epizoda_sedi()` to navíc ověřuje z `feature_details` v odpovědi, ne z názvu.
Druhá cesta je **otisk souboru** (`moviehash`): OpenSubtitles jím páruje titulky
přímo s konkrétním souborem, takže sedí i časově. Otisk se počítá z prvních a
posledních 64 kB — začátek už kvůli hlavičce čte `mediainfo.probe()`, konec stojí
jediný `Range` dotaz navíc.

Co API vyžaduje (ověřeno živě 2026-09-20, ne jen z dokumentace):

* **Klíč je povinný na každý dotaz.** Bez hlavičky `Api-Key` projde jen holé
  `?imdb_id=…`, a to jen když odpověď náhodou leží na CDN; `languages`, `query`,
  `moviehash` i `page` vrací `403 {"message":"You cannot consume this service"}`.
* **Parametry musí být v dotazu seřazené abecedně**, jinak přijde `301` na
  kanonický tvar (server si tím drží cache). Proto `_dotaz()` řadí vždy.
* **Kvóta se počítá na stahování, ne na hledání**: 5 souborů na IP za 24 hodin
  bez přihlášení, 20 s běžným účtem, u VIP bez omezení. Hledat jde zadarmo
  (limit je 5 dotazů za sekundu). Stahuje se proto až v okamžiku přehrání a
  nejvýš `MAX_KE_STAZENI` souborů na titul.

Přihlášení je dobrovolné: bez něj funguje pět titulků denně, s účtem dvacet.
Heslo se posílá jen do `/login` a nikam se neukládá — v profilu leží jen vydaný
token a ten se do logu nedostane (`crash.scrub()` maskuje `Bearer`).

Bez závislostí na hostiteli — jde zkusit samostatně:
    NOKTURNO_OS_KEY=… python3 opensubtitles_api.py tt0133093 | tt14688458 1 1
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from tracks import LANG_CODES

BASE = "https://api.opensubtitles.com/api/v1"
TIMEOUT = 8
SEARCH_TTL = 6 * 3600       # titulky k titulu přibývají po dnech, ne po minutách
DOWN_TTL = 300              # po výpadku se síť takhle dlouho nezkouší (jako u DashApi)
DOWN_KEY = "nokturno:opensubtitles:down"
TOKEN_NAME = "opensubtitles_session"
TOKEN_TTL = 20 * 3600       # token API platí 24 h; obnovujeme dřív, ať nevyprší za chodu
MAX_VYSLEDKU = 5            # kolik kandidátů si klient nechá (stahuje se pak nejvýš MAX_KE_STAZENI)
MAX_KE_STAZENI = 1          # kvóta je 5 souborů na den — víc než jeden titulek na titul si nedovolíme
ODSTUP = 0.25               # sekundy mezi dotazy; API pouští 5 za sekundu
BLOK = 64 * 1024            # otisk souboru počítá z prvních a posledních 64 kB
MIN_VELIKOST = 2 * BLOK     # menší soubor nemá z čeho otisk spočítat

# OpenSubtitles mluví ISO 639-1 (`cs`, `sk`), naše kódy jsou `CZ`/`SK`/`EN`/`HU`.
# Překlad se bere z `tracks.LANG_CODES`, ať nevznikne druhá tabulka, která se rozejde:
# bez něj by se `cs` nepoznalo jako čeština a české titulky by v pořadí spadly za slovenské.
NA_NAS_KOD = {kod: nas for nas, kody in LANG_CODES.items() for kod in kody}
DO_ISO = {"CZ": "cs", "SK": "sk", "EN": "en", "HU": "hu"}

_LOGGER = logging.getLogger(__name__)


class OpenSubtitlesError(Exception):
    pass


class KvotaVycerpana(OpenSubtitlesError):
    """Denní strop stahování. Hledat jde dál, stáhnout ne — má smysl to říct uživateli."""


def otisk(zacatek, konec, velikost):
    """Otisk souboru podle OpenSubtitles (velikost + součet prvních a posledních 64 kB).

    Sčítá se po osmi bajtech jako little-endian, v 64 bitech. Vrátí prázdno, když
    soubor na otisk nestačí nebo výřezy nedošly celé — počítat ho z kratšího kusu
    nejde, vyšlo by jiné číslo než tomu, kdo má soubor na disku.
    """
    if velikost < MIN_VELIKOST or len(zacatek) < BLOK or len(konec) < BLOK:
        return ""
    soucet = velikost & 0xFFFFFFFFFFFFFFFF
    for kus in (zacatek[:BLOK], konec[-BLOK:]):
        for i in range(0, BLOK, 8):
            soucet = (soucet + int.from_bytes(kus[i:i + 8], "little")) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % soucet


def _cislo(hodnota):
    try:
        return int(hodnota)
    except (TypeError, ValueError):
        return 0


class OpenSubtitlesApi:
    """Hledání a stahování titulků. `store` je `Store` doplňku (cache a token), může chybět."""

    def __init__(self, api_key, store=None, username="", password="", user_agent="Nokturno v1.0",
                 opener=None):
        self.api_key = (api_key or "").strip()
        self.store = store
        self.username = (username or "").strip()
        self.password = password or ""
        self.user_agent = user_agent
        self._opener = opener
        self._token = ""
        self._posledni = 0.0

    # --- síť ---------------------------------------------------------------

    def _pauza(self):
        """API pouští 5 dotazů za sekundu; víc vrací 429 a blokuje na chvíli celý klíč."""
        zbytek = ODSTUP - (time.monotonic() - self._posledni)
        if zbytek > 0:
            time.sleep(zbytek)
        self._posledni = time.monotonic()

    def _dotaz(self, cesta, params=None, telo=None, s_tokenem=True):
        if not self.api_key:
            raise OpenSubtitlesError("Není klíč k OpenSubtitles.")
        url = BASE + cesta
        if params:
            # parametry musí být seřazené abecedně, jinak server odpoví 301 na kanonický tvar
            cisté = {k: v for k, v in params.items() if v not in ("", None)}
            url += "?" + urllib.parse.urlencode(sorted(cisté.items()))
        hlavicky = {
            "Api-Key": self.api_key,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if telo is not None:
            hlavicky["Content-Type"] = "application/json"
        token = self._prihlas() if s_tokenem else ""
        if token:
            hlavicky["Authorization"] = "Bearer " + token
        data = json.dumps(telo).encode("utf-8") if telo is not None else None
        req = urllib.request.Request(url, data=data, headers=hlavicky)
        self._pauza()
        try:
            otevreno = self._opener.open(req, timeout=TIMEOUT) if self._opener \
                else urllib.request.urlopen(req, timeout=TIMEOUT)
            with otevreno as odpoved:
                return json.loads(odpoved.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as err:
            if err.code == 406:
                raise KvotaVycerpana("Denní limit stahování z OpenSubtitles je vyčerpaný.") from err
            if err.code == 401 and s_tokenem and self._token:
                self._zapomen_token()   # token vypršel dřív, než jsme čekali
            # tělo chyby může nést jméno účtu nebo id souboru, do hlášky jde jen kód
            raise OpenSubtitlesError("HTTP %s" % err.code) from err
        except urllib.error.URLError as err:
            raise OpenSubtitlesError(str(err.reason)) from err
        except ValueError as err:
            raise OpenSubtitlesError("Odpověď nedává smysl: %s" % err) from err

    # --- přihlášení --------------------------------------------------------

    def _zapomen_token(self):
        self._token = ""
        if self.store is not None:
            try:
                self.store.save(TOKEN_NAME, {})
            except Exception:  # noqa: BLE001 – profil jen pro čtení nesmí shodit hledání
                pass

    def _prihlas(self):
        """Token účtu, když je vyplněné jméno a heslo. Bez účtu se vrací prázdno —
        API to bere, jen s nižší kvótou (5 souborů na IP za den místo 20)."""
        if not (self.username and self.password):
            return ""
        if self._token:
            return self._token
        if self.store is not None:
            ulozeno = self.store.load(TOKEN_NAME, {}) or {}
            if ulozeno.get("user") == self.username and ulozeno.get("do", 0) > time.time():
                self._token = str(ulozeno.get("token") or "")
                if self._token:
                    return self._token
        odpoved = self._dotaz("/login", telo={"username": self.username, "password": self.password},
                              s_tokenem=False)
        self._token = str(odpoved.get("token") or "")
        if not self._token:
            raise OpenSubtitlesError("Přihlášení k OpenSubtitles se nepovedlo.")
        if self.store is not None:
            try:
                self.store.save(TOKEN_NAME, {"user": self.username, "token": self._token,
                                             "do": time.time() + TOKEN_TTL})
            except Exception:  # noqa: BLE001
                pass
        return self._token

    # --- hledání -----------------------------------------------------------

    @staticmethod
    def _epizoda_sedi(polozka, season, episode):
        """Díl ověřený z `feature_details`, ne z názvu souboru.

        Titulky k jinému dílu se u WebShare přichytávaly proto, že se filtrovalo
        jménem souboru (5.2.34). Tady server sezónu i díl zná, takže se ptáme jeho;
        když je neuvádí (film, nebo neúplný záznam), položka se nezahazuje — jazyk
        i díl se stejně pozná až z textu při stažení.
        """
        if not (season and episode):
            return True
        detail = polozka.get("feature_details") or {}
        s, e = detail.get("season_number"), detail.get("episode_number")
        if s is None and e is None:
            return True
        return _cislo(s) == int(season) and _cislo(e) == int(episode)

    def _polozky(self, data, season, episode):
        out = []
        for radek in (data.get("data") or []):
            if not isinstance(radek, dict):
                continue
            atr = radek.get("attributes") or {}
            if not self._epizoda_sedi(atr, season, episode):
                continue
            soubory = [f for f in (atr.get("files") or []) if isinstance(f, dict) and f.get("file_id")]
            if not soubory:
                continue
            out.append({
                "file_id": _cislo(soubory[0].get("file_id")),
                "lang": NA_NAS_KOD.get(str(atr.get("language") or "").strip().lower(), ""),
                "release": str(atr.get("release") or "")[:120],
                "downloads": _cislo(atr.get("download_count")),
                "hash_match": bool(atr.get("moviehash_match")),
                # strojový překlad je u češtiny vzácný (7 ze 116 ve vzorku), ale když
                # je vedle lidského, patří za něj
                "machine": bool(atr.get("machine_translated") or atr.get("ai_translated")),
            })
        return out

    @staticmethod
    def _poradi(polozka, jazyky):
        """Menší číslo = dřív. Otisk souboru přebíjí všechno ostatní — takové titulky
        sedí přímo k tomuhle souboru, ne jen k titulu."""
        try:
            jazyk = jazyky.index(polozka["lang"])
        except ValueError:
            jazyk = len(jazyky)
        return (0 if polozka["hash_match"] else 1, jazyk, 1 if polozka["machine"] else 0,
                -polozka["downloads"])

    def hledej(self, imdb_id="", jazyky=(), season=0, episode=0, moviehash=""):
        """Kandidáti na titulky, seřazení od nejlepšího. Nikdy nevyhodí výjimku ze sítě —
        titulky jsou příslušenství, výpadek nesmí shodit výpis streamů."""
        if not self.api_key or not (imdb_id or moviehash):
            return []
        if self._vypadek():
            return []
        jazyky = [j.upper() for j in jazyky if j]
        params = {"languages": ",".join(DO_ISO[j] for j in jazyky if j in DO_ISO)}
        if moviehash:
            params["moviehash"] = moviehash
        if imdb_id:
            cislo = str(imdb_id)[2:].lstrip("0") if str(imdb_id).startswith("tt") else str(imdb_id)
            if season and episode:
                params["parent_imdb_id"] = cislo
                params["season_number"] = int(season)
                params["episode_number"] = int(episode)
            else:
                params["imdb_id"] = cislo
        try:
            data = self._dotaz("/subtitles", params=params)
        except OpenSubtitlesError as err:
            _LOGGER.debug("OpenSubtitles hledání: %s", err)
            self._znac_vypadek()
            return []
        polozky = self._polozky(data, season, episode)
        polozky.sort(key=lambda p: self._poradi(p, jazyky))
        return polozky[:MAX_VYSLEDKU]

    def hledej_cachovane(self, imdb_id="", jazyky=(), season=0, episode=0):
        """Jako `hledej()`, ale výsledek si na `SEARCH_TTL` pamatuje profil. Otisk
        souboru se necachuje — ten se ptá na konkrétní soubor, ne na titul."""
        if self.store is None:
            return self.hledej(imdb_id, jazyky, season, episode)
        klic = "os:%s:%s:%s:%s" % (imdb_id, ",".join(jazyky), season or 0, episode or 0)
        return self.store.cached_if(klic, SEARCH_TTL,
                                    lambda: self.hledej(imdb_id, jazyky, season, episode)) or []

    # --- stažení -----------------------------------------------------------

    def odkaz(self, file_id):
        """Dočasná adresa souboru s titulky. **Tohle spotřebuje kvótu** (5 na IP
        a den bez účtu), takže se volá až při přehrávání, ne při výpisu streamů."""
        data = self._dotaz("/download", telo={"file_id": int(file_id)})
        link = str(data.get("link") or "")
        if not link:
            raise OpenSubtitlesError("OpenSubtitles neposlal odkaz na soubor.")
        return link

    def ucet(self):
        """Stav účtu — `{"user": jméno, "zbyva": počet, "vip": bool}`, nebo None bez přihlášení.

        `GET /infos/user` chce token, takže bez vyplněného jména a hesla nemá co vrátit;
        kvóta se pak stejně počítá na IP a server ji nikde neříká."""
        if not (self.username and self.password):
            return None
        data = self._dotaz("/infos/user").get("data") or {}
        return {
            "user": str(data.get("nickname") or self.username),
            "zbyva": _cislo(data.get("remaining_downloads")),
            "vip": bool(data.get("vip")),
        }

    # --- výpadek -----------------------------------------------------------

    def _vypadek(self):
        return bool(self.store is not None and self.store.peek_cached(DOWN_KEY, DOWN_TTL))

    def _znac_vypadek(self):
        if self.store is not None:
            try:
                self.store.cached(DOWN_KEY, DOWN_TTL, lambda: 1)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":   # pragma: no cover – ruční zkouška
    import os
    import sys
    api = OpenSubtitlesApi(os.environ.get("NOKTURNO_OS_KEY", ""))
    argv = sys.argv[1:] or ["tt0133093"]
    sezona, dil = (int(argv[1]), int(argv[2])) if len(argv) >= 3 else (0, 0)
    for radek in api.hledej(argv[0], ("CZ", "SK"), sezona, dil):
        print(radek)
