#!/usr/bin/env python3
"""Unit tests — Autonomie-Fixes Batch WS1 (2026-07-05).

Covers the four autonomy gaps closed in this batch:
  1a. Kill-Switch: strukturierter JSON-State (scope) + Daily-Auto-Clear am
      naechsten UTC-Tag; Legacy-Plaintext-Flags = scope 'manual' (nie auto).
  1b. Monitor-Heartbeat: monitor_worker ist in EXPECTED_INTERVALS_MIN.
  1c. Ghost-Blacklist: 9+ Fails → rollierende 7-Tage-Sperre statt permanent.
  1d. FAILED-Requeue: pure Klassifikation (transient vs. strukturell) +
      One-Shot-Guard (requeue_count) + Altersfenster.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timezone, timedelta

import pytest

from bot.db.connection import DB


# ── 1a. Kill-Switch scope + daily auto-clear ─────────────────────────────────

@pytest.fixture
def ks(tmp_path, monkeypatch):
    import bot.core.kill_switch as ks_mod
    importlib.reload(ks_mod)
    flag = tmp_path / "kill_switch.flag"
    monkeypatch.setattr(ks_mod, "_KILL_SWITCH_FILE", flag)
    monkeypatch.setattr(ks_mod, "KILL_SWITCH_FILE", flag)
    return ks_mod


def test_activate_writes_structured_state(ks):
    ks.activate("Tagesverlust -5.2%", scope="daily")
    state = ks.get_state()
    assert state["active"] is True
    assert state["scope"] == "daily"
    assert state["reason"] == "Tagesverlust -5.2%"
    assert state["tripped_at"]  # timestamp present
    # get_reason() bleibt lesbar (kein rohes JSON)
    assert ks.get_reason() == "Tagesverlust -5.2%"


def test_legacy_plaintext_flag_is_manual_scope(ks):
    ks._KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    ks._KILL_SWITCH_FILE.write_text("Emergency stop — manual echo")
    state = ks.get_state()
    assert state["scope"] == "manual"
    assert state["reason"] == "Emergency stop — manual echo"
    assert state["tripped_at"] is None


def test_invalid_scope_falls_back_to_manual(ks):
    ks.activate("x", scope="hourly")  # not a valid scope
    assert ks.get_state()["scope"] == "manual"


def test_daily_auto_clears_on_new_utc_day(ks):
    ks.activate("Tagesverlust", scope="daily")
    # simulate: tripped yesterday
    import json
    data = json.loads(ks._KILL_SWITCH_FILE.read_text())
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    data["tripped_at"] = yesterday
    ks._KILL_SWITCH_FILE.write_text(json.dumps(data))

    cleared, detail = ks.auto_clear_if_new_day()
    assert cleared is True
    assert "auto-cleared" in detail
    assert ks.is_kill_switch_active() is False


def test_daily_same_day_not_cleared(ks):
    ks.activate("Tagesverlust", scope="daily")  # tripped_at = now (today)
    cleared, _ = ks.auto_clear_if_new_day()
    assert cleared is False
    assert ks.is_kill_switch_active() is True


@pytest.mark.parametrize("scope", ["weekly", "monthly", "manual"])
def test_non_daily_scopes_never_auto_clear(ks, scope):
    ks.activate("Breach", scope=scope)
    import json
    data = json.loads(ks._KILL_SWITCH_FILE.read_text())
    data["tripped_at"] = "2026-01-01 00:00:00"  # long ago
    ks._KILL_SWITCH_FILE.write_text(json.dumps(data))

    cleared, _ = ks.auto_clear_if_new_day()
    assert cleared is False
    assert ks.is_kill_switch_active() is True


def test_plaintext_flag_never_auto_clears(ks):
    ks._KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    ks._KILL_SWITCH_FILE.write_text("manual stop")
    cleared, _ = ks.auto_clear_if_new_day()
    assert cleared is False
    assert ks.is_kill_switch_active() is True


def test_corrupt_tripped_at_fails_safe(ks):
    import json
    ks._KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    ks._KILL_SWITCH_FILE.write_text(json.dumps(
        {"reason": "x", "scope": "daily", "tripped_at": "not-a-date"}
    ))
    cleared, _ = ks.auto_clear_if_new_day()
    assert cleared is False  # fail-safe: Flag bleibt stehen
    assert ks.is_kill_switch_active() is True


# ── 1b. Monitor-Heartbeat ─────────────────────────────────────────────────────

def test_monitor_worker_is_heartbeat_monitored():
    from bot.core.heartbeat import EXPECTED_INTERVALS_MIN
    assert EXPECTED_INTERVALS_MIN.get("monitor_worker") == 30
    # all 7 workers covered — no blind spot
    assert set(EXPECTED_INTERVALS_MIN) == {
        "data_worker", "risk_worker", "reconciler", "signal_worker",
        "execution_worker", "monitor_worker", "discovery_worker",
    }


# ── 1c. Ghost-Blacklist Auto-Expiry ──────────────────────────────────────────

@pytest.fixture
def trade_db(tmp_path):
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
    db.execute("""
        CREATE TABLE IF NOT EXISTS instrument_failures (
            instrument_id INTEGER PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_failure_at TEXT,
            blacklisted_until TEXT
        )
    """)
    return db


def test_ninth_failure_is_rolling_7d_not_permanent(trade_db):
    from bot.db.repo import TradeRepo
    repo = TradeRepo(trade_db)
    # simulate 8 prior failures
    trade_db.execute(
        "INSERT INTO instrument_failures (instrument_id, consecutive_failures) VALUES (42, 8)"
    )
    count, label = repo.record_ghost_failure(42)
    assert count == 9
    assert label == "7d-rolling"
    row = trade_db.fetchone(
        "SELECT blacklisted_until FROM instrument_failures WHERE instrument_id = 42"
    )
    until = datetime.strptime(row["blacklisted_until"], "%Y-%m-%d %H:%M:%S")
    delta_days = (until - datetime.now(timezone.utc).replace(tzinfo=None)).days
    assert 6 <= delta_days <= 7          # ~7 Tage, NICHT 9999-12-31
    assert repo.is_instrument_blacklisted(42) is True


def test_duration_tiers(trade_db):
    from bot.db.repo import TradeRepo
    repo = TradeRepo(trade_db)
    assert repo._blacklist_duration_hours(2) == 0
    assert repo._blacklist_duration_hours(3) == 6
    assert repo._blacklist_duration_hours(6) == 168
    assert repo._blacklist_duration_hours(9) == 168    # war: None (permanent)
    assert repo._blacklist_duration_hours(15) == 168


# ── 1d. FAILED-Requeue Klassifikation ────────────────────────────────────────

def _now_str(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def test_transient_classification():
    from bot.workers.execution_worker import is_transient_failure
    # transient
    assert is_transient_failure("APIError: HTTP 502 from /orders: bad gateway")
    assert is_transient_failure("APIError: HTTP 503 from /orders: unavailable")
    assert is_transient_failure("APIError: read timeout after 30s")
    assert is_transient_failure("Unexpected error: ConnectionError('reset by peer')")
    # strukturell — NIE requeuen
    assert not is_transient_failure("APIError: HTTP 400 from /orders: bad request")
    assert not is_transient_failure("Ghost order: orderId=x but position never materialized")
    assert not is_transient_failure("Blocked: allowOpenPosition=false")
    assert not is_transient_failure("Market closed: NYSE closed until 14:30")
    assert not is_transient_failure("Kill switch active: daily loss")
    assert not is_transient_failure(None)
    assert not is_transient_failure("")


def test_classify_requeue_guards():
    from bot.workers.execution_worker import classify_requeue
    base = {
        "requeue_count": 0,
        "rejection_reason": "APIError: HTTP 503 from /orders: unavailable",
        "created_at": _now_str(minutes_ago=10),
    }
    assert classify_requeue(dict(base)) is True
    # one-shot: bereits requeued → nie wieder
    assert classify_requeue({**base, "requeue_count": 1}) is False
    # zu alt (> 60 min)
    assert classify_requeue({**base, "created_at": _now_str(minutes_ago=90)}) is False
    # strukturelle Fehler
    assert classify_requeue({**base, "rejection_reason": "Blocked: x"}) is False
    # unparsebare created_at → fail-safe kein Requeue
    assert classify_requeue({**base, "created_at": "garbage"}) is False
    assert classify_requeue({**base, "created_at": None}) is False


def test_requeue_count_migration_idempotent(trade_db):
    from bot.db.repo import TradeRepo
    TradeRepo(trade_db)   # first init adds the column
    TradeRepo(trade_db)   # second init must not fail
    cols = [r[1] for r in trade_db.fetchall("PRAGMA table_info(trades)")]
    assert "requeue_count" in cols


def test_update_status_accepts_requeue_count(trade_db):
    from bot.db.repo import TradeRepo
    repo = TradeRepo(trade_db)
    tid = repo.create(42, "NVDA", "BUY", 100.0, 3.0)
    repo.update_status(tid, "APPROVED", requeue_count=1)
    row = trade_db.fetchone("SELECT status, requeue_count FROM trades WHERE id = ?", (tid,))
    assert row["status"] == "APPROVED"
    assert row["requeue_count"] == 1
