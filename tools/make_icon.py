#!/usr/bin/env python3
"""Značka Nokturna — ikona doplňku, ikona repozitáře a fanart.

    python3 tools/make_icon.py            # ikony a fanart
    python3 tools/make_icon.py podpora    # obrázek podpory do README (.github/podpora.png)
    python3 tools/make_icon.py 6.0.0      # obrázek k vydání 6.0.0 s CZtorem (.github/nokturno-6.0.0-cztor.png)
    python3 tools/make_icon.py 6.6.0      # obrázek k vydání 6.6.0 s přehledem novinek (.github/nokturno-6.6.0-novinky.png)
    python3 tools/make_icon.py 7.0.0      # obrázek k vydání 7.0 s Přehraj.to a novinkami 6.6 (.github/nokturno-7.0.0-prehrajto.png)

Značka je prstenec, v něm „N" s perforacemi filmového pásu a nad ním úplněk
s vyříznutým play. Kreslí se vektorově (cairosvg) a skládá po vrstvách,
protože `fill-rule="evenodd"` odečítá jen tvary ležící celé uvnitř — přesahující
část by naopak vyplnila. Odečet se proto dělá nad alfa kanálem a pod výřezy
prosvítá pozadí i s přechodem.

Zdroj pravdy je tenhle skript, ne vygenerované PNG — při jakékoli úpravě značky
se mění tady a soubory se přegenerují. Vyžaduje `pip install cairosvg pillow`
(jen pro vývoj, do Kodi se nic z toho nedostane).
"""
import io
import math
import os

import cairosvg
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SS = 2            # nadvzorkování, cairo kreslí hrany ostře, ale odečet alfy ne
ICON = 512        # Kodi 19+ chce ikonu 512×512
FANART = (1920, 1080)

# Noční paleta: modrá obloha s nádechem indiga, teplé zlato na značce.
GOLD_STOPS = (("0", "#FFE7B4"), ("0.45", "#F3C476"), ("1", "#CE8C2E"))
GOLD_FLAT = "#F0B95C"
SKY = (("0", "#2B3A7C"), ("0.55", "#141C42"), ("1", "#060917"))
SKY_FLAT = "#121B40"

FONTS = "/usr/share/fonts/opentype/inter"

DEFS = f'''
<linearGradient id="gold" gradientUnits="userSpaceOnUse" x1="130" y1="120" x2="400" y2="420">
  {"".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in GOLD_STOPS)}</linearGradient>
<linearGradient id="sky" x1="0.1" y1="0" x2="0.9" y2="1">
  {"".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in SKY)}</linearGradient>
<radialGradient id="glow" cx="0.5" cy="0.40" r="0.60">
  <stop offset="0" stop-color="#5468C0" stop-opacity="0.45"/>
  <stop offset="1" stop-color="#5468C0" stop-opacity="0"/></radialGradient>
<linearGradient id="rim" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="#FFFFFF" stop-opacity="0.22"/>
  <stop offset="0.45" stop-color="#FFFFFF" stop-opacity="0"/></linearGradient>'''

STARS = ''.join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="#FFF" opacity="{o}"/>'
                for x, y, r, o in [(84, 92, 3.0, .50), (146, 56, 2.0, .30), (436, 316, 2.4, .34),
                                   (62, 214, 1.9, .26), (356, 58, 1.7, .24)])
NIGHT = (f'<rect width="512" height="512" fill="url(#sky)"/>'
         f'<rect width="512" height="512" fill="url(#glow)"/>' + STARS)
FLAT = f'<rect width="512" height="512" fill="{SKY_FLAT}"/>'


# --- vektorové stavební kameny ----------------------------------------------
def svg(body, w=512, h=512):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}"><defs>{DEFS}</defs>{body}</svg>')


def render(markup, w=512, h=512, scale=SS):
    png = cairosvg.svg2png(bytestring=svg(markup, w, h).encode(),
                           output_width=int(w * scale), output_height=int(h * scale))
    return Image.open(io.BytesIO(png)).convert("RGBA")


def circ(cx, cy, r):
    return f'M{cx-r},{cy} a{r},{r} 0 1,0 {2*r},0 a{r},{r} 0 1,0 {-2*r},0 Z '


