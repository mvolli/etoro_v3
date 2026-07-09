#!/usr/bin/env python3
"""Unit tests — fix/sell-exit-cooldown + fix/signal-dedup (KTA.DE 2026-07-06).

Vorfall: data_worker erzeugte bei anhaltender Überhitzung alle 5 min ein
neues FRESH-Signal (39 Stück an einem Vormittag); der SELL-Exit konsumierte
jedes einzeln und halbierte die Position dabei endlos (~$500 → $14.75).

Zwei Verteidigungslinien:
  A) sell_exits: Instrument nach einem SELL-Exit für SELL_EXIT_COOLDOWN_H
     gesperrt (position_state.sell_exit_at).
  B) SignalRepo.has_recent_signal: identisches Signal pro Instrument pro
     TTL-Fenster wird gar nicht erst gespeichert.
"""
from __future__ import annotations

import pytest

from bot.core.sell_exits import (
    evaluate_sell_exits,
    load_blocked_instruments,
    mark_sell_exit,
)
from bot.core.trailing_stop import _ensure_position_state_table
from bot.db.connection import DB
from bot.db.repo import SignalRepo


@pytest.fixture
def db(tmp_path):
    db = DB(db_path=tmp_path / "trading.db")
    _ensure_position_state_table(db)
    db.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id INTEGER, signal_type TEXT, conviction TEXT,
            score REAL, rsi REAL, macd_hist REAL, bb_pct REAL, price REAL,
            generated_at TEXT NOT NULL DEFAULT (datetime('now','utc')),
            expires_at TEXT, status TEXT
        )
    """)
    return db


def _pos(pos_id="3499781281", iid=42, symbol="KTA.DE", amount=500.0, pnl_pct=20.9):
    return {
        "positionID": pos_id, "instrumentID": iid, "symbol": symbol,
        "amount": amount, "openRate": 10.0,
        "unrealizedPnL": {"pnL": amount * pnl_pct / 100.0},
    }


def _sig(sig_id=1, iid=42, sig_type="BB_UPPER_RSI_OVERBOUGHT"):
    return {"id": sig_id, "instrument_id": iid, "signal_type": sig_type}


# ── A) Cooldown ───────────────────────────────────────────────────────────────

def test_sell_exit_fires_without_cooldown(db):
    actions = evaluate_sell_exits([_sig()], [_pos()], blocked_instruments=set())
    assert len(actions) == 1
    assert actions[0].close_pct == 50.0


def test_blocked_instrument_does_not_fire():
    actions = evaluate_sell_exits([_sig()], [_pos()], blocked_instruments={42})
    assert actions == []


def test_cooldown_roundtrip_blocks_next_cycle(db):
    positions = [_pos()]
    # Zyklus 1: kein Cooldown → feuert
    blocked = load_blocked_instruments(db, positions)
    assert blocked == set()
    assert len(evaluate_sell_exits([_sig(1)], positions, blocked)) == 1
    # Executor markiert den Exit
    mark_sell_exit(db, "3499781281", "KTA.DE")
    # Zyklus 2 (5 min später, NEUES Signal 2): Instrument gesperrt
    blocked = load_blocked_instruments(db, positions)
    assert blocked == {42}
    assert evaluate_sell_exits([_sig(2)], positions, blocked) == []


def test_cooldown_expires(db):
    mark_sell_exit(db, "3499781281", "KTA.DE")
    # Exit künstlich 25h in die Vergangenheit legen (Cooldown 24h)
    db.execute(
        "UPDATE position_state SET sell_exit_at = datetime('now','-25 hours','utc') "
        "WHERE position_id = '3499781281'"
    )
    assert load_blocked_instruments(db, [_pos()]) == set()


def test_cooldown_is_per_instrument(db):
    mark_sell_exit(db, "3499781281", "KTA.DE")
    positions = [_pos(), _pos(pos_id="p9", iid=99, symbol="NVDA")]
    blocked = load_blocked_instruments(db, positions)
    assert blocked == {42}
    # Anderes Instrument feuert weiterhin
    actions = evaluate_sell_exits([_sig(3, iid=99)], positions, blocked)
    assert len(actions) == 1 and actions[0].symbol == "NVDA"


def test_load_blocked_fails_open_without_db():
    assert load_blocked_instruments(None, [_pos()]) == set()


# ── B) Signal-Dedup ───────────────────────────────────────────────────────────

def test_has_recent_signal_blocks_duplicate(db):
    # fix/cooldown-self-block: FRESH signals must NOT self-block (they found
    # themselves in the DB under the old query, causing permanent rejection
    # cascades). Only CONSUMED signals (trade placed) trigger the cooldown.
    repo = SignalRepo(db)
    repo.create(42, "BB_UPPER_RSI_OVERBOUGHT", "HIGH", 75.0, ttl_minutes=360)
    assert repo.has_recent_signal(42, "BB_UPPER_RSI_OVERBOUGHT", 360) is False


def test_consumed_signal_still_blocks_duplicate(db):
    # Konsumiert = auf diese Episode wurde reagiert → trotzdem Dedup
    repo = SignalRepo(db)
    sid = repo.create(42, "BB_UPPER_RSI_OVERBOUGHT", "HIGH", 75.0, ttl_minutes=360)
    repo.update_signal_status(sid, "CONSUMED")
    assert repo.has_recent_signal(42, "BB_UPPER_RSI_OVERBOUGHT", 360) is True


def test_old_signal_does_not_block(db):
    repo = SignalRepo(db)
    repo.create(42, "BB_UPPER_RSI_OVERBOUGHT", "HIGH", 75.0, ttl_minutes=360)
    db.execute("UPDATE signals SET generated_at = datetime('now','-7 hours','utc')")
    assert repo.has_recent_signal(42, "BB_UPPER_RSI_OVERBOUGHT", 360) is False


def test_different_type_or_instrument_does_not_block(db):
    repo = SignalRepo(db)
    repo.create(42, "BB_UPPER_RSI_OVERBOUGHT", "HIGH", 75.0, ttl_minutes=360)
    assert repo.has_recent_signal(42, "BB_LOWER_RSI_OVERSOLD", 360) is False
    assert repo.has_recent_signal(99, "BB_UPPER_RSI_OVERBOUGHT", 360) is False
