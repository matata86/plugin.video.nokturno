# plugin.video.luna — Luna: Absolute Cinema pro Kodi

Video doplněk pro Kodi (19+ / Python 3, testováno na Kodi 21 Omega, CoreELEC), který je klientem serveru **[Luna: Absolute Cinema](https://stremio.cz/d/47-luna-absolute-cinema-addon-pro-prehravani-sifrovaneho-obsahu-z-webshare)** — Stremio addon serveru pro streamování z WebShare.

Doplněk sám nic nestahuje ani se nepřihlašuje k WebShare. Volá HTTP API Luny (Stremio protokol) a streamy přehrává přes ni:

- **Filmy / Seriály** — katalogy TMDB z Luny: Trendy, Populární, Nejlépe hodnocené, Podle roku, Podle jazyka (s žánry)
- **Hledat film / Hledat seriál**
- seriály → série → epizody, s plakáty, popisy, hodnocením, obsazením
- výběr kvality streamu (4K HDR, Full HD, …) s bitrate, velikostí, jazyky zvuku a titulků — nebo automaticky nejlepší

## Předpoklady

Běžící server Luna v LAN (na PC, NAS, nebo jako [addon Home Assistantu](https://github.com/matata86/fns-ha-tweaks)). Kodi jen potřebuje jeho adresu.

## Instalace

1. Stáhni `plugin.video.luna-x.y.z.zip` z [Releases](https://github.com/matata86/plugin.video.luna/releases).
2. Kodi → Doplňky → Instalovat ze souboru ZIP (musí být povoleno „Neznámé zdroje“).
3. Otevři setup stránku Luny (`http://IP-Luny:7126/setup`), zkopíruj **adresu doplňku** (obsahuje `…/e1.XXXX/manifest.json`).
4. V nastavení doplňku ji vlož do pole *Adresa doplňku nebo token* — adresa serveru se z ní vezme sama.

## Struktura

```
addon.xml
default.py                    # router a obrazovky Kodi
resources/lib/luna_api.py     # klient API Luny (bez závislosti na Kodi, jde spustit samostatně)
resources/settings.xml
resources/language/…          # en_GB, cs_CZ
```

Test klienta bez Kodi: `python3 resources/lib/luna_api.py http://IP:7126 e1.XXXX`

## Licence

MIT
