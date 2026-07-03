#!/usr/bin/env python3
"""Unit tests — fix/yfinance-fallback-resolution.

Covers candidate generation (HK zero-padding, .HKG swap, .US strip, alias
map, session cache) and the fetch_ohlcv fallback loop: delisted primary →
next candidate, transient-error retries, all-candidates-fail aggregation,
and persistence of successful resolutions.
"""
from __future__ import annotations

import sqlite3

import pytest

import bot.core.ohlcv_cache as oc


@pytest.fixture(autouse=True)
def _clean_session_cache():
    oc._RESOLVED_SYMBOL_CACHE.clear()
    yield
    oc._RESOLVED_SYMBOL_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(oc.time, "sleep", lambda s: None)


# ── generate_symbol_candidates ────────────────────────────────────────────────

def test_curated_hk_symbol_has_no_structural_guesses():
    # 00027.HK hat einen kuratierten Alias (0728.HK, China Telecom).
    # 0027.HK wäre auf Yahoo Galaxy Entertainment — darf NIE Kandidat sein!
    cands = oc.generate_symbol_candidates("00027.HK")
    assert cands == ["0728.HK", "00027.HK"]
    assert "0027.HK" not in cands


def test_uncurated_hk_symbol_gets_structural_variants():
    cands = oc.generate_symbol_candidates("00088.HK")
    assert cands[0] == "00088.HK"
    assert "0088.HK" in cands      # identitätserhaltend: gleiche Codenummer
    assert "00088.HKG" in cands


def test_us_suffix_stripped():
    cands = oc.generate_symbol_candidates("CVX.US")
    assert cands == ["CVX", "CVX.US"]   # kuratiert: Alias + raw, keine Extras


def test_hkg_suffix_maps_back_to_hk():
    cands = oc.generate_symbol_candidates("00027.HKG")
    assert "0027.HK" in cands


def test_plain_symbol_has_single_candidate():
    assert oc.generate_symbol_candidates("AAPL") == ["AAPL"]


def test_no_duplicates():
    for sym in ("00027.HK", "CVX.US", "AAPL", "0728.HK", "00088.HK"):
        cands = oc.generate_symbol_candidates(sym)
        assert len(cands) == len(set(cands))


def test_session_cache_wins():
    oc._RESOLVED_SYMBOL_CACHE["FOO.US"] = "FOO.SW"
    assert oc.generate_symbol_candidates("FOO.US")[0] == "FOO.SW"


# ── fix/asx-yfinance-suffix ────────────────────────────────────────────────────

def test_uncurated_asx_symbol_gets_suffix_swap():
    cands = oc.generate_symbol_candidates("XYZ.ASX")
    assert cands[0] == "XYZ.ASX"
    assert "XYZ.AX" in cands


def test_curated_asx_mismatch_has_no_structural_guess():
    # HMC.ASX's stored yfinance_symbol resolved to HighCom Ltd, a DIFFERENT
    # company than HMC Capital Ltd (HMC.AX) — must stay a pure curated hit.
    cands = oc.generate_symbol_candidates("HMC.ASX")
    assert cands == ["HMC.AX", "HMC.ASX"]

    cands = oc.generate_symbol_candidates("PNI.ASX")
    assert cands == ["PNI.AX", "PNI.ASX"]


def test_06881_hk_curated_to_6881():
    # Stored DB yfinance_symbol was 'GALA-USD' (an unrelated crypto token) —
    # 06881.HK must resolve to the correct HK stock, not that garbage.
    cands = oc.generate_symbol_candidates("06881.HK")
    assert cands == ["6881.HK", "06881.HK"]


# ── fetch_ohlcv fallback loop ─────────────────────────────────────────────────

class FakeDF:
    empty = False


def test_fallback_to_second_candidate(monkeypatch):
    calls = []

    def fake_once(sym, s, e):
        calls.append(sym)
        if sym == "0728.HK":
            return None, 'no_data'         # leere Antwort → next candidate
        return FakeDF(), 'ok'

    monkeypatch.setattr(oc, "_fetch_yf_once", fake_once)
    df, delisted, resolved = oc.fetch_ohlcv("00027.HK", "2026-01-01", "2026-07-01")
    assert df is not None
    assert not delisted
    assert resolved == "00027.HK"
    assert calls == ["0728.HK", "00027.HK"]


def test_resolution_cached_when_differs(monkeypatch):
    def fake_once(sym, s, e):
        if sym == "0088.HK":
            return FakeDF(), 'ok'
        return None, 'delisted'

    monkeypatch.setattr(oc, "_fetch_yf_once", fake_once)
    df, delisted, resolved = oc.fetch_ohlcv("00088.HK", "2026-01-01", "2026-07-01")
    assert resolved == "0088.HK"
    assert oc._RESOLVED_SYMBOL_CACHE["00088.HK"] == "0088.HK"


