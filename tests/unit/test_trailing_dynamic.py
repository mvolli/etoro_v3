#!/usr/bin/env python3
"""Unit tests — dynamic quick-profit (Stufe 1).

Covers the two new mechanics layered onto evaluate_trailing:
  ① MOMENTUM_FADE — universal: a built-up gain that gives back ≥ retrace_frac of
     its peak locks a partial + arms BE. One-shot per position. Works in the
     sub-BE gap where the ladder never reaches, and yields to a due ladder level.
  ② SCALP tier — opt-in via strategy='scalp': an early first rung (ATR×2, clamped)
     that is allowed to fire below the normal +3% BE-trigger gate.

The fade needs a real high-water-mark, so the end-to-end tests drive TWO cycles
(peak set on cycle 1, fade evaluated on cycle 2) — exactly the risk_worker rhythm.
"""
from __future__ import annotations

import pytest

import bot.core.trailing_stop as ts
from bot.core.trailing_stop import (
    _resolve_profit_levels,
    _scalp_rung,
    evaluate_trailing,
    mark_level_taken,
    mark_momentum_faded,
    set_strategy,
    should_momentum_fade,
)
from bot.db.connection import DB


@pytest.fixture
def db(tmp_path):
    return DB(db_path=tmp_path / "trading.db")


@pytest.fixture(autouse=True)
def _reset_module_config():
    """Restore code-default thresholds so a prior apply_config() in another
    test cannot leak mutated globals into these assertions."""
    saved = {k: getattr(ts, k) for k in (
        "MOMENTUM_FADE_ENABLED", "MOMENTUM_ARM_PCT", "MOMENTUM_RETRACE_FRAC",
        "MOMENTUM_MIN_LOCK_PCT", "MOMENTUM_FADE_CLOSE_PCT",
        "SCALP_ENABLED", "SCALP_ATR_MULT", "SCALP_MIN_PCT", "SCALP_MAX_PCT", "SCALP_CLOSE_PCT",
    )}
    ts.MOMENTUM_FADE_ENABLED = True
    ts.MOMENTUM_ARM_PCT = 2.0
    ts.MOMENTUM_RETRACE_FRAC = 0.40
    ts.MOMENTUM_MIN_LOCK_PCT = 1.0
    ts.MOMENTUM_FADE_CLOSE_PCT = 50.0
    ts.SCALP_ENABLED = True
    ts.SCALP_ATR_MULT = 2.0
    ts.SCALP_MIN_PCT = 2.0
    ts.SCALP_MAX_PCT = 5.0
    ts.SCALP_CLOSE_PCT = 25
    yield
    for k, v in saved.items():
        setattr(ts, k, v)


def _seed_instrument_atr(db, instrument_id=42, atr_pct=1.2):
    db.execute("""
        CREATE TABLE IF NOT EXISTS instruments (
            instrument_id INTEGER PRIMARY KEY, symbol TEXT,
            atr_pct REAL, atr_updated_at TEXT
        )
    """)
    db.execute(
        "INSERT INTO instruments (instrument_id, symbol, atr_pct) VALUES (?, ?, ?)",
        (instrument_id, "NVDA", atr_pct),
    )


def _pos(pos_id="p1", symbol="NVDA", amount=1000.0, pnl_pct=5.0, open_rate=100.0):
    return {
        "positionID": pos_id, "symbol": symbol, "instrumentID": 42,
        "amount": amount, "openRate": open_rate,
        "unrealizedPnL": {"pnL": amount * pnl_pct / 100.0},
    }


# ── should_momentum_fade (pure) ───────────────────────────────────────────────

def test_fade_false_at_fresh_high():
    # peak == current → nothing given back
    assert should_momentum_fade(5.0, 5.0, already_faded=False) is False


def test_fade_true_after_retrace():
    # peak 5 → floor 3.0; current 2.5 ≤ floor and ≥ min_lock
    assert should_momentum_fade(2.5, 5.0, already_faded=False) is True


def test_fade_false_below_min_lock():
    # gave back plenty but current 0.5% < min_lock 1.0 → BE/SL territory
    assert should_momentum_fade(0.5, 5.0, already_faded=False) is False


def test_fade_false_peak_below_arm():
    # peak 1.8% never reached the +2% arm threshold
    assert should_momentum_fade(1.0, 1.8, already_faded=False) is False


def test_fade_false_when_already_faded():
    assert should_momentum_fade(2.5, 5.0, already_faded=True) is False


def test_fade_false_when_disabled():
    ts.MOMENTUM_FADE_ENABLED = False
    assert should_momentum_fade(2.5, 5.0, already_faded=False) is False


