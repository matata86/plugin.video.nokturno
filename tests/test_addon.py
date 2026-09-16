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
import shutil
import sys
import tempfile
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

import default                                # noqa: E402
import service                                # noqa: E402
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
        for lang in ("cs_cz", "en_gb", "sk_sk"):
            chybi = sorted(used - set(po_ids(lang)))
            self.assertEqual(chybi, [], f"{lang}: chybí #{chybi}")

    def test_bez_duplicit_a_stejna_sada_ve_vsech_jazycich(self):
        sady = {}
        for lang in ("cs_cz", "en_gb", "sk_sk"):
            ids = po_ids(lang)
            dup = sorted({i for i in ids if ids.count(i) > 1})
            self.assertEqual(dup, [], f"{lang}: duplicitní #{dup}")
            sady[lang] = set(ids)
        self.assertEqual(sady["cs_cz"], sady["en_gb"])
        self.assertEqual(sady["cs_cz"], sady["sk_sk"])

    def test_zadny_prazdny_preklad(self):
        # angličtina je zdrojový jazyk: text nese msgid a msgstr je podle zvyklostí Kodi prázdný
        for lang, pole in (("cs_cz", "msgstr"), ("sk_sk", "msgstr"), ("en_gb", "msgid")):
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
        self.assertEqual(default.display_name({"id": "tt1", "name": "Matrix", "releaseInfo": "1999-"}), "Matrix (1999)")
        self.assertEqual(default.display_name({"id": "tt1", "name": "Matrix"}), "Matrix")
        self.assertEqual(default.display_name({"id": "sosacd_5", "name": "Matrix CZ/EN (The Matrix)",
                                               "_title": "Matrix", "year": 1999}), "Matrix (1999)")

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
        bar.update.assert_called_with(int(3 / 8 * 100), "WebShare: 1 · Ověřuji metadata: 2/4")
        # chyba jádra v hlášce nese zdroj sama
        self.assertEqual(default.describe_error(default.NokturnoError("WebShare: soubor není")), "WebShare: soubor není")
        self.assertEqual(default.error_label(default.NokturnoError("Chybí odkaz na stream.")), "Nokturno")

    def test_search_progress_hlasi_odkud_a_kolik(self):
        """Ukazatel průběhu čtenáři dřív ukazoval jen procento — teď i to, odkud kolik
        streamů zatím přišlo, ve stejném pořadí, v jakém zdroje dorazily."""
        bar = mock.Mock()
        progress = default.SearchProgress(bar, 10)
        progress.tick()
        bar.update.assert_called_with(10)
        progress.source("Luna", 0)
        bar.update.assert_called_with(10, "Luna: 0")
        progress.source("WebShare", 12)
        bar.update.assert_called_with(10, "Luna: 0 · WebShare: 12")

    def test_search_progress_hlasi_overovani_zvuku(self):
        """Poslední a nejdelší fáze (čtení hlaviček souborů) — kolik už je ověřeno
        z kolika se doopravdy čte, vedle přehledu zdrojů."""
        bar = mock.Mock()
        progress = default.SearchProgress(bar, 10)
        progress.source("WebShare", 12)
        progress.audio(0, 5)
        bar.update.assert_called_with(0, "WebShare: 12 · Ověřuji metadata: 0/5")
        progress.audio(3, 5)
        bar.update.assert_called_with(0, "WebShare: 12 · Ověřuji metadata: 3/5")

        # bez source() (search_run, hledání podle názvu) audio() se nevolá vůbec —
        # ale kdyby, ukazatel si i tak nechá jen tuhle část, ne prázdný text z create()
        bar2 = mock.Mock()
        holy = default.SearchProgress(bar2, 10)
        holy.audio(1, 2)
        bar2.update.assert_called_with(0, "Ověřuji metadata: 1/2")

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
        self.assertIn("~4K", default.stream_label(s))
        s = {"url": "ws:1", "label": "Film.mkv", "detail": "9 GB", "source": "ws", "quality_rank": 4}
        self.assertNotIn("~", default.stream_label(s).split("[/B]")[0])


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
            return heading in ("Vítej v Nokturnu", "Přehrát z detailu filmu")   # jen úvod a krok TMDb Helperu

        with mock.patch.object(default, "TMDBH_PLAYER", dest), mock.patch.object(xbmcgui.Dialog, "yesno", side_effect=yesno):
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

    def test_ask_nabidne_vyber_jen_v_rezimu_seznamu(self):
        streams = [{"url": "ws:1", "label": "Film.2020.1080p.mkv", "detail": "2 GB", "source": "ws"}]
        common = [mock.patch.object(default, "load_meta", return_value=({"name": "Film", "year": 2020}, None)),
                  mock.patch.object(default, "collect_streams", return_value=streams)]
        for mode, ask, asked in (("1", "1", True), ("1", "", False), ("0", "1", False), ("2", "", True)):
            reset_kodi()
            xbmcaddon.settings["stream_mode"] = mode
            with common[0], common[1], mock.patch.object(xbmcgui.Dialog, "select", return_value=-1) as select, \
                 mock.patch.object(default, "resolve_url", return_value="https://cdn/x.mkv"), \
                 mock.patch.object(default, "fill_info"), mock.patch.object(default, "mark_playing"), \
                 mock.patch.object(default.STORE, "remember_item"):
                default.play({}, "movie", "tt1", ask=ask)
            self.assertEqual(select.called, asked, f"stream_mode={mode} ask={ask!r}")


