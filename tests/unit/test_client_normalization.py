"""fix/client-symbol-normalization (2026-07-28): exchange-suffix aliases.

Covers EToroClient._normalize_symbol_for_comparison() (.ASX/.AX exchange
suffixes on top of the existing -USD/USD quote-currency stripping) and
verify_instrument_identity() with a fake client — no network, no DB.

Regression: the 'CAR.ASX resolves to instrument_id=3317, but local data
expected CAR.AX' pre-flight block (2026-07-28) — CAR.AX and CAR.ASX are
the same ASX listing under two suffix conventions and must normalize
to the same base ticker.

Also locks in that verify_instrument_identity() has NO price-based
fallback for a genuine symbol mismatch: a mismatch that survives
normalization must always be rejected, never waved through on price
similarity — that fallback would have silently re-opened the DOT-USD
Futures-vs-Spot-ID ghost-order incident (futures/spot prices for the
same underlying are routinely within any such tolerance).
"""
from __future__ import annotations

import types

from bot.api.client import EToroClient


# ─── _normalize_symbol_for_comparison ─────────────────────────────────────────

class TestNormalize:
    def test_usd_suffixes_stripped(self):
        assert EToroClient._normalize_symbol_for_comparison("BTC-USD") == "BTC"
        assert EToroClient._normalize_symbol_for_comparison("eth/usd") == "ETH"
        assert EToroClient._normalize_symbol_for_comparison("XRPUSD") == "XRP"

    def test_asx_ax_alias_matches(self):
        # The 2026-07-28 CAR.AX vs CAR.ASX pre-flight-block incident.
        assert (
            EToroClient._normalize_symbol_for_comparison("CAR.AX")
            == EToroClient._normalize_symbol_for_comparison("CAR.ASX")
            == "CAR"
        )

    def test_compound_exchange_and_quote_suffix(self):
        # Exchange suffix stripped before quote-currency suffix.
        assert EToroClient._normalize_symbol_for_comparison("CAR.ASX-USD") == "CAR"
        assert EToroClient._normalize_symbol_for_comparison("CAR.AX-USD") == "CAR"

    def test_different_base_ticker_stays_different(self):
        # Genuinely different instruments must NOT collapse to the same key.
        assert (
            EToroClient._normalize_symbol_for_comparison("DOT-USD")
            != EToroClient._normalize_symbol_for_comparison("BTC-USD")
        )

    def test_empty(self):
        assert EToroClient._normalize_symbol_for_comparison("") == ""


# ─── verify_instrument_identity via Fake-Client ───────────────────────────────

def _verify(expected_symbol, meta, current_price=None):
    fake = types.SimpleNamespace()
    fake.get_instrument_metadata = lambda instrument_id: meta
    fake.get_current_price = lambda instrument_id: current_price
    # verify_instrument_identity() calls the normalizer via self. — bind
    # the real (static) implementation onto the fake object too.
    fake._normalize_symbol_for_comparison = (
        EToroClient._normalize_symbol_for_comparison
    )
    return EToroClient.verify_instrument_identity(fake, 3317, expected_symbol)


class TestVerifyInstrumentIdentity:
    def test_asx_ax_alias_ok(self):
        # Same regression case, exercised through the public method.
        ok, reason = _verify("CAR.AX", {"symbolFull": "CAR.ASX"})
        assert ok
        assert "Identity OK" in reason

    def test_exact_match_ok(self):
        ok, _ = _verify("AAPL", {"symbolFull": "AAPL"})
        assert ok

    def test_hard_mismatch_is_never_fail_open(self):
        # Even with a live price that's suspiciously close to some
        # reference price, a genuine base-ticker mismatch must be
        # rejected — no price-similarity escape hatch.
        ok, reason = _verify(
            "DOT-USD", {"symbolFull": "BTC"}, current_price=50000.0
        )
        assert not ok
        assert "MISMATCH" in reason

    def test_fail_open_on_missing_metadata(self):
        ok, reason = _verify("AAPL", {})
        assert ok
        assert "fail-open" in reason

    def test_fail_open_on_missing_symbol_field(self):
        ok, reason = _verify("AAPL", {"someOtherField": "x"})
        assert ok
        assert "fail-open" in reason
