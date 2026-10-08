"""Unit-Tests für fee_model.py (fix/fee-tracking, 2026-10-08)."""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src"
sys.path.insert(0, str(SRC))

from bot.core.fee_model import (
    fee_pct_for_symbol,
    estimate_open_fee,
    load_fee_config,
)


# ── fee_pct_for_symbol ────────────────────────────────────────────────────────

def test_std_1pct():
    assert fee_pct_for_symbol("AAPL") == 1.0
    assert fee_pct_for_symbol("VOO") == 1.0
    assert fee_pct_for_symbol("MSFT") == 1.0

def test_high_2pct_asx():
    assert fee_pct_for_symbol("BHP.AX") == 2.0
    assert fee_pct_for_symbol("CBA.AX") == 2.0

def test_high_2pct_hk():
    assert fee_pct_for_symbol("2600.HK") == 2.0
    assert fee_pct_for_symbol("700.HK") == 2.0

def test_high_2pct_t():
    assert fee_pct_for_symbol("TM.T") == 2.0
    assert fee_pct_for_symbol("7203.T") == 2.0

def test_high_2pct_ax():
    assert fee_pct_for_symbol("BHP.AX") == 2.0

def test_t_no_false_positive_tw():
    """`.T` matcht `.T` aber NICHT `.TW` (Taiwan)."""
    assert fee_pct_for_symbol("2330.TW") == 1.0

def test_case_insensitive():
    assert fee_pct_for_symbol("bhp.ax") == 2.0
    assert fee_pct_for_symbol("2600.hk") == 2.0

def test_no_suffix():
    assert fee_pct_for_symbol("AAPL") == 1.0
    assert fee_pct_for_symbol("") == 1.0


# ── estimate_open_fee ─────────────────────────────────────────────────────────

def test_fee_calculation_std():
    # $100 × 1 % = $1.00
    assert estimate_open_fee(100.0, "AAPL") == 1.00

def test_fee_calculation_high():
    # $50 × 2 % = $1.00
    assert estimate_open_fee(50.0, "2600.HK") == 1.00

def test_fee_calculation_precise():
    # $101.77 × 1 % = $1.0177
    assert estimate_open_fee(101.77, "AAPL") == 1.0177

def test_fee_none_on_zero():
    assert estimate_open_fee(0, "AAPL") is None

def test_fee_none_on_negative():
    assert estimate_open_fee(-10, "AAPL") is None

def test_fee_none_on_null():
    assert estimate_open_fee(None, "AAPL") is None

def test_fee_none_on_typeerror():
    assert estimate_open_fee("abc", "AAPL") is None


# ── load_fee_config ───────────────────────────────────────────────────────────

def test_load_config_none():
    cfg = load_fee_config(None)
    assert cfg["high_fee_pct"] == 2.0
    assert ".ASX" in cfg["high_fee_suffixes"]

def test_load_config_valid():
    cfg = load_fee_config({"fee_tiering": {
        "high_fee_suffixes": [".ASX", ".HK"],
        "fee_pct": 3.0,
    }})
    assert cfg["high_fee_pct"] == 3.0
    assert ".ASX" in cfg["high_fee_suffixes"]
    assert ".T" not in cfg["high_fee_suffixes"]

def test_load_config_empty():
    cfg = load_fee_config({})
    assert cfg["high_fee_pct"] == 2.0

def test_load_config_malformed():
    cfg = load_fee_config({"fee_tiering": {"fee_pct": "not_a_number"}})
    assert cfg["high_fee_pct"] == 2.0  # fail-open


# ── Integration: fee in estimate with custom config ───────────────────────────

def test_custom_config_high_fee():
    cfg = load_fee_config({"fee_tiering": {
        "high_fee_suffixes": [".XYZ"],
        "fee_pct": 5.0,
    }})
    # .XYZ → 5 %
    assert estimate_open_fee(100.0, "TEST.XYZ",
                             cfg["high_fee_suffixes"],
                             cfg["high_fee_pct"]) == 5.00
    # AAPL → 1 %
    assert estimate_open_fee(100.0, "AAPL",
                             cfg["high_fee_suffixes"],
                             cfg["high_fee_pct"]) == 1.00


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  ✓ {name}")
            except Exception as e:
                failed += 1
                print(f"  ✗ {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
