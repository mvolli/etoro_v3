#!/usr/bin/env python3
"""fix/event-trade-link (2026-09-10).

Mehrere Close-Pfade (risk_sl, exposure_trim, concentration) iterieren ueber
LIVE-API-Positionen statt ueber Trades und hatten die trade_id nicht zur
Hand. Messung 2026-09-10: 212 Close-Events ohne trade_id, davon risk_sl 157
(alle), zusammen -225,56 USD realisiertes PnL.

`realized_by_trade()` gruppiert nach trade_id — diese Events waren fuer die
Kelly-Sizing-Grundlage und jede Residuum-Rechnung unsichtbar.

Echte SQLite-DB, weil genau die Aufloesung trades.api_position_id ->
trades.id geprueft wird.
"""
from __future__ import annotations

import sqlite3

import pytest

from bot.db.connection import DB
from bot.db.repo import TradeEventRepo

_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY, symbol TEXT, status TEXT,
    api_position_id TEXT, amount_usd REAL, pnl_pct REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, position_id TEXT,
    order_id TEXT, instrument_id INTEGER, symbol TEXT NOT NULL,
    event_type TEXT NOT NULL, source TEXT NOT NULL, event_at TEXT NOT NULL,
    close_pct REAL, units REAL, price REAL, amount_usd REAL, pnl_usd REAL,
    pnl_pct REAL, pnl_source TEXT, reason TEXT, discord_channel_id TEXT,
    discord_message_id TEXT, chart_posted INTEGER NOT NULL DEFAULT 0,
    reported_final INTEGER NOT NULL DEFAULT 0, pnl_filled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    spread_pct REAL, cost_usd REAL
);
"""


@pytest.fixture
def repo(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO trades (id, symbol, status, api_position_id) VALUES (?,?,?,?)",
        [(1, "AAA", "CLOSED", "555001"),
         (2, "BBB", "CLOSED", ""),      # Fehltrade Juni/Juli: leerer String
         (3, "CCC", "CLOSED", ""),      # zweiter davon
         (4, "DDD", "CLOSED", None)],
    )
    con.commit()
    con.close()
    return TradeEventRepo(DB(p))


def _record(repo, **kw):
    kw.setdefault("symbol", "AAA")
    kw.setdefault("event_type", "CLOSE")
    kw.setdefault("source", "risk_sl")
    return repo.record(**kw)


def test_trade_id_wird_aus_position_id_aufgeloest(repo):
    """Der Kernfall: risk_sl kennt nur die position_id."""
    eid = _record(repo, position_id="555001", pnl_usd=-12.5)
    row = repo.db.fetchone("SELECT trade_id FROM trade_events WHERE id=?", (eid,))
    assert row["trade_id"] == 1


def test_leere_position_id_loest_NICHT_auf(repo):
    """api_position_id='' trifft 2 Trades — ein Match waere falsch verknuepft."""
    eid = _record(repo, symbol="BBB", position_id="")
    row = repo.db.fetchone("SELECT trade_id FROM trade_events WHERE id=?", (eid,))
    assert row["trade_id"] is None


def test_nur_leerzeichen_loest_NICHT_auf(repo):
    eid = _record(repo, symbol="BBB", position_id="   ")
    assert repo.db.fetchone(
        "SELECT trade_id FROM trade_events WHERE id=?", (eid,))["trade_id"] is None


def test_unbekannte_position_id_bleibt_NULL(repo):
    eid = _record(repo, position_id="999999")
    assert repo.db.fetchone(
        "SELECT trade_id FROM trade_events WHERE id=?", (eid,))["trade_id"] is None


def test_explizite_trade_id_gewinnt(repo):
    """Aufrufer, die die trade_id kennen, werden nicht ueberschrieben."""
    eid = _record(repo, trade_id=42, position_id="555001")
    assert repo.db.fetchone(
        "SELECT trade_id FROM trade_events WHERE id=?", (eid,))["trade_id"] == 42


def test_ohne_position_id_kein_absturz(repo):
    eid = _record(repo, position_id=None)
    assert eid is not None
    assert repo.db.fetchone(
        "SELECT trade_id FROM trade_events WHERE id=?", (eid,))["trade_id"] is None


def test_realized_by_trade_sieht_das_event_jetzt(repo):
    """Der eigentliche Zweck: die Kelly-Grundlage darf es nicht verlieren."""
    from bot.core.trade_pnl import realized_by_trade
    _record(repo, position_id="555001", amount_usd=100.0, pnl_pct=-3.5,
            event_type="CLOSE")
    rea = realized_by_trade(repo.db)
    assert 1 in rea
    assert rea[1]["realized_usd"] == pytest.approx(-3.5)
