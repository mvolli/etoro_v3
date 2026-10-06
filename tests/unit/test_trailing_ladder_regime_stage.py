#!/usr/bin/env python3
"""Unit tests — fix/ladder-regime-decision-stage (2026-10-06).

Die Regime-Unterdrueckung der Profit-Leiter ("let winners run" im
DEFENSIVE/CRITICAL) lag bisher in execute_trailing_actions, also NACH dem
KI-Profit-Advisor (ein echter LLM-Call pro Zyklus) und NACH dem
partial_close_shadow-Eintrag. Gemessen: 191 Calls und 191 Ledger-Rows fuer
eine einzige blockierte ZM-Stufe (01.-05.10.2026).

Diese Tests nageln fest, dass das Vorziehen in evaluate_trailing() das
Handelsverhalten NICHT veraendert:
  * blockierte Leiter-Stufe -> gar keine Action (kein Durchfallen auf Fade/BE)
  * in NORMAL/CAUTION feuert die Stufe unveraendert
  * ein in FULL_EXIT umgeschlagener Rung laeuft in ALLEN Regimes
  * der Momentum-Fade bleibt in jedem Regime erreichbar, wenn keine Stufe faellig ist
"""
from __future__ import annotations

import pytest

from bot.core.trailing_stop import (
    SUPPRESS_LADDER_REGIMES,
    evaluate_trailing,
    save_profit_levels,
)
from bot.db.connection import DB


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Vollausstieg aus, Fade-Parameter auf die Modul-Defaults festnageln.

    Sonst entscheidet die gerade geladene config.yaml mit (arm 5.0 /
    retrace 0.30 / max_abs 4.0) und die Tests wackeln mit der Config.
    """
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


@pytest.fixture
def db(tmp_path):
    return DB(db_path=tmp_path / "trading.db")


def _pos(pnl_pct, pos_id="p1", symbol="ZM", amount=1000.0, open_rate=100.0):
    return {
        "positionID": pos_id,
        "symbol": symbol,
        "instrumentID": 42,
        "amount": amount,
        "openRate": open_rate,
        "unrealizedPnL": {"pnL": amount * pnl_pct / 100.0},
    }


def _seed(db, rung=5.0, close_pct=25.0, peak=5.5, remaining=1.0, pos_id="p1"):
    """Eingefrorene Leiter + Peak/Restanteil setzen (wie im Live-Zustand)."""
    save_profit_levels(db, pos_id, "ZM", [{"threshold": rung, "close_pct": close_pct}])
    db.execute(
        "UPDATE position_state SET peak_pnl_pct = ?, remaining_frac = ?, "
        "momentum_faded = 0 WHERE position_id = ?",
        (peak, remaining, pos_id),
    )


# ── 1. Blockierte Stufe erzeugt gar keine Action ──────────────────────────────
@pytest.mark.parametrize("regime", SUPPRESS_LADDER_REGIMES)
def test_ladder_suppressed_at_decision_stage(db, regime):
    _seed(db)
    assert evaluate_trailing([_pos(5.5)], regime=regime, db=db) == []


@pytest.mark.parametrize("regime", ["NORMAL", "CAUTION"])
def test_ladder_fires_outside_suppressed_regimes(db, regime):
    _seed(db)
    actions = evaluate_trailing([_pos(5.5)], regime=regime, db=db)
    assert len(actions) == 1
    assert actions[0].action == "PARTIAL_CLOSE"
    assert actions[0].close_pct == 25.0
    assert actions[0].level_threshold == 5.0


def test_default_regime_is_not_suppressed(db):
    """Ohne regime-Argument (Tests/Altaufrufer) muss die Leiter feuern."""
    _seed(db)
    actions = evaluate_trailing([_pos(5.5)], db=db)
    assert [a.action for a in actions] == ["PARTIAL_CLOSE"]


# ── 2. Kein Durchfallen auf den Fade (das waere Variante (i)) ─────────────────
def test_no_fallthrough_to_fade_in_defensive(db):
    """Peak 10 / PnL 5.5 -> der Fade WUERDE feuern (Floor 6.0), die faellige
    Stufe maskiert ihn aber. Vor und nach diesem Commit gleich: kein Fade.
    """
    _seed(db, peak=10.0)
    assert evaluate_trailing([_pos(5.5)], regime="DEFENSIVE", db=db) == []


def test_ladder_still_masks_fade_in_normal(db):
    """Gegenprobe: die Maskierung ist Bestandsverhalten, nicht neu."""
    _seed(db, peak=10.0)
    actions = evaluate_trailing([_pos(5.5)], regime="NORMAL", db=db)
    assert [a.action for a in actions] == ["PARTIAL_CLOSE"]


# ── 3. FULL_EXIT-Umschlag ist kein Teilverkauf und laeuft in allen Regimes ────
@pytest.mark.parametrize("regime", ["NORMAL", "DEFENSIVE", "CRITICAL"])
def test_full_exit_conversion_runs_in_every_regime(db, regime):
    """Rest 50 %: ein weiterer 25-%-Trim fiele unter MIN_REMAINING_PCT, also
    Vollausstieg — Untergrenzen-Schutz, den das Regime nie unterdruecken darf.
    """
    _seed(db, remaining=0.5)
    actions = evaluate_trailing([_pos(5.5)], regime=regime, db=db)
    assert [a.action for a in actions] == ["FULL_EXIT"]


# ── 4. Der Fade bleibt in jedem Regime erreichbar, wenn keine Stufe faellig ist
@pytest.mark.parametrize("regime", ["NORMAL", "DEFENSIVE", "CRITICAL"])
def test_momentum_fade_unaffected(db, regime):
    """Stufe liegt bei +20 % (nicht faellig), Peak 10 / PnL 5.5 -> Fade feuert."""
    _seed(db, rung=20.0, peak=10.0)
    actions = evaluate_trailing([_pos(5.5)], regime=regime, db=db)
    assert [a.action for a in actions] == ["MOMENTUM_FADE"]