class TestPrehratelnePolozky(unittest.TestCase):
    """Film a díl jsou přehratelné v každém režimu výběru streamu. Přehrát v detailu (Arctic Fuse)
    volá PlayMedia na cestu položky — složka se streamy tam nic nepřehrála (Office 2026-09-14)."""

    def setUp(self):
        reset_kodi()

    def streams_menu(self, li):
        akce = dict(li.context).get("Seznam streamů", "")
        self.assertTrue(akce.startswith("ActivateWindow(Videos,plugin://plugin.video.nokturno/?"), akce)
        self.assertTrue(akce.endswith(",return)"), akce)
        self.assertTrue(any("toggle_watched" in a for _l, a in li.context), "menu se skládá jedním voláním")
        return params_of(akce[len("ActivateWindow(Videos,"):-len(",return)")])

    def test_film_prehratelny_v_kazdem_rezimu_se_seznamem_v_menu(self):
        for mode, ask in (("0", None), ("1", "1"), ("2", None)):
            reset_kodi()
            xbmcaddon.settings["stream_mode"] = mode
            default.add_meta_item({"id": "tt1", "name": "Film", "year": 2020}, "movie", alt="sosacd_1")
            _h, url, li, is_folder = xbmcplugin.items[-1]
            self.assertFalse(is_folder, f"stream_mode={mode}")
            self.assertEqual(li.properties.get("IsPlayable"), "true")
            p = params_of(url)
            self.assertEqual((p["action"], p["id"], p.get("alt"), p.get("ask")), ("play", "tt1", "sosacd_1", ask))
            menu = self.streams_menu(li)
            self.assertEqual((menu["action"], menu["type"], menu["id"], menu["alt"]), ("streams", "movie", "tt1", "sosacd_1"))

    def test_ve_vypisu_nokturna_je_film_slozka_a_v_menu_vyber_dialogem(self):
        xbmcaddon.settings["stream_mode"] = "1"
        xbmc.cond_visible.add("Window.IsMedia")
        xbmc.info_labels["Container.PluginName"] = "plugin.video.nokturno"
        try:
            default.add_meta_item({"id": "tt1", "name": "Film", "year": 2020}, "movie", alt="sosacd_1")
        finally:
            xbmc.cond_visible.clear()
            xbmc.info_labels.clear()
        _h, url, li, is_folder = xbmcplugin.items[-1]
        self.assertTrue(is_folder, "klik ve výpisu otevře seznam streamů nativně")
        self.assertEqual((params_of(url)["action"], params_of(url)["id"]), ("streams", "tt1"))
        akce = dict(li.context).get("Vybrat stream a přehrát", "")
        self.assertTrue(akce.startswith("PlayMedia(plugin://plugin.video.nokturno/?"), akce)
        p = params_of(akce[len("PlayMedia("):-1])
        self.assertEqual((p["action"], p["id"], p["alt"], p["ask"]), ("play", "tt1", "sosacd_1", "1"))

    def test_rozkoukany_film_ve_vypisu_nokturna_je_prehratelny_ne_slozka(self):
        """2026-09-16: i v režimu „Vybrat ze seznamu streamů“ (folder_mode) musí titul
        s uloženou referencí streamu (Pokračovat ve sledování) přehrát rovnou tu, ne
        zase nabídnout celé hledání — jinak je celá zkratka k ničemu (Office, nahlášeno
        uživatelem: Pokračovat vždycky ukázalo „Načítám streamy“ a trvalo to dlouho)."""
        xbmcaddon.settings["stream_mode"] = "1"
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

    def test_dil_serie_prehratelny_s_id_serialu(self):
        xbmcaddon.settings["stream_mode"] = "1"
        meta = {"id": "tt9", "name": "Seriál", "videos": [{"season": 1, "episode": 1, "title": "Pilot"}]}
        with mock.patch.object(default, "meta_for", return_value=meta):
            default.list_episodes({}, "tt9", 1)
        _h, url, li, is_folder = xbmcplugin.items[-1]
        self.assertFalse(xbmcplugin.ended[-1]["cacheToDisc"], "widget a výpis sdílí adresu, položky se liší podle okna")
        self.assertFalse(is_folder)
        p = params_of(url)
        self.assertEqual((p["action"], p["type"], p["id"], p["series"], p["ask"]), ("play", "series", "tt9:1:1", "tt9", "1"))
        menu = self.streams_menu(li)
        self.assertEqual((menu["action"], menu["id"], menu["series"]), ("streams", "tt9:1:1", "tt9"))


