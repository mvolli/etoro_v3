#!/usr/bin/env python3
"""Unit tests — fix/reconcile-fee-line (2026-10-16).

Der Bericht druckte "erwartete Equity" ohne die Kaufgebuehren, die
reconcile() im Residuum bereits abzieht — die gedruckten Zahlen gingen
nicht auf (Luecke = fee_usd). Ausserdem fehlte jede Zeile fuer die
Flat-Fee auf Schliessungen.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import pnl_reconcile as pr  # noqa: E402


class _DB:
    """Minimal-Wrapper mit fetchone/fetchall wie bot.db.connection.DB."""

    def __init__(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.execute("CREATE TABLE trade_events (trade_id INTEGER, event_type TEXT, "
                       "event_at TEXT, close_pct REAL, amount_usd REAL, symbol TEXT)")

    def fetchall(self, q, p=()):
        return self.c.execute(q, p).fetchall()

    def fetchone(self, q, p=()):
        return self.c.execute(q, p).fetchone()


def _ev(db, tid, et, at, pct, amt, sym):
    db.c.execute("INSERT INTO trade_events VALUES (?,?,?,?,?,?)", (tid, et, at, pct, amt, sym))


def test_schliessgebuehr_flat_je_order_mit_dedup():
    db = _DB()
    _ev(db, 1, "OPEN", "2026-10-01 10:00:00", None, 200, "KO")
    _ev(db, 1, "PARTIAL_CLOSE", "2026-10-02 10:00:00", 25, 50, "KO")
    _ev(db, 1, "PARTIAL_CLOSE", "2026-10-02 10:00:00", 25, 50, "KO")   # Doppelbuchung
    _ev(db, 1, "CLOSE", "2026-10-03 10:00:00", 100, 150, "KO")
    _ev(db, 2, "CLOSE", "2026-10-03 11:00:00", 100, 60, "BOQ.ASX")       # High-Fee $2
    out = pr._close_fee_estimate(db)
    assert out["orders"] == 3
    assert out["fee_usd"] == pytest.approx(1.0 + 1.0 + 2.0)


def test_schliessgebuehr_respektiert_epoche():
    db = _DB()
    _ev(db, 1, "CLOSE", "2026-09-01 10:00:00", 100, 100, "KO")
    _ev(db, 2, "CLOSE", "2026-10-01 10:00:00", 100, 100, "KO")
    assert pr._close_fee_estimate(db, since="2026-09-10")["orders"] == 1


def test_schliessgebuehr_fail_open():
    class Kaputt:
        def fetchall(self, *a, **k):
            raise RuntimeError("db weg")
    assert pr._close_fee_estimate(Kaputt()) == {"orders": 0, "fee_usd": 0.0}


def test_erwartete_equity_enthaelt_kaufgebuehren(monkeypatch, capsys):
    fake = {"start_equity": 10000.0, "realized_usd": -100.0, "unattributed_usd": 0.0,
            "unrealized_usd": 20.0, "fee_usd": 300.0, "fee_opens": 250,
            "trades": 10, "tranchen": 12, "equity": 9000.0, "residual_usd": None}
    erwartet = 10000.0 - 100.0 + 20.0 - 300.0
    fake["residual_usd"] = round(fake["equity"] - erwartet, 2)
    import bot.core.trade_pnl as tp
    monkeypatch.setattr(tp, "reconcile", lambda db, since=None: dict(fake))
    monkeypatch.setattr(tp, "recorded_costs", lambda db, since=None: {
        "fills_mit_kosten": 0, "fills_gesamt": 0, "cost_usd": 0.0})
    monkeypatch.setattr(pr, "_fill_count", lambda db, since=None: 0)
    monkeypatch.setattr(pr, "_close_fee_estimate", lambda db, since=None: {"orders": 0, "fee_usd": 0.0})
    pr._bericht(None)
    out = capsys.readouterr().out
    assert "9,620.00" in out          # erwartet inkl. Kaufgebuehren
    assert "Kaufgebuehren" in out
    # Pruefsumme: erwartet + Residuum == tatsaechliche Equity
    assert erwartet + fake["residual_usd"] == pytest.approx(fake["equity"])
