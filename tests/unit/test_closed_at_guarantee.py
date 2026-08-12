"""Unit tests fuer fix/closed-at-guarantee (2026-08-12).

45 Trades standen auf status='CLOSED' mit closed_at IS NULL. Jede zeitbasierte
Auswertung verlor sie stillschweigend: llm_review_worker filtert explizit
"AND t.closed_at IS NOT NULL", config_experiment_worker vergleicht Fenster
ueber closed_at, get_pending_verification sortiert danach. Die Lernschleife
wurde also mit einem Loch gefuettert, ohne dass es auffiel.

Statt jeden Schreibpfad einzeln zu pruefen, garantiert es die zentrale Stelle.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from bot.db.connection import DB
from bot.db.repo import TradeRepo


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        db = DB(path)
        db.execute("""CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id INTEGER, symbol TEXT, direction TEXT,
            status TEXT NOT NULL, amount_usd REAL, stop_loss_pct REAL,
            stop_loss_price REAL, api_position_id TEXT, entry_price REAL,
            exit_price REAL, pnl_usd REAL, pnl_pct REAL, rejection_reason TEXT,
            signal_id INTEGER, created_at TEXT, approved_at TEXT,
            submitted_at TEXT, confirmed_at TEXT, closed_at TEXT,
            order_id TEXT, signal_price REAL, requeue_count INTEGER DEFAULT 0,
            verification_status TEXT DEFAULT 'VERIFIED',
            verify_attempts INTEGER DEFAULT 0)""")
        db.execute("INSERT INTO trades (id, symbol, status, amount_usd) "
                   "VALUES (1, 'TEST', 'ACTIVE', 100.0)")
        yield TradeRepo(db)


def _closed_at(repo) -> str | None:
    return repo.db.fetchone("SELECT closed_at FROM trades WHERE id=1")["closed_at"]


def test_closed_ohne_zeitstempel_bekommt_einen(repo):
    """Der Kern: CLOSED ohne closed_at darf nicht mehr entstehen."""
    repo.update_status(1, "CLOSED")
    assert _closed_at(repo) is not None


def test_expliziter_zeitstempel_gewinnt(repo):
    """Der Reconciler setzt closed_at bewusst — das darf nicht ueberschrieben werden."""
    repo.update_status(1, "CLOSED", closed_at="2026-07-29 19:37:42")
    assert _closed_at(repo) == "2026-07-29 19:37:42"


def test_andere_status_bekommen_keinen_zeitstempel(repo):
    repo.update_status(1, "FAILED")
    assert _closed_at(repo) is None
    repo.update_status(1, "APPROVED")
    assert _closed_at(repo) is None


def test_zeitstempel_ist_sortierbares_format(repo):
    """Alle Konsumenten vergleichen closed_at als TEXT — Format muss passen."""
    repo.update_status(1, "CLOSED")
    val = _closed_at(repo)
    assert len(val) == 19 and val[4] == "-" and val[10] == " "


def test_zusaetzliche_felder_bleiben_erhalten(repo):
    repo.update_status(1, "CLOSED", pnl_usd=12.5, pnl_pct=3.4)
    row = repo.db.fetchone("SELECT pnl_usd, pnl_pct, closed_at FROM trades WHERE id=1")
    assert row["pnl_usd"] == 12.5 and row["pnl_pct"] == 3.4
    assert row["closed_at"] is not None
