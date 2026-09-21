"""Stav účtů napříč zdroji — jedno místo pro menu v Kodi i senzor v HA.

Za větou „prostě mi to nejde" stojí několik různých příčin, které od sebe dnes
nejdou rozeznat: vypršelé předplatné WebShare, pauza HellSpy po 429, Luna, která
neběží, nebo účet bez Premium. Uživatel to pozná až z prázdného seznamu streamů.

Vzor je `luna_api.diagnose()`: vrací **kód příčiny a čísla, ne hotovou větu**.
Text si skládá každá větev sama — Kodi z jazykových řetězců (a v barvě, protože
✔/✘ kreslí fonty skinů jako prázdný proužek), HA z atributů senzoru.

Čtení a obnova jsou schválně dvě různé věci:

* `stav()` složí obraz z uloženého záznamu a **nesáhne na síť** — menu v Kodi ho
  čte při každém otevření a to se nesmí zpomalit.
* `zjisti()` se ptá po síti a patří na pozadí (služba v Kodi, časovač v HA).

Dotazy navíc jsou schválně drobné a vzácné: jeden dotaz na zdroj za `TTL`.
HellSpy se **nikdy neptá** — jeho pauza po 429 se jen přečte (`blocked_for`),
protože právě opakovanými dotazy si doplněk blokaci dvakrát přivodil (6.0.2,
6.0.4). Co jádro zjistí při běžné práci (selhaný login WebShare, 429 z HellSpy),
se do záznamu zapíše rovnou přes `Engine._note_account()` — zadarmo a čerstvěji
než jakákoli obnova na pozadí.
"""
import time

from hellspy_api import blocked_for
from prehrajto_api import blocked_for as prehrajto_blocked_for
from luna_api import diagnose as luna_diagnose

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"

#: Pořadí, v jakém se stav skládá do hlášky — nejdřív to, co uživatel platí.
SOURCES = ("luna", "webshare", "cztor", "fastshare", "sledujteto", "prehrajto", "hellspy", "storage")

STORE = "accounts"          # accounts.json v profilu
OFFLINE = "accounts_offline"   # značka „při poslední obnově nebyla síť" (jen `ts`)
OFFLINE_TTL = 3600             # jak dlouho značka platí; služba podle ní zkusí obnovu dřív
TTL = 12 * 3600             # jak dlouho platí uložený záznam; musí být delší než interval
                            # obnovy na pozadí (Kodi `ACCOUNTS_EVERY` 6 h), jinak by v menu
                            # stál stav trvale označený jako zastaralý
STALE_AFTER = 48 * 3600     # nad tím se záznam bere jako neznámý, ne jen starý
WARN_DAYS = 5               # kolik dní před koncem předplatného už varovat
LOW_CREDIT_GB = 1.0         # kredit FastShare pod tímhle je varování

#: Které kódy znamenají, že uživatel musí něco udělat. Menu podle toho mlčí, nebo se ozve.
BAD = (WARN, FAIL)


def _zaznam(level, code, **detail):
    return {"level": level, "code": code, "detail": detail}


