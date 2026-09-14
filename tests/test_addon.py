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
import shutil
import sys
import tempfile
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
        self.assertEqual(lines[0][0], self.version.split("~")[0])

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
                                  dav1_url="http://nas.lan/dav/", dav1_username="u", dav1_password="p")
        engine = default.KodiEngine()
        self.assertIsNone(engine.ws, "WebShare vypnutý přepínačem, i když je účet vyplněný")
        self.assertIsNotNone(engine.hs)
        self.assertIsNotNone(engine.st)
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
                        meta_video=None):
            failures.append(("Luna", ConnectionRefusedError("[Errno 111] Connection refused")))
            failures.append(("WebShare", WebshareError("login: Wrong password")))
            on_progress(3, 8)
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
        bar.update.assert_called_with(int(3 / 8 * 100))
        # chyba jádra v hlášce nese zdroj sama
        self.assertEqual(default.describe_error(default.NokturnoError("WebShare: soubor není")), "WebShare: soubor není")
        self.assertEqual(default.error_label(default.NokturnoError("Chybí odkaz na stream.")), "Nokturno")

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

    def test_popisek_s_odhadnutou_kvalitou_z_jadra(self):
        s = {"url": "ws:1", "label": "Film.mkv", "detail": "9 GB", "source": "ws", "quality_rank": 4, "_estimated": True}
        self.assertIn("~4K", default.stream_label(s))
        s = {"url": "ws:1", "label": "Film.mkv", "detail": "9 GB", "source": "ws", "quality_rank": 4}
        self.assertNotIn("~", default.stream_label(s).split("[/B]")[0])


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
        return {(p["src"], p["catalog"], p["type"], p.get("genre")) for p in out if p["action"] == "catalog"}

    def warm(self):
        return {(p["src"], p["catalog"], p["type"], p.get("genre")) for p in map(params_of, service.warm_urls())}

    def test_s_klicem_tmdb(self):
        xbmcaddon.settings["tmdb_api_key"] = "abc"
        menu = self.browse({"tmdb": object(), "sosac_db": object(), "luna": None, "cinemeta": None})
        self.assertTrue(self.warm() <= menu, self.warm() - menu)
        self.assertIn(("sosac_db", "moviesrecentlyadded_dub", "movie", None), self.warm())
        self.assertIn(("sosac_db", "moviesrecentlyadded_subs", "movie", None), self.warm())
        self.assertIn(("sosac_db", "tvshowsrecentlyadded", "series", None), self.warm())

    def test_zahrivani_bere_aktualni_nastaveni(self):
        """Modulový ADDON služby nevidí změny — po zadání klíče TMDB se dál zahřívala Luna."""
        xbmcaddon.settings["token"] = "t"
        self.assertEqual({p["src"] for p in map(params_of, service.warm_urls())}, {"luna", "sosac_db"})
        xbmcaddon.settings["tmdb_api_key"] = "abc"
        self.assertEqual({p["src"] for p in map(params_of, service.warm_urls())}, {"tmdb", "sosac_db"})

    def test_s_lunou(self):
        xbmcaddon.settings["token"] = "t"
        menu = self.browse({"tmdb": None, "sosac_db": object(), "luna": object(), "cinemeta": None})
        self.assertTrue(self.warm() <= menu, self.warm() - menu)

    def test_bez_luny_i_tmdb_zahriva_jen_sosac(self):
        xbmcaddon.settings["luna_enabled"] = "false"
        self.assertEqual({p["src"] for p in map(params_of, service.warm_urls())}, {"sosac_db"})

    def test_nove_dily_jen_u_serialu_a_nove_filmy_jen_u_filmu(self):
        menu = self.browse({"tmdb": None, "sosac_db": object(), "luna": None, "cinemeta": object()})
        self.assertIn(("sosac_db", "tvshowsrecentlyadded", "series", None), menu)
        self.assertNotIn(("sosac_db", "tvshowsrecentlyadded", "movie", None), menu)
        self.assertNotIn(("sosac_db", "moviesrecentlyadded_dub", "series", None), menu)
        # bez TMDB i Luny drží Populární Cinemeta
        self.assertIn(("cinemeta", "top", "movie", None), menu)


class FakeStats:
    def __init__(self, due=True):
        self.uses, self.plays, self.sent = [], [], []
        self._due = due

    def note_use(self, ts):
        self.uses.append(ts)

    def note_play(self, key, title, year, kind):
        self.plays.append((key, title, year, kind))

    def due(self):
        return self._due

    def send(self, url, **kwargs):
        self.sent.append((url, kwargs))
        return True, ""


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
        for lib in ("hellspy_api", "sledujteto_api", "storage_api", "mediainfo", "sk_SK", "tests/"):
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
