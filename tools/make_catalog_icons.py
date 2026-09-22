#!/usr/bin/env python3
"""Ikony katalogů z dashboardu (`default.py::DASH_ICONS`).

    python3 tools/make_catalog_icons.py      # → resources/media/catalog/*.png

Kodi má vlastní ikony jen pro pár obecných věcí (filmy, seriály, oblíbené), takže
podkategorie jako Pohádky nebo Romantické by v menu měly všechny tutéž ikonu. Tyhle
piktogramy jsou bílé na barevném kruhu, aby byly čitelné i v malé dlaždici skinu.

Zdroj pravdy je tenhle skript, ne vygenerované PNG — při úpravě se mění tady a soubory
se přegenerují. Kreslí se ve čtyřnásobku a pak zmenšuje, protože PIL nemá vyhlazování
hran (stejný postup jako `make_xmas_icon.py`). Vyžaduje Pillow, jen pro vývoj.
"""
import math
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "resources", "media", "catalog")
SIZE = 256
S = 4          # nadvzorkování
WHITE = (255, 255, 255, 255)

# klíč ikony: barva kruhu
COLORS = {
    "fairytale": (124, 92, 200),    # fialová — pohádky
    "comedy": (240, 168, 48),       # oranžová — komedie
    "romance": (214, 72, 108),      # růžová — romantické
    "family": (72, 160, 148),       # tyrkysová — rodinné
    "animation": (86, 148, 224),    # modrá — animované
}


def _disk(color):
    img = Image.new("RGBA", (SIZE * S, SIZE * S), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([0, 0, SIZE * S - 1, SIZE * S - 1], fill=color + (255,))
    return img


def fairytale(d, n):
    """Koruna: tři hroty na podstavci, uprostřed kamínek."""
    d.polygon([(0.22 * n, 0.68 * n), (0.22 * n, 0.34 * n), (0.355 * n, 0.50 * n),
               (0.50 * n, 0.28 * n), (0.645 * n, 0.50 * n), (0.78 * n, 0.34 * n),
               (0.78 * n, 0.68 * n)], fill=WHITE)
    d.rounded_rectangle([0.22 * n, 0.70 * n, 0.78 * n, 0.78 * n], radius=0.03 * n, fill=WHITE)
    for x in (0.22, 0.50, 0.78):   # kuličky na hrotech
        d.ellipse([(x - 0.045) * n, 0.26 * n, (x + 0.045) * n, 0.35 * n], fill=WHITE)


def comedy(d, n):
    """Obličej se smíchem: oči a úsměv se vyřežou z bílého kruhu."""
    d.ellipse([0.18 * n, 0.18 * n, 0.82 * n, 0.82 * n], fill=WHITE)
    hole = (0, 0, 0, 0)
    # ústa: z kruhové výseče zůstane jen dolní oblouk, proto se horní půlka vrátí bílou
    d.pieslice([0.30 * n, 0.38 * n, 0.70 * n, 0.72 * n], start=20, end=160, fill=hole)
    d.rectangle([0.30 * n, 0.38 * n, 0.70 * n, 0.55 * n], fill=WHITE)
    for x in (0.36, 0.64):   # oči až nakonec, jinak je ten obdélník přemaže
        d.ellipse([(x - 0.055) * n, 0.33 * n, (x + 0.055) * n, 0.47 * n], fill=hole)


def romance(d, n):
    """Srdce: dva kruhy a špička dolů."""
    d.ellipse([0.18 * n, 0.24 * n, 0.52 * n, 0.58 * n], fill=WHITE)
    d.ellipse([0.48 * n, 0.24 * n, 0.82 * n, 0.58 * n], fill=WHITE)
    d.polygon([(0.185 * n, 0.44 * n), (0.815 * n, 0.44 * n), (0.50 * n, 0.80 * n)], fill=WHITE)


def family(d, n):
    """Dvě postavy vedle sebe, jedna menší — hlava a ramena."""
    for cx, r, top in ((0.36, 0.115, 0.22), (0.66, 0.085, 0.32)):
        d.ellipse([(cx - r) * n, top * n, (cx + r) * n, (top + 2 * r) * n], fill=WHITE)
        body = 0.30 if r > 0.10 else 0.22
        d.pieslice([(cx - body / 2) * n, (top + 2 * r + 0.02) * n,
                    (cx + body / 2) * n, (top + 2 * r + 0.02 + body) * n],
                   start=180, end=360, fill=WHITE)
        d.rectangle([(cx - body / 2) * n, (top + 2 * r + 0.02 + body / 2) * n,
                     (cx + body / 2) * n, 0.78 * n], fill=WHITE)


def animation(d, n):
    """Tlapka: čtyři prsty a polštářek."""
    for x, y, r in ((0.24, 0.33, 0.072), (0.41, 0.23, 0.078), (0.59, 0.23, 0.078), (0.76, 0.33, 0.072)):
        d.ellipse([(x - r) * n, (y - r * 1.15) * n, (x + r) * n, (y + r * 1.15) * n], fill=WHITE)
    d.ellipse([0.28 * n, 0.45 * n, 0.72 * n, 0.82 * n], fill=WHITE)


SHAPES = {"fairytale": fairytale, "comedy": comedy, "romance": romance,
          "family": family, "animation": animation}


def main():
    os.makedirs(OUT, exist_ok=True)
    n = SIZE * S
    for key, color in COLORS.items():
        img = _disk(color)
        layer = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        SHAPES[key](ImageDraw.Draw(layer), n)
        img.alpha_composite(layer)
        img.resize((SIZE, SIZE), Image.LANCZOS).save(os.path.join(OUT, f"{key}.png"))
        print(f"{key}.png")


if __name__ == "__main__":
    main()
