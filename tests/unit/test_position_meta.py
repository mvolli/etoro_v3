#!/usr/bin/env python3
"""Tests — bot/core/position_meta.py (fix/position-meta-dedup 2026-07-15)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from bot.core.position_meta import days_held_from, enrich_signal_and_age


# ── days_held_from ────────────────────────────────────────────────────────────

def test_days_held_naive_sqlite_format():
    opened = (datetime.now(timezone.utc) - timedelta(days=12)).strftime("%Y-%m-%d %H:%M:%S")
    assert days_held_from(opened) == 12


def test_days_held_iso_with_tz():
    opened = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    assert days_held_from(opened) == 3


def test_days_held_fail_safe():
    assert days_held_from(None) is None
    assert days_held_from("") is None
    assert days_held_from("kaputt-2026") is None


# ── enrich_signal_and_age ─────────────────────────────────────────────────────

def _mk_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, instrument_id INTEGER,"
                 " status TEXT, signal_id INTEGER, created_at TEXT)")
    conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT)")
    conn.execute("INSERT INTO signals VALUES (1, 'GOLDEN_CROSS')")
    conn.execute("INSERT INTO signals VALUES (2, 'RSI_EXTREME_OVERSOLD')")
    opened = (datetime.now(timezone.utc) - timedelta(days=11)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO trades VALUES (10, 100, 'ACTIVE', 1, ?)", (opened,))
    # CLOSED-Trade fuer Instrument 200 — darf mit Default-Statusfilter NICHT matchen
    conn.execute("INSERT INTO trades VALUES (11, 200, 'CLOSED', 2, ?)", (opened,))
    return conn


def test_enrich_active_trade(tmp_path):
    conn = _mk_db(tmp_path)
    positions = [{"instrument_id": 100}]
    enrich_signal_and_age(conn.cursor(), positions)
    assert positions[0]["signal_type"] == "GOLDEN_CROSS"
    assert positions[0]["days_held"] == 11


def test_enrich_no_matching_trade_uses_defaults(tmp_path):
    conn = _mk_db(tmp_path)
    positions = [{"instrument_id": 200}, {"instrument_id": 999}]
    enrich_signal_and_age(conn.cursor(), positions)
    for pos in positions:
        assert pos["signal_type"] == "UNBEKANNT"
        assert pos["opened_at"] is None
        assert pos["days_held"] is None


def test_enrich_status_filter_parameterizable(tmp_path):
    conn = _mk_db(tmp_path)
    positions = [{"instrument_id": 200}]
    enrich_signal_and_age(conn.cursor(), positions,
                          statuses=("ACTIVE", "CLOSED", "CONFIRMED"),
                          default_signal_type=None)
    assert positions[0]["signal_type"] == "RSI_EXTREME_OVERSOLD"
    assert positions[0]["days_held"] == 11
