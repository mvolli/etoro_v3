#!/usr/bin/env python3
"""fix/closed-at-nicht-ueberschreiben (2026-09-11).

Die Garantie aus fix/closed-at-guarantee (2026-08-12) setzt closed_at auf
"jetzt", wenn CLOSED ohne Zeitstempel geschrieben wird. Sie unterschied
aber nicht zwischen "wird gerade geschlossen" und "ist laengst geschlossen
und bekommt nur ein Feld nachgetragen".

Der Reconciler ruft in seiner Verifikations-Schleife alle 5 Minuten

    trade_repo.update_status(t_id, "CLOSED", verify_attempts=attempts)

Gemessen am 2026-09-10: alle 30 PENDING-Trades standen auf 22:16:54 — dem
letzten Reconciler-Lauf — waehrend ihre CLOSE-Events 21:31:30 bis 21:32:08
zeigten. Die echte Schlusszeit war weg.

Schlimmer noch: der Reconciler rechnet age_days aus genau diesem Feld und
gibt nach VERIFY_EXPIRY_DAYS=7 auf. Weil dieselbe Schleife den Wert vorher
auffrischte, blieb age_days bei ~0 — der Ablauf konnte nie greifen, und
die 30 Trades haetten sich mit 8.640 Log-Zeilen am Tag endlos wiederholt.
"""
from __future__ import annotations

import sqlite3

import pytest

from bot.db.connection import DB
from bot.db.repo import TradeRepo

_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, status TEXT,
    amount_usd REAL, closed_at TEXT, verify_attempts INTEGER DEFAULT 0,
    verification_status TEXT, pnl_usd REAL, pnl_pct REAL,
    entry_price REAL, exit_price REAL, api_position_id TEXT,
    created_at TEXT, approved_at TEXT, submitted_at TEXT, confirmed_at TEXT
);
"""


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    return DB(p)


def test_erstes_schliessen_setzt_den_zeitstempel(db):
    """Die Garantie bleibt: CLOSED ohne Zeitstempel ist ein kaputter Zustand."""
    db.execute("INSERT INTO trades (id, symbol, status) VALUES (1, 'SPY', 'ACTIVE')")
    TradeRepo(db).update_status(1, "CLOSED")
    row = db.fetchone("SELECT closed_at FROM trades WHERE id = 1")
    assert row["closed_at"]


def test_zweiter_aufruf_laesst_den_zeitstempel_stehen(db):
    """Der Kern: der Reconciler-Retry darf die Schlusszeit nicht verschieben."""
    db.execute("INSERT INTO trades (id, symbol, status, closed_at)"
               " VALUES (1, 'SPY', 'CLOSED', '2026-09-10 21:31:30')")
    repo = TradeRepo(db)
    for versuch in (1, 2, 3):
        repo.update_status(1, "CLOSED", verify_attempts=versuch)
    row = db.fetchone("SELECT closed_at, verify_attempts FROM trades WHERE id = 1")
    assert row["closed_at"] == "2026-09-10 21:31:30"
    assert row["verify_attempts"] == 3


def test_expliziter_wert_gewinnt_weiterhin(db):
    db.execute("INSERT INTO trades (id, symbol, status, closed_at)"
               " VALUES (1, 'SPY', 'CLOSED', '2026-09-10 21:31:30')")
    TradeRepo(db).update_status(1, "CLOSED", closed_at="2026-09-01 10:00:00")
    row = db.fetchone("SELECT closed_at FROM trades WHERE id = 1")
    assert row["closed_at"] == "2026-09-01 10:00:00"


def test_leerer_zeitstempel_wird_nachgetragen(db):
    """closed_at = '' oder NULL zaehlt als 'fehlt', nicht als 'gesetzt'."""
    db.execute("INSERT INTO trades (id, symbol, status, closed_at)"
               " VALUES (1, 'SPY', 'CLOSED', '')")
    TradeRepo(db).update_status(1, "CLOSED", verify_attempts=1)
    row = db.fetchone("SELECT closed_at FROM trades WHERE id = 1")
    assert row["closed_at"]


def test_ablauf_kann_jetzt_greifen(db):
    """Die eigentliche Wirkung: age_days waechst, statt bei 0 zu kleben."""
    from datetime import datetime, timezone
    alt = "2026-09-01 10:00:00"
    db.execute("INSERT INTO trades (id, symbol, status, closed_at,"
               " verification_status) VALUES (1, 'SPY', 'CLOSED', ?, 'PENDING')",
               (alt,))
    repo = TradeRepo(db)
    for versuch in range(1, 6):
        repo.update_status(1, "CLOSED", verify_attempts=versuch)
    row = db.fetchone("SELECT closed_at FROM trades WHERE id = 1")
    alter_tage = (datetime.now(timezone.utc)
                  - datetime.fromisoformat(row["closed_at"]).replace(
                      tzinfo=timezone.utc)).total_seconds() / 86400
    assert alter_tage > 7, "VERIFY_EXPIRY_DAYS bleibt unerreichbar"
