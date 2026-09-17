"""Unit tests — fix/rebuy-cooldown-closed (2026-09-17).

Der Re-Buy-Cooldown des Signal-Workers deckte nur
status IN ('APPROVED','SUBMITTING','ACTIVE') ab. Ein Exposure-Auto-Trim,
der die Position GANZ schliesst (min_remaining_pct: 50), setzt den Trade
aber auf CLOSED und fiel damit aus der Sperre: naechster FRESH-Signal-Zyklus
kaufte denselben Namen sofort wieder (9531.T dreimal in 3 Tagen; GFRD.L
8 Min; 5101.T 77 Min).

recent_buy_instrument_ids() muss CLOSED-Trades jetzt mitzaehlen, gemaessen
an closed_at. Die SQL-Queries laufen hier gegen eine echte in-memory
SQLite-DB — nicht gegen einen Mock — damit die Syntax selbst gedeckt ist.
"""
import sqlite3

import pytest

from bot.core.rebuy_cooldown import recent_buy_instrument_ids


@pytest.fixture
def sqlite_db(tmp_path):
    """Echte in-memory-DB mit trades-Table + Wrapper im DB-Interface."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            instrument_id INTEGER,
            symbol TEXT,
            status TEXT,
            created_at TEXT,
            closed_at TEXT
        )
    """)

    class Db:
        def __init__(self):
            self.conn = conn

        def fetchall(self, sql, params=()):
            return conn.execute(sql, params).fetchall()

        def close(self):
            conn.close()

    yield Db()
    conn.close()


NOW = "2026-09-16 12:00:00"


def _insert(db, iid, symbol, status, created, closed=None):
    db.conn.execute(
        "INSERT INTO trades (instrument_id, symbol, status, created_at, closed_at) "
        "VALUES (?,?,?,?,?)", (iid, symbol, status, created, closed))


def test_open_position_blocks(sqlite_db):
    _insert(sqlite_db, 1, "FOO", "ACTIVE", "2026-09-16 09:00:00")
    assert recent_buy_instrument_ids(sqlite_db, 6.0, NOW) == {1}


@pytest.mark.parametrize("status", ["APPROVED", "SUBMITTING", "ACTIVE"])
def test_each_open_status_blocks(sqlite_db, status):
    _insert(sqlite_db, 7, "BAR", status, "2026-09-16 10:00:00")
    assert recent_buy_instrument_ids(sqlite_db, 6.0, NOW) == {7}


def test_closed_trade_inside_window_blocks(sqlite_db):
    """Der Churn-Fix: CLOSED innerhalb des Fensters ist gesperrt."""
    _insert(sqlite_db, 2, "BAZ", "CLOSED", "2026-09-16 05:00:00",
            "2026-09-16 11:00:00")
    assert recent_buy_instrument_ids(sqlite_db, 6.0, NOW) == {2}


def test_closed_trade_outside_window_does_not_block(sqlite_db):
    """Gekauft UND geschlossen vor dem Fenster -> wieder frei."""
    _insert(sqlite_db, 3, "OLD", "CLOSED", "2026-09-14 05:00:00",
            "2026-09-14 09:00:00")
    assert recent_buy_instrument_ids(sqlite_db, 6.0, NOW) == set()


def test_closed_by_created_at_only_does_not_block(sqlite_db):
    """Gekauft vor 10h, erst vor 1h geschlossen -> Sperrfenster laeuft
    von closed_at, NICHT von created_at (sonst doppelte Sperre)."""
    _insert(sqlite_db, 4, "LATE", "CLOSED", "2026-09-15 20:00:00",
            "2026-09-16 11:00:00")
    assert recent_buy_instrument_ids(sqlite_db, 6.0, NOW) == {4}


def test_rejected_and_failed_never_block(sqlite_db):
    _insert(sqlite_db, 5, "REJ", "REJECTED", "2026-09-16 11:30:00")
    _insert(sqlite_db, 6, "FAIL", "FAILED", "2026-09-16 11:30:00",
            "2026-09-16 11:31:00")
    assert recent_buy_instrument_ids(sqlite_db, 6.0, NOW) == set()


def test_closed_null_closed_at_never_blocks(sqlite_db):
    """CLOSED ohne closed_at (verlorenes Zeitstempel) darf die Sperre
    nicht endlos machen — und auch nicht fehlschlagen."""
    _insert(sqlite_db, 8, "NOCLOSE", "CLOSED", "2026-09-16 11:30:00", None)
    assert recent_buy_instrument_ids(sqlite_db, 6.0, NOW) == set()


def test_mixed_statuses(sqlite_db):
    _insert(sqlite_db, 1, "A", "ACTIVE", "2026-09-16 11:00:00")
    _insert(sqlite_db, 2, "B", "CLOSED", "2026-09-16 10:00:00",
            "2026-09-16 11:30:00")
    _insert(sqlite_db, 3, "C", "CLOSED", "2026-09-16 01:00:00",
            "2026-09-16 05:00:00")
    _insert(sqlite_db, 4, "D", "REJECTED", "2026-09-16 11:00:00")
    assert recent_buy_instrument_ids(sqlite_db, 6.0, NOW) == {1, 2}


def test_disabled_returns_empty(sqlite_db):
    _insert(sqlite_db, 1, "A", "ACTIVE", "2026-09-16 11:00:00")
    assert recent_buy_instrument_ids(sqlite_db, 0.0, NOW) == set()
    assert recent_buy_instrument_ids(sqlite_db, None, NOW) == set()


def test_fails_open_on_db_error(sqlite_db):
    sqlite_db.close()
    assert recent_buy_instrument_ids(sqlite_db, 6.0, NOW) == set()


def test_default_now_uses_real_clock(sqlite_db):
    """now_iso=None -> datetime('now'): eine gerade geschlossene Position
    wird ohne explizite Uhr gesperrt."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    created = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    closed = (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    _insert(sqlite_db, 9, "NOW", "CLOSED", created, closed)
    assert 9 in recent_buy_instrument_ids(sqlite_db, 6.0)
