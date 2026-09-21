"""Kontrola doplňku pro Kodi — bez Kodi, bez sítě a bez účtů.

    python3 -m unittest discover -s tests -v

Moduly `xbmc*` nahrazují podstrčené verze v `tests/stubs/`, které si jen
pamatují, co po nich plugin chtěl. Ověřuje se to, co je vlastní doplňku (jádro
má testy ve svém repu): přísný filtr názvů souborů, slučování streamů, router
a to, jak končí při chybě zdroje, soulad zahřívání cache s menu, a konzistence
souborů, které Kodi čte samo (strings.po, settings.xml, addon.xml).
"""
import os
import pathlib
import re
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE_LIB = ROOT.parent.parent / "nokturno-core" / "nokturno_core" / "lib"
sys.path.insert(0, str(ROOT / "tests" / "stubs"))
sys.path.insert(0, str(ROOT))

import xbmc, xbmcaddon, xbmcgui, xbmcplugin   # noqa: E402,E401

_PROFILE = tempfile.mkdtemp(prefix="nokturno-test-")
xbmcaddon.info.update(path=str(ROOT), profile=_PROFILE,
                      version=ET.parse(ROOT / "addon.xml").getroot().get("version"))
# Kodi předává handle a adresu pluginu v argv — default.py je čte hned při importu
sys.argv = ["plugin://plugin.video.nokturno/", "1", ""]

import default
import remote_setup
import transfer                                # noqa: E402
import service                                # noqa: E402
import kodi_marks                             # noqa: E402
import accounts as accounts_module         # noqa: E402
from luna_api import LunaError                # noqa: E402
from webshare_api import WebshareError        # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import build_repo                             # noqa: E402

HANDLE = 1
LANG_DIR = ROOT / "resources" / "language"


def tearDownModule():
    shutil.rmtree(_PROFILE, ignore_errors=True)


def reset_kodi():
    for m in (xbmc, xbmcaddon, xbmcgui, xbmcplugin):
        m.reset()


def params_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def wait_message_thread():
    """Zpráva z dashboardu se od 6.1.4~beta2 ukazuje ve vlastním vlákně — test na něj počká."""
    for t in threading.enumerate():
        if t.name == "nokturno-message":
            t.join(5)


def po_ids(lang):
    text = (LANG_DIR / f"resource.language.{lang}" / "strings.po").read_text(encoding="utf-8")
    return [int(m) for m in re.findall(r'^msgctxt "#(\d+)"', text, re.M)]


def ids_in_code():
    out = set()
    for name in ("default.py", "service.py"):
        out.update(int(m) for m in re.findall(r"\bLf?\((3\d{4})\b", (ROOT / name).read_text(encoding="utf-8")))
    return out


class TestKnihovnaJeKopieJadra(unittest.TestCase):
    """`resources/lib/` se needituje tady — kdyby se rozešla s jádrem, sync ji přepíše."""

    @unittest.skipUnless(CORE_LIB.is_dir(), "jádro není vedle doplňku")
    def test_lib_odpovida_jadru(self):
        # sync při rozesílání přepisuje relativní importy, prosté porovnání souborů nestačí —
        # rozhoduje sám rozesílací skript
        import subprocess
        sync = CORE_LIB.parent.parent / "tools" / "sync_core.py"
        out = subprocess.run([sys.executable, str(sync), "--check", "kodi"], capture_output=True, text=True)
        self.assertIn("ke změně: 0 souborů", out.stdout, f"spusť `python3 tools/sync_core.py kodi` v jádru\n{out.stdout}")


class TestRetezce(unittest.TestCase):
    def test_kazdy_pouzity_retezec_ma_preklad(self):
        used = ids_in_code()
        for lang in ("cs_cz", "en_gb", "sk_sk", "hu_hu"):
            chybi = sorted(used - set(po_ids(lang)))
            self.assertEqual(chybi, [], f"{lang}: chybí #{chybi}")

    def test_bez_duplicit_a_stejna_sada_ve_vsech_jazycich(self):
        sady = {}
        for lang in ("cs_cz", "en_gb", "sk_sk", "hu_hu"):
            ids = po_ids(lang)
            dup = sorted({i for i in ids if ids.count(i) > 1})
            self.assertEqual(dup, [], f"{lang}: duplicitní #{dup}")
            sady[lang] = set(ids)
        self.assertEqual(sady["cs_cz"], sady["en_gb"])
        self.assertEqual(sady["cs_cz"], sady["sk_sk"])
        self.assertEqual(sady["cs_cz"], sady["hu_hu"])

    def test_zadny_prazdny_preklad(self):
        # angličtina je zdrojový jazyk: text nese msgid a msgstr je podle zvyklostí Kodi prázdný
        for lang, pole in (("cs_cz", "msgstr"), ("sk_sk", "msgstr"), ("hu_hu", "msgstr"), ("en_gb", "msgid")):
            text = (LANG_DIR / f"resource.language.{lang}" / "strings.po").read_text(encoding="utf-8")
            bloky = re.findall(r'msgctxt "#(\d+)"\nmsgid "([^"]*)"\nmsgstr "([^"]*)"\n', text)
            prazdne = [sid for sid, msgid, msgstr in bloky if not (msgid if pole == "msgid" else msgstr)]
            self.assertEqual(prazdne, [], f"{lang}: prázdný {pole} u #{prazdne}")

    def test_Lf_bez_zastupneho_symbolu_hodnoty_pripoji(self):
        with mock.patch.object(default.ADDON, "getLocalizedString", return_value="Chyba: %s"):
            self.assertEqual(default.Lf(30000, "x"), "Chyba: x")
        with mock.patch.object(default.ADDON, "getLocalizedString", return_value="Chyba"):
            self.assertEqual(default.Lf(30000, "x", 2), "Chyba x 2")


class TestNastaveni(unittest.TestCase):
    def test_kazdy_cteny_klic_je_v_settings_xml(self):
        xml_ids = set(re.findall(r'<setting id="([a-z_0-9]+)"', (ROOT / "resources" / "settings.xml").read_text()))
        used = set()
        for name in ("default.py", "service.py"):
            used.update(re.findall(r'\b(?:setting|on|getSetting)\("([a-z_0-9]+)"', (ROOT / name).read_text()))
        # dav<slot>_* se skládají za běhu, ověří se zvlášť
        chybi = sorted(k for k in used if k not in xml_ids)
        self.assertEqual(chybi, [])
        for slot in range(1, default.STORAGE_SLOTS + 1):
            for suffix in ("url", "username", "password", "name"):
                self.assertIn(f"dav{slot}_{suffix}", xml_ids)


class TestAddonXml(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(ROOT / "addon.xml").getroot()
        self.version = self.root.get("version")

    def test_verze_ma_tvar_kodi(self):
        self.assertRegex(self.version, r"^\d+\.\d+\.\d+(~[a-z]+\d*)?$")
        self.assertEqual(build_repo.addon_version(str(ROOT)), self.version)

    def test_novinky_zacinaji_aktualni_verzi(self):
        lines = default.changelog_lines()
        self.assertTrue(lines)
        # u bety novinky nesou celé „4.0.0~beta1“ (build_repo.check bere i holé 4.0.0)
        self.assertIn(lines[0][0], (self.version, self.version.split("~")[0]))

    def test_kazdy_radek_novinek_se_da_precist(self):
        news = self.root.find(".//news").text.strip().splitlines()
        self.assertEqual(len(default.changelog_lines()), len(news),
                         "řádek <news> bez „verze – text“ by v Novinkách tiše zmizel")

    def test_changelog_od_verze(self):
        since = default.changelog_lines()[-1][0]   # nejstarší verze
        self.assertTrue(all(default._vkey(v) > default._vkey(since) for v, _t in default.changelog_lines(since)))
        self.assertEqual(default.changelog_lines(self.version), [])

    def test_ikona_a_fanart_existuji(self):
        for tag, rel in build_repo.addon_assets(str(ROOT)).items():
            self.assertTrue((ROOT / rel).is_file(), f"{tag}: {rel}")


class TestBuildRepo(unittest.TestCase):
    def test_razeni_verzi_jako_kodi(self):
        vk = build_repo.version_key
        self.assertLess(vk("3.1.9"), vk("3.1.10"))
        self.assertLess(vk("3.2.0~beta1"), vk("3.2.0"))
        self.assertLess(vk("3.2.0~beta1"), vk("3.2.0~beta2"))
        self.assertLess(vk("3.1.10"), vk("3.2.0~beta1"))
        self.assertEqual(sorted(["3.2.0", "3.1.10", "3.2.0~beta1", "3.1.9"], key=vk),
                         ["3.1.9", "3.1.10", "3.2.0~beta1", "3.2.0"])


class TestPomocneFunkce(unittest.TestCase):
    def test_split_episode_id(self):
        self.assertEqual(default.split_episode_id("tt0903747:1:2"), ("tt0903747", 1, 2))
        self.assertEqual(default.split_episode_id("sosac2_21849:3:12"), ("sosac2_21849", 3, 12))
        self.assertEqual(default.split_episode_id("tt0133093"), ("tt0133093", None, None))
        self.assertEqual(default.split_episode_id("tt1:x:2"), ("tt1:x:2", None, None))

    def test_display_name(self):
        """Bez roku — rok kreslí každý seznam zvlášť (Label2, info tag), v názvu byl dvakrát
        (2026-09-16, přání uživatele)."""
        self.assertEqual(default.display_name({"id": "tt1", "name": "Matrix", "releaseInfo": "1999-"}), "Matrix")
        self.assertEqual(default.display_name({"id": "tt1", "name": "Matrix"}), "Matrix")
        self.assertEqual(default.display_name({"id": "sosacd_5", "name": "Matrix CZ/EN (The Matrix)",
                                               "_title": "Matrix", "year": 1999}), "Matrix")

    def test_stary_snimek_s_rokem_v_nazvu_se_kresli_bez_roku(self):
        """Snímky uložené do bety 21 mají „Matrix (1999)“ — uřízne se jen rok, který sedí."""
        self.assertEqual(default.strip_year("Matrix (1999)", "1999"), "Matrix")
        self.assertEqual(default.strip_year("Matrix (1999)", ""), "Matrix")
        self.assertEqual(default.strip_year("Blade Runner 2049", "2017"), "Blade Runner 2049")
        self.assertEqual(default.strip_year("Film (2001)", "2020"), "Film (2001)")
        default.STORE.remember_item("tt_rok_v_nazvu", {"type": "movie", "id": "tt_rok_v_nazvu", "title": "Matrix (1999)",
                                                        "year": "1999", "plot": "x", "art": {"poster": "p"}, "rating": "8"})
        default.add_snapshot_item("tt_rok_v_nazvu", default.STORE.item("tt_rok_v_nazvu"))
        _h, _url, li, _f = xbmcplugin.items[-1]
        self.assertEqual(li.getLabel(), "Matrix")
        self.assertIn(("setTitle", ("Matrix",), {}), li.tag.calls)

    def test_runtime_minutes(self):
        self.assertEqual(default.runtime_minutes("2h42min"), 162)
        self.assertEqual(default.runtime_minutes("1h"), 60)
        self.assertEqual(default.runtime_minutes("42"), 42)
        self.assertEqual(default.runtime_minutes("45 min"), 45)
        self.assertEqual(default.runtime_minutes(""), 0)
        self.assertEqual(default.runtime_minutes(None), 0)

    def test_split_year(self):
        self.assertEqual(default.split_year("Pět švestek 2026"), ("Pět švestek", "2026"))
        self.assertEqual(default.split_year("2012"), ("2012", ""))
        self.assertEqual(default.split_year("Blade Runner 2049"), ("Blade Runner 2049", ""))
        self.assertEqual(default.split_year("  matrix  "), ("matrix", ""))

    def test_filter_year(self):
        merged = [({"name": "A", "year": 2026}, None), ({"name": "B", "releaseInfo": "1999-"}, None),
                  ({"name": "C"}, None)]
        self.assertEqual([m["name"] for m, _a in default.filter_year(merged, "2026")], ["A", "C"])
        self.assertEqual(default.filter_year(merged, ""), merged)

    def test_merge_results_spoji_stejny_titul(self):
        luna = [{"id": "tt1", "name": "Matrix", "year": 1999}, {"id": "tt2", "name": "Jiný film", "year": 2000}]
        sosac = [{"id": "sosacd_9", "_title": "Matrix", "_orig": "The Matrix", "year": 1999},
                 {"id": "sosacd_8", "_title": "Matrix", "year": 2020},   # rok se liší → jiný titul
                 {"id": "sosacd_7", "_title": "Jen v Sosáči", "year": 2001}]
        merged = default.merge_results(luna, sosac)
        self.assertEqual([(m["id"], alt) for m, alt in merged],
                         [("tt1", "sosacd_9"), ("tt2", None), ("sosacd_8", None), ("sosacd_7", None)])

    def test_build_url_vynecha_prazdne(self):
        url = default.build_url(action="play", id="tt1", series=None, alt="", skip=0)
        self.assertEqual(params_of(url), {"action": "play", "id": "tt1", "skip": "0"})
        self.assertTrue(url.startswith("plugin://plugin.video.nokturno/?"))

    def test_storage_first(self):
        streams = [{"source": "ws"}, {"source": "dav"}, {"source": "hs"}, {"source": "dav", "n": 2}]
        self.assertEqual([s["source"] for s in default.storage_first(streams)], ["dav", "dav", "ws", "hs"])


class TestFillInfo(unittest.TestCase):
    """`fill_info` skládá VideoInfoTag z meta — obsazení s fotkou, počet hlasů,
    trailer a věkový rating jsou z TMDB (viz nokturno_core/lib/tmdb_api.py:meta)."""

    def setUp(self):
        reset_kodi()

    def calls(self, li, name):
        return [args for n, args, _kw in li.tag.calls if n == name]

    def test_tmdb_obsazeni_s_fotkou_hlasy_trailer_mpaa_scenarista(self):
        li = xbmcgui.ListItem()
        meta = {
            "id": "tt1", "name": "Film", "year": 2026, "imdbRating": 8.1, "voteCount": 12345,
            "mpaa": "15", "trailerYoutubeId": "abc123",
            "cast": [{"name": "Herec", "character": "Role", "photo": "https://image.tmdb.org/t/p/w500/h.jpg"}],
            "director": ["Režisér"], "writer": ["Scénárista"],
        }
        default.fill_info(li, meta)
        self.assertEqual(self.calls(li, "setVotes"), [(12345,)])
        self.assertEqual(self.calls(li, "setMpaa"), [("15",)])
        self.assertEqual(self.calls(li, "setTrailer"),
                         [("plugin://plugin.video.youtube/play/?video_id=abc123",)])
        self.assertEqual(self.calls(li, "setWriters"), [(["Scénárista"],)])
        [(actors,)] = self.calls(li, "setCast")
        # stub Actor jen zaznamená pozici argumentů (jméno, role, pořadí, fotka) — viz stubs/xbmc.py
        self.assertEqual([(a.args[0], a.args[1], a.args[3]) for a in actors],
                         [("Herec", "Role", "https://image.tmdb.org/t/p/w500/h.jpg")])

    def test_cast_jen_jmena_bez_fotky_kdyz_neni_z_tmdb(self):
        """Luna/Cinemeta (přes obohacení Sosáče) dávají jen jména — starý tvar musí projít beze změny."""
        li = xbmcgui.ListItem()
        default.fill_info(li, {"id": "tt1", "name": "Film", "cast": ["Herec Jedna", "Herec Dva"]})
        [(actors,)] = self.calls(li, "setCast")
        self.assertEqual([(a.args[0], a.args[1], a.args[3]) for a in actors],
                         [("Herec Jedna", "", ""), ("Herec Dva", "", "")])

    def test_bez_tmdb_udaju_se_nic_nenastavi(self):
        li = xbmcgui.ListItem()
        default.fill_info(li, {"id": "tt1", "name": "Film"})
        for name in ("setVotes", "setMpaa", "setTrailer", "setWriters"):
            self.assertEqual(self.calls(li, name), [])


class TestSearchRun(unittest.TestCase):
    """Prázdný dotaz se dá na `search_run` poslat i mimo dialog `search_new` (crafted plugin://
    URL, widget) — Sosáč/WebShare/HellSpy na prázdné `q=` vrací nerozparsovatelnou odpověď."""

    def setUp(self):
        reset_kodi()

    def test_prazdny_dotaz_konci_bez_volani_zdroju(self):
        for query in ("", "   ", None):
            reset_kodi()
            with mock.patch.object(default, "list_ws_results") as ws:
                default.search_run({}, "any", query)
            ws.assert_not_called()
            self.assertEqual(xbmcplugin.ended, [{"handle": default.HANDLE, "succeeded": False,
                                                 "updateListing": False, "cacheToDisc": False}])
            self.assertEqual(xbmcplugin.items, [], f"query={query!r} nesmí nic vypsat")

    def test_neprazdny_dotaz_projde_dal(self):
        with mock.patch.object(default, "list_ws_results") as ws:
            default.search_run({}, "ws", "matrix")
        ws.assert_called_once()


class TestFiltrNazvuSouboru(unittest.TestCase):
    """Přísný filtr fulltextových zdrojů (WebShare, HellSpy) — chyby tady se
    projeví jako cizí film mezi streamy, nebo naopak prázdný seznam."""

    def relevant(self, meta, video=None, ctype="movie", strict=True):
        queries, relevant = default.title_queries({}, meta, video, ctype, strict=strict)
        return queries, relevant

    def test_idiom_se_stejnymi_slovy_neprojde(self):
        queries, relevant = self.relevant({"_title": "Pět švestek", "year": 2026})
        self.assertEqual(queries, ["Pět švestek 2026", "Pět švestek"])
        self.assertTrue(relevant("Pet.svestek.2026.1080p.CZ.mkv"))
        self.assertTrue(relevant("[WEB] Pět švestek (2026) FHD.mkv"))
        self.assertFalse(relevant("Seber.si.svych.pet.svestek.1983.mkv"))
        self.assertFalse(relevant("seber si svych pet svestek.mkv"))

    def test_uvolneny_filtr_pusti_slova_kdekoli(self):
        _q, relevant = self.relevant({"_title": "Pět švestek", "year": 2026}, strict=False)
        self.assertTrue(relevant("seber si svych pet svestek.mkv"))
        self.assertFalse(relevant("uplne jiny film.mkv"))

    def test_rok_musi_sedet_na_rok_presne(self):
        _q, relevant = self.relevant({"_title": "Pět švestek", "year": 2026})
        self.assertTrue(relevant("Pet svestek 2025.mkv"))     # ±1 rok kvůli různým datům premiéry
        self.assertFalse(relevant("Pet svestek 2020.mkv"))
        self.assertTrue(relevant("Pet svestek.mkv"))          # bez roku se nevylučuje

    def test_pokracovani_filmu_neprojde(self):
        _q, relevant = self.relevant({"_title": "Jak vycvičit draka", "year": 2010})
        self.assertTrue(relevant("Jak_vycvicit_draka_2010_CZ.mkv"))
        self.assertTrue(relevant("Jak.vycvicit.draka.CZ.5.1.mkv"))       # 5.1 je zvuk, ne díl
        self.assertFalse(relevant("Jak.vycvicit.draka.2.2014.mkv"))
        self.assertFalse(relevant("Jak vycvicit draka II (2014).mkv"))

    def test_dil_serialu_mezi_filmy_neprojde(self):
        _q, relevant = self.relevant({"_title": "Avatar", "year": 2009})
        self.assertFalse(relevant("Avatar.S01E03.mkv"))
        self.assertFalse(relevant("Avatar 1x03 CZ.mkv"))
        self.assertTrue(relevant("Avatar.2009.mkv"))

    def test_kratky_nazev_musi_stat_na_zacatku(self):
        queries, relevant = self.relevant({"_title": "To", "_orig": "It", "year": 2017})
        self.assertIn("It 2017", queries)
        self.assertTrue(relevant("To.2017.CZ.mkv"))
        self.assertTrue(relevant("CZ To 2017.mkv"))
        self.assertTrue(relevant("To - It (2017).mkv"))
        self.assertFalse(relevant("Nekdo.to.rad.horke.1959.mkv"))
        self.assertFalse(relevant("What.Happened.to.Monday.2017.mkv"))

    def test_kratke_slovo_na_zacatku_patri_k_nazvu(self):
        _q, relevant = self.relevant({"_title": "S čerty nejsou žerty", "year": 1984})
        self.assertTrue(relevant("S certy nejsou zerty 1984 CZ.avi"))
        self.assertTrue(relevant("S.certy.nejsou.zerty.mkv"))

    def test_epizoda_potrebuje_znacku_dilu(self):
        video = {"season": 1, "episode": 2}
        queries, relevant = self.relevant({"_title": "Breaking Bad", "year": 2008}, video=video, ctype="series")
        self.assertEqual(queries, ["Breaking Bad S01E02"])
        self.assertTrue(relevant("Breaking.Bad.S01E02.CZ.mkv"))
        self.assertTrue(relevant("Breaking Bad 1x02.mkv"))
        self.assertTrue(relevant("Breaking Bad 01x02.mkv"))
        self.assertFalse(relevant("Breaking.Bad.S01E03.mkv"))
        self.assertFalse(relevant("Breaking.Bad.S11E02.mkv"))
        self.assertFalse(relevant("Breaking.Bad.2008.mkv"))

    def test_original_je_dalsi_varianta(self):
        queries, relevant = self.relevant({"_title": "Vykoupení z věznice Shawshank", "_orig": "The Shawshank Redemption",
                                           "year": 1994})
        self.assertEqual(queries[2], "The Shawshank Redemption 1994")
        self.assertTrue(relevant("The.Shawshank.Redemption.1994.CZ.mkv"))
        self.assertTrue(relevant("Vykoupeni z veznice Shawshank.mkv"))
        self.assertFalse(relevant("Redemption.2013.mkv"))


class TestSlucovaniStreamu(unittest.TestCase):
    def test_stejny_soubor_z_luny_a_napřímo_jen_jednou(self):
        luna = {"source": "main", "label": "Matrix.1999.1080p.mkv", "detail": "4.2 GB | Zvuk: CZ 5.1", "url": "l"}
        ws = {"source": "ws", "label": "Matrix.1999.1080p.mkv", "detail": "4.2 GB", "url": "ws:1"}
        hs = {"source": "hs", "label": "Matrix.1999.1080p.mkv", "detail": "4.2 GB", "url": "hs:1"}
        jiny = {"source": "ws", "label": "Matrix.1999.720p.mkv", "detail": "1.5 GB", "url": "ws:2"}
        out = default.drop_duplicates([luna, ws, hs, jiny])
        self.assertEqual([s["url"] for s in out], ["l", "ws:2"])

    def test_dva_prime_zdroje_se_srovnaji_mezi_sebou(self):
        ws = {"source": "ws", "label": "Film.mkv", "detail": "2.0 GB", "url": "ws:1"}
        hs = {"source": "hs", "label": "Film.mkv", "detail": "2.0 GB", "url": "hs:1"}
        self.assertEqual([s["url"] for s in default.drop_duplicates([ws, hs])], ["ws:1"])

    def test_luna_bez_nazvu_se_paruje_podle_velikosti(self):
        luna = {"source": "main", "label": "(WS) Full HD", "detail": "2.4 GB | Zvuk: CZ 2.0", "url": "l"}
        search = {"source": "search", "label": "(WS) Full HD", "detail": "2.4 GB", "url": "s"}
        ws = {"source": "ws", "label": "Extraktori.2x06.1080p.mkv", "detail": "2.5 GB", "url": "ws:1"}
        daleko = {"source": "ws", "label": "Extraktori.2x06.jiny.1080p.mkv", "detail": "3.0 GB", "url": "ws:2"}
        out = default.drop_duplicates([luna, search, ws, daleko])
        self.assertEqual([s["url"] for s in out], ["l", "ws:2"])

    def test_bez_luny_se_nic_neztrati(self):
        streams = [{"source": "ws", "label": "a.mkv", "detail": "1 GB", "url": "1"},
                   {"source": "hs", "label": "b.mkv", "detail": "1 GB", "url": "2"}]
        self.assertEqual(len(default.drop_duplicates(streams)), 2)


class TestJadroVKodi(unittest.TestCase):
    """`default.py` stojí nad `Engine` z jádra — `KodiEngine` mu jen podstrkuje klienty
    podle přepínačů v nastavení a překládá předvolby z indexů na hodnoty jádra."""

    def setUp(self):
        reset_kodi()

    def test_volby_z_nastaveni(self):
        xbmcaddon.settings.update(pref_lang="1", sort_streams="2", hide_sd="true", max_bitrate_mbps="12,5",
                                  audio_probe="5", cross_search="false", ws_enabled="false", ws_username="u")
        opts = default.engine_options()
        self.assertEqual((opts["pref_lang"], opts["sort_streams"], opts["hide_sd"]), ("CZ", "size_desc", True))
        self.assertEqual((opts["audio_probe"], opts["cross_search"], opts["search_streams"]), ("5", False, True))
        self.assertEqual(opts["ws_username"], "", "vypnutý zdroj se jádru nehlásí ani při vyplněném účtu")
        engine = default.KodiEngine()
        self.assertAlmostEqual(engine._effective_max_gb({"runtime": "1h"}), 12.5e6 * 3600 / 8 / 2 ** 30, places=3)
        self.assertIs(engine.store, default.STORE, "jedno úložiště pro celý plugin")

    def test_klienty_podle_prepinacu(self):
        xbmcaddon.settings.update(ws_enabled="false", ws_username="u", ws_password="p",
                                  hs_enabled="true", st_enabled="true", st_email="a@b", st_password="x",
                                  fs_enabled="true", fs_username="u", fs_password="p",
                                  dav1_url="http://nas.lan/dav/", dav1_username="u", dav1_password="p")
        engine = default.KodiEngine()
        self.assertIsNone(engine.ws, "WebShare vypnutý přepínačem, i když je účet vyplněný")
        self.assertIsNotNone(engine.hs)
        self.assertIsNotNone(engine.st)
        self.assertIsNotNone(engine.fs)
        self.assertEqual(default.get_apis()["fs"].login_name, "u")
        self.assertEqual([s.slot for s in engine.storages], [1])
        self.assertIs(engine.hs, engine.hs, "klient se staví jednou za volání pluginu")
        apis = default.get_apis()
        self.assertIs(apis["engine"], default.engine_of(apis))
        self.assertIs(apis["dav"], apis["engine"].storages)
        holy = {}
        self.assertIsInstance(default.engine_of(holy), default.KodiEngine)
        self.assertIs(default.engine_of(holy), holy["engine"])

    def test_collect_streams_prevadi_vypadky_na_upozorneni(self):
        engine = default.KodiEngine()
        def raw_streams(ctype, item_id, alt=None, series_id=None, on_progress=None, failures=None, strict=True,
                        meta_video=None, on_source_done=None, on_audio_progress=None):
            failures.append(("Luna", ConnectionRefusedError("[Errno 111] Connection refused")))
            failures.append(("WebShare", WebshareError("login: Wrong password")))
            on_progress(3, 8)
            on_source_done("WebShare", 1)
            on_audio_progress(2, 4)
            self.assertEqual(meta_video, ({"name": "Film"}, None))
            self.assertFalse(strict)
            return [{"url": "ws:1", "label": "Film.mkv", "source": "ws", "_direct": True, "_loose": True}]
        engine.raw_streams = raw_streams
        bar = mock.Mock()
        progress = default.SearchProgress(bar, 30)
        errors = []
        out = default.collect_streams({"engine": engine}, "movie", "tt1", {"name": "Film"}, progress=progress,
                                      strict=False, errors=errors)
        self.assertEqual(out[0]["url"], "ws:1")
        self.assertEqual([type(e).__name__ for e in errors], ["SourceFailure", "SourceFailure"])
        self.assertEqual(default.skipped_notice(errors),
                         "Luna neodpovídá; WebShare: login: Wrong password — přeskočeno")
        bar.update.assert_called_with(int(3 / 8 * 100), "Streamy: 1 · Meta: 2/4")
        # chyba jádra v hlášce nese zdroj sama
        self.assertEqual(default.describe_error(default.NokturnoError("WebShare: soubor není")), "WebShare: soubor není")
        self.assertEqual(default.error_label(default.NokturnoError("Chybí odkaz na stream.")), "Nokturno")

    def test_search_progress_hlasi_nalezene_streamy(self):
        """Dokud přicházejí zdroje, jen součet „Streamy: N“ — ne výčet po zdrojích
        (2026-09-16, přání uživatele: na TV nečitelné)."""
        bar = mock.Mock()
        progress = default.SearchProgress(bar, 10)
        progress.tick()
        bar.update.assert_called_with(10)
        progress.source("Luna", 7)
        bar.update.assert_called_with(10, "Streamy: 7")
        progress.source("WebShare", 28)
        bar.update.assert_called_with(10, "Streamy: 35")

    def test_search_progress_metadata_pribudou_k_nalezenym_streamum(self):
        """Poslední fáze (čtení hlaviček) — „Streamy“ zůstávají, přibude
        „Meta: x/y“ (2026-09-16, přání uživatele)."""
        bar = mock.Mock()
        progress = default.SearchProgress(bar, 10)
        progress.source("WebShare", 12)
        progress.audio(0, 5)
        bar.update.assert_called_with(0, "Streamy: 12 · Meta: 0/5")
        progress.audio(3, 5)
        bar.update.assert_called_with(0, "Streamy: 12 · Meta: 3/5")
        progress.source("Vlastní úložiště", 2)   # zdroj dorazí až během ověřování
        bar.update.assert_called_with(0, "Streamy: 14 · Meta: 3/5")
        holy = default.SearchProgress(mock.Mock(), 10)   # bez zdrojů jen metadata
        holy.audio(1, 2)
        holy.bar.update.assert_called_with(0, "Meta: 1/2")

    def test_resolve_url_pres_jadro_a_token(self):
        engine = default.KodiEngine()
        engine.resolve = lambda url, prefer_external=False: "https://cdn/x.mkv"
        api = mock.Mock(token="tok")
        engine._clients["ws"] = api
        self.assertEqual(default.resolve_url({"engine": engine}, "ws:abc"), "https://cdn/x.mkv")
        self.assertEqual(xbmcgui.Window(10000).getProperty("nokturno.ws_token"), "tok")
        with self.assertRaises(default.NokturnoError):
            default.resolve_url({"engine": default.KodiEngine()}, "ws:abc")   # bez účtu
        self.assertIn(default.NokturnoError, default.Errors)

    def test_hledani_pres_jadro(self):
        """`_search_merge`/`search_source` jsou obálky nad `Engine.search_pairs` — holý katalog
        bez popisů pro volbu Filmy/Seriály, výpadky jako `SourceFailure` pro dialog."""
        engine = default.KodiEngine()
        volani = []
        def search_pairs(ctype, query, want_year=None, limit=None, on_tick=None, on_count=None,
                         failures=None, with_enrich=True, force=False):
            volani.append((ctype, query, want_year, with_enrich))
            failures.append(("Luna", ConnectionRefusedError("[Errno 111]")))
            if on_tick:
                on_tick()
            return [({"id": "sosacd_1", "_title": "Film", "year": 2020}, None)], False
        engine.search_pairs = search_pairs
        errors = []
        merged, mixed = default._search_merge({"engine": engine}, "movie", "Film", "2020", errors, tick=lambda: None)
        self.assertEqual((volani[-1], mixed, len(merged)), (("movie", "Film", 2020, False), False, 1))
        merged, _ = default.search_source({"engine": engine}, "movie", "Film", "", errors)
        self.assertEqual(volani[-1], ("movie", "Film", None, True))
        self.assertEqual([default.error_label(e) for e in errors], ["Luna", "Luna"])
        # rok z dotazu jako text pro odkazy, filtr a sloučení přes jádro
        self.assertEqual(default.split_year("Pět švestek 2026"), ("Pět švestek", "2026"))
        self.assertEqual(default.filter_year([({"name": "A", "year": 2026}, None), ({"name": "B"}, None)], "2026")[1][0]["name"], "B")

    def test_popisek_s_odhadnutou_kvalitou_z_jadra(self):
        s = {"url": "ws:1", "label": "Film.mkv", "detail": "9 GB", "source": "ws", "quality_rank": 4, "_estimated": True}
        self.assertIn("~4K", default.stream_lines(s)[0])
        s = {"url": "ws:1", "label": "Film.mkv", "detail": "9 GB", "source": "ws", "quality_rank": 4}
        self.assertNotIn("~", default.stream_lines(s)[0].split("[/B]")[0])


    def test_odhadnuty_jazyk_s_vlnovkou(self):
        # jádro dá odhad z názvu do `langs` s příznakem `_langs_from_name` — v popisku vlnovka
        s = {"url": "fs:1", "label": "Matrix.1999.2160p.CZ.mkv", "detail": "20 GB", "source": "fs"}
        default.parse_stream(s)
        s.update(langs=["CZ"], _langs_from_name=True)
        top = default.stream_lines(s)[0]
        self.assertIn("~CZ", top)
        # ověřený z detailu zdroje: bez vlnovky
        s.pop("_langs_from_name")
        self.assertNotIn("~CZ", default.stream_lines(s)[0])


    def test_dva_radky_vyberu_streamu(self):
        s = {"url": "ws:1", "label": "Matrix.1999.2160p.HDR.x265.CZ.EN.mkv", "detail": "20 GB", "source": "ws",
             "_tracks": [{"lang": "CZ", "channels": "5.1", "codec": "AC3"}, {"lang": "EN", "channels": "7.1", "codec": "TrueHD"}],
             "_media": {"width": 3840, "height": 1608}}
        default.parse_stream(s)   # jako v jádru: datový tok a titulky se doplní až po rozboru popisku
        s.update(bitrate=25.3, subs=["CZ"])
        with mock.patch.object(default, "on", return_value=True):
            top, bottom = default.stream_lines(s)
        self.assertIn("[B]CZ[/B]", top)
        self.assertIn("GB", top)
        self.assertNotIn("AC3", top)
        for kus in ("3840×1608", "HEVC", "HDR", "AC3 5.1 CZ", "TrueHD 7.1 EN", "25.3 Mb/s", "Tit.: CZ"):
            self.assertIn(kus, bottom)
        # odznak kvality místo nápisu: 4K s HDR, nápis kvality v horním řádku odpadne
        self.assertTrue(default.quality_icon(s).endswith(os.path.join("quality", "4k-hdr.png")))
        self.assertTrue(os.path.exists(default.quality_icon(s)))
        self.assertNotIn("4K", top)
        odhad = {"url": "ws:2", "label": "Film.mkv", "detail": "9 GB", "source": "ws", "quality_rank": 3, "_estimated": True}
        self.assertIn("~FHD", default.stream_lines(odhad)[0], "odhadnutá kvalita zůstává i nápisem")
        self.assertTrue(default.quality_icon(odhad).endswith("fhd.png"))

    def stream(self):
        s = {"url": "ws:1", "label": "Matrix.1999.2160p.x265.CZ.mkv", "detail": "20 GB", "source": "ws",
             "_tracks": [{"lang": "CZ", "channels": "5.1", "codec": "AC3"}], "_media": {"width": 3840, "height": 1608}}
        default.parse_stream(s)
        s.update(bitrate=25.3, subs=["CZ"])
        return s

    def test_poradi_polozek_streamu_z_nastaveni(self):
        s = self.stream()
        xbmcaddon.settings["stream_layout"] = "bitrate,size|langs"
        top, bottom = default.stream_lines(s)
        self.assertLess(top.index("Mb/s"), top.index("GB"))
        self.assertIn("[B]CZ[/B]", bottom)
        for skryte in ("AC3", "Tit.:", "3840"):
            self.assertNotIn(skryte, top + bottom, "co v pořadí není, se neukáže")
        # prázdný dolní řádek je v pořádku
        xbmcaddon.settings["stream_layout"] = "size"
        self.assertEqual(default.stream_layout(), (["size"], []))
        self.assertEqual(default.stream_lines(s)[1], "")

    def test_neplatne_poradi_a_stare_prepinace(self):
        top, bottom = default.STREAM_LAYOUT_DEFAULT.split("|")
        vychozi = (top.split(","), bottom.split(","))
        for spatne in ("size,nesmysl", "size|size", "a|b|c"):
            xbmcaddon.settings["stream_layout"] = spatne
            self.assertEqual(default.stream_layout(), vychozi, spatne)
        # instalace, která dřív vypnula velikost a soubor, je nevidí ani po přechodu
        xbmcaddon.settings.clear()
        xbmcaddon.settings.update(show_size="false", show_file="false")
        top, bottom = default.stream_layout()
        self.assertNotIn("size", top)
        self.assertNotIn("file", bottom)
        # vlastní pořadí staré přepínače ignoruje
        xbmcaddon.settings["stream_layout"] = "size|file"
        self.assertEqual(default.stream_layout(), (["size"], ["file"]))

    def test_vychozi_poradi_tlacitkem(self):
        xbmcaddon.settings["stream_layout"] = "size"
        default.stream_layout_reset()
        self.assertEqual(xbmcaddon.settings["stream_layout"], default.STREAM_LAYOUT_DEFAULT)


class TestTmdbHelperPlayer(unittest.TestCase):
    """„Přehrát“ v detailu filmu z TMDb Helperu (Arctic Fuse) → hledání streamů v Nokturnu."""

    SAMPLE = {"imdb": "tt1", "season": 1, "episode": 2}

    def setUp(self):
        reset_kodi()
        import json
        self.player = json.loads((ROOT / "resources" / "players" / "nokturno.json").read_text(encoding="utf-8"))

    def query(self, mode):
        url = self.player[mode].format_map(self.SAMPLE)
        self.assertTrue(url.startswith("plugin://plugin.video.nokturno/?"), url)
        return url.split("/?", 1)[1]

    def test_player_vede_na_akce_nokturna(self):
        self.assertEqual(self.player["plugin"], "plugin.video.nokturno")
        self.assertEqual(self.player["is_resolvable"], "true")
        # složkové režimy (search_*) TMDb Helper otevírá až po zavření detailu přes ActivateWindow —
        # na Office 2026-09-14 se streamy načetly, ale okno se neotevřelo; výběr je proto v pluginu
        self.assertEqual({k for k in self.player if k.startswith(("play_", "search_"))}, {"play_movie", "play_episode"})
        for mode in ("play_movie", "play_episode"):
            self.assertIn("imdb", self.player["assert"][mode], "bez IMDb id se player nemá nabízet")
        with mock.patch.object(default, "get_apis", return_value={}), mock.patch.object(default, "play") as play:
            default.router(self.query("play_movie"))
            self.assertEqual(play.call_args.args[1:4], ("movie", "tt1", None))
            self.assertEqual(play.call_args.kwargs["ask"], "1")
            default.router(self.query("play_episode"))
            self.assertEqual(play.call_args.args[1:4], ("series", "tt1:1:2", "tt1"), "díl = id seriálu:sezóna:díl")

    def test_tlacitko_nainstaluje_player_a_nastavi_vychozi(self):
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "players", "nokturno.json")
        with mock.patch.object(default, "TMDBH_PLAYER", dest):
            default.tmdbhelper_player()   # TMDb Helper chybí
            self.assertFalse(os.path.exists(dest))
            self.assertEqual(xbmcgui.notifications[-1][2], xbmcgui.NOTIFICATION_WARNING)
            xbmc.cond_visible.add("System.HasAddon(plugin.video.themoviedb.helper)")
            with mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True):
                default.tmdbhelper_player()
        self.assertEqual(pathlib.Path(dest).read_bytes(), (ROOT / "resources" / "players" / "nokturno.json").read_bytes())
        self.assertEqual(xbmcaddon.settings["default_player_movies"], "nokturno.json play_movie")
        self.assertEqual(xbmcaddon.settings["default_player_episodes"], "nokturno.json play_episode")
        shutil.rmtree(tmp, ignore_errors=True)

    def test_pruvodce_nabidne_player_jen_s_tmdb_helperem(self):
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "players", "nokturno.json")
        otazky = []

        def yesno(heading, *a, **k):
            otazky.append(heading)
            return heading == "Přehrát z detailu filmu"   # jen krok TMDb Helperu

        with mock.patch.object(default, "TMDBH_PLAYER", dest), mock.patch.object(xbmcgui.Dialog, "yesno", side_effect=yesno), \
             mock.patch.object(xbmcgui.Dialog, "yesnocustom", return_value=1):   # úvod: průvodce ovladačem
            default.setup_wizard(force=True)
            self.assertNotIn("Přehrát z detailu filmu", otazky, "bez TMDb Helperu se na player neptá")
            self.assertFalse(os.path.exists(dest))
            otazky.clear()
            xbmc.cond_visible.add("System.HasAddon(plugin.video.themoviedb.helper)")
            default.setup_wizard(force=True)
        self.assertIn("Přehrát z detailu filmu", otazky)
        self.assertEqual(pathlib.Path(dest).read_bytes(), (ROOT / "resources" / "players" / "nokturno.json").read_bytes())
        self.assertEqual(xbmcaddon.settings["default_player_movies"], "nokturno.json play_movie")
        self.assertEqual(xbmcaddon.settings["default_player_episodes"], "nokturno.json play_episode")
        shutil.rmtree(tmp, ignore_errors=True)

    def test_sluzba_drzi_nainstalovany_player_aktualni(self):
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "nokturno.json")
        with mock.patch.object(service, "TMDBH_PLAYER", dest), mock.patch.dict(xbmcaddon.info, path=str(ROOT)):
            service.refresh_tmdbhelper_player()
            self.assertFalse(os.path.exists(dest), "bez tlačítka se do TMDb Helperu nic nezapisuje")
            pathlib.Path(dest).write_text("{}", encoding="utf-8")
            service.refresh_tmdbhelper_player()
        self.assertEqual(pathlib.Path(dest).read_bytes(), (ROOT / "resources" / "players" / "nokturno.json").read_bytes())
        shutil.rmtree(tmp, ignore_errors=True)

    def test_ask_vzdy_dialog_s_predvybranym_zapamatovanym_streamem(self):
        streams = [{"url": "ws:1", "label": "Serial.S01E02.1080p.CZ.mkv", "detail": "2 GB", "source": "ws"},
                   {"url": "ws:2", "label": "Serial.S01E02.2160p.EN.mkv", "detail": "9 GB", "source": "ws"}]
        video = {"season": 1, "episode": 2, "title": "Díl"}
        for ask, asked in (("1", True), ("", False)):
            reset_kodi()
            default.STORE.set_stream_pref("tt77", default.stream_signature(dict(streams[1])))
            with mock.patch.object(default, "load_meta", return_value=({"name": "Seriál", "year": 2020}, video)), \
                 mock.patch.object(default, "collect_streams", return_value=streams), \
                 mock.patch.object(xbmcgui.Dialog, "select", return_value=-1) as select, \
                 mock.patch.object(default, "resolve_url", return_value="https://cdn/x.mkv"), \
                 mock.patch.object(default, "fill_info"), mock.patch.object(default, "mark_playing"), \
                 mock.patch.object(default, "upnext_notify"), mock.patch.object(default.STORE, "remember_item"):
                default.play({}, "series", "tt77:1:2", "tt77", ask=ask)
            self.assertEqual(select.called, asked, f"ask={ask!r}")
            if asked:
                rows = select.call_args[0][1]
                self.assertTrue(rows[select.call_args.kwargs["preselect"]].art["icon"].endswith("4k.png"))
            else:
                self.assertTrue(xbmcplugin.resolved[-1][1], "bez ask hraje zapamatovaný stream bez ptaní")

