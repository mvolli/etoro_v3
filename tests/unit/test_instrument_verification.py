#!/usr/bin/env python3
"""Unit tests — fix/discovery-identity-verification.

Covers the pure verification module plus the discovery resolver's
fail-closed behaviour (no more hash placeholders).

Includes the VALT.L regression case from 2026-07-02: signal price $113.04
(yfinance, correct) vs. eToro live price $5,200 (wrong instrument behind
the resolved ID) must be rejected at discovery time.
"""
from __future__ import annotations

import pytest

from bot.core.instrument_verification import (
    MAX_PRICE_DEVIATION_PCT_DEFAULT,
    check_identity,
    check_price_consistency,
    extract_live_symbol,
    normalize_symbol,
    verify_candidate,
)
from bot.workers.discovery_worker import _symbol_to_instrument_id


# ─── normalize / extract ──────────────────────────────────────────────────────

class TestNormalize:
    def test_basic_upper_strip(self):
        assert normalize_symbol(" aapl ") == "AAPL"

    def test_usd_suffixes_stripped(self):
        assert normalize_symbol("BTC-USD") == "BTC"
        assert normalize_symbol("eth/usd") == "ETH"
        assert normalize_symbol("XRPUSD") == "XRP"

    def test_exchange_suffix_kept(self):
        # .L is a different LISTING, not a quote currency — must be kept.
        assert normalize_symbol("VALT.L") == "VALT.L"
        assert normalize_symbol("valt.l") != "VALT"

    def test_empty(self):
        assert normalize_symbol("") == ""
        assert normalize_symbol(None) == ""  # type: ignore[arg-type]


class TestExtractLiveSymbol:
    def test_priority_symbolfull_first(self):
        meta = {"symbolFull": "VALT.L", "displayName": "Vanguard FTSE UK"}
        assert extract_live_symbol(meta) == "VALT.L"

    def test_fallback_chain(self):
        assert extract_live_symbol({"ticker": "AAPL"}) == "AAPL"
        assert extract_live_symbol({"displayName": "Apple"}) == "Apple"

    def test_missing(self):
        assert extract_live_symbol(None) == ""
        assert extract_live_symbol({}) == ""
        assert extract_live_symbol({"foo": "bar"}) == ""


# ─── identity check ───────────────────────────────────────────────────────────

class TestIdentity:
    def test_match(self):
        ok, _ = check_identity("VALT.L", {"symbolFull": "VALT.L"})
        assert ok

    def test_crypto_suffix_match(self):
        ok, _ = check_identity("BTC-USD", {"symbolFull": "BTC"})
        assert ok

    def test_hard_mismatch(self):
        # The VALT.L incident shape: local symbol vs. Valterra Platinum ID
        ok, reason = check_identity("VALT.L", {"symbolFull": "VPL"})
        assert not ok
        assert "MISMATCH" in reason

    def test_fail_open_on_missing_meta(self):
        ok, reason = check_identity("VALT.L", None)
        assert ok
        assert "fail-open" in reason

    def test_fail_open_on_meta_without_symbol(self):
        ok, _ = check_identity("VALT.L", {"instrumentId": 1456})
        assert ok


# ─── price consistency ────────────────────────────────────────────────────────

class TestPriceConsistency:
    def test_valt_l_regression_blocked(self):
        # 2026-07-02 incident: yfinance $113.04 vs. eToro $5,200.
        # No scale factor (1, 100, 0.01) brings these within 25%.
        ok, dev, _ = check_price_consistency(113.04, 5200.0)
        assert not ok
        assert dev > MAX_PRICE_DEVIATION_PCT_DEFAULT

    def test_same_unit_passes(self):
        ok, dev, scale = check_price_consistency(100.0, 102.0)
        assert ok
        assert scale == 1.0
        assert dev == pytest.approx(2.0)

    def test_gbp_vs_pence_passes_at_x100(self):
        # yfinance 113.04 GBP vs. eToro 1.1304 (quoted in GBP while yf gives
        # pence would be the inverse) — scale 100 rescues it.
        ok, dev, scale = check_price_consistency(11304.0, 113.04)
        assert ok
        assert scale == 100.0
        assert dev == pytest.approx(0.0, abs=1e-6)

    def test_pence_vs_gbp_passes_at_x001(self):
        ok, _, scale = check_price_consistency(113.04, 11304.0)
        assert ok
        assert scale == 0.01

    def test_boundary_exactly_at_limit_passes(self):
        ok, dev, _ = check_price_consistency(100.0, 125.0, max_deviation_pct=25.0)
        assert ok
        assert dev == pytest.approx(25.0)

    def test_just_over_limit_fails(self):
        ok, _, _ = check_price_consistency(100.0, 125.1, max_deviation_pct=25.0)
        assert not ok

    def test_missing_prices_pass(self):
        assert check_price_consistency(None, 100.0)[0]
        assert check_price_consistency(100.0, None)[0]
        assert check_price_consistency(0.0, 100.0)[0]
        assert check_price_consistency(100.0, 0.0)[0]

    def test_collapse_direction_also_fails(self):
        ok, _, _ = check_price_consistency(5200.0, 113.0)
        assert not ok


# ─── combined verify_candidate ────────────────────────────────────────────────

class TestVerifyCandidate:
    def test_full_pass(self):
        ok, reason = verify_candidate(
            "VALT.L", {"symbolFull": "VALT.L"}, 113.04, 112.50
        )
        assert ok
        assert "Preis OK" in reason

    def test_identity_mismatch_wins_even_with_matching_price(self):
        ok, reason = verify_candidate(
            "VALT.L", {"symbolFull": "VPL"}, 113.04, 113.04
        )
        assert not ok
        assert "MISMATCH" in reason

    def test_price_net_catches_failopen_identity(self):
        # Metadata unavailable (identity fail-open) + wrong-instrument price
        # → the price check is the net that still blocks it.
        ok, reason = verify_candidate("VALT.L", None, 113.04, 5200.0)
        assert not ok
        assert "Preis-Mismatch" in reason

    def test_failopen_identity_and_no_price_passes(self):
        # Nothing to compare at all → pass (documented behaviour; the
        # execution-time slippage gate remains the last line of defence).
        ok, _ = verify_candidate("VALT.L", None, 113.04, None)
        assert ok

    def test_crypto_pass(self):
        ok, _ = verify_candidate("BTC-USD", {"symbolFull": "BTC"}, 65000.0, 64800.0)
        assert ok


# ─── discovery resolver: fail-closed, no placeholders ─────────────────────────

class TestSymbolResolution:
    IMAP = {1003: "META", 1456: "VALT.L"}

    def test_known_symbol_resolves(self):
        assert _symbol_to_instrument_id("META", self.IMAP) == 1003
        assert _symbol_to_instrument_id("  valt.l ", self.IMAP) == 1456

    def test_unknown_symbol_returns_none_not_placeholder(self):
        result = _symbol_to_instrument_id("DOESNOTEXIST", self.IMAP)
        assert result is None

    def test_no_hash_placeholder_range(self):
        # Regression: the old fallback produced IDs in [100000, 999999].
        for sym in ("FOO", "BAR.L", "XYZ-USD"):
            assert _symbol_to_instrument_id(sym, {}) is None

    def test_empty_map(self):
        assert _symbol_to_instrument_id("META", {}) is None
