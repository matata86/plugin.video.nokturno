"""Které zdroje má instalace v nastavení aktivní — do statistik.

Jeden výčet pro obě cesty: ruční odeslání z nastavení (`default.stats_sources`)
i hlášení služby na pozadí (`service.stats_context`). Dřív měla každá svůj
a rozešly se — Přehraj.to a vlastní úložiště přibyly jen do jádra, takže je od
7.0.0 nehlásila ani jedna (a dashboard je do 7.4.2 ani neznal a zahazoval).

`get(key)` dodává volající: plugin čte nastavení přes `ADDON.getSetting`,
služba přes vlastní instanci `xbmcaddon.Addon()`.

Posílají se jen klíče — žádné účty, hesla ani adresy úložišť.
"""

# Klíče musí sedět s `SOURCE_KEYS` v dashboardu (`Dashboard/backend/stats.py`),
# jinak je server při zápisu zahodí, a s `Engine.sources()` v jádru, odkud je
# berou HA a Stremio. Prowlarr (`torrent`) Kodi nemá.
SOURCE_KEYS = ["luna", "sosac", "webshare", "hellspy", "sledujteto", "fastshare",
               "prehrajto", "cztor", "storage", "tmdb", "trakt"]


def stats_sources(get):
    def zapnuto(key, default="true"):
        return (get(key) or default) != "false"

    def vyplneno(key):
        return bool((get(key) or "").strip())

    return [name for name, active in (
        ("luna", zapnuto("luna_enabled") and vyplneno("token")),
        ("sosac", zapnuto("sosac_enabled") and vyplneno("streamuj_username")),
        ("webshare", zapnuto("ws_enabled", "false") and vyplneno("ws_username")),
        ("hellspy", zapnuto("hs_enabled", "false")),
        ("sledujteto", zapnuto("st_enabled", "false") and vyplneno("st_email")),
        ("fastshare", zapnuto("fs_enabled", "false") and vyplneno("fs_username")),
        # Přehraj.to hledá i bez účtu (HTML záloha), stačí tedy přepínač — jako HellSpy.
        ("prehrajto", zapnuto("pt_enabled", "false")),
        ("cztor", zapnuto("cz_enabled", "false")),
        ("storage", any(vyplneno("dav%d_url" % n) and zapnuto("dav%d_enabled" % n) for n in (1, 2, 3))),
        ("tmdb", vyplneno("tmdb_api_key")),
        ("trakt", zapnuto("trakt_enabled", "false")),
    ) if active]
