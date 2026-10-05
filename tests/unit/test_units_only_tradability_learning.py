"""Unit tests — fix/units-only-tradability (2026-10-05).

Zwei Luecken, ein Lerner:
  1. UnitsOnlyMinShare (client-Block, Real Shares): ABB.ST lief am
     2026-09-30 3x durch den vollen Order-Path und FAILED. Der Titel IST
     handelbar — aber NUR ab 1 whole share. Der Lerner schreibt den
     Anteilswert als min_position_amount (+learned_at), damit der
     signal_worker pre-rejects (BROKER_MIN). is_tradable bleibt UNANGEFASST
     (der waechstliche Tradability-Sync wuerde is_tradable=0 sonst zurueck
     setzen und der Fehler nur verschoeben).
  2. notFoundInstrumentIds: SLV (id 834108) lief 2x (2026-10-01/02) in
     "Blocked: Instrument 834108 not found (not tradable)" und der Lerner
     kannte das Pattern nicht — is_tradable blieb 1 und der
     Core-Sweep-Auto-Whitelist (24h-TTL) queue-te es jeden Tag neu.

Tests laufen gegen in-memory SQLite (tmp-Pfad, eigenes Schema) — NIE gegen
data/trading.db (Harte Regel 1).
"""
from __future__ import annotations

import sqlite3

from bot.workers.execution_worker import (
    _learn_from_rejection,
    is_instrument_not_found_error,
    is_units_only_min_share_error,
    parse_units_only_price,
)

UNITS_ONLY_ERROR = (
    "UnitsOnlyMinShare: 100.00 buys 0.1038 share(s) (< 1) of ABB.ST "
    "at 963.40000000 — increase size to at least one whole share"
)
NOT_FOUND_ERROR = "Instrument 834108 not found (not tradable)"


# ── pure detector/parse functions ────────────────────────────────────────────

class TestPureFunctions:
    def test_units_only_detected(self):
        assert is_units_only_min_share_error(UNITS_ONLY_ERROR) is True

    def test_units_only_with_blocked_prefix(self):
        # Preflight-Blocks landen im Trade als "Blocked: ..."
        assert is_units_only_min_share_error("Blocked: " + UNITS_ONLY_ERROR) is True

    def test_units_only_negative(self):
        assert is_units_only_min_share_error(
            "eToro 720: MinimumPositionAmount: 1000 (Dollars)") is False
        assert is_units_only_min_share_error(
            "Instrument 834108 not found (not tradable)") is False
        assert is_units_only_min_share_error(None) is False
        assert is_units_only_min_share_error("") is False

    def test_parse_units_only_price(self):
        shares, price = parse_units_only_price(UNITS_ONLY_ERROR)
        assert shares == 0.1038
        assert price == 963.4

    def test_parse_units_only_price_garbage(self):
        assert parse_units_only_price(None) == (None, None)
        assert parse_units_only_price("UnitsOnlyMinShare: unparsable") == (
            None, None)

    def test_not_found_detected(self):
        assert is_instrument_not_found_error(NOT_FOUND_ERROR) is True
        assert is_instrument_not_found_error("Blocked: " + NOT_FOUND_ERROR) is True

    def test_not_found_negative(self):
        assert is_instrument_not_found_error(
            "eToro 814: visible internal only") is False
        assert is_instrument_not_found_error(
            UNITS_ONLY_ERROR) is False
        assert is_instrument_not_found_error(None) is False
        assert is_instrument_not_found_error("") is False


# ── _learn_from_rejection integration (in-memory DB) ────────────────────────

