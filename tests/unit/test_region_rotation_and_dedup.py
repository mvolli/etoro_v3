#!/usr/bin/env python3
"""Unit tests — fix/region-rotation-market-hours, fix/data-worker-dedup,
fix/golden-cross-event, fix/macd-floor-scale (2026-07-14)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import bot.core.market_hours as mh
from bot.core.signals import generate_signal
from bot.db.connection import DB
from bot.db.repo import SignalRepo
from bot.workers.discovery_worker import (
    REGION_ROTATIONS,
    _get_region_discovery_chunk,
    _region_scan_relevant,
)


# ── Region-Rotation ───────────────────────────────────────────────────────────

def test_region_rotations_cover_all_db_regions():
    covered: set[str] = set()
    for rot in REGION_ROTATIONS:
        covered |= set(rot["regions"])
    # Alle market_region-Werte der aktiven Instrumente muessen rotiert werden
    for region in ("EU", "US", "ASIA_AU", "ASIA_JP", "ASIA_CN", "GLOBAL"):
        assert region in covered, f"{region} fehlt in REGION_ROTATIONS"


def test_region_rotation_market_keys_are_defined():
    for rot in REGION_ROTATIONS:
        if rot["market_keys"] is None:
            continue
        for key in rot["market_keys"]:
            assert key in mh.MARKET_DEFINITIONS, f"{key} fehlt in MARKET_DEFINITIONS"


def test_region_scan_relevant_none_is_always_true():
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)  # Samstag
    assert _region_scan_relevant(None, now) is True


def test_region_scan_relevant_eu_weekend_closed():
    # Samstag 12:00 UTC — EU zu, auch kein Open innerhalb 3h
    saturday = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    assert _region_scan_relevant(("EU",), saturday) is False


def test_region_scan_relevant_eu_open_and_prelook():
    # Montag 10:00 UTC = 12:00 CEST — EU offen
    monday_open = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    assert _region_scan_relevant(("EU",), monday_open) is True
    # Montag 05:30 UTC = 07:30 CEST — EU zu, oeffnet aber 09:00 CEST (Prelook +1.5h)
    monday_pre = datetime(2026, 7, 13, 5, 30, tzinfo=timezone.utc)
    assert _region_scan_relevant(("EU",), monday_pre) is True


def test_generic_chunk_partitions_are_disjoint_and_complete(tmp_path):
    db = DB(db_path=tmp_path / "t.db")
    db.execute("""
        CREATE TABLE instruments (
            instrument_id INTEGER PRIMARY KEY,
            symbol TEXT, yfinance_symbol TEXT,
            market_region TEXT, asset_class TEXT,
            is_active INTEGER DEFAULT 1,
            is_tradable INTEGER DEFAULT 1
        )
    """)
    for iid in range(100, 140):
        db.execute(
            "INSERT INTO instruments (instrument_id, symbol, yfinance_symbol,"
            " market_region, asset_class) VALUES (?, ?, ?, 'ASIA_JP', 'stock')",
            (iid, f"SYM{iid}", f"{iid}.T"),
        )
    all_ids: set[int] = set()
    total = 0
    for idx in range(4):
        chunk = _get_region_discovery_chunk(db, ("ASIA_JP",), idx, 4)
        ids = {iid for _, iid, _ in chunk}
        assert not (all_ids & ids), "Chunks ueberlappen"
        all_ids |= ids
        total += len(chunk)
    assert total == 40 and len(all_ids) == 40


# ── has_fresh_signal vs. has_recent_signal ────────────────────────────────────

@pytest.fixture()
def sig_db(tmp_path):
    db = DB(db_path=tmp_path / "s.db")
    db.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id INTEGER NOT NULL,
            generated_at TEXT NOT NULL DEFAULT (datetime('now','utc')),
            signal_type TEXT NOT NULL,
            conviction TEXT NOT NULL,
            score REAL NOT NULL,
            rsi REAL, macd_hist REAL, bb_pct REAL, price REAL,
            expires_at TEXT NOT NULL,
            status TEXT DEFAULT 'FRESH'
        )
    """)
    return db


def test_has_fresh_signal_blocks_fresh_duplicate(sig_db):
    repo = SignalRepo(sig_db)
    repo.create(instrument_id=1, signal_type="GOLDEN_CROSS", conviction="HIGH",
                score=40.0, ttl_minutes=60)
    assert repo.has_fresh_signal(1, "GOLDEN_CROSS") is True
    # has_recent_signal (Trade-Cooldown) darf auf FRESH NICHT anspringen
    assert repo.has_recent_signal(1, "GOLDEN_CROSS", 60) is False
    # anderer Typ / anderes Instrument bleibt frei
    assert repo.has_fresh_signal(1, "TREND_PULLBACK") is False
    assert repo.has_fresh_signal(2, "GOLDEN_CROSS") is False


def test_has_fresh_signal_ignores_consumed_and_expired(sig_db):
    repo = SignalRepo(sig_db)
    repo.create(instrument_id=1, signal_type="GOLDEN_CROSS", conviction="HIGH",
                score=40.0, ttl_minutes=60)
    sig_db.execute("UPDATE signals SET status='CONSUMED' WHERE instrument_id=1")
    assert repo.has_fresh_signal(1, "GOLDEN_CROSS") is False
    assert repo.has_recent_signal(1, "GOLDEN_CROSS", 60) is True


# ── GOLDEN_CROSS als Ereignis ─────────────────────────────────────────────────

def _gc_indicators(**overrides):
    base = {
        "rsi": 50.0, "macd_hist": 0.5, "macd_hist_prev": 0.4,
        "bb_pct": 0.5, "price": 105.0,
        "sma20": 100.0, "sma50": 98.0,
        "vol_ratio": 1.0,
    }
    base.update(overrides)
    return base


def test_golden_cross_fires_on_recent_cross():
    ind = _gc_indicators(sma20_lookback=97.0, sma50_lookback=98.0)  # vor 5 Bars noch unter
    result = generate_signal("TEST", ind)
    assert "GOLDEN_CROSS" in result.signal_types


def test_golden_cross_does_not_fire_on_steady_state():
    ind = _gc_indicators(sma20_lookback=99.0, sma50_lookback=98.0)  # war schon drueber
    result = generate_signal("TEST", ind)
    assert "GOLDEN_CROSS" not in result.signal_types


def test_golden_cross_fail_closed_without_history():
    result = generate_signal("TEST", _gc_indicators())  # keine lookback-Werte
    assert "GOLDEN_CROSS" not in result.signal_types


# ── MACD-Floor preisnormiert (TREND_PULLBACK) ────────────────────────────────

def _tp_indicators(price, macd_hist):
    return {
        "rsi": 45.0, "macd_hist": macd_hist, "macd_hist_prev": macd_hist - 0.1,
        "bb_pct": 0.5, "price": price,
        "sma20": price * 0.995, "sma50": price * 0.97,
        "vol_ratio": 1.0,
    }


def test_trend_pullback_macd_floor_scales_with_price():
    # Hochpreisig: -4.0 bei 100k = -0.004% — muss durch (alter absoluter Floor blockte)
    high = generate_signal("BTCLIKE", _tp_indicators(100_000.0, -4.0))
    assert "TREND_PULLBACK" in high.signal_types
    # Niedrigpreisig: -0.004 bei $10 = -0.04% — muss blocken (alter Floor liess durch)
    low = generate_signal("PENNY", _tp_indicators(10.0, -0.004))
    assert "TREND_PULLBACK" not in low.signal_types
