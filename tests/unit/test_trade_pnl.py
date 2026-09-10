#!/usr/bin/env python3
"""Unit tests — bot/core/trade_pnl.py (autoritatives Trade-Ergebnis).

Bestandsaufnahme 2026-09-05: `trades.pnl_usd` enthaelt nur die LETZTE
Tranche, 898 von 910 Teilschliessungen tragen gar keinen Dollar-Betrag,
und `trade_events` enthaelt Duplikate aus Bulk-Batch-Laeufen. Dieses Modul
liefert die Summe ueber alle Tranchen und stellt sie der Kontoentwicklung
gegenueber.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bot.core.trade_pnl import (
    event_pnl_usd, realized_by_trade, realized_unattributed, reconcile,
)


class _DB:
    def __init__(self, events=(), equity=None, unreal=None):
        self._events = list(events)
        self._equity = equity
        self._unreal = unreal

    def fetchall(self, sql, params=()):
        # fix/orphan-events (2026-09-10): Der Mock ignorierte die
        # WHERE-Klausel und lieferte jeder Abfrage ALLE Events. Seit
        # reconcile() zusaetzlich realized_unattributed() ruft, haette das
        # jedes Event doppelt gezaehlt — der Mock muss die beiden Abfragen
        # auseinanderhalten koennen.
        nur_waisen = "trade_id IS NULL" in sql
        # dedupliziert wie die echte SQL es tut
        seen, out = set(), []
        for e in self._events:
            if nur_waisen != (e["trade_id"] is None):
                continue
            key = (e["trade_id"], e.get("event_at"), e.get("close_pct"))
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
        return out

    def fetchone(self, sql, params=()):
        if "CURRENT_EQUITY" in sql:
            return {"value": str(self._equity)} if self._equity is not None else None
        if "unrealized_pnl" in sql:
            return {"u": self._unreal}
        return None


def _ev(tid, amount, pct, at="t1", cp=25.0, et="PARTIAL_CLOSE", usd=None):
    return {"trade_id": tid, "event_at": at, "close_pct": cp,
            "event_type": et, "amount_usd": amount, "pnl_pct": pct,
            "pnl_usd": usd}


# ── event_pnl_usd ────────────────────────────────────────────────────────────

def test_waehrungssichere_herleitung():
    """amount_usd ist die Kostenbasis in USD, pnl_pct die Bewegung."""
    assert event_pnl_usd(68.82, 1.005) == pytest.approx(0.6917, abs=1e-3)


def test_gemeldeter_wert_gewinnt():
    """Ein vom Broker gelieferter Betrag schlaegt jede Herleitung."""
    assert event_pnl_usd(100.0, 50.0, stored_usd=3.21) == 3.21


def test_verlust_bleibt_negativ():
    assert event_pnl_usd(200.0, -2.5) == pytest.approx(-5.0)


@pytest.mark.parametrize("amt,pct", [(None, 1.0), (100.0, None), (None, None)])
def test_fehlende_felder_liefern_none(amt, pct):
    assert event_pnl_usd(amt, pct) is None


def test_muell_wirft_nicht():
    assert event_pnl_usd("x", "y") is None


# ── realized_by_trade ────────────────────────────────────────────────────────

def test_summiert_alle_tranchen():
    """Der Kern: nicht nur die letzte Tranche."""
    db = _DB([_ev(1, 100.0, 5.0, at="a"),
              _ev(1, 80.0, 3.0, at="b"),
              _ev(1, 60.0, -1.0, at="c", et="CLOSE", cp=100.0)])
    r = realized_by_trade(db)[1]
    assert r["realized_usd"] == pytest.approx(5.0 + 2.4 - 0.6)
    assert r["tranchen"] == 3


def test_dupletten_zaehlen_einmal():
    """trade_events hat Duplikate aus Bulk-Laeufen — sie duerfen nicht doppeln."""
    db = _DB([_ev(1, 100.0, 5.0, at="a"), _ev(1, 100.0, 5.0, at="a")])
    r = realized_by_trade(db)[1]
    assert r["tranchen"] == 1
    assert r["realized_usd"] == pytest.approx(5.0)


def test_trades_bleiben_getrennt():
    db = _DB([_ev(1, 100.0, 5.0, at="a"), _ev(2, 100.0, -5.0, at="a")])
    r = realized_by_trade(db)
    assert r[1]["realized_usd"] == pytest.approx(5.0)
    assert r[2]["realized_usd"] == pytest.approx(-5.0)


def test_defekte_db_faellt_open():
    class _Broken:
        def fetchall(self, *a, **k):
            raise RuntimeError("db weg")
    assert realized_by_trade(_Broken()) == {}


# ── reconcile ────────────────────────────────────────────────────────────────

def test_residuum_macht_die_luecke_sichtbar():
    """Genau der Zweck: was kein Trade-Datensatz erklaert.

    Start 10.000 + realisiert 5 + unrealisiert 0 = 10.005 erwartet,
    tatsaechlich 9.900 -> Residuum -105.
    """
    db = _DB([_ev(1, 100.0, 5.0, at="a")], equity=9900.0, unreal=0.0)
    r = reconcile(db)
    assert r["realized_usd"] == pytest.approx(5.0)
    assert r["residual_usd"] == pytest.approx(-105.0)


def test_sauberes_konto_hat_kein_residuum():
    db = _DB([_ev(1, 100.0, 5.0, at="a")], equity=10005.0, unreal=0.0)
    assert reconcile(db)["residual_usd"] == pytest.approx(0.0)


def test_unrealisiertes_zaehlt_mit():
    db = _DB([_ev(1, 100.0, 5.0, at="a")], equity=10105.0, unreal=100.0)
    assert reconcile(db)["residual_usd"] == pytest.approx(0.0)


def test_ohne_equity_kein_residuum():
    """Kein Kontostand -> keine erfundene Zahl."""
    db = _DB([_ev(1, 100.0, 5.0, at="a")], equity=None)
    assert reconcile(db)["residual_usd"] is None


# ── feat/fill-costs (2026-09-05) ─────────────────────────────────────────────

from bot.core.trade_pnl import estimate_fill_cost, recorded_costs


def test_halber_spread_je_fill():
    """Modell: gegen die Mitte zahlt man den halben Spread — je Fill."""
    assert estimate_fill_cost(200.0, 1.5) == pytest.approx(1.5)
    assert estimate_fill_cost(1000.0, 0.2) == pytest.approx(1.0)


def test_kosten_sind_nie_negativ():
    """Auch bei negativem Betrag ist Reibung ein Abfluss."""
    assert estimate_fill_cost(-200.0, 1.5) == pytest.approx(1.5)


def test_ohne_spread_bleibt_es_leer():
    """check_spread_gate liefert fail-open None — dann KEINE Null vortaeuschen."""
    assert estimate_fill_cost(200.0, None) is None
    assert estimate_fill_cost(None, 1.5) is None


def test_muell_wirft_nicht_bei_kosten():
    assert estimate_fill_cost("x", "y") is None


class _CostDB:
    def __init__(self, row):
        self._row = row

    def fetchone(self, sql, params=()):
        return self._row


def test_erfasste_kosten_werden_summiert():
    db = _CostDB({"c": 12.5, "m": 3, "n": 10})
    k = recorded_costs(db)
    assert k == {"cost_usd": 12.5, "fills_mit_kosten": 3, "fills_gesamt": 10}


def test_leere_kostenspalte_ist_kein_fehler():
    db = _CostDB({"c": 0, "m": 0, "n": 42})
    assert recorded_costs(db)["fills_mit_kosten"] == 0


def test_kosten_fallen_open():
    class _Broken:
        def fetchone(self, *a, **k):
            raise RuntimeError("db weg")
    assert recorded_costs(_Broken())["cost_usd"] == 0.0


# ── realized_unattributed (fix/orphan-events 2026-09-10) ─────────────────────

def test_waisen_werden_getrennt_ausgewiesen():
    """Events ohne trade_id: echtes Geld, aber keinem Trade zuordenbar."""
    db = _DB([_ev(1, 100.0, 5.0, at="a"), _ev(None, 200.0, -2.0, at="b")])
    assert realized_by_trade(db).keys() == {1}          # Waise NICHT dabei
    un = realized_unattributed(db)
    assert un["realized_usd"] == pytest.approx(-4.0)
    assert un["tranchen"] == 1


def test_waisen_zaehlen_in_der_kontorechnung_mit():
    """Start 10.000 + 5 (zugeordnet) - 4 (Waise) = 10.001 -> kein Residuum."""
    db = _DB([_ev(1, 100.0, 5.0, at="a"), _ev(None, 200.0, -2.0, at="b")],
             equity=10001.0, unreal=0.0)
    r = reconcile(db)
    assert r["realized_usd"] == pytest.approx(5.0)
    assert r["unattributed_usd"] == pytest.approx(-4.0)
    assert r["unattributed_tranchen"] == 1
    assert r["residual_usd"] == pytest.approx(0.0)


def test_ohne_waisen_bleibt_alles_wie_vorher():
    db = _DB([_ev(1, 100.0, 5.0, at="a")], equity=10005.0, unreal=0.0)
    r = reconcile(db)
    assert r["unattributed_usd"] == 0.0
    assert r["unattributed_tranchen"] == 0
    assert r["residual_usd"] == pytest.approx(0.0)


def test_waisen_faellt_open_bei_defekter_db():
    class _Broken:
        def fetchall(self, *a, **k):
            raise RuntimeError("db weg")
    assert realized_unattributed(_Broken()) == {"realized_usd": 0.0, "tranchen": 0}
