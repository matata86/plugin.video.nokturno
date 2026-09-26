#!/usr/bin/env python3
"""Sestaví Kodi repozitář do složky repo/ (addons.xml, addons.xml.md5, zipy).

Spouštět z kořene repozitáře po každé změně verze v addon.xml:
    python3 tools/build_repo.py          # stabilní verze → repo/
    python3 tools/build_repo.py --beta   # beta („3.2.0~beta1“) → repo-beta/
Stabilní repozitář (`repository.nokturno`) čte jen repo/, beta repozitář
(`repository.nokturno.beta`) repo/ i repo-beta/ a Kodi vezme nejvyšší verzi.
Kodi si pak z raw.githubusercontent.com stáhne addons.xml, porovná verze
a nabídne/provede aktualizaci.
"""
import hashlib
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BETA = "--beta" in sys.argv[1:]
REPO = os.path.join(ROOT, "repo-beta" if BETA else "repo")
ADDONS = {
    "plugin.video.nokturno": ROOT,                                   # samotný doplněk = kořen repozitáře
}
if not BETA:
    # repozitáře jen ve stabilním repo/ — beta repozitář se tak dá nainstalovat
    # i z „Nokturno repozitář“ a ze zrcadel, která repo-beta/ nemají
    ADDONS["repository.nokturno"] = os.path.join(ROOT, "repository.nokturno")
    ADDONS["repository.nokturno.beta"] = os.path.join(ROOT, "repository.nokturno.beta")
EXCLUDE = {".git", ".gitignore", "repo", "repo-beta", "tools", "tests", "repository.nokturno",
           "repository.nokturno.beta", "__pycache__", ".github",
           # v zipu bez užitku: vánoční seznam (torrenty bere jen HA, ale engine.py je
           # importuje, takže prowlarr.py a qbittorrent.py v zipu být musí)
           "lists", "icon-vanoce.png"}


def addon_version(path):
    return ET.parse(os.path.join(path, "addon.xml")).getroot().get("version")


def addon_assets(path):
    """Cesty k ikoně, fanartu a screenshotům přesně tak, jak je addon.xml deklaruje
    (relativně ke kořeni doplňku), jako seznam dvojic (tag, cesta) — screenshotů
    bývá víc. Kodi je při náhledu v repozitáři hledá na
    `<datadir>/<addon_id>/<tahle cesta>`, ne vedle zipu na pevném místě."""
    root = ET.parse(os.path.join(path, "addon.xml")).getroot()
    assets = root.find(".//assets")
    if assets is None:
        return []
    return [(el.tag, el.text.strip()) for el in assets
            if el.tag in ("icon", "fanart", "screenshot") and el.text and el.text.strip()]


def zip_addon(addon_id, src, version):
    is_repo = addon_id.startswith("repository.")
    out_dir = os.path.join(REPO, addon_id)
    os.makedirs(out_dir, exist_ok=True)
    # Zip musí vždycky ležet i pod verzovaným jménem `{addon_id}-{verze}.zip`:
    # při `<datadir zip="true">` si Kodi adresu skládá samo z id a verze v
    # addons.xml, jiné jméno pro něj neexistuje. Repozitářové doplňky k tomu
    # mají ještě kopii pod holým `{addon_id}.zip` — na tu odkazují návody na
    # fóru („Instalovat ze zipu" z URL), proto se její jméno nesmí měnit
    # s verzí. (2026-09-17: verzovaná jména se od 2026-09-15 negenerovala a
    # instalace beta repozitáře z „Nokturno repozitáře" končila 404.)
    out = os.path.join(out_dir, f"{addon_id}-{version}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in EXCLUDE]
            for f in files:
                if f in EXCLUDE or f.endswith((".pyc", ".zip")):
                    continue
                full = os.path.join(base, f)
                zf.write(full, os.path.join(addon_id, os.path.relpath(full, src)))
    if is_repo:
        # Repozitáře se sotva kdy vydávají v nové verzi a starší se nikdy
        # neinstalují zpátky — z verzovaných zipů zůstává jen ta vydávaná.
        shutil.copy(out, os.path.join(out_dir, f"{addon_id}.zip"))
        for name in os.listdir(out_dir):
            if name.startswith(f"{addon_id}-") and name.endswith(".zip") \
                    and name != os.path.basename(out):
                os.remove(os.path.join(out_dir, name))
    # Staré verzované zipy pluginu se nemažou — zůstávají v repu všechny (jde se
    # k nim vrátit ruční instalací ze ZIPu, kdyby nová verze něco pokazila) a
    # navíc to řeší i past 2026-09-10: klient s čerstvě staženou addons.xml, co
    # ještě ukazuje na starou verzi (GitHub raw content se propaguje pár minut),
    # by jinak po starém zipu sáhl a dostal 404.
    # Ikona/fanart musí ležet přesně na cestě, kterou addon.xml deklaruje
    # (u pluginu „resources/icon.png“, u repozitáře jen „icon.png“) — Kodi si
    # při náhledu v Instalovat ze zdroje stahuje `<datadir>/<addon_id>/<ta cesta>`
    # rovnou, bez ohledu na to, kde leží uvnitř zipu. Špatné umístění hlásilo
    # při instalaci chybu (404 na ikonu), i když samotný zip byl v pořádku.
    for tag, rel in addon_assets(src):
        cand = os.path.join(src, rel)
        if not os.path.exists(cand):
            continue
        dest = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(cand, dest)
    return out


def version_key(text):
    """Pořadí jako v Kodi (`CAddonVersion`): '1.5.36' → ((1, 5, 36), …) a část
    za „~“ řadí verzi před stejnou bez ní — '3.2.0~beta1' < '3.2.0'."""
    main, _, tag = text.partition("~")
    nums = tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.\-+]", main))
    tag_key = tuple(int(p) if p.isdigit() else p for p in re.findall(r"\d+|\D+", tag))
    return nums, (0, tag_key) if tag else (1, ())


