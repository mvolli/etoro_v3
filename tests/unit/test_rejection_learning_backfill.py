"""Unit tests — fix/rejection-learning-backfill (2026-10-09).

Der Rejection-Lerner (fix/units-only-tradability, 0bf2e23) ist rein reaktiv —
er lernt nur aus NEUEN Ablehnungen. Instrumente, die VOR dem Lerner FAILED
sind, haben ihre Lernwerte nie erhalten und laufen aktiv weiter:
  - ABB.ST: 3x UnitsOnlyMinShare, min_position_amount blieb leer.
  - SLV / FLOW: dauerhafter Block, is_tradable blieb 1.

aggregate_failed() ist die pure Aggregations-Logik des Backfill-Scripts —
sie verwendet die GLEICHEN pure Detector-/Parse-Functions wie der LIVE-Lerner
(keine Duplikation). Tests laufen gegen in-memory SQLite (Harte Regel 1).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from backfill_rejection_learning import aggregate_failed  # noqa: E402

UNITS_ONLY = (
    "UnitsOnlyMinShare: 100.00 buys 0.1038 share(s) (< 1) of ABB.ST "
    "at 963.40000000 — increase size to at least one whole share"
)
NOT_FOUND = "Blocked: Instrument 834108 not found (not tradable)"
INTERNAL = "Order rejected: eToro 814: instrument is visible internal only"
NOT_ELIGIBLE = "Blocked: Instrument 263 not eligible for real trading (allowOpenPosition=False)"
SEVENTY20 = "Order rejected: eToro 720: MinimumPositionAmount: 1000 (Dollars)"
UNRELATED = "APIError: HTTP 401 Unauthorized"


def _row(iid, sym, reason):
    return {"instrument_id": iid, "symbol": sym, "rejection_reason": reason}


class TestAggregateFailed:
    def test_not_found_marks_tradable_zero(self):
        tz, ma = aggregate_failed([_row(834108, "SLV", NOT_FOUND)])
        assert tz == {834108: "SLV"}
        assert ma == {}

    def test_internal_only_marks_tradable_zero(self):
        tz, _ = aggregate_failed([_row(100500, "FLOW", INTERNAL)])
        assert tz == {100500: "FLOW"}

    def test_not_eligible_marks_tradable_zero(self):
        tz, _ = aggregate_failed([_row(263, "HKG50.FUT", NOT_ELIGIBLE)])
        assert tz == {263: "HKG50.FUT"}

    def test_units_only_learns_share_price(self):
        tz, ma = aggregate_failed([_row(1014564, "ABB.ST", UNITS_ONLY)])
        assert tz == {}  # unitsOnly IST handelbar -> is_tradable unangefasst
        assert ma == {1014564: (963.4, True)}

    def test_static_720_minimum(self):
        tz, ma = aggregate_failed([_row(700, "NATGAS", SEVENTY20)])
        assert tz == {}
        assert ma == {700: (1000.0, False)}

    def test_unrelated_reason_ignored(self):
        tz, ma = aggregate_failed([_row(1, "X", UNRELATED)])
        assert tz == {}
        assert ma == {}

    def test_highest_units_price_wins(self):
        # Zwei Ablehnungen, Kurs ist gestiegen — der hoeherer Anteilswert gewinnt.
        low = "UnitsOnlyMinShare: 50.00 buys 0.05 share(s) (< 1) of A at 900.00 — increase"
        high = "UnitsOnlyMinShare: 50.00 buys 0.05 share(s) (< 1) of A at 990.00 — increase"
        _, ma = aggregate_failed([_row(9, "A", low), _row(9, "A", high)])
        assert ma == {9: (990.0, True)}

    def test_multiple_instruments(self):
        rows = [
            _row(834108, "SLV", NOT_FOUND),
            _row(1014564, "ABB.ST", UNITS_ONLY),
            _row(100500, "FLOW", INTERNAL),
            _row(1, "OK", UNRELATED),
        ]
        tz, ma = aggregate_failed(rows)
        assert set(tz) == {834108, 100500}
        assert set(ma) == {1014564}


class TestBackfillIdempotentOnTmpDb:
    """End-to-end auf einer in-memory SQLite — NIE gegen data/trading.db."""

    def _mkdb(self, tmp_path):
        db_path = tmp_path / "t.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE instruments (instrument_id INTEGER, symbol TEXT, "
                     "is_tradable INTEGER, min_position_amount REAL, "
                     "min_position_amount_learned_at TEXT, tradability_checked_at TEXT)")
        conn.execute("CREATE TABLE trades (instrument_id INTEGER, symbol TEXT, "
                     "status TEXT, rejection_reason TEXT)")
        conn.commit()
        return conn, db_path

    def _run_script(self, conn, db_path, dry_run=False):
        # Führt das Script-Modul auf der tmp-DB aus. main() importiert
        # load_config/DB lokal, daher werden die QUELL-MODULE gepatcht.
        import importlib
        import backfill_rejection_learning as mod
        importlib.reload(mod)

        from types import SimpleNamespace
        cfg = SimpleNamespace(db=SimpleNamespace(path=str(db_path)))

        class FakeDB:
            def __init__(self, **kw):
                self._c = conn
                self._c.row_factory = sqlite3.Row
            def fetchall(self, sql, params=()):
                return self._c.execute(sql, params).fetchall()
            def fetchone(self, sql, params=()):
                return self._c.execute(sql, params).fetchone()
            def execute(self, sql, params=()):
                cur = self._c.execute(sql, params)
                self._c.commit()
                return cur

        import bot.config as _bcfg
        import bot.db.connection as _bconn
        import unittest.mock as mock
        with mock.patch.object(_bcfg, "load_config", return_value=cfg), \
             mock.patch.object(_bconn, "DB", FakeDB):
            sys.argv = ["backfill", "--dry-run"] if dry_run else ["backfill"]
            mod.main()

    def test_backfill_applies_and_is_idempotent(self, tmp_path):
        conn, db_path = self._mkdb(tmp_path)
        conn.execute("INSERT INTO instruments VALUES (834108,'SLV',1,NULL,NULL,NULL)")
        conn.execute("INSERT INTO instruments VALUES (1014564,'ABB.ST',1,NULL,NULL,NULL)")
        conn.execute("INSERT INTO instruments VALUES (100500,'FLOW',1,NULL,NULL,NULL)")
        conn.execute("INSERT INTO trades VALUES (834108,'SLV','FAILED',?)", (NOT_FOUND,))
        conn.execute("INSERT INTO trades VALUES (1014564,'ABB.ST','FAILED',?)", (UNITS_ONLY,))
        conn.execute("INSERT INTO trades VALUES (100500,'FLOW','FAILED',?)", (INTERNAL,))
        conn.commit()

        self._run_script(conn, db_path, dry_run=False)

        slv = conn.execute("SELECT is_tradable FROM instruments WHERE symbol='SLV'").fetchone()
        flow = conn.execute("SELECT is_tradable FROM instruments WHERE symbol='FLOW'").fetchone()
        abb = conn.execute("SELECT min_position_amount, min_position_amount_learned_at FROM instruments WHERE symbol='ABB.ST'").fetchone()
        assert slv["is_tradable"] == 0
        assert flow["is_tradable"] == 0
        assert abb["min_position_amount"] == pytest.approx(963.4)
        assert abb["min_position_amount_learned_at"] is not None  # KURS -> learned_at gesetzt

        # Zweiter Lauf: keine Aenderungen mehr (idempotent).
        self._run_script(conn, db_path, dry_run=False)
        slv2 = conn.execute("SELECT is_tradable FROM instruments WHERE symbol='SLV'").fetchone()
        assert slv2["is_tradable"] == 0
        conn.close()
