#!/usr/bin/env python3
"""Unit tests — fix/asset-class-concentration (audit H7).

Post-trade asset-class drift detection: check_asset_class_gate blocks new
buys past a sector cap, but price appreciation can drift a whole class past
its limit afterwards. check_asset_class_violations() detects that (warn-only,
no auto-close).
"""
from __future__ import annotations

from bot.core.concentration_monitor import check_asset_class_violations


# instrument_map: id -> symbol. Use real US_TECH names (cap 40%).
_MAP = {1: "NVDA", 2: "META", 3: "MSFT", 4: "JPM", 5: "GS"}


def _pos(iid, amount):
    return {"instrumentID": iid, "amount": amount}


def test_no_violation_under_cap():
    # US_TECH 30% of 10k, cap 40% → no violation
    positions = [_pos(1, 1500), _pos(2, 1500)]
    assert check_asset_class_violations(positions, 10_000.0, _MAP) == []


def test_us_tech_drift_over_cap_detected():
    # NVDA+META+MSFT = 4500 = 45% of 10k, US_TECH cap 40% → violation
    positions = [_pos(1, 1500), _pos(2, 1500), _pos(3, 1500)]
    v = check_asset_class_violations(positions, 10_000.0, _MAP)
    assert len(v) == 1
    assert v[0]["asset_class"] == "US_TECH"
    assert round(v[0]["actual_pct"]) == 45
    assert v[0]["limit_pct"] == 40.0
    assert v[0]["symbols"] == ["META", "MSFT", "NVDA"]


def test_financial_uses_20pct_cap():
    # JPM+GS = 2500 = 25% of 10k, FINANCIAL cap 20% (from finding #3) → violation
    positions = [_pos(4, 1500), _pos(5, 1000)]
    v = check_asset_class_violations(positions, 10_000.0, _MAP)
    assert len(v) == 1
    assert v[0]["asset_class"] == "FINANCIAL"
    assert v[0]["limit_pct"] == 20.0


def test_unmapped_symbol_ignored():
    positions = [{"instrumentID": 999, "amount": 9000}]  # not in _MAP → no class
    assert check_asset_class_violations(positions, 10_000.0, {}) == []


def test_zero_equity_returns_empty():
    assert check_asset_class_violations([_pos(1, 1000)], 0.0, _MAP) == []


def test_multiple_classes_reported_independently():
    # US_TECH 45% (violation) + FINANCIAL 25% (violation)
    positions = [_pos(1, 1500), _pos(2, 1500), _pos(3, 1500), _pos(4, 1500), _pos(5, 1000)]
    v = check_asset_class_violations(positions, 10_000.0, _MAP)
    classes = {x["asset_class"] for x in v}
    assert classes == {"US_TECH", "FINANCIAL"}
