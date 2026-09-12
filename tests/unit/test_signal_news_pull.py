"""feat/signal-news-pull (2026-09-12): synchroner News-Check im Kaufpfad.

Gemessen gegen die Live-DB: 93,4 % der 211 Epochen-Trades liefen auf
Signalen, die NACH dem letzten stuendlichen news_flags_worker-Lauf geboren
wurden. Der Abstand von Signalgeburt zu Freigabe betraegt im Schnitt
3,0 Minuten — Signal und Kauf fallen in denselben 15-Minuten-Zyklus. Der
stuendliche Worker kann ein Symbol vor seinem ERSTEN Kauf strukturell nicht
sehen; fix/news-candidate-floor hilft nur den 6,6 %, die eine Stunde
ueberleben.

Der Pull laeuft auf dem Geld-Pfad, deshalb sind die Grenzen hier die
eigentliche Testsubstanz: hartes Wall-Clock-Budget, Fail-open in jeder
Richtung, und eine Verschmelzung, die nur verschaerfen kann.
"""
from __future__ import annotations

import time

import pytest

from bot.workers.news_flags_worker import (
    FLAG_RANG, pull_regel_flags, staerkeres_flag,
)


AVOID = {"flag": "AVOID", "severity": "HIGH", "reason": "Earnings am 2026-09-13",
         "source": "earnings_calendar"}
CAUTION = {"flag": "CAUTION", "severity": "MEDIUM", "reason": "ueber Kursziel",
           "source": "analyst_target"}


def _entries(*syms):
    return [{"symbol": s, "yf": s} for s in syms]


# ── Verschmelzung: nur verschaerfen ──────────────────────────────────────────

def test_avoid_verdraengt_caution():
    assert staerkeres_flag(CAUTION, AVOID) is AVOID


def test_caution_schwaecht_ein_bestehendes_avoid_nicht_ab():
    """Der Kernfall: ein frisches AVOID aus dem stuendlichen Lauf darf durch
    einen schwaecheren Pull-Treffer nicht verlorengehen."""
    assert staerkeres_flag(AVOID, CAUTION) is AVOID


def test_leerer_pull_laesst_bestehendes_flag_stehen():
    assert staerkeres_flag(AVOID, None) is AVOID
    assert staerkeres_flag(None, None) is None


def test_bei_gleichstand_gewinnt_das_bestehende():
    """Der stuendliche Lauf kennt zusaetzlich die Headline-Bewertung."""
    stuendlich = dict(CAUTION, reason="Headline: Untersuchung laeuft")
    assert staerkeres_flag(stuendlich, CAUTION) is stuendlich


def test_es_gibt_keinen_rang_ueber_avoid():
    """Asymmetrie-Schutz: ein halluziniertes 'BUY' kann nichts verstaerken."""
    assert FLAG_RANG.get("BUY", 0) == 0
    assert staerkeres_flag(AVOID, {"flag": "BUY"}) is AVOID


# ── Der Pull selbst ──────────────────────────────────────────────────────────

def test_earnings_treffer_spart_den_zweiten_call():
    """Earnings ist bereits AVOID — der teurere Analysten-Call entfaellt."""
    gerufen = []
    flags, ab = pull_regel_flags(
        _entries("AAPL"),
        _earnings=lambda y: AVOID,
        _analyst=lambda y: gerufen.append(y) or CAUTION,
    )
    assert flags == {"AAPL": AVOID}
    assert gerufen == [], "Analysten-Call haette entfallen muessen"
    assert ab is False


def test_ohne_earnings_wird_das_kursziel_geprueft():
    flags, _ = pull_regel_flags(
        _entries("AAPL"), _earnings=lambda y: None, _analyst=lambda y: CAUTION)
    assert flags == {"AAPL": CAUTION}


def test_symbole_ohne_treffer_erscheinen_nicht():
    flags, _ = pull_regel_flags(
        _entries("A", "B"), _earnings=lambda y: None, _analyst=lambda y: None)
    assert flags == {}


def test_leere_kandidatenliste():
    assert pull_regel_flags([]) == ({}, False)


def test_yf_symbol_wird_bevorzugt_der_key_bleibt_das_bot_symbol():
    """Der Bot kennt VU.PA, yfinance braucht seinen eigenen Ticker."""
    gesehen = []
    flags, _ = pull_regel_flags(
        [{"symbol": "VU.PA", "yf": "VIE.PA"}],
        _earnings=lambda y: gesehen.append(y) or AVOID,
        _analyst=lambda y: None)
    assert gesehen == ["VIE.PA"]
    assert list(flags) == ["VU.PA"]


# ── Die harte Grenze: Wall-Clock ─────────────────────────────────────────────

def test_zeitbudget_bricht_ab_und_meldet_es():
    """Ein try/except je Symbol begrenzt die GESAMTlatenz nicht. Eine
    haengende yfinance-Verbindung sitzt 30 s, der signal_worker hat bis zum
    Execution-Slot nur 180 s."""
    def langsam(_y):
        time.sleep(0.05)
        return AVOID
    flags, ab = pull_regel_flags(
        _entries(*[f"S{i}" for i in range(20)]),
        budget_s=0.12, _earnings=langsam, _analyst=lambda y: None)
    assert ab is True, "Abbruch haette gemeldet werden muessen"
    assert 0 < len(flags) < 20, f"Teilergebnis erwartet, war {len(flags)}"


def test_budget_null_prueft_kein_symbol():
    flags, ab = pull_regel_flags(
        _entries("A", "B"), budget_s=0.0,
        _earnings=lambda y: AVOID, _analyst=lambda y: None)
    assert (flags, ab) == ({}, True)


def test_gefundene_flags_ueberleben_den_abbruch():
    """Ein Teil-Pull darf nicht alles verwerfen — was geprueft wurde, zaehlt."""
    n = {"i": 0}
    def zaehlend(_y):
        n["i"] += 1
        if n["i"] > 1:
            time.sleep(0.2)
        return AVOID
    flags, ab = pull_regel_flags(
        _entries("A", "B", "C"), budget_s=0.1,
        _earnings=zaehlend, _analyst=lambda y: None)
    assert ab is True
    assert "A" in flags


# ── Fail-open ────────────────────────────────────────────────────────────────

def test_ein_kaputtes_symbol_reisst_die_anderen_nicht_mit():
    """Abgefangen wird JE SYMBOL, nicht nur um den ganzen Pull herum.

    Sonst haette ein einziges kaputtes Instrument die Flags aller uebrigen
    Kandidaten mitgerissen — und die waeren ungeprueft gekauft worden.
    """
    def manchmal_kaputt(y):
        if y == "B":
            raise RuntimeError("yfinance down")
        return AVOID
    flags, ab = pull_regel_flags(
        _entries("A", "B", "C"), _earnings=manchmal_kaputt,
        _analyst=lambda y: None)
    assert flags == {"A": AVOID, "C": AVOID}
    assert ab is False


def test_eintrag_ohne_symbol_wird_uebersprungen():
    flags, ab = pull_regel_flags(
        [{"yf": "AAPL"}, {"symbol": "MSFT", "yf": "MSFT"}],
        _earnings=lambda y: AVOID, _analyst=lambda y: None)
    assert flags == {"MSFT": AVOID}
    assert ab is False
