"""Hlídání: nové díly sledovaných seriálů a tituly, které čekají na stream.

Dvě různé věci pod jednou střechou:

- **sledované seriály** (`watchlist.json`, `{sid: položka}`) — nový díl se hlásí,
  až když má stream, ne jen když vyšel. `latest` je poslední odvysílaný díl podle
  metadat, `available` nejnovější díl se streamem a `new` díl, o kterém uživatel
  ještě neví (zhasne `mark_seen`);
- **hlídané tituly** (`wantlist.json` = co si uživatel přidal, `trakt_list.json` =
  výsledky kontroly, `trakt_flags.json` = příznak „kontrolovat dál“) — film nebo
  seriál, který zatím stream nemá, nebo má, ale ne v kvalitě či zvuku, jaký uživatel
  chce. Ozve se, když se stream objeví nebo když přibudou další.

Jména souborů zůstala z integrace pro Home Assistant, kde to celé vzniklo — data
tak po přechodu na jádro nepotřebují převod.

Kontrola stojí dotazy na všechny zdroje, a právě opakovanými dotazy si doplněk
už dvakrát přivodil blokaci od HellSpy (6.0.2, 6.0.4). Proto:
- seriál nejvýš po `SERIES_EVERY`, titul po `WANTED_EVERY`, a počítá se i kontrola
  z jiného zařízení (`checked_ts` jde synchronizací) — domácnost s HA a třemi Kodi
  se nezeptá čtyřikrát;
- na jeden seriál nejvýš `BUDGET` dotazů na streamy, v seznamu nejvýš `MAX_ITEMS`;
- díl bez data vydání není odvysílaný (`aired_episodes`).

Synchronizace (`sync.py`, okruh `watchlist`) nese deník `watchlog`
(`{"s:<sid>"|"w:<wid>": {"on", "ts"}}`) a k němu celé položky. U každé položky
vyhrává novější zápis, odebrání se přenáší jako `on: False`. Oznámení se
počítají zvlášť na každém zařízení (`pending_notices`), takže se ozve i to, které
nový díl samo nenašlo, jen ho dostalo synchronizací.
"""
import datetime
import logging
import re
import time

_LOGGER = logging.getLogger(__name__)

SERIES = "watchlist"
WANTED = "wantlist"
RESULTS = "trakt_list"
FLAGS = "trakt_flags"
LOG = "watchlog"
NOTIFIED = "watch_notified"   # jen místní — co už tohle zařízení oznámilo

SECTION = "watchlist"          # jméno sekce ve změnách synchronizace

SERIES_EVERY = 6 * 3600
WANTED_EVERY = 24 * 3600
# kontrola jiného zařízení (nebo časovač o chvilku dřív) se počítá, ať se dvě
# kola těsně po sobě neptají zdrojů dvakrát na totéž
SLACK = 15 * 60
BUDGET = 6          # dotazů na streamy na jeden seriál v jedné kontrole
MAX_ITEMS = 40      # kolik titulů jedna kontrola projde (každý = dotaz na všechny zdroje)
NOTICE_MAX_AGE = 3 * 86400   # starší nález už neoznamovat (nové zařízení ve skupině)
LOG_MAX = 1000

SERIES_FIELDS = ("title", "alt", "poster")
WANTED_FIELDS = ("type", "title", "year", "alt", "poster", "series", "query")


def _now():
    return int(time.time())


def _iso(ts=None):
    return datetime.datetime.fromtimestamp(ts or time.time()).astimezone().isoformat(timespec="seconds")


