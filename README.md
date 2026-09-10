# Nokturno — filmy a seriály z WebShare, Sosáče a Luny pro Kodi

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/matata86)

Video doplněk pro Kodi (19+ / Python 3, testováno na Kodi 21 Omega, CoreELEC). Tři rovnocenné zdroje, každý jde zapnout samostatně:

| zdroj | co dává | co potřebuje |
|---|---|---|
| **WebShare přímo** | hledání souborů na WebShare a přehrávání, bez dalšího serveru | účet WebShare (jméno + heslo) |
| **Sosáč** | katalogy Sosáče (nejpopulárnější, nově přidané, žánry, podle písmene), filmy i seriály s epizodami, CZ dabing, CZ titulky ze streamuj | účet **Streamuj.tv** (jméno + heslo) — nic víc; katalogy jsou veřejné, k Sosáči se nepřihlašuje, Stremio není potřeba |
| **Luna: Absolute Cinema** | TMDB katalogy (trendy, populární, podle roku, žánru…), streamy z WebShare s rozpoznanou kvalitou a jazyky | běžící server [Luna](https://stremio.cz/d/47-luna-absolute-cinema-addon-pro-prehravani-sifrovaneho-obsahu-z-webshare) v LAN (např. jako [addon Home Assistantu](https://github.com/matata86/ha-addons)) |

Přihlašovací údaje zůstávají v Kodi — doplněk je posílá jen službě, ke které patří (WebShare, Streamuj, Luna).

## Co umí

- **Hledat** napříč zapnutými zdroji — jeden dotaz pro filmy i seriály; volba typu se nabídne, jen když dotaz najde obojí. Stejný titul z více zdrojů jen jednou, zdroj je vidět až ve výběru streamu
- **rok v dotazu je filtr** — „Pět švestek 2026“ vrátí jen film z roku 2026; číslo, které patří k názvu („2012“, „Blade Runner 2049“), se jako rok nebere
- **Hledat na WebShare** — soubory přímo z WebShare API (řazení: relevance / nejnovější / hodnocení / velikost)
- **katalogy** Luny (TMDB) i Sosáče; seriály → série → epizody s plakáty, popisy, hodnocením, obsazením
- **streamy z více zdrojů u jednoho titulu** (WebShare přes Lunu, fulltext WebShare, Sosáč) s popisem kvality, bitrate, velikosti, jazyků zvuku a titulků; filtr (preferovaný jazyk, skrýt SD, max. velikost) a řazení, nebo automaticky nejlepší
- **Pokračovat ve sledování** (rozkoukané + další díl), **Můj seznam**, **Naposledy zhlédnuté**, historie hledání, zhlédnuto/rozkoukáno (i bez Kodi knihovny)
- **Stahování** streamů i souborů na pozadí do zvolené složky
- **Trakt.tv** scrobble (vlastní client id/secret), IMDb id pro doplňky titulků, cesty pro widgety skinu
- **hodnocení v procentech** — položky nesou vlastnost `RatingPercent` („58 %“) vedle běžného ratingu, takže ji skin může ukázat místo hvězdiček
- **anonymní statistiky** používání, které jdou v nastavení vypnout (viz níže)
- **Up Next** — je-li nainstalovaná služba [Up Next](https://kodi.tv/addons/omega/service.upnext), dostane u seriálů informaci o dalším dílu a ke konci epizody nabídne jeho přehrání

## Předpoklady

Aspoň jeden zdroj z tabulky výše — účet WebShare, účet Sosáče, nebo server Luna v LAN.

## Instalace

**Doporučeno – přes repozitář (automatické aktualizace):**

1. Stáhni **[repository.nokturno.zip](https://github.com/matata86/plugin.video.nokturno/raw/main/repo/repository.nokturno/repository.nokturno.zip)** (vždy aktuální verze repozitáře).
2. Kodi → Doplňky → Instalovat ze souboru ZIP (musí být povoleno „Neznámé zdroje“) → vyber ten zip.
3. Doplňky → Instalovat z repozitáře → **Nokturno repozitář** → Video doplňky → **Nokturno** → Instalovat.
4. Od té doby Kodi nové verze stahuje samo (nebo je nabídne, podle nastavení aktualizací).

**Ručně:** stáhni `plugin.video.nokturno-x.y.z.zip` z [Releases](https://github.com/matata86/plugin.video.nokturno/releases) → Kodi → Doplňky → Instalovat ze souboru ZIP.

## Nastavení zdrojů

Zapni, co máš — jeden, dva nebo všechny tři:

- **WebShare (přímo)** — jméno + heslo k WebShare (nebo 40znakový salted hash, který používá WebShare doplněk pro Stremio).
- **Sosáč** — jméno + heslo ke **Streamuj.tv** (přehrávač Sosáče). Katalogy a hledání jdou z veřejných JSON exportů `tv.sosac.to`, streamy ze `streamuj.tv` — stejně jako oficiální Kodi doplněk Sosáče. Starší režim přes Stremio doplněk Sosáče (`userId`) zůstává v nastavení jako záloha.
- **Luna** — otevři setup stránku Luny (`http://IP-Luny:7126/setup`), zkopíruj **adresu doplňku** (`…/e1.XXXX/manifest.json`) a vlož ji do pole *Adresa doplňku nebo token*; adresa serveru se z ní vezme sama.

## Struktura

```
addon.xml
default.py                    # router a obrazovky Kodi
resources/lib/luna_api.py     # klient API Luny (bez závislosti na Kodi, jde spustit samostatně)
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
| název, rok, typ a počet, kolikrát se u titulu zobrazily streamy | co je nejvíc žádané |

Počítá se zobrazení streamů, ne přehrání. Spousta streamů nejde přehrát vůbec
(mrtvý odkaz, region, chybějící titulky), a to nic nevypovídá o tom, jak moc
je titul žádaný — jen o kvalitě zrovna toho jednoho odkazu.

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

Data chodí na `https://nokturno.full-net.cz/collect`. Adresa je v nastavení —
kdo chce statistiky posílat jinam nebo nikam, přepíše ji. Hlášení je jeden POST s tímto tělem, takže si vlastní
sběrač napíše kdokoli:

```json
{
  "id": "náhodných 32 hex znaků",
  "version": "1.5.18", "platform": "Android", "kodi": "21.1", "lang": "cs",
  "installed": 1786000000, "last_used": 1788970000, "plays_total": 5,
  "plays": [{"key": "tt0133093", "t": "Matrix", "y": 1999, "k": "movie", "c": 3, "l": 1788970000}]
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

## Podpora

Pomohlo ti to? Kafe autorovi udělá radost ☕

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/matata86)

**https://ko-fi.com/matata86**