def ring(cx, cy, r, w):
    """Mezikruží — vnitřní kruh leží celý uvnitř vnějšího, evenodd tu stačí."""
    return circ(cx, cy, r + w / 2) + circ(cx, cy, r - w / 2)


def n_outline(x, y, w, h, t, d):
    """Jeden neprotínající se obrys písmene N (levá noha, diagonála, pravá noha).

    Tři samostatné tvary by se překryly a evenodd by z překryvů udělalo díry,
    proto se počítají i oba body, kde hrany diagonály narazí na nohy.
    """
    s1, s2 = (w - d - t) / (w - d), t / (w - d)
    p = [(x, y), (x + d, y), (x + w - t, y + h * s1), (x + w - t, y), (x + w, y),
         (x + w, y + h), (x + w - d, y + h), (x + t, y + h * s2), (x + t, y + h), (x, y + h)]
    return "M" + " L".join(f"{a:.2f},{b:.2f}" for a, b in p) + " Z "


def rrect(cx, cy, w, h, r, ang):
    """Otočený obdélník se zaoblenými rohy — jedna perforace filmového pásu."""
    a = math.radians(ang)
    ca, sa = math.cos(a), math.sin(a)
    P = lambda px, py: f"{cx + px*ca - py*sa:.2f},{cy + px*sa + py*ca:.2f}"  # noqa: E731
    W, H = w / 2, h / 2
    return (f"M{P(-W+r,-H)} L{P(W-r,-H)} A{r},{r} {ang} 0,1 {P(W,-H+r)} L{P(W,H-r)} "
            f"A{r},{r} {ang} 0,1 {P(W-r,H)} L{P(-W+r,H)} A{r},{r} {ang} 0,1 {P(-W,H-r)} "
            f"L{P(-W,-H+r)} A{r},{r} {ang} 0,1 {P(-W+r,-H)} Z ")


def round_poly(pts, r):
    """Mnohoúhelník se zaoblenými vrcholy — rohy se oříznou a spojí kvadratikou."""
    n, out = len(pts), ""
    for i, V in enumerate(pts):
        P, N = pts[(i - 1) % n], pts[(i + 1) % n]
        def toward(T):
            dx, dy = T[0] - V[0], T[1] - V[1]
            L = math.hypot(dx, dy) or 1
            k = min(r, L / 2)
            return V[0] + dx / L * k, V[1] + dy / L * k
        A, B = toward(P), toward(N)
        out += f"{'M' if i == 0 else 'L'}{A[0]:.1f},{A[1]:.1f} Q{V[0]:.1f},{V[1]:.1f} {B[0]:.1f},{B[1]:.1f} "
    return out + "Z "


def play(cx, cy, size):
    """Play uvnitř úplňku. Těžiště trojúhelníku leží vlevo od geometrického
    středu, proto se kreslí posunutý doprava — jinak vypadá přilepený k hraně."""
    h, w = size, size * 0.88
    x = cx - w * 0.40
    return round_poly([(x, cy - h / 2), (x + w, cy), (x, cy + h / 2)], size * 0.13)


def perforations(x, y, w, h, t, d, per_stem=4, per_diag=3, hw=15, hh=11):
    out = ""
    for sx in (x + t / 2, x + w - t / 2):
        for i in range(per_stem):
            out += rrect(sx, y + 24 + i * (h - 48) / (per_stem - 1), hw, hh, 3.2, 0)
    ang = math.degrees(math.atan2(h, w - d)) - 90
    for i in range(per_diag):
        f = 0.30 + (0.42 / max(1, per_diag - 1)) * i
        out += rrect(x + d / 2 + (w - d) * f, y + h * f, hw, hh, 3.2, ang)
    return out


# --- skládání ----------------------------------------------------------------
def layer(add, sub, fill, scale=SS):
    """Tvar mínus tvar. Odečítá se alfa, ne cesta — viz docstring modulu."""
    mark = render(f'<path fill-rule="evenodd" fill="{fill}" d="{add}"/>', scale=scale)
    if sub:
        eraser = render(f'<path fill-rule="evenodd" fill="#fff" d="{sub}"/>', scale=scale)
        mark.putalpha(ImageChops.subtract(mark.getchannel("A"), eraser.getchannel("A")))
    return mark


