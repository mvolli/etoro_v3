#!/usr/bin/env python3
"""feat/ratsche-beide-masse (2026-09-12).

Die Ratsche gab eine Lockerung frei, wenn `n_closed >= 20 UND
SUM(trades.pnl_usd) > 0`. Diese Spalte haelt bei gestaffelten
Schliessungen nur die LETZTE Tranche und unterschaetzt systematisch.

Ein Wechsel auf `realized_by_trade()` allein waere die falsche Antwort:
das Mass summiert alle Tranchen, kennt aber die Reibung nicht. Gemessen
am 2026-09-12 auf der Live-DB (Fenster ab ZAESUR_DATE):

    CORE_SWEEP        pnl_usd -181.18   realisiert +288.35
    MACD_TURN+BB_LOW  pnl_usd  -81.30   realisiert   +3.73
    TREND+GOLDEN_CROSS pnl_usd -46.50   realisiert -148.09

Der Wechsel haette zwei von vier Typen von "eingefroren" auf "frei"
gekippt — darunter die Dip-Kerbe, die der dipbuy_regime-Gate vom
2026-09-11 gerade daempfen soll. Rechnet man die gemessene Reibung zu
(1,27 USD je Fill aus dem Epoch-Fenster, CORE_SWEEP hat 815 Fills), ist
KEIN Typ positiv.

Deshalb: beide Masse, und die Lockerung verlangt, dass keines
widerspricht. Strikt strenger als jede Einzelvariante, ohne
Reibungsmodell.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import bot.workers.llm_review_worker as lrw

SIG = "TESTSIGNAL"


@pytest.fixture
def env(tmp_path, monkeypatch):
    w = tmp_path / "w.json"
    w.write_text(json.dumps({"adjustments": {SIG: {"score_multiplier": 0.25}}}))
    monkeypatch.setattr(lrw, "SIGNAL_WEIGHTS_PATH", w)
    monkeypatch.setattr(lrw, "DECISION_LOG_PATH", tmp_path / "d.json")
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, signal_id INTEGER,"
        " status TEXT, pnl_usd REAL, created_at TEXT);"
        "CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT);")
    con.commit(); con.close()
    return {"db": db, "weights": w}


def _stats(n_closed, pnl_usd, realized):
    return {SIG: {"n_closed": n_closed, "sum_pnl_usd": pnl_usd,
                  "sum_realized_usd": realized}}


def _ratsche(monkeypatch, env, stats, proposed=1.0):
    monkeypatch.setattr(lrw, "_collect_realized_signal_pnl", lambda _p: stats)
    adj = {SIG: {"score_multiplier": proposed}}
    frozen = lrw._ratchet_signal_weights(adj, db_path=env["db"])
    return adj[SIG]["score_multiplier"], frozen


# ── der Kern ────────────────────────────────────────────────────────────────

def test_beide_positiv_gibt_frei(monkeypatch, env):
    mult, frozen = _ratsche(monkeypatch, env, _stats(25, 40.0, 60.0))
    assert mult == 1.0 and SIG not in frozen


def test_realisiert_positiv_aber_pnl_usd_negativ_bleibt_eingefroren(monkeypatch, env):
    """Genau der Fall CORE_SWEEP: +288 realisiert, -181 naiv."""
    mult, frozen = _ratsche(monkeypatch, env, _stats(113, -181.18, 288.35))
    assert mult == 0.25, "Lockerung auf nur EINEM zustimmenden Mass"
    assert SIG in frozen


def test_pnl_usd_positiv_aber_realisiert_negativ_bleibt_eingefroren(monkeypatch, env):
    """Die Gegenrichtung — der Grund, warum das alte Mass allein nicht reicht."""
    mult, frozen = _ratsche(monkeypatch, env, _stats(30, 12.0, -95.0))
    assert mult == 0.25 and SIG in frozen


def test_zu_wenig_trades_bleibt_eingefroren(monkeypatch, env):
    mult, frozen = _ratsche(monkeypatch, env, _stats(19, 40.0, 60.0))
    assert mult == 0.25 and SIG in frozen


def test_fehlende_zweitmessung_gibt_nicht_frei(monkeypatch, env):
    """Fail-safe: 0.0 ist nicht > 0. Ein Fehler darf nie lockern."""
    mult, frozen = _ratsche(monkeypatch, env, {SIG: {"n_closed": 50,
                                                     "sum_pnl_usd": 99.0}})
    assert mult == 0.25 and SIG in frozen


def test_daempfung_laeuft_weiterhin_ungehindert(monkeypatch, env):
    """Die Ratsche bremst nur Lockerungen — Verschaerfung bleibt frei."""
    mult, frozen = _ratsche(monkeypatch, env, _stats(5, -50.0, -50.0),
                            proposed=0.1)
    assert mult == 0.1 and SIG not in frozen


def test_zweitmessung_kommt_aus_realized_by_trade(tmp_path):
    """Ende-zu-Ende: die Sammelfunktion fuellt sum_realized_usd wirklich."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE trades (id INTEGER PRIMARY KEY, signal_id INTEGER,
            status TEXT, pnl_usd REAL, created_at TEXT);
        CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT);
        CREATE TABLE trade_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER, event_at TEXT, close_pct REAL, event_type TEXT,
            amount_usd REAL, pnl_pct REAL, pnl_usd REAL);
        INSERT INTO signals VALUES (1, 'X');
        INSERT INTO trades VALUES (1, 1, 'CLOSED', 5.0, '2026-08-01');
        -- zwei Tranchen: die naive Spalte kennt nur eine
        INSERT INTO trade_events (trade_id, event_at, close_pct, event_type,
            amount_usd, pnl_pct, pnl_usd)
            VALUES (1, '2026-08-02', 50, 'PARTIAL_CLOSE', 100.0, 10.0, 10.0);
        INSERT INTO trade_events (trade_id, event_at, close_pct, event_type,
            amount_usd, pnl_pct, pnl_usd)
            VALUES (1, '2026-08-03', 100, 'CLOSE', 100.0, 5.0, 5.0);
    """)
    con.commit(); con.close()
    out = lrw._collect_realized_signal_pnl(db)
    assert out["X"]["sum_pnl_usd"] == pytest.approx(5.0)
    assert out["X"]["sum_realized_usd"] == pytest.approx(15.0)
