#!/usr/bin/env python3
"""Unit tests — feat/partial-min-tranche in der Entscheidungsstufe (2026-10-16).

Die Mindest-Tranche muss in evaluate_trailing() greifen, nicht erst in
execute_trailing_actions(): dazwischen ruft der risk_worker fuer jede
PARTIAL_CLOSE-/MOMENTUM_FADE-Action den KI-Profit-Advisor (LLM-Call). Eine
erst danach verworfene Stufe bliebe faellig und kostete jeden 5-min-Zyklus
einen Call ohne Order (Muster aus fix/ladder-regime-decision-stage).
"""
from __future__ import annotations

import pytest

from bot.core import partial_close_policy as pcp
from bot.core.trailing_stop import evaluate_trailing, save_profit_levels
from bot.db.connection import DB


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    import bot.core.trailing_stop as _ts
    monkeypatch.setattr(_ts, "FULL_EXIT_ENABLED", False)
    monkeypatch.setattr(_ts, "MOMENTUM_FADE_ENABLED", True)
    monkeypatch.setattr(_ts, "MOMENTUM_ARM_PCT", 2.0)
    monkeypatch.setattr(_ts, "MOMENTUM_RETRACE_FRAC", 0.40)
    monkeypatch.setattr(_ts, "MOMENTUM_MIN_LOCK_PCT", 1.0)
    monkeypatch.setattr(_ts, "MOMENTUM_MAX_RETRACE_ABS", 999.0)
    monkeypatch.setattr(_ts, "MOMENTUM_FADE_CLOSE_PCT", 25.0)
    monkeypatch.setattr(_ts, "MIN_REMAINING_PCT", 50.0)
    monkeypatch.setattr(_ts, "STALE_EXIT_ENABLED", False)
    yield
    pcp._config_cache = None


def _min_tranche(monkeypatch, value):
    monkeypatch.setattr(pcp, "_config_cache", dict(pcp.DEFAULT_CONFIG, min_tranche_usd=value))


@pytest.fixture
def db(tmp_path):
    return DB(db_path=tmp_path / "trading.db")


def _pos(pnl_pct, amount=1000.0):
    return {"positionID": "p1", "symbol": "ZM", "instrumentID": 42, "amount": amount,
            "openRate": 100.0, "unrealizedPnL": {"pnL": amount * pnl_pct / 100.0}}


def _seed(db, rung=5.0, close_pct=25.0, peak=5.5, remaining=1.0):
    save_profit_levels(db, "p1", "ZM", [{"threshold": rung, "close_pct": close_pct}])
    db.execute("UPDATE position_state SET peak_pnl_pct = ?, remaining_frac = ?, "
               "momentum_faded = 0 WHERE position_id = 'p1'", (peak, remaining))


# ── Default 0: Verhalten unveraendert ────────────────────────────────────────
def test_default_aus_leiter_feuert(db, monkeypatch):
    _min_tranche(monkeypatch, 0.0)
    _seed(db)
    assert [a.action for a in evaluate_trailing([_pos(5.5)], regime="NORMAL", db=db)] == ["PARTIAL_CLOSE"]


def test_default_aus_fade_feuert(db, monkeypatch):
    _min_tranche(monkeypatch, 0.0)
    _seed(db, rung=20.0, peak=10.0)
    assert [a.action for a in evaluate_trailing([_pos(5.5)], regime="NORMAL", db=db)] == ["MOMENTUM_FADE"]


# ── Leiter-Stufe ─────────────────────────────────────────────────────────────
def test_kleine_leiterstufe_erzeugt_keine_action(db, monkeypatch):
    # 25 % von $1000 = $250 < $300 -> keine Action, also auch kein LLM-Call
    _min_tranche(monkeypatch, 300.0)
    _seed(db)
    assert evaluate_trailing([_pos(5.5)], regime="NORMAL", db=db) == []


def test_kein_durchfallen_auf_fade(db, monkeypatch):
    # Stufe faellig + Peak weit oben: blockierte Stufe darf nicht zum Fade werden
    _min_tranche(monkeypatch, 300.0)
    _seed(db, peak=10.0)
    assert evaluate_trailing([_pos(5.5)], regime="NORMAL", db=db) == []


def test_grosse_leiterstufe_feuert(db, monkeypatch):
    _min_tranche(monkeypatch, 200.0)   # $250 >= $200
    _seed(db)
    assert [a.action for a in evaluate_trailing([_pos(5.5)], regime="NORMAL", db=db)] == ["PARTIAL_CLOSE"]


def test_full_exit_umschlag_nie_unterdrueckt(db, monkeypatch):
    # Restanteil 50 % -> Stufe wird FULL_EXIT; Mindest-Tranche greift nicht
    _min_tranche(monkeypatch, 10_000.0)
    _seed(db, remaining=0.5)
    assert [a.action for a in evaluate_trailing([_pos(5.5)], regime="NORMAL", db=db)] == ["FULL_EXIT"]


# ── Momentum-Fade ────────────────────────────────────────────────────────────
def test_kleiner_fade_unterdrueckt(db, monkeypatch):
    _min_tranche(monkeypatch, 300.0)
    _seed(db, rung=20.0, peak=10.0)
    assert evaluate_trailing([_pos(5.5)], regime="NORMAL", db=db) == []


def test_grosser_fade_feuert(db, monkeypatch):
    _min_tranche(monkeypatch, 200.0)
    _seed(db, rung=20.0, peak=10.0)
    assert [a.action for a in evaluate_trailing([_pos(5.5)], regime="NORMAL", db=db)] == ["MOMENTUM_FADE"]


@pytest.mark.parametrize("regime", ["DEFENSIVE", "CRITICAL"])
def test_fade_in_stressregime_ebenfalls_gefiltert(db, monkeypatch, regime):
    # Fades laufen in jedem Regime — die Gebuehren-Schwelle also auch
    _min_tranche(monkeypatch, 300.0)
    _seed(db, rung=20.0, peak=10.0)
    assert evaluate_trailing([_pos(5.5)], regime=regime, db=db) == []


# ── Hilfsfunktion ────────────────────────────────────────────────────────────
def test_below_min_tranche_fail_open():
    assert pcp.below_min_tranche(None, 25.0, min_tranche_usd=100.0) is False
    assert pcp.below_min_tranche(200.0, None, min_tranche_usd=100.0) is False
    assert pcp.below_min_tranche(200.0, 100.0, min_tranche_usd=1_000.0) is False
    assert pcp.below_min_tranche(200.0, 25.0, min_tranche_usd=0.0) is False
    assert pcp.below_min_tranche(200.0, 25.0, min_tranche_usd=100.0) is True