RING = dict(r=174, w=15)
NBOX = (162, 170, 178, 188, 42, 48)     # x, y, šířka, výška, tloušťka nohy, šířka diagonály
MOON = dict(cx=386, cy=126, r=54, gap=15, play=52)


def mark(background, fill, scale=SS):
    """Značka na zadaném pozadí.

    Mezera kolem měsíce se odečítá jen z prstence. Kdyby zasáhla i do N,
    ukousne mu roh nohy v místě, kde se měsíce vůbec nedotýká, a vypadá to
    jako vada vykreslení, ne jako záměr.
    """
    x, y, w, h, t, d = NBOX
    m = MOON
    out = render(background, scale=scale)
    for spec in (
        (ring(256, 256, RING["r"], RING["w"]), circ(m["cx"], m["cy"], m["r"] + m["gap"]), fill),
        (n_outline(x, y, w, h, t, d), perforations(x, y, w, h, t, d), fill),
        (circ(m["cx"], m["cy"], m["r"]), play(m["cx"], m["cy"], m["play"]), fill),
    ):
        out = Image.alpha_composite(out, layer(*spec, scale=scale))
    return out


def font(name, size):
    path = os.path.join(FONTS, name)
    if os.path.exists(path):
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def make_fanart(path):
    """Pozadí pro Kodi: značka vpravo, název vlevo, zbytek tmavý a klidný — kůže
    přes fanart vykresluje text, takže se nesmí prát o pozornost.

    Značka se sem sází bez dlaždice. Ikona má vlastní podklad i světelný lem
    a na velké ploše by se její okraj rýsoval jako obdélník ve vzduchu.
    """
    W, H = FANART
    sky = render(f'<rect width="{W}" height="{H}" fill="url(#sky)"/>', W, H, scale=1).convert("RGB")

    # Záře přes cairosvg tu dělá viditelnou hranu přechodu, rozmazaná elipsa ne.
    # Kreslí se ve zmenšenině (rychlejší rozostření), takže i souřadnice musí být
    # v jejím měřítku — ve full-size by elipsa spadla mimo plátno.
    q = 4
    glow = Image.new("RGB", (W // q, H // q), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([c / q for c in (940, 20, 1980, 1060)], fill=(32, 42, 96))
    glow = glow.filter(ImageFilter.GaussianBlur(30)).resize((W, H), Image.BICUBIC)
    sky = ImageChops.add(sky, glow)

    dr = ImageDraw.Draw(sky, "RGBA")
    for x, y, r, o in [(180, 150, 3, 90), (470, 96, 2, 60), (1520, 790, 3, 70), (130, 620, 2, 55),
                       (1150, 120, 2, 50), (860, 900, 2, 45), (1780, 250, 3, 65), (330, 920, 2, 40),
                       (700, 210, 2, 42), (1620, 560, 2, 38)]:
        dr.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, o))

    logo = mark("", "url(#gold)", scale=1).resize((620, 620), Image.LANCZOS)
    sky.paste(logo, (1150, 230), logo)

    dr.text((170, 330), "Nokturno", font=font("InterDisplay-Bold.otf", 152), fill=(243, 196, 118))
    # Hlavní řádek je vlastní úložiště — to je, kvůli čemu doplněk vznikl a co hraje
    # bez cizího účtu. Vyhledávače pod ním drobněji a se slovem „volitelně“, ať je
    # z obrázku na první pohled vidět, co je služba doplňku a co jen doplňková možnost.
    dr.text((177, 520), "Tvoje vlastní úložiště  ·  WebDAV",
            font=font("InterDisplay-Bold.otf", 52), fill=(255, 255, 255))
    radky = ("volitelně i veřejné vyhledávače třetích stran:",
             "WebShare · Sosáč · Sledujteto · FastShare · HellSpy · CZtor · Přehraj.to · Luna")
    size = 34
    while size > 20:
        f = font("InterDisplay-Medium.otf", size)
        if max(dr.textlength(r, font=f) for r in radky) <= 950:
            break
        size -= 2
    f = font("InterDisplay-Medium.otf", size)
    for i, radek in enumerate(radky):
        dr.text((177, 616 + i * (size + 14)), radek, font=f, fill=(163, 176, 218))
    dr.text((177, 730), "Kodi  ·  Home Assistant  ·  Stremio",
            font=font("InterDisplay-Medium.otf", 38), fill=(203, 212, 240))
    sky.save(path, quality=92, subsampling=0)