class TestPrehratelnePolozky(unittest.TestCase):
    """Film a díl nejsou nikdy složka. Přehrát v detailu (Arctic Fuse) volá PlayMedia na cestu
    položky — složka se streamy tam nic nepřehrála (Office 2026-09-14)."""

    def setUp(self):
        reset_kodi()

    def streams_menu(self, li):
        akce = dict(li.context).get("Vybrat stream", "")
        self.assertTrue(akce.startswith("RunPlugin(plugin://plugin.video.nokturno/?"), akce)
        self.assertTrue(any("toggle_watched" in a for _l, a in li.context), "menu se skládá jedním voláním")
        return params_of(akce[len("RunPlugin("):-1])

    def test_stahnout_v_menu_otevre_dialog_ke_stazeni(self):
        default.add_meta_item({"id": "tt1", "name": "Film", "year": 2020}, "movie", alt="sosacd_1")
        _h, _url, li, _f = xbmcplugin.items[-1]
        akce = dict(li.context).get("Stáhnout", "")
        p = params_of(akce[len("RunPlugin("):-1])
        self.assertEqual((p["action"], p["id"], p["alt"]), ("title_download", "tt1", "sosacd_1"))

    def test_film_mimo_vypis_prehratelny_s_dialogem_a_seznamem_v_menu(self):
        default.add_meta_item({"id": "tt1", "name": "Film", "year": 2020}, "movie", alt="sosacd_1")
        _h, url, li, is_folder = xbmcplugin.items[-1]
        self.assertFalse(is_folder)
        self.assertEqual(li.properties.get("IsPlayable"), "true")
        p = params_of(url)
        self.assertEqual((p["action"], p["id"], p.get("alt"), p.get("ask")), ("play", "tt1", "sosacd_1", "1"))
        menu = self.streams_menu(li)
        self.assertEqual((menu["action"], menu["type"], menu["id"], menu["alt"]), ("title", "movie", "tt1", "sosacd_1"))

    def test_ve_vypisu_nokturna_je_film_ne_slozka_a_seznam_v_menu(self):
        xbmc.cond_visible.add("Window.IsMedia")
        xbmc.info_labels["Container.PluginName"] = "plugin.video.nokturno"
        try:
            default.add_meta_item({"id": "tt1", "name": "Film", "year": 2020}, "movie", alt="sosacd_1")
        finally:
            xbmc.cond_visible.clear()
            xbmc.info_labels.clear()
        _h, url, li, is_folder = xbmcplugin.items[-1]
        self.assertFalse(is_folder, "ne-složka bez IsPlayable: klik = skript (handle −1), Přehrát = rozklíčování")
        self.assertNotEqual(li.properties.get("IsPlayable"), "true")
        self.assertEqual((params_of(url)["action"], params_of(url)["id"], params_of(url)["alt"]),
                         ("title", "tt1", "sosacd_1"))
        self.assertEqual(self.streams_menu(li)["action"], "title")

    def test_rozkoukany_film_ve_vypisu_nokturna_je_prehratelny_ne_slozka(self):
        """2026-09-16: i v režimu „Vybrat ze seznamu streamů“ (folder_mode) musí titul
        s uloženou referencí streamu (Pokračovat ve sledování) přehrát rovnou tu, ne
        zase nabídnout celé hledání — jinak je celá zkratka k ničemu (Office, nahlášeno
        uživatelem: Pokračovat vždycky ukázalo „Načítám streamy“ a trvalo to dlouho)."""
        xbmc.cond_visible.add("Window.IsMedia")
        xbmc.info_labels["Container.PluginName"] = "plugin.video.nokturno"
        default.STORE.set_resume("tt_resume_test", 452.8, 6106.8, stream_url="ws:abc", stream_subs="cz.srt")
        try:
            default.add_meta_item({"id": "tt_resume_test", "name": "Film", "year": 2020}, "movie", alt="sosacd_1")
        finally:
            xbmc.cond_visible.clear()
            xbmc.info_labels.clear()
        _h, url, li, is_folder = xbmcplugin.items[-1]
        self.assertFalse(is_folder, "rozkoukaný titul se má rovnou přehrát, ne otevřít složku")
        self.assertEqual(li.properties.get("IsPlayable"), "true")
        p = params_of(url)
        self.assertEqual((p["action"], p["id"], p.get("url"), p.get("subs")),
                         ("play", "tt_resume_test", "ws:abc", "cz.srt"))

    def test_klik_na_titul_ukaze_dialog_a_pusti_vybrany_stream(self):
        """Kodi ne-přehratelnou ne-složku spustí jako skript s handle −1 → `pick_title`:
        dialog na dva řádky, vybraný stream přes PlayMedia s jeho referencí."""
        streams = [{"url": "ws:1", "label": "Film.2020.1080p.CZ.mkv", "detail": "2 GB", "source": "ws",
                    "subtitles": ["ws:sub"]}]
        with mock.patch.object(default, "HANDLE", -1), mock.patch.object(default, "get_apis", return_value={}), \
             mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)), \
             mock.patch.object(default, "collect_streams", return_value=streams), \
             mock.patch.object(default, "mark_viewed"), \
             mock.patch.object(xbmcgui, "DialogProgress") as modal, \
             mock.patch.object(xbmcgui, "DialogProgressBG") as bg, \
             mock.patch.object(xbmcgui.Dialog, "select", return_value=0) as select:
            default.router("action=title&type=movie&id=tt1&alt=sosacd_1")
        self.assertTrue(select.call_args.kwargs["useDetails"])
        cmd = [b for b in xbmc.builtins if b.startswith("PlayMedia(")]
        self.assertEqual(len(cmd), 1, xbmc.builtins)
        p = params_of(cmd[0][len("PlayMedia("):-1])
        self.assertEqual((p["action"], p["id"], p["alt"], p["url"], p["subs"]), ("play", "tt1", "sosacd_1", "ws:1", "ws:sub"))
        self.assertNotIn("ask", p)
        bg.assert_called_once()
        modal.assert_not_called()

    def test_klik_na_titul_zruseny_dialog_nic_nepusti(self):
        streams = [{"url": "ws:1", "label": "Film.2020.1080p.CZ.mkv", "detail": "2 GB", "source": "ws"}]
        with mock.patch.object(default, "HANDLE", -1), mock.patch.object(default, "get_apis", return_value={}), \
             mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)), \
             mock.patch.object(default, "collect_streams", return_value=streams), \
             mock.patch.object(default, "mark_viewed"), \
             mock.patch.object(xbmcgui.Dialog, "select", return_value=-1):
            default.router("action=title&type=movie&id=tt1")
        self.assertEqual(xbmc.builtins, [])
        self.assertEqual(xbmcplugin.resolved, [])

    def test_prehrat_titulu_v_rezimu_seznamu_rozklicuje_s_dialogem(self):
        """Přehrát v detailu (Estuary přes playlist, Arctic Fuse přes TMDb Helper `PlayMedia`,
        tlačítko Play) tutéž položku rozklíčovává s normálním handle → `play(ask=1)` = dialog
        výběru streamu. Složka `action=streams` tohle neuměla (Office 2026-09-16, bety 13–18:
        Kodi ji při Přehrát spouštělo se stejnými argumenty jako při výpisu)."""
        with mock.patch.object(default, "HANDLE", 12), mock.patch.object(default, "get_apis", return_value={}), \
             mock.patch.object(default, "play") as play:
            default.router("action=title&type=movie&id=tt1&alt=sosacd_1")
        play.assert_called_once()
        self.assertEqual(play.call_args[0][1:3], ("movie", "tt1"))
        self.assertEqual((play.call_args[1]["alt"], play.call_args[1]["ask"]), ("sosacd_1", "1"))
        self.assertFalse([b for b in xbmc.builtins if b.startswith("Container.Update(")])

    def test_dil_serie_prehratelny_s_id_serialu(self):
        meta = {"id": "tt9", "name": "Seriál", "videos": [{"season": 1, "episode": 1, "title": "Pilot"}]}
        with mock.patch.object(default, "meta_for", return_value=meta):
            default.list_episodes({}, "tt9", 1)
        _h, url, li, is_folder = xbmcplugin.items[-1]
        self.assertFalse(xbmcplugin.ended[-1]["cacheToDisc"], "widget a výpis sdílí adresu, položky se liší podle okna")
        self.assertFalse(is_folder)
        p = params_of(url)
        self.assertEqual((p["action"], p["type"], p["id"], p["series"], p["ask"]), ("play", "series", "tt9:1:1", "tt9", "1"))
        menu = self.streams_menu(li)
        self.assertEqual((menu["action"], menu["id"], menu["series"]), ("title", "tt9:1:1", "tt9"))


class TestVyberStreamu(unittest.TestCase):
    """Výběr streamu je vždy dialog na dva řádky s filtrem (klik v Nokturnu, detail, widget, TMDb Helper)."""

    def setUp(self):
        reset_kodi()
        xbmc.cond_visible.clear()
        xbmc.info_labels.clear()
        default.STORE.set_last_stream_filter({})

    def tearDown(self):
        xbmc.cond_visible.clear()
        xbmc.info_labels.clear()

    def test_play_z_vypisu_nokturna_ukaze_dialog_bez_zruseneho_prehrani(self):
        """Kontextové menu „Vybrat stream a přehrát“ ve výpisu → dialog; žádné přesměrování na složku."""
        xbmc.cond_visible.add("Window.IsMedia")
        xbmc.info_labels["Container.PluginName"] = "plugin.video.nokturno"
        streams = [{"url": "ws:1", "label": "Film.2020.1080p.mkv", "detail": "2 GB", "source": "ws"}]
        with mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)), \
             mock.patch.object(default, "collect_streams", return_value=streams), \
             mock.patch.object(xbmcgui.Dialog, "select", return_value=-1) as select:
            default.play({}, "movie", "tt1", ask="1")
        self.assertTrue(select.called)
        self.assertFalse([b for b in xbmc.builtins if b.startswith("Container.Update(")])

    def test_play_s_primym_url_nastavi_resume_point(self):
        """2026-09-16: přehrání přímým odkazem (HA karta, widget, Up Next) resolvovalo
        ListItem bez rozkoukanosti — `apply_watched`/`setResumePoint` se volalo jen
        v seznamech, ne tady, takže titul z „Pokračovat ve sledování“ vždycky
        naskočil od začátku místo od uloženého místa."""
        default.STORE.set_resume("tt1", 543.2, 6000.0)
        with mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)), \
             mock.patch.object(default, "resolve_url", return_value="https://cdn/x.mkv"):
            default.play({}, "movie", "tt1", url="ws:1")
        self.assertEqual(len(xbmcplugin.resolved), 1)
        _handle, succeeded, li = xbmcplugin.resolved[0]
        self.assertTrue(succeeded)
        resume_calls = [c for c in li.tag.calls if c[0] == "setResumePoint"]
        self.assertEqual(resume_calls, [("setResumePoint", (543.2, 6000.0), {})])

    def test_play_s_neplatnym_ulozenym_streamem_spadne_na_nove_hledani(self):
        """2026-09-16: „Pokračovat ve sledování“ posílá uloženou referenci streamu
        (viz `add_playable`) přímo do `play()`, aby se přeskočilo hledání. Zdroj
        mezitím může přestat referenci znát (smazaný soubor, vypršelý účet) —
        `resolve_url` pak hodí `Errors`, ne že by to spadlo, musí to prohledat znovu."""
        streams = [{"url": "ws:2", "label": "Film.2020.1080p.mkv", "detail": "2 GB", "source": "ws"}]
        with mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)), \
             mock.patch.object(default, "collect_streams", return_value=streams), \
             mock.patch.object(default, "resolve_url", side_effect=[WebshareError("pryč"), "https://cdn/x.mkv"]):
            default.play({}, "movie", "tt1", url="ws:1")
        self.assertEqual(len(xbmcplugin.resolved), 1)
        _handle, succeeded, li = xbmcplugin.resolved[0]
        self.assertTrue(succeeded, "spadlo na plné hledání místo chyby")
        self.assertEqual(li.path, "https://cdn/x.mkv")

    def test_play_bez_url_nepouziva_modalni_dialog(self):
        """CLAUDE.md: „nikdy modální dialog v cestě, kterou může spustit widget nebo
        JSON-RPC" — `play()` bez `url` (widget, Up Next, TMDb Helper) na to v betě
        7–11 narazila: modální `DialogProgress` (zavedený pro Zpět = zrušit hledání
        v `list_streams()`) přehrání z TMDb Helperu rozbil, buď nic nešlo přehrát,
        nebo se nic nezobrazilo (2026-09-16, nahlásil uživatel). `play()` se tedy
        vrátila k `DialogProgressBG" — bez možnosti zrušit Zpět, ale bezpečně
        i mimo interaktivní procházení menu."""
        with mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)), \
             mock.patch.object(default, "collect_streams",
                               return_value=[{"url": "ws:1", "label": "Film.mkv", "source": "ws"}]), \
             mock.patch.object(default, "resolve_url", return_value="https://cdn/x.mkv"), \
             mock.patch.object(xbmcgui, "DialogProgress") as modal, \
             mock.patch.object(xbmcgui, "DialogProgressBG") as bg:
            default.play({}, "movie", "tt1")
        modal.assert_not_called()
        bg.assert_called_once()

    def test_dialog_nabidne_filtr_a_vrati_vybrany_stream(self):
        streams = [{"url": "ws:1", "label": "Film.2020.1080p.CZ.Dabing.mkv", "detail": "2 GB", "source": "ws"},
                   {"url": "ws:2", "label": "Film.2020.1080p.ENG.mkv", "detail": "2 GB", "source": "ws"}]
        volani = []

        def select(heading, rows, *a, **k):
            self.assertTrue(k.get("useDetails"))
            labels = [r.getLabel() for r in rows]
            volani.append(labels)
            if len(volani) == 1:
                return 0                                   # Filtr streamů
            return next(i for i, l in enumerate(labels) if "Zrušit filtr" not in l and "Filtr" not in l)

        def multiselect(heading, options, preselect=None):
            return [next(i for i, o in enumerate(options) if o.endswith("Zvuk: CZ"))]

        with mock.patch.object(xbmcgui.Dialog, "select", side_effect=select), \
             mock.patch.object(xbmcgui.Dialog, "multiselect", side_effect=multiselect):
            chosen = default.choose_stream(streams)
        self.assertEqual(chosen["url"], "ws:1")
        self.assertTrue(volani[0][0].startswith("[B]Filtr streamů[/B]"))
        self.assertIn("Zrušit filtr", volani[1])
        self.assertEqual(len(volani[1]), 3, "Filtr + Zrušit filtr + jediný CZ stream")
        self.assertEqual(default.STORE.last_stream_filter()["lang"], ["CZ"])


class TestSeznamStreamu(unittest.TestCase):
    def setUp(self):
        reset_kodi()

    def pick(self, streams, select, meta=({"id": "tt1", "name": "Film", "year": 2020}, None), apis=None, item="tt1",
             ctype="movie"):
        with mock.patch.object(default, "load_meta", return_value=meta), \
             mock.patch.object(default, "collect_streams", side_effect=streams) as collect, \
             mock.patch.object(default, "mark_viewed") as mv, \
             mock.patch.object(xbmcgui, "DialogProgress") as modal, \
             mock.patch.object(xbmcgui, "DialogProgressBG"), \
             mock.patch.object(xbmcgui.Dialog, "select", side_effect=select):
            default.pick_title(apis or {}, ctype, item)
        modal.assert_not_called()
        return collect, mv

    def test_stary_odkaz_na_vypis_streamu_nic_nevypise(self):
        """Výpis streamů jako složka zrušen (5.2.14~beta4) — z widgetu/JSON-RPC nic a žádný dialog."""
        with mock.patch.object(default, "HANDLE", 3), mock.patch.object(default, "get_apis", return_value={}), \
             mock.patch.object(default, "pick_title") as pick:
            default.router("action=streams&type=movie&id=tt1")
        pick.assert_not_called()
        self.assertFalse(xbmcplugin.ended[-1]["succeeded"])
        self.assertEqual(xbmc.builtins, [])

    def test_bez_vysledku_zkusi_rovnou_uvolneny_fulltext(self):
        s = {"url": "ws:1", "label": "Film.mkv", "source": "ws"}
        collect, _mv = self.pick([[], [s]], lambda *a, **k: len(a[1]) - 1, apis={"ws": object()})
        self.assertEqual([c[0][6] for c in collect.call_args_list], [True, False], "přísně, pak uvolněně")
        self.assertTrue(xbmc.builtins and xbmc.builtins[-1].startswith("PlayMedia("))

    def test_dialog_nabidne_uvolneny_fulltext(self):
        s = {"url": "ws:1", "label": "Film.mkv", "source": "ws"}
        labels = []

        def select(heading, rows, *a, **k):
            labels.append([r.getLabel() for r in rows])
            return len(rows) - 1 if len(labels) == 1 else -1   # poprvé poslední řádek = fulltext

        collect, _mv = self.pick([[s], [s]], select, apis={"ws": object()})
        self.assertTrue(labels[0][-1].startswith("Zkusit uvolněný fulltext"))
        self.assertFalse(any(l.startswith("Zkusit") for l in labels[1]), "uvolněný už fulltext nenabízí")
        self.assertEqual([c[0][6] for c in collect.call_args_list], [True, False])

    def test_stahnout_vybrany_stream_misto_prehrani(self):
        s = {"url": "ws:1", "label": "Film.2020.1080p.CZ.mkv", "source": "ws"}
        xbmcaddon.settings["download_dir"] = "/tmp/stahovani"
        with mock.patch.object(default, "download_stream") as dl, \
             mock.patch.object(default, "HANDLE", -1), mock.patch.object(default, "get_apis", return_value={}), \
             mock.patch.object(default, "load_meta", return_value=({"id": "tt1", "name": "Film", "year": 2020}, None)), \
             mock.patch.object(default, "collect_streams", return_value=[s]), \
             mock.patch.object(default, "mark_viewed"), \
             mock.patch.object(xbmcgui.Dialog, "select", side_effect=lambda h, rows, **k: len(rows) - 1):
            default.router("action=title_download&type=movie&id=tt1&alt=sosacd_1")
        dl.assert_called_once()
        self.assertEqual(dl.call_args[0][1:5], ("ws:1", "Film [Film.2020.1080p.CZ.mkv]", "tt1", "movie"))
        self.assertFalse([b for b in xbmc.builtins if b.startswith("PlayMedia(")], "nic se nepřehrává")

    def test_stahnout_bez_slozky_nic_nehleda(self):
        with mock.patch.object(default, "collect_streams") as collect, \
             mock.patch.object(default, "HANDLE", -1), mock.patch.object(default, "get_apis", return_value={}):
            default.router("action=title_download&type=movie&id=tt1")
        collect.assert_not_called()

    def test_mark_viewed_u_serialu_posila_nazev_serialu_ne_epizody(self):
        """2026-09-16: statistiky se serverem slučují podle normalizovaného názvu
        (`db.canonical_key`), takže skutečný (ne jen generický placeholder) název
        konkrétní epizody by rozštěpil sledovanost jednoho seriálu na tolik
        „titulů", kolik různých epizod se sledovalo. `mark_viewed` proto musí vždy
        dostat název seriálu, i když má epizoda vlastní netriviální název."""
        streams = [{"url": "ws:1", "label": "Lupin.S01E01.mkv", "detail": "1 GB", "source": "ws"}]
        meta = {"id": "tt123", "name": "Lupin", "_title": "Lupin", "year": 2021}
        video = {"title": "Skutečný název epizody, ne placeholder"}
        _c, mv = self.pick([streams], lambda *a, **k: -1, meta=(meta, video), item="tt123:1:2", ctype="series")
        mv.assert_called_once()
        self.assertEqual(mv.call_args[0][1], "Lupin")

    def test_title_polozky_sezony_je_cislo_sezony_ne_nazev_serialu(self):
        """Stejná chyba jako u streamů výš, tentokrát u výběru sezóny — `fill_info()`
        nastaví Title na název seriálu (správně pro epizody/film), skin ale u řádku
        sezón kreslí `ListItem.Title`, takže bez přepsání byly všechny položky
        pojmenované stejně jako seriál (2026-09-15, nahlásil uživatel screenshotem
        z Kodi: „Lupin Lupin Lupin“ místo „1. série“/„2. série“/„3. série“)."""
        meta = {"id": "tt123", "name": "Lupin", "_title": "Lupin",
                "videos": [{"season": s, "episode": 1} for s in (1, 2, 3)]}
        with mock.patch.object(default, "meta_for", return_value=meta):
            default.list_seasons({}, "tt123")
        rows = [li for _h, _u, li, _f in xbmcplugin.items]
        self.assertEqual(len(rows), 3)
        for li in rows:
            titles = [c[1][0] for c in li.tag.calls if c[0] == "setTitle"]
            self.assertEqual(titles[-1], li.getLabel())
            self.assertNotEqual(titles[-1], "Lupin")


