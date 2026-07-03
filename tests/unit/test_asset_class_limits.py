#!/usr/bin/env python3
"""Unit tests — fix/asset-class-limits-gap.

FINANCIAL/CONSUMER/HEALTHCARE/ENERGY are populated in ASSET_CLASS_MAP
(JPM/WMT/JNJ/XOM etc.) but had no entry in ASSET_CLASS_LIMITS —
check_asset_class_gate()'s `.get(asset_class, 100.0)` fallback meant these
four sectors had NO enforced concentration limit at all. Covers the fix
(20% cap, matching COMMODITY/BOND/INTL) and locks in that every mapped
asset class now has a real (<100%) limit, so this gap can't silently
reopen if a new class gets added to ASSET_CLASS_MAP without a matching
ASSET_CLASS_LIMITS entry.
"""
from __future__ import annotations

import pytest

from bot.core.risk import ASSET_CLASS_LIMITS, ASSET_CLASS_MAP, check_asset_class_gate


def test_financial_sector_now_capped():
    # 5 positions of $1500 each = $7500 = 15% of $50k equity (under 20% cap)
    open_positions = [{"symbol": s, "amount_usd": 1500.0} for s in ("JPM", "BAC", "GS", "V", "MA")]
    result = check_asset_class_gate("C", buy_amount=1000.0, equity=50_000.0, open_positions=open_positions)
    assert result.allowed  # 8500/50000 = 17% — still under 20%


def test_financial_sector_blocks_over_cap():
    # $9000 existing (18%) + $2000 new = $11000 = 22% > 20% cap
    open_positions = [{"symbol": s, "amount_usd": 1800.0} for s in ("JPM", "BAC", "GS", "V", "MA")]
    result = check_asset_class_gate("WFC", buy_amount=2000.0, equity=50_000.0, open_positions=open_positions)
    assert not result.allowed
    assert "FINANCIAL" in result.summary()


@pytest.mark.parametrize("asset_class", ["FINANCIAL", "CONSUMER", "HEALTHCARE", "ENERGY"])
def test_previously_unlimited_classes_now_capped_at_20pct(asset_class):
    limit = ASSET_CLASS_LIMITS[asset_class]
    assert limit == 20.0
    # A single existing position already at the cap; any further buy must block.
    symbol = next(s for s, cls in ASSET_CLASS_MAP.items() if cls == asset_class)
    equity = 10_000.0
    open_positions = [{"symbol": symbol, "amount_usd": equity * limit / 100.0}]
    result = check_asset_class_gate(symbol, buy_amount=1.0, equity=equity, open_positions=open_positions)
    assert not result.allowed


def test_no_asset_class_falls_back_to_unlimited_get_default():
    # Consumer/Healthcare/Energy/Financial each map to a real key with a
    # real (non-100.0) limit now. Regression guard: ASSET_CLASS_LIMITS must
    # cover every class that ASSET_CLASS_MAP actually assigns, so the
    # `.get(asset_class, 100.0)` fallback in check_asset_class_gate() never
    # silently activates for a class someone forgot to cap.
    mapped_classes = set(ASSET_CLASS_MAP.values())
    uncapped = mapped_classes - set(ASSET_CLASS_LIMITS)
    assert uncapped == set(), f"asset classes with no enforced limit: {uncapped}"


def test_healthcare_sector_allows_under_cap():
    open_positions = [{"symbol": "JNJ", "amount_usd": 1000.0}]
    result = check_asset_class_gate("PFE", buy_amount=500.0, equity=20_000.0, open_positions=open_positions)
    assert result.allowed  # 1500/20000 = 7.5%, well under 20%
