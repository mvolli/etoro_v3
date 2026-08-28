"""Einordnung falscher yfinance_symbol-Zuordnungen.

Anlass 2026-08-28: Der Identity-Guard meldete "lokal 'CMI' != eToro 'CTRM'".
Die instrument_id war korrekt — falsch war das yfinance_symbol. 27 aktive
Instrumente holten dadurch Kurse eines FREMDEN Unternehmens:
HPQ -> HP (Helmerich & Payne), MANU -> MU (Micron), MOS -> MC (Moelis),
BGN.MI -> G.MI (Assicurazioni statt Banca Generali).

Die Tests sichern vor allem ab, was NICHT angefasst werden darf: ADRs, die
bewusst auf ihre Heimatboerse zeigen. Ein erster Entwurf ohne diese
Unterscheidung schlug 294 Korrekturen vor und haette SONY -> 6758.T,
SAN -> SAN.MC und HSBC -> HSBA.L zerstoert.
"""
import importlib.util
import pathlib

import pytest

_pfad = (pathlib.Path(__file__).resolve().parents[2]
         / "scripts" / "fix_yfinance_symbol_mismatches.py")
_spec = importlib.util.spec_from_file_location("yf_fix", _pfad)
yf_fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(yf_fix)


@pytest.mark.parametrize("sym, erwartet", [
    ("SDIPB.ST", "SDIP-B.ST"),      # eToro ohne, yfinance mit Bindestrich
    ("PLAZB.ST", "PLAZ-B.ST"),
    ("ERIC-A.ST", "ERIC-A.ST"),     # schon in yfinance-Form
    ("BIFB.CO", "BIF-B.CO"),
    ("HPQ", "HPQ"),                 # ohne Boersensuffix unveraendert
    ("BGN.MI", "BGN.MI"),           # .MI ist keine Bindestrich-Boerse
])
def test_vorschlag(sym, erwartet):
    assert yf_fix._vorschlag(sym) == erwartet


@pytest.mark.parametrize("sym", [
    "CC_1510", "AI_2878", "OPC_13635",   # <Kuerzel>_<id> aus alten Resolves
    "SGE.old", "MRK.DE_old",
    "IBCC.DE11",                          # Ziffern hinter dem Suffix
])
def test_platzhalter_erkannt(sym):
    assert yf_fix._ist_platzhalter(sym), f"{sym} muesste als Platzhalter gelten"


@pytest.mark.parametrize("sym", ["HPQ", "ERIC-A.ST", "BGN.MI", "AIV", "2BTC.DE"])
def test_echte_ticker_sind_keine_platzhalter(sym):
    assert not yf_fix._ist_platzhalter(sym)


@pytest.mark.parametrize("sym, gattung", [
    ("ERIC-A.ST", "A"), ("ERIC-B.ST", "B"),
    ("SEBC.ST", "C"), ("VOLV-A.ST", "A"),
    ("HPQ", None), ("BGN.MI", None),
])
def test_gattung(sym, gattung):
    g = yf_fix._gattung(sym)
    assert (g[1] if g else None) == gattung


def test_gattungen_bleiben_unterscheidbar():
    """A- und B-Aktien duerfen nie zusammenfallen — andere Kurse, andere Rechte."""
    assert yf_fix._vorschlag("ERICA.ST") != yf_fix._vorschlag("ERICB.ST")
