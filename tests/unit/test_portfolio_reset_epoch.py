#!/usr/bin/env python3
"""feat/portfolio-reset (2026-09-10).

Der Reset wirft das PORTFOLIO weg, nicht das Wissen. Zwei Sichten muessen
sich deshalb sauber trennen lassen:

  Audit-Sicht  (since=None) — Basis aus dem Kapital-Ledger, realisiert ueber
                die ganze Kontohistorie. Das Residuum behaelt sein
                Gedaechtnis.
  Epoch-Sicht  (since=ISO)  — Basis EPOCH_START_EQUITY, realisiert nur ab
                dem Reset.

Die gefaehrliche Kombination ist epoch-gefilterte Realisierung gegen
kumulative Kapitalbasis: das Residuum bekaeme dann die gesamte
Vorgeschichte aufgebuerdet. Beide Fenster muessen zusammen umziehen.
"""
from __future__ import annotations

import sqlite3

import pytest

from bot.core.trade_pnl import (realized_by_trade, realized_unattributed,
                                reconcile)
from bot.db.connection import DB

_SCHEMA = """
CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE portfolio_snapshot (api_position_id TEXT PRIMARY KEY, unrealized_pnl REAL);
CREATE TABLE capital_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT, amount_usd REAL,
    note TEXT, created_at TEXT
);
CREATE TABLE trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, event_at TEXT,
    close_pct REAL, event_type TEXT, amount_usd REAL, pnl_pct REAL, pnl_usd REAL
);
"""

EPOCH = "2026-09-10T21:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO capital_events (occurred_at, amount_usd, note)"
                " VALUES ('2026-06-24 19:44:00', 10000.0, 'Start')")
    # vor der Epoche: -300 USD realisiert, davon -50 ohne Trade-Bezug
    con.execute("INSERT INTO trade_events (trade_id, event_at, close_pct,"
                " event_type, amount_usd, pnl_pct, pnl_usd)"
                " VALUES (1, '2026-08-01T10:00:00+00:00', 100, 'CLOSE',"
                " 500.0, -50.0, -250.0)")
    con.execute("INSERT INTO trade_events (trade_id, event_at, close_pct,"
                " event_type, amount_usd, pnl_pct, pnl_usd)"
                " VALUES (NULL, '2026-08-02T10:00:00+00:00', 100, 'CLOSE',"
                " 100.0, -50.0, -50.0)")
    # nach der Epoche: +120 USD realisiert, davon +20 ohne Trade-Bezug
    con.execute("INSERT INTO trade_events (trade_id, event_at, close_pct,"
                " event_type, amount_usd, pnl_pct, pnl_usd)"
                " VALUES (2, '2026-09-11T10:00:00+00:00', 100, 'CLOSE',"
                " 400.0, 25.0, 100.0)")
    con.execute("INSERT INTO trade_events (trade_id, event_at, close_pct,"
                " event_type, amount_usd, pnl_pct, pnl_usd)"
                " VALUES (NULL, '2026-09-11T11:00:00+00:00', 100, 'CLOSE',"
                " 80.0, 25.0, 20.0)")
    con.execute("INSERT INTO system_state VALUES ('CURRENT_EQUITY','10120.0')")
    con.execute("INSERT INTO system_state VALUES ('EPOCH_START_EQUITY','10000.0')")
    con.commit()
    con.close()
    return DB(p)


# ── die Filter selbst ────────────────────────────────────────────────────────

def test_realized_by_trade_ohne_since_ist_kumulativ(db):
    """Default-Verhalten unveraendert — das ist der Kelly-Pfad."""
    r = realized_by_trade(db)
    assert set(r) == {1, 2}
    assert sum(v["realized_usd"] for v in r.values()) == pytest.approx(-150.0)


def test_realized_by_trade_mit_since_nur_ab_epoche(db):
    r = realized_by_trade(db, since=EPOCH)
    assert set(r) == {2}
    assert r[2]["realized_usd"] == pytest.approx(100.0)


def test_unattributed_ohne_since_ist_kumulativ(db):
    u = realized_unattributed(db)
    assert u["tranchen"] == 2
    assert u["realized_usd"] == pytest.approx(-30.0)


def test_unattributed_mit_since_nur_ab_epoche(db):
    u = realized_unattributed(db, since=EPOCH)
    assert u["tranchen"] == 1
    assert u["realized_usd"] == pytest.approx(20.0)


# ── die beiden Sichten ───────────────────────────────────────────────────────

def test_audit_sicht_behaelt_das_gedaechtnis(db):
    """10.000 Basis + (-150) + (-30) = 9.820 erwartet gegen 10.120 Equity."""
    r = reconcile(db)
    assert r["since"] is None
    assert r["start_equity"] == pytest.approx(10_000.0)
    assert r["realized_usd"] == pytest.approx(-150.0)
    assert r["unattributed_usd"] == pytest.approx(-30.0)
    assert r["residual_usd"] == pytest.approx(300.0)


def test_epoch_sicht_startet_sauber(db):
    """10.000 Epoch-Basis + 100 + 20 = 10.120 == Equity -> Residuum 0."""
    r = reconcile(db, since=EPOCH)
    assert r["since"] == EPOCH
    assert r["start_equity"] == pytest.approx(10_000.0)
    assert r["realized_usd"] == pytest.approx(100.0)
    assert r["unattributed_usd"] == pytest.approx(20.0)
    assert r["residual_usd"] == pytest.approx(0.0)


def test_epoch_sicht_zieht_die_basis_mit(db):
    """Der eigentliche Fehler, gegen den dieser Test steht.

    Wuerde die Epoch-Sicht die KUMULATIVE Ledger-Basis nehmen, kaeme
    dasselbe Residuum heraus wie in der Audit-Sicht — die Vorgeschichte
    waere der neuen Epoche angelastet. Beide muessen sich unterscheiden.
    """
    audit = reconcile(db)
    epoch = reconcile(db, since=EPOCH)
    assert audit["residual_usd"] != epoch["residual_usd"]
    assert epoch["residual_usd"] == pytest.approx(0.0)


def test_epoch_sicht_ohne_marker_bricht_ab(db):
    """Lieber ein Fehler als eine still falsche Zahl."""
    db.execute("DELETE FROM system_state WHERE key='EPOCH_START_EQUITY'")
    with pytest.raises(ValueError, match="EPOCH_START_EQUITY"):
        reconcile(db, since=EPOCH)


def test_expliziter_start_equity_gewinnt_auch_mit_since(db):
    r = reconcile(db, start_equity=5_000.0, since=EPOCH)
    assert r["start_equity"] == pytest.approx(5_000.0)
