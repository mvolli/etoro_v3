"""Unit tests — fix/eligibility-tradability-learning (2026-09-18).

Zwei Luecken, ein Fix:
  1. Der Rejection-Lerner kannte nur eToro-814 ("visible internal only").
     Der PREFLIGHT-Eligibility-Block (allowOpenPosition=false) markierte
     einen Titel NIE als is_tradable=0 — er wurde also nie von
     Discovery/Signal gefiltert und (via Core-Sweep-Auto-Whitelist, 24h-TTL)
     in einer Endlosschleife neu versuch: NSDQ100.FUT 22x, JPN225.FUT 15x,
     HKG50.FUT 3x — alle FAILED, 0 Orders (2026-07-29..2026-09-15).
  2. plan_core_sweep war die EINZIGE Stelle OHNE is_tradable-Filter —
     selbst nachdem ein Titel is_tradable=0 markiert war, der Planer
     kandidierte ihn aus der DB-Whitelist trotzdem weiter.

Tests laufen gegen in-memory SQLite (tmp-Pfad, eigenes Schema) — NIE gegen
data/trading.db (Harte Regel 1).
"""
from __future__ import annotations

import sqlite3

import pytest

from bot.core.core_sweep import (
    _ensure_core_sweep_whitelist_table,
    plan_core_sweep,
)
from bot.workers.execution_worker import (
    _learn_from_rejection,
    is_internal_only_error,
    is_not_eligible_error,
)

# ── is_not_eligible_error (pure) ─────────────────────────────────────────────

ELIG_ERROR = (
    "Instrument 255 not eligible for real trading (allowOpenPosition=False)"
)


class TestIsNotEligibleError:
    def test_eligibility_block_detected(self):
        assert is_not_eligible_error(ELIG_ERROR) is True

    def test_eligibility_block_preflight_prefix(self):
        # Preflight-Blocks landen im Trade als "Blocked: ..."
        assert is_not_eligible_error("Blocked: " + ELIG_ERROR) is True

    def test_entry_orders_defer_not_affected(self):
        assert is_not_eligible_error(
            "allowEntryOrders=False (market closed)"
        ) is False

    def test_internal_only_not_conflated(self):
        assert is_not_eligible_error("eToro 814: visible internal only") is False

    def test_none_and_empty(self):
        assert is_not_eligible_error(None) is False
        assert is_not_eligible_error("") is False


# ── _learn_from_rejection: allowOpenPosition=false -> is_tradable=0 ─────────

class _FakeDb:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))


class TestLearnFromRejection:
    def test_allowopenposition_false_marks_not_tradable(self):
        db = _FakeDb()
        _learn_from_rejection(db, 255, "NSDQ100.FUT", ELIG_ERROR)
        assert any(
            "is_tradable = 0" in sql and params == (255,)
            for sql, params in db.executed
        )

    def test_internal_only_still_marks_not_tradable(self):
        db = _FakeDb()
        _learn_from_rejection(db, 9, "X", "eToro 814: visible internal only")
        assert any(
            "is_tradable = 0" in sql and params == (9,)
            for sql, params in db.executed
        )

    def test_min_amount_error_does_not_mark_not_tradable(self):
        db = _FakeDb()
        _learn_from_rejection(
            db, 1, "Y", "eToro 720: MinimumPositionAmount: 1000 (Dollars)"
        )
        assert not any("is_tradable = 0" in sql for sql, _ in db.executed)

    def test_min_amount_still_learned(self):
        db = _FakeDb()
        _learn_from_rejection(
            db, 1, "Y", "eToro 720: MinimumPositionAmount: 1000 (Dollars)"
        )
        assert any(
            "min_position_amount" in sql and params == (1000.0, 1)
            for sql, params in db.executed
        )

    def test_never_raises_on_garbage(self):
        db = _FakeDb()
        _learn_from_rejection(None, 1, "Z", None)  # db=None -> swallowed


# ── plan_core_sweep: is_tradable-Filter ──────────────────────────────────────

@pytest.fixture
def sweep_db(tmp_path):
    """Echte in-memory-DB mit instruments + core_sweep_whitelist."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE instruments (
            instrument_id INTEGER PRIMARY KEY,
            symbol TEXT,
            is_tradable INTEGER,
            yahoo_status TEXT,
            min_position_amount REAL
        )
    """)
    _ensure_core_sweep_whitelist_table(conn)

    class Db:
        def fetchone(self, sql, params=()):
            return conn.execute(sql, params).fetchone()

        def fetchall(self, sql, params=()):
            return conn.execute(sql, params).fetchall()

        def execute(self, sql, params=()):
            return conn.execute(sql, params)

        def close(self):
            conn.close()

    yield Db()
    conn.close()


def _cfg(**over) -> dict:
    block = {
        "enabled": True,
        "reserve_target_pct": 15.0,
        "reserve_floor_pct": 10.0,
        "per_position_pct": 4.0,
        "max_position_pct": 6.0,
        "max_sweeps_per_run": 4,
        "rsi_overbought": 75.0,
        "regimes": ["NORMAL", "CAUTION"],
        "whitelist": {"AAA": 1, "BBB": 2},
    }
    block.update(over)
    return {"trading": {"core_sweep": block}}


def _seed_instruments(db, rows):
    for iid, sym, tradable in rows:
        db.execute(
            "INSERT INTO instruments (instrument_id, symbol, is_tradable) "
            "VALUES (?,?,?)",
            (iid, sym, tradable),
        )


def test_not_tradable_candidate_is_skipped(sweep_db):
    _seed_instruments(sweep_db, [(1, "AAA", 0), (2, "BBB", 1)])
    orders, reasons = plan_core_sweep(
        _cfg(), equity=10_000.0, cash=3_000.0, regime="NORMAL", db=sweep_db
    )
    assert [o.instrument_id for o in orders] == [2]
    assert any("AAA: is_tradable=0 (Broker) — SKIP" in r for r in reasons)


def test_both_not_tradable_no_orders(sweep_db):
    _seed_instruments(sweep_db, [(1, "AAA", 0), (2, "BBB", 0)])
    orders, reasons = plan_core_sweep(
        _cfg(), equity=10_000.0, cash=3_000.0, regime="NORMAL", db=sweep_db
    )
    assert orders == []
    assert any("is_tradable=0 (Broker)" in r for r in reasons)


def test_null_tradable_is_fail_open(sweep_db):
    """is_tradable=NULL heisst 'noch nie geprueft' -> weiterhin Kandidat."""
    _seed_instruments(sweep_db, [(1, "AAA", None), (2, "BBB", 1)])
    orders, _ = plan_core_sweep(
        _cfg(), equity=10_000.0, cash=3_000.0, regime="NORMAL", db=sweep_db
    )
    assert {o.instrument_id for o in orders} == {1, 2}


def test_missing_row_is_fail_open(sweep_db):
    """Keine instruments-Zeile (z.B. Test-Doppel) -> fail-open wie heute."""
    _seed_instruments(sweep_db, [(1, "AAA", None)])  # BBB(2) fehlt
    orders, _ = plan_core_sweep(
        _cfg(), equity=10_000.0, cash=3_000.0, regime="NORMAL", db=sweep_db
    )
    assert {o.instrument_id for o in orders} == {1, 2}


def test_without_db_argument_unchanged():
    """Rueckwaerts-Kompatibilitaet: ohne db kein Filter, volles Ergebnis."""
    orders, _ = plan_core_sweep(
        _cfg(), equity=10_000.0, cash=3_000.0, regime="NORMAL"
    )
    assert {o.instrument_id for o in orders} == {1, 2}
