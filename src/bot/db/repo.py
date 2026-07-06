"""
eToro Trading Bot V3 — Repository Layer
src/bot/db/repo.py

All SQL lives here.  Workers import Repo classes and call typed methods;
they never write SQL directly.

Repositories:
  TradeRepo      — CRUD + status-machine helpers for the `trades` table
  SignalRepo     — create / read fresh / expire for the `signals` table
  PortfolioRepo  — upsert / query for `portfolio_snapshot`
  StateRepo      — key-value wrapper around `system_state`
  LogRepo        — structured writes / reads for `system_log`
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .connection import DB


# ── helpers ───────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """Convert a sqlite3.Row to a plain dict (or None)."""
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _utcnow() -> str:
    """ISO-8601 UTC timestamp string, e.g. '2024-01-15 09:30:00'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── TradeRepo ─────────────────────────────────────────────────────────────────

# Fields that update_status() is allowed to patch alongside the status column.
_TRADE_UPDATE_FIELDS = frozenset(
    {
        "api_position_id",
        "order_id",
        "entry_price",
        "exit_price",
        "pnl_usd",
        "pnl_pct",
        "rejection_reason",
        "stop_loss_price",
        "approved_at",
        "submitted_at",
        "confirmed_at",
        "closed_at",
        "requeue_count",   # fix/failed-trade-requeue: one-shot retry marker
    }
)

