#!/usr/bin/env python3
"""Ikony položky Hlídané v hlavním menu — zvonek, s novým dílem s oranžovou tečkou.

    python3 tools/make_watch_icon.py   # → resources/media/watch.png, watch-new.png

Tvary jsou Material Design Icons (`bell-ring-outline`, `bell-badge-outline`, licence
Apache 2.0), bílé na průhledném podkladu jako ikony standardní sady skinů. Tečka má
barvu nového dílu ve výpisu (`WATCH_NEW` v default.py).
"""
import os

import cairosvg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "resources", "media")
SIZE = 256

BELL_RING = ("M10,21H14A2,2 0 0,1 12,23A2,2 0 0,1 10,21M21,19V20H3V19L5,17V11C5,7.9 7.03,5.17 10,4.29C10,4.19 "
             "10,4.1 10,4A2,2 0 0,1 12,2A2,2 0 0,1 14,4C14,4.1 14,4.19 14,4.29C16.97,5.17 19,7.9 19,11V17L21,19M17,11"
             "A5,5 0 0,0 12,6A5,5 0 0,0 7,11V18H17V11M19.75,3.19L18.33,4.61C20.04,6.3 21,8.6 21,11H23C23,8.07 "
             "21.84,5.25 19.75,3.19M1,11H3C3,8.6 3.96,6.3 5.67,4.61L4.25,3.19C2.16,5.25 1,8.07 1,11Z")
BELL_BADGE = ("M19 17V11.8C18.5 11.9 18 12 17.5 12H17V18H7V11C7 8.2 9.2 6 12 6C12.1 4.7 12.7 3.6 13.5 2.7C13.2 2.3 "
              "12.6 2 12 2C10.9 2 10 2.9 10 4V4.3C7 5.2 5 7.9 5 11V17L3 19V20H21V19L19 17M10 21C10 22.1 10.9 23 12 "
              "23S14 22.1 14 21H10")
ORANGE = "#E8A33D"


def svg(body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{SIZE}" height="{SIZE}">'
            f'{body}</svg>').encode()


def main():
    cairosvg.svg2png(bytestring=svg(f'<path fill="#fff" d="{BELL_RING}"/>'),
                     write_to=os.path.join(OUT, "watch.png"))
    cairosvg.svg2png(bytestring=svg(f'<path fill="#fff" d="{BELL_BADGE}"/>'
                                    f'<circle cx="17.5" cy="6.5" r="3.5" fill="{ORANGE}"/>'),
                     write_to=os.path.join(OUT, "watch-new.png"))


if __name__ == "__main__":
    main()
