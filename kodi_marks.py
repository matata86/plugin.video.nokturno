"""Zhlédnuto nastavené skinem Kodi („Označit jako zhlédnuté“) → evidence Nokturna.

Nokturno si zhlédnuto a rozkoukanost vede samo (`watched.json`, klíč = id titulu), protože
Kodi klíčuje stav celou adresou položky včetně odkazu na stream — jeden film tak mívá ve
videodatabázi Kodi až sedm řádků — a stav se sdílí s Home Assistantem a Traktem. Skin ale
nabízí vlastní „Označit jako zhlédnuté“ a to píše jen do videodatabáze Kodi:

- Kodi 21 stav z databáze na položku pluginu nepřenese, když není `IsPlayable`
  (`CVideoDatabase::GetPlayCounts`) — tituly ve výpisu Nokturna přehratelné nejsou
  (`action=title`, od 5.2.7~beta20), takže fajfka se neukáže a další stisk jen přičítá;
- notifikace `VideoLibrary.OnUpdate` chodí jen u položek z knihovny (`m_iDbId > 0`),
  služba se o změně nedozví.

Proto se čte přímo tabulka `files` (jen pro čtení, jeden dotaz) a porovnává se
s posledním viděným stavem v `kodi_marks.json`. Přičtené přehrání = zhlédnuto, zrušené
zhlédnutí (Kodi nastaví `playCount` i `lastPlayed` na NULL) = nezhlédnuto. Kodi po
označení výpis hned obnoví, takže plugin změnu převezme při jeho kreslení; služba se
dívá ještě jednou za čas kvůli widgetům a synchronizaci.

Obráceně se stav Nokturna zrcadlí do existujících řádků Kodi (`collect(write=…)`):
databáze Kodi pak souhlasí s Nokturnem a každý zásah skinu ji opravdu změní.

Porovnává se jen proti stavu, který už Nokturno jednou vidělo. Při prvním běhu se
jen zapamatuje, co v databázi je — staré řádky z přehrávání by jinak přepsaly
pozdější volby v Nokturnu. Při sdílené databázi (MySQL) soubor nemusí existovat
nebo je zastaralý; pak se nic nemění, protože se nic nemění ani v něm.
"""
import json
import os
import re
import sqlite3
import urllib.parse

PLUGIN_PATH = "plugin://plugin.video.nokturno/"
STATE = "kodi_marks"        # kodi_marks.json v profilu: {"base": true, "rows": {adresa: [playCount, lastPlayed]}}
ACTIONS = ("title", "play")  # položky titulů; soubory (ws:/hs:/dav:) jsou IsPlayable a Kodi je kreslí samo
_DB_RE = re.compile(r"^MyVideos(\d+)\.db$")


def find_db(db_dir):
    """Nejnovější videodatabáze (`MyVideos131.db` u Kodi 21) — po aktualizaci Kodi
    zůstávají ležet i staré soubory s nižším číslem."""
    try:
        names = os.listdir(db_dir)
    except OSError:
        return None
    found = [(int(m.group(1)), name) for name in names for m in [_DB_RE.match(name)] if m]
    return os.path.join(db_dir, max(found)[1]) if found else None


def read_rows(db_path):
    """{adresa položky: (playCount, lastPlayed)} pro řádky Nokturna.

    `None`, když databázi nejde přečíst (chybí, zamčená, jiné schéma) — na rozdíl od
    prázdného výsledku se pak stav z minula nesmí přepsat, jinak by příští čtení
    považovalo všechny řádky za nové."""
    if not db_path or not os.path.isfile(db_path):
        return None
    try:
        # ne URI `mode=ro` — tvar `file:` cesty se liší na Windows; `query_only` stačí,
        # soubor existuje (jinak by ho `connect` založil) a zapisovat do něj nechceme
        conn = sqlite3.connect(db_path, timeout=2)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT files.strFilename, files.playCount, files.lastPlayed FROM files "
            "JOIN path ON files.idPath = path.idPath WHERE path.strPath = ?", (PLUGIN_PATH,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {name: (int(count or 0), last or None) for name, count, last in rows}


def key_of(url):
    """Klíč titulu z adresy položky (`?action=title&type=movie&id=tt0133093` → `tt0133093`)."""
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    if query.get("action") not in ACTIONS:
        return None
    return query.get("id") or None


def diff(rows, seen):
    """Změny od minula: {klíč: True (zhlédnuto) / False (nezhlédnuto)}.

    `seen` je stav z minula ve stejném tvaru jako `rows` (dvojice jako seznamy z JSON).
    Nový řádek se bere jako změna jen tehdy, když ho vytvořil skin — `playCount` > 0,
    nebo zrušené zhlédnutí (NULL/NULL). Řádky z přerušeného přehrávání mění jen
    `lastPlayed`, ty se ignorují. Protichůdné změny téhož klíče (dva řádky) se zahodí."""
    out, conflict = {}, set()
    for url, (count, last) in rows.items():
        prev = seen.get(url)
        if prev is not None and (int(prev[0] or 0), prev[1] or None) == (count, last):
            continue
        key = key_of(url)
        if not key:
            continue
        before = int(prev[0] or 0) if prev is not None else 0
        if count > before:
            mark = True
        elif not count and last is None:
            mark = False
        else:
            continue
        if key in out and out[key] != mark:
            conflict.add(key)
        out[key] = mark
    for key in conflict:
        out.pop(key, None)
    return out


def collect(store, db_dir, apply=None, write=None):
    """Přečte videodatabázi, vrátí změny od minula a zapamatuje si nový stav.

    `apply(changes)` převezme změny do evidence (volá se před zrcadlením).
    `write(adresa, zhlédnuto)` zapíše stav Nokturna zpět do řádku Kodi: jinak by
    zrušení zhlédnutí skinem nad řádkem, který je už prázdný (NULL/NULL), databázi
    nezměnilo a nešlo by ho poznat. Zrcadlí se jen existující řádky titulů, nové se
    nezakládají. Po zápisu se databáze přečte znovu, ať se vlastní zápis nebere jako
    změna od skinu.

    Při prvním běhu (bez `base`) se změny nehlásí, jen se zrcadlí a zapamatuje stav."""
    db_path = find_db(db_dir)
    rows = read_rows(db_path)
    if rows is None:
        return {}
    state = store.load(STATE, {}) or {}
    seen = state.get("rows") or {}
    changes = diff(rows, seen) if state.get("base") else {}
    if apply and changes:
        apply(changes)
    if write:
        wrote = False
        for url, (count, _last) in rows.items():
            key = key_of(url)
            if key is None:
                continue
            want = store.playcount(key) > 0
            if want != (count > 0):
                write(url, want)
                wrote = True
        if wrote:
            again = read_rows(db_path)
            rows = again if again is not None else rows
    fresh = {url: [count, last] for url, (count, last) in rows.items()}
    if not state.get("base") or fresh != seen:
        store.save(STATE, {"base": True, "rows": fresh})
    return changes


def rpc_writer(execute):
    """`write` pro `collect()` nad JSON-RPC Kodi (`xbmc.executeJSONRPC`).

    `Files.SetFileDetails` s nulou nastaví `playCount` i `lastPlayed` na NULL, stejně
    jako zrušení zhlédnutí ze skinu; s jedničkou dá `lastPlayed` = teď."""
    def write(url, watched):
        execute(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "Files.SetFileDetails",
                            "params": {"file": url, "media": "video", "playcount": 1 if watched else 0}}))
    return write
