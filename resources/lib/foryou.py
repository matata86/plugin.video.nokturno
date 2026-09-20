"""„Pro tebe“ — doporučení z toho, co uživatel doopravdy viděl.

Vlastní síť tenhle modul nemá. Dostane hotovou funkci, která k jednomu titulu
vrátí podobné (`TmdbApi.similar`, bez klíče TMDB `DashApi.similar` — dashboard se
na TMDB zeptá za doplněk), a jen je sloučí, seřadí a odfiltruje. Jde ho proto
testovat bez sítě a použít z kterékoli větve rodiny.

**Počítá se u klienta, ne na serveru.** Historie zhlédnutí je jen v profilu
doplňku (`Store`, klíč `watched`) — server ji nemá a mít nemá. Stejnou úvahou
odmítla 6.2.2 brát jazykové katalogy z dashboardu: co jde spočítat z toho, co
klient už má, nemá cenu vykupovat novým veřejným endpointem a cizími účty.

Cena na TMDB (studená cache, `SEEDS` × `PER_SEED`): na vzor `/find/tt…` plus
`/{kind}/{id}/recommendations`, tedy 2 dotazy (výjimečně 3, když doporučení je
míň než 10 a `similar()` dobírá z `/similar`), cache 7 dní. K tomu `_details` na
každý výsledný titul — ty jsou ale cachované 30 dní a sdílené se všemi ostatními
katalogy, takže populární tituly bývají zahřáté z Populárních. Nový zhlédnutý
titul zdraží jen o jeden studený vzor.
"""
import random
import re

SEEDS = 5        # kolik naposledy zhlédnutých titulů slouží jako vzor
PER_SEED = 12    # kolik doporučení se od každého vzoru vezme
LIMIT = 40       # kolik položek má výsledný seznam nejvýš

# Celý doplněk stojí na IMDb id; doporučení umíme jen k nim. Soubory z úložiště
# (`ws:`/`hs:`/`dav:`/`dl:`) ani vlastní id Sosáče (`sosacd_…`) TMDB nezná.
IMDB_RE = re.compile(r"^tt\d{6,10}$")


def split_episode_id(item_id):
    """'tt0903747:1:2' → ('tt0903747', 1, 2); 'tt0133093' → ('tt0133093', None, None)."""
    parts = str(item_id).split(":")
    if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
        return ":".join(parts[:-2]), int(parts[-2]), int(parts[-1])
    return str(item_id), None, None


def seed_ids(rows, ctype, limit=SEEDS):
    """Vzory pro doporučení: naposledy zhlédnuté tituly daného typu.

    `rows`: `[(klíč, záznam)]` z `Store.recently_watched()`, nejnovější první.
    Klíč epizody se zkrátí na id seriálu, takže seriál vystupuje jako jeden vzor,
    ne jako pět dílů za sebou. Typ se pozná podle toho, jestli klíč nese sezónu
    a díl — vlastní příznak u záznamu `watched` není a doplňovat ho zpětně by
    znamenalo dotaz na API u každé položky historie.
    """
    want = "series" if ctype == "series" else "movie"
    out = []
    for key, _entry in rows:
        base, season, _episode = split_episode_id(key)
        if ("series" if season is not None else "movie") != want:
            continue
        if not IMDB_RE.match(base) or base in out:
            continue
        out.append(base)
        if len(out) >= limit:
            break
    return out


def known_ids(*row_groups):
    """Id všeho, co uživatel viděl nebo rozkoukal — doporučovat mu to nemá smysl."""
    return {split_episode_id(key)[0] for rows in row_groups for key, _entry in rows}


def recommend(seeds, similar_fn, skip=(), limit=LIMIT, per_seed=PER_SEED):
    """Sloučí doporučení k jednotlivým vzorům do jednoho seznamu.

    Pořadí: napřed tituly, které doporučuje víc vzorů (shoda dvou různých filmů je
    silnější signál než první místo u jednoho), pak podle nejlepšího pořadí, jaké
    titul u některého vzoru měl, a při rovnosti podle stáří vzoru (`seeds` chodí
    od nejnovějšího).

    Každá položka nese navíc `_because` — id vzoru, u kterého se objevila poprvé.
    Klient z něj udělá „Protože jsi viděl …“ podle snímku titulu, takže to nestojí
    žádný dotaz navíc.

    `similar_fn(id)` vrací položky ve tvaru katalogu; výpadek zdroje si ošetřuje
    volající a sem pošle prázdný seznam. `skip`: id, která se mají vynechat.
    """
    skip = set(skip) | set(seeds)
    found, order = {}, []
    for rank, seed in enumerate(seeds):
        for pos, item in enumerate((similar_fn(seed) or [])[:per_seed]):
            iid = item.get("id")
            if not iid or iid in skip:
                continue
            rec = found.get(iid)
            if rec is None:
                found[iid] = {"item": dict(item, _because=seed), "hits": 1, "best": pos, "rank": rank}
                order.append(iid)
            else:
                rec["hits"] += 1
                rec["best"] = min(rec["best"], pos)
    order.sort(key=lambda i: (-found[i]["hits"], found[i]["best"], found[i]["rank"]))
    return [found[i]["item"] for i in order[:limit]]


def genre_counts(snapshots):
    """Jak často se který žánr objevil v tom, co uživatel viděl (snímky titulů
    z `Store.item()` — `genres` v nich je od 5.2.7~beta20)."""
    counts = {}
    for snap in snapshots:
        for genre in (snap or {}).get("genres") or []:
            genre = str(genre).strip()
            if genre:
                counts[genre] = counts.get(genre, 0) + 1
    return counts


def pick_genre(counts, rnd=None):
    """Náhodný žánr vážený četností: kdo kouká hlavně komedie, dostane spíš komedii,
    ale ne pokaždé. Bez historie `None` — volající pak vezme obecný katalog."""
    ranked = sorted(counts, key=lambda g: (-counts[g], g))
    total = sum(counts[g] for g in ranked if counts[g] > 0)
    if total <= 0:
        return None
    hranice = (rnd or random.random)() * total
    for genre in ranked:
        if counts[genre] <= 0:
            continue
        hranice -= counts[genre]
        if hranice < 0:
            return genre
    return ranked[0]