class TestRouter(unittest.TestCase):
    def setUp(self):
        reset_kodi()
        default.STORE.save("wizard_done", True)

    def test_hlavni_menu_bez_zdroju_nabidne_nastaveni(self):
        with mock.patch.object(default, "get_apis", return_value={"luna": None, "sosac": None, "ws": None}):
            default.router("")
        self.assertEqual(len(xbmcplugin.ended), 1)
        self.assertEqual([params_of(u)["action"] for u in xbmcplugin.urls()], ["setup_wizard", "settings"],
                         "průvodce je i po přeskočení (wizard_done), bez zdroje nemá doplněk co ukázat")
        self.assertEqual([n[2] for n in xbmcgui.notifications], [xbmcgui.NOTIFICATION_WARNING])

    def test_akce_bez_parametru_neshodi_plugin(self):
        """Chybějící parametr (starý odkaz, ruční URL z HA) = upozornění a uzavřený handle,
        ne neošetřená výjimka — ta 2026-09-14 na Office nechala otevřený handle a Kodi se
        při souběhu s dalším dialogem samo ukončilo."""
        for url in ("action=history_remove", "action=toggle_watched", "action=download_remove&x=1"):
            reset_kodi()
            default.router(url)
            self.assertEqual(len(xbmcplugin.ended), 1, url)
            self.assertFalse(xbmcplugin.ended[-1]["succeeded"], f"{url}: succeeded musí být False")
            self.assertEqual(xbmcgui.notifications[-1][2], xbmcgui.NOTIFICATION_ERROR)
        reset_kodi()
        default.router("action=search&kind=any")   # HA a starší widgety posílají kind místo type
        self.assertEqual(len(xbmcplugin.ended), 1)
        self.assertTrue(xbmcplugin.ended[-1]["succeeded"])
        self.assertFalse(xbmcgui.notifications)

    def test_hlavni_menu_se_zdroji(self):
        default.router("")
        akce = [params_of(u).get("action") for u in xbmcplugin.urls()]
        for a in ("search", "browse", "favourites", "settings"):
            self.assertIn(a, akce)
        self.assertEqual(akce.count("browse"), 2)
        self.assertFalse(xbmcplugin.ended[-1]["cacheToDisc"], "menu se mění podle stavu, do cache nepatří")

    def test_nastaveni_konci_bez_slozky(self):
        default.router("?action=settings")
        self.assertEqual(xbmcaddon.opened_settings, ["plugin.video.nokturno"])
        self.assertFalse(xbmcplugin.ended[-1]["succeeded"])

    def test_chyba_zdroje_u_vypisu_ukonci_slozku_neuspechem(self):
        with mock.patch.object(default, "browse_menu", side_effect=LunaError("Luna neodpovídá")):
            default.router("?action=browse&type=movie")
        self.assertEqual(xbmcplugin.ended[-1]["succeeded"], False)
        self.assertEqual(xbmcgui.notifications[-1][2], xbmcgui.NOTIFICATION_ERROR)
        self.assertEqual(xbmcplugin.resolved, [])

    def test_chyba_zdroje_u_prehrani_vrati_neuspech_prehravaci(self):
        with mock.patch.object(default, "play", side_effect=WebshareError("bez účtu")):
            default.router("?action=play&type=movie&id=tt1&url=ws:abc")
        self.assertEqual(len(xbmcplugin.resolved), 1)
        self.assertFalse(xbmcplugin.resolved[0][1])
        self.assertEqual(xbmcplugin.ended, [], "po setResolvedUrl už endOfDirectory nepatří")

    def test_chyba_u_akce_bez_vypisu_nic_neukoncuje(self):
        with mock.patch.object(default, "download_stream", side_effect=WebshareError("x")):
            default.router("?action=download&url=ws:1&id=tt1")
        self.assertEqual(xbmcplugin.ended, [])
        self.assertEqual(xbmcplugin.resolved, [])
        self.assertEqual(xbmcgui.notifications[-1][2], xbmcgui.NOTIFICATION_ERROR)

    def test_vymazani_cache_je_akce_ne_slozka(self):
        default.STORE.save("cache_version", "x")
        with mock.patch.object(default.STORE, "clear_cache") as clear:
            default.router("?action=clear_cache")
        clear.assert_called_once()
        self.assertFalse(xbmcplugin.ended[-1]["succeeded"])


class TestHlaseniOPadech(unittest.TestCase):
    """Neočekávaná výjimka v routeru → fronta `crash/` v profilu; výpadek zdroje ne.
    Odesílá služba (`CrashSender`), a jen se zapnutými statistikami i přepínačem."""

    def setUp(self):
        reset_kodi()
        default.STORE.save("wizard_done", True)
        xbmcaddon.settings.update(stats_enabled="true")
        self.reporter = default.CrashReporter(default.PROFILE)
        shutil.rmtree(self.reporter.queue_dir, ignore_errors=True)
        try:
            os.remove(self.reporter.state_path)
        except OSError:
            pass
        xbmcgui.Window(10000).clearProperty(default.CRASH_PROP)

    def hlaseni(self):
        out = []
        for path in self.reporter.pending():
            with open(path, encoding="utf-8") as f:
                out.append(json.loads(f.read()))
        return out

    def test_pad_v_kodu_se_zaradi(self):
        with mock.patch.object(default, "browse_menu", side_effect=ZeroDivisionError("token=tajne")):
            default.router("?action=browse&type=movie")
        self.assertFalse(xbmcplugin.ended[-1]["succeeded"], "handle se zavře dřív než hlášení")
        [r] = self.hlaseni()
        self.assertEqual((r["product"], r["action"], r["type"]), ("kodi", "browse", "ZeroDivisionError"))
        self.assertNotIn("tajne", r["message"] + r["traceback"])
        self.assertEqual(xbmcgui.Window(10000).getProperty(default.CRASH_PROP), "1")

    def test_stejny_pad_podruhe_uz_ne(self):
        for _ in range(3):
            with mock.patch.object(default, "browse_menu", side_effect=ZeroDivisionError("x")):
                default.router("?action=browse&type=movie")
        self.assertEqual(len(self.hlaseni()), 1)

    def test_vypadek_zdroje_se_nehlasi(self):
        with mock.patch.object(default, "browse_menu", side_effect=LunaError("Luna neodpovídá")):
            default.router("?action=browse&type=movie")
        self.assertEqual(self.hlaseni(), [])

    def test_vypnute_statistiky_nebo_prepinac(self):
        for nastaveni in ({"stats_enabled": "false"}, {"crash_reports": "false"}):
            xbmcaddon.settings.update({"stats_enabled": "true", "crash_reports": "true", **nastaveni})
            with mock.patch.object(default, "browse_menu", side_effect=ZeroDivisionError("x")):
                default.router("?action=browse&type=movie")
            self.assertEqual(self.hlaseni(), [], nastaveni)

    def test_sluzba_odesle_frontu_po_signalu(self):
        with mock.patch.object(default, "browse_menu", side_effect=ZeroDivisionError("x")):
            default.router("?action=browse&type=movie")
        sender = service.CrashSender(self.reporter)
        with mock.patch.object(self.reporter, "flush", return_value=(1, 0)) as flush, \
                mock.patch.object(service.threading, "Thread") as vlakno:
            vlakno.side_effect = lambda target, **kw: mock.Mock(start=target)
            sender.tick()
        flush.assert_called_once()
        self.assertEqual(flush.call_args[0][0], service.CRASH_URL)
        self.assertEqual(xbmcgui.Window(10000).getProperty(service.CRASH_PROP), "")

    def test_sluzba_bez_souhlasu_frontu_smaze(self):
        with mock.patch.object(default, "browse_menu", side_effect=ZeroDivisionError("x")):
            default.router("?action=browse&type=movie")
        xbmcaddon.settings["crash_reports"] = "false"
        sender = service.CrashSender(self.reporter)
        with mock.patch.object(self.reporter, "flush") as flush:
            sender.tick()
        flush.assert_not_called()
        self.assertEqual(self.hlaseni(), [])

    def test_pad_vlakna_sluzby(self):
        with mock.patch.object(service, "PROFILE", default.PROFILE):
            service.capture_service_crash("service:nokturno-sync", KeyError("since"))
        [r] = self.hlaseni()
        self.assertEqual((r["action"], r["type"]), ("service:nokturno-sync", "KeyError"))


class TestOpravyZAuditu(unittest.TestCase):
    def setUp(self):
        reset_kodi()
        default.STORE.save("wizard_done", False)

    def test_pruvodce_se_na_ciste_instalaci_nabidne_v_menu(self):
        """Přepínače sosac/luna/hs mají výchozí true — podle nich průvodce nikdy nebyl potřeba."""
        self.assertFalse(default.accounts_set())
        default.setup_wizard()   # bez účtů nic neuloží (dialog v testu odpoví „Přeskočit“ → uloží až na konci)
        default.STORE.save("wizard_done", False)
        default.router("")
        akce = [params_of(u).get("action") for u in xbmcplugin.urls()]
        self.assertEqual(akce[0], "setup_wizard", "průvodce je první položka, ne modální dialog v kořeni")
        xbmcaddon.settings["ws_username"] = "ja"
        self.assertTrue(default.accounts_set())
        xbmcplugin.reset()
        default.router("")
        self.assertNotIn("setup_wizard", [params_of(u).get("action") for u in xbmcplugin.urls()])
        default.STORE.save("wizard_done", False)
        default.setup_wizard()
        self.assertTrue(default.STORE.load("wizard_done", False), "s účtem se průvodce považuje za hotový")

    def test_pokracovat_umi_hellspy_a_uloziste(self):
        odebrat = [("Odebrat z Pokračovat", "RunPlugin(plugin://x/?action=remove_progress)")]
        default.add_snapshot_item("hs:123:abc", {"type": "hs", "id": "hs:123:abc", "title": "Film.mkv", "art": {}}, odebrat)
        self.assertEqual(params_of(xbmcplugin.urls()[0]), {"action": "play_hs", "id": "123", "hash": "abc", "name": "Film.mkv"})
        xbmcaddon.settings.update(dav1_url="http://nas.lan/dav/", dav1_username="u", dav1_password="p", dav1_name="NAS")
        default.add_snapshot_item("dav:1:Filmy/a.mkv", {"type": "dav", "id": "dav:1:Filmy/a.mkv", "title": "a.mkv", "art": {}}, odebrat)
        self.assertEqual(params_of(xbmcplugin.urls()[1]), {"action": "play_dav", "slot": "1", "path": "Filmy/a.mkv", "name": "a.mkv"})
        for _h, _u, li, _f in xbmcplugin.items:
            self.assertIn(odebrat[0], li.context, "kontext z výpisu (Odebrat z Pokračovat) musí projít i u HellSpy a úložiště")
        # úložiště, které už v nastavení není, se tiše vynechá; rozbitý klíč taky
        default.add_snapshot_item("dav:3:x.mkv", {"type": "dav", "id": "dav:3:x.mkv", "title": "x", "art": {}})
        default.add_snapshot_item("dav:zle", {"type": "dav", "id": "dav:zle", "title": "x", "art": {}})
        self.assertEqual(len(xbmcplugin.items), 2)
        self.assertIsNone(default.recover_snapshot({}, "dav:1:Filmy/a.mkv"), "snímek úložiště se z meta nedohledává")

    def test_cizi_vyjimka_v_routeru_uklidi_handle(self):
        with mock.patch.object(default, "play", side_effect=KeyError("id")):
            default.router("?action=play&type=movie&id=tt1")
        self.assertEqual([r[1] for r in xbmcplugin.resolved], [False])
        self.assertIn("KeyError", xbmcgui.notifications[-1][1])
        xbmcplugin.reset()
        with mock.patch.object(default, "browse_menu", side_effect=RuntimeError("kodi")):
            default.router("?action=browse&type=movie")
        self.assertFalse(xbmcplugin.ended[-1]["succeeded"])

    def test_addon_xml_vyzaduje_kodi_20(self):
        root = ET.parse(ROOT / "addon.xml").getroot()
        ver = root.find(".//import[@addon='xbmc.python']").get("version")
        self.assertGreaterEqual(tuple(int(x) for x in ver.split(".")), (3, 0, 1), "setMediaType a VideoStreamDetail jsou Kodi 20+")


class TestHubenySnimek(unittest.TestCase):
    """Titul přidaný do Mého seznamu/Pokračovat dřív, než pro něj doběhlo obohacení
    (TMDB, přepočet na pozadí), dostal snímek bez popisu i fotky — a bez opravy tak
    zůstal navždy, i po doplnění dat (2026-09-16, „Ztracená žena“ v Mém seznamu)."""

    def test_snimek_nese_hodnoceni_zanry_stopaz_a_vypis_je_kresli(self):
        """Můj seznam/Pokračovat kreslí ze snímku, ne z API — bez těchhle polí měl titul jen
        název a popis, žádné hvězdičky, žánr, stopáž ani věk (2026-09-16, nahlásil uživatel:
        „u všech seznamů musí být hodnocení a rok")."""
        meta = {"id": "tt_snap_full", "name": "Film", "year": 2020, "imdbRating": "7.4", "voteCount": 1200,
                "genres": ["Drama"], "runtime": "118 min", "mpaa": "15+", "description": "Popis",
                "poster": "p.jpg", "background": "b.jpg"}
        snap = default.snapshot(meta, "movie")
        self.assertEqual((snap["rating"], snap["votes"], snap["genres"], snap["mpaa"]), ("7.4", 1200, ["Drama"], "15+"))
        self.assertFalse(default.thin_snapshot(snap))
        li = xbmcgui.ListItem(label="x")
        default.fill_info_snapshot(li, snap)
        calls = {c[0]: c[1] for c in li.tag.calls}
        self.assertEqual(calls["setRating"], (7.4,))
        self.assertEqual(calls["setVotes"], (1200,))
        self.assertEqual(calls["setGenres"], (["Drama"],))
        self.assertEqual(calls["setMpaa"], ("15+",))
        self.assertEqual(calls["setDuration"], (118 * 60,))
        self.assertEqual(calls["setYear"], (2020,))
        self.assertTrue(calls["setPlot"][0].startswith("[B]"), calls["setPlot"])
        self.assertEqual(li.properties.get("RatingPercent"), "74 %")

    def test_stary_snimek_bez_hodnoceni_se_jednou_dohleda(self):
        """Snímky z verzí před hodnocením (bez klíče `rating`) se berou jako hubené →
        `recover_snapshot` je jednou dohledá a uloží; soubory WebShare/HellSpy/úložiště
        meta nemají, ty se nedohledávají."""
        self.assertTrue(default.thin_snapshot({"type": "movie", "id": "tt1", "title": "F", "plot": "x", "art": {"poster": "p"}}))
        self.assertFalse(default.thin_snapshot({"type": "ws", "id": "ws:1", "title": "soubor.mkv", "art": {}}))
        self.assertFalse(default.thin_snapshot({"type": "movie", "id": "tt1", "plot": "x", "art": {"poster": "p"}, "rating": ""}))

    def setUp(self):
        reset_kodi()

    def test_thin_snapshot_pozna_prazdny_popis_i_fotku(self):
        self.assertTrue(default.thin_snapshot({"title": "X", "plot": "", "art": {}, "rating": ""}))
        self.assertFalse(default.thin_snapshot({"title": "X", "plot": "Popis", "art": {}, "rating": ""}))
        self.assertFalse(default.thin_snapshot({"title": "X", "plot": "", "art": {"poster": "http://p"}, "rating": ""}))
        self.assertFalse(default.thin_snapshot(None))

    def test_toggle_fav_hubeny_snimek_se_pri_pridani_obnovi(self):
        default.STORE.remember_item("tt_thin_add", {"type": "movie", "id": "tt_thin_add", "title": "tt_thin_add",
                                                     "plot": "", "art": {}})
        engine = default.KodiEngine()
        engine.meta = lambda ctype, item_id, series_id=None: (
            {"id": item_id, "name": "Film", "year": 2026, "description": "Popis", "poster": "http://p"}, None)
        default.toggle_fav({"engine": engine}, "tt_thin_add", "movie")
        snap = default.STORE.item("tt_thin_add")
        self.assertEqual(snap["plot"], "Popis")
        self.assertEqual(snap["art"].get("poster"), "http://p")

    def test_toggle_fav_selhani_meta_necha_puvodni_hubeny_snimek(self):
        """Když se refresh nepovede, nesmí se hubený snímek nahradit ještě chudším
        (holý klíč místo skutečného titulu)."""
        default.STORE.remember_item("tt_thin_fail", {"type": "movie", "id": "tt_thin_fail", "title": "Skutečný název",
                                                      "plot": "", "art": {}})
        engine = default.KodiEngine()
        engine.meta = mock.Mock(side_effect=default.LunaError("výpadek"))
        default.toggle_fav({"engine": engine}, "tt_thin_fail", "movie")
        snap = default.STORE.item("tt_thin_fail")
        self.assertEqual(snap["title"], "Skutečný název")

    def test_list_favourites_hubeny_snimek_se_dohleda_i_zpetne(self):
        default.STORE.toggle_favourite("tt_thin_list", {"type": "movie", "id": "tt_thin_list", "title": "tt_thin_list",
                                                         "plot": "", "art": {}})
        engine = default.KodiEngine()
        engine.meta = lambda ctype, item_id, series_id=None: (
            {"id": item_id, "name": "Film", "year": 2026, "description": "Popis", "poster": "http://p"}, None)
        with mock.patch.object(default, "get_apis", return_value={"engine": engine}):
            xbmcplugin.reset()
            default.list_favourites()
        snap = default.STORE.item("tt_thin_list")
        self.assertEqual(snap["plot"], "Popis", "hubený snímek se má opravit i zpětně, ne jen při dalším přidání")
        li = xbmcplugin.items[0][2]
        plots = [args[0] for name, args, _kw in li.getVideoInfoTag().calls if name == "setPlot"]
        self.assertEqual(plots, ["Popis"], "výpis ukazuje už opravená data, ne stará hubená")


class TestMenuAZahrivani(unittest.TestCase):
    """`service.warm_urls()` musí zahřívat přesně ty výpisy, které `browse_menu()` nabízí —
    jinak se zahřívá něco jiného a první otevření trvá desítky sekund (3.1.8)."""

    def setUp(self):
        reset_kodi()

    def browse(self, apis):
        out = []
        for ctype in ("movie", "series"):
            xbmcplugin.reset()
            default.browse_menu(apis, ctype)
            out.extend(params_of(u) for u in xbmcplugin.urls())
        # „Populární na TMDB“/„Nejlépe hodnocené“ jdou přes action=genres (2026-09-15) —
        # stejný src/catalog/type jako dřív, jen se nejdřív nabídne výběr žánru
        # („Vše“ = beze změny výsledná adresa), zahřívání pořád warmuje přímo katalog
        return {(p["src"], p["catalog"], p["type"], p.get("genre")) for p in out if p["action"] in ("catalog", "genres")}

    def browse_lang(self, apis):
        out = []
        for ctype in ("movie", "series"):
            xbmcplugin.reset()
            default.browse_menu(apis, ctype)
            out.extend(params_of(u) for u in xbmcplugin.urls())
        return {(p["want"], p["type"]) for p in out if p["action"] == "lang_catalog_menu"}

    def warm(self):
        return {(p["src"], p["catalog"], p["type"], p.get("genre")) for p in map(params_of, service.warm_urls())}

    def test_s_klicem_tmdb(self):
        xbmcaddon.settings["tmdb_api_key"] = "abc"
        menu = self.browse({"tmdb": object(), "sosac_db": object(), "luna": None, "cinemeta": None})
        self.assertTrue(self.warm() <= menu, self.warm() - menu)

    def test_zahrivani_bere_aktualni_nastaveni(self):
        """Modulový ADDON služby nevidí změny — po zadání klíče TMDB se dál zahřívala Luna."""
        xbmcaddon.settings["token"] = "t"
        self.assertEqual({p["src"] for p in map(params_of, service.warm_urls())}, {"luna", "trend"})
        xbmcaddon.settings["tmdb_api_key"] = "abc"
        self.assertEqual({p["src"] for p in map(params_of, service.warm_urls())}, {"tmdb", "trend"})

    def test_s_lunou(self):
        xbmcaddon.settings["token"] = "t"
        menu = self.browse({"tmdb": None, "sosac_db": object(), "luna": object(), "cinemeta": None})
        self.assertTrue(self.warm() <= menu, self.warm() - menu)

    def test_zapnuta_luna_bez_tokenu_se_nezahriva(self):
        """`luna_enabled` je výchozí zapnuté i bez tokenu, ale `get_luna()` pak vrátí None
        a zahřívaný katalog skončí chybou „Není nastaven žádný zdroj" — čtyři řádky
        v kodi.logu při každém warm-upu a nic zahřátého (log uživatele 2026-09-17)."""
        self.assertEqual(xbmcaddon.settings.get("token", ""), "", "výchozí stav je bez tokenu")
        self.assertEqual({p["src"] for p in map(params_of, service.warm_urls())}, {"trend"})

    def test_bez_luny_i_tmdb_zahriva_jen_trend(self):
        xbmcaddon.settings["luna_enabled"] = "false"
        # vlastní žebříček (trend) nepotřebuje ani jedno z nich, zahřívá se vždycky;
        # „s CZ dabingem/titulky“ (lang_catalog) se nezahřívá vůbec — nemá cache (2026-09-15)
        self.assertEqual({p["src"] for p in map(params_of, service.warm_urls())}, {"trend"})

    def test_dabing_a_titulky_jsou_u_filmu_i_serialu(self):
        """Živá kontrola jazyka (2026-09-15) nahradila Sosáčův export — dřív měly
        filmy dabing i titulky a seriály jen (mylně pojmenovaný) dabing, teď mají
        oba typy obě položky stejně, protože se jazyk zjišťuje stejně pro oba."""
        menu = self.browse_lang({"tmdb": None, "sosac_db": object(), "luna": None, "cinemeta": object()})
        self.assertIn(("dub", "movie"), menu)
        self.assertIn(("subs", "movie"), menu)
        self.assertIn(("dub", "series"), menu)
        self.assertIn(("subs", "series"), menu)
        # bez TMDB i Luny drží Populární Cinemeta
        menu_catalog = self.browse({"tmdb": None, "sosac_db": object(), "luna": None, "cinemeta": object()})
        self.assertIn(("cinemeta", "top", "movie", None), menu_catalog)

    def test_bez_sosac_db_zadny_dabing_ani_titulky(self):
        menu = self.browse_lang({"tmdb": None, "sosac_db": None, "luna": None, "cinemeta": None})
        self.assertEqual(menu, set())

    def test_nejsledovanejsi_je_vzdycky_v_menu_zanr_a_rok_uz_ne(self):
        """Vlastní žebříček (dashboard) nepotřebuje TMDB ani Lunu, na rozdíl od
        ostatních řádků není za `pick()` — je v menu vždycky. Samostatná položka
        „Podle roku“ z hlavního menu Filmy/Seriály vypadla (2026-09-15) a zpátky
        nepřibyla — na rozdíl od „Podle žánru“, ta se od 2026-09-15 (druhé kolo)
        vrátila zabudovaná do „Populární na TMDB“/„Nejlépe hodnocené“
        (`action="genres"`, viz `test_popularni_a_nejlepe_hodnocene_jdou_pres_genres`)."""
        menu = self.browse({"tmdb": None, "sosac_db": None, "luna": None, "cinemeta": None})
        self.assertIn(("trend", "nejsledovanejsi", "movie", None), menu)
        self.assertIn(("trend", "nejsledovanejsi", "series", None), menu)

        xbmcaddon.settings["tmdb_api_key"] = "abc"
        xbmcplugin.reset()
        default.browse_menu({"tmdb": object(), "sosac_db": None, "luna": None, "cinemeta": None}, "movie")
        katalogy = {params_of(u).get("catalog") for u in xbmcplugin.urls()}
        self.assertNotIn("year", katalogy)

    def test_popularni_a_nejlepe_hodnocene_jdou_pres_genres(self):
        """2026-09-15 (druhé kolo): obě jdou přes `list_genres()` — uživatel dřív
        neměl jak si Populární/Nejlépe hodnocené přefiltrovat podle žánru."""
        default.browse_menu({"tmdb": object(), "sosac_db": None, "luna": None, "cinemeta": None}, "movie")
        # `.get()`: menu má od „Pro tebe"/„Náhodný film" i položky bez `catalog`
        podle_katalogu = {params_of(u).get("catalog"): params_of(u)["action"] for u in xbmcplugin.urls()}
        self.assertEqual(podle_katalogu.get("popular"), "genres")
        self.assertEqual(podle_katalogu.get("top_rated"), "genres")


class FakeTmdb:
    """Jen to, co „Pro tebe" a „Náhodný film" z TMDB potřebují."""

    def __init__(self, podobne=None, katalog=None, zanry=("Komedie", "Drama"), chyba=None):
        self.podobne = podobne or {}
        self.katalog = katalog or []
        self.zanry = list(zanry)
        self.chyba = chyba
        self.similar_volani = []
        self.catalog_volani = []

    def similar(self, ctype, imdb_id, limit=40):
        self.similar_volani.append((ctype, imdb_id, limit))
        if self.chyba:
            raise self.chyba
        return self.podobne.get(imdb_id, [])[:limit]

    def catalogs(self, ctype):
        return [{"id": "popular", "name": "Populární", "genres": self.zanry}]

    def catalog(self, ctype, cid, genre=None, search=None, skip=0):
        self.catalog_volani.append((cid, genre, skip))
        return list(self.katalog)


def meta_item(iid, name=None, **extra):
    return dict({"id": iid, "name": name or iid, "_title": name or iid, "type": "movie",
                 "year": "2020", "description": "popis", "poster": "", "background": "",
                 "genres": ["Komedie"]}, **extra)


class TestProTebe(unittest.TestCase):
    """„Pro tebe" (6.3.0) — doporučení TMDB k naposledy zhlédnutým, počítaná u klienta
    z `watched` v profilu. Sleduje se hlavně to, co audit u katalogů hlídá pořád dokola:
    kolik se toho opravdu tahá ze sítě a co se stane, když zdroj neodpoví."""

    def setUp(self):
        reset_kodi()
        default.STORE.save("watched", {})
        default.STORE.save("items", {})
        default.STORE.save(default.FORYOU_SEEN_KEY, {})
        default.STORE.clear_cache()

    def videno(self, *keys):
        for key in keys:
            default.STORE.set_watched(key)

    def test_serial_je_jeden_vzor_a_typ_se_nepopletl(self):
        self.videno("tt0000001", "tt0000002:1:1", "tt0000002:1:2")
        self.assertEqual(default.foryou_seeds({}, "movie"), ["tt0000001"])
        self.assertEqual(default.foryou_seeds({}, "series"), ["tt0000002"])

    def test_doporuceni_z_tmdb_bez_uz_videnych(self):
        self.videno("tt0000001", "tt0000009")
        tmdb = FakeTmdb({"tt0000009": [meta_item("tt0000003"), meta_item("tt0000001")],
                         "tt0000001": [meta_item("tt0000004")]})
        default.list_foryou({"tmdb": tmdb, "dash": None}, "movie")
        ids = {params_of(u).get("id") for u in xbmcplugin.urls()}
        self.assertEqual(ids, {"tt0000003", "tt0000004"})   # tt0000001 je vzor i zhlédnuté

    def test_druhe_otevreni_uz_na_tmdb_nesaha(self):
        self.videno("tt0000001")
        tmdb = FakeTmdb({"tt0000001": [meta_item("tt0000003")]})
        default.list_foryou({"tmdb": tmdb, "dash": None}, "movie")
        xbmcplugin.reset()
        default.list_foryou({"tmdb": tmdb, "dash": None}, "movie")
        self.assertEqual(len(tmdb.similar_volani), 1)
        self.assertEqual([params_of(u).get("id") for u in xbmcplugin.urls()], ["tt0000003"])

    def test_novy_zhlednuty_titul_cache_zneplatni(self):
        self.videno("tt0000001")
        tmdb = FakeTmdb({"tt0000001": [meta_item("tt0000003")], "tt0000002": [meta_item("tt0000004")]})
        default.list_foryou({"tmdb": tmdb, "dash": None}, "movie")
        self.videno("tt0000002")
        xbmcplugin.reset()
        default.list_foryou({"tmdb": tmdb, "dash": None}, "movie")
        self.assertEqual({params_of(u).get("id") for u in xbmcplugin.urls()}, {"tt0000003", "tt0000004"})

    def test_vypadek_tmdb_ukaze_posledni_znamy_stav(self):
        self.videno("tt0000001")
        tmdb = FakeTmdb({"tt0000001": [meta_item("tt0000003")]})
        default.list_foryou({"tmdb": tmdb, "dash": None}, "movie")
        self.videno("tt0000002")          # jiné vzory → cache už nesedí
        rozbity = FakeTmdb(chyba=default.TmdbError("nope"))
        xbmcplugin.reset()
        default.list_foryou({"tmdb": rozbity, "dash": None}, "movie")
        self.assertEqual([params_of(u).get("id") for u in xbmcplugin.urls()], ["tt0000003"])

    def test_po_vypadku_se_nezkousi_znovu_hned(self):
        self.videno("tt0000001")
        rozbity = FakeTmdb(chyba=default.TmdbError("nope"))
        default.list_foryou({"tmdb": rozbity, "dash": None}, "movie")
        default.list_foryou({"tmdb": rozbity, "dash": None}, "movie")
        self.assertEqual(len(rozbity.similar_volani), 1)   # značka výpadku drží 5 minut

    def test_bez_klice_tmdb_se_pta_dashboardu(self):
        self.videno("tt0000001")

        class FakeDash:
            def __init__(self):
                self.volani = []

            def similar(self, ctype, imdb_id):
                self.volani.append((ctype, imdb_id))
                return [meta_item("tt0000005")]

        dash = FakeDash()
        default.list_foryou({"tmdb": None, "dash": dash}, "movie")
        self.assertEqual(dash.volani, [("movie", "tt0000001")])
        self.assertEqual([params_of(u).get("id") for u in xbmcplugin.urls()], ["tt0000005"])

    def test_protoze_jsi_videl_je_ze_snimku_bez_dotazu(self):
        self.videno("tt0000001")
        default.STORE.remember_item("tt0000001", {"title": "Matrix (1999)", "year": "1999", "type": "movie"})
        tmdb = FakeTmdb({"tt0000001": [meta_item("tt0000003")]})
        default.list_foryou({"tmdb": tmdb, "dash": None}, "movie")
        tag = xbmcplugin.items[0][2].getVideoInfoTag()
        plot = next(args[0] for name, args, _kw in tag.calls if name == "setPlot")
        self.assertIn("Matrix", plot.split("\n")[0])
        self.assertNotIn("1999", plot.split("\n")[0])   # rok kreslí Label2, ne název

    def test_bez_historie_se_polozka_v_menu_neukaze(self):
        apis = {"tmdb": None, "sosac_db": None, "luna": None, "cinemeta": None, "dash": None}
        default.browse_menu(apis, "movie")
        self.assertNotIn("foryou", [params_of(u).get("action") for u in xbmcplugin.urls()])
        self.videno("tt0000001")
        xbmcplugin.reset()
        default.browse_menu(apis, "movie")
        self.assertIn("foryou", [params_of(u).get("action") for u in xbmcplugin.urls()])

    def test_zahriva_se_jen_otevreny_typ(self):
        self.assertEqual(service.foryou_warm_urls(), [])
        default.note_foryou_open("movie")
        self.assertEqual([params_of(u)["type"] for u in service.foryou_warm_urls()], ["movie"])
        default.STORE.save(default.FORYOU_SEEN_KEY,
                           {"movie": int(time.time()) - (default.FORYOU_SEEN_DAYS + 1) * 86400})
        self.assertEqual(service.foryou_warm_urls(), [])