def test_fade_boundary_exactly_at_floor():
    # peak 5 → floor exactly 3.0; equal counts as faded (<=)
    assert should_momentum_fade(3.0, 5.0, already_faded=False) is True
    assert should_momentum_fade(3.01, 5.0, already_faded=False) is False


# ── scalp ladder resolution ───────────────────────────────────────────────────

def test_scalp_rung_atr_scaled_and_clamped():
    assert _scalp_rung(1.2)["threshold"] == 2.4      # 1.2×2, within [2,5]
    assert _scalp_rung(0.5)["threshold"] == 2.0      # 1.0 → clamped up to min 2.0
    assert _scalp_rung(4.0)["threshold"] == 5.0      # 8.0 → clamped down to max 5.0


def test_scalp_prepends_early_rung():
    swing = _resolve_profit_levels(1.2, "swing")
    scalp = _resolve_profit_levels(1.2, "scalp")
    assert scalp[0]["threshold"] == 2.4
    assert scalp[0]["close_pct"] == 25
    assert scalp[1:] == swing            # swing rungs preserved after the scalp rung


def test_scalp_not_prepended_when_disabled():
    ts.SCALP_ENABLED = False
    assert _resolve_profit_levels(1.2, "scalp") == _resolve_profit_levels(1.2, "swing")


# ── evaluate_trailing end-to-end ──────────────────────────────────────────────

def test_no_fade_at_fresh_high(db):
    # +5% on the first cycle: peak == current → BREAK_EVEN, never a fade.
    actions = evaluate_trailing([_pos(pnl_pct=5.0)], db=db)
    assert [a.action for a in actions] == ["BREAK_EVEN"]


def test_fade_fires_on_second_cycle_after_retrace(db):
    evaluate_trailing([_pos(pnl_pct=5.0)], db=db)          # peak = 5
    actions = evaluate_trailing([_pos(pnl_pct=2.5)], db=db)  # gave back 50%
    assert [a.action for a in actions] == ["MOMENTUM_FADE"]
    assert actions[0].close_pct == 50.0


def test_fade_fires_in_sub_be_gap(db):
    # Peak +2.8% never reached +3% BE-trigger; retrace to +1.5% still locks.
    evaluate_trailing([_pos(pnl_pct=2.8)], db=db)          # peak 2.8, no action
    actions = evaluate_trailing([_pos(pnl_pct=1.5)], db=db)
    assert [a.action for a in actions] == ["MOMENTUM_FADE"]


def test_fade_is_one_shot(db):
    evaluate_trailing([_pos(pnl_pct=5.0)], db=db)
    mark_momentum_faded(db, "p1", "NVDA")                   # simulate executor
    actions = evaluate_trailing([_pos(pnl_pct=2.5)], db=db)
    assert [a.action for a in actions] != ["MOMENTUM_FADE"]


def test_ladder_level_takes_priority_over_fade(db):
    _seed_instrument_atr(db, atr_pct=1.2)                   # rungs 7.2 / 12 / 21.6
    evaluate_trailing([_pos(pnl_pct=30.0)], db=db)          # peak 30, +7.2 rung due
    mark_level_taken(db, "p1", "NVDA", 7.2)                 # executor would persist this
    # +13%: gave back >40% of peak (fade eligible) BUT the +12 rung is due →
    # structured ladder must win.
    actions = evaluate_trailing([_pos(pnl_pct=13.0)], db=db)
    assert [a.action for a in actions] == ["PARTIAL_CLOSE"]
    assert actions[0].level_threshold == 12.0


def test_swing_position_no_early_take_below_gate(db):
    # Control: a swing position at +2.5% (below +3% gate, no prior peak) → nothing.
    assert evaluate_trailing([_pos(pnl_pct=2.5)], db=db) == []


def test_scalp_tier_fires_below_be_gate(db):
    _seed_instrument_atr(db, atr_pct=1.2)                   # scalp rung = 2.4
    set_strategy(db, "p1", "NVDA", "scalp")
    actions = evaluate_trailing([_pos(pnl_pct=2.5)], db=db)  # 2.5 ≥ scalp gate 2.4
    assert [a.action for a in actions] == ["PARTIAL_CLOSE"]
    assert actions[0].level_threshold == 2.4
    assert actions[0].close_pct == 25


def test_peak_persists_across_cycles(db):
    evaluate_trailing([_pos(pnl_pct=8.0)], db=db)
    evaluate_trailing([_pos(pnl_pct=4.0)], db=db)          # peak stays 8
    meta = ts.load_position_dynamic(db, ["p1"])["p1"]
    assert meta["peak"] == pytest.approx(8.0)
