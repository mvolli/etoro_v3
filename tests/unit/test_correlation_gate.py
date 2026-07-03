#!/usr/bin/env python3
"""Unit tests — fix/correlation reduce-tier, batching, fail-open alert.

get_size_factor halves size in the 0.60–0.80 band; check_correlation_gate
blocks at >=0.80, flags complete fail-open; get_correlations_with serves
cached pairs without any download and batches missing pairs into one
yf.download call.
"""
from __future__ import annotations

import sys
import types

import pytest

import bot.core.correlation as corr_mod
from bot.core.correlation import (
    CORRELATION_REDUCE_FACTOR,
    check_correlation_gate,
    get_correlations_with,
    get_size_factor,
)


@pytest.fixture
def cache_db(tmp_path):
    return str(tmp_path / "trading.db")


def _seed_cache(db_path, pairs):
    conn = corr_mod._get_cache_conn(db_path)
    try:
        corr_mod._ensure_cache_table(conn)
        for a, b, r in pairs:
            corr_mod._set_cached(conn, a, b, r)
    finally:
        conn.close()


POSITIONS = [
    {"symbol": "MSFT", "amount_usd": 500.0},
    {"symbol": "GLD", "amount_usd": 400.0},
]


# ── Reduce-Tier ───────────────────────────────────────────────────────────────

def test_reduce_band_halves_size(cache_db):
    _seed_cache(cache_db, [("AAPL", "MSFT", 0.70), ("AAPL", "GLD", 0.10)])
    factor, reason = get_size_factor("AAPL", POSITIONS, db_path=cache_db)
    assert factor == CORRELATION_REDUCE_FACTOR
    assert "MSFT" in reason


def test_low_correlation_full_size(cache_db):
    _seed_cache(cache_db, [("AAPL", "MSFT", 0.30), ("AAPL", "GLD", 0.10)])
    factor, _ = get_size_factor("AAPL", POSITIONS, db_path=cache_db)
    assert factor == 1.0


def test_no_positions_full_size(cache_db):
    assert get_size_factor("AAPL", [], db_path=cache_db)[0] == 1.0


def test_no_data_full_size(cache_db, monkeypatch):
    # Kein Cache, Download schlägt fehl → fail-open volle Größe
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(
        download=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    ))
    factor, reason = get_size_factor("AAPL", POSITIONS, db_path=cache_db)
    assert factor == 1.0
    assert "keine Daten" in reason


# ── Block-Gate + Fail-Open-Alert ─────────────────────────────────────────────

def test_block_at_080(cache_db):
    _seed_cache(cache_db, [("AAPL", "MSFT", 0.85)])
    allowed, reason = check_correlation_gate("AAPL", POSITIONS, db_path=cache_db)
    assert not allowed
    assert "blockiert" in reason


def test_broad_etf_higher_tolerance(cache_db):
    _seed_cache(cache_db, [("MSFT", "SPY", 0.90)])
    allowed, _ = check_correlation_gate(
        "SPY", [{"symbol": "MSFT", "amount_usd": 500.0}], db_path=cache_db
    )
    assert allowed  # ETF-Schwelle 0.95


def test_complete_fail_open_is_flagged(cache_db, monkeypatch, caplog):
    import logging
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(
        download=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    ))
    with caplog.at_level(logging.WARNING):
        allowed, reason = check_correlation_gate("AAPL", POSITIONS, db_path=cache_db)
    assert allowed  # weiterhin fail-open …
    assert "FAIL-OPEN" in reason  # … aber sichtbar
    assert any("FAIL-OPEN" in r.message for r in caplog.records)


# ── Batching ──────────────────────────────────────────────────────────────────

def test_cached_pairs_need_no_download(cache_db, monkeypatch):
    _seed_cache(cache_db, [("AAPL", "MSFT", 0.5), ("AAPL", "GLD", 0.2)])

    def _boom(*a, **k):
        raise AssertionError("yf.download darf bei vollem Cache nicht aufgerufen werden")

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=_boom))
    corrs = get_correlations_with("AAPL", ["MSFT", "GLD"], db_path=cache_db)
    assert corrs == {"MSFT": 0.5, "GLD": 0.2}


def test_missing_pairs_use_single_batch_download(cache_db, monkeypatch):
    import numpy as np
    import pandas as pd

    calls = []

    def fake_download(symbols, **kwargs):
        calls.append(list(symbols))
        idx = pd.date_range("2026-06-01", periods=20, freq="D")
        rng = np.random.default_rng(7)
        base = rng.normal(0, 1, 20).cumsum() + 100
        cols = pd.MultiIndex.from_product([["Close"], symbols])
        df = pd.DataFrame(index=idx, columns=cols, dtype=float)
        for i, s in enumerate(symbols):
            # AAPL/MSFT stark korreliert, GLD unabhängig
            if s == "GLD":
                df[("Close", s)] = rng.normal(0, 1, 20).cumsum() + 50
            else:
                df[("Close", s)] = base + i * 0.01
        return df

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=fake_download))
    corrs = get_correlations_with("AAPL", ["MSFT", "GLD"], db_path=cache_db)

    assert len(calls) == 1                      # EIN Download für beide Paare
    assert set(calls[0]) == {"AAPL", "MSFT", "GLD"}
    assert corrs["MSFT"] == pytest.approx(1.0, abs=0.01)
    assert abs(corrs["GLD"]) < 0.9

    # zweiter Aufruf: alles gecacht, kein weiterer Download
    corrs2 = get_correlations_with("AAPL", ["MSFT", "GLD"], db_path=cache_db)
    assert len(calls) == 1
    assert corrs2["MSFT"] == pytest.approx(corrs["MSFT"])
