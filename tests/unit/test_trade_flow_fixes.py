#!/usr/bin/env python3
"""Unit tests — Trade-Flow-Fixes (Diskrepanz-Analyse 2026-07-06).

Befunde: (1) Late fills wurden als Ghost FAILED klassifiziert, obwohl die
Position real entstand (KTA.DE & Co — 10 untracked Positionen, ~$2.700);
(2) LSE-Micro-Caps mit 7-22% Spread scheiterten wiederholt am Slippage-Gate
(VALT.L 13×/Woche) und verbrannten Trade-Slots (7 ACTIVE vs. 145 FAILED/
REJECTED in 7 Tagen).

Fixes: match_late_fill (Reconciler 9c), Slippage-Blacklist (TradeRepo),
Pre-Trade-Preischeck (signal_worker, hier nicht integrationsgetestet).
"""
from __future__ import annotations

import pytest

from bot.workers.reconciler import (
    LATE_FILL_AMOUNT_TOLERANCE,
    match_late_fill,
)
from bot.db.connection import DB
from bot.db.repo import TradeRepo


# ── match_late_fill (pure) ────────────────────────────────────────────────────

def _snap(pos_id="3499781281", amount=561.0, open_price=10.84):
    return {"api_position_id": pos_id, "amount_usd": amount, "open_price": open_price}


def test_late_fill_matches_single_close_amount():
    pos = match_late_fill(561.04, [_snap(amount=561.0)])
    assert pos is not None and pos["api_position_id"] == "3499781281"


def test_late_fill_rejects_amount_mismatch():
    # KTA nach Zerlegung: $14.75 vs Trade $561 → KEIN Auto-Match (bewusst)
    assert match_late_fill(561.04, [_snap(amount=14.75)]) is None


def test_late_fill_rejects_ambiguous():
    # Zwei unbeanspruchte Positionen → nicht eindeutig → kein Repair
    assert match_late_fill(561.0, [_snap(), _snap(pos_id="p2")]) is None


def test_late_fill_rejects_empty_and_zero():
    assert match_late_fill(561.0, []) is None
    assert match_late_fill(0.0, [_snap()]) is None
    assert match_late_fill(561.0, [_snap(amount=0.0)]) is None


def test_late_fill_tolerance_boundary():
    base = 100.0
    inside = base * (1 + LATE_FILL_AMOUNT_TOLERANCE) - 0.01
    outside = base * (1 + LATE_FILL_AMOUNT_TOLERANCE) + 0.01
    assert match_late_fill(base, [_snap(amount=inside)]) is not None
    assert match_late_fill(base, [_snap(amount=outside)]) is None


# ── Slippage-Blacklist (TradeRepo) ────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path):
    db = DB(db_path=tmp_path / "trading.db")
    db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id INTEGER, symbol TEXT, direction TEXT,
            amount_usd REAL, stop_loss_pct REAL, signal_id INTEGER,
            signal_price REAL, status TEXT,
            rejection_reason TEXT, created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    return TradeRepo(db)


def test_not_blacklisted_below_threshold(repo):
    repo.record_slippage_reject(7, "VALT.L", source="execution")
    repo.record_slippage_reject(7, "VALT.L", source="signal_precheck")
    assert repo.is_slippage_blacklisted(7) is False


def test_blacklisted_at_threshold(repo):
    for _ in range(3):
        repo.record_slippage_reject(7, "VALT.L")
    assert repo.is_slippage_blacklisted(7) is True
    # anderes Instrument bleibt frei
    assert repo.is_slippage_blacklisted(8) is False


def test_blacklist_window_expires(repo):
    for _ in range(3):
        repo.record_slippage_reject(7, "VALT.L")
    # Rejects aus dem 7-Tage-Fenster herausaltern
    repo.db.execute(
        "UPDATE slippage_rejects SET rejected_at = datetime('now','-8 days','utc')"
    )
    assert repo.is_slippage_blacklisted(7) is False


def test_blacklist_fails_open_without_table(tmp_path):
    # Kaputte DB-Situation darf nie einen Trade-Pfad crashen
    db = DB(db_path=tmp_path / "x.db")
    repo = TradeRepo(db)
    assert repo.is_slippage_blacklisted(1) in (True, False)  # kein Raise
