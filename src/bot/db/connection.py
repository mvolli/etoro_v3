"""
eToro Trading Bot V3 — Database Connection Layer
src/bot/db/connection.py

Provides:
  DB      — thin wrapper around sqlite3 with WAL configuration.
  DBPool  — simple named-connection wrapper (single-connection "pool").
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


# ── DB ────────────────────────────────────────────────────────────────────────

class DB:
    """
    Lightweight SQLite wrapper with WAL mode and row-factory enabled.

    Usage (context-manager — auto-commit + close):
        with DB(db_path) as db:
            db.execute("INSERT INTO ...", (...,))

    Usage (explicit):
        db = DB(db_path)
        rows = db.fetchall("SELECT * FROM trades WHERE status=?", ("ACTIVE",))
    """

    def __init__(
        self,
        db_path: str | Path,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.db_path = Path(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        # Holds the connection while inside a `with` block; None otherwise.
        self._conn: sqlite3.Connection | None = None

    # ── low-level ─────────────────────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        """
        Open and configure a new SQLite connection.

        Applied PRAGMAs:
          journal_mode = WAL      — concurrent readers while writer is active
          busy_timeout = N ms     — auto-retry on locked DB instead of raising
          foreign_keys = ON       — enforce FK constraints
          synchronous  = NORMAL   — good balance of safety vs speed with WAL
        """
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=self.busy_timeout_ms / 1000,  # sqlite3 uses seconds
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ── context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "DB":
        self._conn = self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        if self._conn is None:
            return False
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
            self._conn = None
        return False  # do not suppress exceptions

    # ── convenience helpers (short-lived connections) ─────────────────────────

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """
        Execute a single statement in a short-lived connection.
        Commits on success; rolls back and re-raises on error.
        Returns the cursor (useful for lastrowid / rowcount).
        """
        conn = self.connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fetchone(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        """Return the first row of a SELECT query, or None."""
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        """Return all rows of a SELECT query as a list."""
        conn = self.connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # ── internal helper used by repos for multi-statement transactions ─────────

    def _get_conn(self) -> sqlite3.Connection:
        """
        Return the active context-manager connection, or open a new one.
        Used internally by repos that need a single connection for a transaction.
        Callers are responsible for commit/rollback/close when not in a `with` block.
        """
        if self._conn is not None:
            return self._conn
        return self.connect()

    def close(self) -> None:
        """Close the active context-manager connection if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            finally:
                self._conn = None

    def __repr__(self) -> str:
        return f"DB({self.db_path})"


# ── DBPool ────────────────────────────────────────────────────────────────────

class DBPool:
    """
    Simple named-connection wrapper that acts as a 'pool' of one.

    For a single-process bot a true connection pool is unnecessary.
    DBPool stores a configured DB instance and provides get() for callers
    that prefer the pool pattern without pulling in a heavy library.

    Example::
        pool = DBPool(db_path="/data/trading.db", busy_timeout_ms=5000)
        db = pool.get()
        rows = db.fetchall("SELECT * FROM trades")
    """

    def __init__(
        self,
        db_path: str | Path,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._db = DB(db_path=db_path, busy_timeout_ms=busy_timeout_ms)

    def get(self) -> DB:
        """Return the underlying DB instance."""
        return self._db

    def __repr__(self) -> str:
        return f"DBPool({self._db.db_path})"