class TestNahodnyTitul(unittest.TestCase):
    """„Náhodný film" (6.3.0) — ne-složka, takže z výpisu ani z widgetu nevznikne modál."""

    def setUp(self):
        reset_kodi()
        default.STORE.save("watched", {})
        default.STORE.save("items", {})
        default.STORE.clear_cache()

    def test_polozka_v_menu_je_ne_slozka(self):
        apis = {"tmdb": None, "sosac_db": None, "luna": None, "cinemeta": None, "dash": None}
        default.browse_menu(apis, "movie")
        nahodny = [(u, folder) for _h, u, _li, folder in xbmcplugin.items
                   if params_of(u).get("action") == "random"]
        self.assertEqual(len(nahodny), 1)
        self.assertFalse(nahodny[0][1])

    def test_zanr_se_bere_z_toho_co_uzivatel_videl(self):
        default.STORE.set_watched("tt0000001")
        default.STORE.remember_item("tt0000001", {"title": "X", "type": "movie", "genres": ["Drama"]})
        tmdb = FakeTmdb(katalog=[meta_item("tt0000007")], zanry=("Komedie", "Drama"))
        meta, genre = default.random_pick({"tmdb": tmdb}, "movie")
        self.assertEqual(meta["id"], "tt0000007")
        self.assertEqual(genre, "Drama")
        self.assertEqual(tmdb.catalog_volani[0][1], "Drama")

    def test_zanr_ktery_zdroj_nenabizi_se_nepouzije(self):
        default.STORE.set_watched("tt0000001")
        default.STORE.remember_item("tt0000001", {"title": "X", "type": "movie", "genres": ["Comedy"]})
        tmdb = FakeTmdb(katalog=[meta_item("tt0000007")], zanry=("Komedie",))
        _meta, genre = default.random_pick({"tmdb": tmdb}, "movie")
        self.assertEqual(genre, "")

    def test_klik_ve_vypisu_otevre_dialog_vyberu_streamu(self):
        tmdb = FakeTmdb(katalog=[meta_item("tt0000007")])
        with mock.patch.object(default, "HANDLE", -1), \
                mock.patch.object(default, "pick_title") as pick, \
                mock.patch.object(default, "play") as play:
            default.random_title({"tmdb": tmdb}, "movie")
        pick.assert_called_once()
        self.assertEqual(pick.call_args[0][2], "tt0000007")
        play.assert_not_called()

    def test_prehrat_ze_skinu_jde_pres_play(self):
        tmdb = FakeTmdb(katalog=[meta_item("tt0000007")])
        with mock.patch.object(default, "play") as play, mock.patch.object(default, "pick_title") as pick:
            default.random_title({"tmdb": tmdb}, "movie")
        play.assert_called_once()
        self.assertEqual(play.call_args[1].get("ask"), "1")
        pick.assert_not_called()

    def _jazyk_engine(self, mapa):
        """Falešné jádro: `mapa` = id → seznam streamů."""
        class E:
            def raw_streams(self, ctype, cid, **kw):
                return mapa.get(cid, [])
        return E()

    def test_has_pref_lang(self):
        self.assertTrue(default.has_pref_lang([{"langs": ["CZ"]}], "CZ"))
        self.assertTrue(default.has_pref_lang([{"subs": ["SK"]}], "CZ"))   # náhradní jazyk titulků
        self.assertFalse(default.has_pref_lang([{"langs": ["EN"], "subs": ["EN"]}], "CZ"))
        self.assertFalse(default.has_pref_lang([], "CZ"))

    def test_nahodny_bere_titul_s_preferovanym_jazykem(self):
        tmdb = FakeTmdb(katalog=[meta_item("tt0000001"), meta_item("tt0000002"), meta_item("tt0000003")])
        engine = self._jazyk_engine({"tt0000001": [{"langs": ["EN"]}], "tt0000002": [{"langs": ["EN"]}],
                                     "tt0000003": [{"langs": ["CZ"]}]})
        with mock.patch.object(default, "setting", side_effect=lambda k, d="": "1" if k == "pref_lang" else d):
            for _ in range(5):
                meta, _genre, ok = default.random_choose({"tmdb": tmdb, "engine": engine}, "movie")
                self.assertEqual(meta["id"], "tt0000003")
                self.assertTrue(ok)

    def test_nikdo_nevyhovi_vezme_se_cokoli_a_oznami_se_to(self):
        tmdb = FakeTmdb(katalog=[meta_item("tt0000001")])
        engine = self._jazyk_engine({"tt0000001": [{"langs": ["EN"]}]})
        with mock.patch.object(default, "setting", side_effect=lambda k, d="": "1" if k == "pref_lang" else d):
            meta, _genre, ok = default.random_choose({"tmdb": tmdb, "engine": engine}, "movie")
        self.assertEqual(meta["id"], "tt0000001")
        self.assertFalse(ok)

    def test_bez_preferovaneho_jazyka_se_neoveruje(self):
        tmdb = FakeTmdb(katalog=[meta_item("tt0000001")])

        class E:
            def raw_streams(self, *a, **k):
                raise AssertionError("nemá se volat")
        meta, _genre, ok = default.random_choose({"tmdb": tmdb, "engine": E()}, "movie")
        self.assertEqual(meta["id"], "tt0000001")
        self.assertTrue(ok)

    def test_bez_zdroje_jen_oznameni_a_neuspesny_konec(self):
        with mock.patch.object(default, "play") as play:
            default.random_title({"tmdb": None, "luna": None, "cinemeta": None}, "movie")
        play.assert_not_called()
        self.assertEqual(xbmcplugin.resolved[-1][1], False)


class FakeSosacDb:
    """Kandidáti pro `list_lang_catalog` — místo skutečného exportu Sosáče jen
    surový seznam bez jazyka, ten se zjišťuje živě přes `raw_streams()`."""
    def __init__(self, items):
        self.items = items
        self.calls = []

    def catalog(self, ctype, cid, genre=None, search=None, skip=0, page=100):
        self.calls.append((ctype, cid, skip, page))
        return self.items[:page]


class TestLangCatalog(unittest.TestCase):
    """`list_lang_catalog` (2026-09-15) — živá kontrola CZ dabingu/titulků napříč
    aktivními zdroji uživatele, náhrada za nespolehlivý/chybějící export Sosáče."""

    def setUp(self):
        reset_kodi()
        default.STORE.clear_cache()   # `list_lang_catalog` teď cachuje na 8 h — sdílený STORE napříč testy
        self.engine = default.KodiEngine()

    def kandidati(self, n, ctype="movie"):
        return [{"id": f"sosacd_m_{i}", "type": ctype, "name": f"Film {i}", "year": "2026"} for i in range(n)]

    def test_rozdeli_podle_jazyka(self):
        cand = self.kandidati(3)

        def raw_streams(ctype, item_id, **kw):
            return {
                "sosacd_m_0": [{"langs": ["CZ"], "subs": []}],          # dabing
                "sosacd_m_1": [{"langs": [], "subs": ["CZ"]}],          # jen titulky
                "sosacd_m_2": [{"langs": [], "subs": []}],              # ani jedno
            }[item_id]
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}

        xbmcplugin.reset()
        default.list_lang_catalog(apis, "movie", "dub")
        dabing = {params_of(u).get("id") for u in xbmcplugin.urls()}
        self.assertEqual(dabing, {"sosacd_m_0"})

        xbmcplugin.reset()
        default.list_lang_catalog(apis, "movie", "subs")
        titulky = {params_of(u).get("id") for u in xbmcplugin.urls()}
        self.assertEqual(titulky, {"sosacd_m_1"})

    def test_dabing_ma_prednost_pred_titulky(self):
        """Titul s dabingem i titulky patří jen do seznamu dabingu — stejná
        sémantika jako dřív u Sosáčova exportu (`subs and not dub`)."""
        cand = self.kandidati(1)
        self.engine.raw_streams = lambda ctype, item_id, **kw: [{"langs": ["CZ"], "subs": ["CZ"]}]
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        default.list_lang_catalog(apis, "movie", "subs")
        self.assertEqual(xbmcplugin.urls(), [])

    def test_limit_30_naplni_seznam_a_nezkouma_vic_nez_potreba(self):
        """Dabing i titulky se počítají v jednom průchodu (2026-09-15) — dokud
        titulky (tady žádné) nedosáhnou cíle nebo nedojdou kandidáti, průchod
        neskončí jen proto, že dabing už má 30; jakmile ale i titulky cíl
        naplní (test níže), zastaví se dřív než na `LANG_CATALOG_CAP`."""
        cand = self.kandidati(default.LANG_CATALOG_CAP)
        volano = []

        def raw_streams(ctype, item_id, **kw):
            volano.append(item_id)
            return [{"langs": ["CZ"], "subs": []}]   # nikdy titulky bez dabingu → cíl titulků se nikdy nenaplní
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual(len(volano), default.LANG_CATALOG_CAP)
        self.assertEqual(len(xbmcplugin.urls()), default.LANG_CATALOG_TARGET)

    def test_limit_zastavi_jakmile_dabing_i_titulky_maji_cil(self):
        cand = self.kandidati(default.LANG_CATALOG_CAP)
        volano = []

        def raw_streams(ctype, item_id, **kw):
            volano.append(item_id)
            i = int(item_id.rsplit("_", 1)[-1])
            return [{"langs": ["CZ"], "subs": []}] if i % 2 == 0 else [{"langs": [], "subs": ["CZ"]}]
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual(len(volano), 2 * default.LANG_CATALOG_TARGET)

    def test_cap_kdyz_se_cil_nenaplni(self):
        cand = self.kandidati(default.LANG_CATALOG_CAP)
        volano = []

        def raw_streams(ctype, item_id, **kw):
            volano.append(item_id)
            return [{"langs": [], "subs": []}]   # nikdy nevyhoví
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual(len(volano), default.LANG_CATALOG_CAP)
        self.assertEqual(xbmcplugin.urls(), [])

    def test_vypadek_kandidata_se_preskoci(self):
        cand = self.kandidati(2)

        def raw_streams(ctype, item_id, **kw):
            if item_id == "sosacd_m_0":
                raise ConnectionError("timeout")
            return [{"langs": ["CZ"], "subs": []}]
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual({params_of(u).get("id") for u in xbmcplugin.urls()}, {"sosacd_m_1"})

    def test_bez_sosac_db_nic_nezkouma(self):
        apis = {"engine": self.engine, "sosac_db": None, "luna": None}
        default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual(xbmcplugin.urls(), [])

    def test_seriove_epizody_pouzivaji_vlastni_typ_movie(self):
        """Seznam pod Seriály (`ctype="series"`) skládají ploché epizody
        (`type: "movie"` v metadatech) — streamy i vykreslení se řídí typem
        položky, ne obalujícím menu, jinak by se epizoda otevírala jako složka sezón."""
        cand = self.kandidati(1, ctype="movie")
        volane_ctype = []

        def raw_streams(ctype, item_id, **kw):
            volane_ctype.append(ctype)
            return [{"langs": ["CZ"], "subs": []}]
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        default.list_lang_catalog(apis, "series", "dub")
        self.assertEqual(volane_ctype, ["movie"])
        self.assertEqual(params_of(xbmcplugin.urls()[0])["action"], "play")

    def test_dily_serialu_se_zobrazuji_s_cislem_dilu_ne_jen_nazvem(self):
        """`bare_title()` u Sosáčových id přednostně bere `_title` — u epizod
        (`episode_meta()` v jádru) je to ale záměrně jen holý název seriálu
        (potřebuje ho hledání napříč zdroji), zatímco `name` nese i sezónu/díl.
        Bez opravy se pod Seriály zobrazovalo desetkrát za sebou jen jméno
        seriálu bez rozlišení (2026-09-15, nahlásil uživatel: „Dogu“ 10x)."""
        cand = [{"id": f"sosacd_m_ep{i}", "type": "movie", "name": f"Dogu {2}x{i:02d} Díl {i}",
                 "_title": "Dogu", "year": ""} for i in range(1, 4)]
        self.engine.raw_streams = lambda ctype, item_id, **kw: [{"langs": ["CZ"], "subs": []}]
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        default.list_lang_catalog(apis, "series", "dub")
        labels = [li.getLabel() for _h, _u, li, _f in xbmcplugin.items]
        self.assertEqual(labels, ["Dogu 2x01 Díl 1", "Dogu 2x02 Díl 2", "Dogu 2x03 Díl 3"])

    def test_druhe_otevreni_nezkouma_znovu_streamy(self):
        """8h cache (2026-09-15, po ověření rychlosti): druhé otevření stejného
        seznamu (dabing/movie) se má obsloužit z cache, bez dalšího volání
        `raw_streams()` pro každého kandidáta znovu."""
        cand = self.kandidati(2)
        calls = []

        def raw_streams(ctype, item_id, **kw):
            calls.append(item_id)
            return [{"langs": ["CZ"], "subs": []}]
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual(len(calls), 2)
        xbmcplugin.reset()
        default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual(len(calls), 2)   # beze změny — druhé volání šlo z cache
        self.assertEqual({params_of(u).get("id") for u in xbmcplugin.urls()}, {"sosacd_m_0", "sosacd_m_1"})

    def test_otevreni_titulku_po_dabingu_nezkouma_streamy_znovu(self):
        """Dabing i titulky sdílí jeden výpočet a jednu cache (`lang_catalog:{ctype}`,
        2026-09-15) — otevření druhého seznamu hned po prvním má jít z cache, ne
        spustit vlastní `raw_streams()` znovu pro všechny kandidáty."""
        cand = self.kandidati(4)
        calls = []

        def raw_streams(ctype, item_id, **kw):
            calls.append(item_id)
            i = int(item_id.rsplit("_", 1)[-1])
            return [{"langs": ["CZ"], "subs": []}] if i % 2 == 0 else [{"langs": [], "subs": ["CZ"]}]
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual(len(calls), 4)
        pocet_po_dabingu = len(calls)
        xbmcplugin.reset()
        default.list_lang_catalog(apis, "movie", "subs")
        self.assertEqual(len(calls), pocet_po_dabingu)   # beze změny — titulky přišly z cache
        self.assertEqual({params_of(u).get("id") for u in xbmcplugin.urls()},
                         {"sosacd_m_1", "sosacd_m_3"})

    def test_zahrivani_na_pozadi_neukazuje_progress(self):
        """Zahřívání (`warming()` — `nokturno.warm` property) běží na pozadí přes
        JSON-RPC, `DialogProgressBG` by tam jen zbytečně blikal."""
        cand = self.kandidati(1)
        self.engine.raw_streams = lambda ctype, item_id, **kw: [{"langs": ["CZ"], "subs": []}]
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        xbmcgui.Window(10000).setProperty(default.WARM_PROP, "1")
        try:
            with mock.patch.object(default.xbmcgui, "DialogProgressBG") as bar_cls:
                default.list_lang_catalog(apis, "movie", "dub")
        finally:
            xbmcgui.Window(10000).clearProperty(default.WARM_PROP)
        bar_cls.assert_not_called()

    def test_soubezne_otevreni_stejneho_seznamu_nepocita_znovu(self):
        """Zámek přes vlastnost okna (`LANG_LOCK_PROP`) — je-li seznam pro tentýž
        klíč zrovna zamčený (jiný proces ho počítá), druhé volání nespouští
        `raw_streams()` znovu, jen počká a přečte, co najde v cache."""
        cand = self.kandidati(2)
        calls = []

        def raw_streams(ctype, item_id, **kw):
            calls.append(item_id)
            return [{"langs": ["CZ"], "subs": []}]
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        xbmcgui.Window(10000).setProperty(f"{default.LANG_LOCK_PROP}:lang_catalog:movie", str(time.time()))
        try:
            default.list_lang_catalog(apis, "movie", "dub")
        finally:
            xbmcgui.Window(10000).clearProperty(f"{default.LANG_LOCK_PROP}:lang_catalog:movie")
        self.assertEqual(calls, [])
        self.assertEqual(xbmcplugin.urls(), [])   # nic v cache, tak radši prázdný seznam než souběžný výpočet

    def test_stary_mrtvy_zamek_se_prebere_bez_cekani(self):
        """Zabitý proces (Kodi po 5 s neuposlechnutí abortu skript zabije, `finally`
        se nestihne) nechá zámek navěky nastavený — bez kontroly stáří by každé
        další otevření jen 90 s marně čekalo a pak vrátilo prázdno navěky
        (2026-09-15, nahlásil uživatel: „úplně se to seklo"). Zámek starší než
        `LANG_LOCK_STALE` se má rovnou přebrat, ne čekat."""
        cand = self.kandidati(1)
        calls = []

        def raw_streams(ctype, item_id, **kw):
            calls.append(item_id)
            return [{"langs": ["CZ"], "subs": []}]
        self.engine.raw_streams = raw_streams
        apis = {"engine": self.engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        stary = time.time() - default.LANG_LOCK_STALE - 1
        xbmcgui.Window(10000).setProperty(f"{default.LANG_LOCK_PROP}:lang_catalog:movie", str(stary))
        default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual(calls, ["sosacd_m_0"])
        self.assertEqual({params_of(u).get("id") for u in xbmcplugin.urls()}, {"sosacd_m_0"})

    def test_zamek_se_uvolni_i_po_chybe(self):
        """`finally` v `_lang_catalog_locked()` — pád při výpočtu nesmí nechat
        zámek navěky nastavený, jinak by žádné další otevření nikdy nedoběhlo."""
        apis = {"engine": self.engine, "sosac_db": None, "luna": None}
        prop = f"{default.LANG_LOCK_PROP}:lang_catalog:movie"

        def bum(*a, **kw):
            raise RuntimeError("bum")
        with mock.patch.object(default, "_build_lang_catalog", side_effect=bum):
            with self.assertRaises(RuntimeError):
                default.list_lang_catalog(apis, "movie", "dub")
        self.assertEqual(xbmcgui.Window(10000).getProperty(prop), "")


class TestLangCatalogMenu(unittest.TestCase):
    """`lang_catalog_menu()` (2026-09-15) — vstupní bod z menu Filmy/Seriály.
    Na rozdíl od `list_lang_catalog()` nespouští drahý živý přepočet automaticky,
    jen když je jasné, že se nic nezpozdí (cache hotová, nebo to už počítá někdo
    jiný) — jinak nabídne obyčejnou položku seznamu, kterou musí uživatel sám
    kliknout, žádný modální dialog (ten nejde, cesta jde spustit i z widgetu)."""

    def setUp(self):
        reset_kodi()
        default.STORE.clear_cache()
        self.engine = default.KodiEngine()
        self.apis = {"engine": self.engine, "sosac_db": FakeSosacDb([]), "luna": None}

    def test_bez_cache_a_bez_zamku_nabidne_jen_polozku_ke_spusteni(self):
        xbmcplugin.reset()
        default.lang_catalog_menu(self.apis, "movie", "dub")
        urls = xbmcplugin.urls()
        self.assertEqual(len(urls), 1)
        params = params_of(urls[0])
        self.assertEqual(params["action"], "lang_catalog_trigger")
        self.assertEqual(params["want"], "dub")
        self.assertEqual(params["type"], "movie")

    def test_hotova_cache_jde_rovnou_do_seznamu(self):
        default.STORE.cached_if("lang_catalog:movie", default.LANG_CATALOG_TTL,
                                 lambda: {"dub": [{"id": "sosacd_m_0", "type": "movie", "name": "Film"}],
                                          "subs": []})
        xbmcplugin.reset()
        default.lang_catalog_menu(self.apis, "movie", "dub")
        self.assertEqual({params_of(u).get("id") for u in xbmcplugin.urls()}, {"sosacd_m_0"})

    def test_cizi_vypocet_uz_bezi_neceka_jen_ohlasi(self):
        """Dřív šlo rovnou do `list_lang_catalog()`, která na cizí zámek čeká
        až `LANG_LOCK_WAIT` (90 s) — kratší než reálná doba běhu (až ~4 min),
        takže skoro vždycky skončilo tichým prázdným seznamem (2026-09-15,
        nahlásil uživatel). Teď se na nic nečeká, jen se to ohlásí."""
        prop = f"{default.LANG_LOCK_PROP}:lang_catalog:movie"
        xbmcgui.Window(10000).setProperty(prop, str(time.time()))
        try:
            xbmcplugin.reset()
            default.lang_catalog_menu(self.apis, "movie", "dub")
        finally:
            xbmcgui.Window(10000).clearProperty(prop)
        urls = xbmcplugin.urls()
        self.assertEqual(len(urls), 1)
        self.assertEqual(params_of(urls[0])["action"], "lang_catalog_menu")


class TestLangCatalogTrigger(unittest.TestCase):
    """`lang_catalog_trigger()` (2026-09-15, druhé kolo) — klik na „Klepni pro
    spuštění" nepočítá nic sám, jen požádá `service.py` (`LANG_TRIGGER_PROP`)
    a hned se vrátí, ať uživatel nemusí čekat na místě."""

    def setUp(self):
        reset_kodi()
        default.STORE.clear_cache()
        self.apis = {"engine": default.KodiEngine(), "sosac_db": FakeSosacDb([]), "luna": None}

    def test_zapise_zadost_a_nespocita_nic_sam(self):
        xbmcplugin.reset()
        default.lang_catalog_trigger(self.apis, "movie", "dub")
        self.assertEqual(xbmcgui.Window(10000).getProperty(f"{default.LANG_TRIGGER_PROP}:movie"), "1")
        urls = xbmcplugin.urls()
        self.assertEqual(len(urls), 1)
        self.assertEqual(params_of(urls[0])["action"], "lang_catalog_menu")

    def test_mezitim_hotovo_jde_rovnou_do_seznamu_bez_zadosti(self):
        default.STORE.cached_if("lang_catalog:movie", default.LANG_CATALOG_TTL,
                                 lambda: {"dub": [{"id": "sosacd_m_0", "type": "movie", "name": "Film"}],
                                          "subs": []})
        xbmcplugin.reset()
        default.lang_catalog_trigger(self.apis, "movie", "dub")
        self.assertEqual({params_of(u).get("id") for u in xbmcplugin.urls()}, {"sosacd_m_0"})
        self.assertEqual(xbmcgui.Window(10000).getProperty(f"{default.LANG_TRIGGER_PROP}:movie"), "")

    def test_cizi_vypocet_uz_bezi_neceka_a_nezada_znovu(self):
        """Dřív šlo v tomhle případě rovnou do `list_lang_catalog()` — stejný
        blokující bug jako v `lang_catalog_menu()` (viz tam)."""
        prop = f"{default.LANG_LOCK_PROP}:lang_catalog:movie"
        xbmcgui.Window(10000).setProperty(prop, str(time.time()))
        try:
            xbmcplugin.reset()
            default.lang_catalog_trigger(self.apis, "movie", "dub")
        finally:
            xbmcgui.Window(10000).clearProperty(prop)
        # nová žádost se nezapisuje, výpočet už běží
        self.assertEqual(xbmcgui.Window(10000).getProperty(f"{default.LANG_TRIGGER_PROP}:movie"), "")
        urls = xbmcplugin.urls()
        self.assertEqual(len(urls), 1)
        self.assertEqual(params_of(urls[0])["action"], "lang_catalog_menu")


class FakeStats:
    def __init__(self, due=True):
        self.uses, self.plays, self.sent, self.seen = [], [], [], []
        self._due = due
        self.last_message = None
        self.data = {}

    def note_use(self, ts):
        self.uses.append(ts)

    def note_play(self, key, title, year, kind):
        self.plays.append((key, title, year, kind))

    def due(self):
        return self._due

    def send(self, url, **kwargs):
        self.sent.append((url, kwargs))
        return True, ""

    def mark_message_seen(self, message_id):
        self.seen.append(message_id)


class FakeStorage:
    def __init__(self, slot, name, files):
        self.slot, self.name, self._files = slot, name, files

    def files(self):
        return self._files


class TestMojeUloziste(unittest.TestCase):
    """`list_dav_browse()` — napřed vždy jméno úložiště, pak teprve data
    (2026-09-15, i s jediným nastaveným úložištěm — dřív se s jedním úložištěm
    rovnou skočilo na data a jméno se nikde neukázalo)."""

    def setUp(self):
        reset_kodi()

    def test_jedine_uloziste_prvne_ukaze_jeho_jmeno(self):
        storage = FakeStorage(1, "NUC Office", [{"path": "Film.mkv", "name": "Film.mkv"}])
        default.list_dav_browse({"dav": [storage]})
        self.assertEqual([li.getLabel() for _h, _u, li, _f in xbmcplugin.items], ["NUC Office"])
        self.assertEqual(params_of(xbmcplugin.urls()[0]).get("slot"), "1")

    def test_vic_ulozist_ukaze_obe_jmena(self):
        s1, s2 = FakeStorage(1, "NUC Office", []), FakeStorage(2, "NAS doma", [])
        default.list_dav_browse({"dav": [s1, s2]})
        self.assertEqual([li.getLabel() for _h, _u, li, _f in xbmcplugin.items], ["NUC Office", "NAS doma"])

    def test_vybrane_uloziste_ukazuje_data(self):
        storage = FakeStorage(1, "NUC Office", [{"path": "Film.mkv", "name": "Film.mkv"}])
        default.list_dav_browse({"dav": [storage]}, slot=1)
        self.assertEqual(len(xbmcplugin.items), 1)   # jen jeden soubor, žádná podsložka navíc
        self.assertEqual(params_of(xbmcplugin.urls()[0])["action"], "play_dav")

    def test_bez_uloziste_chyba(self):
        with self.assertRaises(default.StorageError):
            default.list_dav_browse({"dav": []})


class TestSluzbaStatistiky(unittest.TestCase):
    def setUp(self):
        reset_kodi()
        xbmcaddon.settings.update(stats_enabled="true", ws_enabled="true", ws_username="u",
                                  sosac_enabled="true", streamuj_username="s", tmdb_api_key="k")
        xbmc.cond_visible.add("System.Platform.Android")

    def test_kontext_hlasi_jen_zapnute_zdroje_bez_uctu(self):
        ctx = service.stats_context(xbmcaddon.Addon())
        self.assertEqual(ctx["sources"], ["sosac", "webshare", "tmdb"])
        self.assertEqual(ctx["platform"], "Android")
        self.assertEqual(ctx["product"], "kodi")
        self.assertEqual(ctx["version"], xbmcaddon.info["version"])
        for k, v in ctx.items():
            self.assertNotIn("u", str(v) if k == "sources" else "", "jméno účtu nesmí do statistik")

    def test_plne_hlaseni_kdyz_je_cas(self):
        stats = FakeStats(due=True)
        service.stats_tick(stats)
        self.assertEqual(len(stats.sent), 1)
        url, kwargs = stats.sent[0]
        self.assertEqual(url, service.COLLECT_URL)
        self.assertEqual(kwargs["product"], "kodi")
        self.assertIn("webshare", kwargs["sources"])
        self.assertNotIn("ping", kwargs)

    def test_neni_cas_nic_neposila(self):
        stats = FakeStats(due=False)
        service.stats_tick(stats)
        self.assertEqual(stats.sent, [])

    def test_vypnute_statistiky_posilaji_jen_ping(self):
        xbmcaddon.settings["stats_enabled"] = "false"
        stats = FakeStats(due=True)
        service.stats_tick(stats)
        self.assertEqual(len(stats.sent), 1)
        _url, kwargs = stats.sent[0]
        self.assertIs(kwargs.get("ping"), True)
        self.assertEqual(kwargs["product"], "kodi")
        self.assertNotIn("sources", kwargs)
        self.assertNotIn("platform", kwargs)

    def test_zprava_z_dashboardu_se_zobrazi_a_oznaci_precteno(self):
        """`textviewer()`, ne `.ok()` — delší zprávu jde posouvat, `.ok()` ji prostě ořízne."""
        stats = FakeStats(due=True)
        stats.last_message = {"id": 7, "text": "Nová verze je venku"}
        service.stats_tick(stats)
        wait_message_thread()
        self.assertEqual(xbmcgui.oks, [])
        self.assertEqual(len(xbmcgui.textviewers), 1)
        self.assertEqual(xbmcgui.textviewers[0][1], "Nová verze je venku")
        self.assertEqual(stats.seen, [7])

    def test_zprava_prijde_i_pri_vypnutych_statistikach(self):
        xbmcaddon.settings["stats_enabled"] = "false"
        stats = FakeStats(due=True)
        stats.last_message = {"id": 3, "text": "ahoj"}
        service.stats_tick(stats)
        wait_message_thread()
        self.assertEqual(stats.seen, [3])

    def test_bez_zpravy_se_nic_nezobrazi(self):
        stats = FakeStats(due=True)
        service.stats_tick(stats)
        self.assertEqual(xbmcgui.oks, [])
        self.assertEqual(xbmcgui.textviewers, [])
        self.assertEqual(stats.seen, [])

    def test_udalosti_z_pluginu_se_prevezmou_a_smazou(self):
        win = xbmcgui.Window(10000)
        win.setProperty(service.USED_PROP, "1700000000")
        win.setProperty(service.VIEWED_PROP, '{"id": "tt1", "title": "Matrix", "year": 1999, "kind": "movie"}')
        stats = FakeStats(due=False)
        service.stats_tick(stats)
        self.assertEqual(stats.uses, [1700000000])
        self.assertEqual(stats.plays, [("tt1", "Matrix", 1999, "movie")])
        self.assertEqual(win.getProperty(service.USED_PROP), "")
        self.assertEqual(win.getProperty(service.VIEWED_PROP), "")

    def test_rozbity_json_udalosti_neshodi_sluzbu(self):
        xbmcgui.Window(10000).setProperty(service.VIEWED_PROP, "{nic")
        stats = FakeStats(due=False)
        service.stats_tick(stats)
        self.assertEqual(stats.plays, [])

    def test_force_stats_po_aktualizaci_nemava_dokud_nedobehne_start(self):
        """2026-09-16: modální Dialog().ok() volaný hned na prvním tiku služby (než
        doběhne start skinu) nikdo nezaznamená — FORCE_STATS_PROP (nastaví default.py
        při detekci nové verze) se proto neuplatní, dokud neuplyne MESSAGE_DELAY od
        startu služby (`_STARTED_AT`); vlastnost zůstane nastavená pro další tik."""
        xbmcgui.Window(10000).setProperty(service.FORCE_STATS_PROP, "1")
        with mock.patch.object(service, "_STARTED_AT", time.time()):
            stats = FakeStats(due=False)
            service.stats_tick(stats)
        self.assertEqual(stats.sent, [])
        self.assertEqual(xbmcgui.Window(10000).getProperty(service.FORCE_STATS_PROP), "1")

    def test_force_stats_po_aktualizaci_posle_hned_jak_dobehne_start(self):
        xbmcgui.Window(10000).setProperty(service.FORCE_STATS_PROP, "1")
        with mock.patch.object(service, "_STARTED_AT", time.time() - service.MESSAGE_DELAY - 1):
            stats = FakeStats(due=False)
            service.stats_tick(stats)
        self.assertEqual(len(stats.sent), 1)
        self.assertEqual(xbmcgui.Window(10000).getProperty(service.FORCE_STATS_PROP), "")

    def test_force_stats_po_aktualizaci_nepta_se_znovu_kdyz_uz_poslano(self):
        """Start služby po aktualizaci statistiky poslal a zprávu vyzvedl; druhé odeslání do
        minut dashboard odmítl (HTTP 429) — nucené se přeskočí."""
        xbmcgui.Window(10000).setProperty(service.FORCE_STATS_PROP, "1")
        with mock.patch.object(service, "_STARTED_AT", time.time() - service.MESSAGE_DELAY - 1):
            stats = FakeStats(due=False)
            stats.data["last_sent"] = time.time() - 60
            service.stats_tick(stats)
        self.assertEqual(stats.sent, [])
        self.assertEqual(xbmcgui.Window(10000).getProperty(service.FORCE_STATS_PROP), "")


CZ_SRT = ("1\n00:00:01,000 --> 00:00:03,000\nŘekni mi, proč jsi tady a co tu děláš.\n\n"
          "2\n00:00:04,000 --> 00:00:06,000\nNevím, jestli můžu věřit tomu, že přijdeš.\n\n") * 8
EN_SRT = ("1\n00:00:01,000 --> 00:00:03,000\nTell me what you are doing here and why.\n\n"
          "2\n00:00:04,000 --> 00:00:06,000\nI don't know if I can trust that you will come to the party.\n\n") * 8


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestTitulkyAZvuk(unittest.TestCase):
    """2026-09-16: zvuk a titulky podle preferovaného jazyka — titulky ze zdroje se stáhnou
    s jazykem v názvu (`local_subtitles`), služba po startu přepne stopy (`Player.apply_tracks`)."""

    def setUp(self):
        reset_kodi()
        xbmcaddon.settings["pref_lang"] = "1"   # CZ

    def test_titulky_se_stahnou_s_jazykem_a_ceske_jdou_prvni(self):
        bodies = {"https://ws/en.srt": EN_SRT.encode("utf-8"), "https://ws/cz.srt": CZ_SRT.encode("cp1250")}
        seen_headers = {}

        def urlopen(req, timeout=0):
            seen_headers[req.full_url] = dict(req.header_items())
            return _Resp(bodies[req.full_url])
        links = {"ws:en": "https://ws/en.srt", "ws:cz": "https://ws/cz.srt|Cookie=a%3Db"}
        with mock.patch.object(default, "resolve_url", side_effect=lambda apis, ref: links[ref]), \
             mock.patch.object(default.urllib.request, "urlopen", side_effect=urlopen):
            paths = default.local_subtitles({}, ["ws:en", "ws:cz"])
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].endswith(".cze.srt"), paths)
        self.assertTrue(paths[1].endswith(".eng.srt"), paths)
        with open(paths[0], encoding="utf-8-sig") as f:
            self.assertEqual(f.read(), CZ_SRT, "windows-1250 převedené do UTF-8")
        self.assertEqual(seen_headers["https://ws/cz.srt"].get("Cookie"), "a=b", "hlavičky za | jdou do požadavku")

    def test_nestazene_titulky_projdou_odkazem(self):
        with mock.patch.object(default, "resolve_url", return_value="https://ws/x.srt"), \
             mock.patch.object(default.urllib.request, "urlopen", side_effect=OSError("síť")):
            self.assertEqual(default.local_subtitles({}, ["ws:x"]), ["https://ws/x.srt"])

    def test_pomale_a_nedostupne_titulky_prehrani_nezdrzi(self):
        """Office 2026-09-16: WebShare hlásil u titulků „temporarily unavailable“ až po 5–13 s."""
        gate = threading.Event()

        def resolve(apis, ref):
            if ref == "ws:pomale":
                gate.wait(5)
                return "https://ws/pomale.srt"
            raise default.NokturnoError("WebShare tenhle soubor teď nevydá")
        started = time.time()
        with mock.patch.object(default, "resolve_url", side_effect=resolve), \
             mock.patch.object(default, "SUBS_BUDGET_S", 0.3):
            self.assertEqual(default.local_subtitles({}, ["ws:pomale", "ws:pryc"]), [])
        gate.set()
        self.assertLess(time.time() - started, 2)

    def test_play_preda_titulky_a_jazyky_streamu_sluzbe(self):
        streams = [{"url": "ws:1", "label": "Film.2020.1080p.mkv", "detail": "2 GB", "source": "ws",
                    "langs": ["EN"], "subtitles": ["ws:sub"]}]
        with mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)), \
             mock.patch.object(default, "collect_streams", return_value=streams), \
             mock.patch.object(default, "resolve_url", return_value="https://cdn/x.mkv"), \
             mock.patch.object(default, "local_subtitles", return_value=["/tmp/nokturno-1.cze.srt"]) as local:
            default.play({}, "movie", "tt1")
        local.assert_called_once_with({}, ["ws:sub"])
        _handle, succeeded, li = xbmcplugin.resolved[0]
        self.assertTrue(succeeded)
        self.assertEqual(li.subtitles, ["/tmp/nokturno-1.cze.srt"])
        playing = json.loads(xbmcgui.Window(10000).getProperty(default.PLAYING_PROP))
        self.assertEqual(playing["stream_langs"], ["EN"])

    def run_tracks(self, props, item=None):
        calls = []

        def rpc(method, **params):
            calls.append((method, params))
            if method == "Player.GetActivePlayers":
                return [{"playerid": 1, "type": "video"}]
            if method == "Player.GetProperties":
                return props
            return "OK"
        player = service.Player(store=default.STORE, stats=None)
        item = item or {"id": "tt1"}
        player.item = item
        with mock.patch.object(service, "rpc", side_effect=rpc), \
             mock.patch.object(service, "TRACKS_DELAY", 0), \
             mock.patch.object(service.Player, "isPlayingVideo", return_value=True):
            player.apply_tracks(item)
        return [c for c in calls if c[0].startswith("Player.Set")]

    def test_anglicky_default_prepne_na_cesky_dabing_a_vypne_titulky(self):
        props = {"audiostreams": [{"index": 0, "language": "eng", "channels": 6},
                                  {"index": 1, "language": "cze", "channels": 6}],
                 "currentaudiostream": {"index": 0, "language": "eng"},
                 "subtitles": [{"index": 0, "language": "cze"}],
                 "currentsubtitle": {"index": 0, "language": "cze"}, "subtitleenabled": True}
        self.assertEqual(self.run_tracks(props), [
            ("Player.SetAudioStream", {"playerid": 1, "stream": 1}),
            ("Player.SetSubtitle", {"playerid": 1, "subtitle": "off"}),
        ])

    def test_cesky_zvuk_vypne_i_vynucene_titulky(self):
        """Office 2026-09-16 (Počátek): po přepnutí na českou stopu zůstaly zapnuté „CZE forced“."""
        props = {"audiostreams": [{"index": 0, "language": "eng", "channels": 6},
                                  {"index": 1, "language": "cze", "channels": 6}],
                 "currentaudiostream": {"index": 0, "language": "eng"},
                 "subtitles": [{"index": 0, "language": "cze", "name": "CZE"},
                               {"index": 1, "language": "cze", "name": "CZE forced", "isdefault": True}],
                 "currentsubtitle": {"index": 1, "language": "cze", "name": "CZE forced"}, "subtitleenabled": True}
        self.assertEqual(self.run_tracks(props), [
            ("Player.SetAudioStream", {"playerid": 1, "stream": 1}),
            ("Player.SetSubtitle", {"playerid": 1, "subtitle": "off"}),
        ])

    def test_bez_ceskeho_zvuku_zapne_ceske_titulky(self):
        props = {"audiostreams": [{"index": 0, "language": "eng", "channels": 6}],
                 "currentaudiostream": {"index": 0, "language": "eng"},
                 "subtitles": [{"index": 0, "language": "eng"}, {"index": 1, "language": "cze", "name": "nokturno-ab"}],
                 "currentsubtitle": {}, "subtitleenabled": False}
        self.assertEqual(self.run_tracks(props), [
            ("Player.SetSubtitle", {"playerid": 1, "subtitle": 1, "enable": True}),
        ])

    def test_neoznacena_stopa_rozhodne_jazyk_ze_zdroje(self):
        props = {"audiostreams": [{"index": 0, "language": "", "channels": 2}],
                 "currentaudiostream": {"index": 0, "language": ""},
                 "subtitles": [{"index": 0, "language": "cze"}], "currentsubtitle": {}, "subtitleenabled": False}
        self.assertEqual(self.run_tracks(props, {"id": "tt1", "stream_langs": ["EN"]}),
                         [("Player.SetSubtitle", {"playerid": 1, "subtitle": 0, "enable": True})])
        self.assertEqual(self.run_tracks(props, {"id": "tt1", "stream_langs": ["CZ"]}), [])
        self.assertEqual(self.run_tracks(props, {"id": "tt1"}), [], "nevíme — nic neměnit")

    def test_nastaveni_vypne_prepinani(self):
        xbmcaddon.settings["auto_audio"] = "false"
        xbmcaddon.settings["auto_subs"] = "0"
        props = {"audiostreams": [{"index": 0, "language": "eng"}, {"index": 1, "language": "cze"}],
                 "currentaudiostream": {"index": 0, "language": "eng"},
                 "subtitles": [{"index": 0, "language": "cze"}], "currentsubtitle": {}, "subtitleenabled": False}
        self.assertEqual(self.run_tracks(props), [])
        xbmcaddon.settings["pref_lang"] = "0"
        xbmcaddon.settings["auto_audio"] = "true"
        xbmcaddon.settings["auto_subs"] = "1"
        self.assertEqual(self.run_tracks(props), [], "bez preferovaného jazyka se nic nemění")

    def test_jiny_titul_mezitim_nic_neprepina(self):
        player = service.Player(store=default.STORE, stats=None)
        player.item = {"id": "jiny"}
        with mock.patch.object(service, "rpc", side_effect=AssertionError("nemá sahat na přehrávač")), \
             mock.patch.object(service, "TRACKS_DELAY", 0):
            player.apply_tracks({"id": "tt1"})


