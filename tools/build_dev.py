#!/usr/bin/env python3
"""Sestaví vývojový build větve `sync` — mimo repo/ i repo-beta/.

Větev `sync` (synchronizace více Kodi bez Home Assistanta) se **nevydává**:
nejde do Kodi repozitářů, na GitHub jako release ani do zrcadel. Na Office
a na CoreELEC se nosí ručně, proto tenhle build umí jen dvě věci — připravit
čistý strom doplňku a zip, a vypsat příkazy, kterými se nahraje.

    python3 tools/build_dev.py

Staví do `/tmp/nokturno-sync/`, ne do složky projektu: ta má v cestě mezery
a `smbclient` na nich `lcd` rozbije stejně tiše jako na relativní cestě
(„file does not exist“, viz `pristupy.md`). Vyloučené soubory bere ze
sdíleného `EXCLUDE` v `build_repo.py`, ať se pravidla neduplikují — plus
`.git*`, který v zipu repozitáře nevadí, ale v ručně nahrávaném stromu ano.
"""
import os
import shutil
import subprocess
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_repo import EXCLUDE, ROOT, addon_version  # noqa: E402

ADDON_ID = "plugin.video.nokturno"
OUT = "/tmp/nokturno-sync"
DROP = EXCLUDE | {".git", ".gitignore", ".github", "dist"}


def branch():
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def build_tree(dest):
    """Čistý strom doplňku — co by jinak `mput *` natáhlo na box (`.git/`,
    `__pycache__/`, testy) sem nepatří."""
    files = 0
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in DROP]
        for name in names:
            if name in DROP or name.endswith((".pyc", ".zip")):
                continue
            src = os.path.join(base, name)
            rel = os.path.relpath(src, ROOT)
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)
            files += 1
    return files


def main():
    version = addon_version(ROOT)
    if "~sync" not in version:
        sys.exit(f"addon.xml má {version} — vývojový build chce verzi s „~sync“ "
                 f"(např. 9.99.0~sync1), ať ji repozitář na boxu nikdy nepřebije")

    shutil.rmtree(OUT, ignore_errors=True)
    tree = os.path.join(OUT, ADDON_ID)
    files = build_tree(tree)

    zip_path = os.path.join(OUT, f"{ADDON_ID}-{version}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, names in os.walk(tree):
            for name in names:
                full = os.path.join(base, name)
                zf.write(full, os.path.join(ADDON_ID, os.path.relpath(full, tree)))

    size = os.path.getsize(zip_path) / 1024
    print(f"{ADDON_ID} {version} (větev {branch()}) — {files} souborů, {size:.0f} kB")
    print(f"  strom: {tree}")
    print(f"  zip:   {zip_path}")
    print(f"""
Obývák (CoreELEC 192.168.1.21) — Kodi se po Application.Quit nastartuje samo:
  smbclient -N //192.168.1.21/Addons -c 'recurse ON; prompt OFF; lcd {tree}; cd {ADDON_ID}; mput *'

Office (Android 192.168.1.22) — port se po restartu boxu mění, napřed `adb devices`;
před nahráním se domluv s ostatními session, kdo box zrovna má:
  D=/storage/emulated/0/Android/data/org.xbmc.kodi/files/.kodi/addons/{ADDON_ID}
  adb -s 192.168.1.22:<port> push {tree}/. "$D/"

Restart jen `Application.Quit` a počkat na zmizení procesu — nikdy `am force-stop`
(2026-09-16 shodil Addons33.db a vypnul 37 doplňků včetně skinu).""")


if __name__ == "__main__":
    main()