RELEASE = (1200, 630)   # poměr, který Facebook i fóra ukazují bez ořezu


def make_release_600(path):
    """Obrázek k vydání 6.0.0: značka, číslo verze a nový zdroj CZtor. Tatáž noční obloha
    a zlato jako fanart, ať je na první pohled jasné, že patří k doplňku."""
    W, H = RELEASE
    sky = render(f'<rect width="{W}" height="{H}" fill="url(#sky)"/>', W, H, scale=1).convert("RGB")
    q = 4
    glow = Image.new("RGB", (W // q, H // q), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([c / q for c in (560, -60, 1300, 700)], fill=(32, 42, 96))
    sky = ImageChops.add(sky, glow.filter(ImageFilter.GaussianBlur(24)).resize((W, H), Image.BICUBIC))
    dr = ImageDraw.Draw(sky, "RGBA")
    for x, y, r, o in [(90, 80, 3, 90), (330, 50, 2, 60), (1090, 520, 3, 70), (70, 430, 2, 55),
                       (760, 70, 2, 50), (600, 580, 2, 45), (1140, 160, 3, 65), (240, 590, 2, 40)]:
        dr.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, o))
    logo = mark("", "url(#gold)", scale=1).resize((430, 430), Image.LANCZOS)
    sky.paste(logo, (740, 100), logo)
    gold, dim = (243, 196, 118), (163, 176, 218)
    dr.text((70, 80), "Nokturno 6.0", font=font("InterDisplay-Bold.otf", 108), fill=gold)
    dr.text((74, 215), "Nový zdroj", font=font("InterDisplay-Medium.otf", 42), fill=dim)
    # štítek CZtor — zlatá pilulka s tmavým písmem
    f = font("InterDisplay-Bold.otf", 96)
    tw = dr.textlength("CZtor", font=f)
    dr.rounded_rectangle((70, 275, 70 + tw + 80, 415), radius=70, fill=gold)
    dr.text((110, 285), "CZtor", font=f, fill=(20, 28, 66))
    dr.text((74, 450), "Kodi  ·  Home Assistant", font=font("InterDisplay-Medium.otf", 40), fill=(255, 255, 255))
    dr.text((74, 510), "Spárování PINem, heslo se nezadává", font=font("InterDisplay-Medium.otf", 32), fill=dim)
    sky.save(path, quality=92, subsampling=0) if path.endswith(".jpg") else sky.save(path)


def make_release_660(path):
    """Obrázek k vydání 6.6.0: pět největších novinek řady 6.2–6.6 pod sebou. Tatáž
    noční obloha a zlato jako u 6.0.0 a fanartu."""
    W, H = RELEASE
    sky = render(f'<rect width="{W}" height="{H}" fill="url(#sky)"/>', W, H, scale=1).convert("RGB")
    q = 4
    glow = Image.new("RGB", (W // q, H // q), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([c / q for c in (700, -80, 1360, 640)], fill=(32, 42, 96))
    sky = ImageChops.add(sky, glow.filter(ImageFilter.GaussianBlur(24)).resize((W, H), Image.BICUBIC))
    dr = ImageDraw.Draw(sky, "RGBA")
    for x, y, r, o in [(1130, 60, 3, 90), (1010, 40, 2, 60), (1150, 560, 3, 70), (60, 600, 2, 55),
                       (960, 590, 2, 45), (1170, 300, 2, 60), (40, 40, 2, 50)]:
        dr.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, o))
    logo = mark("", "url(#gold)", scale=1).resize((250, 250), Image.LANCZOS)
    sky.paste(logo, (945, 190), logo)
    gold, dim = (243, 196, 118), (163, 176, 218)
    dr.text((70, 42), "Nokturno 6.6", font=font("InterDisplay-Bold.otf", 88), fill=gold)
    dr.text((74, 150), "Co je nového od verze 6.2", font=font("InterDisplay-Medium.otf", 34), fill=dim)
    radky = [("Synchronizace více Kodi", "i bez Home Assistanta"),
             ("Titulky z OpenSubtitles", "česky a slovensky"),
             ("Pro Tebe", "a náhodný film či seriál"),
             ("Přenos nastavení", "do dalšího Kodi kódem"),
             ("Stav zdrojů v menu", "co nefunguje a proč")]
    big, small = font("InterDisplay-Bold.otf", 42), font("InterDisplay-Medium.otf", 30)
    y = 222
    for hlavni, doplnek in radky:
        dr.ellipse((74, y + 14, 94, y + 34), fill=gold)
        dr.text((112, y), hlavni, font=big, fill=(255, 255, 255))
        dr.text((112 + dr.textlength(hlavni, font=big) + 18, y + 8), doplnek, font=small, fill=dim)
        y += 74
    sky.save(path)


