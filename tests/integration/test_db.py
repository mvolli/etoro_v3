"""Integration tests for DB layer — hermetic (in-memory temp DB)."""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, "src")

import pytest
from bot.db.connection import DB
from bot.db.repo import StateRepo, LogRepo


# ── Schema (mirrored from scripts/init_db.py — keep in sync) ───────────────

TABLES = [
    (
        "instruments",
        """
        CREATE TABLE IF NOT EXISTS instruments (
            instrument_id   INTEGER PRIMARY KEY,
            symbol          TEXT NOT NULL UNIQUE,
            name            TEXT,
            sector          TEXT,
            asset_class     TEXT,
            last_updated    TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
        """,
    ),
    (
        "signals",
        """
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id   INTEGER NOT NULL REFERENCES instruments(instrument_id),
            generated_at    TEXT NOT NULL DEFAULT (datetime('now','utc')),
            signal_type     TEXT NOT NULL,
            conviction      TEXT NOT NULL CHECK(conviction IN ('VERY_HIGH','HIGH','MEDIUM','LOW')),
            score           REAL NOT NULL,
            rsi             REAL,
            macd_hist       REAL,
            bb_pct          REAL,
            price           REAL,
            expires_at      TEXT NOT NULL
        )
        """,
    ),
    (
        "trades",
        """
        CREATE TABLE IF NOT EXISTS trades (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id       INTEGER NOT NULL REFERENCES instruments(instrument_id),
            symbol              TEXT NOT NULL,
            direction           TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
            status              TEXT NOT NULL CHECK(status IN (
                                    'PENDING_APPROVAL','APPROVED','SUBMITTING',
                                    'ACTIVE','CLOSING','CLOSED','FAILED','REJECTED'
                                )),
            amount_usd          REAL NOT NULL,
            stop_loss_pct       REAL NOT NULL DEFAULT 3.0,
            stop_loss_price     REAL,
            api_position_id     TEXT,
            entry_price         REAL,
            exit_price          REAL,
            pnl_usd             REAL,
            pnl_pct             REAL,
            rejection_reason    TEXT,
            signal_id           INTEGER REFERENCES signals(id),
            created_at          TEXT NOT NULL DEFAULT (datetime('now','utc')),
            approved_at         TEXT,
            submitted_at        TEXT,
            confirmed_at        TEXT,
            closed_at           TEXT
        )
        """,
    ),
    (
        "portfolio_snapshot",
        """
        CREATE TABLE IF NOT EXISTS portfolio_snapshot (
            api_position_id     TEXT PRIMARY KEY,
            instrument_id       INTEGER,
            symbol              TEXT,
            is_buy              INTEGER NOT NULL DEFAULT 1,
            amount_usd          REAL,
            open_price          REAL,
            current_price       REAL,
            unrealized_pnl      REAL,
            unrealized_pnl_pct  REAL,
            stop_loss_rate      REAL,
            is_no_stop_loss     INTEGER DEFAULT 0,
            last_synced         TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
        """,
    ),
    (
        "system_state",
        """
        CREATE TABLE IF NOT EXISTS system_state (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
        """,
    ),
    (
        "system_log",
        """
        CREATE TABLE IF NOT EXISTS system_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL DEFAULT (datetime('now','utc')),
            level   TEXT NOT NULL CHECK(level IN ('DEBUG','INFO','WARN','WARNING','ERROR','CRITICAL')),
            worker  TEXT NOT NULL,
            message TEXT NOT NULL,
            details TEXT
        )
        """,
    ),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_signals_instrument ON signals(instrument_id, generated_at)",
    "CREATE INDEX IF NOT EXISTS idx_signals_expires    ON signals(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_trades_status      ON trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_trades_instrument  ON trades(instrument_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_portfolio_instrument ON portfolio_snapshot(instrument_id)",
    "CREATE INDEX IF NOT EXISTS idx_syslog_ts          ON system_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_syslog_level       ON system_log(level, ts)",
]

INITIAL_STATE = [
    ("CURRENT_REGIME", "NORMAL"),
    ("PEAK_EQUITY", "10000.0"),
    ("CURRENT_EQUITY", "0.0"),
    ("DRAWDOWN_PCT", "0.0"),
    ("CB_ACTIVE", "false"),
]


def _init_schema(conn: sqlite3.Connection) -> None:
    """Apply the full schema to a connection."""
    for _name, ddl in TABLES:
        conn.execute(ddl)
    for idx_sql in INDEXES:
        conn.execute(idx_sql)
    for key, value in INITIAL_STATE:
        conn.execute(
            "INSERT OR IGNORE INTO system_state (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


@pytest.fixture
def db(tmp_path: Path):
    """Create a temporary file-based DB with the full schema."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(conn)
    finally:
        conn.close()

    return DB(str(db_path))


def test_state_regime(db):
    sr = StateRepo(db)
    sr.set_regime("NORMAL")
    assert sr.get_regime() == "NORMAL"


def test_state_equity(db):
    sr = StateRepo(db)
    sr.set("CURRENT_EQUITY", "9469.16")
    assert abs(sr.get_equity() - 9469.16) < 0.01


def test_log_write(db):
    lr = LogRepo(db)
    lr.write("INFO", "test", "V3 integration test OK")
    r = lr.get_recent(limit=1, worker="test")
    assert r and r[0]["message"] == "V3 integration test OK"


def test_db_fetchone(db):
    r = db.fetchone("SELECT value FROM system_state WHERE key=?", ("CURRENT_REGIME",))
    assert r is not None
    assert r["value"] in ("NORMAL", "DRAWDOWN", "RECOVERY")