# addons.xml stahuje každé Kodi při každé kontrole aktualizací. Se všemi verzemi
# a celými <news> měl 7 MB (repo/) a 8 MB (repo-beta/). V „Vyberte verzi" stačí
# posledních pár verzí, starší zipy v repu zůstávají pro ruční instalaci.
MAX_VERSIONS = 10
NEWS_LIMIT = 1500


def short_news(xml):
    """<news> v indexu zkrácené na celé řádky do `NEWS_LIMIT` znaků. addon.xml
    v zipu zůstává celý — Novinky ve verzi v doplňku čtou z něj."""
    def cut(m):
        kept, size = [], 0
        for line in m.group(2).split("\n"):
            if kept and size + len(line) > NEWS_LIMIT:
                break
            kept.append(line)
            size += len(line) + 1
        return m.group(1) + "\n".join(kept) + m.group(3)
    return re.sub(r"(<news>)(.*?)(</news>)", cut, xml, count=1, flags=re.S)


def all_versions_xml(addon_id, out_dir):
    """`<addon>` bloky pro posledních `MAX_VERSIONS` verzí, které v repu leží jako zip.

    Kodi nabízí v „Vyberte verzi" jen to, co je vypsané v addons.xml — samotná
    přítomnost starého zipu nestačí. Metadata každé verze se čtou z addon.xml
    uvnitř jejího zipu, takže se nemusí nikde držet stranou.
    """
    blocks = {}
    for name in os.listdir(out_dir):
        # repozitářové doplňky mají jen `{addon_id}.zip` (viz zip_addon), plugin
        # verzované `{addon_id}-{verze}.zip`
        if name != f"{addon_id}.zip" and not (name.startswith(f"{addon_id}-") and name.endswith(".zip")):
            continue
        with zipfile.ZipFile(os.path.join(out_dir, name)) as zf:
            xml = zf.read(f"{addon_id}/addon.xml").decode("utf-8")
        xml = re.sub(r"<\?xml[^>]*\?>\s*", "", xml).strip()
        blocks[ET.fromstring(xml).get("version")] = short_news(xml)
    return [blocks[v] for v in sorted(blocks, key=version_key)[-MAX_VERSIONS:]]


def check(version):
    """Co se dřív hlídalo jen okem: <news> začíná vydávanou verzí a kopie jádra sedí."""
    news = (ET.parse(os.path.join(ROOT, "addon.xml")).getroot().findtext(".//news") or "").lstrip()
    if not news.startswith(version.split("~")[0]):
        sys.exit(f"<news> v addon.xml nezačíná verzí {version} — doplň řádek s novinkami")
    sync = os.path.join(ROOT, "..", "..", "nokturno-core", "tools", "sync_core.py")
    if os.path.exists(sync):
        import subprocess
        out = subprocess.run([sys.executable, sync, "--check", "kodi"], capture_output=True, text=True)
        if "ke změně: 0 souborů" not in out.stdout:
            sys.exit(f"resources/lib neodpovídá jádru — spusť `python3 tools/sync_core.py kodi` v jádru\n{out.stdout}")


def main():
    version = addon_version(ROOT)
    if "~sync" in version:
        sys.exit(f"{version} je vývojový build větve `sync` — do žádného repozitáře nepatří "
                 f"(boxy si ho berou ručně). Použij tools/build_dev.py")
    if BETA and "~" not in version:
        sys.exit(f"--beta chce verzi s „~“ (např. 3.2.0~beta1), addon.xml má {version}")
    if not BETA and "~" in version:
        sys.exit(f"{version} je beta — spusť s --beta (do stabilního repo/ nepatří)")
    check(version)
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
    print(f"{os.path.basename(REPO)}/addons.xml + md5 hotovo")


if __name__ == "__main__":
    main()
