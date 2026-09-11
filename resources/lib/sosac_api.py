"""Společné kousky kolem Sosáče: rozpoznání staršího ID a porovnávání názvů.

Klient starého Stremio rozhraní Sosáče (`stremio.sosac.tv`) je pryč — Sosáč se
čte z veřejných exportů (`sosac_direct`) a k přehrání stačí účet Streamuj, takže
userId ani login k Sosáči nemají co dělat. Zůstává tu jen to, co používá zbytek
doplňku: `SosacError`, porovnání názvů a poznání starých ID (`sosac2_…`), aby
tituly uložené ze starších verzí nepadly do špatného zdroje.
"""
import re
import unicodedata

ID_PREFIX = "sosac2_"   # ID starého Stremio rozhraní, dnes jen v uložených datech


def is_sosac_id(item_id):
    return str(item_id or "").startswith(ID_PREFIX)


ARTICLES = ("the ", "a ", "an ")


def normalize(text):
    """Bez diakritiky, malá písmena, jen alfanumerika, bez úvodního členu – pro porovnání názvů.

    TMDB (Luna) má „Matrix“, Sosáč „The Matrix“ – bez odstranění členu by se nepotkaly.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    for art in ARTICLES:
        if text.startswith(art):
            text = text[len(art):]
            break
    return text


def names_match(a, b):
    na, nb = normalize(a), normalize(b)
    return bool(na) and na == nb


class SosacError(Exception):
    pass
