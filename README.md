![Nokturno](resources/media/fanart.jpg)

# Nokturno — filmy a seriály z WebShare, Sosáče, Luny a HellSpy pro Kodi


Podrobný návod (instalace, nastavení každého zdroje, používání, řešení problémů) je ve [wiki](https://github.com/matata86/plugin.video.nokturno/wiki).

Video doplněk pro Kodi (19+ / Python 3, testováno na Kodi 21 Omega, CoreELEC). Čtyři rovnocenné zdroje streamů, každý jde zapnout samostatně:

| zdroj | co dává | co potřebuje |
|---|---|---|
| **WebShare přímo** | hledání souborů na WebShare a přehrávání, bez dalšího serveru | účet WebShare (jméno + heslo) |
| **Sosáč** | katalogy Sosáče (nejpopulárnější, nově přidané, žánry, podle písmene), filmy i seriály s epizodami, CZ dabing, CZ titulky ze streamuj | účet **Streamuj.tv** (jméno + heslo) — nic víc; katalogy jsou veřejné, k Sosáči se nepřihlašuje, Stremio není potřeba |
| **Luna: Absolute Cinema** | TMDB katalogy (trendy, populární, podle roku, žánru…), streamy z WebShare s rozpoznanou kvalitou a jazyky | běžící server [Luna](https://stremio.cz/d/47-luna-absolute-cinema-addon-pro-prehravani-sifrovaneho-obsahu-z-webshare) v LAN (např. jako [addon Home Assistantu](https://github.com/matata86/ha-addons)) |
| **HellSpy** | fulltextové hledání souborů na hellspy.to a přehrávání původních souborů | nic, rozhraní je veřejné |

Přihlašovací údaje zůstávají v Kodi — doplněk je posílá jen službě, ke které patří (WebShare, Streamuj, Luna).

### Vlastní databáze filmů a seriálů

Katalog (*Filmy* / *Seriály*) a hledání titulů běžely dřív jen přes Lunu (nebo přihlášený Sosáč) — bez nich se dřív ani nezobrazily v menu. Teď se použije řetězec zdrojů metadat, v tomhle pořadí (každý se zkusí, jen když předchozí nic nevrátil):

1. **TMDB** — jakmile má uživatel vlastní zdarma klíč (viz *Nastavení → Vlastní databáze filmů a seriálů* → nápověda s návodem), má přednost **i před Lunou** — umí česky i to, co Luna neřekne (popis, obsazení). Luna zůstává zdrojem streamů, ne metadat.
2. **Luna** — bez TMDB klíče, když je dostupná (beze změny oproti dřívějšku)
3. **Veřejný katalog Sosáče** — bez TMDB i Luny, bez účtu, české tituly a žánry, ale bez popisu
4. **Cinemeta** — poslední záchrana, funguje vždy, ale jen anglicky

Streamy samotné (WebShare/HellSpy/Luna) se pak hledají stejně jako dřív — vlastní databáze řeší jen "co je to za titul", ne odkud stream stáhnout.

## Co umí

- **Průvodce prvním nastavením** — hned po instalaci doplněk sám provede vyplněním zdrojů (WebShare, Sosáč, Luna, HellSpy, TMDB klíč) i změřením rychlosti internetu pro nastavení datového toku, ať není potřeba předem vědět, co a kde v nastavení hledat. Jde přeskočit a kdykoli znovu spustit z *Nastavení → Pokročilé*. Stávající instalace (aktualizace ze starší verze) se nabízet nezačne — pozná se podle už zapnutého zdroje.
- **Přesná hláška, když zdroj neodpoví** — jmenuje konkrétní zdroj (WebShare, Luna, Sosáč…), ne obecnou chybu; u vícezdrojového hledání jde o blokující dialog, ne mizící upozornění, takže se snadno nepřehlédne. Výsledky ze zbylých fungujících zdrojů se po potvrzení zobrazí normálně.
- **Hledat** napříč zapnutými zdroji — jeden dotaz pro filmy i seriály; volba typu se nabídne, jen když dotaz najde obojí. Stejný titul z více zdrojů jen jednou, zdroj je vidět až ve výběru streamu
- **rok v dotazu je filtr** — „Pět švestek 2026“ vrátí jen film z roku 2026; číslo, které patří k názvu („2012“, „Blade Runner 2049“), se jako rok nebere
- **Hledat na WebShare** — soubory přímo z WebShare API (řazení: relevance / nejnovější / hodnocení / velikost)
- **katalogy** Luny (TMDB) i Sosáče; seriály → série → epizody s plakáty, popisy, hodnocením, obsazením
- **streamy z více zdrojů u jednoho titulu** — Luna, přímý fulltext WebShare, Sosáč i HellSpy se prohledají **souběžně** a stejný soubor nalezený víc cestami se ukáže jen jednou; u každého streamu je zdroj, kvalita (u souborů bez kvality v názvu odhad podle velikosti se značkou `~`), datový tok, délka, velikost a jazyky zvuku i titulků — zjištěné ze zdroje, nebo dočtené z hlavičky souboru a označené `~`, když jde jen o odhad. Řazení podle nastavení, nebo se pustí automaticky nejlepší
- **Zkusit fulltext na WebShare/HellSpy** — tlačítko dole v seznamu streamů spustí uvolněnější hledání pro případ, že přísný filtr (chrání proti nabídnutí úplně jiného titulu, který hledaná slova jen náhodou obsahuje) zahodil skutečnou shodu; takové výsledky jsou označené jako neověřené
- **Filtr streamů** přímo v seznamu — podle kvality, jazyka zvuku, počtu kanálů (5.1 a víc), kodeku, titulků i zdroje; nabízí jen to, co se v aktuálním seznamu skutečně vyskytuje, s počtem nalezeného v závorce
- **Max. datový tok** místo pevné velikosti v GB — nastavení umí i změřit rychlost internetu a spočítat dovolený tok s 25% rezervou; skutečná velikost se pak dopočítá podle stopáže právě otevřeného titulu, ne podle jednoho čísla pro všechno
- **Pokračovat ve sledování** (rozkoukané + další díl), **Můj seznam**, **Naposledy zhlédnuté**, historie hledání (posledních 10 dotazů), zhlédnuto/rozkoukáno (i bez Kodi knihovny)
- **Zapamatovaný stream u seriálu** — jakmile si u seriálu jednou vybereš stream (zdroj, kvalitu, jazyk), další díly se pustí stejně bez ptaní; výběr se nabídne, jen když u dílu ta kombinace chybí
- **Značka dalšího dílu** — v seznamu epizod je `»` u prvního nezhlédnutého dílu, který navazuje na poslední zhlédnutý
- **Otestovat zdroje** — tlačítko v *Nastavení → Pokročilé* ověří Lunu, Sosáč i přihlášení k WebShare a řekne, co nefunguje, bez čekání na prázdný seznam streamů
- **Rychlejší procházení** — služba na pozadí drží načtené katalogy pro domovskou obrazovku a předstahuje streamy dalšího dílu rozkoukaných seriálů, takže se otevírají hned
- **Stahování** streamů i souborů na pozadí do zvolené složky
- **Trakt.tv** scrobble (vlastní client id/secret), IMDb id pro doplňky titulků, cesty pro widgety skinu
- **hodnocení v procentech** — položky nesou vlastnost `RatingPercent` („58 %“) vedle běžného ratingu, takže ji skin může ukázat místo hvězdiček
- **anonymní statistiky** používání, které jdou v nastavení vypnout (viz níže)
- **Synchronizace mezi více Kodi** — zhlédnuto, rozkoukané (i pozice) a Můj seznam se sdílí přes integraci [Nokturno pro Home Assistant](https://github.com/matata86/nokturno-ha): v HA si v nastavení integrace opiš klíč, v Kodi zapni *Nastavení → Synchronizace*, vyplň adresu HA a klíč. Běží na pozadí, ručně přes „Synchronizovat teď" v menu
- **Up Next** — je-li nainstalovaná služba [Up Next](https://kodi.tv/addons/omega/service.upnext), dostane u seriálů informaci o dalším dílu a ke konci epizody nabídne jeho přehrání (a s zapamatovaným streamem ho pustí rovnou)
- **Sledování předplatného WebShare** — tlačítko v nastavení ukáže, kolik dní zbývá; upozornění pár dní před koncem a pak každý den, dokud předplatné nevyprší
- **Žánr v popisu titulu** — tučně na začátku, přeložený do češtiny i u titulů z anglicky mluvících zdrojů
- **Nápověda ke každé volbě v nastavení** — dole v dialogu se zobrazí, co položka dělá, ne jen její název

## Předpoklady

Katalog a hledání titulů fungují i úplně bez nastavení (vlastní databáze, viz výš). Pro skutečné streamy je ale potřeba aspoň jeden zdroj z tabulky výše — účet WebShare, účet Sosáče, server Luna v LAN, nebo prostě zapnout HellSpy (veřejný, nic nepotřebuje).

## Instalace

**Doporučeno – přes repozitář (automatické aktualizace).** Nejdřív je potřeba
povolit Nastavení → Systém → Doplňky → **Neznámé zdroje**.

*Přímo v Kodi, bez prohlížeče (vhodné pro TV a set-top boxy):*

1. Nastavení → Správce souborů → **Přidat zdroj** → jako adresu zadej
   `https://nokturno.full-net.cz/repo/` a pojmenuj ji třeba `Nokturno`.
2. Doplňky → **Instalovat ze souboru ZIP** → `Nokturno` → `repository.nokturno`
   → `repository.nokturno.zip`.
3. Doplňky → Instalovat z repozitáře → **Nokturno repozitář** → Video doplňky →
   **Nokturno** → Instalovat.

*Nebo se ZIPem staženým v prohlížeči:* stáhni
**[repository.nokturno.zip](https://github.com/matata86/plugin.video.nokturno/raw/main/repo/repository.nokturno/repository.nokturno.zip)**,
přenes ho do zařízení s Kodi a pokračuj od kroku 2.

Od té doby Kodi nové verze stahuje samo (nebo je nabídne, podle nastavení
aktualizací). Kontrola běží jednou denně; hned si ji vyžádáš místní nabídkou na
**Nokturno repozitář** → *Zkontrolovat aktualizace*.

**Ručně, bez repozitáře:** stáhni `plugin.video.nokturno-x.y.z.zip` z
[Releases](https://github.com/matata86/plugin.video.nokturno/releases) → Kodi →
Doplňky → Instalovat ze souboru ZIP.

**Návrat na starší verzi:** místní nabídka na doplňku → *Informace* → **Verze**;
repozitář nabízí i předchozí vydání.

## Nastavení zdrojů

Zapni, co máš — jeden, víc, nebo všechny čtyři:

- **WebShare (přímo)** — jméno + heslo k WebShare (nebo 40znakový salted hash, který používá WebShare doplněk pro Stremio).
- **Sosáč** — jméno + heslo ke **Streamuj.tv** (přehrávač Sosáče). Katalogy a hledání jdou z veřejných JSON exportů `tv.sosac.to`, streamy ze `streamuj.tv` — stejně jako oficiální Kodi doplněk Sosáče. Starší režim přes Stremio doplněk Sosáče (`userId`) zůstává v nastavení jako záloha.
- **Luna** — otevři setup stránku Luny (`http://IP-Luny:7126/setup`), zkopíruj **adresu doplňku** (`…/e1.XXXX/manifest.json`) a vlož ji do pole *Adresa doplňku nebo token*; adresa serveru se z ní vezme sama.
- **HellSpy** — jen přepínač v nastavení, rozhraní je veřejné a účet nepotřebuje.
- **Vlastní databáze filmů a seriálů (TMDB)** — nepovinné, ale s klíčem má přednost i před Lunou (viz výš): zdarma klíč z [themoviedb.org](https://www.themoviedb.org/signup) → ikona profilu → *Nastavení* → *API* → *Request an API Key* → *Developer* → krátký formulář → zkopíruj **API Key (v3 auth)** (ne delší "API Read Access Token") do *Nastavení → Vlastní databáze filmů a seriálů*. Bez klíče se použije Luna (je-li dostupná), jinak zdarma veřejný katalog Sosáče a Cinemeta, ale bez českého popisu.

## Struktura

```
addon.xml
default.py                    # router a obrazovky Kodi
resources/lib/luna_api.py     # klient API Luny (bez závislosti na Kodi, jde spustit samostatně)
resources/lib/cinemeta_api.py # vlastní databáze: Cinemeta (Stremio), poslední záchrana bez klíče/účtu
resources/lib/tmdb_api.py     # vlastní databáze: TMDB s vlastním klíčem uživatele (česky, s popisem)
resources/lib/sosac_direct.py # Sosáč napřímo: veřejné JSONy tv.sosac.to + streamy/titulky ze streamuj.tv
resources/lib/sosac_api.py    # starší režim přes Stremio API Sosáče + párování názvů pro hledání napříč
resources/lib/webshare_api.py # přímý klient WebShare API (login s md5crypt/sha1, hledání, odkaz)
resources/lib/stats.py        # čítače používání a jejich odesílání (bez závislosti na Kodi)
resources/lib/store.py        # historie hledání + zhlédnuto/rozkoukáno (JSON v profilu)
resources/lib/streams.py      # rozbor, filtr a řazení streamů
resources/lib/trakt_api.py    # Trakt.tv (device code, scrobble, historie)
service.py                    # služba: zhlédnuto/pozice, Trakt scrobble, stahování, statistiky
repository.nokturno/          # repozitář pro automatické aktualizace
tools/build_repo.py           # sestaví repo/ (addons.xml, md5, zipy) po změně verze
resources/settings.xml
resources/language/…          # en_GB, cs_CZ
```

Test klientů bez Kodi: `python3 resources/lib/luna_api.py http://IP:7126 e1.XXXX`, `python3 resources/lib/sosac_direct.py <streamuj_user> <streamuj_heslo>`

## Anonymní statistiky

Doplněk umí hlásit, jak se používá. Slouží to k jedinému: vědět, kolik lidí ho
má, na čem běží a o co je zájem. Sběr je ve výchozím stavu zapnutý a vypíná se
jedním přepínačem v *Nastavení → Statistiky*.

**Co se posílá**

| Údaj | K čemu |
|---|---|
| náhodné id instalace | odlišení zařízení, negeneruje se z ničeho, co by šlo zpětně přiřadit |
| verze doplňku, verze Kodi, platforma, jazyk | na čem doplněk běží |
| kdy se čítače založily a poslední použití | kolik instalací je živých |
| id, název a rok titulů, u kterých se zobrazily streamy, a kdy naposledy | které tituly jsou žádané — server si sám odvodí „kolik různých dnů", vícekrát za den se nepočítá |

**Co se neposílá:** žádné přihlašovací údaje ke zdrojům, žádná IP adresa,
žádný obsah hledání, nic z Traktu, nic ze stahování.

Data odesílá služba na pozadí, nejvýš jednou za 6 hodin, v jednom malém POST
požadavku. Posílá se kumulativní stav, ne přírůstky — když se odeslání
nepovede, nic se neztratí a nic se nezapočítá dvakrát. Selhání se nikde
neprojeví, doplněk kvůli statistikám nikdy nečeká.

Navíc se pošle i při vypnutí Kodi, restartu doplňku po změně nastavení nebo
jeho zakázání — zpřesní to čas posledního vidění o hodiny až šest. Skutečnou
odinstalaci (smazání složky doplňku) tohle zachytit nemůže, protože při ní
žádný kód doplňku neběží. Server proto instalaci, která se dlouho neozvala,
sám počítá jako mrtvou (`dead_after` v konfiguraci sběrného bodu).

Data chodí na `https://nokturno.full-net.cz/collect`. Odesílání se vypíná
přepínačem v nastavení. Hlášení je jeden POST s tímto tělem, takže je vidět
přesně, co odchází:

```json
{
  "id": "náhodných 32 hex znaků",
  "version": "1.5.18", "platform": "Android", "kodi": "21.1", "lang": "cs",
  "installed": 1786000000, "last_used": 1788970000
}
```

Očekávaná odpověď je `{"ok": true}`.

## Nokturno v Home Assistantu

Stejné zdroje umí i [**integrace Nokturno pro Home Assistant**](https://github.com/matata86/nokturno-ha) (instalace přes HACS). Hledá ve WebShare, Sosáči a Luně, výsledky pouští **v Kodi právě přes tenhle doplněk** (`plugin://plugin.video.nokturno/…`), takže titul skončí v „Pokračovat ve sledování" a Kodi si pamatuje pozici. Navíc umí stáhnout film do Home Assistantu nebo poslat odkaz do mobilu.

[![Otevřít repozitář v HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=matata86&repository=nokturno-ha&category=integration)
[![Přidat integraci](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nokturno)
[![Přidat repozitář s doplňky](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmatata86%2Fha-addons)

Tlačítka otevřou tvoji instanci: první přidá integraci do HACS, druhé spustí její nastavení, třetí přidá repozitář s addony (server **Luna** jako doplněk HA).

Účty se nastavují stejné jako tady; obě aplikace sdílejí knihovny zdrojů, takže se chovají shodně.

## Licence

MIT

---

