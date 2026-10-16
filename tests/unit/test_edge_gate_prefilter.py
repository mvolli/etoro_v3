#!/usr/bin/env python3
"""Edge-Gate-Vorfilter im signal_worker (2026-10-16).

Blockierte Signaltypen wurden bis dahin als APPROVED-Trade angelegt, vom
Veto-LLM geprueft, in Discord gemeldet und erst im execution_worker FAILED
gesetzt (61 Stueck am 16.10.). Der Vorfilter faellt dieselbe Entscheidung
schon vor der Trade-Anlage.
"""
from __future__ import annotations

import sqlite3

import pytest

from bot.core import edge_gate
from bot.db.connection import DB
from bot.workers import signal_worker as sw

_SCHEMA = """
CREATE TABLE signals (id INTEGER PRIMARY KEY, instrument_id INTEGER,
    signal_type TEXT, generated_at TEXT);
CREATE TABLE signal_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT, instrument_id INTEGER, signal_type TEXT,
    fwd_5d_pnl REAL, edge_shadow INTEGER);
"""


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    rows = []
    # BAD: n=40, klar negativ. GOOD: n=40, klar positiv.
    for i in range(40):
        d = f"2026-09-{(i % 20) + 1:02d}"   # 20 Handelstage (Cluster-SE)
        rows.append((d, 100 + i, "BAD", -0.02 + (i % 3) * 0.001, None))
        rows.append((d, 200 + i, "GOOD", 0.03 + (i % 3) * 0.001, None))
    con.executemany("INSERT INTO signal_outcomes (signal_date, instrument_id, signal_type, "
                    "fwd_5d_pnl, edge_shadow) VALUES (?,?,?,?,?)", rows)
    con.execute("INSERT INTO signals VALUES (1, 100, 'BAD', '2026-09-01 10:00:00')")
    con.commit()
    con.close()
    return DB(p)


def test_negativer_typ_wird_geblockt_und_markiert(db):
    cache: dict = {}
    assert sw._edge_gate_blocks(db, "BAD", 1, cache) is True
    row = db.fetchone("SELECT edge_shadow FROM signal_outcomes WHERE instrument_id=100")
    assert row["edge_shadow"] == 1


def test_positiver_typ_laeuft_durch(db):
    assert sw._edge_gate_blocks(db, "GOOD", None, {}) is False


def test_exempt_typ_laeuft_durch(db):
    # CORE_SWEEP ohne Daten waere shadow, ist aber ausgenommen.
    assert sw._edge_gate_blocks(db, "CORE_SWEEP", None, {}) is False


def test_ein_evaluate_pro_typ_und_lauf(db, monkeypatch):
    calls = []
    orig = edge_gate.evaluate

    def _spy(t, r, d):
        calls.append(t)
        return orig(t, r, d)

    monkeypatch.setattr(edge_gate, "evaluate", _spy)
    cache: dict = {}
    for _ in range(5):
        sw._edge_gate_blocks(db, "BAD", None, cache)
    assert calls == ["BAD"]


def test_gate_fehler_ist_fail_open(db, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(edge_gate, "evaluate", _boom)
    assert sw._edge_gate_blocks(db, "BAD", None, {}) is False


def test_live_mode_aus_blockt_nicht(db, monkeypatch):
    monkeypatch.setattr(edge_gate, "LIVE_MODE", False)
    assert sw._edge_gate_blocks(db, "BAD", None, {}) is False


def test_blocks_entspricht_execution_regel():
    assert edge_gate.blocks({"live_mode": True, "shadow": True, "exempt": False}) is True
    assert edge_gate.blocks({"live_mode": True, "shadow": True, "exempt": True}) is False
    assert edge_gate.blocks({"live_mode": True, "shadow": False, "exempt": False}) is False
    assert edge_gate.blocks({"live_mode": False, "shadow": True, "exempt": False}) is False
