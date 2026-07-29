#!/usr/bin/env python3
"""Regression — fix/core-sweep-duplicate-approval (2026-07-29).

Der Core-Sweep baute `held_instrument_ids` nur aus offenen Positionen.
Ein Instrument mit bereits APPROVED-Trade wurde deshalb jeden 15-min-
Zyklus erneut eingeplant und vom execution_worker als "Duplicate
instrument_id in same execution batch" verworfen — 143 von 143
Duplikat-Rejects der letzten 3 Tage stammten aus CORE_SWEEP.

Nebenwirkung, die den Fix wichtig macht: diese Phantom-Rejects gingen in
die 14-Tage-Reject-Rate ein, auf deren Basis der LLM Review Worker
CORE_SWEEP autonom auf score_multiplier 0.2 gedrosselt hat.
"""
from __future__ import annotations

import pytest

from bot.core.core_sweep import plan_core_sweep
from bot.db.connection import DB
from bot.db.repo import TradeRepo


@pytest.fixture
def db(tmp_path):
    d = DB(db_path=tmp_path / "t.db")
    d.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_id INTEGER NOT NULL,
        symbol TEXT NOT NULL, direction TEXT NOT NULL, status TEXT NOT NULL,
        amount_usd REAL NOT NULL, stop_loss_pct REAL NOT NULL DEFAULT 3.0,
        signal_id INTEGER, signal_price REAL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    return d


def _mk(db, instrument_id, status, symbol="X"):
    db.execute("INSERT INTO trades (instrument_id, symbol, direction, status, "
               "amount_usd) VALUES (?, ?, 'BUY', ?, 100.0)",
               (instrument_id, symbol, status))


# ── Repo: statuses-Parameter ──────────────────────────────────────────────────

def test_default_statuses_unchanged(db):
    """Bestehender Aufrufer (normaler Signalpfad) sieht weiter nur APPROVED."""
    repo = TradeRepo(db)
    _mk(db, 1003, "APPROVED")
    _mk(db, 255, "SUBMITTING")
    _mk(db, 2385, "ACTIVE")
    _mk(db, 999, "REJECTED")
    assert repo.get_approved_instrument_ids() == {1003}


def test_submitting_included_when_requested(db):
    repo = TradeRepo(db)
    _mk(db, 1003, "APPROVED")
    _mk(db, 255, "SUBMITTING")
    _mk(db, 2385, "ACTIVE")
    assert repo.get_approved_instrument_ids(("APPROVED", "SUBMITTING")) == {1003, 255}


def test_empty_when_nothing_pending(db):
    repo = TradeRepo(db)
    _mk(db, 2385, "CLOSED")
    assert repo.get_approved_instrument_ids(("APPROVED", "SUBMITTING")) == set()


# ── Core-Sweep respektiert die Pending-Exposure ───────────────────────────────

_CFG = {
    "trading": {"core_sweep": {
        "enabled": True,
        "whitelist": {"META": 1003, "UBER": 1186},
        "reserve_pct": 0.0, "target_size_pct": 5.0, "max_sweeps": 5,
        "reject_cooldown_after": 0,
    }},
}


def test_instrument_with_approved_trade_is_not_swept_again(db):
    """Der Kern des Fixes: META hat einen APPROVED-Trade -> kein zweiter Sweep."""
    held = {1003}  # so wie signal_worker es jetzt befuellt
    orders, reasons = plan_core_sweep(
        _CFG, equity=10000.0, cash=5000.0, regime="NORMAL",
        held_instrument_ids=held, atr_by_id={}, rsi_by_id={}, db=db,
    )
    swept = {o.instrument_id for o in orders}
    assert 1003 not in swept
    assert 1186 in swept          # der freie Titel wird weiterhin gekauft


def test_without_guard_duplicate_would_be_planned(db):
    """Belegt die Ursache: ohne die APPROVED-ID im Set plant der Sweep erneut."""
    orders, _ = plan_core_sweep(
        _CFG, equity=10000.0, cash=5000.0, regime="NORMAL",
        held_instrument_ids=set(), atr_by_id={}, rsi_by_id={}, db=db,
    )
    assert 1003 in {o.instrument_id for o in orders}