def make_release_700(path):
    """Obrázek k vydání 7.0: nový zdroj Přehraj.to nahoře jako hlavní zpráva a pod ním
    novinky řady 6.6 drobněji — jde do týchž příspěvků na Facebooku, kde už visí obrázek
    k 6.6, takže musí říct obojí naráz: co je úplně nové a co přibylo předtím."""
    W, H = RELEASE
    sky = render(f'<rect width="{W}" height="{H}" fill="url(#sky)"/>', W, H, scale=1).convert("RGB")
    q = 4
    glow = Image.new("RGB", (W // q, H // q), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([c / q for c in (640, -120, 1340, 560)], fill=(32, 42, 96))
    sky = ImageChops.add(sky, glow.filter(ImageFilter.GaussianBlur(24)).resize((W, H), Image.BICUBIC))
    dr = ImageDraw.Draw(sky, "RGBA")
    for x, y, r, o in [(1130, 60, 3, 90), (1010, 40, 2, 60), (1150, 560, 3, 70), (60, 600, 2, 55),
                       (960, 600, 2, 45), (1170, 320, 2, 60), (40, 40, 2, 50), (800, 30, 2, 42)]:
        dr.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, o))
    logo = mark("", "url(#gold)", scale=1).resize((250, 250), Image.LANCZOS)
    sky.paste(logo, (905, 40), logo)
    gold, dim, bila = (243, 196, 118), (163, 176, 218), (255, 255, 255)

    dr.text((70, 42), "Nokturno 7.0", font=font("InterDisplay-Bold.otf", 88), fill=gold)
    dr.text((74, 150), "Osmý zdroj obsahu", font=font("InterDisplay-Medium.otf", 34), fill=dim)
    f = font("InterDisplay-Bold.otf", 76)
    tw = dr.textlength("Přehraj.to", font=f)
    dr.rounded_rectangle((70, 200, 70 + tw + 70, 310), radius=55, fill=gold)
    dr.text((105, 208), "Přehraj.to", font=f, fill=(20, 28, 66))
    dr.text((74 + tw + 110, 232), "funguje i zdarma", font=font("InterDisplay-Medium.otf", 34), fill=bila)

    # Vodorovná linka odděluje novou zprávu od toho, co už v příspěvku stálo.
    dr.line((74, 344, 1130, 344), fill=(90, 106, 168), width=2)
    dr.text((74, 360), "A novinky řady 6.6", font=font("InterDisplay-Medium.otf", 32), fill=dim)
    radky = [("Synchronizace více Kodi", "i bez Home Assistanta"),
             ("Titulky z OpenSubtitles", "česky a slovensky"),
             ("Pro Tebe", "a náhodný film či seriál"),
             ("Přenos nastavení", "do dalšího Kodi kódem"),
             ("Stav zdrojů v menu", "co nefunguje a proč")]
    big, small = font("InterDisplay-Bold.otf", 34), font("InterDisplay-Medium.otf", 26)
    y = 412
    for hlavni, doplnek in radky:
        dr.ellipse((76, y + 12, 92, y + 28), fill=gold)
        dr.text((112, y), hlavni, font=big, fill=bila)
        dr.text((112 + dr.textlength(hlavni, font=big) + 16, y + 6), doplnek, font=small, fill=dim)
        y += 42
    sky.save(path)


