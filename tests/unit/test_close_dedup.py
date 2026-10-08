"""Unit-Tests für close_dedup.py (fix/doppelbuchungen, 2026-10-08).

Testet:
  - extract_order_id: Robuste Order-ID-Extraktion aus API-Antworten
  - has_recent_close: Dedup-Gate für llm_tighten
  - trade_already_closed: Dedup-Gate für risk_sl
"""
from __future__ import annotations

import sqlite3
import tempfile
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/ is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from bot.core.close_dedup import (
    extract_order_id,
    has_recent_close,
    trade_already_closed,
)


# ── extract_order_id ──────────────────────────────────────────────────────────

class TestExtractOrderId:
    def test_basic_orderId(self):
        assert extract_order_id({"orderId": "12345"}) == "12345"

    def test_orderID_caps(self):
        assert extract_order_id({"orderID": "abc"}) == "abc"

    def test_order_id_underscore(self):
        assert extract_order_id({"order_id": 999}) == "999"

    def test_OrderId_mixed(self):
        assert extract_order_id({"OrderId": "X-42"}) == "X-42"

    def test_integer_value(self):
        assert extract_order_id({"orderId": 42}) == "42"

    def test_nested_raw(self):
        assert extract_order_id({"raw": {"orderId": "nested-1"}}) == "nested-1"

    def test_nested_raw_orderID(self):
        assert extract_order_id({"raw": {"orderID": 77}}) == "77"

    def test_no_order_id(self):
        assert extract_order_id({"status": "ok", "success": True}) is None

    def test_empty_string(self):
        assert extract_order_id({"orderId": ""}) is None

    def test_whitespace_only(self):
        assert extract_order_id({"orderId": "   "}) is None

    def test_none_response(self):
        assert extract_order_id(None) is None

    def test_non_dict(self):
        assert extract_order_id("orderId=123") is None
        assert extract_order_id(42) is None
        assert extract_order_id([]) is None

    def test_prefers_top_level_over_raw(self):
        """Top-level orderId wins over nested raw.orderId."""
        resp = {"orderId": "top", "raw": {"orderId": "nested"}}
        assert extract_order_id(resp) == "top"

    def test_falls_back_to_raw_when_no_top(self):
        resp = {"status": 1, "raw": {"orderId": "only-raw"}}
        assert extract_order_id(resp) == "only-raw"

    def test_strips_whitespace(self):
        assert extract_order_id({"orderId": "  123  "}) == "123"


# ── Helper: in-memory DB mit fetchone-Wrapper ────────────────────────────────

class _TestDB:
    """Minimaler DB-Wrapper, der dem Bot-DB-Interface (fetchone) entspricht."""
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        cur = self._conn.execute(sql, params)
        return cur.fetchone()
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)
    def commit(self):
        self._conn.commit()

def _make_db() -> _TestDB:
    """In-memory SQLite with the minimal schema for close_dedup tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE trade_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER,
            position_id TEXT,
            order_id TEXT,
            instrument_id INTEGER,
            symbol TEXT,
            event_type TEXT,
            source TEXT,
            event_at TEXT,
            close_pct REAL,
            units REAL,
            price REAL,
            amount_usd REAL,
            pnl_usd REAL,
            pnl_pct REAL,
            pnl_source TEXT,
            reason TEXT,
            chart_posted INTEGER DEFAULT 0,
            reported_final INTEGER DEFAULT 0,
            spread_pct REAL,
            cost_usd REAL
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_position_id TEXT,
            status TEXT,
            verification_status TEXT,
            created_at TEXT,
            closed_at TEXT
        )
    """)
    conn.commit()
    return _TestDB(conn)


# ── has_recent_close ──────────────────────────────────────────────────────────

