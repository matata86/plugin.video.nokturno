"""Český a slovenský název titulu podle IMDb id — z Wikidat, bez klíče a bez účtu.

Bez Luny a bez vlastního klíče TMDB (typicky doplněk pro Stremio) zná jádro jen
anglický název z Cinemety. Soubory na WebShare, HellSpy a Sledujteto se ale
jmenují česky („Harry Potter a Ohnivý pohár 2005 CZ dabing") a přísný filtr
názvu je pak zahodil jako jiný titul. Wikidata mají u filmů i seriálů vazbu na
IMDb (vlastnost P345) a lokalizované názvy:

    action=query&list=search&srsearch=haswbstatement:P345=<tt…>   → Q-id
    action=wbgetentities&ids=<Q>&props=labels|aliases&languages=cs|sk

Wikimedia chce popisný User-Agent. Výsledek si jádro cachuje (30 dní).
"""
import json
import urllib.parse
import urllib.request

API = "https://www.wikidata.org/w/api.php"
TIMEOUT = 10
UA = "Nokturno/3 (https://github.com/matata86/nokturno-core)"
LANGS = ("cs", "sk")
ALIASES_MAX = 2


class WikidataError(Exception):
    pass


def _get(**params):
    url = API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as err:  # noqa: BLE001 – síť, DNS, rozsypaný JSON
        raise WikidataError(str(err)[:120]) from err


def local_titles(imdb_id):
    """[český název, slovenský název, české alternativní názvy…] — bez duplicit, může být prázdné."""
    if not str(imdb_id or "").startswith("tt"):
        return []
    found = (_get(action="query", list="search", srsearch=f"haswbstatement:P345={imdb_id}", srlimit=1)
             .get("query") or {}).get("search") or []
    if not found:
        return []
    qid = found[0].get("title")
    entity = ((_get(action="wbgetentities", ids=qid, props="labels|aliases", languages="|".join(LANGS))
               .get("entities") or {}).get(qid)) or {}
    names = [(entity.get("labels") or {}).get(lang, {}).get("value") for lang in LANGS]
    names += [a.get("value") for a in ((entity.get("aliases") or {}).get("cs") or [])[:ALIASES_MAX]]
    out = []
    for name in names:
        if name and name not in out:
            out.append(name)
    return out
