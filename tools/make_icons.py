#!/usr/bin/env python3
"""Jednotná sada ikon menu (`default.py::icon()`).

    python3 tools/make_icons.py          # → resources/media/icons/*.png

Kodi dodává ikony ze skinu (`DefaultMovies.png` a spol.), jenže jejich sada je
malá a v každém skinu jiná: žánrovou ikonu má jednu pro všechny žánry, sezónní
nemá žádnou a půlka položek menu tak nese ikonu, která s ní nesouvisí. Doplněk
si proto nese vlastní sadu v jednom stylu — Material Design Icons (Apache 2.0),
bílá silueta na průhledném pozadí, stejně jako je kreslí skiny.

Zdroj pravdy je tenhle skript: jméno ikony v MDI je celý recept, PNG se dá
kdykoli přegenerovat. Potřebuje síť (`MDI_URL`), Pillow a cairosvg — jen pro
vývoj, hotová PNG jsou v repozitáři.

Silueta má tmavý kruh na pozadí (`CIRCLE_*`): na Arctic Fuse se ikona kreslí
i jako zvětšený náhled (thumb) a bílý glyf na průhledném pozadí v něm zmizel
do prázdna — kruh dává kontrast bez ohledu na barvu podkladu skinu.
"""
import io
import os
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "resources", "media", "icons")
SIZE = 256
PAD = 0.20           # okraj glyfu uvnitř kruhu
CIRCLE_PAD = 0.04     # okraj kruhu uvnitř dlaždice
CIRCLE_FILL = (32, 32, 32, 235)
MDI_URL = "https://cdn.jsdelivr.net/npm/@mdi/svg@7.4.47/svg/{}.svg"

# jméno u nás → jméno v MDI (https://pictogrammers.com/library/mdi/)
ICONS = {
    # kořen a procházení
    "search": "magnify", "continue": "play-circle", "movies": "movie-open",
    "series": "television-classic", "tvguide": "television-guide", "mylist": "bookmark-multiple",
    "storage": "harddisk", "downloads": "download", "settings": "cog", "wizard": "wizard-hat",
    "news": "newspaper-variant-outline", "accounts": "shield-check", "check": "clipboard-check",
    # seznamy filmů a seriálů
    "foryou": "thumb-up", "popular": "fire", "trending": "trending-up", "toprated": "trophy",
    "newadded": "movie-plus", "random": "shuffle-variant", "tap": "gesture-tap",
    "genre": "shape", "all": "playlist-play", "similar": "movie-search", "recent": "history",
    # ostatní
    "folder": "folder", "more": "dots-horizontal-circle", "clear": "delete", "sync": "sync",
    "ha": "home-assistant", "calendar": "calendar", "channel": "broadcast", "ended": "clock-end",
    "warning": "alert", "error": "alert-circle",
    # katalogy z dashboardu (`dash_api.ICONS`)
    "star": "star", "crown": "crown", "masks": "drama-masks", "heart": "heart",
    "family": "account-group", "paw": "paw", "tree": "pine-tree", "pumpkin": "halloween",
    "calendar-star": "calendar-star",
}


def render(mdi_name):
    # až tady, ať se mapa `ICONS` dá přečíst i bez Pillow a cairosvg (testy doplňku)
    import cairosvg
    from PIL import Image, ImageDraw

    with urllib.request.urlopen(MDI_URL.format(mdi_name), timeout=20) as resp:
        svg = resp.read().decode("utf-8")
    svg = svg.replace("<path ", '<path fill="#ffffff" ', 1)
    side = round(SIZE * (1 - 2 * PAD))
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=side, output_height=side)
    glyph = Image.open(io.BytesIO(png)).convert("RGBA")

    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    circle_r = round(SIZE * (1 - 2 * CIRCLE_PAD) / 2)
    center = SIZE // 2
    draw = ImageDraw.Draw(img)
    draw.ellipse(
        (center - circle_r, center - circle_r, center + circle_r, center + circle_r),
        fill=CIRCLE_FILL,
    )
    img.alpha_composite(glyph, ((SIZE - side) // 2, (SIZE - side) // 2))
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, mdi_name in ICONS.items():
        render(mdi_name).save(os.path.join(OUT, f"{name}.png"))
        print(f"{name}.png ({mdi_name})")


if __name__ == "__main__":
    main()
