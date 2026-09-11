#!/usr/bin/env python3
"""Značka Nokturna — ikona doplňku, ikona repozitáře a fanart.

    python3 tools/make_icon.py

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
NIGHT = ('<rect width="512" height="512" rx="112" fill="url(#sky)"/>'
         '<rect width="512" height="512" rx="112" fill="url(#glow)"/>' + STARS +
         '<rect x="1.5" y="1.5" width="509" height="509" rx="110.5" fill="none" '
         'stroke="url(#rim)" stroke-width="3"/>')
FLAT = f'<rect width="512" height="512" rx="112" fill="{SKY_FLAT}"/>'


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

    dr.text((170, 398), "Nokturno", font=font("InterDisplay-Bold.otf", 152), fill=(243, 196, 118))
    # Podtitulek se musí vejít vedle značky, proto se písmo zmenšuje, dokud
    # se řádek nevejde — s přibývajícími zdroji by jinak zajel pod logo.
    sub = "WebShare  ·  Sosáč  ·  Luna  ·  HellSpy  ·  Home Assistant"
    size = 44
    while size > 26:
        f = font("InterDisplay-Medium.otf", size)
        if dr.textlength(sub, font=f) <= 950:
            break
        size -= 2
    dr.text((177, 592), sub, font=font("InterDisplay-Medium.otf", size), fill=(163, 176, 218))
    sky.save(path, quality=92, subsampling=0)


def main():
    icon = mark(NIGHT, "url(#gold)").resize((ICON, ICON), Image.LANCZOS)
    icon.save(os.path.join(ROOT, "resources", "media", "icon.png"))
    # Repozitář dostane plochou variantu téže značky — v seznamu doplňků je tak
    # rozeznatelný od samotného doplňku, ale patří zjevně k němu.
    repo = mark(FLAT, GOLD_FLAT).resize((ICON, ICON), Image.LANCZOS)
    repo.save(os.path.join(ROOT, "repository.nokturno", "resources", "icon.png"))
    make_fanart(os.path.join(ROOT, "resources", "media", "fanart.jpg"))
    print("resources/media/{icon.png,fanart.jpg} + repository.nokturno/resources/icon.png hotovo")


if __name__ == "__main__":
    main()