def browser_form(page):
    """Co by z formuláře odeslal prohlížeč beze změn: zaškrtnuté checkboxy, hodnoty polí, vybrané volby."""
    from html.parser import HTMLParser
    form = {}

    class P(HTMLParser):
        select = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "input" and a.get("name"):
                if a.get("type") == "checkbox":
                    if "checked" in a:
                        form[a["name"]] = "on"
                else:
                    form[a["name"]] = a.get("value", "")
            elif tag == "select":
                P.select = a.get("name")
            elif tag == "option" and "selected" in a and P.select:
                form[P.select] = a.get("value", "")
    P().feed(page)
    return form


class TestNastavitZMobilu(unittest.TestCase):
    """2026-09-16: QR na TV → formulář v mobilu ve stejné Wi-Fi → uložení do nastavení."""

    def setUp(self):
        reset_kodi()
        xbmcgui.windows_shown.clear()

    def test_formular_ze_settings_xml(self):
        schema = default.remote_setup_schema()
        ids = [s["id"] for s in schema]
        self.assertEqual(ids[0], "ws")
        self.assertNotIn("advanced", ids)
        self.assertNotIn("info", ids)
        fields = {f["id"]: f for s in schema for f in s["fields"] if f.get("type") not in ("heading", "info", "action")}
        self.assertEqual(fields["ws_password"]["type"], "password")
        self.assertEqual(fields["ws_username"]["type"], "text")
        self.assertEqual(fields["ws_username"]["enable"], ("ws_enabled", "true"))
        self.assertEqual(fields["ws_enabled"]["type"], "bool")
        self.assertEqual(fields["pref_lang"]["type"], "choice")
        self.assertEqual([v for v, _ in fields["audio_probe"]["options"]][:3], ["0", "4", "8"])
        self.assertNotIn("remote_setup_action", fields, "tlačítka akcí na stránku nepatří")
        self.assertNotIn("download_dir", fields)
        self.assertTrue(schema[0]["open"])
        self.assertEqual(fields["stream_layout"]["type"], "order")
        self.assertEqual([k for k, _ in fields["stream_layout"]["items"]], list(default.STREAM_PARTS))
        for stary in ("show_size", "show_file", "stream_layout_reset", "stream_layout_remote"):
            self.assertNotIn(stary, fields)
        # tlačítko Nastavit z mobilu v kategorii Výběr streamu = stránka jen s ní
        jen = default.remote_setup_schema("streamlist")
        self.assertEqual([s["id"] for s in jen], ["streamlist"])
        self.assertTrue(jen[0]["open"])
        self.assertEqual([f["id"] for f in jen[0]["fields"]], ["stream_layout"])
        storage = next(s for s in schema if s["id"] == "storage")
        headings = [f["label"] for f in storage["fields"] if f.get("type") == "heading"]
        self.assertEqual(headings, ["Úložiště 1", "Úložiště 2", "Úložiště 3"])

    def test_tlacitko_v_kategorii_otevre_jen_ji(self):
        with mock.patch.object(default, "remote_setup", return_value=None) as rs:
            default.router("action=remote_setup&section=streamlist")
            default.router("action=remote_setup&section=nesmysl")
            default.router("action=remote_setup")
        self.assertEqual([c[0] for c in rs.call_args_list], [("streamlist",), (None,), (None,)])

    def run_setup(self, submit):
        """Spustí remote_setup, `submit(url)` hraje roli mobilu."""
        started = []

        class Fake(remote_setup.SetupServer):
            def start(self, host="0.0.0.0", ports=None):
                port = super().start(host="127.0.0.1", ports=[0])
                started.append(self)
                threading.Thread(target=submit, args=(self.url("127.0.0.1"),), daemon=True).start()
                return port
        with mock.patch.object(remote_setup, "SetupServer", Fake), \
             mock.patch.object(default.xbmc, "getIPAddress", create=True, return_value="192.168.1.22"), \
             mock.patch.object(default, "REMOTE_SETUP_TIMEOUT", 5):
            result = default.remote_setup()
        return result, started

    def test_ulozi_zmeny_z_mobilu(self):
        xbmcaddon.settings.update(ws_username="stary", ws_password="tajne", ws_enabled="false", pref_lang="1")

        def mobil(url):
            with urllib.request.urlopen(url.replace("192.168.1.22", "127.0.0.1"), timeout=5) as resp:
                page = resp.read().decode()
            assert "tajne" not in page
            form = browser_form(page)
            form.update(ws_enabled="on", ws_username="novy")
            urllib.request.urlopen(urllib.request.Request(url, data=urllib.parse.urlencode(form).encode()),
                                   timeout=5).read()
        result, started = self.run_setup(mobil)
        self.assertEqual(result, 2)
        self.assertEqual(xbmcaddon.settings["ws_username"], "novy")
        self.assertEqual(xbmcaddon.settings["ws_enabled"], "true")
        self.assertEqual(xbmcaddon.settings["ws_password"], "tajne", "prázdné heslo zůstane")
        self.assertTrue(started[0].finished, "server po uložení skončí")
        self.assertEqual(len(xbmcgui.windows_shown), 1)
        self.assertFalse([n for n in os.listdir(default.PROFILE) if n.startswith("remote-setup-") and "bg" not in n],
                         "QR obrázek se po sobě uklidí")

    def test_zruseni_na_tv(self):
        def zpet(url):
            time.sleep(0.3)
            xbmcgui.windows_shown[-1].onAction(mock.Mock(getId=lambda: 92))
        result, started = self.run_setup(zpet)
        self.assertIsNone(result)
        self.assertTrue(started[0].finished)

    def test_adresa_na_androidu_klikatelna_jinde_jen_text(self):
        """2026-09-18: uživatel se na QR díval z mobilu a chtěl adresu otevřít rovnou, ne ji
        ručně přepisovat — na Androidu je z labelu tlačítko, klik/OK spustí prohlížeč přes
        StartAndroidActivity. Na CoreELEC/Linux (bez Androidu) ten builtin nic nedělá, takže
        tam adresa zůstává jen čitelný text bez zaostření."""
        xbmc.cond_visible.add("System.Platform.Android")
        try:
            def zpet(url):
                time.sleep(0.1)
                win = xbmcgui.windows_shown[-1]
                self.assertIsNotNone(win.link, "na Androidu je adresa ControlButton, ne jen label")
                self.assertIs(win.focused, win.link, "adresa má mít fokus rovnou")
                win.onControl(win.link)
                win.onAction(mock.Mock(getId=lambda: 92))
            result, _started = self.run_setup(zpet)
        finally:
            xbmc.cond_visible.discard("System.Platform.Android")
        self.assertIsNone(result)
        self.assertTrue(any("StartAndroidActivity" in b and "android.intent.action.VIEW" in b
                            for b in xbmc.builtins))

        xbmc.builtins.clear()
        xbmcgui.windows_shown.clear()

        def zpet_bez_androidu(url):
            time.sleep(0.1)
            win = xbmcgui.windows_shown[-1]
            self.assertIsNone(win.link, "bez Androidu zůstává obyčejný label")
            win.onAction(mock.Mock(getId=lambda: 92))
        self.run_setup(zpet_bez_androidu)
        self.assertFalse(any("StartAndroidActivity" in b for b in xbmc.builtins))

    def test_tuknuti_na_adresu_projde_i_pres_onAction(self):
        """5.2.21~beta1–4 (telefon uživatele): ťuknutí měnilo jen fokus tlačítka, `onControl`
        nikdy nepřišel. Klik se proto bere i z `onAction` (OK, levé tlačítko myši, ťuknutí),
        když má adresa fokus; obě cesty pro tentýž klik = jedno otevření; ťuknutí jinam
        (fokus mimo adresu) a pohyb myši nic neotevřou."""
        xbmc.cond_visible.add("System.Platform.Android")
        try:
            def tuknuti(url):
                time.sleep(0.1)
                win = xbmcgui.windows_shown[-1]
                win.onAction(mock.Mock(getId=lambda: 107))          # MOUSE_MOVE zaostří, nic víc
                self.assertFalse(xbmcgui.notifications)
                win.onAction(mock.Mock(getId=lambda: 401))          # TOUCH_TAP s fokusem na adrese
                win.onControl(win.link)                             # a řádná cesta pro tentýž klik
                win.onAction(mock.Mock(getId=lambda: 100))          # i leftclick z touch keymapy
                self.assertEqual(len(xbmcgui.notifications), 1, "jeden klik = jedno otevření")
                win._opened_at = 0
                win.focused = win.controls[3]                       # fokus na textboxu, ne na adrese
                win.onAction(mock.Mock(getId=lambda: 7))
                self.assertEqual(len(xbmcgui.notifications), 1, "OK mimo adresu nic neotevře")
                win.focused = None                                  # bez fokusu getFocusId vyhodí výjimku
                win.onAction(mock.Mock(getId=lambda: 100))
                self.assertEqual(len(xbmcgui.notifications), 1)
                win.focused = win.link
                win.onAction(mock.Mock(getId=lambda: 7))            # OK na ovladači
                self.assertEqual(len(xbmcgui.notifications), 2)
                win.onAction(mock.Mock(getId=lambda: 92))
            result, _started = self.run_setup(tuknuti)
        finally:
            xbmc.cond_visible.discard("System.Platform.Android")
        self.assertIsNone(result)
        self.assertEqual(sum("StartAndroidActivity" in b for b in xbmc.builtins), 2)
        self.assertEqual(xbmcgui.notifications[0][1], "Otvírám v prohlížeči…")

    def test_zpet_doruceny_jen_behem_cekani_kodi(self):
        """Na Office Zpět dialog nezavřelo: Kodi pouští `onAction` jen uvnitř volání svého API.
        Tady ho proto doručí až podstrčené `MONITOR.waitForAbort` — smyčka ho musí volat."""
        def doruc_zpet(timeout=0):
            if xbmcgui.windows_shown:
                xbmcgui.windows_shown[-1].onAction(mock.Mock(getId=lambda: 10))
            return False
        with mock.patch.object(default.MONITOR, "waitForAbort", side_effect=doruc_zpet) as cekani:
            start = time.time()
            result, started = self.run_setup(lambda url: None)
        self.assertIsNone(result)
        self.assertTrue(cekani.called)
        self.assertLess(time.time() - start, 3)
        self.assertTrue(started[0].finished)

    def test_bez_site(self):
        with mock.patch.object(default.xbmc, "getIPAddress", create=True, return_value=""):
            self.assertIsNone(default.remote_setup())
        self.assertTrue(xbmcgui.oks)

    def test_pruvodce_z_mobilu_a_navrat_po_zruseni(self):
        volby = iter([2, 2, -1])
        with mock.patch.object(xbmcgui.Dialog, "yesnocustom", side_effect=lambda *a, **k: next(volby)), \
             mock.patch.object(default, "remote_setup", side_effect=[None, 3]) as remote, \
             mock.patch.object(default, "_wizard_accounts") as ovladacem:
            default.setup_wizard(force=True)
        self.assertEqual(remote.call_count, 2, "zrušení vrátí na úvodní volbu")
        ovladacem.assert_not_called()
        self.assertTrue(default.STORE.load("wizard_done", False))


class FakeDash:
    """Dashboard bez sítě: menu katalogů, podobné tituly a TV program."""
    def __init__(self, menu=(), similar=(), tv=None):
        self._menu, self._similar, self._tv = list(menu), list(similar), tv
        self.tv_calls = []

    def menu(self, placement=None, ctype=None):
        return [e for e in self._menu if (placement is None or e["placement"] == placement)
                and (ctype is None or e["kind"] == ctype)]

    def group(self, slug):
        level = list(self._menu)
        for _ in range(3):
            match = next((e for e in level if e["slug"] == slug), None)
            if match is not None:
                return list(match.get("children") or [])
            level = [c for e in level for c in (e.get("children") or [])]
        return []

    def similar(self, ctype, imdb_id):
        return list(self._similar)

    def tv_program(self, day=None, kind=None, channel=None):
        self.tv_calls.append((day, kind, channel))
        return self._tv


class TestObsahZDashboardu(unittest.TestCase):
    MENU = [{"slug": "vanoce", "title": "Vánoční filmy", "kind": "movie", "placement": "root", "icon": "christmas"},
            {"slug": "sagy", "title": "Ságy", "kind": "series", "placement": "browse", "icon": ""}]

    def setUp(self):
        reset_kodi()

    def test_katalogy_v_hlavnim_menu_a_ve_filmech_serialech(self):
        default.main_menu({"dash": FakeDash(self.MENU), "cinemeta": object()})
        rows = [params_of(u) for u in xbmcplugin.urls()]
        self.assertIn({"action": "catalog", "type": "movie", "catalog": "vanoce", "src": "dash"}, rows)
        self.assertIn({"action": "tv"}, rows)
        ikona = next(li for _h, u, li, _f in xbmcplugin.items if "vanoce" in u).art["icon"]
        self.assertTrue(ikona.endswith("icon-vanoce.png"))
        xbmcplugin.reset()
        default.browse_menu({"dash": FakeDash(self.MENU)}, "series")
        self.assertIn("sagy", {params_of(u).get("catalog") for u in xbmcplugin.urls()})
        xbmcplugin.reset()
        default.browse_menu({"dash": FakeDash(self.MENU)}, "movie")
        self.assertNotIn("sagy", {params_of(u).get("catalog") for u in xbmcplugin.urls()})

    def test_slozka_s_podkategoriemi_vede_na_dalsi_vypis(self):
        menu = [{"slug": "vanoce", "title": "Vánoce", "kind": "movie", "placement": "root", "icon": "christmas",
                 "children": [{"slug": "komedie", "title": "Komedie", "kind": "movie", "placement": "root",
                               "icon": "", "children": []}]}]
        default.main_menu({"dash": FakeDash(menu), "cinemeta": object()})
        rows = [params_of(u) for u in xbmcplugin.urls()]
        self.assertIn({"action": "dash_group", "catalog": "vanoce", "type": "movie"}, rows)
        self.assertNotIn("catalog", [r.get("action") for r in rows])
        xbmcplugin.reset()
        default.list_dash_group({"dash": FakeDash(menu)}, "vanoce", "movie")
        self.assertEqual([params_of(u) for u in xbmcplugin.urls()],
                         [{"action": "catalog", "type": "movie", "catalog": "komedie", "src": "dash"}])

    def test_zmizela_slozka_skonci_jako_obycejny_katalog(self):
        """Server u složky vrátí slité položky potomků — lepší než prázdný výpis."""
        with mock.patch.object(default, "list_catalog") as vypis:
            default.list_dash_group({"dash": FakeDash(self.MENU)}, "vanoce", "movie")
        vypis.assert_called_once_with({"dash": mock.ANY}, "movie", "vanoce", "dash")

    def test_katalog_z_dashboardu_nema_dalsi_stranku(self):
        class Api:
            def catalog(self, *a, **k):
                return [{"id": f"tt00000{i:02d}", "name": f"Film {i}"} for i in range(30)]
        with mock.patch.object(default, "add_meta_item"):
            default.list_catalog({"dash": Api()}, "movie", "vanoce", "dash")
        self.assertEqual(xbmcplugin.urls(), [])

    def test_podobne_v_kontextu_jen_u_imdb_id(self):
        self.assertEqual(default.similar_context("movie", "sosac2_123"), [])
        label, cmd = default.similar_context("movie", "tt0133093")[0]
        self.assertTrue(cmd.startswith("ActivateWindow(Videos,"))
        self.assertEqual(params_of(cmd[len("ActivateWindow(Videos,"):-len(",return)")]),
                         {"action": "similar", "type": "movie", "id": "tt0133093"})

    def test_podobne_bez_tmdb_z_dashboardu(self):
        dash = FakeDash(similar=[{"id": "tt0234215", "name": "Matrix Reloaded", "type": "movie"}])
        default.list_similar({"tmdb": None, "dash": dash}, "movie", "tt0133093")
        self.assertEqual([params_of(u).get("id") for u in xbmcplugin.urls()], ["tt0234215"])

    def test_podobne_tmdb_chyba_spadne_na_dashboard(self):
        class Tmdb:
            def similar(self, *a):
                raise default.TmdbError("neplatný klíč")
        dash = FakeDash(similar=[{"id": "tt0234215", "name": "Matrix Reloaded", "type": "movie"}])
        default.list_similar({"tmdb": Tmdb(), "dash": dash}, "movie", "tt0133093")
        self.assertEqual(len(xbmcplugin.urls()), 1)

    def tv_data(self):
        now = int(time.time())
        return {"today": "2026-09-17", "date": "2026-09-18", "dates": ["2026-09-17", "2026-09-18"],
                "channels": [{"slug": "ct1", "name": "ČT1"}],
                "items": [{"channel": "ct1", "channel_name": "ČT1", "start": now + 3600, "stop": now + 9000,
                           "kind": "movie", "title": "Pelíšky", "episode_title": "", "season": None, "episode": None,
                           "meta": {"id": "tt0167331", "name": "Pelíšky", "year": "1999", "type": "movie"}},
                          {"channel": "ct1", "channel_name": "ČT1", "start": now + 9000, "stop": now + 12000,
                           "kind": "series", "title": "Komisařka Florence", "episode_title": "Útěk", "season": 8,
                           "episode": 5, "meta": {"id": "tt1234567", "name": "Komisařka Florence", "type": "series"}}]}

    def test_tv_program_volby_nahore_a_poradu_s_casem(self):
        default.list_tv({"dash": FakeDash(tv=self.tv_data())}, "2026-09-18", "", "ct1")
        items = xbmcplugin.items
        self.assertEqual([params_of(u).get("field") for _h, u, _li, _f in items[:3]], ["day", "channel", "kind"])
        self.assertTrue(all(not folder for _h, _u, _li, folder in items[:3]), "volby jsou ne-složky (handle −1)")
        self.assertIn("Zítra", items[0][2].getLabel())
        self.assertIn("ČT1", items[1][2].getLabel())
        self.assertIn("Pelíšky", items[3][2].getLabel())
        self.assertIn("8x05 Útěk", items[4][2].getLabel())
        self.assertEqual(params_of(items[4][1])["action"], "seasons")
        self.assertFalse(xbmcplugin.ended[-1]["cacheToDisc"])

    def test_tv_program_nedostupny(self):
        default.list_tv({"dash": FakeDash(tv=None)})
        self.assertEqual(xbmcplugin.urls(), [])
        self.assertTrue(xbmcplugin.ended[-1]["succeeded"])

    def test_tv_volba_dne_prepne_vypis(self):
        dash = FakeDash(tv=self.tv_data())
        with mock.patch.object(xbmcgui.Dialog, "select", return_value=0) as select:
            default.tv_pick({"dash": dash}, "day", "2026-09-18", "movie", "ct1")
        self.assertEqual(select.call_args.kwargs["preselect"], 1)
        cmd = xbmc.builtins[-1]
        self.assertTrue(cmd.endswith(",replace)"))
        self.assertEqual(params_of(cmd[len("Container.Update("):-len(",replace)")]),
                         {"action": "tv", "date": "2026-09-17", "kind": "movie", "channel": "ct1"})

    def test_tv_volba_zruseni_nic_neudela(self):
        with mock.patch.object(xbmcgui.Dialog, "select", return_value=-1):
            default.tv_pick({"dash": FakeDash(tv=self.tv_data())}, "channel")
        self.assertEqual(xbmc.builtins, [])


class FakeCztor:
    """Stačí na párování a stav účtu — síť ani tokeny nejsou potřeba."""

    def __init__(self, polls=(False, True), active=True):
        self.polls = list(polls)
        self.active = active
        self.logged_out = False
        self._paired = False

    def paired(self):
        return self._paired

    def start_pin(self):
        return {"pin": "434252", "poll_token": "P", "expires": time.time() + 600, "interval": 1,
                "url": "https://cztor.com/activate"}

    def poll_pin(self, token):
        self._paired = self.polls.pop(0)
        return self._paired

    def profile(self):
        return {"name": "Tester", "plan": "Basic", "active": self.active, "valid_until": "2026-10-13"}

    def logout(self):
        self.logged_out = True


class TestCztor(unittest.TestCase):
    def setUp(self):
        reset_kodi()
        default.STORE.save("cztor_session", {})

    def test_bez_prepinace_ani_parovani_neni_zdroj(self):
        self.assertIsNone(default.get_cztor())
        xbmcaddon.settings["cz_enabled"] = "true"
        self.assertIsNone(default.get_cztor(), "zapnutý, ale nespárovaný")
        default.STORE.save("cztor_session", {"device_id": "d", "access_token": "A", "refresh_token": "R",
                                             "expires": time.time() + 3600})
        self.assertIsNotNone(default.get_cztor())
        self.assertIsNotNone(default.KodiEngine().cz)
        self.assertIn("cztor", default.stats_sources())

    def test_parovani_pinem(self):
        fake = FakeCztor()
        with mock.patch.object(default, "cztor_client", return_value=fake), \
                mock.patch.object(default.MONITOR, "waitForAbort", return_value=False):
            default.router("action=cztor_pair")
        self.assertEqual(xbmcaddon.settings.get("cz_enabled"), "true")
        self.assertFalse(fake.polls, "čekalo se, dokud PIN nepotvrdil")
        self.assertIn("Basic", xbmcgui.oks[-1][1])

    def test_neaktivni_predplatne_se_ukaze(self):
        fake = FakeCztor(active=False)
        fake._paired = True
        with mock.patch.object(default, "cztor_client", return_value=fake):
            default.router("action=cztor_status")
        self.assertIn(default.L(30572, "Předplatné CZtor není aktivní."), xbmcgui.oks[-1][1])

    def test_odhlaseni(self):
        fake = FakeCztor()
        with mock.patch.object(default, "cztor_client", return_value=fake):
            default.router("action=cztor_logout")
        self.assertTrue(fake.logged_out)
        self.assertEqual(len(xbmcgui.notifications), 1)

    def test_stream_cztor_ma_barevny_stitek(self):
        self.assertIn("CZtor", default.SOURCE_TAGS["cz"])
        self.assertEqual(default.SOURCE_GROUP["cz"], "CZtor")



class TestTlacitkaZVypisu(unittest.TestCase):
    """Tlačítka s dialogem (Ověřit Lunu…) otevřená z výpisu mají handle ≥ 0 a musí zavřít adresář."""

    def test_luna_check_z_vypisu_zavre_adresar(self):
        xbmcplugin.ended.clear()
        with mock.patch.object(default, "HANDLE", 7), mock.patch.object(default, "luna_check"):
            default.router("action=luna_check")
        self.assertEqual(xbmcplugin.ended, [{"handle": 7, "succeeded": False, "cacheToDisc": False, "updateListing": False}])

    def test_luna_check_z_nastaveni_bez_handle_nic_nezavira(self):
        xbmcplugin.ended.clear()
        with mock.patch.object(default, "HANDLE", -1), mock.patch.object(default, "luna_check"):
            default.router("action=luna_check")
        self.assertEqual(xbmcplugin.ended, [])

    def test_vyjimka_adresar_stejne_zavre(self):
        xbmcplugin.ended.clear()
        with mock.patch.object(default, "HANDLE", 7):
            with self.assertRaises(RuntimeError):
                default._tlacitko(mock.Mock(side_effect=RuntimeError("x")))
        self.assertEqual(len(xbmcplugin.ended), 1)


if __name__ == "__main__":
    unittest.main()


