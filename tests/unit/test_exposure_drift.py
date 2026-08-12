"""Unit tests fuer fix/exposure-drift-monitor (2026-08-12).

check_exposure_gate ist ein reines PRE-Trade-Gate — nach dem Einstieg pruefte
NICHTS das Gesamt-Exposure erneut. Dieselbe blinde Stelle, fuer die
check_asset_class_violations eine Ebene tiefer gebaut wurde.

Die Drift-Richtung ist kontraintuitiv: `amount` ist eingesetztes Kapital, nicht
Marktwert — Exposure-% steigt also, wenn die EQUITY FAELLT. Der Live-Fall am
2026-08-12: $10.000 -> $8.668 Equity bei ~konstant $7.100 investiert ergab
71% -> 81.9% ohne einen einzigen neuen Kauf.
"""
from __future__ import annotations

from bot.core.concentration_monitor import check_total_exposure_drift


def _pos(*amounts: float) -> list[dict]:
    return [{"amount": a} for a in amounts]


def test_innerhalb_des_caps_kein_befund():
    assert check_total_exposure_drift(_pos(1000, 2000), equity=10_000.0,
                                      max_exposure_pct=75.0) is None


def test_genau_am_cap_kein_befund():
    """75.0% ist noch erlaubt — nur ECHTES Ueberschreiten meldet."""
    assert check_total_exposure_drift(_pos(7500.0), equity=10_000.0,
                                      max_exposure_pct=75.0) is None


def test_ueber_cap_meldet_mit_kennzahlen():
    d = check_total_exposure_drift(_pos(4000, 4190), equity=10_000.0,
                                   max_exposure_pct=75.0)
    assert d is not None
    assert round(d["actual_pct"], 1) == 81.9
    assert round(d["breach_pct"], 1) == 6.9
    assert round(d["excess_amount"], 0) == 690.0
    assert d["position_count"] == 2
    assert d["severity"] == "WARNING"


def test_drift_entsteht_durch_fallende_equity_ohne_neuen_kauf():
    """Der eigentliche Mechanismus: Investment konstant, Equity faellt.

    Das ist der Grund, warum ein reines Pre-Trade-Gate hier nicht reicht —
    der Cap wird ueberschritten, ohne dass je ein Gate ausgeloest wurde.
    """
    invested = _pos(7100.0)
    assert check_total_exposure_drift(invested, 10_000.0, 75.0) is None   # 71.0%
    drift = check_total_exposure_drift(invested, 8_668.0, 75.0)           # 81.9%
    assert drift is not None
    assert drift["actual_pct"] > 75.0


def test_equity_null_meldet_nichts_statt_zu_dividieren():
    assert check_total_exposure_drift(_pos(100.0), equity=0.0,
                                      max_exposure_pct=75.0) is None


def test_cap_null_deaktiviert_den_check():
    assert check_total_exposure_drift(_pos(9999.0), equity=10_000.0,
                                      max_exposure_pct=0.0) is None


def test_leeres_portfolio_meldet_nichts():
    assert check_total_exposure_drift([], equity=10_000.0,
                                      max_exposure_pct=75.0) is None


def test_fehlende_und_kaputte_amounts_zaehlen_als_null():
    d = check_total_exposure_drift(
        [{"amount": 8000.0}, {}, {"amount": None}], equity=10_000.0,
        max_exposure_pct=75.0,
    )
    assert d is not None
    assert d["total_amount"] == 8000.0
