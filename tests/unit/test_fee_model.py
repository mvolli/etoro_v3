"""Unit-Tests für fee_model.py (fix/fee-flat, 2026-10-09).

Flat-Modell: $1.00 Standard / $2.00 High-Fee — NICHT prozentual.
API-Beweis: 18 offene Bot-Positionen, 10×$2 + 8×$1 = $28 total.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src"
sys.path.insert(0, str(SRC))

from bot.core.fee_model import (
    fee_pct_for_symbol,
    is_high_fee_symbol,
    estimate_open_fee,
    load_fee_config,
)


# ── is_high_fee_symbol ────────────────────────────────────────────────────────

def test_is_high_asx():
    assert is_high_fee_symbol("BHP.AX") is True

def test_is_high_hk():
    assert is_high_fee_symbol("2600.HK") is True

def test_is_high_t():
    assert is_high_fee_symbol("TM.T") is True
    assert is_high_fee_symbol("7203.T") is True

def test_is_high_ae():
    # EMAAR.AE: $2 im API-Beleg
    assert is_high_fee_symbol("EMAAR.AE") is True

def test_not_high_us():
    assert is_high_fee_symbol("AAPL") is False
    assert is_high_fee_symbol("VOO") is False

def test_not_high_de():
    assert is_high_fee_symbol("SIE.DE") is False
    assert is_high_fee_symbol("DTEG.DE") is False

def test_t_no_false_positive_tw():
    """`.T` matcht `.T` aber NICHT `.TW` (Taiwan)."""
    assert is_high_fee_symbol("2330.TW") is False

def test_case_insensitive():
    assert is_high_fee_symbol("bhp.ax") is True
    assert is_high_fee_symbol("2600.hk") is True

def test_no_suffix():
    assert is_high_fee_symbol("AAPL") is False
    assert is_high_fee_symbol("") is False


# ── fee_pct_for_symbol (LEGACY — nur Kandidaten-Ranking) ─────────────────────

def test_fee_pct_std():
    assert fee_pct_for_symbol("AAPL") == 1.0
    assert fee_pct_for_symbol("VOO") == 1.0

def test_fee_pct_high_asx():
    assert fee_pct_for_symbol("BHP.AX") == 2.0

def test_fee_pct_high_hk():
    assert fee_pct_for_symbol("2600.HK") == 2.0

def test_fee_pct_custom():
    assert fee_pct_for_symbol("TEST.XYZ", high_fee_suffixes=(".XYZ",), high_fee_pct=5.0) == 5.0


# ── estimate_open_fee (FLAT $1/$2) ────────────────────────────────────────────

def test_flat_std_us():
    """$100 AAPL → $1.00 flat (Nicht 100×1%=1.00 — Zufall, aber egal)."""
    assert estimate_open_fee(100.0, "AAPL") == 1.0

def test_flat_std_de():
    """$500 SIE.DE → $1.00 flat."""
    assert estimate_open_fee(500.0, "SIE.DE") == 1.0

def test_flat_high_hk():
    """$50 2600.HK → $2.00 flat (Nicht 50×2%=1.00)."""
    assert estimate_open_fee(50.0, "2600.HK") == 2.0

def test_flat_high_asx():
    """$200 BHP.AX → $2.00 flat (Nicht 200×2%=4.00)."""
    assert estimate_open_fee(200.0, "BHP.AX") == 2.0

def test_flat_high_ae():
    """$300 EMAAR.AE → $2.00 flat."""
    assert estimate_open_fee(300.0, "EMAAR.AE") == 2.0

def test_flat_not_proportional_large():
    """$1000 AAPL → $1.00 flat (Nicht 1000×1%=10.00)."""
    assert estimate_open_fee(1000.0, "AAPL") == 1.0

def test_flat_not_proportional_high_large():
    """$1000 2600.HK → $2.00 flat (Nicht 1000×2%=20.00)."""
    assert estimate_open_fee(1000.0, "2600.HK") == 2.0

def test_flat_small_amount_same():
    """$10 AAPL → $1.00 flat (Nicht 10×1%=0.10)."""
    assert estimate_open_fee(10.0, "AAPL") == 1.0

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
    assert cfg["high_fee_flat"] == 2.0
    assert cfg["std_fee_flat"] == 1.0
    assert cfg["high_fee_pct"] == 2.0  # legacy
    assert ".ASX" in cfg["high_fee_suffixes"]
    assert ".AE" in cfg["high_fee_suffixes"]  # fix/fee-flat: .AE hinzugefügt

def test_load_config_valid_flat():
    cfg = load_fee_config({"fee_tiering": {
        "high_fee_suffixes": [".ASX", ".HK"],
        "fee_flat_high": 3.0,
        "fee_flat_std": 0.5,
    }})
    assert cfg["high_fee_flat"] == 3.0
    assert cfg["std_fee_flat"] == 0.5
    assert ".ASX" in cfg["high_fee_suffixes"]
    assert ".T" not in cfg["high_fee_suffixes"]

def test_load_config_legacy_fallback():
    """Ohne fee_flat_* → fee_pct als Flat-Fallback."""
    cfg = load_fee_config({"fee_tiering": {
        "high_fee_suffixes": [".ASX"],
        "fee_pct": 2.0,
    }})
    # fee_pct=2.0 wird als high_fee_flat interpretiert
    assert cfg["high_fee_flat"] == 2.0
    assert cfg["std_fee_flat"] == 1.0

def test_load_config_empty():
    cfg = load_fee_config({})
    assert cfg["high_fee_flat"] == 2.0
    assert cfg["std_fee_flat"] == 1.0

def test_load_config_malformed():
    cfg = load_fee_config({"fee_tiering": {"fee_pct": "not_a_number"}})
    assert cfg["high_fee_flat"] == 2.0  # fail-open


# ── Integration: fee in estimate with custom config ───────────────────────────

def test_custom_config_high_fee():
    cfg = load_fee_config({"fee_tiering": {
        "high_fee_suffixes": [".XYZ"],
        "fee_flat_high": 5.0,
        "fee_flat_std": 0.5,
    }})
    # .XYZ → $5.00 flat
    assert estimate_open_fee(100.0, "TEST.XYZ",
                             cfg["high_fee_suffixes"],
                             cfg["high_fee_flat"],
                             cfg["std_fee_flat"]) == 5.0
    # AAPL → $0.50 flat (custom std)
    assert estimate_open_fee(100.0, "AAPL",
                             cfg["high_fee_suffixes"],
                             cfg["high_fee_flat"],
                             cfg["std_fee_flat"]) == 0.5


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
