"""Přerušení rozdělané práce na žádost hostitele.

Kodi při `Application.Quit` čeká, až doběhnou všechny skripty doplňků — a dlouhé
smyčky jádra (hledání napříč zdroji, čtení hlaviček, průchod úložiště, přepočet
katalogu s CZ dabingem po titulech) dřív běžely dál, dokud nedoběhly samy. Na TV
boxu to znamenalo vypínání trvající minuty (Office 2026-09-16: přes 2 minuty).

Jádro o Kodi nic neví (a nesmí — hlídá test), takže dostane jen zavolatelný
`should_stop()`; doplněk pro Kodi mu podstrčí `xbmc.Monitor().abortRequested`
(plus vlastní příznak zrušení dialogem), HA a Stremio nic — tam se nikdy
nepřeruší nic.

`Aborted` dědí z `BaseException`, ne z `Exception`, stejně jako `KeyboardInterrupt`:
jádro má na desítkách míst „výpadek jednoho zdroje nesmí shodit ostatní"
(`except Exception`), a přerušení má naopak projít úplně vším až ven k hostiteli,
který ví, jak skončit (Kodi: zavřít handle bez hlášky). Výsledek přerušené práce
se nikdy necachuje — výjimka projde i `Store.cached_if()` dřív, než zapíše.
"""
from concurrent.futures import FIRST_COMPLETED, wait

STOP_POLL = 1.0   # s – jak často se při čekání na vlákna ptát should_stop()


class Aborted(BaseException):
    """Hostitel končí (nebo uživatel zrušil) — rozdělaná práce se zahazuje."""

    def __init__(self, message="Přerušeno na žádost hostitele."):
        super().__init__(message)


def never():
    """Výchozí `should_stop` — nikdy nepřerušit (HA, Stremio, testy bez hostitele)."""
    return False


def check(should_stop):
    """Vyhodí `Aborted`, když `should_stop()` říká, že je čas skončit."""
    if should_stop is not None and should_stop():
        raise Aborted()


def gather(pool, futures, should_stop, on_done=None, poll=STOP_POLL):
    """Počká na všechny `futures` z `pool` a mezi tím se každou `poll` sekundu
    ptá `should_stop()`. Nahrazuje `with ThreadPoolExecutor(...)` + `as_completed()`,
    které čekaly na úplně všechno bez možnosti přestat.

    Při přerušení zruší, co ještě ani nezačalo (`Future.cancel()`), executor
    zavře BEZ čekání na rozběhnutá vlákna (ta doběhnou sama na vlastní timeout —
    síťové volání ve stdlib zvenčí přerušit nejde) a vyhodí `Aborted`. Bez
    přerušení executor zavře normálně a vrátí `futures` v původním pořadí,
    všechny hotové. `on_done(future)` se volá za každou dokončenou, v pořadí
    dokončení — pro ukazatele průběhu. `pool.shutdown(cancel_futures=)` je až
    od Pythonu 3.9, Kodi 20 má 3.8, proto se ruší ručně.
    """
    pending = set(futures)
    try:
        while pending:
            done, pending = wait(pending, timeout=poll, return_when=FIRST_COMPLETED)
            for future in done:
                if on_done:
                    on_done(future)
            if pending and should_stop is not None and should_stop():
                for future in pending:
                    future.cancel()
                raise Aborted()
    except BaseException:
        pool.shutdown(wait=False)
        raise
    pool.shutdown(wait=True)
    return list(futures)
