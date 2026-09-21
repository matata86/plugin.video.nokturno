"""Klíče nastavení a výchozí hodnoty, které čte `engine.Engine`.

Zdroj pravdy pro všechny tři konzumenty jádra. Integrace pro Home Assistant si
tenhle soubor vtahuje do svého `const.py` a doplňuje k němu vlastní symboly
(`DOMAIN`, `SERVICE_*`, `SIGNAL_*`, `EVENT_*`), které se jádra netýkají.

Pozor: `engine.py` dnes většinu klíčů čte řetězcem, ne přes tyhle konstanty.
Sjednotit to je úklid navíc, ne součást vytažení jádra — hodnoty tady proto
musí zůstat přesně takové, jaké řetězce engine používá.
"""

# --- účty zdrojů ---------------------------------------------------------
CONF_LUNA_URL = "luna_url"
CONF_LUNA_TOKEN = "luna_token"
CONF_WS_USER = "ws_username"
CONF_WS_PASS = "ws_password"
CONF_STREAMUJ_USER = "streamuj_username"
CONF_STREAMUJ_PASS = "streamuj_password"
CONF_TMDB_KEY = "tmdb_api_key"
CONF_HS_ENABLED = "hs_enabled"   # HellSpy je veřejný, stačí přepínač
CONF_ST_EMAIL = "st_email"       # Sledujteto — hledání chce účet, přehrávání Premium
CONF_ST_PASS = "st_password"
CONF_FS_USER = "fs_username"     # FastShare — hledání bez účtu, přehrávání z kreditu nebo tarifu
CONF_FS_PASS = "fs_password"
CONF_PT_ENABLED = "pt_enabled"   # Přehraj.to — přepínač; bez účtu jen první strana a překódování
CONF_PT_EMAIL = "pt_email"       # s Premium účtem stránkování a původní soubor
CONF_PT_PASS = "pt_password"
CONF_CZ_ENABLED = "cz_enabled"   # CZtor — přepínač; účet se páruje PINem, tokeny drží úložiště jádra

# --- torrenty ------------------------------------------------------------
CONF_PROWLARR_URL = "prowlarr_url"
CONF_PROWLARR_KEY = "prowlarr_key"
CONF_QBIT_URL = "qbit_url"
CONF_QBIT_USER = "qbit_username"
CONF_QBIT_PASS = "qbit_password"

# --- předvolby streamů ---------------------------------------------------
CONF_PREF_LANG = "pref_lang"
CONF_PREF_SURROUND = "pref_surround"
CONF_HIDE_SD = "hide_sd"
CONF_MAX_BITRATE = "max_bitrate_mbps"
CONF_SORT = "sort_streams"

# --- stahování a odkazy ven ----------------------------------------------
CONF_DOWNLOAD_DIR = "download_dir"
CONF_EXTERNAL_HOST = "external_host"

# --- povolené hodnoty ----------------------------------------------------
LANGS = ["", "CZ", "SK", "EN", "HU"]
SORT_ORDERS = ["source", "quality", "size_desc", "size_asc"]

DEFAULT_LUNA_URL = "http://192.168.1.10:7126"
DEFAULT_PROWLARR_URL = "http://192.168.1.10:9696"
DEFAULT_QBIT_URL = "http://192.168.1.10:9091"
DEFAULT_DOWNLOAD_DIR = "/media/nokturno"
DEFAULT_SORT = "size_desc"  # 2026-09-13: nové instalace řadí streamy podle velikosti, ne kvality
