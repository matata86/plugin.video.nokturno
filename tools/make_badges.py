#!/usr/bin/env python3
"""Odznaky kvality do seznamu streamů.

    python3 tools/make_badges.py     # → resources/media/badges/*.png

Kodi v popisku položky neumí barevný rámeček, umí ale u každého řádku obrázek.
Odznak se proto kreslí jako malé PNG a sází se místo plakátu: v seznamu streamů
je plakát u všech řádků stejný, takže neříká nic, kdežto kvalita rozhoduje.

Jméno souboru je `<kvalita>.png`, s příznakem `<kvalita>-hdr.png` / `-dv.png`.
"""
import io
import os

import cairosvg
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "resources", "media", "badges")
W, H = 200, 200   # čtverec: Kodi dává ikoně čtvercové místo, obdélník by v něm plaval

# krátký název, číselná podoba a barva podle kvality
LEVELS = {
    "4k":      ("UHD", "4K", "#C0392B", "#8E2A20"),
    "fullhd":  ("FHD", "1080", "#2E8B57", "#1F6140"),
    "hd":      ("HD", "720", "#2C6FB5", "#1E4E80"),
    "sd":      ("SD", "576", "#5A5A5A", "#3E3E3E"),
}
FLAGS = {"": None, "hdr": ("HDR", "#D4A017"), "dv": ("DV", "#6C3FA0")}


def svg(top, bottom, light, dark, flag):
    """Čtvercový odznak: nahoře zkratka, pod ní číselná podoba, dole příznak.

    Rozdělení je pevné, ať mají všechny odznaky text na stejné výšce a v seznamu
    pod sebou lícují.
    """
    strip = 42 if flag else 0
    body = H - strip
    half = body // 2
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           f'<rect x="4" y="4" width="{W-8}" height="{H-8}" rx="18" fill="{dark}"/>',
           f'<rect x="4" y="4" width="{W-8}" height="{half}" rx="18" fill="{light}"/>',
           f'<rect x="4" y="{half-18}" width="{W-8}" height="18" fill="{light}"/>',
           f'<text x="{W//2}" y="{half-22}" text-anchor="middle" font-family="DejaVu Sans, sans-serif"'
           f' font-size="58" font-weight="bold" fill="#FFFFFF">{top}</text>',
           f'<text x="{W//2}" y="{body-20}" text-anchor="middle" font-family="DejaVu Sans, sans-serif"'
           f' font-size="52" font-weight="bold" fill="#FFFFFF">{bottom}</text>']
    if flag:
        name, colour = flag
        out.append(f'<rect x="4" y="{H-strip-4}" width="{W-8}" height="{strip}" rx="14" fill="{colour}"/>')
        out.append(f'<rect x="4" y="{H-strip-4}" width="{W-8}" height="14" fill="{colour}"/>')
        out.append(f'<text x="{W//2}" y="{H-14}" text-anchor="middle" font-family="DejaVu Sans, sans-serif"'
                   f' font-size="30" font-weight="bold" fill="#FFFFFF">{name}</text>')
    out.append("</svg>")
    return "".join(out)


def main():
    os.makedirs(OUT, exist_ok=True)
    made = []
    for key, (top, bottom, light, dark) in LEVELS.items():
        for suffix, flag in FLAGS.items():
            name = f"{key}-{suffix}.png" if suffix else f"{key}.png"
            png = cairosvg.svg2png(bytestring=svg(top, bottom, light, dark, flag).encode(),
                                   output_width=W * 2, output_height=H * 2)
            Image.open(io.BytesIO(png)).resize((W, H), Image.LANCZOS).save(os.path.join(OUT, name))
            made.append(name)
    print(f"{len(made)} odznaků v resources/media/badges/")


if __name__ == "__main__":
    main()