class TestFrontaAZahrivani(unittest.TestCase):
    """Audit 2026-09-14: fronta stahování drží vnitřní odkaz a stabilní id, zahřívání cache
    skutečně obnovuje, žádný mrtvý řetězec."""

    def setUp(self):
        reset_kodi()
        self.tmp = tempfile.mkdtemp()
        xbmcaddon.settings["download_dir"] = self.tmp
        default.STORE.save("downloads", [])

    def test_fronta_nese_vnitrni_odkaz_a_stabilni_id(self):
        apis = {"ws": None, "hs": None, "luna": None, "sosac": None}
        with mock.patch.object(default, "load_meta", side_effect=LunaError("bez sítě")):
            default.download_stream(apis, "ws:abc", "Film.2020.1080p.mkv", "tt1", "movie")
            default.download_stream(apis, "ws:abc", "Film.2020.1080p.mkv", "tt1", "movie")
        fronta = default.STORE.downloads()
        self.assertEqual(len(fronta), 1, "tentýž soubor podruhé se nezařadí (dřív hash() per proces)")
        self.assertEqual(fronta[0]["url"], "ws:abc", "vnitřní odkaz, rozklíčuje ho až služba")
        import hashlib
        self.assertEqual(fronta[0]["id"], "dl:tt1:" + hashlib.sha1(b"ws:abc").hexdigest()[:8])
        self.assertTrue(fronta[0]["dest"].endswith("Film.2020.1080p.mkv"))

    def test_pripona_z_odkazu_jen_kdyz_nazev_nema(self):
        apis = {"ws": None}
        with mock.patch.object(default, "load_meta", side_effect=LunaError("x")), \
             mock.patch.object(default, "resolve_url", return_value="https://cdn/x.mp4?sig=1") as res:
            default.download_stream(apis, "streamuj:https://www.streamuj.tv/1", "Sosáč CZ - HD", "sosacd_1", "movie")
        res.assert_called_once()
        self.assertTrue(default.STORE.downloads()[-1]["dest"].endswith("Sosáč CZ - HD.mp4"))

    def test_popisek_smazat_jen_u_hotoveho_stazeni(self):
        """Kontextové menu u hotového stahování maže skutečný soubor z disku (viz
        download_remove), takže má psát „Smazat", ne „Odebrat ze seznamu" — ten
        zůstává u chyby/fronty, kde žádný soubor na disku není (nahlásil uživatel)."""
        default.STORE.save("downloads", [
            {"id": "dl:1", "name": "Hotovo.mkv", "status": "done", "dest": "/x/Hotovo.mkv", "size": 10},
            {"id": "dl:2", "name": "Chyba.mkv", "status": "error", "error": "timeout"},
            {"id": "dl:3", "name": "Fronta.mkv", "status": "queued"},
        ])
        xbmcplugin.items.clear()
        default.list_downloads()
        labels = {li.getLabel(): [c[0] for c in li.context] for _h, _u, li, _f in xbmcplugin.items}
        self.assertIn(default.L(30516, "Delete"), next(lbl for name, lbl in labels.items() if "Hotovo" in name))
        self.assertIn(default.L(30084), next(lbl for name, lbl in labels.items() if "Chyba" in name))
        self.assertIn(default.L(30083), next(lbl for name, lbl in labels.items() if "Fronta" in name))

    def test_sluzba_rozklicuje_az_pri_stahovani(self):
        self.assertEqual(service.resolve_internal("https://cdn/a.mkv", default.STORE), ("https://cdn/a.mkv", {}))
        xbmcaddon.settings.update(dav1_url="http://nas.lan/dav/", dav1_username="u", dav1_password="p")
        link, headers = service.resolve_internal("dav:1:Filmy/a b.mkv", default.STORE)
        self.assertEqual(link, "http://nas.lan/dav/Filmy/a%20b.mkv")
        self.assertTrue(headers.get("Authorization", "").startswith("Basic "))
        with self.assertRaises(Exception):
            service.resolve_internal("dav:9:x", default.STORE)

    def test_pripona_v_hranate_zavorce_se_nezdvoji(self):
        self.assertEqual(default.strip_inner_ext("The Son [Metoda.S01E04.1080p.mkv]"), "The Son [Metoda.S01E04.1080p]")
        self.assertEqual(default.strip_inner_ext("Matrix (1999)"), "Matrix (1999)")
        self.assertEqual(default.strip_inner_ext("a.mkv"), "a.mkv")
        base = default.strip_inner_ext("Křížová cesta [Rapl.S01E02.mkv]")
        self.assertEqual(base + default.guess_ext("https://cdn/x.mkv", base), "Křížová cesta [Rapl.S01E02].mkv")

    def test_sluzba_rozklicuje_cztor(self):
        # stahování z CZtor padalo na „unknown url type: cz“ — služba `cz:` neznala
        with mock.patch.object(service.CztorApi, "resolve", return_value="https://cdn.giganthost/a.mkv") as res:
            self.assertEqual(service.resolve_internal("cz:movie:1:2", default.STORE),
                             ("https://cdn.giganthost/a.mkv", {}))
        res.assert_called_once_with("cz:movie:1:2")

    def test_zahrivani_nastavi_priznak_a_api_cache_jen_zapisuji(self):
        videno = []
        with mock.patch.object(service, "rpc_directory",
                               side_effect=lambda url: videno.append(xbmcgui.Window(10000).getProperty(service.WARM_PROP))):
            xbmcaddon.settings["tmdb_api_key"] = "k"
            service.warm_caches(xbmc.Monitor(), "catalogs")
        self.assertTrue(videno and all(v == "1" for v in videno), "během zahřívání je příznak nastavený")
        self.assertEqual(xbmcgui.Window(10000).getProperty(service.WARM_PROP), "", "po zahřátí zmizí")
        self.assertFalse(default.get_sosac_db().fresh)
        xbmcgui.Window(10000).setProperty(default.WARM_PROP, "1")
        self.assertTrue(default.get_sosac_db().fresh)
        self.assertLess(service.WARM_EVERY, 3 * 3600, "pod TTL žebříčků Sosáče")

    def test_zadny_mrtvy_retezec(self):
        code = "".join((ROOT / n).read_text(encoding="utf-8") for n in ("default.py", "service.py"))
        code += (ROOT / "resources" / "settings.xml").read_text(encoding="utf-8")
        used = set(int(x) for x in re.findall(r"\b(3\d{4})\b", code))
        mrtve = sorted(set(po_ids("cs_cz")) - used)
        self.assertEqual(mrtve, [], f"řetězce bez použití: {mrtve}")
        self.assertIn(30402, used, "průběh měření rychlosti má vlastní řetězec, ne label tlačítka")


class TestUdrzbaKodi(unittest.TestCase):
    def setUp(self):
        reset_kodi()

    def test_kazdy_vypis_nabizi_razeni(self):
        src = (ROOT / "default.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("xbmcplugin.setContent(HANDLE, "), 1, "jen uvnitř set_content()")
        default.set_content("movies")
        self.assertEqual(xbmcplugin.contents, ["movies"])
        self.assertEqual(xbmcplugin.sort_methods[:2], [xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_LABEL_IGNORE_THE])
        self.assertIn(xbmcplugin.SORT_METHOD_VIDEO_YEAR, xbmcplugin.sort_methods)

    def test_razeni_nechava_nas_popisek_s_rokem(self):
        """Kodi bez výslovné masky dosadí u každé metody řazení `%T` a ve výpisu ukáže
        titul z info tagu místo popisku — katalog a hledání tak měly „Matrix“ bez roku,
        Můj seznam (snímek s titulem i rokem) „Matrix (1999)“ (2026-09-16). Každá metoda
        proto nese `%L`; rok/hodnocení zůstávají ve druhém sloupci."""
        default.set_content("movies")
        self.assertTrue(xbmcplugin.sort_masks)
        for method, label, _label2 in xbmcplugin.sort_masks:
            self.assertEqual(label, "%L", method)
        masks = dict((m, l2) for m, _l, l2 in xbmcplugin.sort_masks)
        self.assertEqual(masks[xbmcplugin.SORT_METHOD_VIDEO_YEAR], "%Y")
        self.assertEqual(masks[xbmcplugin.SORT_METHOD_VIDEO_RATING], "%R")

    def test_druhy_sloupec_u_titulu_je_vzdy_rok(self):
        """Bez masky dosadí Kodi do Label2 `%D` (stopáž) a Arctic Fuse ji kreslí vpravo místo
        roku — Můj seznam (snímek se stopáží) měl vpravo délku, žebříček (bez stopáže) rok
        (2026-09-16). U filmů a seriálů má být u výchozího řazení i podle názvu vždy rok."""
        for content in ("movies", "tvshows"):
            reset_kodi()
            default.set_content(content)
            masks = dict((m, l2) for m, _l, l2 in xbmcplugin.sort_masks)
            self.assertEqual(masks[xbmcplugin.SORT_METHOD_UNSORTED], "%Y", content)
            self.assertEqual(masks[xbmcplugin.SORT_METHOD_LABEL_IGNORE_THE], "%Y", content)

    def test_novinky_umi_beta_verzi(self):
        radky = default.parse_news("3.2.0~beta1 – nová věc\n3.1.12 – oprava\nnesmysl bez verze\n3.1.10 – starší")
        self.assertEqual([v for v, _t in radky], ["3.2.0~beta1", "3.1.12", "3.1.10"])
        self.assertEqual([v for v, _t in default.parse_news("3.2.0~beta1 – x\n3.1.12 – y", since="3.1.12")], ["3.2.0~beta1"])
        self.assertLess(default._vkey("3.2.0~beta1"), default._vkey("3.2.0"))
        self.assertEqual(default._vkey("3.1.12"), build_repo.version_key("3.1.12"))

    def test_zip_bez_balastu_a_build_hlida_novinky(self):
        for f in ("lists", "icon-vanoce.png", "tests"):
            self.assertIn(f, build_repo.EXCLUDE)
        # engine.py je importuje — v zipu chybět nesmí (dřív byly vyjmuté jako „jen HA")
        self.assertFalse({"prowlarr.py", "qbittorrent.py", "engine.py"} & build_repo.EXCLUDE)
        build_repo.check(ET.parse(ROOT / "addon.xml").getroot().get("version"))   # aktuální stav projde
        with self.assertRaises(SystemExit):
            build_repo.check("9.9.9")

    def test_repozitar_ma_verzovany_zip(self):
        """Kodi si při `<datadir zip="true">` skládá adresu zipu z id a verze v addons.xml —
        `repository.nokturno.beta.zip` bez verze v názvu pro něj neexistuje a instalace
        beta repozitáře z „Nokturno repozitáře" končila 404 (2026-09-15 až 2026-09-17).
        Holá kopie zůstává vedle: na ni odkazují návody na fóru."""
        repo = ROOT / "repo"
        for addon_id in ("repository.nokturno", "repository.nokturno.beta"):
            verze = ET.parse(ROOT / addon_id / "addon.xml").getroot().get("version")
            self.assertTrue((repo / addon_id / f"{addon_id}-{verze}.zip").exists(),
                            f"{addon_id}-{verze}.zip chybí — Kodi ho hledá přesně pod tímhle jménem")
            self.assertTrue((repo / addon_id / f"{addon_id}.zip").exists(), addon_id)
        # addons.xml musí tu verzi inzerovat, jinak Kodi sáhne po jiné adrese
        addons = (repo / "addons.xml").read_text(encoding="utf-8")
        for addon_id in ("repository.nokturno", "repository.nokturno.beta"):
            verze = ET.parse(ROOT / addon_id / "addon.xml").getroot().get("version")
            self.assertIn(f'<addon id="{addon_id}" name=', addons)
            self.assertIn(f'version="{verze}"', addons)

    def test_readme_bez_zastaralych_tvrzeni(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for zastarale in ("všechny čtyři", "userId", "Kodi (19", "en_GB, cs_CZ\n"):
            self.assertNotIn(zastarale, readme, zastarale)
        for lib in ("hellspy_api", "sledujteto_api", "fastshare_api", "storage_api", "mediainfo", "sk_SK", "tests/"):
            self.assertIn(lib, readme, lib)
        self.assertIn("github.com/matata86/plugin.video.nokturno/issues", (ROOT / "addon.xml").read_text(encoding="utf-8"))


class TestProrezavaniCacheKodi(unittest.TestCase):
    def test_zahrivani_promaze_prosle(self):
        import os
        import time
        reset_kodi()
        default.STORE.cached("stary", 10, lambda: {"x": 1})
        cache = pathlib.Path(_PROFILE) / "cache"
        for f in cache.glob("*.json"):
            os.utime(f, (time.time() - 5 * 86400,) * 2)
        with mock.patch.object(service, "rpc_directory"):
            service.warm_caches(xbmc.Monitor(), "all")
        self.assertEqual(list(cache.glob("*.json")), [])


class TestJazykRozhrani(unittest.TestCase):
    def test_zanry_cesky_jen_pro_cs_sk(self):
        self.assertEqual(default.genre_label("Action"), "Akční")   # stub hlásí jazyk cs
        with mock.patch.object(default, "GENRES_LOCAL", False):
            self.assertEqual(default.genre_label("Action"), "Action")

    def test_bez_cestiny_natvrdo(self):
        src = (ROOT / "default.py").read_text(encoding="utf-8")
        for natvrdo in ('bar.create("Nokturno"', '"Day": "Za den"', 'StorageError: "Úložiště"', '"dav": "Úložiště"',
                        'else "bez Premium'):
            self.assertNotIn(natvrdo, src, natvrdo)


class TestPreruseniPriKonciKodi(unittest.TestCase):
    """Kodi při `Application.Quit` čeká na doběhnutí skriptů doplňku (2026-09-16, Office:
    přes 2 minuty — zahřívání ze služby prohledávalo zdroje po titulech). Jádro dostane
    `should_stop` (`lib/abort.py`), doplněk mu podstrčí `Monitor.abortRequested()` a
    vlastní příznak zrušení dialogem; `Aborted` pak projde až do routeru."""

    def setUp(self):
        reset_kodi()
        default.CANCEL.clear()
        default._quit_checked[0] = 0.0
        service.QUITTING.clear()
        default.STORE.clear_cache()
        default.STORE.save("wizard_done", True)

    def tearDown(self):
        default.CANCEL.clear()
        service.QUITTING.clear()
        default._quit_checked[0] = 0.0

    def test_quit_ze_sluzby_zastavi_plugin(self):
        """Kodi posílá pluginu spuštěnému přes JSON-RPC stop až po zastavení síťových
        služeb (a ty čekají na něj) — služba proto na `System.OnQuit` nastaví vlastnost
        okna a plugin se podle ní zastaví, i když jeho Monitor pořád hlásí False."""
        self.assertFalse(default.should_stop())
        default._quit_checked[0] = 0.0
        monitor = service.ServiceMonitor()
        monitor.onNotification("xbmc", "Player.OnPlay", "{}")
        self.assertFalse(monitor.abortRequested())
        monitor.onNotification("xbmc", "System.OnQuit", '{"exitcode": 0}')
        self.assertTrue(monitor.abortRequested())
        self.assertTrue(monitor.waitForAbort(60), "čekání skončí hned, ne až za minutu")
        self.assertTrue(xbmcgui.Window(10000).getProperty(service.QUIT_PROP))
        self.assertEqual(default.QUIT_PROP, service.QUIT_PROP)
        self.assertFalse(xbmc.Monitor().abortRequested(), "Monitor pluginu o konci neví")
        self.assertTrue(default.should_stop())

    def test_sluzba_pri_startu_smaze_stary_priznak(self):
        xbmcgui.Window(10000).setProperty(service.QUIT_PROP, "1")
        with mock.patch.object(service, "ServiceMonitor", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                service.main()
        self.assertEqual(xbmcgui.Window(10000).getProperty(service.QUIT_PROP), "")

    def test_should_stop_cte_okno_jen_obcas(self):
        reads = []
        real = xbmcgui.Window.getProperty

        def get(win, key):
            reads.append(key)
            return real(win, key)
        with mock.patch.object(xbmcgui.Window, "getProperty", get):
            for _ in range(50):
                default.should_stop()
        self.assertLessEqual(len(reads), 2)

    def test_should_stop_sleduje_monitor_i_zruseni(self):
        self.assertFalse(default.should_stop())
        xbmc.abort = True
        self.assertTrue(default.should_stop(), "Kodi končí")
        xbmc.abort = False
        default.CANCEL.set()
        self.assertTrue(default.should_stop(), "uživatel zrušil dialogem")

    def test_jadro_i_klienti_dostanou_should_stop(self):
        xbmcaddon.settings.update(dav1_url="http://nas.lan/dav/", dav1_username="u", dav1_password="p",
                                  streamuj_username="u", streamuj_password="p")
        engine = default.KodiEngine()
        self.assertIs(engine.should_stop, default.should_stop)
        self.assertIs(default.get_storages()[0].should_stop, default.should_stop)
        self.assertIs(default.get_sosac_db().should_stop, default.should_stop)
        self.assertIs(default.get_sosac().should_stop, default.should_stop)

    def test_lang_catalog_se_prerusi_a_nezapise_do_cache(self):
        """Přepočet po titulech: po požadavku na ukončení vyhodí `Aborted` (ne `break` —
        nedokončený seznam by se jinak zapsal na 8 h do cache) a router zavře handle
        bez chybové hlášky. Zámek přes okno se uklidí."""
        engine = default.KodiEngine()
        cand = [{"id": f"sosacd_m_{i}", "type": "movie", "name": f"Film {i}", "year": "2026"} for i in range(10)]
        calls = []

        def raw_streams(ctype, item_id, **kw):
            calls.append(item_id)
            xbmc.abort = len(calls) >= 2   # během první dávky přijde Application.Quit
            return [{"langs": ["CZ"], "subs": []}]
        engine.raw_streams = raw_streams
        apis = {"engine": engine, "sosac_db": FakeSosacDb(cand), "luna": None}
        with mock.patch.object(default, "get_apis", return_value=apis):
            default.router("action=lang_catalog&type=movie&want=dub")
        self.assertEqual(len(calls), default.LANG_CATALOG_WORKERS, "další dávka se už neprozkoumala")
        self.assertEqual(len(xbmcplugin.ended), 1)
        self.assertFalse(xbmcplugin.ended[0]["succeeded"])
        self.assertEqual(xbmcgui.notifications, [], "přerušení není chyba, žádná hláška")
        self.assertIsNone(default.STORE.peek_cached("lang_catalog:movie", float("inf")),
                          "nedokončený seznam se nesmí zapsat do cache")
        self.assertEqual(xbmcgui.Window(10000).getProperty(f"{default.LANG_LOCK_PROP}:lang_catalog:movie"), "",
                         "zámek se uklidí")
        self.assertTrue(any("přerušeno" in m for m, _l in xbmc.logged))

    def test_router_zavre_prehrani_bez_hlasky(self):
        with mock.patch.object(default, "get_apis", return_value={}), \
                mock.patch.object(default, "play", side_effect=default.Aborted()):
            default.router("action=play&type=movie&id=tt1")
        self.assertEqual([r[1] for r in xbmcplugin.resolved], [False])
        self.assertEqual(xbmcgui.notifications, [])

    def test_konec_pluginu_zavre_pool_popisu(self):
        """Nečinná vlákna `enrich` by Kodi drželo po doběhnutí pluginu i při vypínání."""
        with mock.patch.object(default, "router") as router, \
                mock.patch.object(default, "release_enrich") as release:
            default.main("action=favourites")
            router.assert_called_once_with("action=favourites")
            release.assert_called_once_with(cancel=False)
            release.reset_mock()
            router.side_effect = RuntimeError("pád")
            default.CANCEL.set()
            with self.assertRaises(RuntimeError):
                default.main("action=x")
            release.assert_called_once_with(cancel=True)

    def test_router_zavre_prehrani_titulu_jako_prehrani(self):
        """`action=title` s handle ≥ 0 je Přehrát v detailu — Kodi čeká `setResolvedUrl`,
        ne `endOfDirectory`, a to i po přerušení."""
        with mock.patch.object(default, "HANDLE", 5), mock.patch.object(default, "get_apis", return_value={}), \
                mock.patch.object(default, "play", side_effect=default.Aborted()):
            default.router("action=title&type=movie&id=tt1")
        self.assertEqual([r[1] for r in xbmcplugin.resolved], [False])
        self.assertEqual(xbmcplugin.ended, [])

    def test_prefetch_se_zastavi(self):
        xbmc.abort = True
        snap = {"series": "tt1", "season": 1, "episode": 1, "name": "x"}
        with mock.patch.object(default.STORE, "recently_watched", return_value=[("tt1:1:1", {})]), \
                mock.patch.object(default.STORE, "item", return_value=snap), \
                mock.patch.object(default, "next_episode", side_effect=AssertionError("nemá se hledat")):
            default.prefetch({"engine": default.KodiEngine()}, "next")
        self.assertEqual(len(xbmcplugin.ended), 1)

    def test_prefetch_zavira_adresár_uspesne(self):
        """Služba volá prefetch přes `Files.GetDirectory`; `succeeded=False` by Kodi
        v každém kole zahřívání zapsalo `GetDirectory - Error getting plugin://…`
        do `kodi.log`, který uživatelé posílají na dashboard."""
        with mock.patch.object(default.STORE, "recently_watched", return_value=[]):
            default.prefetch({"engine": default.KodiEngine()}, "next")
        self.assertEqual(len(xbmcplugin.ended), 1)
        self.assertTrue(xbmcplugin.ended[0]["succeeded"])
        self.assertFalse(xbmcplugin.ended[0]["cacheToDisc"])
        self.assertEqual(xbmcplugin.items, [], "prefetch nic nevypisuje")

    def test_sluzba_neprednacita_kdyz_kodi_konci(self):
        """`prefetch_next_later` čeká 15 s přes `waitForAbort` (stub vrátí True = konec) — pak nic."""
        with mock.patch.object(service, "warm_caches", side_effect=AssertionError("nemá zahřívat")):
            service.prefetch_next_later()
            time.sleep(0.2)


class TestAudit614Beta2(unittest.TestCase):
    """Kodi 6.1.4~beta2 — položky z auditu 2026-09-19 (`AUDIT-2026-09-19.md`, § Kodi)."""

    def setUp(self):
        reset_kodi()

    def tearDown(self):
        xbmc.cond_visible.clear()
        xbmc.info_labels.clear()
        xbmc.abort = False

    # 1. log z nastavení bez tajemství
    def test_log_send_cisti_tajemstvi(self):
        import gzip
        log = tempfile.NamedTemporaryFile("wb", suffix=".log", delete=False)
        log.write("2026-09-19 info <general>: token=abcdef123456 https://user:pw@example.com/x?wst=XYZ "
                  "jan.novak@example.com 192.168.1.21 Authorization: Bearer secret\nbežný řádek\n".encode("utf-8"))
        log.close()
        self.addCleanup(os.unlink, log.name)
        sent = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b""

        def urlopen(req, timeout=None):
            sent["body"] = gzip.decompress(req.data).decode("utf-8")
            return Resp()

        with mock.patch.object(default.xbmcvfs, "translatePath", return_value=log.name), \
                mock.patch.object(default.urllib.request, "urlopen", urlopen):
            default.log_send(ask=False)
        body = sent["body"]
        for secret in ("abcdef123456", "user:pw", "wst=XYZ", "jan.novak@", "192.168.1.21", "Bearer secret"):
            self.assertNotIn(secret, body, body)
        self.assertIn("bežný řádek", body)
        self.assertEqual(xbmcgui.notifications[-1][2], xbmcgui.NOTIFICATION_INFO)

    # 2a. hledání: modál jen po kliku ve výpisu Nokturna
    @staticmethod
    def _luna_pada(apis, ctype, query, want_year, errors):
        errors.append(LunaError("HTTP 503"))
        return [], False

    def test_chyba_zdroje_pri_hledani_bez_modalu_mimo_vypis(self):
        with mock.patch.object(default, "search_source", side_effect=self._luna_pada):
            default.search_run({"ws": None, "luna": object()}, "movie", "matrix")
        self.assertEqual(xbmcgui.oks, [], "widget/JSON-RPC nesmí dostat modál")
        self.assertEqual(xbmcgui.notifications[-1][2], xbmcgui.NOTIFICATION_WARNING)
        self.assertTrue(any("Zdroj neodpověděl" in it[2].label for it in xbmcplugin.items),
                        "chyba má být vidět i jako položka ve výpisu")
        self.assertTrue(xbmcplugin.ended)

    def test_chyba_zdroje_pri_hledani_modal_ve_vypisu_nokturna(self):
        xbmc.cond_visible.add("Window.IsMedia")
        xbmc.info_labels["Container.PluginName"] = "plugin.video.nokturno"

        with mock.patch.object(default, "search_source", side_effect=self._luna_pada):
            default.search_run({"ws": None, "luna": object()}, "movie", "matrix")
        self.assertEqual(len(xbmcgui.oks), 1)

    # 2b. přehrání bez streamu: dialog „hledat pod jiným názvem“ jen z výpisu Nokturna
    def test_prehrani_bez_streamu_mimo_vypis_nespousti_dialog(self):
        with mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)), \
                mock.patch.object(default, "collect_streams", return_value=[]):
            default.play({}, "movie", "tt1")
        self.assertEqual([r[1] for r in xbmcplugin.resolved], [False])
        self.assertFalse([b for b in xbmc.builtins if "fulltext=1" in b], xbmc.builtins)

    def test_prehrani_bez_streamu_z_vypisu_nabidne_jiny_nazev(self):
        xbmc.cond_visible.add("Window.IsMedia")
        xbmc.info_labels["Container.PluginName"] = "plugin.video.nokturno"
        with mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)), \
                mock.patch.object(default, "collect_streams", return_value=[]):
            default.play({}, "movie", "tt1")
        self.assertTrue([b for b in xbmc.builtins if "fulltext=1" in b])

    # 2c. zpráva z dashboardu ve vlákně, ne při přehrávání a ne při vypínání
    def test_zprava_z_dashboardu_neblokuje_smycku_sluzby(self):
        stats = FakeStats()
        stats.last_message = {"id": 9, "text": "ahoj"}
        started = threading.Event()

        def textviewer(self, heading, text, usemono=False):
            started.set()
            time.sleep(0.3)
            xbmcgui.textviewers.append((heading, text))

        with mock.patch.object(xbmcgui.Dialog, "textviewer", textviewer):
            t0 = time.monotonic()
            service._show_pending_message(stats)
            self.assertLess(time.monotonic() - t0, 0.2, "smyčka služby nesmí čekat na zavření zprávy")
            self.assertTrue(started.wait(2))
            self.assertEqual(stats.seen, [], "přečteno až po zavření")
            wait_message_thread()
        self.assertEqual(stats.seen, [9])

    def test_zprava_z_dashboardu_pocka_na_konec_prehravani(self):
        stats = FakeStats()
        stats.last_message = {"id": 9, "text": "ahoj"}
        with mock.patch.object(xbmc.Player, "isPlaying", return_value=True):
            service._show_pending_message(stats)
            wait_message_thread()
        self.assertEqual(xbmcgui.textviewers, [])
        self.assertEqual(stats.seen, [], "neukázaná zpráva se nesmí označit jako přečtená")
        service.QUITTING.set()
        try:
            service._show_pending_message(stats)
            wait_message_thread()
        finally:
            service.QUITTING.clear()
        self.assertEqual(xbmcgui.textviewers, [])

    # 3. cache jen při změně formátu, ne při každé verzi
    def test_cache_se_maze_jen_pri_zmene_formatu(self):
        default.STORE.save("cache_version", default.CACHE_FORMAT)
        default.STORE.save("seen_version", "0.0.0")
        with mock.patch.object(default.STORE, "clear_cache") as clear:
            default.migrate_on_start()
        clear.assert_not_called()
        self.assertEqual(xbmcgui.Window(10000).getProperty(default.FORCE_STATS_PROP), "1",
                         "nová verze má i tak popohnat statistiky (zpráva z dashboardu)")
        self.assertEqual(default.STORE.load("seen_version", ""), default._ADDON_VERSION)
        default.STORE.save("cache_version", "stary-tvar")
        with mock.patch.object(default.STORE, "clear_cache") as clear:
            default.migrate_on_start()
        clear.assert_called_once()
        self.assertEqual(default.STORE.load("cache_version", ""), default.CACHE_FORMAT)

    # 4. čtení MyVideos*.db jen před výpisem titulů
    def test_znacky_kodi_se_ctou_jen_pred_vypisem_titulu(self):
        with mock.patch.object(default, "router"), mock.patch.object(default, "adopt_kodi_marks") as adopt:
            for q in ("?action=play&type=movie&id=tt1", "?action=prefetch&kind=next", "?action=log_send",
                      "?action=title&type=movie&id=tt1", "?action=settings"):
                default.main(q)
            adopt.assert_not_called()
            for q in ("", "?action=continue", "?action=episodes&id=tt1&season=1", "?action=favourites",
                      "?action=recent", "?action=catalog&type=movie&cat=x", "?action=search_run&type=any&q=m",
                      "?action=toggle_watched&id=tt1"):
                default.main(q)
            self.assertEqual(adopt.call_count, 8)

    # 5. zahřívání dalšího dílu jen u čerstvě sledovaných seriálů
    def test_prefetch_jen_cerstve_serialy_a_nejvys_pet(self):
        now = int(time.time())
        rows = [(f"tt{i}:1:1", {"playcount": 1, "ts": now - i * 3600}) for i in range(1, 9)]
        rows.append(("tt99:1:1", {"playcount": 1, "ts": now - 20 * 86400}))
        snaps = {f"tt{i}:1:1": {"series": f"tt{i}", "season": 1, "episode": 1, "name": "x"} for i in range(1, 9)}
        snaps["tt99:1:1"] = {"series": "tt99", "season": 1, "episode": 1, "name": "starý"}
        looked = []

        def next_episode(apis, snap):
            looked.append(snap["series"])
            return None

        with mock.patch.object(default.STORE, "recently_watched", return_value=rows), \
                mock.patch.object(default.STORE, "item", side_effect=lambda k: snaps.get(k)), \
                mock.patch.object(default, "next_episode", next_episode):
            default.prefetch({"engine": default.KodiEngine()}, "next")
        self.assertEqual(looked, ["tt1", "tt2", "tt3", "tt4", "tt5"])
        self.assertNotIn("tt99", looked)

    def test_prefetch_stary_serial_se_preskoci_i_kdyz_je_prvni(self):
        now = int(time.time())
        rows = [("tt99:1:1", {"playcount": 1, "ts": now - 20 * 86400})]
        with mock.patch.object(default.STORE, "recently_watched", return_value=rows), \
                mock.patch.object(default.STORE, "item", return_value={"series": "tt99", "season": 1, "episode": 1}), \
                mock.patch.object(default, "next_episode", side_effect=AssertionError("nemá se hledat")):
            default.prefetch({"engine": default.KodiEngine()}, "next")
        self.assertEqual(len(xbmcplugin.ended), 1)