class _FakeDb:
    """Wickelt echte SQL auf einer in-memory-DB ab."""

    def __init__(self, instrument_id=1):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE instruments (
                instrument_id INTEGER PRIMARY KEY,
                symbol TEXT,
                is_tradable INTEGER,
                tradability_checked_at TEXT,
                min_position_amount REAL,
                min_position_amount_learned_at TEXT
            )
        """)
        self.conn.execute(
            "INSERT INTO instruments (instrument_id, symbol, is_tradable) "
            "VALUES (?,?,1)", (instrument_id, "SYM"))
        self.conn.commit()

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def row(self, instrument_id=1):
        return self.conn.execute(
            "SELECT * FROM instruments WHERE instrument_id=?",
            (instrument_id,)).fetchone()


class TestLearnUnitsOnly:
    def test_learns_share_price_with_timestamp(self):
        db = _FakeDb(42)
        _learn_from_rejection(db, 42, "ABB.ST", UNITS_ONLY_ERROR)
        r = db.row(42)
        assert r["min_position_amount"] == 963.4
        assert r["min_position_amount_learned_at"] is not None

    def test_does_not_touch_is_tradable(self):
        """unitsOnly-Titel IST handelbar — is_tradable bleibt 1."""
        db = _FakeDb(42)
        _learn_from_rejection(db, 42, "ABB.ST", UNITS_ONLY_ERROR)
        assert db.row(42)["is_tradable"] == 1

    def test_unparsable_price_still_no_crash(self):
        db = _FakeDb(7)
        _learn_from_rejection(db, 7, "X", "UnitsOnlyMinShare: garbled")
        assert db.row(7)["min_position_amount"] is None
        assert db.row(7)["is_tradable"] == 1


class TestLearnNotFound:
    def test_marks_not_tradable(self):
        db = _FakeDb(834108)
        _learn_from_rejection(db, 834108, "SLV", NOT_FOUND_ERROR)
        r = db.row(834108)
        assert r["is_tradable"] == 0
        assert r["tradability_checked_at"] is not None

    def test_blocked_prefix_still_marks(self):
        db = _FakeDb(834108)
        _learn_from_rejection(db, 834108, "SLV", "Blocked: " + NOT_FOUND_ERROR)
        assert db.row(834108)["is_tradable"] == 0

    def test_does_not_learn_min_amount(self):
        db = _FakeDb(834108)
        _learn_from_rejection(db, 834108, "SLV", NOT_FOUND_ERROR)
        assert db.row(834108)["min_position_amount"] is None

    def test_never_raises_on_garbage(self):
        _learn_from_rejection(None, 1, "Z", None)  # db=None -> swallowed
        _learn_from_rejection(None, 1, "Z", "random error text")


# ── signal_worker: effective_broker_min (Freshness des gelernten Werts) ─────

from datetime import datetime, timedelta, timezone

from bot.workers.signal_worker import effective_broker_min


class TestEffectiveBrokerMin:
    def test_static_min_always_effective(self):
        """eToro-720-Minimum (kein learned_at) gilt unveraendert."""
        assert effective_broker_min(1000.0, None) == 1000.0
        assert effective_broker_min(1000.0, "") == 1000.0

    def test_fresh_learned_value_effective(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        assert effective_broker_min(963.4, ts) == 963.4

    def test_stale_learned_value_fails_open(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        assert effective_broker_min(963.4, ts) is None

    def test_boundary_7_days(self):
        now = datetime(2026, 10, 5, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=7, seconds=-60)).isoformat()
        stale = (now - timedelta(days=7, seconds=60)).isoformat()
        assert effective_broker_min(100.0, fresh, now=now) == 100.0
        assert effective_broker_min(100.0, stale, now=now) is None

    def test_naive_learned_at_treated_utc(self):
        ts = "2026-10-05T00:00:00"
        now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        assert effective_broker_min(100.0, ts, now=now) == 100.0

    def test_unparseable_learned_at_fails_open_to_value(self):
        assert effective_broker_min(100.0, "garbled") == 100.0

    def test_none_and_zero(self):
        assert effective_broker_min(None, None) is None
        assert effective_broker_min(0.0, None) is None
        assert effective_broker_min("garbled", None) is None
