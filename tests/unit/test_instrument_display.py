#!/usr/bin/env python3
"""Unit tests — resolve_instrument_display() (Data-Rich Discord Embeds).

Covers: ID lookup, symbol lookup (case-insensitive), name/region formatting,
unknown-ID fallback ("Instrument #<id>"), unknown-symbol pass-through,
and fail-open when the DB file is missing.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "bot"))
import discord_embeds as de


@pytest.fixture
def instruments_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trading.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE instruments (
            instrument_id INTEGER PRIMARY KEY,
            symbol TEXT, name TEXT, market_region TEXT, asset_class TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO instruments VALUES (?, ?, ?, ?, ?)",
        [
            (1014, "CVX.US", "Chevron", "US", "stocks"),
            (2358, "00027.HK", "China Telecom", "HK", "stocks"),
            (7, "NONAME", None, None, "stocks"),
            (8, "SAMENAME", "samename", "EU", "stocks"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(de, "_TRADING_DB_PATH", db_path)
    de._INSTRUMENT_LOOKUP_CACHE.clear()
    yield db_path
    de._INSTRUMENT_LOOKUP_CACHE.clear()


def test_resolve_by_id(instruments_db):
    assert de.resolve_instrument_display(1014) == "CVX.US — Chevron (US)"


def test_resolve_by_symbol_case_insensitive(instruments_db):
    assert de.resolve_instrument_display("cvx.us") == "CVX.US — Chevron (US)"


def test_no_name_no_region(instruments_db):
    assert de.resolve_instrument_display(7) == "NONAME"


def test_name_equal_to_symbol_not_duplicated(instruments_db):
    assert de.resolve_instrument_display(8) == "SAMENAME (EU)"


def test_unknown_id_gets_context(instruments_db):
    assert de.resolve_instrument_display(99999999) == "Instrument #99999999"


def test_unknown_symbol_passes_through(instruments_db):
    assert de.resolve_instrument_display("FOOBAR") == "FOOBAR"


def test_empty_ref(instruments_db):
    assert de.resolve_instrument_display("") == "?"
    assert de.resolve_instrument_display("?") == "?"


def test_fail_open_without_db(tmp_path, monkeypatch):
    monkeypatch.setattr(de, "_TRADING_DB_PATH", tmp_path / "missing.db")
    de._INSTRUMENT_LOOKUP_CACHE.clear()
    # DB fehlt → Eingabe kommt unverändert/mit Kontext zurück, kein Crash
    assert de.resolve_instrument_display("AAPL") == "AAPL"
    assert de.resolve_instrument_display(1234) == "Instrument #1234"
    de._INSTRUMENT_LOOKUP_CACHE.clear()
