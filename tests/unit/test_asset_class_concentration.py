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


# ── Unification (2026-09-11): DB-Sektor-Fallback wie das Pre-Trade-Gate ─────────
# resolve_asset_class: kuratiertes ASSET_CLASS_MAP zuerst, dann instruments.sector
# als SECTOR:{sektor}. Nur aktiv, wenn der Worker eine Sektor-Map übergibt.


def test_sector_fallback_for_unmapped_symbol():
    # SAVE ist NICHT in ASSET_CLASS_MAP, aber DB-Sektor vorhanden →
    # SECTOR:RENEWABLES. 90% von 10k > Default-Cap 20% → Violation.
    # (Vor der Unification wurde dieses Symbol still übersprungen.)
    m = {6: "SAVE"}
    v = check_asset_class_violations([_pos(6, 9000)], 10_000.0, m,
                                     {"SAVE": "RENEWABLES"})
    assert len(v) == 1
    assert v[0]["asset_class"] == "SECTOR:RENEWABLES"
    assert v[0]["limit_pct"] == 20.0


def test_sector_fallback_inactive_without_map():
    # Keine Sektor-Map (None oder leeres dict) → ASSET_CLASS_MAP-only,
    # unmapped SAVE wird ignoriert. Backwards-kompatibel mit dem alten
    # ASSET_CLASS_MAP.get()-Verhalten.
    m = {6: "SAVE"}
    assert check_asset_class_violations([_pos(6, 9000)], 10_000.0, m) == []
    assert check_asset_class_violations([_pos(6, 9000)], 10_000.0, m, {}) == []


def test_sector_equivalent_class_merges_into_sector():
    # JPM ist kuratiert FINANCIAL — ein _SECTOR_EQUIVALENT_CLASS. Mit DB-Sektor
    # löst resolve_asset_class zu SECTOR:Financial Services auf (weil FINANCIAL
    # nur den Default-Cap liefern würde) — exakt wie das Pre-Trade-Gate.
    m = {4: "JPM"}
    v = check_asset_class_violations([_pos(4, 9000)], 10_000.0, m,
                                     {"JPM": "Financial Services"})
    assert len(v) == 1
    assert v[0]["asset_class"] == "SECTOR:Financial Services"
    assert v[0]["limit_pct"] == 20.0
