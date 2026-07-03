#!/usr/bin/env python3
"""Unit tests — fix/partial-close-level-tracking.

Covers: each PROFIT_TAKE_LEVEL fires exactly once per position, lowest
pending level first, BREAK_EVEN fallback once all due levels are taken,
persistence round-trip, and stale-state cleanup.
"""
from __future__ import annotations

import pytest

from bot.core.trailing_stop import (
    PROFIT_TAKE_LEVELS,
    _resolve_profit_levels,
    cleanup_position_state,
    evaluate_trailing,
    load_levels_taken,
    load_profit_levels,
    mark_level_taken,
)
from bot.db.connection import DB


@pytest.fixture
def db(tmp_path):
    return DB(db_path=tmp_path / "trading.db")


def _seed_instrument_atr(db, instrument_id=42, atr_pct=1.2):
    db.execute("""
        CREATE TABLE IF NOT EXISTS instruments (
            instrument_id INTEGER PRIMARY KEY,
            symbol TEXT,
            atr_pct REAL,
            atr_updated_at TEXT
        )
    """)
    db.execute(
        "INSERT INTO instruments (instrument_id, symbol, atr_pct) VALUES (?, ?, ?)",
        (instrument_id, "NVDA", atr_pct),
    )


def _pos(pos_id="p1", symbol="NVDA", amount=1000.0, pnl_pct=16.0, open_rate=100.0):
    return {
        "positionID": pos_id,
        "symbol": symbol,
        "instrumentID": 42,
        "amount": amount,
        "openRate": open_rate,
        "unrealizedPnL": {"pnL": amount * pnl_pct / 100.0},
    }


def test_level_fires_once(db):
    # 1. Lauf bei +16%: Level 15 feuert
    actions = evaluate_trailing([_pos(pnl_pct=16.0)], db=db)
    assert len(actions) == 1
    assert actions[0].action == "PARTIAL_CLOSE"
    assert actions[0].level_threshold == 15.0

    mark_level_taken(db, "p1", "NVDA", 15.0)

    # 2. Lauf, PnL% unverändert (Rest-Position): KEIN erneuter Partial-Close
    actions = evaluate_trailing([_pos(pnl_pct=16.0)], db=db)
    assert len(actions) == 1
    assert actions[0].action == "BREAK_EVEN"


def test_next_level_fires_after_first_taken(db):
    mark_level_taken(db, "p1", "NVDA", 15.0)
    actions = evaluate_trailing([_pos(pnl_pct=27.0)], db=db)
    assert actions[0].action == "PARTIAL_CLOSE"
    assert actions[0].level_threshold == 25.0


def test_lowest_pending_level_first(db):
    # PnL springt direkt auf +55%: Level 15 zuerst (Bible-Reihenfolge),
    # 25/50 folgen in späteren Zyklen
    actions = evaluate_trailing([_pos(pnl_pct=55.0)], db=db)
    assert actions[0].level_threshold == 15.0

    mark_level_taken(db, "p1", "NVDA", 15.0)
    actions = evaluate_trailing([_pos(pnl_pct=55.0)], db=db)
    assert actions[0].level_threshold == 25.0

    mark_level_taken(db, "p1", "NVDA", 25.0)
    actions = evaluate_trailing([_pos(pnl_pct=55.0)], db=db)
    assert actions[0].level_threshold == 50.0

    mark_level_taken(db, "p1", "NVDA", 50.0)
    actions = evaluate_trailing([_pos(pnl_pct=55.0)], db=db)
    assert actions[0].action == "BREAK_EVEN"


def test_below_be_trigger_no_action(db):
    # BREAK_EVEN_TRIGGER_PCT is now 3.0 (was 5.0) — use a value clearly below it
    assert evaluate_trailing([_pos(pnl_pct=1.0)], db=db) == []


def test_be_range_tracks_break_even(db):
    actions = evaluate_trailing([_pos(pnl_pct=8.0)], db=db)
    assert len(actions) == 1
    assert actions[0].action == "BREAK_EVEN"


def test_persistence_roundtrip(db):
    mark_level_taken(db, "p9", "AAPL", 15.0)
    mark_level_taken(db, "p9", "AAPL", 25.0)
    taken = load_levels_taken(db, ["p9"])
    assert taken["p9"] == {15.0, 25.0}


def test_levels_are_per_position(db):
    mark_level_taken(db, "p1", "NVDA", 15.0)
    # andere Position, gleiches Symbol: Level 15 feuert dort trotzdem
    actions = evaluate_trailing([_pos(pos_id="p2", pnl_pct=16.0)], db=db)
    assert actions[0].action == "PARTIAL_CLOSE"
    assert actions[0].level_threshold == 15.0


def test_cleanup_removes_stale_positions(db):
    mark_level_taken(db, "gone", "X", 15.0)
    mark_level_taken(db, "live", "Y", 15.0)
    deleted = cleanup_position_state(db, {"live"})
    assert deleted == 1
    assert load_levels_taken(db, ["gone", "live"]) == {"live": {15.0}}


def test_stateless_fallback_without_db():
    # db=None (Tests/Legacy): fällt auf altes Verhalten zurück, crasht nicht
    actions = evaluate_trailing([_pos(pnl_pct=16.0)], db=None)
    assert actions[0].action == "PARTIAL_CLOSE"


def test_resolve_profit_levels_uses_fixed_ladder_without_atr():
    assert _resolve_profit_levels(None) == PROFIT_TAKE_LEVELS
    assert _resolve_profit_levels(0) == PROFIT_TAKE_LEVELS


def test_resolve_profit_levels_scales_with_atr():
    # Low-vol blue-chip (ATR 1.2%): ladder well below the fixed 15/25/50
    low = _resolve_profit_levels(1.2)
    assert low[0]['threshold'] == pytest.approx(7.2)
    assert low[1]['threshold'] == pytest.approx(12.0)
    assert low[2]['threshold'] == pytest.approx(21.6)

    # High-vol name (ATR 5%): ladder well above the fixed 15/25/50
    high = _resolve_profit_levels(5.0)
    assert high[0]['threshold'] == pytest.approx(30.0)
    assert high[1]['threshold'] == pytest.approx(50.0)
    assert high[2]['threshold'] == pytest.approx(90.0)


def test_atr_adaptive_ladder_used_when_available(db):
    # NVDA with ATR 1.2% → first level at 7.2%, not the fixed 15%
    _seed_instrument_atr(db, instrument_id=42, atr_pct=1.2)
    actions = evaluate_trailing([_pos(pnl_pct=8.0)], db=db)
    assert actions[0].action == "PARTIAL_CLOSE"
    assert actions[0].level_threshold == pytest.approx(7.2)


def test_atr_ladder_frozen_after_first_evaluation(db):
    # First cycle: ATR 1.2% → ladder computed and frozen for this position
    _seed_instrument_atr(db, instrument_id=42, atr_pct=1.2)
    evaluate_trailing([_pos(pnl_pct=8.0)], db=db)
    frozen = load_profit_levels(db, ["p1"])["p1"]
    assert frozen[0]['threshold'] == pytest.approx(7.2)

    # ATR drifts sharply on a later data_worker cycle — must NOT reshuffle
    # thresholds for a position already in flight.
    db.execute("UPDATE instruments SET atr_pct = 5.0 WHERE instrument_id = 42")
    actions = evaluate_trailing([_pos(pnl_pct=8.0)], db=db)
    assert actions[0].level_threshold == pytest.approx(7.2)
