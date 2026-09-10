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


def addon_assets(path):
    """Cesty k ikoně/fanartu přesně tak, jak je addon.xml deklaruje (relativně
    ke kořeni doplňku) — Kodi je při náhledu v repozitáři hledá na
    `<datadir>/<addon_id>/<tahle cesta>`, ne vedle zipu na pevném místě."""
    root = ET.parse(os.path.join(path, "addon.xml")).getroot()
    assets = root.find(".//assets")
    if assets is None:
        return {}
    out = {}
    for tag in ("icon", "fanart"):
        el = assets.find(tag)
        if el is not None and el.text:
            out[tag] = el.text.strip()
    return out


def zip_addon(addon_id, src, version):
    out_dir = os.path.join(REPO, addon_id)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{addon_id}-{version}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in EXCLUDE]
            for f in files:
                if f in EXCLUDE or f.endswith((".pyc", ".zip")):
                    continue
                full = os.path.join(base, f)
                zf.write(full, os.path.join(addon_id, os.path.relpath(full, src)))
    if addon_id.startswith("repository."):
        # stabilní název pro odkaz v README (verzovaný zip zůstává pro Kodi)
        shutil.copy(out, os.path.join(out_dir, f"{addon_id}.zip"))
    # Staré verzované zipy se nemažou — zůstávají v repu všechny (jde se k nim
    # vrátit ruční instalací ze ZIPu, kdyby nová verze něco pokazila) a navíc
    # to řeší i past 2026-09-10: klient s čerstvě staženou addons.xml, co ještě
    # ukazuje na starou verzi (GitHub raw content se propaguje pár minut),
    # by jinak po starém zipu sáhl a dostal 404.
    # Ikona/fanart musí ležet přesně na cestě, kterou addon.xml deklaruje
    # (u pluginu „resources/icon.png“, u repozitáře jen „icon.png“) — Kodi si
    # při náhledu v Instalovat ze zdroje stahuje `<datadir>/<addon_id>/<ta cesta>`
    # rovnou, bez ohledu na to, kde leží uvnitř zipu. Špatné umístění hlásilo
    # při instalaci chybu (404 na ikonu), i když samotný zip byl v pořádku.
    for tag, rel in addon_assets(src).items():
        cand = os.path.join(src, rel)
        if not os.path.exists(cand):
            continue
        dest = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(cand, dest)
    return out


def version_key(text):
    """'1.5.36' → (1, 5, 36); nečíselné části jdou dozadu."""
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.\-+]", text))


def all_versions_xml(addon_id, out_dir):
    """`<addon>` bloky pro všechny verze, které v repu leží jako zip.

    Kodi nabízí v „Vyberte verzi" jen to, co je vypsané v addons.xml — samotná
    přítomnost starého zipu nestačí. Metadata každé verze se čtou z addon.xml
    uvnitř jejího zipu, takže se nemusí nikde držet stranou.
    """
    blocks = {}
    for name in os.listdir(out_dir):
        if not (name.startswith(f"{addon_id}-") and name.endswith(".zip")):
            continue
        with zipfile.ZipFile(os.path.join(out_dir, name)) as zf:
            xml = zf.read(f"{addon_id}/addon.xml").decode("utf-8")
        xml = re.sub(r"<\?xml[^>]*\?>\s*", "", xml).strip()
        blocks[ET.fromstring(xml).get("version")] = xml
    return [blocks[v] for v in sorted(blocks, key=version_key)]


def main():
    os.makedirs(REPO, exist_ok=True)
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<addons>"]
    for addon_id, src in ADDONS.items():
        version = addon_version(src)
        out = zip_addon(addon_id, src, version)
        blocks = all_versions_xml(addon_id, os.path.dirname(out))
        parts.extend(blocks)
        print(f"{addon_id} {version} -> {os.path.relpath(out, ROOT)} ({len(blocks)} verzí v seznamu)")
    parts.append("</addons>")
    addons_xml = "\n".join(parts) + "\n"
    with open(os.path.join(REPO, "addons.xml"), "w", encoding="utf-8") as f:
        f.write(addons_xml)
    with open(os.path.join(REPO, "addons.xml.md5"), "w") as f:
        f.write(hashlib.md5(addons_xml.encode("utf-8")).hexdigest())
    print("repo/addons.xml + md5 hotovo")


if __name__ == "__main__":
    main()