class TradeRepo:
    """Repository for the `trades` table."""

    def __init__(self, db: DB) -> None:
        self.db = db
        self._ensure_requeue_column()

    def _ensure_requeue_column(self) -> None:
        """Idempotent migration: trades.requeue_count (fix/failed-trade-requeue)."""
        try:
            self.db.execute(
                "ALTER TABLE trades ADD COLUMN requeue_count INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass  # column already exists (or trades table absent in bare tests)

    # ── write ──────────────────────────────────────────────────────────────────

    def create(
        self,
        instrument_id: int,
        symbol: str,
        direction: str,
        amount_usd: float,
        stop_loss_pct: float,
        signal_id: int | None = None,
        signal_price: float | None = None,
    ) -> int:
        """
        Insert a new trade with status='PENDING_APPROVAL'.
        Returns the new trade id.

        `signal_price` stores the price from the signal at approval time
        (yfinance data) so execution doesn't need to fetch it again.
        """
        cur = self.db.execute(
            """
            INSERT INTO trades
                (instrument_id, symbol, direction, amount_usd, stop_loss_pct, signal_id, signal_price, status)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, 'PENDING_APPROVAL')
            """,
            (instrument_id, symbol, direction, amount_usd, stop_loss_pct, signal_id, signal_price),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def update_status(
        self,
        trade_id: int,
        new_status: str,
        **extra_fields: Any,
    ) -> None:
        """
        Update trade status plus any supplied extra columns.

        Allowed extra_fields:
            api_position_id, entry_price, exit_price, pnl_usd, pnl_pct,
            rejection_reason, stop_loss_price,
            approved_at, submitted_at, confirmed_at, closed_at

        Unknown field names raise ValueError before touching the DB.
        """
        unknown = set(extra_fields) - _TRADE_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"update_status: unknown fields {unknown}")

        # Always update the status column
        set_clauses = ["status = ?"]
        params: list[Any] = [new_status]

        for field, value in extra_fields.items():
            set_clauses.append(f"{field} = ?")
            params.append(value)

        params.append(trade_id)
        sql = f"UPDATE trades SET {', '.join(set_clauses)} WHERE id = ?"
        self.db.execute(sql, params)

    def lock_for_submission(self, trade_id: int) -> bool:
        """
        Atomically transition status from APPROVED → SUBMITTING.
        Returns True if exactly one row was updated (i.e. the lock was acquired).
        Any concurrent caller will find status != 'APPROVED' and get False.
        """
        cur = self.db.execute(
            """
            UPDATE trades
               SET status = 'SUBMITTING'
             WHERE id = ?
               AND status = 'APPROVED'
            """,
            (trade_id,),
        )
        return cur.rowcount == 1

    # ── read ───────────────────────────────────────────────────────────────────

    def get_by_status(self, status: str | list[str]) -> list[dict]:
        """Return trades matching one or more statuses."""
        if isinstance(status, str):
            status = [status]
        placeholders = ",".join("?" * len(status))
        rows = self.db.fetchall(
            f"SELECT * FROM trades WHERE status IN ({placeholders}) ORDER BY created_at",
            status,
        )
        return _rows_to_dicts(rows)

    # ── Ghost Order Blacklist ──────────────────────────────────────────────────

    GHOST_BLACKLIST_THRESHOLD = 3   # first blacklist after N consecutive ghost failures
    # b) Eskalierende Blacklist-Dauer:
    #    3-5 failures  → 6h cooldown
    #    6-8 failures  → 7 Tage cooldown
    #    9+ failures   → 7 Tage cooldown, ROLLIEREND (fix/ghost-blacklist-auto-expiry:
    #                    vorher permanent bis manueller DB-Reset — ein einmal
    #                    kaputtes Instrument blieb fuer immer gesperrt, auch wenn
    #                    eToro es laengst repariert hatte. Jetzt: nach 7 Tagen ein
    #                    Versuch; schlaegt der fehl, eskaliert der Zaehler sofort
    #                    wieder auf 7 Tage. Risiko: max. 1 Fehlversuch pro Woche.)

    PERMANENT_TIER_EXPIRY_HOURS = 7 * 24   # 9+ Fails: rollierende 7-Tage-Sperre

    def _blacklist_duration_hours(self, count: int) -> float | None:
        """Return blacklist duration in hours (None is no longer produced;
        kept in the signature for backward compatibility of callers/tests)."""
        if count >= 9:
            return self.PERMANENT_TIER_EXPIRY_HOURS
        if count >= 6:
            return 7 * 24       # 7 days = 168 hours
        if count >= 3:
            return 6            # 6 hours
        return 0                # not yet blacklisted

    def record_ghost_failure(self, instrument_id: int) -> tuple[int, str]:
        """
        Increment consecutive ghost failure counter. Blacklist after threshold
        with escalating duration (see _blacklist_duration_hours).

        Returns (new_count, blacklist_status) where blacklist_status is one of:
            'none', '6h', '7d', 'permanent'
        """
        from datetime import timedelta
        now = _utcnow()
        row = self.db.fetchone(
            "SELECT consecutive_failures FROM instrument_failures WHERE instrument_id = ?",
            (instrument_id,),
        )
        current = row["consecutive_failures"] if row else 0
        new_count = current + 1

        duration_hours = self._blacklist_duration_hours(new_count)
        blacklisted_until: str | None = None
        status_label = "none"
        if duration_hours is None:
            # Defensive: sollte seit fix/ghost-blacklist-auto-expiry nicht mehr
            # vorkommen — behandle wie rollierende 7-Tage-Sperre statt ewig.
            duration_hours = self.PERMANENT_TIER_EXPIRY_HOURS
        if duration_hours > 0:
            blacklisted_until = (
                datetime.now(timezone.utc) + timedelta(hours=duration_hours)
            ).strftime("%Y-%m-%d %H:%M:%S")
            if new_count >= 9:
                status_label = "7d-rolling"   # frueher: permanent
            else:
                status_label = "7d" if duration_hours >= 168 else "6h"

        self.db.execute(
            """
            INSERT INTO instrument_failures
                (instrument_id, consecutive_failures, last_failure_at, blacklisted_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(instrument_id) DO UPDATE SET
                consecutive_failures = excluded.consecutive_failures,
                last_failure_at      = excluded.last_failure_at,
                blacklisted_until    = excluded.blacklisted_until
            """,
            (instrument_id, new_count, now, blacklisted_until),
        )
        return new_count, status_label

    def reset_ghost_failures(self, instrument_id: int) -> None:
        """Clear failure counter after a successful trade confirmation."""
        self.db.execute(
            "DELETE FROM instrument_failures WHERE instrument_id = ?",
            (instrument_id,),
        )

    def is_instrument_blacklisted(self, instrument_id: int) -> bool:
        """Return True if the instrument is currently blacklisted for ghost orders."""
        row = self.db.fetchone(
            "SELECT blacklisted_until FROM instrument_failures WHERE instrument_id = ?",
            (instrument_id,),
        )
        if row and row["blacklisted_until"]:
            return row["blacklisted_until"] > _utcnow()
        return False

    def get_ghost_failure_count(self, instrument_id: int) -> int:
        """Return the current consecutive ghost failure count."""
        row = self.db.fetchone(
            "SELECT consecutive_failures FROM instrument_failures WHERE instrument_id = ?",
            (instrument_id,),
        )
        return row["consecutive_failures"] if row else 0


# ── SignalRepo ────────────────────────────────────────────────────────────────

_CONVICTION_ORDER = {"VERY_HIGH": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
_SIGNAL_STATUSES = frozenset({"FRESH", "CONSUMED", "REJECTED", "EXPIRED"})


class SignalRepo:
    """Repository for the `signals` table."""

    def __init__(self, db: DB) -> None:
        self.db = db

    def create(
        self,
        instrument_id: int,
        signal_type: str,
        conviction: str,
        score: float,
        rsi: float | None = None,
        macd_hist: float | None = None,
        bb_pct: float | None = None,
        price: float | None = None,
        ttl_minutes: int = 60,
    ) -> int:
        """Insert a new signal with status='FRESH' and expiry = now + ttl_minutes. Returns signal id."""
        # fix/sql-hardening: bind the TTL modifier as a parameter instead of
        # f-stringing it into the SQL text. int() coerces so a non-numeric
        # ttl can never inject SQLite date-modifier syntax, even if a future
        # caller sources ttl_minutes from signal/API data instead of config.
        ttl_modifier = f"+{int(ttl_minutes)} minutes"
        cur = self.db.execute(
            """
            INSERT INTO signals
                (instrument_id, signal_type, conviction, score,
                 rsi, macd_hist, bb_pct, price, expires_at, status)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?, 'utc'), 'FRESH')
            """,
            (instrument_id, signal_type, conviction, score, rsi, macd_hist, bb_pct, price, ttl_modifier),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def has_recent_signal(self, instrument_id: int, signal_type: str,
                          within_minutes: int) -> bool:
        """True, wenn für instrument_id bereits ein identisches Signal
        (gleicher signal_type) innerhalb der letzten within_minutes erzeugt
        wurde — egal ob FRESH oder CONSUMED (konsumiert = auf diese Episode
        wurde schon reagiert).

        fix/signal-dedup (KTA.DE 2026-07-06): data_worker läuft alle 5 min
        auf Tages-Bars — eine anhaltende Überhitzung erzeugte 39 identische
        Signale an einem Vormittag, die der SELL-Exit-Pfad einzeln konsumierte
        und die Position dabei in 50%-Schritten zerlegte.
        """
        row = self.db.fetchone(
            """
            SELECT 1 FROM signals
             WHERE instrument_id = ?
               AND signal_type = ?
               AND generated_at > datetime('now', ?, 'utc')
             LIMIT 1
            """,
            (instrument_id, signal_type, f"-{int(within_minutes)} minutes"),
        )
        return row is not None

    def update_signal_status(self, signal_id: int, new_status: str) -> None:
        """Update signal status to CONSUMED, REJECTED, or EXPIRED."""
        if new_status not in _SIGNAL_STATUSES:
            raise ValueError(f"Invalid signal status: {new_status}")
        self.db.execute(
            "UPDATE signals SET status = ? WHERE id = ?",
            (new_status, signal_id),
        )

    def get_fresh(
        self,
        instrument_id: int | None = None,
        min_conviction: str | None = None,
    ) -> list[dict]:
        """
        Return non-expired FRESH signals, optionally filtered by instrument and
        minimum conviction level (LOW < MEDIUM < HIGH < VERY_HIGH).

        EXCLUDES signals with status != 'FRESH' (CONSUMED/REJECTED/EXPIRED).
        This prevents the same signal from being re-processed every cycle.
        """
        clauses = ["expires_at > datetime('now','utc')", "status = 'FRESH'"]
        params: list[Any] = []

        if instrument_id is not None:
            clauses.append("instrument_id = ?")
            params.append(instrument_id)

        if min_conviction is not None:
            min_rank = _CONVICTION_ORDER.get(min_conviction, 0)
            allowed = [c for c, r in _CONVICTION_ORDER.items() if r >= min_rank]
            if allowed:
                placeholders = ",".join("?" * len(allowed))
                clauses.append(f"conviction IN ({placeholders})")
                params.extend(allowed)

        where = " AND ".join(clauses)
        rows = self.db.fetchall(
            f"SELECT * FROM signals WHERE {where} ORDER BY score DESC, generated_at DESC",
            params,
        )
        return _rows_to_dicts(rows)

    def expire_old(self) -> int:
        """Mark expired signals as EXPIRED (soft delete). Returns number of rows updated."""
        cur = self.db.execute(
            "UPDATE signals SET status = 'EXPIRED' WHERE expires_at < datetime('now','utc') AND status = 'FRESH'"
        )
        return cur.rowcount


# ── PortfolioRepo ─────────────────────────────────────────────────────────────

class PortfolioRepo:
    """Repository for the `portfolio_snapshot` table."""

    def __init__(self, db: DB) -> None:
        self.db = db

    def upsert(self, position: dict) -> None:
        """
        INSERT OR REPLACE a portfolio position.
        `position` must contain 'api_position_id'. All other recognised
        columns are optional and default to NULL / table defaults.
        """
        cols = [
            "api_position_id",
            "instrument_id",
            "symbol",
            "is_buy",
            "amount_usd",
            "open_price",
            "current_price",
            "unrealized_pnl",
            "unrealized_pnl_pct",
            "stop_loss_rate",
            "is_no_stop_loss",
            "last_synced",
        ]
        values = [position.get(c) for c in cols]
        # Ensure last_synced is always populated
        if values[11] is None:
            values[11] = _utcnow()

        placeholders = ",".join("?" * len(cols))
        col_list = ",".join(cols)
        self.db.execute(
            f"INSERT OR REPLACE INTO portfolio_snapshot ({col_list}) VALUES ({placeholders})",
            values,
        )

    def get_all(self) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM portfolio_snapshot ORDER BY last_synced DESC"
        )
        return _rows_to_dicts(rows)

    def get_by_instrument(self, instrument_id: int) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM portfolio_snapshot WHERE instrument_id = ?",
            (instrument_id,),
        )
        return _rows_to_dicts(rows)

    def get_total_exposure(self) -> float:
        """Sum of amount_usd across all tracked positions."""
        row = self.db.fetchone(
            "SELECT COALESCE(SUM(amount_usd), 0.0) AS total FROM portfolio_snapshot"
        )
        return float(row["total"]) if row else 0.0  # type: ignore[index]

    def get_position_count(self) -> int:
        row = self.db.fetchone("SELECT COUNT(*) AS cnt FROM portfolio_snapshot")
        return int(row["cnt"]) if row else 0  # type: ignore[index]


# ── StateRepo ─────────────────────────────────────────────────────────────────

class StateRepo:
    """Key-value wrapper around the `system_state` table."""

    def __init__(self, db: DB) -> None:
        self.db = db

    # ── generic ────────────────────────────────────────────────────────────────

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.fetchone(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        )
        return row["value"] if row else default  # type: ignore[index]

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, datetime('now','utc'))
            ON CONFLICT(key) DO UPDATE SET
                value      = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value),
        )

    def get_float(self, key: str, default: float = 0.0) -> float:
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default

    # ── typed convenience ──────────────────────────────────────────────────────

    def get_regime(self) -> str:
        return self.get("CURRENT_REGIME", "NORMAL") or "NORMAL"

    def set_regime(self, regime: str) -> None:
        self.set("CURRENT_REGIME", regime)

    def get_equity(self) -> float:
        return self.get_float("CURRENT_EQUITY", 0.0)

    def get_peak_equity(self) -> float:
        return self.get_float("PEAK_EQUITY", 10_000.0)

    def get_drawdown_pct(self) -> float:
        return self.get_float("DRAWDOWN_PCT", 0.0)


