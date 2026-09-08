#!/usr/bin/env python3
"""Sestaví Kodi repozitář do složky repo/ (addons.xml, addons.xml.md5, zipy).

Spouštět z kořene repozitáře po každé změně verze v addon.xml:
    python3 tools/build_repo.py
Kodi si pak z raw.githubusercontent.com stáhne addons.xml, porovná verze
a nabídne/provede aktualizaci.
"""
import hashlib
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(ROOT, "repo")
ADDONS = {
    "plugin.video.nokturno": ROOT,                                   # samotný doplněk = kořen repozitáře
    "repository.nokturno": os.path.join(ROOT, "repository.nokturno"),
}
EXCLUDE = {".git", ".gitignore", "repo", "tools", "repository.nokturno", "__pycache__", ".github"}


def addon_version(path):
    return ET.parse(os.path.join(path, "addon.xml")).getroot().get("version")


def zip_addon(addon_id, src, version):
    out_dir = os.path.join(REPO, addon_id)
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.endswith(".zip"):
            os.remove(os.path.join(out_dir, old))
    out = os.path.join(out_dir, f"{addon_id}-{version}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in EXCLUDE]
            for f in files:
                if f in EXCLUDE or f.endswith((".pyc", ".zip")):
                    continue
                full = os.path.join(base, f)
                zf.write(full, os.path.join(addon_id, os.path.relpath(full, src)))
    # ikona/fanart vedle zipu – Kodi je ukazuje v obchodě ještě před instalací
    for asset in ("icon.png", "fanart.jpg"):
        for cand in (os.path.join(src, "resources", asset), os.path.join(src, asset)):
            if os.path.exists(cand):
                shutil.copy(cand, os.path.join(out_dir, asset))
                break
    return out


def main():
    os.makedirs(REPO, exist_ok=True)
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<addons>"]
    for addon_id, src in ADDONS.items():
        version = addon_version(src)
        out = zip_addon(addon_id, src, version)
        xml = open(os.path.join(src, "addon.xml"), encoding="utf-8").read()
        xml = re.sub(r"<\?xml[^>]*\?>\s*", "", xml).strip()
        parts.append(xml)
        print(f"{addon_id} {version} -> {os.path.relpath(out, ROOT)}")
    parts.append("</addons>")
    addons_xml = "\n".join(parts) + "\n"
    with open(os.path.join(REPO, "addons.xml"), "w", encoding="utf-8") as f:
        f.write(addons_xml)
    with open(os.path.join(REPO, "addons.xml.md5"), "w") as f:
        f.write(hashlib.md5(addons_xml.encode("utf-8")).hexdigest())
    print("repo/addons.xml + md5 hotovo")


if __name__ == "__main__":
    main()
