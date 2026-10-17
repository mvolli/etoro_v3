#!/usr/bin/env python3
"""feat/outcome-refresh (2026-10-16): signal_outcomes taeglich fortschreiben."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from bot.core import outcome_refresh as orf

_SCHEMA = """
CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT, asset_class TEXT);
CREATE TABLE signals (id INTEGER PRIMARY KEY, instrument_id INTEGER, generated_at TEXT,
    signal_type TEXT, conviction TEXT, score REAL, rsi REAL, status TEXT);
CREATE TABLE ohlcv_daily (instrument_id INTEGER, date TEXT, close REAL,
    UNIQUE(instrument_id, date));
CREATE TABLE signal_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT NOT NULL, instrument_id INTEGER NOT NULL, symbol TEXT,
    signal_type TEXT NOT NULL, conviction TEXT, score REAL, rsi_signal REAL,
    status TEXT, fwd_1d REAL, fwd_3d REAL, fwd_5d REAL, fwd_10d REAL,
    cost REAL DEFAULT 0.011, direction TEXT, fwd_1d_pnl REAL, fwd_3d_pnl REAL,
    fwd_5d_pnl REAL, fwd_10d_pnl REAL, edge_shadow INTEGER,
    UNIQUE(instrument_id, signal_date, signal_type));
