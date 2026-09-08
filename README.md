# Nokturno (plugin.video.luna) — Luna: Absolute Cinema + Sosáč pro Kodi

Video doplněk pro Kodi (19+ / Python 3, testováno na Kodi 21 Omega, CoreELEC). Je klientem serveru **[Luna: Absolute Cinema](https://stremio.cz/d/47-luna-absolute-cinema-addon-pro-prehravani-sifrovaneho-obsahu-z-webshare)** — Stremio addon serveru pro streamování z WebShare.

Doplněk sám nic nestahuje ani se nepřihlašuje k WebShare. Volá HTTP API Luny (Stremio protokol) a streamy přehrává přes ni:

- **Filmy / Seriály** — katalogy TMDB z Luny: Trendy, Populární, Nejlépe hodnocené, Podle roku, Podle jazyka (s žánry)
- **Hledat film / Hledat seriál**
- seriály → série → epizody, s plakáty, popisy, hodnocením, obsazením
- výběr kvality streamu (4K HDR, Full HD, …) s bitrate, velikostí, jazyky zvuku a titulků — nebo automaticky nejlepší; streamy z obou zdrojů Luny (přesná shoda i fulltext WebShare, označeno `(WS)`)

### WebShare přímo (bez serveru Luna)

Kdo nemá kde provozovat Lunu, může doplněk používat jen s **účtem WebShare** — v nastavení zapni *WebShare (přímo)* a vyplň jméno a heslo (nebo 40znakový salted hash, který používá WebShare doplněk pro Stremio). Zdroj Luna jde vypnout. Pak:

- **Hledat na WebShare** — hledání souborů přímo přes WebShare API (řazení: relevance / nejnovější / hodnocení / velikost), přehrání přes stream odkaz
- **Hledat film / seriál** bez Luny prohledá Sosáč a rovnou přidá i soubory z WebShare

### Historie hledání a zhlédnuto

- každé hledání má **historii** (posledních 30 dotazů, položky jde jednotlivě odstranit nebo celou smazat)
- **zhlédnuto / rozkoukáno**: služba na pozadí sleduje přehrávání; nad 90 % označí titul fajfkou, jinak si pamatuje pozici a Kodi nabídne pokračování. V kontextovém menu jde stav přepnout ručně. Ukládá se do profilu doplňku (`addon_data/plugin.video.luna/watched.json`), takže nezávisí na měnících se adresách streamů.

### Sosáč jako druhý zdroj (volitelně)

Doplněk umí přidat i **[Sosáč](https://stremio.cz/d/46-oficialni-sosac-tv-stremio-addon)** přes jeho Stremio API (`stremio.sosac.tv`) — stačí `userId` z instalační adresy Sosáče pro Stremio, žádné přihlašování v Kodi:

- **Sosáč – filmy / seriály** — jeho vlastní katalogy (novinky s dabingem, populární, podle písmene, podle dabingu, rozkoukané…)
- **Hledat film / seriál** prohledá Lunu i Sosáč najednou (položky Sosáče jsou označené)
- u každého titulu se **streamy dohledají i v druhém zdroji** — film z TMDB katalogu Luny nabídne i CZ dabing ze Sosáče a naopak titul ze Sosáče dostane 4K z WebShare (párování podle názvu/originálního názvu a roku)

## Předpoklady

Aspoň jeden zdroj: server Luna v LAN (na PC, NAS, nebo jako addon Home Assistantu), účet WebShare, nebo Sosáč (userId ze Stremio adresy).

## Instalace

1. Stáhni `plugin.video.luna-x.y.z.zip` z [Releases](https://github.com/matata86/plugin.video.luna/releases).
2. Kodi → Doplňky → Instalovat ze souboru ZIP (musí být povoleno „Neznámé zdroje“).
3. Otevři setup stránku Luny (`http://IP-Luny:7126/setup`), zkopíruj **adresu doplňku** (obsahuje `…/e1.XXXX/manifest.json`).
4. V nastavení doplňku ji vlož do pole *Adresa doplňku nebo token* — adresa serveru se z ní vezme sama.
5. (volitelně) V záložce *Sosáč* vlož `userId` (nebo celou adresu `https://stremio.sosac.tv/cs/manifest.json?userId=…`).

## Struktura

```
addon.xml
default.py                    # router a obrazovky Kodi
resources/lib/luna_api.py     # klient API Luny (bez závislosti na Kodi, jde spustit samostatně)
resources/lib/sosac_api.py    # klient Stremio API Sosáče + párování názvů pro hledání napříč
resources/lib/webshare_api.py # přímý klient WebShare API (login s md5crypt/sha1, hledání, odkaz)
resources/lib/store.py        # historie hledání + zhlédnuto/rozkoukáno (JSON v profilu)
service.py                    # služba: sleduje přehrávání a zapisuje zhlédnuto/pozici
resources/settings.xml
resources/language/…          # en_GB, cs_CZ
```

Test klientů bez Kodi: `python3 resources/lib/luna_api.py http://IP:7126 e1.XXXX`, `python3 resources/lib/sosac_api.py <userId>`

## Licence

MIT
