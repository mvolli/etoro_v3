#!/usr/bin/env python3
"""fix/kelly-realized-pnl (2026-09-09).

`trades.pnl_pct` ist bei teilgeschlossenen Trades die SEIT-EINSTIEG-Prozent-
zahl der letzten Tranche — nicht das kapitalgewichtete Ergebnis. Ein Trade,
der bei +25 % eine Tranche mitnimmt und den Rest abrutschen laesst, stand
mit +25 % in der Kelly-Stichprobe und hob den Faktor seines Clusters.

Diese Tests laufen gegen eine echte SQLite-DB, weil genau der JOIN
trades -> trade_events geprueft wird, den ein Mock wegabstrahiert.
"""
from __future__ import annotations

from bot.core.sizing import _recent_trade_rows
from bot.db.connection import DB

_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY, signal_id INTEGER, instrument_id INTEGER,
    status TEXT, pnl_pct REAL, amount_usd REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT);
CREATE TABLE trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, event_type TEXT,
    event_at TEXT, close_pct REAL, amount_usd REAL, pnl_pct REAL, pnl_usd REAL
);
CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, asset_class TEXT);
"""


def _mkdb(tmp_path, events):
    p = tmp_path / "t.db"
    con = __import__("sqlite3").connect(p)
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'DIP_COMBO')")
    con.execute(
        "INSERT INTO trades (id, signal_id, instrument_id, status, pnl_pct,"
        " amount_usd, created_at) VALUES (7, 1, 42, 'CLOSED', 25.0, 100.0,"
        " datetime('now','-3 days'))"
    )
    for ev in events:
        con.execute(
            "INSERT INTO trade_events (trade_id, event_type, event_at,"
            " close_pct, amount_usd, pnl_pct, pnl_usd)"
            " VALUES (?,?,?,?,?,?,?)", ev)
    con.commit()
    con.close()
    return DB(p)


def test_teilverkauf_wird_kapitalgewichtet_statt_seit_einstieg(tmp_path):
    """Halbe Position bei +25 %, Rest bei -5 % -> +10 %, nicht +25 %."""
    db = _mkdb(tmp_path, [
        (7, "PARTIAL_CLOSE", "2026-09-07T10:00:00", 50.0, 50.0, 25.0, None),
        (7, "CLOSE",         "2026-09-08T10:00:00", 100.0, 50.0, -5.0, None),
    ])
    rows = _recent_trade_rows(db)
    assert len(rows) == 1
    st, pct = rows[0]
    assert st == "DIP_COMBO"
    # (50*0.25 + 50*-0.05) / 100 = +10 %
    assert pct == __import__("pytest").approx(10.0)
    # und ausdruecklich NICHT der trades.pnl_pct-Wert
    assert pct != 25.0


def test_trade_ohne_event_ledger_faellt_auf_trades_pnl_pct_zurueck(tmp_path):
    """Trades vor feat/pnl-nachreport (2026-07-28) haben keine Events."""
    db = _mkdb(tmp_path, [])
    rows = _recent_trade_rows(db)
    assert rows == [("DIP_COMBO", 25.0)]


def test_stored_pnl_usd_gewinnt_gegen_prozentrechnung(tmp_path):
    """event_pnl_usd: ein hinterlegter USD-Wert schlaegt amount*pct."""
    db = _mkdb(tmp_path, [
        (7, "CLOSE", "2026-09-08T10:00:00", 100.0, 100.0, 25.0, 3.0),
    ])
    _, pct = _recent_trade_rows(db)[0]
    assert pct == __import__("pytest").approx(3.0)   # 3 USD auf 100 USD Basis


def test_nullbasis_faellt_zurueck_statt_zu_dividieren(tmp_path):
    """basis_usd = 0 darf keine ZeroDivision und keinen 0-%-Wert erzeugen."""
    db = _mkdb(tmp_path, [
        (7, "CLOSE", "2026-09-08T10:00:00", 100.0, 0.0, 25.0, 1.0),
    ])
    _, pct = _recent_trade_rows(db)[0]
    assert pct == 25.0    # Fallback auf trades.pnl_pct
