#!/usr/bin/env python3
"""
eToro Trading Bot V3 — Database Initialization Script
Creates data/trading.db with WAL mode and full schema.
Idempotent: safe to run multiple times.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Resolve project root (one level above scripts/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "trading.db"


# ── DDL ──────────────────────────────────────────────────────────────────────

TABLES: list[tuple[str, str]] = [
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

INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_signals_instrument ON signals(instrument_id, generated_at)",
    "CREATE INDEX IF NOT EXISTS idx_signals_expires    ON signals(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_trades_status      ON trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_trades_instrument  ON trades(instrument_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_portfolio_instrument ON portfolio_snapshot(instrument_id)",
    "CREATE INDEX IF NOT EXISTS idx_syslog_ts          ON system_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_syslog_level       ON system_log(level, ts)",
]

INITIAL_STATE: list[tuple[str, str]] = [
    ("CURRENT_REGIME", "NORMAL"),
    ("PEAK_EQUITY", "10000.0"),
    ("CURRENT_EQUITY", "0.0"),
    ("DRAWDOWN_PCT", "0.0"),
    ("CB_ACTIVE", "false"),
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _configure(conn: sqlite3.Connection) -> None:
    """Apply PRAGMAs required for WAL-mode production use."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[init_db] Opening database: {db_path}")
    conn = sqlite3.connect(str(db_path))

    try:
        _configure(conn)
        print("[init_db] PRAGMAs applied (WAL, busy_timeout=5000, foreign_keys=ON, synchronous=NORMAL)")

        # ── tables ────────────────────────────────────────────────────────────
        for table_name, ddl in TABLES:
            conn.execute(ddl)
            print(f"[init_db]   ✓ table  '{table_name}'")

        # ── indexes ───────────────────────────────────────────────────────────
        for idx_sql in INDEXES:
            conn.execute(idx_sql)
            # extract index name for friendly output
            idx_name = idx_sql.split("EXISTS")[1].split("ON")[0].strip()
            print(f"[init_db]   ✓ index  '{idx_name}'")

        # ── seed system_state (INSERT OR IGNORE → idempotent) ─────────────────
        for key, value in INITIAL_STATE:
            conn.execute(
                """
                INSERT OR IGNORE INTO system_state (key, value)
                VALUES (?, ?)
                """,
                (key, value),
            )
            print(f"[init_db]   ✓ state  {key} = {value!r}  (ignored if already present)")

        conn.commit()
        print("[init_db] ✅ Database initialised successfully.")

    except Exception as exc:
        conn.rollback()
        print(f"[init_db] ❌ ERROR: {exc}", file=sys.stderr)
        raise
    finally:
        conn.close()


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