class TestHasRecentClose:
    def test_no_events(self):
        db = _make_db()
        assert has_recent_close(db, "pos1") is False

    def test_recent_close_event(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trade_events (position_id, event_type, event_at, symbol) "
            "VALUES ('pos1', 'CLOSE', datetime('now', 'localtime'), 'TEST')"
        )
        db.commit()
        assert has_recent_close(db, "pos1") is True

    def test_recent_partial_close(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trade_events (position_id, event_type, event_at, symbol) "
            "VALUES ('pos1', 'PARTIAL_CLOSE', datetime('now', 'localtime'), 'TEST')"
        )
        db.commit()
        assert has_recent_close(db, "pos1") is True

    def test_old_close_outside_window(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trade_events (position_id, event_type, event_at, symbol) "
            "VALUES ('pos1', 'CLOSE', datetime('now', '-2 hours', 'localtime'), 'TEST')"
        )
        db.commit()
        assert has_recent_close(db, "pos1") is False

    def test_open_event_not_counted(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trade_events (position_id, event_type, event_at, symbol) "
            "VALUES ('pos1', 'OPEN', datetime('now', 'localtime'), 'TEST')"
        )
        db.commit()
        assert has_recent_close(db, "pos1") is False

    def test_different_position(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trade_events (position_id, event_type, event_at, symbol) "
            "VALUES ('pos2', 'CLOSE', datetime('now', 'localtime'), 'TEST')"
        )
        db.commit()
        assert has_recent_close(db, "pos1") is False

    def test_custom_window(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trade_events (position_id, event_type, event_at, symbol) "
            "VALUES ('pos1', 'CLOSE', datetime('now', '-10 minutes', 'localtime'), 'TEST')"
        )
        db.commit()
        # 5-min window: too old
        assert has_recent_close(db, "pos1", window_minutes=5) is False
        # 30-min window: recent enough
        assert has_recent_close(db, "pos1", window_minutes=30) is True

    def test_none_db(self):
        assert has_recent_close(None, "pos1") is False

    def test_empty_position_id(self):
        db = _make_db()
        assert has_recent_close(db, "") is False


# ── trade_already_closed ──────────────────────────────────────────────────────

class TestTradeAlreadyClosed:
    def test_no_trades(self):
        db = _make_db()
        assert trade_already_closed(db, "pos1") is False

    def test_closed_pending(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trades (api_position_id, status, verification_status) "
            "VALUES ('pos1', 'CLOSED', 'PENDING')"
        )
        db.commit()
        assert trade_already_closed(db, "pos1") is True

    def test_closed_verified(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trades (api_position_id, status, verification_status) "
            "VALUES ('pos1', 'CLOSED', 'VERIFIED')"
        )
        db.commit()
        assert trade_already_closed(db, "pos1") is True

    def test_active_trade(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trades (api_position_id, status) "
            "VALUES ('pos1', 'ACTIVE')"
        )
        db.commit()
        assert trade_already_closed(db, "pos1") is False

    def test_submitted_trade(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trades (api_position_id, status) "
            "VALUES ('pos1', 'SUBMITTING')"
        )
        db.commit()
        assert trade_already_closed(db, "pos1") is False

    def test_different_position(self):
        db = _make_db()
        db.execute(
            "INSERT INTO trades (api_position_id, status) "
            "VALUES ('pos2', 'CLOSED')"
        )
        db.commit()
        assert trade_already_closed(db, "pos1") is False

    def test_none_db(self):
        assert trade_already_closed(None, "pos1") is False

    def test_empty_position_id(self):
        db = _make_db()
        assert trade_already_closed(db, "") is False


if __name__ == "__main__":
    # Simple runner without pytest
    import traceback
    classes = [TestExtractOrderId, TestHasRecentClose, TestTradeAlreadyClosed]
    passed = failed = 0
    for cls in classes:
        inst = cls()
        for name in dir(inst):
            if name.startswith("test_"):
                try:
                    getattr(inst, name)()
                    passed += 1
                except Exception as e:
                    failed += 1
                    print(f"FAIL {cls.__name__}.{name}: {e}")
                    traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
