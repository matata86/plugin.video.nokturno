#!/usr/bin/env python3
"""Vánoční varianta ikony doplňku — čepice na „N", sníh, mrazivé nebe.

Vychází z `resources/media/icon2.png`, nepřekresluje ji: identita (zlaté N z filmového
pásu a měsíc) musí zůstat, jinak lidem v seznamu doplňků zmizí to, co znají.

    python3 tools/make_xmas_icon.py            # → resources/media/icon-vanoce.png
    python3 tools/make_xmas_icon.py --apply    # rovnou i jako resources/media/icon2.png

Kreslí se ve čtyřnásobku a pak zmenšuje, protože PIL nemá vyhlazování hran.
"""
import argparse
import os
import random

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "resources", "media", "icon2.png")
OUT = os.path.join(ROOT, "resources", "media", "icon-vanoce.png")
S = 4  # nadvzorkování

RED, RED_DARK = (198, 40, 44), (154, 25, 30)
SNOW = (248, 250, 255)


def frost(img):
    """Chladnější podklad — o Vánocích má nebe blíž k modré než k fialové."""
    r, g, b, a = img.split()
    b = b.point(lambda v: min(255, int(v * 1.12 + 10)))
    r = r.point(lambda v: int(v * 0.96))
    return Image.merge("RGBA", (r, g, b, a))


def santa_hat(size):
    """Čepice zvlášť, aby šla natočit — pootočená působí líp než symetrická."""
    w, h = size, int(size * 0.95)
    hat = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(hat)
    # kužel: špička doprava, aby se čepice „skládala" přes levý dřík písmene
    d.polygon([(w * 0.06, h * 0.80), (w * 0.44, h * 0.04), (w * 0.92, h * 0.52)],
              fill=RED)
    d.polygon([(w * 0.44, h * 0.04), (w * 0.92, h * 0.52), (w * 0.62, h * 0.52)],
              fill=RED_DARK)          # stín na odvrácené straně
    band = [w * 0.00, h * 0.72, w * 0.78, h * 0.99]
    # poloměr musí zůstat pod polovinou výšky pruhu, jinak PIL spadne
    d.rounded_rectangle(band, radius=int(h * 0.12), fill=SNOW)   # kožešinový lem
    pom = h * 0.24
    d.ellipse([w * 0.84, h * 0.44, w * 0.84 + pom, h * 0.44 + pom], fill=SNOW)
    return hat


def snowflakes(size, count, seed=7):
    """Vločky: pár velkých vepředu, drobné vzadu — jinak to vypadá jako šum."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rnd = random.Random(seed)
    for i in range(count):
        r = rnd.uniform(size * 0.006, size * 0.020)
        x, y = rnd.uniform(0, size), rnd.uniform(0, size * 0.92)
        alpha = int(rnd.uniform(90, 235))
        d.ellipse([x - r, y - r, x + r, y + r], fill=SNOW + (alpha,))
    return layer


def snow_caps(size):
    """Závěje na spodní hraně — ikona tak stojí v krajině, ne ve vzduchoprázdnu."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    base = size * 0.905
    d.ellipse([-size * 0.10, base, size * 0.55, base + size * 0.22], fill=SNOW + (235,))
    d.ellipse([size * 0.30, base + size * 0.02, size * 1.10, base + size * 0.24], fill=SNOW + (235,))
    d.rectangle([0, base + size * 0.085, size, size], fill=SNOW + (235,))
    return layer


def build():
    src = Image.open(SRC).convert("RGBA")
    size = src.size[0] * S
    img = frost(src.resize((size, size), Image.LANCZOS))

    img.alpha_composite(snow_caps(size))

    # levý dřík „N" začíná na x 0.32–0.40 a jeho horní hrana je na y 0.33 —
    # čepice musí dosednout přesně tam, jinak se vznáší nebo zakrývá písmeno
    hat = santa_hat(int(size * 0.26)).rotate(-12, resample=Image.BICUBIC, expand=True)
    img.alpha_composite(hat, (int(size * 0.250), int(size * 0.098)))

    flakes = snowflakes(size, 46)
    img.alpha_composite(flakes.filter(ImageFilter.GaussianBlur(size * 0.0015)))

    out = img.resize(src.size, Image.LANCZOS)
    # maska původní ikony: zaoblené rohy musí zůstat průhledné i po vrstvení
    out.putalpha(Image.composite(out.split()[3], src.split()[3], src.split()[3].point(lambda v: 255 if v == 0 else 0)))
    out.putalpha(src.split()[3])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="uložit i jako resources/media/icon2.png")
    args = ap.parse_args()
    icon = build()
    icon.save(OUT)
    print("→", os.path.relpath(OUT, ROOT))
    if args.apply:
        icon.save(SRC)
        print("→", os.path.relpath(SRC, ROOT), "(přepsáno)")


if __name__ == "__main__":
    main()
