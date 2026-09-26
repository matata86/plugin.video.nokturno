![Nokturno](https://raw.githubusercontent.com/matata86/plugin.video.nokturno/main/resources/media/fanart.jpg)

# Nokturno — přehrávač vlastního úložiště pro Kodi, volitelně i z WebShare, Sosáče, Luny, HellSpy, Sledujteto, FastShare, CZtoru a Přehraj.to

[![Ko-fi](https://img.shields.io/badge/Ko--fi-podpo%C5%99%20autora-ff5e5b?logo=ko-fi&logoColor=white)](https://ko-fi.com/matata86) [![PayPal](https://img.shields.io/badge/PayPal-paypal.me%2Fmatata86-00457C?logo=paypal&logoColor=white)](https://paypal.me/matata86) [![Bitcoin](https://img.shields.io/badge/Bitcoin-BTC-f7931a?logo=bitcoin&logoColor=white)](#podpora)

Podrobný návod (instalace, nastavení každého zdroje, používání, řešení problémů) je ve [wiki](https://github.com/matata86/plugin.video.nokturno/wiki).

> **Patří k sobě:** stejné zdroje nabízí i [**Nokturno pro Home Assistant**](https://github.com/matata86/nokturno-ha) (HACS integrace, přehrává právě přes tenhle doplněk) a [**Nokturno pro Stremio**](https://github.com/matata86/nokturno-stremio) (i Nuvio). Všechny tři stojí na společném jádru [nokturno-core](https://github.com/matata86/nokturno-core).

## Rychlý start

1. V Kodi povol *Nastavení → Systém → Doplňky → **Neznámé zdroje***.
2. Přidej zdroj `https://nokturno.stream/repo/` a z něj nainstaluj `repository.nokturno.zip`, pak doplněk **Nokturno** (přesný postup níž v [Instalaci](#instalace)).
3. Po instalaci tě **průvodce** provede vyplněním zdrojů. Nic víc není nutné — HellSpy funguje bez účtu a katalogy bez nastavení.

Nastavení účtů se dá pohodlně vyplnit **z mobilu** (QR kód na TV) nebo přenést z jiného Kodi.

## Zdroje

Video doplněk pro Kodi (20 Nexus a novější — `InfoTagVideo`, testováno na Kodi 21 Omega, CoreELEC). Nokturno je především přehrávač **tvého vlastního úložiště** (NAS, Nextcloud, server); osm dalších zdrojů streamů z internetu je volitelná doplňková služba, každý jde zapnout samostatně:

| zdroj | co dává | co potřebuje |
|---|---|---|
| **Vlastní úložiště** (od 3.1.0) | tvoje soubory z NAS, Nextcloudu nebo serveru — až tři WebDAV složky; u titulů mezi streamy vždy první, v menu **Moje úložiště** | adresa složky na WebDAV, případně jméno a heslo |
| **WebShare přímo** (volitelně) | hledání souborů na WebShare a přehrávání, bez dalšího serveru | účet WebShare (jméno + heslo) |
| **Sosáč** (volitelně) | katalogy Sosáče (nejpopulárnější, nově přidané, žánry), filmy i seriály s epizodami, CZ dabing, CZ titulky ze streamuj | účet **Streamuj.tv** (jméno + heslo) — nic víc; katalogy jsou veřejné, k Sosáči se nepřihlašuje, Stremio není potřeba |
| **Luna: Absolute Cinema** (volitelně) | TMDB katalogy (trendy, populární, podle roku, žánru…), streamy z WebShare s rozpoznanou kvalitou a jazyky | běžící server [Luna](https://stremio.cz/d/47-luna-absolute-cinema-addon-pro-prehravani-sifrovaneho-obsahu-z-webshare) v LAN (např. jako [addon Home Assistantu](https://github.com/matata86/ha-addons)) |
| **HellSpy** (volitelně) | fulltextové hledání souborů na hellspy.to a přehrávání původních souborů | nic, rozhraní je veřejné |
| **Sledujteto** (volitelně, od 3.0.0) | fulltextové hledání na sledujteto.cz a přehrávání; rozlišení, kanály a kodek zvuku posílá přímo jejich API | účet Sledujteto (e-mail + heslo), k přehrání **Premium** |
| **FastShare** (volitelně, od 5.1.0) | fulltextové hledání na fastshare.cz a přehrávání; rozlišení a stopáž posílá jejich API, zvuk se dočte z hlavičky souboru (pár set kB z kreditu, jednou za 30 dní) | účet FastShare (jméno + heslo), přehrání z **kreditu** nebo neomezeného tarifu |
| **CZtor** (volitelně, od 6.0.0) | předplatný katalog cztor.com; zvuk, titulky a rozlišení rovnou z API | předplatné CZtor, párování PINem (*Nastavení → CZtor → Spárovat PINem*, heslo se nezadává) |
| **Přehraj.to** (volitelně, od 7.0.0) | fulltextové hledání na prehraj.to a přehrávání; funguje i **bez účtu** (první strana výsledků, překódované 1080p), s **Premium** se stránkuje a hraje původní soubor; rozlišení a zvuk se dočtou z hlavičky souboru | nic; volitelně účet Přehraj.to (e-mail + heslo, jiný než na Sledujteto), Premium pro původní soubory |

Přihlašovací údaje zůstávají v Kodi — doplněk je posílá jen službě, ke které patří (WebShare, Streamuj, Luna, Sledujteto, FastShare, CZtor, Přehraj.to).

## Co umí

- **Vlastní úložiště + osm volitelných zdrojů** v jednom hledání a jednom výběru streamu (stejný soubor z více zdrojů se sloučí do jednoho řádku)
- **Výběr streamu v dialogu** — kvalita, jazyk zvuku i titulků, kanály, kodek, velikost; filtr a zapamatovaný stream u seriálu
- **Pokračovat ve sledování**, Můj seznam, rozkoukané a zhlédnuté i bez Kodi knihovny; **Trakt.tv** a **Up Next**
- **Synchronizace více Kodi** (od 6.6.0) — zhlédnuto, Můj seznam, historie, nastavení i přihlášení se sdílí mezi zařízeními **bez Home Assistanta**; server do dat nevidí
- **Katalogy, žebříčky, TV program, Pro Tebe** a náhodný titul; **Stav zdrojů** v menu řekne, co nefunguje
- **Nastavit z mobilu**, **přenos nastavení** do dalšího Kodi a průvodce prvním nastavením
- **Titulky z OpenSubtitles**, **stahování** na pozadí s navazováním, **Přehrát z detailu** přes TMDb Helper (Arctic Fuse)

<details>
<summary><b>Všechny funkce podrobně</b></summary>

- **Nastavit z mobilu** — na TV se ukáže QR kód a adresa, nastavení vyplníš v pohodlném formuláři v telefonu (včetně hledání a ověření Luny); hesla se nezobrazují
- **Přenos nastavení do dalšího Kodi** (od 6.2.0) — kód `NKT-XXXX-XXXX`, obsah šifrovaný, platí 15 minut; nebo přes soubor na USB. CZtor a Trakt se nepřenášejí
- **Titulky z OpenSubtitles** (od 6.4.0) — doplní české a slovenské titulky, když je zdroje nemají; bez účtu 5 za den, s vlastním 20
- **Pro Tebe** a **Náhodný film / seriál** (od 6.3.0) — doporučení podle historie zhlédnutého (počítá se v tvém Kodi) a losování titulu v oblíbeném žánru s tvým jazykem
- **Stav zdrojů v menu** (od 6.3.1) — řádek nahoře se ukáže, jen když je co řešit (předplatné, Luna, HellSpy, Premium, CZtor, Přehraj.to)
- **Diagnostika Luny** — tlačítka *Najít Lunu v síti* a *Ověřit nastavení Luny*, které řeknou konkrétní příčinu
- **TV program**, **podobné tituly** a **katalogy z dashboardu** (i s podsložkami)
- **Sloučené verze streamů** (`×3`), zvuk a titulky podle preferovaného jazyka (čeština, slovenština, angličtina, maďarština), stahování z kontextového menu s navazováním po přerušení
- **Hlášení o pádech** a zprávy z dashboardu (obojí anonymní, hlášení jde vypnout)
- **Průvodce prvním nastavením** — hned po instalaci doplněk sám provede vyplněním zdrojů (WebShare, Sosáč, Luna, HellSpy, Sledujteto, FastShare, TMDB klíč) i změřením rychlosti internetu pro nastavení datového toku a — je-li nainstalovaný TMDb Helper — nastavením Nokturna jako přehrávače pro Přehrát v detailu filmu, ať není potřeba předem vědět, co a kde v nastavení hledat. Jde přeskočit a kdykoli znovu spustit z *Nastavení → Pokročilé*. Stávající instalace (aktualizace ze starší verze) se nabízet nezačne — pozná se podle už zapnutého zdroje.
- **Přesná hláška, když zdroj neodpoví** — jmenuje konkrétní zdroj (WebShare, Luna, Sosáč…), ne obecnou chybu; u vícezdrojového hledání jde o blokující dialog, ne mizící upozornění, takže se snadno nepřehlédne. Výsledky ze zbylých fungujících zdrojů se po potvrzení zobrazí normálně.
- **Hledat** napříč zapnutými zdroji — jeden dotaz pro filmy i seriály; volba typu se nabídne, jen když dotaz najde obojí. Stejný titul z více zdrojů jen jednou, zdroj je vidět až ve výběru streamu
- **rok v dotazu je filtr** — „Pět švestek 2026“ vrátí jen film z roku 2026; číslo, které patří k názvu („2012“, „Blade Runner 2049“), se jako rok nebere
- **Hledat na WebShare** — soubory přímo z WebShare API (řazení: relevance / nejnovější / hodnocení / velikost)
- **katalogy** Luny (TMDB) i Sosáče; seriály → série → epizody s plakáty, popisy, hodnocením, obsazením
- **streamy z více zdrojů u jednoho titulu** — Luna, přímý fulltext WebShare, Sosáč, HellSpy, Sledujteto i FastShare se prohledají **souběžně** a stejný soubor nalezený víc cestami se ukáže jen jednou; u každého streamu je zdroj, kvalita (u souborů bez kvality v názvu odhad podle velikosti se značkou `~`), datový tok, délka, velikost a jazyky zvuku i titulků — zjištěné ze zdroje, nebo dočtené z hlavičky souboru a označené `~`, když jde jen o odhad. Řazení podle nastavení
- **Vlastní úložiště** (od 3.1.0) — až tři složky s vlastními soubory na WebDAV (NAS, Nextcloud, server). Soubor se k titulu přiřadí podle názvu a složek nad ním (rok u filmu, `S01E02` u dílu), mezi streamy je vždy první se jménem úložiště na začátku řádku; **Moje úložiště** v hlavním menu prochází úložiště po složkách. Nic se do úložiště nezapisuje (žádné `.nfo`/`.strm`). Návod a pojmenování souborů: [wiki → Vlastní úložiště](https://github.com/matata86/plugin.video.nokturno/wiki/Vlastni-uloziste)
- **Zkusit uvolněný fulltext (WebShare, HellSpy, Sledujteto, FastShare)** — tlačítko dole v seznamu streamů spustí uvolněnější hledání pro případ, že přísný filtr (chrání proti nabídnutí úplně jiného titulu, který hledaná slova jen náhodou obsahuje) zahodil skutečnou shodu; takové výsledky jsou označené jako neověřené
- **Filtr streamů** přímo v seznamu — podle kvality, jazyka zvuku, počtu kanálů (5.1 a víc), kodeku, titulků i zdroje; nabízí jen to, co se v aktuálním seznamu skutečně vyskytuje, s počtem nalezeného v závorce
- **Výběr streamu v dialogu na dva řádky** — klik na titul ve výpisu, Přehrát v detailu, widget i TMDb Helper nabídnou stejný dialog: nahoře kvalita, jazyk a velikost, pod tím rozlišení a kodek, zvukové stopy, datový tok, délka, titulky a zdroj. Nahoře v něm **Filtr streamů**, **Zrušit filtr** a **Použít poslední filtr**. Streamy se načítají jen s ukazatelem v rohu obrazovky. Kvalita je obrázek vlevo, **co a v jakém pořadí se u streamu ukazuje, si nastavíš** v *Nastavení → Výběr streamu* (nebo pohodlně šipkami přes *Nastavit z mobilu*). V kontextovém menu filmu a dílu je **Vybrat stream** a **Stáhnout** (stejný dialog, vybraný stream se stáhne místo přehrání). **Vybrat stream** — dialog i u rozkoukaného titulu, který by jinak hrál rovnou; když přísné hledání nic nenajde, zkusí se uvolněný fulltext sám, jinak je jako poslední řádek dialogu
- **Max. datový tok** místo pevné velikosti v GB — nastavení umí i změřit rychlost internetu a spočítat dovolený tok s 25% rezervou; skutečná velikost se pak dopočítá podle stopáže právě otevřeného titulu, ne podle jednoho čísla pro všechno
- **Pokračovat ve sledování** (rozkoukané + další díl), **Můj seznam**, **Naposledy zhlédnuté**, historie hledání (posledních 10 dotazů), zhlédnuto/rozkoukáno (i bez Kodi knihovny)
- **Zapamatovaný stream u seriálu** — jakmile si u seriálu jednou vybereš stream (zdroj, kvalitu, jazyk), v dialogu dalšího dílu je stejná kombinace předvybraná a Up Next nebo Home Assistant ji pustí rovnou bez ptaní
- **Značka dalšího dílu** — v seznamu epizod je `»` u prvního nezhlédnutého dílu, který navazuje na poslední zhlédnutý
- **Otestovat zdroje** — tlačítko v *Nastavení → Pokročilé* ověří Lunu, Sosáč i přihlášení k WebShare a řekne, co nefunguje, bez čekání na prázdný seznam streamů
- **Zkontrolovat aktualizace doplňků** — tlačítko v *Nastavení → Pokročilé* vyžádá kontrolu repozitářů hned, ne až při denní kontrole Kodi
- **Rychlejší procházení** — služba na pozadí drží načtené katalogy pro domovskou obrazovku a předstahuje streamy dalšího dílu rozkoukaných seriálů, takže se otevírají hned
- **Stahování** streamů i souborů na pozadí do zvolené složky
- **Trakt.tv** scrobble (stačí přihlášení kódem na trakt.tv/activate, i s free účtem), IMDb id pro doplňky titulků, cesty pro widgety skinu
- **hodnocení v procentech** — položky nesou vlastnost `RatingPercent` („58 %“) vedle běžného ratingu, takže ji skin může ukázat místo hvězdiček
- **anonymní statistiky** používání, které jdou v nastavení vypnout (viz níže)
- **Synchronizace mezi více Kodi** (od 6.6.0) — zhlédnuto, rozkoukané (i pozice), Můj seznam a historie hledání se sdílí mezi všemi tvými Kodi, **i bez Home Assistanta**. Středisko je dashboard Nokturna, který drží jen zapečetěná data — jsou zašifrovaná klíčem odvozeným z kódu skupiny a server je nepřečte. *Nastavení → Synchronizace*: na prvním Kodi zvol *Založit skupinu* a opiš kód `NKT-XXXX-XXXX-XXXX-XXXX`, na dalších *Připojit se ke skupině*. Zvlášť se zapíná, co se sdílí: zhlédnuto a rozkoukanost, Můj seznam, historie hledání a (výchozí vypnuté) **nastavení doplňku** a **přihlášení ke zdrojům** — ta jdou zašifrovaná, ale kdo má kód skupiny, přečte je, takže kód patří jen tvým zařízením. **Home Assistant** je dalším členem skupiny: kód zadáš i v nastavení integrace. Kodi v domácí síti může dál synchronizovat přímo s HA přes adresu a klíč (volba *Středisko synchronizace → Home Assistant*). Běží na pozadí, ručně přes „Synchronizovat teď" v menu
- **Up Next** — je-li nainstalovaná služba [Up Next](https://kodi.tv/addons/omega/service.upnext), dostane u seriálů informaci o dalším dílu a ke konci epizody nabídne jeho přehrání (a s zapamatovaným streamem ho pustí rovnou)
- **Sledování předplatného WebShare** — tlačítko v nastavení ukáže, kolik dní zbývá; upozornění pár dní před koncem a pak každý den, dokud předplatné nevyprší
- **Žánr v popisu titulu** — tučně na začátku, přeložený do češtiny i u titulů z anglicky mluvících zdrojů
- **Nápověda ke každé volbě v nastavení** — dole v dialogu se zobrazí, co položka dělá, ne jen její název

</details>

### Přehrát z detailu filmu (TMDb Helper, Arctic Fuse)

Skiny jako Arctic Fuse ukazují detail filmu nebo dílu přes doplněk TMDb Helper. Jeho
tlačítko **Přehrát** umí spustit Nokturno: *Nastavení doplňku → Pokročilé → Přidat
Nokturno do TMDb Helperu*. Doplněk tam uloží player a nabídne ho jako výchozí —
Přehrát pak podle IMDb id najde streamy v Nokturnu a nabídne dialog s filtrem. Filmy
a díly z výpisů i widgetů Nokturna se z detailu přehrají i bez TMDb Helperu.

## Předpoklady

Katalog a hledání titulů fungují i úplně bez nastavení (vlastní databáze, viz výš). Vlastní úložiště potřebuje jen adresu WebDAV složky. Volitelné zdroje z internetu chtějí aspoň jeden z tabulky výše — účet WebShare, účet Sosáče, server Luna v LAN, nebo prostě zapnout HellSpy (veřejný, nic nepotřebuje).

## Instalace

**Doporučeno – přes repozitář (automatické aktualizace).** Nejdřív je potřeba
povolit Nastavení → Systém → Doplňky → **Neznámé zdroje**.

*Přímo v Kodi, bez prohlížeče (vhodné pro TV a set-top boxy):*

1. Nastavení → Správce souborů → **Přidat zdroj** → jako adresu zadej
   `https://nokturno.stream/repo/` a pojmenuj ji třeba `Nokturno`.
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

**Beta verze (novinky dřív, můžou obsahovat chyby):** Doplňky → Instalovat
z repozitáře → **Nokturno repozitář** → Repozitáře doplňků → **Nokturno
repozitář (beta)** → Instalovat. Kodi pak nabízí stabilní verze i bety a vždy
nainstaluje tu nejnovější; po vydání stabilní verze se beta sama nahradí
stabilní. Zpět jen na stabilní verze: beta repozitář odinstaluj a v Informacích
o doplňku vyber poslední stabilní verzi (nebo počkej na další).

**Ručně, bez repozitáře:** stáhni `plugin.video.nokturno-x.y.z.zip` z
[Releases](https://github.com/matata86/plugin.video.nokturno/releases) → Kodi →
Doplňky → Instalovat ze souboru ZIP.

**Návrat na starší verzi:** místní nabídka na doplňku → *Informace* → **Verze**;
repozitář nabízí i předchozí vydání.

## Nastavení zdrojů

Vlastní úložiště nastavíš v kategorii *Vlastní úložiště* (adresa WebDAV složky). Volitelné zdroje z internetu zapni, co máš — jeden, víc, nebo všech osm:

- **WebShare (přímo)** — jméno + heslo k WebShare (nebo 40znakový salted hash, který používá WebShare doplněk pro Stremio).
- **Sosáč** — jméno + heslo ke **Streamuj.tv** (přehrávač Sosáče). Katalogy a hledání jdou z veřejných JSON exportů `tv.sosac.to`, streamy ze `streamuj.tv` — stejně jako oficiální Kodi doplněk Sosáče.
- **Luna** — otevři setup stránku Luny (`http://IP-Luny:7126/setup`), zkopíruj **adresu doplňku** (`…/e1.XXXX/manifest.json`) a vlož ji do pole *Adresa doplňku nebo token*; adresa serveru se z ní vezme sama.
- **HellSpy** — jen přepínač v nastavení, rozhraní je veřejné a účet nepotřebuje.
- **Sledujteto** — e-mail a heslo v kategorii *Sledujteto*. Hledá se s jakýmkoli účtem, přehrát jde jen s **Premium**; *Nastavení → Pokročilé → Otestovat zdroje* ukáže, jestli je aktivní.
- **FastShare** — jméno a heslo v kategorii *FastShare*. Hledá se i bez účtu, přehrání se odečte z **kreditu** podle velikosti souboru (pokud nemáš neomezené stahování); *Nastavení → Pokročilé → Otestovat zdroje* ukáže, kolik kreditu zbývá. Soubor chce cookie z přihlášení, Kodi ji posílá samo.
- **CZtor** — *Nastavení → CZtor → Spárovat PINem*: na TV se ukáže PIN, zadáš ho na `cztor.com/activate`; heslo se do doplňku nezadává.
- **Přehraj.to** — zapnuté rovnou po instalaci, účet není potřeba. E-mail a heslo (kategorie *Přehraj.to*) přidají další strany výsledků a u **Premium** původní soubor místo překódovaného 1080p. Server omezuje dotazy z jedné adresy (HTTP 429) — doplněk pak zdroj na 10 minut přeskočí; *Nastavení → Pokročilé → Otestovat zdroje* ukáže stav.
- **Vlastní databáze filmů a seriálů (TMDB)** — nepovinné, ale s klíčem má přednost i před Lunou (viz výš): zdarma klíč z [themoviedb.org](https://www.themoviedb.org/signup) → ikona profilu → *Nastavení* → *API* → *Request an API Key* → *Developer* → krátký formulář → zkopíruj **API Key (v3 auth)** (ne delší "API Read Access Token") do *Nastavení → Vlastní databáze filmů a seriálů*. Bez klíče se použije Luna (je-li dostupná), jinak zdarma veřejný katalog Sosáče a Cinemeta, ale bez českého popisu.

## Vlastní databáze filmů a seriálů

Katalog (*Filmy* / *Seriály*) a hledání titulů běžely dřív jen přes Lunu (nebo přihlášený Sosáč) — bez nich se dřív ani nezobrazily v menu. Teď se použije řetězec zdrojů metadat, v tomhle pořadí (každý se zkusí, jen když předchozí nic nevrátil):

1. **TMDB** — jakmile má uživatel vlastní zdarma klíč (viz *Nastavení → Vlastní databáze filmů a seriálů* → nápověda s návodem), má přednost **i před Lunou** — umí česky i to, co Luna neřekne (popis, obsazení). Luna zůstává zdrojem streamů, ne metadat.
2. **Luna** — bez TMDB klíče, když je dostupná (beze změny oproti dřívějšku)
3. **Veřejný katalog Sosáče** — bez TMDB i Luny, bez účtu, české tituly a žánry, ale bez popisu
4. **Cinemeta** — poslední záchrana, funguje vždy, ale jen anglicky

Streamy samotné (WebShare/HellSpy/Sledujteto/FastShare/CZtor/Přehraj.to/Luna) se pak hledají stejně jako dřív — vlastní databáze řeší jen "co je to za titul", ne odkud stream stáhnout.

## Nokturno v Home Assistantu

Stejné zdroje umí i [**integrace Nokturno pro Home Assistant**](https://github.com/matata86/nokturno-ha) (instalace přes HACS). Hledá ve stejných zdrojích (WebShare, Sosáč, Luna, HellSpy, Sledujteto, FastShare, vlastní úložiště), výsledky pouští **v Kodi právě přes tenhle doplněk** (`plugin://plugin.video.nokturno/…`), takže titul skončí v „Pokračovat ve sledování" a Kodi si pamatuje pozici. Navíc umí stáhnout film do Home Assistantu nebo poslat odkaz do mobilu.

[![Otevřít repozitář v HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=matata86&repository=nokturno-ha&category=integration)
[![Přidat integraci](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=nokturno)
[![Přidat repozitář s doplňky](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmatata86%2Fha-addons)

Tlačítka otevřou tvoji instanci: první přidá integraci do HACS, druhé spustí její nastavení, třetí přidá repozitář s addony (server **Luna** jako doplněk HA).

Účty se nastavují stejné jako tady; obě aplikace sdílejí knihovny zdrojů, takže se chovají shodně.

## Nokturno ve Stremiu

Stejné zdroje (WebShare, Sosáč, Sledujteto, FastShare, HellSpy) jsou i jako [**doplněk pro Stremio**](https://github.com/matata86/nokturno-stremio) — funguje i v Nuviu a dalších klientech Stremia. Na rozdíl od tohoto doplňku nemá vlastní katalogy ani knihovnu — dohledává jen streamy k titulům, které si Stremio najde samo.

**Přidat do Stremia:** [nokturno.stream/configure](https://nokturno.stream/configure).

## Pro pokročilé a vývojáře

### Struktura

```
addon.xml
default.py                    # router a obrazovky Kodi (nad jádrem: KodiEngine)
resources/lib/engine.py       # jádro: hledání, streamy ze všech zdrojů, párování, hlavičky, odkazy (sdílené s HA a Stremiem)
resources/lib/const.py        # klíče nastavení společné s HA a Stremiem
resources/players/nokturno.json # player pro TMDb Helper (Přehrát v detailu filmu → Nokturno)
resources/lib/luna_api.py     # klient API Luny (bez závislosti na Kodi, jde spustit samostatně)
resources/lib/cinemeta_api.py # vlastní databáze: Cinemeta (Stremio), poslední záchrana bez klíče/účtu
resources/lib/tmdb_api.py     # vlastní databáze: TMDB s vlastním klíčem uživatele (česky, s popisem)
resources/lib/sosac_direct.py # Sosáč napřímo: veřejné JSONy tv.sosac.to + streamy/titulky ze streamuj.tv
resources/lib/sosac_api.py    # párování názvů (normalize, names_match) pro hledání napříč zdroji
resources/lib/webshare_api.py # přímý klient WebShare API (login s md5crypt/sha1, hledání, odkaz)
resources/lib/hellspy_api.py  # HellSpy (veřejné, bez účtu)
resources/lib/sledujteto_api.py # Sledujteto (účet, Premium pro přehrání, zvuk a titulky z API)
resources/lib/fastshare_api.py # FastShare (hledání bez účtu, přehrání s cookie z přihlášení, kredit)
resources/lib/storage_api.py  # vlastní úložiště (WebDAV, až tři sloty)
resources/lib/mediainfo.py    # čtení hlavičky souboru (MKV/MP4/AVI): zvuk, titulky, rozlišení
resources/lib/enrich.py       # popisy titulů Sosáče z Luny/Cinemety
resources/lib/wikidata_api.py # české a slovenské názvy z Wikidat pro fulltext
resources/lib/source_errors.py # srozumitelné hlášky o výpadku zdroje (bez tokenů)
resources/lib/sync.py         # synchronizace zhlédnuto/Můj seznam mezi Kodi (dashboard nebo HA)
resources/lib/stats.py        # čítače používání a jejich odesílání (bez závislosti na Kodi)
resources/lib/store.py        # historie hledání + zhlédnuto/rozkoukáno (JSON v profilu)
resources/lib/streams.py      # rozbor, filtr a řazení streamů
resources/lib/trakt_api.py    # Trakt.tv (device code, scrobble, historie)
resources/lib/prowlarr.py, qbittorrent.py # torrenty (používá je integrace pro HA, engine je importuje)
service.py                    # služba: zhlédnuto/pozice, Trakt scrobble, stahování, statistiky
repository.nokturno/          # repozitář pro automatické aktualizace (stable)
repository.nokturno.beta/     # repozitář beta (repo/ + repo-beta/)
tools/build_repo.py           # sestaví repo/ (addons.xml, md5, zipy) po změně verze; --beta → repo-beta/
tests/                        # testy bez Kodi (stubs xbmc*), `python3 -m unittest discover -s tests`
resources/settings.xml
resources/language/…          # en_GB, cs_CZ, sk_SK
```

`resources/lib/` je vysypaná kopie sdíleného jádra [nokturno-core](https://github.com/matata86/nokturno-core) — opravy patří tam.

Test klientů bez Kodi: `python3 resources/lib/luna_api.py http://IP:7126 e1.XXXX`, `python3 resources/lib/sosac_direct.py <streamuj_user> <streamuj_heslo>`

### Anonymní statistiky

Od 3.0.0 hlášení nese i to, **které zdroje máš zapnuté** (jen přepínače — žádné účty ani adresy).

Doplněk umí hlásit, jak se používá. Slouží to k jedinému: vědět, kolik lidí ho
má, na čem běží a o co je zájem. Sběr je ve výchozím stavu zapnutý a vypíná se
jedním přepínačem v *Nastavení → Statistiky*.

**Po vypnutí** se dál jednou za 6 hodin posílá jen náhodné id instalace, typ
a verze doplňku — aby bylo vidět, kolik instalací žije. Žádné tituly, zdroje,
platforma, jazyk ani časy použití.

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

Data chodí na `https://nokturno.stream/collect`. Odesílání se vypíná
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

### Odeslání logu

V *Nastavení → Pokročilé* je (úplně dole) tlačítko **Odeslat log Kodi**. Po
potvrzení („Opravdu odeslat log?") pošle poslední ~500 KB souboru `kodi.log`
(gzip) na server, aby šel nahlášený problém rozklíčovat — nic dalšího zařízení
neopouští. Funguje i při vypnutých statistikách, protože jde o jednorázovou
diagnostickou akci na vyžádání, ne o průběžný sběr.

Log se posílá na `https://nokturno.stream/logs?id=<id instalace>&version=<verze>`
jako syrová gzip data v těle POST požadavku (stejné `id` jako u statistik).
Očekávaná odpověď je HTTP 200; při chybě (síť, server, moc časté odeslání)
doplněk zobrazí notifikaci s důvodem. Nahlášené logy vidí administrátor v
[dashboardu statistik](https://github.com/matata86/plugin.video.nokturno/wiki/Reseni-problemu#jak-nahlásit-chybu),
starší 30 dní se mažou automaticky.

## Právní upozornění

Nokturno je především přehrávač a správce **vlastního úložiště** — obsah, který
si sami nahrajete a zpřístupníte (např. přes WebDAV), přehrává napřímo. Jako
doplňkovou službu si můžete volitelně napojit i některé veřejně dostupné
vyhledávače třetích stran (WebShare, Sosáč, HellSpy, Sledujteto, FastShare,
Přehraj.to, CZtor, Luna, OpenSubtitles) — v tom případě je Nokturno jen
technické rozhraní, samo žádný obsah nehostuje, neukládá ani neposkytuje.

Doplněk smíte používat jen k obsahu, ke kterému máte zákonné oprávnění, licenci
nebo jiný právní titul. Při prvním spuštění se právní upozornění ukáže
k odsouhlasení (Nastavení → Podmínky použití, úplně první kategorie — přepínač
jde kdykoli vypnout i znovu zapnout), plný text i kam nahlásit nelegální obsah
u jednotlivých zdrojů je i na <https://nokturno.stream/terms>.

## Licence

Zdrojový kód je veřejně čitelný pro transparentnost a instalaci přes oficiální
kanály (GitHub Releases, repozitář zipů). Kopírování, úpravy a šíření bez
svolení autora nejsou dovolené — viz [LICENSE](LICENSE).

---

## Podpora

[![Podpoř Nokturno — Ko-fi, PayPal, Bitcoin](.github/podpora.png)](https://ko-fi.com/matata86)

- **Ko-fi:** https://ko-fi.com/matata86
- **PayPal:** https://paypal.me/matata86
- **Bitcoin:** `bc1qhjwt8xxmuym0xsd50yfpvjph00386uz73gqwlc`