def _ts(rec):
    try:
        return int((rec or {}).get("ts") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _touch(store, key, on):
    """Zapíše změnu do deníku — bez toho by ji synchronizace neposlala."""
    with store.updating(LOG, {}) as log:
        log[key] = {"on": bool(on), "ts": _now()}
        if len(log) > LOG_MAX:
            for old in sorted(log, key=lambda k: _ts(log[k]))[:len(log) - LOG_MAX]:
                log.pop(old, None)


def _fold(text):
    import unicodedata
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


# --- seznamy -------------------------------------------------------------------

def series(store):
    return store.load(SERIES, {})


def wanted(store):
    return store.load(WANTED, {})


def results(store):
    return store.load(RESULTS, {})


def flags(store):
    return store.load(FLAGS, {})


def is_series_watched(store, sid):
    return sid in series(store)


def is_wanted(store, wid):
    return wid in wanted(store)


def is_flagged(store, wid):
    return wid in flags(store)


def watch_series(store, sid, info=None):
    """Seriál mezi sledované (nebo doplní název a plakát u už sledovaného)."""
    with store.updating(SERIES, {}) as data:
        item = data.get(sid) or {"id": sid, "added": _iso()}
        item.update({k: v for k, v in (info or {}).items() if k in SERIES_FIELDS and v})
        data[sid] = item
    _touch(store, "s:" + sid, True)
    return item


def unwatch_series(store, sid):
    with store.updating(SERIES, {}) as data:
        found = data.pop(sid, None) is not None
    _touch(store, "s:" + sid, False)
    return found


def want(store, wid, info=None):
    """Titul mezi hlídané. `wid` může být i `q:<název>` — titul, který zatím žádný
    zdroj nezná; hlídá se podle názvu, dokud se neobjeví."""
    with store.updating(WANTED, {}) as data:
        item = data.get(wid) or {"id": wid, "added": _iso()}
        item.update({k: v for k, v in (info or {}).items() if k in WANTED_FIELDS and v})
        item.setdefault("type", "movie")
        if item.get("query"):
            item.setdefault("title", item["query"])
        data[wid] = item
    _touch(store, "w:" + wid, True)
    return item


def query_id(query):
    return "q:" + (query or "").strip().lower()


def unwant(store, wid):
    """Přestat hlídat — i s uloženou kontrolou a příznakem, jinak by položka
    zůstala v seznamu (karta HA i Kodi kreslí výsledky kontroly)."""
    with store.updating(WANTED, {}) as data:
        found = data.pop(wid, None) is not None
    with store.updating(RESULTS, {}) as data:
        data.pop(wid, None)
    with store.updating(FLAGS, {}) as data:
        data.pop(wid, None)
    _touch(store, "w:" + wid, False)
    return found


def toggle_flag(store, wid):
    """„Kontrolovat dál“ — titul stream má, ale ne v kvalitě nebo zvuku, jaký uživatel
    chce, takže ho dál hlídat. Vrací nový stav."""
    with store.updating(FLAGS, {}) as data:
        if data.pop(wid, None) is None:
            data[wid] = True
        flagged = wid in data
    if wid in wanted(store):
        _touch(store, "w:" + wid, True)   # u titulu z Traktu jen místně
    return flagged


def mark_seen(store, sid=None):
    """Zhasne nový díl u jednoho seriálu (nebo u všech). Vrací, kolik jich svítí dál."""
    changed = []
    with store.updating(SERIES, {}) as data:
        for key, item in data.items():
            if sid in (None, key) and item.get("new"):
                item.pop("new", None)
                changed.append(key)
        left = sum(1 for i in data.values() if i.get("new"))
    for key in changed:
        _touch(store, "s:" + key, True)
    return left


def new_count(store):
    return sum(1 for i in series(store).values() if i.get("new"))


def due(rec, every, now=None):
    """Je čas znovu kontrolovat? Počítá se i kontrola z jiného zařízení."""
    try:
        last = int((rec or {}).get("checked_ts") or 0)
    except (TypeError, ValueError):
        last = 0
    return (now or _now()) - last >= every - SLACK


def anything_due(store, now=None):
    """Bez sítě: má smysl pouštět kontrolu? (služba v Kodi se podle toho rozhodne,
    jestli vůbec budit plugin). Tituly z Traktu tu nejsou — ty se kontrolují jednou
    denně spolu s ostatními."""
    now = now or _now()
    if any(due(i, SERIES_EVERY, now) for i in series(store).values()):
        return True
    done = results(store)
    return any(due(done.get(wid), WANTED_EVERY, now) for wid in wanted(store))


# --- kontrola seriálů ------------------------------------------------------------

def aired_episodes(episodes, today):
    """Odvysílané díly bez speciálů, setříděné podle sezóny a čísla.

    **Chybějící datum znamená neodvysíláno.** U běžícího seriálu nemá TMDB datum
    právě u posledních dílů sezóny, protože se teprve natáčejí; do 6.2.8 se takový díl
    počítal za odvysílaný a kontrola každých šest hodin hledala díly, které ještě
    neexistují (~180 HTTP dotazů denně na jeden seriál). Seriály, kterým TMDB data
    nedává vůbec, projdou celé — jinak by u nich kontrola nefungovala.
    """
    known = [e for e in episodes if e.get("season")]
    if any(e.get("released") for e in known):
        known = [e for e in known if e.get("released") and e["released"][:10] <= today]
    return sorted(known, key=lambda e: (e["season"], e["episode"]))


def skip_gap_candidates(aired, key):
    """Díly nejnovější sezóny novější než `key`, od nejnovějšího.

    Kontrola jde od posledního dostupného dílu dopředu a končí u prvního bez streamu.
    Když díl chybí uprostřed (Zrádci: S02E10–13 nikde, S03E01 ano), nová řada by se
    nikdy nenahlásila — proto se tyhle díly zkusí zvlášť.
    """
    if not aired:
        return []
    season = max(e["season"] for e in aired)
    newer = [e for e in aired if e["season"] == season and (e["season"], e["episode"]) > key]
    return sorted(newer, key=lambda e: e["episode"], reverse=True)


def _episode(ep, opts=()):
    return {"season": ep["season"], "episode": ep["episode"], "title": ep.get("title") or "",
            "id": ep["id"], "released": (ep.get("released") or "")[:10],
            # díl jen na trackeru — stažení chvíli trvá, karta to má říct
            "torrent": bool(opts) and all(o.get("kind") == "torrent" for o in opts)}


def check_one_series(engine, sid, item, today=None):
    """Co je u seriálu nového. Vrací `{"latest", "available", "found"}`, nebo None,
    když se nepodařilo načíst díly (pak se kontrola nezapíše a zkusí se příště)."""
    today = today or datetime.date.today().isoformat()
    try:
        episodes = engine.episodes(sid, None)
    except Exception as err:  # noqa: BLE001 – jeden seriál nesmí zastavit zbytek
        _LOGGER.debug("hlídání %s: %s", sid, err)
        return None
    aired = aired_episodes(episodes, today)
    if not aired:
        return {"latest": None, "available": item.get("available"), "found": None}
    last = aired[-1]
    latest = {"season": last["season"], "episode": last["episode"], "title": last.get("title") or "",
              "id": last.get("id"), "released": (last.get("released") or "")[:10]}
    known = item.get("available") or {}
    known_key = (known.get("season", 0), known.get("episode", 0))
    budget = [BUDGET]

    def options(ep):
        """Čím se dá díl pustit — streamy, a když žádné nejsou, torrenty."""
        if budget[0] <= 0:
            return []
        budget[0] -= 1
        try:
            return engine.streams_or_torrents("series", ep["id"], item.get("alt"), sid) or []
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("hlídání streamy %s: %s", ep["id"], err)
            return []

    found = None
    if not known:
        # poprvé: od nejnovější sezóny zpět, poslední díl sezóny — první sezóna se streamem vyhrává
        last_per_season = {}
        for ep in aired:
            last_per_season[ep["season"]] = ep
        for season in sorted(last_per_season, reverse=True)[:3]:
            opts = options(last_per_season[season])
            if opts:
                found = _episode(last_per_season[season], opts)
                known_key = (season, last_per_season[season]["episode"])
                break
    # pak po dílech dopředu — díly přibývají postupně, první chybějící ukončí hledání
    gap = None
    for ep in (e for e in aired if (e["season"], e["episode"]) > known_key):
        opts = options(ep)
        if not opts:
            gap = (ep["season"], ep["episode"])
            break
        found = _episode(ep, opts)
        known_key = (ep["season"], ep["episode"])
    if gap:
        for ep in skip_gap_candidates(aired, max(known_key, gap)):
            opts = options(ep)
            if opts:
                found = _episode(ep, opts)
                break
    return {"latest": latest, "available": found or item.get("available"), "found": found}


def _save_series(store, sid, before, result, now):
    """Zapíše výsledek do **čerstvě načtené** položky — mezitím mohl přijít zásah
    odjinud (zhasnutý nový díl z karty HA) a ten se nesmí přepsat stavem z doby,
    kdy kontrola začínala."""
    with store.updating(SERIES, {}) as data:
        item = data.get(sid)
        if item is None:
            return None          # mezitím přestal být sledovaný
        if result["latest"]:
            item["latest"] = result["latest"]
        found = result["found"]
        if found:
            first = "available" not in before and "checked" not in before
            item["available"] = found
            if not first:        # při zařazení jen zapamatovat, hlásit až příště
                item["new"] = dict(found, ts=now)
        item["checked"] = _iso(now)
        item["checked_ts"] = now
    _touch(store, "s:" + sid, True)
    return item


def check_series(engine, store, force=False, only=None, should_stop=None):
    """Projde sledované seriály, kterým vypršel interval (`force` = všechny).
    Vrací id seriálů, které se opravdu kontrolovaly."""
    now = _now()
    done = []
    for sid, item in list(series(store).items())[:MAX_ITEMS]:
        if only is not None and sid != only:
            continue
        if not force and not due(item, SERIES_EVERY, now):
            continue
        if should_stop and should_stop():
            break
        result = check_one_series(engine, sid, item)
        if result is None:
            continue
        _save_series(store, sid, item, result, _now())
        done.append(sid)
    return done


# --- kontrola hlídaných titulů ----------------------------------------------------

def check_one_wanted(engine, item, before=None):
    """Výsledek kontroly jednoho titulu (záznam do `trakt_list`)."""
    before = before or {}
    now = _now()
    target, alt = item["id"], item.get("alt")
    if str(target).startswith("q:"):
        # ruční položka — zkusit, jestli už titul některý zdroj zná
        query = item.get("query") or item.get("title") or ""
        try:
            found = engine.search(item.get("type", "movie"), query, 5) or []
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("hlídání hledání %s: %s", query, err)
            found = []
        # hledání vrací i nepodobné tituly („Duna 3" → „Vánoční prázdniny"),
        # takže se bere jen shoda, kde jsou všechna slova dotazu v názvu
        words = [w for w in re.split(r"[^\w]+", _fold(query)) if len(w) > 2]
        hit = next((f for f in found
                    if not words or all(w in _fold(f.get("title") or "") for w in words)), None)
        if hit is None:
            return {**item, "streams": 0, "best": "", "pending": True,
                    "checked": _iso(now), "checked_ts": now}
        target, alt = hit["id"], hit.get("alt")
        item = {**item, "title": hit.get("title") or item.get("title"), "year": hit.get("year") or item.get("year"),
                "poster": hit.get("poster") or item.get("poster"), "found_id": hit["id"], "alt": alt}
    try:
        streams = engine.streams_or_torrents(item.get("type", "movie"), target, alt, item.get("series")) or []
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("hlídání streamy %s: %s", item["id"], err)
        streams = []
    if not streams and before.get("streams"):
        # streamy nezmizí přes noc — spíš výpadek sítě (Kodi na mobilu na pozadí). Nulou by se
        # přepsal dobrý výsledek a příští kontrola by pak hlásila „už je k dispozici“ znovu.
        kept = {**before, "checked": _iso(now), "checked_ts": now}
        kept.pop("gained", None)
        return kept
    record = {**item, "type": item.get("type", "movie"), "streams": len(streams),
              "best": streams[0].get("label", "") if streams else "",
              # titul zatím jen na trackeru — pustit ho znamená napřed stáhnout
              "torrent": bool(streams) and all(s.get("kind") == "torrent" for s in streams),
              "checked": _iso(now), "checked_ts": now}
    record.pop("gained", None)
    prev = int(before.get("streams") or 0)
    # hlásí se i nárůst u titulu, který streamy už měl — nová kvalita, jazyk nebo
    # další zdroj navíc je taky dobrá zpráva, ne jen první nález
    if streams and before and len(streams) > prev:
        record["gained"] = {"streams": len(streams), "prev": prev, "ts": now}
    return record


def check_wanted(engine, store, extra=(), force=False, only=None, should_stop=None):
    """Projde hlídané tituly (a `extra` — seznam z Traktu, ten se nesynchronizuje).

    Bez `only` se `trakt_list` staví znovu celý, takže z něj zmizí, co už se nehlídá.
    Vrací id titulů, které se opravdu kontrolovaly.
    """
    own = wanted(store)
    items, seen = [], set()
    for item in list(own.values()) + list(extra or ()):
        if item.get("id") and item["id"] not in seen:
            seen.add(item["id"])
            items.append(item)
    known = results(store)
    now = _now()
    fresh, done = {}, []
    for item in items[:MAX_ITEMS]:
        wid = item["id"]
        before = known.get(wid) or {}
        skip = (only is not None and wid != only) or (not force and before and not due(before, WANTED_EVERY, now))
        if skip or (should_stop and should_stop()):
            if before:
                fresh[wid] = before
            continue
        fresh[wid] = check_one_wanted(engine, item, before)
        done.append(wid)
    with store.updating(RESULTS, {}) as data:
        if only is None:
            data.clear()
        else:
            fresh = {k: v for k, v in fresh.items() if k == only}
        data.update(fresh)
    for wid in done:
        if wid in own:
            _touch(store, "w:" + wid, True)
    return done


# --- oznámení ---------------------------------------------------------------------

def pending_notices(store, now=None):
    """Co tohle zařízení ještě neoznámilo — nový díl, nový stream u hlídaného titulu.

    Nález z jiného zařízení (přes synchronizaci) se oznámí taky, jen jednou. Starší
    než `NOTICE_MAX_AGE` (nebo bez času — data z doby před jádrem) se jen poznamená,
    aby se nové zařízení ve skupině neozvalo desítkou starých zpráv naráz.
    Na disk se sahá, jen když je co poznamenat — služba v Kodi se ptá každou minutu.
    """
    now = now or _now()
    notified = store.load(NOTIFIED, {})
    marks, out = {}, []
    for sid, item in series(store).items():
        new = item.get("new")
        if not isinstance(new, dict):
            continue
        key = "s:" + sid
        marks[key] = mark = str(new.get("id") or f"{new.get('season')}x{new.get('episode')}")
        if notified.get(key) != mark and now - _ts(new) <= NOTICE_MAX_AGE:
            out.append({"kind": "episode", "id": sid, "title": item.get("title") or sid,
                        "season": new.get("season"), "episode": new.get("episode"),
                        "episode_id": new.get("id"),
                        "episode_title": new.get("title") or "", "torrent": bool(new.get("torrent"))})
    for wid, rec in results(store).items():
        gained = rec.get("gained")
        if not isinstance(gained, dict):
            continue
        key = "w:" + wid
        marks[key] = mark = str(gained.get("ts"))
        if notified.get(key) != mark and now - _ts(gained) <= NOTICE_MAX_AGE:
            out.append({"kind": "more" if gained.get("prev") else "available", "id": wid,
                        "title": rec.get("title") or wid, "year": rec.get("year"),
                        "type": rec.get("type", "movie"),
                        "streams": gained.get("streams"), "prev": gained.get("prev")})
    if marks != notified:
        with store.updating(NOTIFIED, {}) as data:
            data.clear()
            data.update(marks)
    return out


# --- synchronizace ------------------------------------------------------------------

def _backfill(store):
    """Seriály a tituly uložené dřív, než deník existoval (HA před 8.3.0), v něm
    chybí — bez záznamu by je synchronizace nikdy neposlala, protože čte jen
    deník. Doplní se jednou s aktuálním časem, stejně jako `favlog` v `sync.py`."""
    log = store.reload(LOG, {})
    missing = ["s:" + k for k in store.reload(SERIES, {}) if "s:" + k not in log]
    missing += ["w:" + k for k in store.reload(WANTED, {}) if "w:" + k not in log]
    if not missing:
        return
    now = _now()
    with store.updating(LOG, {}) as data:
        for key in missing:
            data.setdefault(key, {"on": True, "ts": now})


def collect(store, since, seen):
    """Změny od `since` pro sekci `watchlist`. `seen(rec)` = kdy záznam dorazil
    (`sync._seen` — u středu čas příjmu)."""
    _backfill(store)
    log = store.reload(LOG, {})
    if not log:
        return {}
    ser, wan, res, flg = (store.reload(SERIES, {}), store.reload(WANTED, {}),
                          store.reload(RESULTS, {}), store.reload(FLAGS, {}))
    out = {}
    for key, rec in log.items():
        if not isinstance(rec, dict) or seen(rec) < since:
            continue
        kind, _, ident = key.partition(":")
        if not rec.get("on"):
            out[key] = {"on": False, "ts": _ts(rec)}
        elif kind == "s" and ident in ser:
            out[key] = {"on": True, "ts": _ts(rec), "item": ser[ident]}
        elif kind == "w" and ident in wan:
            entry = {"on": True, "ts": _ts(rec), "item": wan[ident], "flag": ident in flg}
            if ident in res:
                entry["result"] = res[ident]
            out[key] = entry
    return out


def apply(store, changes, stamp=0):
    """Slije cizí změny sekce `watchlist`. Vrací počet přijatých záznamů.
    `stamp` = čas příjmu, razí ho jen střed (HA)."""
    if not isinstance(changes, dict) or not changes:
        return 0
    applied = 0
    with store.updating(LOG, {}) as log, store.updating(SERIES, {}) as ser, \
            store.updating(WANTED, {}) as wan, store.updating(RESULTS, {}) as res, \
            store.updating(FLAGS, {}) as flg:
        for key, rec in changes.items():
            kind, _, ident = str(key).partition(":")
            if kind not in ("s", "w") or not ident or not isinstance(rec, dict):
                continue
            if _ts(rec) <= _ts(log.get(key)):
                continue
            on = bool(rec.get("on"))
            item = rec.get("item")
            if on and not isinstance(item, dict):
                continue
            entry = {"on": on, "ts": _ts(rec)}
            if stamp:
                entry["rts"] = stamp
            log[key] = entry
            applied += 1
            if kind == "s":
                if on:
                    ser[ident] = item
                else:
                    ser.pop(ident, None)
            elif on:
                wan[ident] = item
                if isinstance(rec.get("result"), dict):
                    res[ident] = rec["result"]
                if rec.get("flag"):
                    flg[ident] = True
                else:
                    flg.pop(ident, None)
            else:
                wan.pop(ident, None)
                res.pop(ident, None)
                flg.pop(ident, None)
    return applied