def make_social(path):
    """Obrázek k příspěvkům na fórech a Facebooku (1200×630, poměr `RELEASE`). Říká to,
    co doplněk opravdu je: přehrávač vlastního úložiště, a teprve pod tím volitelné
    vyhledávače. Starší obrázky slibovaly „filmy a seriály z úschoven“ jako hlavní
    službu, což neodpovídá ani právnímu upozornění, ani tomu, čím Nokturno je."""
    W, H = RELEASE
    sky = render(f'<rect width="{W}" height="{H}" fill="url(#sky)"/>', W, H, scale=1).convert("RGB")
    q = 4
    glow = Image.new("RGB", (W // q, H // q), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([c / q for c in (640, -120, 1340, 560)], fill=(32, 42, 96))
    sky = ImageChops.add(sky, glow.filter(ImageFilter.GaussianBlur(24)).resize((W, H), Image.BICUBIC))
    dr = ImageDraw.Draw(sky, "RGBA")
    for x, y, r, o in [(1130, 60, 3, 90), (1010, 40, 2, 60), (1150, 560, 3, 70), (60, 600, 2, 55),
                       (960, 600, 2, 45), (1170, 320, 2, 60), (40, 40, 2, 50), (800, 30, 2, 42)]:
        dr.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, o))
    logo = mark("", "url(#gold)", scale=1).resize((250, 250), Image.LANCZOS)
    sky.paste(logo, (905, 60), logo)
    gold, dim, bila = (243, 196, 118), (163, 176, 218), (255, 255, 255)

    dr.text((70, 60), "Nokturno", font=font("InterDisplay-Bold.otf", 104), fill=gold)
    dr.text((74, 190), "Přehrávač tvého vlastního úložiště", font=font("InterDisplay-Bold.otf", 46), fill=bila)
    f = font("InterDisplay-Bold.otf", 46)
    tw = dr.textlength("WebDAV", font=f)
    dr.rounded_rectangle((74, 262, 74 + tw + 60, 336), radius=37, fill=gold)
    dr.text((104, 272), "WebDAV", font=f, fill=(20, 28, 66))
    dr.text((74 + tw + 92, 282), "NAS, Nextcloud, vlastní server",
            font=font("InterDisplay-Medium.otf", 32), fill=bila)

    dr.line((74, 380, 1130, 380), fill=(90, 106, 168), width=2)
    dr.text((74, 400), "volitelně i veřejné vyhledávače třetích stran",
            font=font("InterDisplay-Medium.otf", 30), fill=dim)
    dr.text((74, 444), "WebShare · Sosáč · Sledujteto · FastShare · HellSpy · CZtor · Přehraj.to · Luna",
            font=font("InterDisplay-Medium.otf", 26), fill=dim)
    dr.text((74, 520), "Kodi  ·  Home Assistant  ·  Stremio",
            font=font("InterDisplay-Medium.otf", 40), fill=(203, 212, 240))
    sky.save(path)


SUPPORT = (1600, 700)
BTC = "bc1qhjwt8xxmuym0xsd50yfpvjph00386uz73gqwlc"