class TestNavazovaniStahovani(unittest.TestCase):
    """Nález 24 z auditu: `.part` se mazal při každé chybě, takže se 20GB film po
    restartu Kodi nebo výpadku sítě stahoval znovu od nuly."""

    class FakeMonitor:
        def __init__(self, stop_after=None):
            self.stop_after, self.volani = stop_after, 0

        def abortRequested(self):
            self.volani += 1
            return self.stop_after is not None and self.volani > self.stop_after

        def waitForAbort(self, _s):
            return True

    class FakeResp:
        def __init__(self, data, code=200, length=None):
            self._data, self._code = data, length if length is not None else len(data)
            self.headers = {"Content-Length": str(self._code)}
            self._kod = code
            self._pos = 0

        def getcode(self):
            return self._kod

        def read(self, n):
            kus = self._data[self._pos:self._pos + n]
            self._pos += len(kus)
            return kus

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def setUp(self):
        reset_kodi()
        self.dir = tempfile.mkdtemp()
        self.dest = os.path.join(self.dir, "film.mkv")
        for d in default.STORE.downloads():
            default.STORE.remove_download(d["id"])

    def _job(self, **kw):
        job = {"id": "d1", "url": "https://cdn/film.mkv", "dest": self.dest, "name": "Film", **kw}
        default.STORE.add_download(dict(job))
        return job

    def test_part_size_pozna_na_co_navazat(self):
        tmp = self.dest + ".part"
        self.assertEqual(service.Downloader.part_size(tmp, 100), 0, "co není, na to se nenaváže")
        with open(tmp, "wb") as f:
            f.write(b"x" * 40)
        self.assertEqual(service.Downloader.part_size(tmp, 100), 40)
        self.assertEqual(service.Downloader.part_size(tmp, 40), 0, "hotový nebo delší = radši znovu")
        self.assertEqual(service.Downloader.part_size(tmp, 10), 0)

    def test_navaze_na_rozdelany_soubor(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(b"A" * 40)
        job = self._job(size=100)
        dl = service.Downloader(default.STORE, self.FakeMonitor())
        pozadavky = []

        def urlopen(req, timeout=None):
            pozadavky.append(req.headers)
            return self.FakeResp(b"B" * 60, code=206)

        with mock.patch.object(service, "resolve_internal", return_value=("https://cdn/film.mkv", {})), \
                mock.patch.object(service.urllib.request, "urlopen", urlopen):
            dl.download(job)
        self.assertEqual(pozadavky[0].get("Range"), "bytes=40-")
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), b"A" * 40 + b"B" * 60, "navázaný soubor musí být celý")
        hotovo = next(d for d in default.STORE.downloads() if d["id"] == "d1")
        self.assertEqual((hotovo["status"], hotovo["size"]), ("done", 100))

    def test_zdroj_bez_navazani_stahuje_od_zacatku(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(b"A" * 40)
        job = self._job(size=100)
        dl = service.Downloader(default.STORE, self.FakeMonitor())
        with mock.patch.object(service, "resolve_internal", return_value=("https://cdn/film.mkv", {})), \
                mock.patch.object(service.urllib.request, "urlopen",
                                  lambda req, timeout=None: self.FakeResp(b"C" * 100, code=200)):
            dl.download(job)
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), b"C" * 100, "server Range neumí — přepsat, ne slepit")

    def test_chyba_site_nechá_rozdelany_soubor_lezet(self):
        job = self._job()
        dl = service.Downloader(default.STORE, self.FakeMonitor())

        class Padne(self.FakeResp):
            def read(self, n):
                if self._pos:
                    raise OSError("spojení spadlo")
                return super().read(n)

        with mock.patch.object(service, "resolve_internal", return_value=("https://cdn/film.mkv", {})), \
                mock.patch.object(service.urllib.request, "urlopen",
                                  lambda req, timeout=None: Padne(b"D" * 200, length=200)):
            dl.download(job)
        self.assertEqual(os.path.getsize(self.dest + ".part"), 200, "stažené zůstává pro navázání")
        self.assertEqual(next(d for d in default.STORE.downloads() if d["id"] == "d1")["status"], "error")

    def test_zruseni_rozdelany_soubor_smaze(self):
        """`download()` si stav přepíše na „running", takže zrušení musí přijít až za běhu —
        přesně jako když uživatel klikne na Zrušit v seznamu stahování."""
        job = self._job()
        dl = service.Downloader(default.STORE, self.FakeMonitor())

        class Zrusi(self.FakeResp):
            def read(self, n):
                default.STORE.update_download("d1", status="cancel")
                return super().read(n)

        with mock.patch.object(service, "resolve_internal", return_value=("https://cdn/film.mkv", {})), \
                mock.patch.object(service.urllib.request, "urlopen",
                                  lambda req, timeout=None: Zrusi(b"E" * 200, length=200)):
            dl.download(job)
        self.assertFalse(os.path.exists(self.dest + ".part"), "zrušené stahování po sobě uklidí")
        self.assertEqual([d["id"] for d in default.STORE.downloads()], [])

    def test_po_restartu_se_bezici_vrati_do_fronty(self):
        self._job(status="running")
        default.STORE.update_download("d1", status="running")
        service.Downloader(default.STORE, self.FakeMonitor()).requeue_running()
        self.assertEqual(next(d for d in default.STORE.downloads() if d["id"] == "d1")["status"], "queued")

    def test_smazani_z_fronty_uklidi_rozdelany_soubor(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(b"x" * 10)
        self._job(status="error")
        default.STORE.update_download("d1", status="error")
        default.download_remove("d1")
        self.assertFalse(os.path.exists(self.dest + ".part"))


class TestZahrivaniJazykovehoKatalogu(unittest.TestCase):
    """Nálezy 12 a 29 z auditu 2026-09-19: „Nově přidané s CZ dabingem/titulky".

    Živé ověřování jazyka napříč zdroji je nejdražší práce, kterou doplněk dělá sám
    od sebe — až 60 kandidátů krát všechny zapnuté zdroje, každých 6 h. Právě tudy
    se doplněk dostal k blokaci HellSpy (6.0.4)."""

    def setUp(self):
        reset_kodi()
        default.STORE.save(default.LANG_SEEN_KEY, {})

    def tearDown(self):
        default.STORE.save(default.LANG_SEEN_KEY, {})

    def test_bez_otevreni_se_nezahriva(self):
        self.assertEqual(service.lang_warm_urls(), [], "kdo seznam nikdy neotevřel, nemá co zahřívat")

    def test_po_otevreni_se_zahriva_jen_otevreny_typ(self):
        default.note_lang_catalog_open("series")
        urls = service.lang_warm_urls()
        self.assertEqual(len(urls), 1)
        self.assertIn("type=series", urls[0])
        self.assertIn("want=dub", urls[0], "dabing i titulky se počítají jedním průchodem")

    def test_po_lhute_se_prestane_zahrivat(self):
        default.STORE.save(default.LANG_SEEN_KEY,
                           {"movie": int(time.time()) - (default.LANG_SEEN_DAYS + 1) * 86400})
        self.assertEqual(service.lang_warm_urls(), [])

    def test_obe_vstupni_cesty_si_otevreni_zapamatuji(self):
        for akce, typ in (("lang_catalog_menu", "movie"), ("lang_catalog_trigger", "series")):
            with mock.patch.object(default, "get_apis", return_value={}), \
                    mock.patch.object(default, "list_lang_catalog"):
                default.router(f"?action={akce}&type={typ}&want=dub")
        self.assertEqual(set(default.STORE.load(default.LANG_SEEN_KEY, {})), {"movie", "series"})

    def test_zahrivani_prepocita_az_kdyz_cache_nevydrzi_do_dalsiho_kola(self):
        """Nález 29: zahřívání běží po 6 h, cache platí 8 h. Bez přepočtu kolo v 6. hodině
        jen přečetlo platnou cache — a mezi 8. a 12. hodinou menu hlásilo „Data nejsou
        připravená"."""
        self.assertEqual(default.LANG_REFRESH_AFTER, default.LANG_CATALOG_TTL - 6 * 3600)
        key = "lang_catalog:movie"
        volani = []

        def cached_if(k, ttl, loader, ok=bool, fresh=False):
            volani.append(fresh)
            return {"dub": [], "subs": []}

        xbmcgui.Window(10000).setProperty(default.WARM_PROP, "1")   # běží zahřívání
        try:
            with mock.patch.object(default.STORE, "cached_if", cached_if), \
                    mock.patch.object(default.STORE, "peek_cached", return_value={"dub": []}):
                default._lang_catalog_locked({}, "movie", key)      # cache je čerstvá
            with mock.patch.object(default.STORE, "cached_if", cached_if), \
                    mock.patch.object(default.STORE, "peek_cached", return_value=None):
                default._lang_catalog_locked({}, "movie", key)      # cache stárne
        finally:
            xbmcgui.Window(10000).clearProperty(default.WARM_PROP)
        self.assertEqual(volani, [False, True])

    def test_mimo_zahrivani_se_nikdy_neprepocitava_nasilim(self):
        """Uživatelovo otevření nesmí platit přepočet, když cache existuje."""
        volani = []

        def cached_if(k, ttl, loader, ok=bool, fresh=False):
            volani.append(fresh)
            return {"dub": [], "subs": []}

        with mock.patch.object(default.STORE, "cached_if", cached_if), \
                mock.patch.object(default.STORE, "peek_cached", return_value=None):
            default._lang_catalog_locked({}, "movie", "lang_catalog:movie")
        self.assertEqual(volani, [False])


class TestLunaDiagnostika(unittest.TestCase):
    """Tlačítka „Najít Lunu v síti“ a „Ověřit nastavení Luny“.

    Luna se instaluje mimo doplněk a lidé o ní hlásí jen „nefunguje mi to“ —
    smysl obou tlačítek je, aby tu větu doplněk nahradil konkrétní příčinou.
    """

    def setUp(self):
        reset_kodi()
        xbmcaddon.settings.clear()

    def diag(self, **kw):
        out = {"level": "fail", "code": "unreachable", "base": "http://192.168.1.10:7126",
               "token": "", "version": "", "detail": ""}
        out.update(kw)
        return out

    def test_vse_v_poradku_jen_oznami_verzi(self):
        xbmcaddon.settings.update({"luna_url": "http://192.168.1.10:7126", "token": "e1.abc"})
        with mock.patch.object(default, "luna_diagnose",
                               return_value=self.diag(level="ok", code="ok", version="1.7.0", token="e1.abc")):
            default.luna_check()
        self.assertIn("1.7.0", xbmcgui.oks[-1][1])

    def test_nedostupna_luna_pojmenuje_adresu(self):
        xbmcaddon.settings.update({"luna_url": "192.168.1.99", "token": "e1.abc"})
        with mock.patch.object(default, "luna_diagnose",
                               return_value=self.diag(base="http://192.168.1.99:7126")), \
                mock.patch.object(xbmcgui.Dialog, "yesnocustom", return_value=0) as dialog:
            default.luna_check()
        self.assertIn("192.168.1.99:7126", dialog.call_args[0][1])

    def test_chyba_nabidne_poslani_logu(self):
        xbmcaddon.settings["token"] = "e1.abc"
        with mock.patch.object(default, "luna_diagnose", return_value=self.diag(code="bad_token", version="1.7.0")), \
                mock.patch.object(xbmcgui.Dialog, "yesnocustom", return_value=2), \
                mock.patch.object(default, "log_send") as log_send:
            default.luna_check()
        log_send.assert_called_once_with(ask=False)   # ptát se podruhé „opravdu?“ nemá smysl

    def test_uspech_o_log_nezada(self):
        with mock.patch.object(default, "luna_diagnose", return_value=self.diag(level="ok", code="ok", version="1.7")), \
                mock.patch.object(default, "log_send", side_effect=AssertionError("nemá se posílat")):
            default.luna_check()

    def test_cela_instalacni_adresa_v_tokenu_se_rozdeli(self):
        """Nejčastější vložení ze /setup Luny — adresa i token v jednom poli."""
        xbmcaddon.settings["token"] = "http://192.168.1.10:7126/metadata/e1.abc/manifest.json"
        with mock.patch.object(default, "luna_diagnose",
                               return_value=self.diag(level="ok", code="ok", version="1.7.0",
                                                      base="http://192.168.1.10:7126", token="e1.abc")):
            default.luna_check()
        self.assertEqual(xbmcaddon.settings["luna_url"], "http://192.168.1.10:7126")
        self.assertEqual(xbmcaddon.settings["token"], "e1.abc")

    def test_diagnostika_nikdy_nespadne(self):
        with mock.patch.object(default, "luna_diagnose", side_effect=OSError("síť spadla")), \
                mock.patch.object(xbmcgui.Dialog, "yesnocustom", return_value=0):
            default.luna_check()   # nesmí vyhodit ven

    def test_nalezeny_server_se_ulozi_a_zapne(self):
        found = [{"url": "http://192.168.1.10:7126", "version": "1.7.0", "name": "Luna: Absolute Cinema"}]
        with mock.patch.object(default, "luna_discover", return_value=found), \
                mock.patch.object(default, "luna_check") as check:
            default.luna_find()
        self.assertEqual(xbmcaddon.settings["luna_url"], "http://192.168.1.10:7126")
        self.assertEqual(xbmcaddon.settings["luna_enabled"], "true")
        check.assert_called_once()   # samotná adresa bez tokenu ještě nic nepřehraje

    def test_vic_serveru_necha_vybrat(self):
        found = [{"url": "http://192.168.1.10:7126", "version": "1.7.0", "name": "Luna"},
                 {"url": "http://192.168.1.20:7126", "version": "1.6.0", "name": "Luna"}]
        with mock.patch.object(default, "luna_discover", return_value=found), \
                mock.patch.object(xbmcgui.Dialog, "select", return_value=1), \
                mock.patch.object(default, "luna_check"):
            default.luna_find()
        self.assertEqual(xbmcaddon.settings["luna_url"], "http://192.168.1.20:7126")

    def test_zruseny_vyber_nic_nemeni(self):
        found = [{"url": "http://a:7126", "version": "1", "name": "Luna"},
                 {"url": "http://b:7126", "version": "1", "name": "Luna"}]
        with mock.patch.object(default, "luna_discover", return_value=found), \
                mock.patch.object(xbmcgui.Dialog, "select", return_value=-1), \
                mock.patch.object(default, "luna_check", side_effect=AssertionError("nemá ověřovat")):
            default.luna_find()
        self.assertNotIn("luna_url", xbmcaddon.settings)

    def test_nic_nenalezeno_poradi_co_dal(self):
        with mock.patch.object(default, "luna_discover", return_value=[]), \
                mock.patch.object(default, "luna_check", side_effect=AssertionError("nemá ověřovat")):
            default.luna_find()
        self.assertIn("7126", xbmcgui.oks[-1][1])
        self.assertNotIn("luna_url", xbmcaddon.settings)

    def test_hledani_nespadne_bez_site(self):
        with mock.patch.object(default, "luna_discover", side_effect=OSError("bez sítě")):
            default.luna_find()
        self.assertTrue(xbmcgui.oks)

    def test_nalezena_adresa_se_overi_hned_ne_z_nastaveni(self):
        """Past, na kterou uživatel narazil: `setSetting` políčko přepíše, ale `getSetting`
        při otevřeném dialogu nastavení vrátí ještě starou hodnotu → „adrese nerozumím“
        nad adresou, kterou doplněk právě sám našel. Adresa se proto předává přímo."""
        xbmcaddon.settings["luna_url"] = ""          # uživatel ji smazal a uložil
        found = [{"url": "http://192.168.1.10:7126", "version": "1.7.0", "name": "Luna"}]
        with mock.patch.object(default, "luna_discover", return_value=found), \
                mock.patch.object(default, "luna_diagnose",
                                  return_value=self.diag(level="ok", code="ok", version="1.7.0")) as diag, \
                mock.patch.object(xbmcgui.Dialog, "yesnocustom", return_value=0):
            default.luna_find()
        self.assertEqual(diag.call_args[0][0], "http://192.168.1.10:7126")

    def test_predana_adresa_prebije_ulozenou(self):
        xbmcaddon.settings.update({"luna_url": "http://stara:7126", "token": "e1.abc"})
        with mock.patch.object(default, "luna_diagnose",
                               return_value=self.diag(level="ok", code="ok")) as diag:
            default.luna_check("http://nova:7126")
        self.assertEqual(diag.call_args[0][0], "http://nova:7126")

    def test_zadat_adresu_overi_znovu_tou_zadanou(self):
        xbmcaddon.settings.update({"luna_url": "http://stara:7126", "token": "e1.abc"})
        with mock.patch.object(default, "luna_diagnose", return_value=self.diag(code="unreachable")) as diag, \
                mock.patch.object(xbmcgui.Dialog, "yesnocustom", side_effect=[1, 0]), \
                mock.patch.object(xbmcgui.Dialog, "input", return_value="192.168.1.5"):
            default.luna_check()
        self.assertEqual([c[0][0] for c in diag.call_args_list], ["http://stara:7126", "192.168.1.5"])

    def test_zadat_adresu_posle_cisty_token(self):
        """Kdyby v tokenu zůstala stará celá adresa, přebila by tu právě zadanou."""
        xbmcaddon.settings.update({"luna_url": "", "token": "http://stara:7126/metadata/e1.abc/manifest.json"})
        with mock.patch.object(default, "luna_diagnose", return_value=self.diag(code="unreachable")) as diag, \
                mock.patch.object(xbmcgui.Dialog, "yesnocustom", side_effect=[1, 0]), \
                mock.patch.object(xbmcgui.Dialog, "input", return_value="192.168.1.5"):
            default.luna_check()
        self.assertEqual(diag.call_args[0][1], "e1.abc")

    def test_zadavani_adresy_se_nezacykli(self):
        with mock.patch.object(default, "luna_diagnose", return_value=self.diag(code="unreachable")) as diag, \
                mock.patch.object(xbmcgui.Dialog, "yesnocustom", return_value=1), \
                mock.patch.object(xbmcgui.Dialog, "input", return_value="192.168.1.5"):
            default.luna_check()
        self.assertLessEqual(len(diag.call_args_list), 4)

    def test_tlacitko_se_pta_na_adresu_predvyplnenou_ulozenou(self):
        """Kodi akci nedá, co má uživatel rozepsané v políčku — ptáme se rovnou."""
        xbmcaddon.settings.update({"luna_url": "http://ulozena:7126", "token": "e1.abc"})
        with mock.patch.object(default, "luna_diagnose",
                               return_value=self.diag(level="ok", code="ok")) as diag, \
                mock.patch.object(xbmcgui.Dialog, "input", return_value="192.168.1.7") as vstup:
            default.router("?action=luna_check")
        self.assertEqual(vstup.call_args[1]["defaultt"], "http://ulozena:7126")
        self.assertEqual(diag.call_args[0][0], "192.168.1.7")

    def test_prazdny_vstup_neoveruje(self):
        with mock.patch.object(default, "luna_diagnose", side_effect=AssertionError("nemá ověřovat")), \
                mock.patch.object(xbmcgui.Dialog, "input", return_value=""):
            default.router("?action=luna_check")

    def test_cela_adresa_zadana_v_overeni_da_i_token(self):
        xbmcaddon.settings.update({"luna_url": "", "token": ""})
        with mock.patch.object(default, "luna_diagnose",
                               return_value=self.diag(level="ok", code="ok", token="e1.novy")) as diag, \
                mock.patch.object(xbmcgui.Dialog, "input",
                                  return_value="http://192.168.1.10:7126/metadata/e1.novy/manifest.json"):
            default.router("?action=luna_check")
        self.assertIn("e1.novy", diag.call_args[0][0])
        self.assertEqual(xbmcaddon.settings["token"], "e1.novy")

    def test_obe_akce_zna_router(self):
        for action in ("luna_check", "luna_find"):
            with mock.patch.object(default, action) as fn:
                default.router(f"?action={action}")
            fn.assert_called_once()


class TestPruvodceLuna(unittest.TestCase):
    """Krok Luny v průvodci prvním spuštěním — adresu uživatel zpravidla nezná."""

    def setUp(self):
        reset_kodi()
        xbmcaddon.settings.clear()

    def wizard(self, found, vlozeno=""):
        """Průvodcem projde jen krok Luny: na ostatní otázky odpoví Ne."""
        headings = []

        def yesno(self, heading, *a, **kw):
            return "Luna" in heading

        def zeptej(self, heading, *a, **kw):
            headings.append(heading)
            return vlozeno

        with mock.patch.object(xbmcgui.Dialog, "yesnocustom", lambda *a, **kw: 1), \
                mock.patch.object(xbmcgui.Dialog, "yesno", yesno), \
                mock.patch.object(xbmcgui.Dialog, "input", zeptej), \
                mock.patch.object(default, "luna_discover", return_value=found):
            default.setup_wizard(force=True)
        return headings[0] if headings else ""

    def test_nalezena_luna_se_ulozi_a_rekne_kam_pro_token(self):
        heading = self.wizard([{"url": "http://192.168.1.10:7126", "version": "1.7.0", "name": "Luna"}])
        self.assertEqual(xbmcaddon.settings["luna_url"], "http://192.168.1.10:7126")
        self.assertEqual(xbmcaddon.settings["luna_enabled"], "true")
        self.assertIn("192.168.1.10:7126", heading)   # adresa /setup rovnou v otázce na token

    def test_bez_nalezu_se_ptá_jako_dřív(self):
        heading = self.wizard([], vlozeno="e1.abc")
        self.assertNotIn("7126", heading)
        self.assertEqual(xbmcaddon.settings.get("token"), "e1.abc")

    def test_sken_bez_site_pruvodce_neshodí(self):
        with mock.patch.object(xbmcgui.Dialog, "yesnocustom", lambda *a, **kw: 1), \
                mock.patch.object(xbmcgui.Dialog, "yesno", lambda self, heading, *a, **kw: "Luna" in heading), \
                mock.patch.object(default, "luna_discover", side_effect=OSError("bez sítě")):
            default.setup_wizard(force=True)


class TestOveritZdrojeLuna(unittest.TestCase):
    """Luna v „Ověřit zdroje": manifest o platnosti tokenu nic neříká."""

    def setUp(self):
        reset_kodi()
        xbmcaddon.settings.clear()
        xbmcaddon.settings.update({"luna_enabled": "true", "luna_url": "http://192.168.1.10:7126",
                                   "token": "e1.abc"})

    def radek_luny(self):
        return next(r for r in xbmcgui.oks[-1][1].split("\n") if r.startswith("Luna"))

    def test_platny_token_ukaze_verzi(self):
        diag = {"level": "ok", "code": "ok", "base": "", "token": "e1.abc", "version": "1.7.0", "detail": ""}
        with mock.patch.object(default, "luna_diagnose", return_value=diag):
            default.test_sources()
        self.assertIn("1.7.0", self.radek_luny())

    def test_neplatny_token_uz_neni_zeleny(self):
        """Dřív se počítaly katalogy z manifestu — a ten Luna vydá i pro nesmyslný token."""
        diag = {"level": "fail", "code": "bad_token", "base": "", "token": "e1.x", "version": "1.7.0", "detail": ""}
        with mock.patch.object(default, "luna_diagnose", return_value=diag):
            default.test_sources()
        radek = self.radek_luny()
        self.assertIn("token", radek.lower())
        self.assertNotIn("OK", radek)

    def test_bez_uctu_v_lune_poradi_kam_se_podivat(self):
        diag = {"level": "warn", "code": "no_streams", "base": "", "token": "e1.x", "version": "1.7.0", "detail": ""}
        with mock.patch.object(default, "luna_diagnose", return_value=diag):
            default.test_sources()
        self.assertIn("WebShare", self.radek_luny())


class TestZhlednutoZKodi(unittest.TestCase):
    """„Označit jako zhlédnuté“ ze skinu píše jen do videodatabáze Kodi — u titulů Nokturna
    (bez IsPlayable) se fajfka neukázala a každé další stisknutí jen přičítalo (Office
    2026-09-18). Doplněk i služba změnu v databázi zachytí a převezmou do evidence."""

    TITLE = "plugin://plugin.video.nokturno/?action=title&type=movie&id=tt_kodi_mark"

    def setUp(self):
        reset_kodi()
        self.dir = tempfile.mkdtemp(prefix="nokturno-db-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        # starší databáze po aktualizaci Kodi zůstává ležet — musí se vzít ta nejnovější
        open(os.path.join(self.dir, "MyVideos121.db"), "w").close()
        self.db = os.path.join(self.dir, "MyVideos131.db")
        conn = sqlite3.connect(self.db)
        conn.executescript("CREATE TABLE path (idPath INTEGER PRIMARY KEY, strPath TEXT);"
                           "CREATE TABLE files (idFile INTEGER PRIMARY KEY, idPath INTEGER, strFilename TEXT,"
                           " playCount INTEGER, lastPlayed TEXT);"
                           "INSERT INTO path VALUES (1, 'plugin://plugin.video.nokturno/'), (2, 'plugin://jiny/');")
        conn.commit()
        conn.close()
        default.STORE.save(kodi_marks.STATE, {})
        default.STORE.set_watched("tt_kodi_mark", False)

    def row(self, url, count, last, path=1):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM files WHERE strFilename = ?", (url,))
        conn.execute("INSERT INTO files (idPath, strFilename, playCount, lastPlayed) VALUES (?, ?, ?, ?)",
                     (path, url, count, last))
        conn.commit()
        conn.close()

    def collect(self):
        return kodi_marks.collect(default.STORE, self.dir)

    def test_bere_nejnovejsi_databazi(self):
        self.assertEqual(kodi_marks.find_db(self.dir), self.db)
        self.assertIsNone(kodi_marks.find_db(os.path.join(self.dir, "neni")))

    def test_prvni_beh_jen_zapamatuje(self):
        """Staré řádky z přehrávání by jinak přepsaly pozdější volby v Nokturnu."""
        self.row(self.TITLE, 1, "2026-09-01 10:00:00")
        self.assertEqual(self.collect(), {})
        self.assertEqual(self.collect(), {})

    def test_oznaceni_a_zruseni_skinem(self):
        self.collect()
        self.row(self.TITLE, 1, "2026-09-18 17:15:28")             # nový řádek od skinu
        self.assertEqual(self.collect(), {"tt_kodi_mark": True})
        self.assertEqual(self.collect(), {}, "stejná změna se nehlásí dvakrát")
        self.row(self.TITLE, None, None)                              # zrušení: NULL/NULL
        self.assertEqual(self.collect(), {"tt_kodi_mark": False})
        self.row(self.TITLE, 2, "2026-09-18 18:00:00")               # znovu označeno, Kodi přičte
        self.assertEqual(self.collect(), {"tt_kodi_mark": True})

    def test_prerusene_prehravani_a_cizi_polozky_se_ignoruji(self):
        self.collect()
        self.row(self.TITLE, None, "2026-09-18 17:00:00")            # jen lastPlayed
        self.row("plugin://plugin.video.nokturno/?action=seasons&id=tt_x", 1, "2026-09-18 17:00:00")
        self.row("plugin://jiny/?action=title&id=tt_y", 1, "2026-09-18 17:00:00", path=2)
        self.assertEqual(self.collect(), {})

    def test_klic_z_adresy_prehrani_i_dilu(self):
        self.assertEqual(kodi_marks.key_of("plugin://plugin.video.nokturno/?action=play&type=series"
                                           "&id=tt1%3A2%3A3&series=tt1&url=ws%3Aabc"), "tt1:2:3")
        self.assertIsNone(kodi_marks.key_of("plugin://plugin.video.nokturno/?action=play_ws&ident=x"))

    def test_protichudne_radky_se_zahodi(self):
        self.collect()
        self.row(self.TITLE, 1, "2026-09-18 17:00:00")
        self.row("plugin://plugin.video.nokturno/?action=play&type=movie&id=tt_kodi_mark&ask=1", None, None)
        self.assertEqual(self.collect(), {})

    def test_necitelna_databaze_neprepise_stav(self):
        """Prázdný stav by příště udělal ze všech řádků nové — a označil je jako zhlédnuté."""
        self.row(self.TITLE, 1, "2026-09-01 10:00:00")
        self.collect()
        os.rename(self.db, self.db + ".x")
        self.assertEqual(self.collect(), {})
        os.rename(self.db + ".x", self.db)
        self.assertEqual(self.collect(), {})

    def test_plugin_prevezme_zmenu_pred_vykreslenim(self):
        self.collect()
        self.row(self.TITLE, 1, "2026-09-18 17:15:28")
        xbmcgui.Window(10000).clearProperty(default.SYNC_PROP)
        with mock.patch.object(default.xbmcvfs, "translatePath", return_value=self.dir), \
                mock.patch.object(default, "get_trakt", return_value=None):
            default.adopt_kodi_marks()
        self.assertEqual(default.STORE.playcount("tt_kodi_mark"), 1)
        self.assertTrue(xbmcgui.Window(10000).getProperty(default.SYNC_PROP), "změna jde i do HA")

    def test_sluzba_prevezme_zmenu_a_posle_do_traktu(self):
        self.collect()
        self.row(self.TITLE, 1, "2026-09-18 17:15:28")
        trakt = mock.Mock()
        marks = service.KodiMarks(default.STORE)
        marks.next = 0
        with mock.patch.object(service.xbmcvfs, "translatePath", return_value=self.dir), \
                mock.patch.object(service, "get_trakt", return_value=trakt):
            marks.tick()
        self.assertEqual(default.STORE.playcount("tt_kodi_mark"), 1)
        trakt.mark_watched.assert_called_once_with("tt_kodi_mark", None, None)

    def kodi_write(self, url, watched):
        """Jako `Files.SetFileDetails`: jednička dá čas, nula NULL/NULL."""
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE files SET playCount = ?, lastPlayed = ? WHERE strFilename = ?",
                     (1 if watched else None, "2026-09-18 19:00:00" if watched else None, url))
        conn.commit()
        conn.close()
        self.writes.append((url, watched))

    def test_zruseni_nad_prazdnym_radkem_po_oznaceni_v_nokturnu(self):
        """Řádek po dřívějším zrušení je NULL/NULL; zhlédnuto z menu Nokturna ho musí přepsat,
        jinak by další zrušení skinem zapsalo totéž a nešlo by poznat."""
        self.writes = []
        self.row(self.TITLE, None, None)
        kodi_marks.collect(default.STORE, self.dir, write=self.kodi_write)
        default.STORE.set_watched("tt_kodi_mark", True)               # menu Nokturna / HA
        self.assertEqual(kodi_marks.collect(default.STORE, self.dir, write=self.kodi_write), {})
        self.assertEqual(self.writes, [(self.TITLE, True)])
        self.row(self.TITLE, None, None)                              # zrušení skinem
        self.assertEqual(kodi_marks.collect(default.STORE, self.dir, write=self.kodi_write),
                         {"tt_kodi_mark": False})

    def test_zrcadli_jen_existujici_radky(self):
        self.writes = []
        default.STORE.set_watched("tt_kodi_mark", True)
        kodi_marks.collect(default.STORE, self.dir, write=self.kodi_write)
        self.assertEqual(self.writes, [])

    def test_prevzata_zmena_se_nezrcadli_zpet(self):
        self.writes = []
        kodi_marks.collect(default.STORE, self.dir, write=self.kodi_write)
        self.row(self.TITLE, 1, "2026-09-18 17:15:28")

        def apply(changes):
            for key, watched in changes.items():
                default.STORE.set_watched(key, watched)
        self.assertEqual(kodi_marks.collect(default.STORE, self.dir, apply=apply, write=self.kodi_write),
                         {"tt_kodi_mark": True})
        self.assertEqual(self.writes, [])

    def test_zapis_pres_json_rpc(self):
        sent = []
        kodi_marks.rpc_writer(sent.append)(self.TITLE, False)
        self.assertEqual(json.loads(sent[0])["params"], {"file": self.TITLE, "media": "video", "playcount": 0})

    def test_nezhlednuto_se_posila_vyslovne(self):
        """Přehratelné položce by Kodi jinak dosadilo počet ze své databáze."""
        li = xbmcgui.ListItem("Film")
        default.apply_watched(li, "tt_kodi_mark")
        self.assertIn(("setPlaycount", (0,), {}), li.tag.calls)

    def test_zhlednuty_nedostane_starou_zalozku_z_kodi(self):
        """Pozice 0 s nenulovou délkou = bod nastavený, ale bez ukazatele rozkoukání."""
        default.STORE.set_watched("tt_kodi_mark", True)
        li = xbmcgui.ListItem("Film")
        default.apply_watched(li, "tt_kodi_mark")
        self.assertIn(("setResumePoint", (0, 1), {}), li.tag.calls)
        default.STORE.set_watched("tt_kodi_mark", False)
        default.STORE.set_resume("tt_kodi_mark", 600, 6000)
        li = xbmcgui.ListItem("Film")
        default.apply_watched(li, "tt_kodi_mark")
        self.assertIn(("setResumePoint", (600.0, 6000.0), {}), li.tag.calls)


class TestSloucenéVerze(unittest.TestCase):
    """Sloučené verze (jádro `group_streams`): jeden řádek s ×N, „Zobrazit všechny streamy“ nad
    fulltextem a náhradní odkaz, když zástupce nejde přehrát (Pelíšky 2026-09-18)."""

    def setUp(self):
        reset_kodi()
        default.STORE.set_last_stream_filter({})
        self.alt = {"url": "ws:2", "label": "Pelisky.1999.1080p.CZ.mkv", "detail": "17.3 GB", "source": "ws"}
        self.rep = {"url": "hs:1", "label": "Pelisky 1999 1080p CZ.mkv", "detail": "18.6 GB", "source": "hs",
                    "_alts": [self.alt]}

    def test_radek_s_poctem_a_zobrazit_vsechny_pred_fulltextem(self):
        rozbaleno = []

        def expand():
            rozbaleno.append(1)
            return [dict(self.rep, _alts=[]), self.alt]
        volby = iter([3, -1])   # 0 = Filtr, 1–2 = streamy, 3 = Zobrazit všechny, 4 = fulltext
        dialogy = []

        def select(heading, rows, **kw):
            dialogy.append([r.getLabel() for r in rows])
            return next(volby)
        extra = {"url": "st:9", "label": "Pelisky.720p.CZ.mkv", "detail": "3 GB", "source": "st"}
        with mock.patch.object(xbmcgui.Dialog, "select", side_effect=select):
            self.assertIsNone(default.choose_stream([self.rep, extra], relax=True, expand=expand))
        prvni = dialogy[0]
        self.assertIn("×2", prvni[1])
        self.assertTrue(prvni[-2].startswith("Zobrazit všechny streamy") and "(3)" in prvni[-2], prvni)
        self.assertTrue(prvni[-1].startswith("Zkusit uvolněný fulltext"))
        self.assertEqual(rozbaleno, [1])
        self.assertFalse(any(label.startswith("Zobrazit všechny") for label in dialogy[1]), "po rozbalení už není co")

    def test_fulltext_zustava_posledni_volbou(self):
        with mock.patch.object(xbmcgui.Dialog, "select", return_value=2):
            self.assertIs(default.choose_stream([self.rep], relax=True, expand=lambda: []), default.FULLTEXT)

    def test_bez_sloucenych_se_zobrazit_vsechny_nenabizi(self):
        with mock.patch.object(xbmcgui.Dialog, "select", return_value=-1) as select:
            default.choose_stream([self.alt], relax=True, expand=lambda: [])
        labels = [r.getLabel() for r in select.call_args[0][1]]
        self.assertFalse(any(label.startswith("Zobrazit všechny") for label in labels))

    def test_nahradni_odkaz_kdyz_zastupce_nejde(self):
        def resolve(apis, url):
            if url == "hs:1":
                raise default.HellspyError("soubor smazán")
            return "https://cdn/" + url
        with mock.patch.object(default, "resolve_url", side_effect=resolve):
            self.assertEqual(default.resolve_first({}, ["hs:1", "ws:2"]), ("ws:2", "https://cdn/ws:2"))
            with self.assertRaises(default.HellspyError):
                default.resolve_first({}, ["hs:1"])

    def test_adresa_prehrani_nese_nahradni_odkazy(self):
        self.assertEqual(default.alts_param(self.rep), "ws:2")
        self.assertIsNone(default.alts_param(self.alt))


class TestPrenosNastaveni(unittest.TestCase):
    """Přenos nastavení do dalšího Kodi (6.2.0): kód na obrazovce, obsah zašifrovaný.

    Hlídá hlavně hranice — co se přenést **nesmí** (cesty toho stroje, klíč
    synchronizace, tokeny účtů) a že se stávající nastavení nepřepíše bez
    zálohy a bez potvrzení."""

    def setUp(self):
        reset_kodi()
        xbmcaddon.settings.clear()
        xbmcaddon.settings.update(ws_enabled="true", ws_username="martin", ws_password="tajne",
                                  pref_lang="1", download_dir="/storage/kodi", sync_key="klic",
                                  cz_enabled="true")
        # CZtor je ve výchozím stavu testu spárovaný — jinak by potvrzený přenos skončil
        # nabídkou párování, a ta chce síť. Testy párování si ho přemockují samy.
        sparovany = mock.Mock()
        sparovany.paired.return_value = True
        patch = mock.patch.object(default, "cztor_client", return_value=sparovany)
        patch.start()
        self.addCleanup(patch.stop)

    # --- co se přenáší -----------------------------------------------------
    def test_obsah_nese_ucty_ale_ne_stroj(self):
        payload = default.transfer_payload()
        self.assertEqual(payload["settings"]["ws_username"], "martin")
        self.assertEqual(payload["settings"]["ws_password"], "tajne")
        self.assertNotIn("download_dir", payload["settings"])
        self.assertNotIn("sync_key", payload["settings"])
        self.assertTrue(payload["source"].startswith("Kodi "))

    def test_cztor_jde_jen_jako_priznak(self):
        payload = default.transfer_payload()
        self.assertEqual(payload["flags"], {"cztor": True})
        self.assertNotIn("cz_", "".join(payload["settings"]))

    def test_neulozena_polozka_jde_s_vychozi_hodnotou(self):
        del xbmcaddon.settings["pref_lang"]
        payload = default.transfer_payload()
        self.assertIn("pref_lang", payload["settings"])

    # --- zápis na druhém zařízení -----------------------------------------
    def cizi_prenos(self, **zmeny):
        xbmcaddon.settings.update(zmeny)
        payload = default.transfer_payload()
        xbmcaddon.settings.update(ws_username="puvodni", ws_password="puvodni")
        return payload

    def test_potvrzeny_prenos_zapise_a_zalohuje(self):
        payload = self.cizi_prenos(ws_username="novy")
        with open(os.path.join(default.PROFILE, "settings.xml"), "w") as f:
            f.write("<settings/>")
        with mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True), \
             mock.patch.object(default.STORE, "clear_cache") as cache:
            default.transfer_apply(payload)
        self.assertEqual(xbmcaddon.settings["ws_username"], "novy")
        self.assertTrue(os.path.exists(os.path.join(default.PROFILE, default.TRANSFER_BACKUP)))
        cache.assert_called_once()          # cache patřila k účtům, které tu byly do teď
        self.assertEqual(len(xbmcgui.notifications), 1)

    def test_bez_potvrzeni_se_nic_nezapise(self):
        payload = self.cizi_prenos(ws_username="novy")
        with mock.patch.object(xbmcgui.Dialog, "yesno", return_value=False):
            default.transfer_apply(payload)
        self.assertEqual(xbmcaddon.settings["ws_username"], "puvodni")

    def test_shodny_prenos_se_jen_ohlasi(self):
        payload = default.transfer_payload()
        with mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True) as yesno:
            default.transfer_apply(payload)
        yesno.assert_not_called()
        self.assertEqual(len(xbmcgui.oks), 1)

    def test_podvrzeny_prenos_neprepise_cestu_ani_klic(self):
        payload = default.transfer_payload()
        payload["settings"].update(download_dir="/cizi", sync_key="cizi", neznamy="1")
        with mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True), \
             mock.patch.object(default.STORE, "clear_cache"):
            default.transfer_apply(payload)
        self.assertEqual(xbmcaddon.settings["download_dir"], "/storage/kodi")
        self.assertEqual(xbmcaddon.settings["sync_key"], "klic")
        self.assertNotIn("neznamy", xbmcaddon.settings)

    def test_novy_ucet_zahodi_token_webshare(self):
        payload = self.cizi_prenos(ws_username="novy")
        xbmcgui.Window(10000).setProperty("nokturno.ws_token", "stary")
        with mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True), \
             mock.patch.object(default.STORE, "clear_cache"):
            default.transfer_apply(payload)
        self.assertEqual(xbmcgui.Window(10000).getProperty("nokturno.ws_token"), "")

    def test_priznak_cztor_nabidne_parovani(self):
        payload = self.cizi_prenos(ws_username="novy")
        fake = mock.Mock()
        fake.paired.return_value = False
        with mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True), \
             mock.patch.object(default.STORE, "clear_cache"), \
             mock.patch.object(default, "cztor_client", return_value=fake), \
             mock.patch.object(default, "cztor_pair") as pair:
            default.transfer_apply(payload)
        pair.assert_called_once()

    def test_sparovany_cztor_se_uz_neptá(self):
        payload = self.cizi_prenos(ws_username="novy")
        fake = mock.Mock()
        fake.paired.return_value = True
        with mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True), \
             mock.patch.object(default.STORE, "clear_cache"), \
             mock.patch.object(default, "cztor_client", return_value=fake), \
             mock.patch.object(default, "cztor_pair") as pair:
            default.transfer_apply(payload)
        pair.assert_not_called()

    # --- cesta přes server -------------------------------------------------
    def test_odeslani_ukaze_kod(self):
        with mock.patch.object(transfer, "send_payload",
                               return_value=("NKT-ABCD-EFGH", 900)) as send:
            default.transfer_send()
        self.assertIn("NKT-ABCD-EFGH", xbmcgui.oks[0][1])
        self.assertIn("martin", json.dumps(send.call_args[0][0]))

    def test_chyba_serveru_se_ukaze_a_nic_nezmeni(self):
        with mock.patch.object(transfer, "send_payload",
                               side_effect=transfer.TransferError("server mlčí")):
            default.transfer_send()
        self.assertIn("server mlčí", xbmcgui.oks[0][1])

    def test_nacteni_bere_kod_od_uzivatele(self):
        payload = self.cizi_prenos(ws_username="novy")
        with mock.patch.object(xbmcgui.Dialog, "input", return_value="NKT-ABCD-EFGH"), \
             mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True), \
             mock.patch.object(default.STORE, "clear_cache"), \
             mock.patch.object(transfer, "receive", return_value=payload) as recv:
            default.transfer_receive()
        recv.assert_called_once_with("NKT-ABCD-EFGH")
        self.assertEqual(xbmcaddon.settings["ws_username"], "novy")

    def test_prazdny_kod_nic_nedela(self):
        with mock.patch.object(xbmcgui.Dialog, "input", return_value=""), \
             mock.patch.object(transfer, "receive") as recv:
            default.transfer_receive()
        recv.assert_not_called()

    # --- cesta přes soubor -------------------------------------------------
    def test_soubor_tam_a_zpet(self):
        folder = tempfile.mkdtemp(prefix="nokturno-prenos-")
        try:
            with mock.patch.object(xbmcgui.Dialog, "browseSingle", return_value=folder):
                default.transfer_file_save()
            soubory = [f for f in os.listdir(folder) if f.endswith(default.TRANSFER_EXT)]
            self.assertEqual(len(soubory), 1)
            cesta = os.path.join(folder, soubory[0])
            with open(cesta, "rb") as f:
                blob = f.read()
            self.assertNotIn(b"tajne", blob)       # heslo v souboru čitelné není
            kod = re.search(r"NKT-[0-9A-Z-]+", xbmcgui.oks[0][1]).group(0)

            xbmcaddon.settings.update(ws_username="puvodni")
            with mock.patch.object(xbmcgui.Dialog, "browseSingle", return_value=cesta), \
                 mock.patch.object(xbmcgui.Dialog, "input", return_value=kod), \
                 mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True), \
                 mock.patch.object(default.STORE, "clear_cache"):
                default.transfer_file_load()
            self.assertEqual(xbmcaddon.settings["ws_username"], "martin")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_spatny_kod_u_souboru_nic_nezapise(self):
        folder = tempfile.mkdtemp(prefix="nokturno-prenos-")
        try:
            with mock.patch.object(xbmcgui.Dialog, "browseSingle", return_value=folder):
                default.transfer_file_save()
            cesta = os.path.join(folder, os.listdir(folder)[0])
            xbmcaddon.settings.update(ws_username="puvodni")
            with mock.patch.object(xbmcgui.Dialog, "browseSingle", return_value=cesta), \
                 mock.patch.object(xbmcgui.Dialog, "input", return_value="NKT-AAAA-AAAA"), \
                 mock.patch.object(xbmcgui.Dialog, "yesno", return_value=True):
                default.transfer_file_load()
            self.assertEqual(xbmcaddon.settings["ws_username"], "puvodni")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    # --- napojení na Kodi --------------------------------------------------
    def test_tlacitka_v_nastaveni_vedou_na_router(self):
        xml = ET.parse(ROOT / "resources" / "settings.xml").getroot()
        kategorie = next(c for c in xml.iter("category") if c.get("id") == "transfer")
        akce = [re.search(r"action=(\w+)", s.findtext("data")).group(1)
                for s in kategorie.iter("setting")]
        self.assertEqual(akce, ["transfer_send", "transfer_receive",
                                "transfer_file_save", "transfer_file_load"])
        for name in akce:
            self.assertIn(name, default.MARKS_SKIP)   # čtení videodatabáze tu nemá co dělat

    def test_router_vola_funkce(self):
        for akce, funkce in (("transfer_send", "transfer_send"),
                             ("transfer_receive", "transfer_receive"),
                             ("transfer_file_save", "transfer_file_save"),
                             ("transfer_file_load", "transfer_file_load")):
            with mock.patch.object(default, funkce) as f:
                default.router("action=" + akce)
            f.assert_called_once()

    def test_prenos_neni_v_seznamu_kategorii_z_mobilu(self):
        """Stránka z mobilu je formulář hodnot — tlačítka přenosu na ni nepatří."""
        self.assertNotIn("transfer", default.REMOTE_SETUP_CATEGORIES)
        ids = [f["id"] for s in default.remote_setup_schema() for f in s["fields"] if f.get("id")]
        self.assertNotIn("transfer_send_action", ids)