# ── LogRepo ───────────────────────────────────────────────────────────────────

class LogRepo:
    """Structured log writer / reader for the `system_log` table."""

    def __init__(self, db: DB) -> None:
        self.db = db

    def write(
        self,
        level: str,
        worker: str,
        message: str,
        details: Any = None,
    ) -> None:
        """
        Persist a log entry.  `details` can be any JSON-serialisable value;
        it will be stored as a JSON string.
        """
        details_str: str | None = None
        if details is not None:
            details_str = json.dumps(details, default=str)

        self.db.execute(
            """
            INSERT INTO system_log (level, worker, message, details)
            VALUES (?, ?, ?, ?)
            """,
            (level.upper(), worker, message, details_str),
        )

    def get_recent(
        self,
        limit: int = 50,
        level: str | None = None,
        worker: str | None = None,
    ) -> list[dict]:
        """
        Return the most recent log entries, newest first.
        Optionally filter by exact level and/or worker name.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if level is not None:
            clauses.append("level = ?")
            params.append(level.upper())
        if worker is not None:
            clauses.append("worker = ?")
            params.append(worker)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        rows = self.db.fetchall(
            f"SELECT * FROM system_log {where} ORDER BY ts DESC LIMIT ?",
            params,
        )
        return _rows_to_dicts(rows)