def make_support(path):
    """Obrázek podpory do README všech repozitářů: tatáž noční obloha jako fanart,
    vlevo výzva a tři způsoby, vpravo QR kód bitcoinové adresy (`bitcoin:` URI,
    peněženka ho otevře rovnou jako platbu). Vyžaduje navíc `pip install qrcode`."""
    import qrcode

    W, H = SUPPORT
    sky = render(f'<rect width="{W}" height="{H}" fill="url(#sky)"/>', W, H, scale=1).convert("RGB")
    q = 4
    glow = Image.new("RGB", (W // q, H // q), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([c / q for c in (-200, -250, 900, 700)], fill=(30, 40, 92))
    sky = ImageChops.add(sky, glow.filter(ImageFilter.GaussianBlur(30)).resize((W, H), Image.BICUBIC))
    dr = ImageDraw.Draw(sky, "RGBA")
    for x, y, r, o in [(120, 620, 2, 50), (560, 60, 2, 45), (1010, 640, 3, 60), (1540, 70, 2, 55),
                       (930, 110, 2, 40), (40, 300, 2, 35), (1200, 40, 2, 38)]:
        dr.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, o))

    logo = mark("", "url(#gold)", scale=1).resize((150, 150), Image.LANCZOS)
    sky.paste(logo, (86, 70), logo)
    gold, text, dim = (243, 196, 118), (232, 230, 245), (163, 176, 218)
    dr.text((262, 88), "Podpoř Nokturno", font=font("InterDisplay-Bold.otf", 84), fill=gold)
    dr.text((266, 188), "Zdarma a bez reklam. Když ti ušetří večer hledání,", font=font("InterDisplay-Medium.otf", 34), fill=dim)
    dr.text((266, 232), "kafe autorovi udělá radost ☕".replace(" ☕", ""), font=font("InterDisplay-Medium.otf", 34), fill=dim)

    rows = (("Ko-fi", "ko-fi.com/matata86", (255, 94, 91)),
            ("PayPal", "paypal.me/matata86", (0, 112, 186)),
            ("Bitcoin", BTC, (247, 147, 26)))
    y = 330
    for label, value, color in rows:
        dr.rounded_rectangle((86, y, 1130, y + 92), radius=18, fill=(23, 22, 43, 215), outline=(47, 45, 77), width=2)
        dr.rounded_rectangle((86, y, 100, y + 92), radius=7, fill=color)
        dr.text((128, y + 22), label, font=font("InterDisplay-SemiBold.otf", 38), fill=text)
        size = 38
        while size > 22 and dr.textlength(value, font=font("InterDisplay-Medium.otf", size)) > 780:
            size -= 1
        f = font("InterDisplay-Medium.otf", size)
        dr.text((330, y + 46), value, font=f, fill=gold if label != "Bitcoin" else text, anchor="lm")
        y += 112

    code = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(f"bitcoin:{BTC}")
    qr = code.make_image(fill_color="black", back_color="white").convert("RGB").resize((340, 340), Image.NEAREST)
    card = Image.new("RGBA", (380, 380), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle((0, 0, 379, 379), radius=28, fill=(255, 255, 255))
    card.paste(qr, (20, 20))
    sky.paste(card, (1170, 170), card)
    dr.text((1360, 590), "Bitcoin — naskenuj v peněžence", font=font("InterDisplay-Medium.otf", 28), fill=dim, anchor="mm")
    sky.save(path, optimize=True)


def make_outage(path):
    """Obrázek k oznámení výpadku veřejných adres (1200×630, poměr `RELEASE`).
    Stavová tabule: co běží dál a co je mimo. Stav se píše slovem v barevném
    štítku, ne fajfkou — ta v některých fontech chybí a vyjde prázdný proužek
    (stejná past jako u stavu zdrojů v Kodi)."""
    W, H = RELEASE
    sky = render(f'<rect width="{W}" height="{H}" fill="url(#sky)"/>', W, H, scale=1).convert("RGB")
    q = 4
    glow = Image.new("RGB", (W // q, H // q), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([c / q for c in (700, -160, 1400, 520)], fill=(30, 40, 92))
    sky = ImageChops.add(sky, glow.filter(ImageFilter.GaussianBlur(24)).resize((W, H), Image.BICUBIC))
    dr = ImageDraw.Draw(sky, "RGBA")
    for x, y, r, o in [(1120, 54, 3, 85), (1004, 36, 2, 60), (1160, 556, 3, 70), (54, 594, 2, 55),
                       (944, 596, 2, 45), (1174, 318, 2, 60), (36, 36, 2, 50), (790, 26, 2, 42)]:
        dr.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, o))

    logo = mark("", "url(#gold)", scale=1).resize((150, 150), Image.LANCZOS)
    sky.paste(logo, (1010, 40), logo)

    gold, bila, dim = (243, 196, 118), (255, 255, 255), (163, 176, 218)
    ok, bad = (86, 186, 124), (214, 94, 94)

    dr.text((70, 52), "Výpadek Tailscale", font=font("InterDisplay-Bold.otf", 78), fill=gold)
    dr.text((74, 150), "Veřejné adresy Nokturna jsou mimo. Doplněk v Kodi běží dál.",
            font=font("InterDisplay-Medium.otf", 34), fill=bila)

    rows = (("Doplněk v Kodi", "BĚŽÍ", ok),
            ("Integrace v Home Assistantu", "BĚŽÍ", ok),
            ("Aktualizace z repozitáře (GitHub)", "BĚŽÍ", ok),
            ("Doplněk pro Stremio", "MIMO", bad),
            ("Zrcadlo repozitáře na tailnetu", "MIMO", bad),
            ("Žebříčky, TV program, katalogy", "MIMO", bad))

    y = 200
    fl = font("InterDisplay-Medium.otf", 32)
    fs = font("InterDisplay-SemiBold.otf", 26)
    for label, stav, color in rows:
        dr.rounded_rectangle((70, y, 1130, y + 54), radius=14,
                             fill=(23, 22, 43, 200), outline=(47, 45, 77), width=2)
        dr.rounded_rectangle((70, y, 82, y + 54), radius=6, fill=color)
        dr.text((106, y + 27), label, font=fl, fill=bila, anchor="lm")
        tw = dr.textlength(stav, font=fs)
        dr.rounded_rectangle((1110 - tw - 32, y + 11, 1110, y + 43), radius=16, fill=color)
        dr.text((1110 - tw - 16, y + 27), stav, font=fs, fill=(16, 22, 48), anchor="lm")
        y += 62

    dr.text((74, 606), "Instalace i teď: zip repozitáře z GitHubu — odkaz v příspěvku",
            font=font("InterDisplay-Medium.otf", 28), fill=dim, anchor="ls")
    sky.save(path, optimize=True)


def make_ha_brand(icon):
    """Značka pro `custom_components/nokturno/brand/` — stejná čtvercová kresba
    jako ikona doplňku, jen v rozměrech, které chce seznam integrací HA."""
    ha_dir = os.path.join(os.path.dirname(os.path.dirname(ROOT)), "HA", "nokturno-ha",
                          "custom_components", "nokturno", "brand")
    for name, size in (("icon.png", 256), ("icon@2x.png", 512), ("logo.png", 256), ("logo@2x.png", 512)):
        icon.resize((size, size), Image.LANCZOS).save(os.path.join(ha_dir, name))


def main():
    # jméno "icon2.png" (ne "icon.png") je záměr — Kodi/Android si obrázek doplňku
    # drží v texturové cache podle URL/cesty a u lokálního souboru mění jen zřídka;
    # nová kresba tak dostala i novou cestu, ať se stará zaoblená verze fyzicky
    # nemá odkud vrátit (viz stejná past u Dashboardu, dokumentace.md, sekce Značka)
    icon = mark(NIGHT, "url(#gold)").resize((ICON, ICON), Image.LANCZOS)
    icon.save(os.path.join(ROOT, "resources", "media", "icon2.png"))
    # Repozitář dostane plochou variantu téže značky — v seznamu doplňků je tak
    # rozeznatelný od samotného doplňku, ale patří zjevně k němu.
    repo = mark(FLAT, GOLD_FLAT).resize((ICON, ICON), Image.LANCZOS)
    repo.save(os.path.join(ROOT, "repository.nokturno", "resources", "icon.png"))
    make_fanart(os.path.join(ROOT, "resources", "media", "fanart.jpg"))
    make_ha_brand(icon)
    print("resources/media/{icon2.png,fanart.jpg} + repository.nokturno/resources/icon.png "
          "+ ../../HA/nokturno-ha/…/brand/{icon,logo}{,@2x}.png hotovo")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["6.0.0"]:
        make_release_600(os.path.join(ROOT, ".github", "nokturno-6.0.0-cztor.png"))
        print(".github/nokturno-6.0.0-cztor.png hotovo")
    elif sys.argv[1:] == ["7.0.0"]:
        make_release_700(os.path.join(ROOT, ".github", "nokturno-7.0.0-prehrajto.png"))
        print(".github/nokturno-7.0.0-prehrajto.png hotovo")
    elif sys.argv[1:] == ["6.6.0"]:
        make_release_660(os.path.join(ROOT, ".github", "nokturno-6.6.0-novinky.png"))
        print(".github/nokturno-6.6.0-novinky.png hotovo")
    elif sys.argv[1:] == ["social"]:
        make_social(os.path.join(ROOT, ".github", "nokturno-uloziste.png"))
        print(".github/nokturno-uloziste.png hotovo")
    elif sys.argv[1:] == ["vypadek"]:
        make_outage(os.path.join(ROOT, ".github", "nokturno-vypadek.png"))
        print(".github/nokturno-vypadek.png hotovo")
    elif sys.argv[1:] == ["podpora"]:
        make_support(os.path.join(ROOT, ".github", "podpora.png"))
        print(".github/podpora.png hotovo")
    else:
        main()
