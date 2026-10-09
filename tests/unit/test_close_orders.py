"""Tests für close_orders.py (Phase 0a, 2026-10-09).

Coverage:
  - ensure_table: idempotent, fail-open
  - sync_close_orders: INSERT neuer Orders, EXECUTED verschwundener, stats
  - has_open_close_order: True/False, fail-open
  - record_close_order: INSERT + ON CONFLICT
  - mark_order_executed
  - _extract_order_id: verschiedene Key-Namen
"""
import sqlite3
import pytest

from bot.core import close_orders as co


# ── Fixtures ─────────────────────────────────────────────────────────────────

class _FakeRow:
    """Row-dict with key access (wie db.fetchone / fetchall)."""
    def __init__(self, data: dict):
        self._d = data
    def __getitem__(self, key):
        return self._d[key]
    def __bool__(self):
        return bool(self._d)


class _FakeDB:
    """Minimaler DB-Wrapper: execute, fetchone, fetchall, commit."""
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.executed: list = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        self.conn.execute(sql, params)

    def fetchone(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return _FakeRow(dict(row)) if row else None

    def fetchall(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        return [_FakeRow(dict(r)) for r in cur.fetchall()]

    def commit(self):
        self.conn.commit()


@pytest.fixture
def db():
    d = _FakeDB()
    co.ensure_table(d)
    return d


# ── ensure_table ─────────────────────────────────────────────────────────────

class TestEnsureTable:
    def test_idempotent(self, db):
        # Zweiter Aufruf darf nicht crashen
        co.ensure_table(db)

    def test_fail_open(self):
        class BadDB:
            def execute(self, *a, **kw):
                raise RuntimeError("boom")
        # Darf nicht werfen
        co.ensure_table(BadDB())


# ── sync_close_orders ────────────────────────────────────────────────────────

class TestSyncCloseOrders:
    def test_insert_new_orders(self, db):
        orders = [
            {"orderID": "111", "positionID": "100", "instrumentID": 42,
             "unitsToDeduct": 5.0, "openDateTime": "2026-10-09T12:00:00Z"},
            {"orderID": "222", "positionID": "200", "instrumentID": 43,
             "unitsToDeduct": 3.0, "openDateTime": "2026-10-09T13:00:00Z"},
        ]
        stats = co.sync_close_orders(db, orders)
        assert stats["inserted"] == 2
        assert stats["total_open"] == 2

    def test_symbol_resolution(self, db):
        orders = [{"orderID": "333", "positionID": "300", "instrumentID": 99,
                   "unitsToDeduct": 1.0}]
        imap = {99: "TEST.AX"}
        co.sync_close_orders(db, orders, imap)
        row = db.fetchone("SELECT symbol FROM close_orders WHERE order_id='333'")
        assert row["symbol"] == "TEST.AX"

    def test_order_disappeared_becomes_executed(self, db):
        # Erster Sync: 1 Order
        orders = [{"orderID": "444", "positionID": "400", "instrumentID": 50,
                   "unitsToDeduct": 2.0}]
        co.sync_close_orders(db, orders)
        assert db.fetchone("SELECT status FROM close_orders WHERE order_id='444'")["status"] == "OPEN"

        # Zweiter Sync: Order fehlt → EXECUTED
        stats = co.sync_close_orders(db, [])
        assert stats["executed"] == 1
        row = db.fetchone("SELECT status FROM close_orders WHERE order_id='444'")
        assert row["status"] == "EXECUTED"

    def test_upsert_on_conflict(self, db):
        orders = [{"orderID": "555", "positionID": "500", "instrumentID": 60,
                   "unitsToDeduct": 1.0}]
        co.sync_close_orders(db, orders)
        # Zweiter Sync mit gleicher order_id → UPDATE, nicht Fehler
        co.sync_close_orders(db, orders)
        row = db.fetchone("SELECT status FROM close_orders WHERE order_id='555'")
        assert row["status"] == "OPEN"

    def test_empty_list(self, db):
        stats = co.sync_close_orders(db, [])
        assert stats["inserted"] == 0
        assert stats["total_open"] == 0


# ── has_open_close_order ─────────────────────────────────────────────────────

class TestHasOpenCloseOrder:
    def test_true_when_open(self, db):
        co.record_close_order(db, "666", "600", "TEST.AX")
        assert co.has_open_close_order(db, "600") is True

    def test_false_when_no_order(self, db):
        assert co.has_open_close_order(db, "999") is False

    def test_false_when_executed(self, db):
        co.record_close_order(db, "777", "700", "TEST.AX")
        co.mark_order_executed(db, "777")
        assert co.has_open_close_order(db, "700") is False

    def test_fail_open_none_db(self):
        assert co.has_open_close_order(None, "123") is False

    def test_fail_open_bad_db(self):
        class BadDB:
            def fetchone(self, *a, **kw):
                raise RuntimeError("boom")
        assert co.has_open_close_order(BadDB(), "123") is False


# ── record_close_order ───────────────────────────────────────────────────────

class TestRecordCloseOrder:
    def test_insert(self, db):
        co.record_close_order(db, "888", "800", "TEST.AX", 61, 4.5)
        row = db.fetchone("SELECT * FROM close_orders WHERE order_id='888'")
        assert row["position_id"] == "800"
        assert row["symbol"] == "TEST.AX"
        assert row["status"] == "OPEN"
        assert row["units_to_deduct"] == 4.5

    def test_upsert(self, db):
        co.record_close_order(db, "999", "900", "TEST.AX")
        co.record_close_order(db, "999", "900", "TEST.AX")
        rows = db.fetchall("SELECT * FROM close_orders WHERE order_id='999'")
        assert len(rows) == 1

    def test_no_order_id(self, db):
        # Leerer order_id → kein INSERT
        co.record_close_order(db, "", "900", "TEST.AX")
        assert db.fetchone("SELECT 1 FROM close_orders WHERE order_id=''") is None


# ── mark_order_executed ──────────────────────────────────────────────────────

class TestMarkOrderExecuted:
    def test_mark(self, db):
        co.record_close_order(db, "1000", "100", "TEST.AX")
        co.mark_order_executed(db, "1000")
        row = db.fetchone("SELECT status FROM close_orders WHERE order_id='1000'")
        assert row["status"] == "EXECUTED"


# ── _extract_order_id ────────────────────────────────────────────────────────

class TestExtractOrderId:
    def test_orderID(self):
        assert co._extract_order_id({"orderID": "123"}) == "123"

    def test_orderId(self):
        assert co._extract_order_id({"orderId": "456"}) == "456"

    def test_order_id(self):
        assert co._extract_order_id({"order_id": "789"}) == "789"

    def test_OrderId(self):
        assert co._extract_order_id({"OrderId": "012"}) == "012"

    def test_missing(self):
        assert co._extract_order_id({}) is None

    def test_empty_string(self):
        assert co._extract_order_id({"orderID": "  "}) is None


# ── Integration: Gate-Logik (Wächter-Test aus dem Plan) ─────────────────────

class TestGateIntegration:
    """Der kritische Wächter-Test: Open-Order → kein zweiter Close."""

    def test_open_order_blocks_second_close(self, db):
        """Wenn ein Close-Order offen ist, muss has_open_close_order True sein.

        Das ist das GATE: der Worker prüft diese Funktion vor jedem
        close_position(). True → kein zweiter Close.
        """
        # Simuliert: Reconciler hat ordersForClose gesynct (1 offener Order)
        orders = [{"orderID": "2000", "positionID": "1500", "instrumentID": 70,
                   "unitsToDeduct": 0.0}]
        co.sync_close_orders(db, orders)

        # Worker will einen neuen Close auf position 1500 → GATE muss True sein
        assert co.has_open_close_order(db, "1500") is True

    def test_no_open_order_allows_close(self, db):
        """Kein offener Order → GATE False → Close erlaubt."""
        assert co.has_open_close_order(db, "2500") is False

    def test_executed_order_allows_close(self, db):
        """Order executed → GATE False → nächster Close erlaubt."""
        orders = [{"orderID": "3000", "positionID": "2500", "instrumentID": 70,
                   "unitsToDeduct": 0.0}]
        co.sync_close_orders(db, orders)
        assert co.has_open_close_order(db, "2500") is True

        # Order wird executed (aus API-Liste verschwunden)
        co.sync_close_orders(db, [])
        assert co.has_open_close_order(db, "2500") is False
