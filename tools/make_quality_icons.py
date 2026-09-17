#!/usr/bin/env python3
"""Odznaky kvality pro dialog výběru streamu (`default.py::quality_icon`).

    python3 tools/make_quality_icons.py

Nahoře barevný nápis (UHD, FHD, HD, SD), pod ním plný štítek s rozlišením (4K, 1080,
720, 480) a u variant s HDR ještě malý zlatý nápis HDR (bílý by zmizel na bílém
podkladu vybraného řádku v Arctic Fuse). Barvy odpovídají `QUALITY_COLORS`
v `default.py`. Průhledné pozadí, čtverec 256 px — Kodi v dialogu `select(useDetails=True)`
kreslí ikonu položky ve čtverci. Zdroj pravdy je tenhle skript, PNG se přegenerují.
Vyžaduje Pillow a písmo Inter (jen pro vývoj).
"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "resources", "media", "quality")
FONT = "/usr/share/fonts/opentype/inter/Inter-Black.otf"
SIZE = 256
SS = 4   # nadvzorkování kvůli hladkým hranám

# klíč souboru: (horní nápis, štítek, barva)
BADGES = {
    "4k": ("UHD", "4K", (224, 106, 96)),
    "fhd": ("FHD", "1080", (111, 209, 138)),
    "hd": ("HD", "720", (111, 182, 240)),
    "sd": ("SD", "480", (160, 160, 160)),
}


def fit_font(text, max_w, max_h):
    size = max_h
    while size > 8:
        font = ImageFont.truetype(FONT, size)
        left, top, right, bottom = font.getbbox(text)
        if right - left <= max_w and bottom - top <= max_h:
            return font
        size -= 2
    return ImageFont.truetype(FONT, size)


def centered(draw, box, text, font, fill):
    x0, y0, x1, y1 = box
    left, top, right, bottom = font.getbbox(text)
    x = x0 + (x1 - x0 - (right - left)) / 2 - left
    y = y0 + (y1 - y0 - (bottom - top)) / 2 - top
    draw.text((x, y), text, font=font, fill=fill)


def badge(top_text, label, color, hdr):
    s = SIZE * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 14 * SS
    width = s - 2 * pad
    # rozvržení: horní nápis, štítek, volitelně HDR — svisle vystředěné
    top_h, label_h, hdr_h, gap = 70 * SS, 96 * SS, 44 * SS, 10 * SS
    total = top_h + gap + label_h + ((gap + hdr_h) if hdr else 0)
    y = (s - total) // 2
    centered(d, (pad, y, s - pad, y + top_h), top_text, fit_font(top_text, width, top_h), color + (255,))
    y += top_h + gap
    d.rounded_rectangle((pad, y, s - pad, y + label_h), radius=18 * SS, fill=color + (255,))
    centered(d, (pad, y, s - pad, y + label_h), label, fit_font(label, width - 28 * SS, label_h - 26 * SS),
             (20, 20, 24, 255))
    if hdr:
        y += label_h + gap
        centered(d, (pad, y, s - pad, y + hdr_h), "HDR", fit_font("HDR", width, hdr_h), (232, 176, 72, 255))
    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    for key, (top_text, label, color) in BADGES.items():
        for hdr in (False, True):
            name = f"{key}-hdr.png" if hdr else f"{key}.png"
            badge(top_text, label, color, hdr).save(os.path.join(OUT, name), optimize=True)
            print(name)


if __name__ == "__main__":
    main()