def _dny_do(datum):
    """Kolik dní zbývá do data „YYYY-MM-DD" (záporně, když už bylo). None = nedává smysl."""
    text = str(datum or "")[:10]
    try:
        cil = time.mktime(time.strptime(text, "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return None
    dnes = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    return int(round((cil - dnes) / 86400.0))


# --- jednotlivé zdroje ------------------------------------------------------
#
# Každá funkce dostane hotového klienta a vrátí záznam. Výjimku nechytá — o to
# se stará `zjisti()`, aby měly všechny zdroje stejné chování při výpadku sítě.

def webshare(api, warn_days=WARN_DAYS):
    """VIP a kolik dní zbývá. `api` je přihlášený `WebshareApi`."""
    status = api.account_status()
    if not status.get("vip"):
        # účet bez VIP existuje a hledá, ale soubor nestáhne rychleji než pár kB/s
        return _zaznam(WARN, "free")
    days = int(status.get("days") or 0)
    detail = {"days": days, "until": str(status.get("until") or "")[:10]}
    if days <= 0:
        return _zaznam(FAIL, "expired", **detail)
    if days <= warn_days:
        return _zaznam(WARN, "expires_soon", **detail)
    return _zaznam(OK, "vip", **detail)


def fastshare(api, low_gb=LOW_CREDIT_GB):
    """Neomezený tarif, nebo kolik kreditu zbývá."""
    account = api.account()
    if account.get("unlimited"):
        return _zaznam(OK, "unlimited")
    gb = round(int(account.get("credit_mb") or 0) / 1024.0, 1)
    if gb <= 0:
        return _zaznam(FAIL, "no_credit", gb=0.0)
    return _zaznam(WARN if gb < low_gb else OK, "credit", gb=gb)


def cztor(client, warn_days=WARN_DAYS, deep=False):
    """Spárování a předplatné. `deep` = ověřit dotazem, jinak stačí poslední známý
    stav ze session (párování a obnovu tokenu dělá klient sám při běžné práci)."""
    if not client.paired():
        return _zaznam(FAIL, "not_paired")
    account = client.profile() if deep else client.account()
    if not account:
        return _zaznam(WARN, "unknown")
    detail = {"plan": str(account.get("plan") or ""), "until": str(account.get("valid_until") or "")[:10]}
    days = _dny_do(detail["until"])
    if not account.get("active") or (days is not None and days < 0):
        return _zaznam(FAIL, "expired", **detail)
    if days is not None:
        detail["days"] = days
        if days <= warn_days:
            return _zaznam(WARN, "expires_soon", **detail)
    return _zaznam(OK, "ok", **detail)


def sledujteto(api):
    """Bez Premium Sledujteto odkaz na přehrání nevydá — účet se přihlásí, ale streamy nehrají."""
    user = api.me()
    return _zaznam(OK, "premium") if user.get("is_premium") else _zaznam(WARN, "no_premium")


def prehrajto(api, cache=None):
    """Pauza po 429, pak Premium. Bez účtu je zdroj v pořádku, jen s méně výsledky
    a překódovaným souborem — proto `ok`, ne varování; `anonymous` to jen pojmenuje.

    Pauzu čte i bez účtu, stejně jako HellSpy: právě opakovanými dotazy si doplněk
    blokaci u HellSpy dvakrát přivodil (6.0.2, 6.0.4).
    """
    zbyva = int(prehrajto_blocked_for(cache))
    if zbyva > 0:
        return _zaznam(WARN, "paused", minutes=max(1, (zbyva + 59) // 60))
    if not (api.email and api.password):
        return _zaznam(OK, "anonymous")
    user = api.me()
    if not user.get("premium"):
        return _zaznam(WARN, "no_premium")
    days = int(user.get("days") or 0)
    if days and days <= WARN_DAYS:
        return _zaznam(WARN, "expires_soon", days=days)
    return _zaznam(OK, "premium", days=days)


def hellspy(cache=None):
    """Pauza po HTTP 429 — **bez jediného dotazu**, jen přečtení stavu."""
    zbyva = int(blocked_for(cache))
    if zbyva > 0:
        return _zaznam(WARN, "paused", minutes=max(1, (zbyva + 59) // 60))
    return _zaznam(OK, "ok")


def luna(base_url, token, deep=True, timeout=20):
    """Přeloží `luna_api.diagnose()` na stejný tvar jako ostatní zdroje."""
    result = luna_diagnose(base_url, token, timeout=timeout, deep=deep)
    return _zaznam(result["level"], result["code"],
                   version=result.get("version", ""), base=result.get("base", ""))


def storage(api):
    """Jeden PROPFIND na kořen — ověří adresu i heslo, strom se prochází až při hledání."""
    api.check()
    return _zaznam(OK, "ok", name=api.name, slot=api.slot)


# --- složení ----------------------------------------------------------------

def compose(saved, sources, now=None, ttl=TTL, stale_after=STALE_AFTER):
    """Uložené záznamy + které zdroje jsou vůbec nastavené → obraz pro rozhraní.

    Vrací seznam v pořadí `SOURCES`; každá položka má navíc `source`, `age`
    (sekundy od zjištění) a `stale` (záznam je starší než `ttl`). Nenastavený
    zdroj je `off` a do hlášky nepatří — uživatel ho vypnul schválně.
    """
    now = time.time() if now is None else now
    out = []
    for name in SOURCES:
        if not sources.get(name):
            out.append({"source": name, "level": OFF, "code": "off", "detail": {}, "age": None, "stale": False})
            continue
        rec = dict(saved.get(name) or {})
        ts = float(rec.get("ts") or 0)
        age = now - ts if ts else None
        if not rec.get("code") or (age is not None and age > stale_after):
            out.append({"source": name, "level": OFF, "code": "unknown", "detail": {}, "age": age, "stale": True})
            continue
        out.append({"source": name, "level": rec.get("level") or OK, "code": rec["code"],
                    "detail": dict(rec.get("detail") or {}), "age": age,
                    "stale": age is not None and age > ttl})
    return out


def worst(rows):
    """Nejzávažnější úroveň v seznamu — podle ní se barví souhrn."""
    for level in (FAIL, WARN):
        if any(r["level"] == level for r in rows):
            return level
    return OK


def problems(rows):
    """Jen to, co stojí za hlášku — v pořadí `SOURCES`, nejzávažnější napřed."""
    bad = [r for r in rows if r["level"] in BAD]
    bad.sort(key=lambda r: (r["level"] != FAIL, SOURCES.index(r["source"])))
    return bad
