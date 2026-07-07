#!/usr/bin/env python3
"""Unit tests — fix/break-even-enforcement.

+5% arms break-even (persistent); an armed position falling back to
BREAK_EVEN_FLOOR_PCT produces a BE_CLOSE full-close action; unarmed
positions never BE-close; execution arms state and runs BE_CLOSE in
stressed regimes too.
"""
from __future__ import annotations

import pytest

from bot.core.trailing_stop import (
    BREAK_EVEN_FLOOR_PCT,
    TrailingAction,
    evaluate_trailing,
    execute_trailing_actions,
    load_be_active,
    mark_break_even_active,
)
from bot.db.connection import DB


@pytest.fixture
def db(tmp_path):
    return DB(db_path=tmp_path / "trading.db")


def _pos(pos_id="p1", symbol="NVDA", amount=1000.0, pnl_pct=8.0):
    return {
        "positionID": pos_id,
        "symbol": symbol,
        "instrumentID": 42,
        "amount": amount,
        "openRate": 100.0,
        "unrealizedPnL": {"pnL": amount * pnl_pct / 100.0},
    }


def test_arming_persists(db):
    mark_break_even_active(db, "p1", "NVDA")
    assert load_be_active(db, ["p1"]) == {"p1"}
    # idempotent
    mark_break_even_active(db, "p1", "NVDA")
    assert load_be_active(db, ["p1"]) == {"p1"}


def test_armed_position_at_floor_produces_be_close(db):
    mark_break_even_active(db, "p1", "NVDA")
    actions = evaluate_trailing([_pos(pnl_pct=0.1)], db=db)
    assert len(actions) == 1
    assert actions[0].action == "BE_CLOSE"


def test_armed_position_above_floor_no_action(db):
    mark_break_even_active(db, "p1", "NVDA")
    actions = evaluate_trailing([_pos(pnl_pct=BREAK_EVEN_FLOOR_PCT + 1.0)], db=db)
    assert actions == []


def test_armed_position_negative_pnl_be_closes(db):
    # Fällt die Position unter Entry bevor der Hard-SL (-3%) greift,
    # schließt BE-Enforcement die Lücke
    mark_break_even_active(db, "p1", "NVDA")
    actions = evaluate_trailing([_pos(pnl_pct=-1.2)], db=db)
    assert actions[0].action == "BE_CLOSE"


def test_unarmed_position_never_be_closes(db):
    actions = evaluate_trailing([_pos(pnl_pct=0.1)], db=db)
    assert actions == []


class FakeClient:
    def __init__(self):
        self.closed = []

    def close_position(self, position_id, instrument_id, units_to_deduct=None):
        self.closed.append(position_id)
        return {"orderId": "ok"}

    def get_portfolio(self):
        return {"clientPortfolio": {"positions": []}}  # Position weg → verified


def test_break_even_action_arms_state(db):
    client = FakeClient()
    action = TrailingAction(
        action="BREAK_EVEN", symbol="NVDA", position_id="p1",
        pnl_pct=8.0, reason="+8%", instrument_id=42,
    )
    stats = execute_trailing_actions(client, [action], db=db)
    assert stats["break_evens"] == 1
    assert load_be_active(db, ["p1"]) == {"p1"}
    assert client.closed == []


def test_be_close_executes_and_verifies(db, monkeypatch):
    import bot.core.trailing_stop as ts
    monkeypatch.setattr(ts, "verify_full_close", lambda *a, **k: (True, "gone", None))
    monkeypatch.setattr(ts, "_post_closed_embed", lambda *a, **k: None)

    client = FakeClient()
    action = TrailingAction(
        action="BE_CLOSE", symbol="NVDA", position_id="p1",
        pnl_pct=0.1, reason="floor", instrument_id=42, amount_usd=1000.0,
    )
    stats = execute_trailing_actions(client, [action], regime="DEFENSIVE", db=db)
    # BE_CLOSE läuft auch in stressed regimes (Verlustschutz)
    assert stats["be_closes"] == 1
    assert client.closed == ["p1"]


def test_be_close_dry_run(db):
    client = FakeClient()
    action = TrailingAction(
        action="BE_CLOSE", symbol="NVDA", position_id="p1",
        pnl_pct=0.1, reason="floor", instrument_id=42,
    )
    stats = execute_trailing_actions(client, [action], dry_run=True, db=db)
    assert stats["be_closes"] == 1
    assert client.closed == []


def test_full_flow_arm_then_enforce(db):
    # +8% → BREAK_EVEN Action → armiert; später +0.1% → BE_CLOSE
    client = FakeClient()
    actions = evaluate_trailing([_pos(pnl_pct=8.0)], db=db)
    assert actions[0].action == "BREAK_EVEN"
    execute_trailing_actions(client, actions, db=db)

    actions = evaluate_trailing([_pos(pnl_pct=0.1)], db=db)
    assert actions[0].action == "BE_CLOSE"