class TestVyberStreamu(unittest.TestCase):
    """Klik v Nokturnu → seznam streamů jako složka; detail/widget/TMDb Helper → dialog s filtrem."""

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
        xbmcaddon.settings["stream_mode"] = "1"
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

        def select(heading, labels, *a, **k):
            volani.append(list(labels))
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

    def test_title_polozky_streamu_je_popis_streamu(self):
        """Arctic Fuse v některých zobrazeních kreslí ListItem.Title — dřív tam byl u všech streamů název filmu."""
        streams = [{"url": "ws:1", "label": "Matrix.1999.2160p.CZ.mkv", "detail": "47.8 GB", "source": "ws"},
                   {"url": "ws:2", "label": "Matrix.1999.1080p.EN.mkv", "detail": "9.1 GB", "source": "ws"}]
        with mock.patch.object(default, "load_meta", return_value=({"id": "tt1", "name": "Matrix", "year": 1999}, None)), \
             mock.patch.object(default, "collect_streams", return_value=streams), \
             mock.patch.object(default, "mark_viewed"):
            default.list_streams({}, "movie", "tt1")
        rows = [(url, li) for _h, url, li, folder in xbmcplugin.items if not folder]
        self.assertEqual(len(rows), 2)
        for _url, li in rows:
            titles = [c[1][0] for c in li.tag.calls if c[0] == "setTitle"]
            self.assertEqual(titles[-1], li.getLabel(), "poslední setTitle = popis streamu")
            self.assertNotEqual(titles[-1], "Matrix")

    def test_zpet_pri_nacitani_streamu_zrusi_vypis(self):
        """Zpět při interaktivním procházení menu Nokturna (`browsing_nokturno()`)
        musí hledání zrušit hned, ne nechat ukazatel průběhu viset."""
        xbmc.cond_visible.add("Window.IsMedia")
        xbmc.info_labels["Container.PluginName"] = default.ADDON_ID
        try:
            with mock.patch.object(default, "load_meta", return_value=({"id": "tt1", "name": "Film", "year": 2020}, None)), \
                 mock.patch.object(default, "collect_streams", return_value=[]), \
                 mock.patch.object(xbmcgui.DialogProgress, "iscanceled", return_value=True):
                default.list_streams({}, "movie", "tt1")
        finally:
            xbmc.cond_visible.clear()
            xbmc.info_labels.clear()
        self.assertEqual(len(xbmcplugin.ended), 1)
        self.assertFalse(xbmcplugin.ended[-1]["succeeded"], "zrušené hledání nesmí ukázat prázdný/chybový výpis")

    def test_vypis_streamu_mimo_menu_nokturna_nepouziva_modalni_dialog(self):
        """CLAUDE.md: „nikdy modální dialog v cestě, kterou může spustit widget nebo
        JSON-RPC" — mimo interaktivní procházení menu Nokturna (`browsing_nokturno()`
        false: widget, JSON-RPC) se modální `DialogProgress` vůbec nesmí použít,
        stejný důvod jako u `play()` (viz beta11 — TMDb Helper)."""
        with mock.patch.object(default, "load_meta", return_value=({"id": "tt1", "name": "Film", "year": 2020}, None)), \
             mock.patch.object(default, "collect_streams",
                               return_value=[{"url": "ws:1", "label": "Film.mkv", "source": "ws"}]), \
             mock.patch.object(xbmcgui, "DialogProgress") as modal, \
             mock.patch.object(xbmcgui, "DialogProgressBG") as bg:
            default.list_streams({}, "movie", "tt1")
        modal.assert_not_called()
        bg.assert_called_once()

    def test_prehrat_nad_slozkou_streamu_jde_do_vyberu_streamu(self):
        """Přehrát v info dialogu (Estuary, Arctic Fuse, TMDb Helper) nad titulem, který
        je v režimu seznamu složkou: Kodi vloží adresu složky do video playlistu a spustí
        skript s `action=streams` v režimu přehrání (čeká `setResolvedUrl`) s nastavenou
        `Playlist.Position`. Vybraná položka + adresa v playlistu + pozice → `play(ask=1)`
        místo výpisu (dvakrát na Office 2026-09-16: „položku se nepodařilo přehrát")."""
        url = "plugin://plugin.video.nokturno/?action=streams&id=tt0133093&type=movie"
        xbmc.info_labels["ListItem.FileNameAndPath"] = url
        xbmc.info_labels["Playlist.Position"] = "1"
        with mock.patch.object(xbmc, "executeJSONRPC",
                               return_value=json.dumps({"result": {"items": [{"file": url}]}})), \
             mock.patch.object(default, "get_apis", return_value={}), \
             mock.patch.object(default, "play") as play, \
             mock.patch.object(default, "list_streams") as listing:
            default.router("action=streams&type=movie&id=tt0133093")
        listing.assert_not_called()
        play.assert_called_once()
        self.assertEqual(play.call_args[0][1:3], ("movie", "tt0133093"))
        self.assertEqual(play.call_args[1]["ask"], "1")

    def test_klik_na_slozku_streamu_vypisuje_i_kdyz_je_vybrana_a_v_playlistu(self):
        """Obyčejný klik na tutéž (vybranou) položku poté, co uživatel výběr streamu z detailu
        zrušil (adresa složky zůstala v playlistu): `Playlist.Position` je prázdné → výpis.
        Busy dialog nerozhoduje — Kodi 21 ho ukazuje v obou režimech (beta13/14 na Office)."""
        url = "plugin://plugin.video.nokturno/?action=streams&id=tt0133093&type=movie"
        xbmc.info_labels["ListItem.FileNameAndPath"] = url
        xbmc.cond_visible.add("Window.IsActive(busydialog)")
        with mock.patch.object(xbmc, "executeJSONRPC",
                               return_value=json.dumps({"result": {"items": [{"file": url}]}})), \
             mock.patch.object(default, "get_apis", return_value={}), \
             mock.patch.object(default, "play") as play, \
             mock.patch.object(default, "list_streams") as listing:
            default.router("action=streams&type=movie&id=tt0133093")
        play.assert_not_called()
        listing.assert_called_once()

    def test_files_getdirectory_z_json_rpc_nad_vybranou_polozkou_vypisuje(self):
        """JSON-RPC `Files.GetDirectory` (HA, zahřívání) a vybraná položka v Kodi může
        náhodou být tatáž — adresa není v playlistu → výpis, bez modálního dialogu
        (CLAUDE.md)."""
        url = "plugin://plugin.video.nokturno/?action=streams&id=tt0133093&type=movie"
        xbmc.info_labels["ListItem.FileNameAndPath"] = url
        with mock.patch.object(xbmc, "executeJSONRPC",
                               return_value=json.dumps({"result": {"items": [
                                   {"file": "plugin://plugin.video.nokturno/?action=streams&id=tt1&type=movie"}]}})), \
             mock.patch.object(default, "get_apis", return_value={}), \
             mock.patch.object(default, "play") as play, \
             mock.patch.object(default, "list_streams") as listing:
            default.router("action=streams&type=movie&id=tt0133093")
        play.assert_not_called()
        listing.assert_called_once()

    def test_play_request_porovnava_parametry_ne_text_adresy(self):
        """Kodi (`CURL`) si parametry v adrese přeskládá abecedně a chybějící/prázdné
        `build_url` vynechává — porovnání musí být přes slovník parametrů."""
        xbmc.info_labels["ListItem.FolderPath"] = "plugin://plugin.video.nokturno/?type=movie&id=tt1&action=streams"
        xbmc.info_labels["Playlist.Position"] = "1"
        with mock.patch.object(xbmc, "executeJSONRPC", return_value=json.dumps({"result": {"items": [
                {"file": "plugin://plugin.video.nokturno/?id=tt1&action=streams&type=movie"}]}})):
            self.assertTrue(default.play_request({"action": "streams", "type": "movie", "id": "tt1", "alt": ""}))
            self.assertFalse(default.play_request({"action": "streams", "type": "movie", "id": "tt2"}))
        with mock.patch.object(xbmc, "executeJSONRPC", return_value="rozbité"):
            self.assertFalse(default.play_request({"action": "streams", "type": "movie", "id": "tt1"}))

    def test_mark_viewed_u_serialu_posila_nazev_serialu_ne_epizody(self):
        """2026-09-16: statistiky se serverem slučují podle normalizovaného názvu
        (`db.canonical_key`), takže skutečný (ne jen generický placeholder) název
        konkrétní epizody by rozštěpil sledovanost jednoho seriálu na tolik
        „titulů", kolik různých epizod se sledovalo. `mark_viewed` proto musí vždy
        dostat název seriálu, i když má epizoda vlastní netriviální název."""
        streams = [{"url": "ws:1", "label": "Lupin.S01E01.mkv", "detail": "1 GB", "source": "ws"}]
        meta = {"id": "tt123", "name": "Lupin", "_title": "Lupin", "year": 2021}
        video = {"title": "Skutečný název epizody, ne placeholder"}
        with mock.patch.object(default, "load_meta", return_value=(meta, video)), \
             mock.patch.object(default, "collect_streams", return_value=streams), \
             mock.patch.object(default, "mark_viewed") as mv:
            default.list_streams({}, "series", "tt123:1:2")
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
        self.assertEqual([params_of(u)["action"] for u in xbmcplugin.urls()], ["settings"])
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

    def setUp(self):
        reset_kodi()

    def test_thin_snapshot_pozna_prazdny_popis_i_fotku(self):
        self.assertTrue(default.thin_snapshot({"title": "X", "plot": "", "art": {}}))
        self.assertFalse(default.thin_snapshot({"title": "X", "plot": "Popis", "art": {}}))
        self.assertFalse(default.thin_snapshot({"title": "X", "plot": "", "art": {"poster": "http://p"}}))
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
        podle_katalogu = {params_of(u)["catalog"]: params_of(u)["action"] for u in xbmcplugin.urls()}
        self.assertEqual(podle_katalogu.get("popular"), "genres")
        self.assertEqual(podle_katalogu.get("top_rated"), "genres")


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
        stats = FakeStats(due=True)
        stats.last_message = {"id": 7, "text": "Nová verze je venku"}
        service.stats_tick(stats)
        self.assertEqual(len(xbmcgui.oks), 1)
        self.assertEqual(xbmcgui.oks[0][1], "Nová verze je venku")
        self.assertEqual(stats.seen, [7])

    def test_zprava_prijde_i_pri_vypnutych_statistikach(self):
        xbmcaddon.settings["stats_enabled"] = "false"
        stats = FakeStats(due=True)
        stats.last_message = {"id": 3, "text": "ahoj"}
        service.stats_tick(stats)
        self.assertEqual(stats.seen, [3])

    def test_bez_zpravy_se_nic_nezobrazi(self):
        stats = FakeStats(due=True)
        service.stats_tick(stats)
        self.assertEqual(xbmcgui.oks, [])
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

    def test_sluzba_rozklicuje_az_pri_stahovani(self):
        self.assertEqual(service.resolve_internal("https://cdn/a.mkv", default.STORE), ("https://cdn/a.mkv", {}))
        xbmcaddon.settings.update(dav1_url="http://nas.lan/dav/", dav1_username="u", dav1_password="p")
        link, headers = service.resolve_internal("dav:1:Filmy/a b.mkv", default.STORE)
        self.assertEqual(link, "http://nas.lan/dav/Filmy/a%20b.mkv")
        self.assertTrue(headers.get("Authorization", "").startswith("Basic "))
        with self.assertRaises(Exception):
            service.resolve_internal("dav:9:x", default.STORE)

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
