"""Krátký popis selhaného zdroje pro uživatele — upozornění v Kodi, karta HA.

Když jeden zdroj selže, hledá se dál v ostatních a uživatel dostane jen
upozornění, který zdroj se přeskočil. Chyby knihoven ale nesou celou adresu
dotazu — u Luny i s tokenem v cestě — a ta do upozornění nepatří, jen do logu.
Síťový výpadek (vypnutý addon, spadlý server, DNS) se hlásí jednotně
„<zdroj> neodpovídá", ostatní chyby zkráceným textem.
"""
import re

_URL_RE = re.compile(r"\s*\((?:https?|ftp)://[^)]*\)|https?://\S+")
_OFFLINE = (
    "connection refused", "connection reset", "timed out", "timeout", "no route to host",
    "network is unreachable", "name or service not known", "temporary failure in name resolution",
    "nodename nor servname", "remote end closed", "errno 111", "errno 113", "errno 101",
    "errno -2", "errno -3", "http 502", "http 503", "http 504", "bad gateway", "service unavailable",
)
MAX_LEN = 90


def describe_failure(label, err):
    """„Luna neodpovídá" / „WebShare: login: Wrong password" — bez adres a tokenů."""
    text = " ".join(_URL_RE.sub("", str(err or "")).split())
    if not text or any(k in text.lower() for k in _OFFLINE):
        return f"{label} neodpovídá"
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN - 1].rstrip() + "…"
    return f"{label}: {text}"


def summarize(failures):
    """[(zdroj, chyba)] → unikátní řádky v pořadí, jak přišly."""
    lines = []
    for label, err in failures:
        line = describe_failure(label, err)
        if line not in lines:
            lines.append(line)
    return lines
