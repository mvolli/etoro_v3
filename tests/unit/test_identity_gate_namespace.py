#!/usr/bin/env python3
"""Regression — fix/identity-gate-namespace (2026-07-29, 2196.HK-Incident).

Das Pre-flight-Identity-Gate bekam `trades.symbol` als Erwartungswert.
Dieses Feld traegt streckenweise das YFINANCE-Symbol ('2196.HK'), waehrend
eToro '02196.HK' liefert → BLOCK auf einem voellig korrekten Instrument
(Trade #891, 2196.HK / instrument_id 2385).

Live-Abgleich 2026-07-29 zeigte: instruments.symbol == eToro-Symbol in
6/6 Faellen (gepaddete UND ungepaddete HK-Codes, .ASX). Der Fix loest
daher das kanonische Symbol aus instruments auf, statt den Vergleich im
Gate aufzuweichen — eine ECHTE Fehlzuordnung muss weiter blocken.
"""
from __future__ import annotations

import pytest

from bot.api.client import ClientConfig, EToroClient
from bot.db.connection import DB
from bot.workers.execution_worker import _canonical_symbol


@pytest.fixture
def db(tmp_path):
    d = DB(db_path=tmp_path / "t.db")
    d.execute("CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, "
              "symbol TEXT, yfinance_symbol TEXT)")
    d.execute("INSERT INTO instruments VALUES (2385, '02196.HK', '2196.HK')")
    d.execute("INSERT INTO instruments VALUES (3317, 'CAR.ASX', 'CAR.AX')")
    d.execute("INSERT INTO instruments VALUES (13696, '836.HK', '0836.HK')")
    return d


# ── _canonical_symbol ─────────────────────────────────────────────────────────

def test_resolves_etoro_symbol_not_yfinance(db):
    # Der Kern des Incidents: trades.symbol='2196.HK' -> '02196.HK'
    assert _canonical_symbol(db, 2385, "2196.HK") == "02196.HK"
    assert _canonical_symbol(db, 3317, "CAR.AX") == "CAR.ASX"
    # instruments haelt hier bewusst das UNgepaddete (eToro liefert es so)
    assert _canonical_symbol(db, 13696, "0836.HK") == "836.HK"


def test_fails_open_on_missing_row(db):
    assert _canonical_symbol(db, 999999, "FALLBACK") == "FALLBACK"


def test_fails_open_on_broken_db():
    class Boom:
        def fetchone(self, *a, **k):
            raise RuntimeError("db weg")

    assert _canonical_symbol(Boom(), 1, "FALLBACK") == "FALLBACK"


# ── Gate-Staerke bleibt erhalten ──────────────────────────────────────────────

class FakeClient(EToroClient):
    """EToroClient ohne Netzwerk — get_instrument_metadata gemockt."""

    def __init__(self, live_symbol):
        self._live = live_symbol

    def get_instrument_metadata(self, instrument_id: int) -> dict:
        return {"symbolFull": self._live} if self._live else {}


def test_canonical_symbol_passes_gate():
    ok, msg = FakeClient("02196.HK").verify_instrument_identity(2385, "02196.HK")
    assert ok is True


def test_yfinance_symbol_would_have_blocked():
    # Belegt die Ursache: mit dem alten Erwartungswert blockt das Gate
    ok, msg = FakeClient("02196.HK").verify_instrument_identity(2385, "2196.HK")
    assert ok is False
    assert "MISMATCH" in msg


def test_genuine_mismatch_still_blocks():
    # DOT-USD-Incident-Klasse: ID zeigt auf ein ANDERES Instrument
    ok, msg = FakeClient("DOTA.FUT").verify_instrument_identity(405, "DOT-USD")
    assert ok is False
    assert "MISMATCH" in msg


def test_metadata_outage_still_fails_open():
    ok, _ = FakeClient(None).verify_instrument_identity(2385, "02196.HK")
    assert ok is True


# ── Quelle: Core-Sweep-Whitelist speichert kanonisch ──────────────────────────

def test_whitelist_writer_stores_canonical_symbol(db, monkeypatch):
    import bot.workers.discovery_worker as dw

    db.execute("""CREATE TABLE core_sweep_whitelist (
        instrument_id INTEGER PRIMARY KEY, symbol TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'config', score REAL, conviction TEXT,
        added_at TEXT, expires_at TEXT, UNIQUE(instrument_id, source))""")
    monkeypatch.setattr(dw, "_ensure_core_sweep_whitelist_table", lambda _db: None)

    # Discovery uebergibt das yfinance-Symbol — gespeichert wird das eToro-Symbol
    dw._cs_auto_discovery(db, "2196.HK", 2385,
                          {"conviction": "HIGH", "score": 42.0, "rsi": 55.0})
    row = db.fetchone("SELECT symbol FROM core_sweep_whitelist WHERE instrument_id=2385")
    assert row["symbol"] == "02196.HK"


def test_whitelist_writer_fails_open_without_instruments_row(db, monkeypatch):
    import bot.workers.discovery_worker as dw

    db.execute("""CREATE TABLE core_sweep_whitelist (
        instrument_id INTEGER PRIMARY KEY, symbol TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'config', score REAL, conviction TEXT,
        added_at TEXT, expires_at TEXT, UNIQUE(instrument_id, source))""")
    monkeypatch.setattr(dw, "_ensure_core_sweep_whitelist_table", lambda _db: None)

    dw._cs_auto_discovery(db, "NEU.US", 424242,
                          {"conviction": "HIGH", "score": 42.0, "rsi": 55.0})
    row = db.fetchone("SELECT symbol FROM core_sweep_whitelist WHERE instrument_id=424242")
    assert row["symbol"] == "NEU.US"