class TestReuseInvoker(unittest.TestCase):
    """`<reuselanguageinvoker>` v addon.xml (nález 18 z auditu 2026-09-19).

    Kodi s ním nechá interpret běžet a při dalším kliknutí spustí `default.py` znovu
    v něm. Tělo souboru se tedy vykoná vícekrát, zatímco `sys.modules`, `sys.path`
    a kořenový logger zůstávají z minula. Testy simulují druhé spuštění tím, že
    tělo souboru pustí do nových globálů.
    """

    def _spust_telo_znovu(self):
        """Druhé spuštění `default.py` v témž interpretu, jako to dělá Kodi."""
        kod = compile((ROOT / "default.py").read_text(encoding="utf-8"), "default.py", "exec")
        globaly = {"__name__": "nokturno_reuse_test", "__file__": str(ROOT / "default.py")}
        exec(kod, globaly)   # noqa: S102 – přesně o tohle tady jde
        return globaly

    def test_prepinac_je_v_metadatech_ne_v_pluginsource(self):
        """Kodi čte `reuselanguageinvoker` z `xbmc.addon.metadata`. Uvnitř
        `xbmc.python.pluginsource` si ho nevšimne — ověřeno na Office 2026-09-20,
        kde takhle umístěný přepínač nic nedělal (nový CPythonInvoker při každém
        kliknutí). Stejné místo mají youtube i themoviedb.helper."""
        korene = ET.parse(ROOT / "addon.xml").getroot()
        meta = korene.find("./extension[@point='xbmc.addon.metadata']")
        self.assertIsNotNone(meta)
        self.assertEqual((meta.findtext("reuselanguageinvoker") or "").strip(), "true")
        plugin = korene.find("./extension[@point='xbmc.python.pluginsource']")
        self.assertIsNone(plugin.find("reuselanguageinvoker"),
                          "v pluginsource je přepínač k ničemu")

    def test_sys_path_neroste(self):
        pred = list(sys.path)
        self._spust_telo_znovu()
        self.assertEqual(sys.path, pred, "cesta k resources/lib se nesmí přidávat znovu")

    def test_log_handler_se_neprida_podruhe(self):
        koren = logging.getLogger()
        pred = [h for h in koren.handlers if getattr(h, "nokturno", False)]
        self.assertEqual(len(pred), 1, "po importu má být právě jeden")
        self._spust_telo_znovu()
        po = [h for h in koren.handlers if getattr(h, "nokturno", False)]
        self.assertEqual(len(po), 1, "druhé spuštění nesmí handler zdvojit")

    def test_znacka_handleru_neni_isinstance(self):
        """Každé spuštění vyrobí novou třídu `_KodiLogHandler`, takže handler z minula
        je instancí jiné třídy téhož jména — proto se poznává atributem."""
        globaly = self._spust_telo_znovu()
        nova_trida = globaly["_KodiLogHandler"]
        self.assertIsNot(nova_trida, default._KodiLogHandler)
        stary = next(h for h in logging.getLogger().handlers if getattr(h, "nokturno", False))
        self.assertFalse(isinstance(stary, nova_trida))
        self.assertTrue(getattr(stary, "nokturno", False))

    def test_handle_a_base_url_se_ctou_znovu(self):
        """Pod reuse přijde nový handle v argv — tělo si ho musí přečíst znovu."""
        puvodni = list(sys.argv)
        sys.argv = ["plugin://plugin.video.nokturno/", "42", ""]
        try:
            globaly = self._spust_telo_znovu()
        finally:
            sys.argv = puvodni
        self.assertEqual(globaly["HANDLE"], 42)
        self.assertEqual(globaly["BASE_URL"], "plugin://plugin.video.nokturno/")

    def test_cancel_je_pro_kazde_spusteni_nove(self):
        """Zrušení minulého běhu nesmí umlčet ten další."""
        globaly = self._spust_telo_znovu()
        self.assertIsNot(globaly["CANCEL"], default.CANCEL)
        self.assertFalse(globaly["CANCEL"].is_set())

    def test_tezke_moduly_se_importuji_az_kdyz_je_potreba(self):
        """`remote_setup` táhne `http.server`, `qr` skládá PNG, `transfer` kryptografii.
        Menu, výpisy ani přehrání je nepotřebují."""
        zdroj = (ROOT / "default.py").read_text(encoding="utf-8")
        hlavicka = zdroj.split("ADDON = xbmcaddon.Addon()")[0]
        for modul in ("qr", "remote_setup", "transfer"):
            self.assertNotRegex(hlavicka, rf"(?m)^(from {modul} import|import {modul}\b)",
                                f"{modul} se má importovat až v funkci")
        self.assertIs(default._qr(), sys.modules["qr"])
        self.assertIs(default._remote_setup(), sys.modules["remote_setup"])
        self.assertIs(default._transfer_core(), sys.modules["transfer"])


class TestPopisCasuFazi(unittest.TestCase):
    """`describe_timings()` skládá řádek do kodi.log s časy fází hledání streamů.

    Pád nahlášený z 6.2.7 (`TypeError: '<' not supported between instances of 'str'
    and 'float'`): zdroj, který nestihl rozpočet, má místo času značku „>20s“, a
    `sorted()` ji porovnával s časy ostatních zdrojů."""

    def test_opozdily_zdroj_neshodi_vypis(self):
        popis = default.describe_timings({
            "zdroje": {"Sosáč": 1.2, "HellSpy": ">20s", "WebShare": 0.4},
            "celkem": 20.5,
        })
        self.assertIn("HellSpy >20s", popis)
        self.assertLess(popis.index("WebShare"), popis.index("Sosáč"), "rychlejší zdroj napřed")
        self.assertLess(popis.index("Sosáč"), popis.index("HellSpy"), "opozdilec až nakonec")

    def test_vsechny_zdroje_opozdene(self):
        popis = default.describe_timings({"zdroje": {"A": ">20s", "B": ">20s"}})
        self.assertIn("A >20s", popis)
        self.assertIn("B >20s", popis)
class TestStavZdroju(unittest.TestCase):
    """Stav zdrojů jako první položka menu (nápad 17 z auditu 2026-09-19).

    „Prostě mi to nejde" má několik různých příčin, které od sebe uživatel
    u televize nerozezná. Hlídá se tu, že se hlásí jen to, co má, že se stav
    kreslí barvou a slovem (ne ✔/✘, ty fonty skinů kreslí jako prázdný proužek)
    a že čtení stavu **nesahá na síť** — menu se otevírá za 0,9 s a to číslo se
    nesmí zhoršit.
    """

    def setUp(self):
        reset_kodi()
        xbmcaddon.settings.clear()

    @staticmethod
    def _row(source, level, code, **detail):
        return {"source": source, "level": level, "code": code, "detail": detail,
                "age": 60, "stale": False}

    def test_veta_nese_jmeno_zdroje_i_cislo(self):
        radek = default.account_line(self._row("webshare", "warn", "expires_soon", days=3))
        self.assertIn("WebShare", radek)
        self.assertIn("3", radek)

    def test_stav_je_slovem_v_barve_hexem(self):
        """Emoji a ✔/✘ kreslí fonty skinů jako prázdný proužek, pojmenované barvy
        závisí na skinu — viz `5.2.30~beta2`."""
        radek = default.account_line(self._row("hellspy", "warn", "paused", minutes=10))
        self.assertRegex(radek, r"\[COLOR FF[0-9A-F]{6}\]")
        for znak in ("✔", "✘", "❌", "⚠"):
            self.assertNotIn(znak, radek)

    def test_kazda_uroven_ma_vlastni_barvu(self):
        barvy = {default.account_line(self._row("webshare", level, "expired"))
                 for level in ("fail", "warn", "ok")}
        self.assertEqual(len(barvy), 3)

    def test_luna_pouziva_svou_sadu_textu_z_5_2_30(self):
        radek = default.account_line(self._row("luna", "fail", "bad_token"))
        self.assertIn("token", radek.lower())

    def test_neznamy_kod_nespadne(self):
        radek = default.account_line(self._row("fastshare", "fail", "nesmysl"))
        self.assertTrue(radek.startswith(default.FS_TAG))

    def test_souhrn_bere_jen_problemy(self):
        rows = [self._row("luna", "ok", "ok"),
                self._row("webshare", "fail", "expired"),
                self._row("hellspy", "warn", "paused", minutes=7)]
        souhrn = default.account_summary(rows)
        # do štítku se vejde jeden zdroj, nejzávažnější napřed; zbytek je „+N"
        self.assertIn("WebShare", souhrn)
        self.assertIn("+1", souhrn)
        self.assertNotIn("Luna", souhrn)

    def test_stitek_menu_se_vejde_na_radek(self):
        """Skin má na řádek zhruba čtyřicet znaků a delší text si roluje pod rukama —
        na Office byl ze souhrnu dvou zdrojů vidět jen prostředek věty."""
        rows = [self._row(s, "warn", c, **d) for s, c, d in (
            ("webshare", "expires_soon", {"days": 3}),
            ("hellspy", "paused", {"minutes": 10}),
            ("sledujteto", "no_premium", {}),
            ("cztor", "not_paired", {}))]
        for r in rows:
            holy = re.sub(r"\[/?COLOR[^\]]*\]", "", default.account_summary([r] + rows[1:]))
            self.assertLessEqual(len(holy), 40, holy)

    def test_vypis_ma_plny_text_i_kdyz_stitek_kratky(self):
        row = self._row("sledujteto", "warn", "no_premium")
        self.assertIn("přehrávání", default.account_line(row, color=False))
        self.assertNotIn("přehrávání", default.account_line(row, color=False, short=True))

    def test_souhrn_bez_problemu_je_prazdny(self):
        self.assertEqual(default.account_summary([self._row("luna", "ok", "ok")]), "")

    def test_popis_polozky_nese_vsechny_zdroje(self):
        """Do štítku se vejdou dva, skiny řádek ořezávají — zbytek musí být v popisu."""
        rows = [self._row(s, "fail", "expired") for s in ("luna", "webshare", "cztor", "fastshare")]
        with mock.patch.object(default.KodiEngine, "accounts", lambda self, **kw: rows):
            default.router("")
        tag = xbmcplugin.items[0][2].getVideoInfoTag()
        popis = next(a[0] for name, a, _kw in tag.calls if name == "setPlot")
        for tag in ("Luna", "WebShare", "CZtor", "FastShare"):
            self.assertIn(tag, popis)

    def test_souhrn_dlouhy_seznam_zkrati(self):
        rows = [self._row(s, "fail", "expired") for s in
                ("luna", "webshare", "cztor", "fastshare", "sledujteto")]
        self.assertIn(f"+{5 - default.SUMMARY_LIMIT}", default.account_summary(rows))

    def test_menu_bez_problemu_polozku_neukaze(self):
        """Kdo problém nemá, tomu by položka jen zabírala místo."""
        with mock.patch.object(default.KodiEngine, "accounts",
                               lambda self, **kw: [self_row for self_row in ()]):
            default.router("")
        popisky = [li.getLabel() for _h, _u, li, _f in xbmcplugin.items]
        self.assertFalse([p for p in popisky if "Stav zdrojů" in p])

    def test_menu_s_problemem_ma_polozku_prvni(self):
        rows = [self._row("webshare", "fail", "expired")]
        with mock.patch.object(default.KodiEngine, "accounts", lambda self, **kw: rows):
            default.router("")
        prvni = xbmcplugin.items[0]
        self.assertIn("Stav zdrojů", prvni[2].getLabel())
        self.assertIn("WebShare", prvni[2].getLabel())
        self.assertIn("action=accounts", prvni[1])

    def test_vypis_vynecha_vypnute_zdroje(self):
        rows = [self._row("webshare", "fail", "expired"),
                {"source": "cztor", "level": "off", "code": "off", "detail": {}, "age": None, "stale": False}]
        with mock.patch.object(default.KodiEngine, "accounts", lambda self, **kw: rows):
            default.router("action=accounts")
        popisky = [li.getLabel() for _h, _u, li, _f in xbmcplugin.items]
        self.assertTrue([p for p in popisky if "WebShare" in p])
        self.assertFalse([p for p in popisky if "CZtor" in p])

    def test_vypis_vede_rovnou_tam_kde_se_to_opravuje(self):
        rows = [self._row("luna", "fail", "unreachable"),
                self._row("cztor", "fail", "not_paired"),
                self._row("webshare", "warn", "expires_soon", days=2)]
        with mock.patch.object(default.KodiEngine, "accounts", lambda self, **kw: rows):
            default.router("action=accounts")
        cile = [url for _h, url, _li, _f in xbmcplugin.items]
        self.assertIn("action=luna_check", cile[0])
        self.assertIn("action=cztor_pair", cile[1])
        self.assertIn("action=sub_status", cile[2])

    def test_vypis_nikdy_neotevre_modal(self):
        """Modál v cestě, kterou umí spustit widget nebo JSON-RPC, zasekne plugin
        i vypínání Kodi (pravidlo z CLAUDE.md)."""
        rows = [self._row("webshare", "fail", "expired")]
        with mock.patch.object(default.KodiEngine, "accounts", lambda self, **kw: rows):
            default.router("action=accounts")
        self.assertEqual(xbmcgui.oks, [])

    def test_obnova_zavira_adresar_uspesne(self):
        """Služba sem chodí přes Files.GetDirectory; `succeeded=False` by dělalo
        v každém kole řádek `error <general>` v kodi.log (viz 6.2.7)."""
        with mock.patch.object(default.KodiEngine, "refresh_accounts", lambda self, **kw: []):
            default.router("action=accounts_refresh")
        self.assertIs(xbmcplugin.ended[-1]["succeeded"], True)

    def test_obnova_nespadne_kdyz_zdroj_selze(self):
        def vybuch(self, **kw):
            raise OSError("síť spadla")
        with mock.patch.object(default.KodiEngine, "refresh_accounts", vybuch):
            default.router("action=accounts_refresh")
        self.assertIs(xbmcplugin.ended[-1]["succeeded"], True)

    def test_stav_projde_nad_plochou_kopii_jadra(self):
        """Doplněk načítá `resources/lib` ploše, ne jako balíček. Relativní import
        uvnitř funkce (líné načtení) zploštění přehlédne a Kodi spadne až za běhu na
        „attempted relative import with no known parent package" — ostatní testy
        jádro importují jako balíček, takže by to prošlo. Tenhle jde přes skutečnou
        cestu: KodiEngine nad vysypanou kopií."""
        xbmcaddon.settings.update({"hs_enabled": "true", "ws_username": "kdosi"})
        rows = default.KodiEngine().accounts()
        self.assertEqual([r["source"] for r in rows], list(accounts_module.SOURCES))
        self.assertEqual(rows[accounts_module.SOURCES.index("hellspy")]["code"], "ok")

    def test_engine_options_nese_vse_co_stav_cte(self):
        """Past z 6.3.1: KodiEngine si klienty staví z `get_*()`, takže jádro do té
        doby `luna_token` ani `cz_enabled` nepotřebovalo a `engine_options()` je
        neposílalo. Stav zdrojů je čte přímo, a bez nich hlásil chybějící token
        i u správně nastavené Luny a CZtor vůbec neukázal."""
        xbmcaddon.settings.update({"luna_enabled": "true", "token": "e1.abc",
                                   "cz_enabled": "true"})
        volby = default.engine_options()
        self.assertEqual(volby["luna_token"], "e1.abc")
        self.assertIs(volby["cz_enabled"], True)

    def test_luna_s_tokenem_nehlasi_chybejici_token(self):
        xbmcaddon.settings.update({"luna_enabled": "true", "token": "e1.abc",
                                   "luna_url": "http://192.168.1.10:7126"})
        diag = {"level": "ok", "code": "ok", "base": "http://192.168.1.10:7126",
                "token": "e1.abc", "version": "1.7.0", "detail": ""}
        engine = default.KodiEngine()
        with mock.patch("accounts.luna_diagnose", return_value=diag):
            engine.refresh_accounts(only=["luna"])
        self.assertEqual(engine.accounts()[accounts_module.SOURCES.index("luna")]["code"], "ok")

    def test_sluzba_obnovuje_pod_platnosti_zaznamu(self):
        """Jinak by v menu stál stav označený jako zastaralý."""
        from accounts import TTL as ACCOUNTS_TTL
        self.assertLess(service.ACCOUNTS_EVERY, ACCOUNTS_TTL)


class TestVydaneDily(unittest.TestCase):
    """Další díl se nabízí a zahřívá jen tehdy, když už vyšel (6.3.4)."""

    def test_dil_s_budoucim_datem_se_preskoci(self):
        # Zrádci: S03E01 vyšel 16. 9., S03E02 vychází až 23. 9.
        videos = [{"season": 3, "episode": 1, "released": "2026-09-16"},
                  {"season": 3, "episode": 2, "released": "2026-09-23"},
                  {"season": 3, "episode": 3, "released": "2026-09-30"}]
        aired = default.aired_videos(videos, "2026-09-20")
        self.assertEqual([(v["season"], v["episode"]) for v in aired], [(3, 1)])

    def test_dil_bez_data_se_u_bezicoho_serialu_preskoci(self):
        # Cizinka: poslední díly sezóny se teprve natáčejí, TMDB u nich datum nemá
        videos = [{"season": 2, "episode": 1, "released": "2026-09-18"},
                  {"season": 2, "episode": 2, "released": "2026-09-25"},
                  {"season": 2, "episode": 8}, {"season": 2, "episode": 9}]
        aired = default.aired_videos(videos, "2026-09-20")
        self.assertEqual([(v["season"], v["episode"]) for v in aired], [(2, 1)])

    def test_serial_bez_jedineho_data_projde_cely(self):
        videos = [{"season": 1, "episode": 2}, {"season": 1, "episode": 1}, {"season": 2, "episode": 1}]
        aired = default.aired_videos(videos, "2026-09-20")
        self.assertEqual([(v["season"], v["episode"]) for v in aired], [(1, 1), (1, 2), (2, 1)])

    def test_specialy_a_prazdny_seznam(self):
        self.assertEqual(default.aired_videos([{"season": 0, "episode": 1, "released": "2020-01-01"}], "2026-09-20"), [])
        self.assertEqual(default.aired_videos([], "2026-09-20"), [])

    def test_dil_vydany_dnes_se_bere(self):
        videos = [{"season": 1, "episode": 1, "released": "2026-09-20"}]
        self.assertEqual(len(default.aired_videos(videos, "2026-09-20")), 1)

    def test_next_episode_nenabidne_nevydany_dil(self):
        meta = {"videos": [{"season": 3, "episode": 1, "released": "2026-09-16", "id": "tt1:3:1"},
                           {"season": 3, "episode": 2, "released": "2099-01-01", "id": "tt1:3:2"}]}

        class Api:
            def meta(self, _ctype, _id):
                return meta

        with mock.patch.object(default, "api_for", lambda _apis, _id: Api()):
            # po S03E01 není co nabídnout — S03E02 ještě nevyšla
            self.assertIsNone(default.next_episode(None, {"series": "tt1", "season": 3, "episode": 1}))
            # po S02E13 je naopak S03E01 na řadě
            found = default.next_episode(None, {"series": "tt1", "season": 2, "episode": 13})
            self.assertIsNotNone(found)
            self.assertEqual((found[0]["season"], found[0]["episode"]), (3, 1))




class TestOpenSubtitles(unittest.TestCase):
    """Titulky z OpenSubtitles v doplňku — nastavení, tlačítko v něm a otisk souboru."""

    def test_nastaveni_ma_kategorii(self):
        strom = ET.parse(ROOT / "resources" / "settings.xml")
        kategorie = {c.get("id") for c in strom.iter("category")}
        self.assertIn("osub", kategorie)
        volby = {s.get("id") for s in strom.iter("setting")}
        self.assertTrue({"os_enabled", "os_username", "os_password", "os_check"} <= volby)

    def test_heslo_je_skryte(self):
        """Heslo k účtu se nesmí na televizi vypsat na obrazovku."""
        strom = ET.parse(ROOT / "resources" / "settings.xml")
        heslo = next(s for s in strom.iter("setting") if s.get("id") == "os_password")
        ovladak = heslo.find("control")
        self.assertIsNotNone(ovladak)
        self.assertEqual(ovladak.findtext("hidden"), "true")

    def test_tlacitko_zna_router(self):
        import default
        self.assertIn("os_check", default.MARKS_SKIP,
                      "tlačítko v nastavení nic nevypisuje, čtení MyVideos*.db je tam zbytečné")
        self.assertTrue(hasattr(default, "os_check"))

    def test_otisk_se_pocita_jen_kdyz_neni_nic_lepsiho(self):
        """Otisk stojí `Range` dotaz navíc — u streamu s jinými titulky se nepočítá."""
        import default
        volano = []

        class Jadro:
            def subtitles_by_hash(self, url, video=None, ctype="movie"):
                volano.append(url)
                return ["os:99"]

        apis = {"engine": Jadro()}
        self.assertEqual(default.subtitle_refs(apis, {"subtitles": []}, "http://x"), [])
        self.assertEqual(volano, [], "bez titulků není co zpřesňovat")
        self.assertEqual(default.subtitle_refs(apis, {"subtitles": ["ws:a"]}, "http://x"), ["ws:a"])
        self.assertEqual(volano, [], "nález z WebShare je vybraný podle názvu releasu, stačí")
        self.assertEqual(default.subtitle_refs(apis, {"subtitles": ["os:1"]}, "http://x"),
                         ["os:99", "os:1"], "otisk sedí ke konkrétnímu souboru, jde první")
        self.assertEqual(volano, ["http://x"])

    def test_chyba_otisku_neshodi_prehrani(self):
        import default

        class Jadro:
            def subtitles_by_hash(self, *a, **k):
                raise RuntimeError("zdroj bez Range")

        self.assertEqual(default.subtitle_refs({"engine": Jadro()}, {"subtitles": ["os:1"]}, "http://x"),
                         ["os:1"])