def test_transient_error_retries_same_candidate(monkeypatch):
    calls = []

    def fake_once(sym, s, e):
        calls.append(sym)
        if len(calls) < 3:
            return None, 'transient'       # transient → retry same ticker
        return FakeDF(), 'ok'

    monkeypatch.setattr(oc, "_fetch_yf_once", fake_once)
    df, delisted, resolved = oc.fetch_ohlcv("AAPL", "2026-01-01", "2026-07-01")
    assert df is not None
    assert calls == ["AAPL", "AAPL", "AAPL"]


def test_all_candidates_delisted(monkeypatch):
    monkeypatch.setattr(oc, "_fetch_yf_once", lambda *a: (None, 'delisted'))
    df, delisted, resolved = oc.fetch_ohlcv("00027.HK", "2026-01-01", "2026-07-01")
    assert df is None
    assert delisted is True
    assert resolved is None


def test_empty_responses_are_not_delisted(monkeypatch):
    # Leere Antworten dürfen NICHT sofort als delisted markieren —
    # die laufen über die 3-Strikes-Regel (update_yahoo_status).
    monkeypatch.setattr(oc, "_fetch_yf_once", lambda *a: (None, 'no_data'))
    df, delisted, resolved = oc.fetch_ohlcv("00027.HK", "2026-01-01", "2026-07-01")
    assert df is None
    assert delisted is False
    assert resolved is None


def test_transient_only_failure_is_not_delisted(monkeypatch):
    monkeypatch.setattr(oc, "_fetch_yf_once", lambda *a: (None, 'transient'))
    df, delisted, resolved = oc.fetch_ohlcv("AAPL", "2026-01-01", "2026-07-01")
    assert df is None
    assert delisted is False


def test_transient_exhaustion_aborts_remaining_candidates(monkeypatch):
    # Rate-Limit trifft alle Kandidaten — nach erschöpften Retries auf dem
    # ersten Kandidaten dürfen die weiteren nicht mehr angefragt werden.
    calls = []

    def fake_once(sym, s, e):
        calls.append(sym)
        return None, 'transient'

    monkeypatch.setattr(oc, "_fetch_yf_once", fake_once)
    oc.fetch_ohlcv("00088.HK", "2026-01-01", "2026-07-01")
    # Nur der erste Kandidat, MAX_FETCH_RETRIES+1 Versuche, dann Abbruch
    assert set(calls) == {"00088.HK"}
    assert len(calls) == oc.MAX_FETCH_RETRIES + 1


# ── _persist_resolution ───────────────────────────────────────────────────────

def _make_instruments_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE instruments (
            instrument_id INTEGER PRIMARY KEY,
            symbol TEXT, yfinance_symbol TEXT,
            last_updated TEXT
        )
    """)
    return conn


def test_persist_curated_resolution_updates_db():
    conn = _make_instruments_conn()
    conn.execute("INSERT INTO instruments VALUES (2358, '00027.HK', '00027.HK', NULL)")

    oc._persist_resolution(conn, 2358, "00027.HK", "0728.HK")   # kuratierter Alias

    row = conn.execute(
        "SELECT yfinance_symbol FROM instruments WHERE instrument_id = 2358"
    ).fetchone()
    assert row[0] == "0728.HK"

    audit = conn.execute(
        "SELECT original_symbol, resolved_symbol, instrument_id, curated FROM symbol_resolutions"
    ).fetchone()
    assert audit == ("00027.HK", "0728.HK", 2358, 1)
    assert oc._RESOLVED_SYMBOL_CACHE["00027.HK"] == "0728.HK"


def test_persist_structural_resolution_does_not_touch_instruments():
    # Geratene (strukturelle) Schreibweisen dürfen die DB-Wahrheit nicht
    # dauerhaft überschreiben — nur Audit-Log + Session-Cache.
    conn = _make_instruments_conn()
    conn.execute("INSERT INTO instruments VALUES (9001, '00088.HK', '00088.HK', NULL)")

    oc._persist_resolution(conn, 9001, "00088.HK", "0088.HK")

    row = conn.execute(
        "SELECT yfinance_symbol FROM instruments WHERE instrument_id = 9001"
    ).fetchone()
    assert row[0] == "00088.HK"   # unverändert

    audit = conn.execute(
        "SELECT resolved_symbol, curated FROM symbol_resolutions"
    ).fetchone()
    assert audit == ("0088.HK", 0)
    assert oc._RESOLVED_SYMBOL_CACHE["00088.HK"] == "0088.HK"


def test_persist_resolution_fail_open_without_conn():
    # conn=None darf nicht crashen — Session-Cache wird trotzdem gesetzt
    oc._persist_resolution(None, None, "A.US", "A")
    assert oc._RESOLVED_SYMBOL_CACHE["A.US"] == "A"