"""
TODAY = "2026-10-16"


def _days(n_back: int) -> list[str]:
    d0 = date.fromisoformat(TODAY)
    return [(d0 - timedelta(days=k)).isoformat() for k in range(n_back, -1, -1)]


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.executescript(_SCHEMA)
    c.execute("INSERT INTO instruments VALUES (1,'AAA','stock'), (2,'BBB','stock')")
    days = _days(20)
    # AAA steigt jeden Tag um 1 %, inkl. einer laufenden Kerze HEUTE mit Ausreisser.
    px = 100.0
    for d in days:
        c.execute("INSERT INTO ohlcv_daily VALUES (1, ?, ?)",
                  (d, 999.0 if d == TODAY else px))
        c.execute("INSERT INTO ohlcv_daily VALUES (2, ?, ?)", (d, 50.0))
        px *= 1.01
    sig_day = days[5]
    c.execute("INSERT INTO signals VALUES (1,1,?,'BUYTYPE','MEDIUM',1,30,'CONSUMED')",
              (sig_day + " 10:00:00",))
    c.execute("INSERT INTO signals VALUES (2,1,?,'BUYTYPE','MEDIUM',1,30,'REJECTED')",
              (sig_day + " 14:00:00",))                       # Dublette am selben Tag
    c.execute("INSERT INTO signals VALUES (3,2,?,'X,SELL','MEDIUM',1,70,'EXPIRED')",
              (sig_day + " 10:00:00",))
    c.execute("INSERT INTO signals VALUES (4,1,?,'BUYTYPE','MEDIUM',1,30,'FRESH')",
              (days[-3] + " 10:00:00",))                       # 2 Tage alt
    c.commit()
    return c


def test_forward_returns_netto_und_dedup(con):
    res = orf.compute(con, today=TODAY)
    rows = {(r["instrument_id"], r["signal_date"]): r for r in res["rows"]}
    assert res["signals"] == 3                                 # Dublette entfernt
    r = rows[(1, _days(20)[5])]
    assert r["fwd_5d"] == pytest.approx(1.01 ** 5 - 1 - 0.011)
    assert r["direction"] == "BUY" and r["fwd_5d_pnl"] == r["fwd_5d"]


def test_laufende_kerze_wird_nie_benutzt(con):
    """Signal 2 Tage alt: t+1 existiert, t+2 waere HEUTE (laufend) -> None."""
    res = orf.compute(con, today=TODAY)
    r = [x for x in res["rows"] if x["signal_date"] == _days(20)[-3]][0]
    assert r["fwd_1d"] == pytest.approx(0.01 - 0.011)
    assert r["fwd_3d"] is None and r["fwd_5d"] is None


def test_sell_pnl_wie_signal_hygiene(con):
    res = orf.compute(con, today=TODAY)
    r = [x for x in res["rows"] if x["instrument_id"] == 2][0]
    assert r["direction"] == "SELL"
    assert r["fwd_5d"] == pytest.approx(-0.011)
    assert r["fwd_5d_pnl"] == pytest.approx(0.011 - 2 * 0.011)


def test_upsert_fuellt_fehlende_horizonte_nach(con):
    orf.write(con, orf.compute(con, today=TODAY)["rows"])
    d = _days(20)[-3]
    assert con.execute("SELECT fwd_5d FROM signal_outcomes WHERE signal_date=?", (d,)).fetchone()[0] is None
    # Fuenf Tage spaeter: die Kerzen existieren -> derselbe Datensatz bekommt den Wert.
    later = (date.fromisoformat(TODAY) + timedelta(days=5)).isoformat()
    px = con.execute("SELECT close FROM ohlcv_daily WHERE instrument_id=1 AND date=?", (_days(20)[-2],)).fetchone()[0]
    con.execute("UPDATE ohlcv_daily SET close=? WHERE instrument_id=1 AND date=?", (px * 1.01, TODAY))
    for k in range(1, 6):
        con.execute("INSERT INTO ohlcv_daily VALUES (1, ?, ?)",
                    ((date.fromisoformat(TODAY) + timedelta(days=k)).isoformat(), px * 1.01 ** (k + 1)))
    orf.write(con, orf.compute(con, today=later)["rows"])
    rows = con.execute("SELECT fwd_5d, fwd_5d_pnl FROM signal_outcomes WHERE signal_date=?", (d,)).fetchall()
    assert len(rows) == 1 and rows[0][0] is not None and rows[0][1] == rows[0][0]


def test_zeilen_ausserhalb_des_fensters_bleiben_unberuehrt(con):
    con.execute("INSERT INTO signal_outcomes (signal_date, instrument_id, signal_type, fwd_5d, fwd_5d_pnl)"
                " VALUES ('2026-07-01', 1, 'BUYTYPE', -0.5, -0.5)")
    orf.write(con, orf.compute(con, today=TODAY)["rows"])
    assert con.execute("SELECT fwd_5d FROM signal_outcomes WHERE signal_date='2026-07-01'").fetchone()[0] == -0.5


def test_ohne_kurs_wird_gezaehlt_nicht_geschrieben(con):
    con.execute("INSERT INTO signals VALUES (9, 77, ?, 'NOBAR', 'MEDIUM', 1, 30, 'FRESH')",
                (_days(20)[5] + " 10:00:00",))
    res = orf.compute(con, today=TODAY)
    assert res["no_bar"] == {"NOBAR": 1}
    assert all(r["signal_type"] != "NOBAR" for r in res["rows"])


# ── feat/gate-market-costs (VoLLi 2026-10-17) ────────────────────────────────

def test_market_cost_je_markt():
    assert orf.market_cost("AAPL", "stock") == 0.011
    assert orf.market_cost("BHP.ASX", "stock") == 0.021
    assert orf.market_cost("0700.HK", "stock") == 0.021
    assert orf.market_cost("7203.T", "stock") == 0.021
    assert orf.market_cost("BTC", "crypto") == 0.021
    assert orf.market_cost("SPY", "etf") == 0.004      # US-ETF = CFD
    assert orf.market_cost("ISAC.L", "etf") == 0.002   # echter UCITS-ETF
    assert orf.market_cost("VOLV-B.ST", "stock") == 0.011


def test_compute_nutzt_marktkosten(con):
    con.execute("UPDATE instruments SET symbol='AAA.ASX' WHERE instrument_id=1")
    r = [x for x in orf.compute(con, today=TODAY)["rows"] if x["instrument_id"] == 1][0]
    assert r["cost"] == 0.021
    assert r["fwd_5d"] == pytest.approx(1.01 ** 5 - 1 - 0.021)


def test_recost_all_rechnet_alte_zeilen_um(con):
    con.execute("INSERT INTO instruments VALUES (3, 'CCC.HK', 'stock')")
    con.execute("INSERT INTO signal_outcomes (signal_date, instrument_id, signal_type, cost, direction,"
                " fwd_5d, fwd_5d_pnl) VALUES ('2026-07-01', 3, 'X', 0.011, 'BUY', 0.02, 0.02)")
    con.execute("INSERT INTO signal_outcomes (signal_date, instrument_id, signal_type, cost, direction,"
                " fwd_5d, fwd_5d_pnl) VALUES ('2026-07-01', 3, 'Y,SELL', 0.011, 'SELL', 0.02, -0.042)")
    assert orf.recost_all(con) == 2
    buy = con.execute("SELECT cost, fwd_5d, fwd_5d_pnl FROM signal_outcomes WHERE signal_type='X'").fetchone()
    assert buy[0] == 0.021 and buy[1] == pytest.approx(0.01) and buy[2] == pytest.approx(0.01)
    sell = con.execute("SELECT fwd_5d, fwd_5d_pnl FROM signal_outcomes WHERE signal_type='Y,SELL'").fetchone()
    assert sell[0] == pytest.approx(0.01) and sell[1] == pytest.approx(-0.01 - 0.042)
    assert orf.recost_all(con) == 0                    # idempotent
