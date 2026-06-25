"""Unit tests for risk gates V5."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.bot.core.risk import (
    check_regime_gate, check_conviction_gate, check_pyramiding_gate,
    check_sl_quality_gate, calculate_sl_price,
    check_buy_gate, evaluate_sl, check_sl_gate,
    CRYPTO_SYMBOLS,
)

# ─── Regime Gate V5 ──────────────────────────────────────────────────────────

def test_regime_normal_allows(): assert check_regime_gate("NORMAL").allowed
def test_regime_caution_allows(): assert check_regime_gate("CAUTION").allowed
def test_regime_defensive_allows(): assert check_regime_gate("DEFENSIVE").allowed
def test_regime_critical_allows(): assert check_regime_gate("CRITICAL").allowed
def test_regime_legacy_drawdown_blocks(): assert not check_regime_gate("DRAWDOWN").allowed

# ─── Conviction Gate ─────────────────────────────────────────────────────────

def test_conviction_normal_accepts_low():
    assert check_conviction_gate("LOW", "NORMAL").allowed

def test_conviction_caution_blocks_low():
    assert not check_conviction_gate("LOW", "CAUTION").allowed

def test_conviction_caution_accepts_medium():
    assert check_conviction_gate("MEDIUM", "CAUTION").allowed

def test_conviction_defensive_blocks_medium():
    assert not check_conviction_gate("MEDIUM", "DEFENSIVE").allowed

def test_conviction_defensive_accepts_high():
    assert check_conviction_gate("HIGH", "DEFENSIVE").allowed

def test_conviction_critical_blocks_high():
    assert not check_conviction_gate("HIGH", "CRITICAL").allowed

def test_conviction_critical_accepts_very_high():
    assert check_conviction_gate("VERY_HIGH", "CRITICAL").allowed

# ─── Pyramiding Gate ─────────────────────────────────────────────────────────

def test_pyramiding_allowed_normal():
    assert check_pyramiding_gate("AAPL", "NORMAL", 2).allowed

def test_pyramiding_allowed_caution():
    assert check_pyramiding_gate("NVDA", "CAUTION", 1).allowed

def test_pyramiding_blocked_defensive():
    assert not check_pyramiding_gate("TSLA", "DEFENSIVE", 1).allowed

def test_pyramiding_blocked_critical():
    assert not check_pyramiding_gate("META", "CRITICAL", 2).allowed

def test_pyramiding_first_fragment_always_ok():
    # First buy (0 existing) always OK even in DEFENSIVE
    assert check_pyramiding_gate("TSLA", "DEFENSIVE", 0).allowed

# ─── SL Quality Gate ─────────────────────────────────────────────────────────

def test_sl_quality_ok():
    result = check_sl_quality_gate(entry_price=100.0, sl_price=97.0)
    assert result.allowed  # 3% distance — fine

def test_sl_quality_meaningful():
    result = check_sl_quality_gate(entry_price=212.50, sl_price=206.0)
    assert result.allowed  # ~3% — fine

def test_sl_quality_too_far():
    result = check_sl_quality_gate(entry_price=100.0, sl_price=40.0)
    assert not result.allowed  # 60% away — meaningless

def test_sl_quality_crypto_near_zero():
    # XRP entry ~$0.60, SL $0.01 — classic bug
    result = check_sl_quality_gate(
        entry_price=0.60, sl_price=0.01, symbol="XRP-USD"
    )
    assert not result.allowed

def test_sl_quality_no_sl_skipped():
    # sl_price=0 → skipped (check_sl_gate handles it)
    result = check_sl_quality_gate(entry_price=100.0, sl_price=0.0)
    assert result.allowed

# ─── SL Calculation ──────────────────────────────────────────────────────────

def test_calculate_sl_normal():
    sl = calculate_sl_price(100.0, "AAPL", 3.0)
    assert abs(sl - 97.0) < 0.01

def test_calculate_sl_crypto_relative():
    # XRP entry ~$62000 per unit (100000 units at $0.62)
    entry = 62504.85  # As shown in eToro API
    sl = calculate_sl_price(entry, "XRP-USD", 3.0)
    assert abs(sl - entry * 0.97) < 0.01
    assert sl > 1.0  # Never near zero

def test_crypto_symbol_detection():
    assert "XRP-USD" in CRYPTO_SYMBOLS
    assert "BTC-USD" in CRYPTO_SYMBOLS
    assert "ETH-USD" in CRYPTO_SYMBOLS
    assert "AAPL" not in CRYPTO_SYMBOLS

# ─── Full Gate V5 ─────────────────────────────────────────────────────────────

def test_buy_gate_caution_blocks_low_conviction():
    result = check_buy_gate(
        symbol="NVDA", buy_amount=500, equity=10000, cash=3000,
        regime="CAUTION", conviction="LOW", has_stop_loss=True,
    )
    assert not result.allowed
    assert "Conviction" in result.reasons[0]

def test_buy_gate_defensive_blocks_pyramiding():
    result = check_buy_gate(
        symbol="TSLA", buy_amount=300, equity=10000, cash=3000,
        regime="DEFENSIVE", conviction="HIGH",
        existing_fragments=1, has_stop_loss=True,
    )
    assert not result.allowed
    assert "Pyramiding" in result.reasons[0]

def test_buy_gate_critical_allows_very_high():
    result = check_buy_gate(
        symbol="AAPL", buy_amount=200, equity=10000, cash=3000,
        regime="CRITICAL", conviction="VERY_HIGH",
        existing_fragments=0, has_stop_loss=True,
    )
    assert result.allowed

def test_buy_gate_sl_quality_blocks_bad_sl():
    result = check_buy_gate(
        symbol="XRP-USD", buy_amount=200, equity=10000, cash=3000,
        regime="NORMAL", conviction="HIGH",
        has_stop_loss=True, entry_price=0.60, sl_price=0.001,
    )
    assert not result.allowed

# ─── SL Evaluation ───────────────────────────────────────────────────────────

def test_sl_ok(): assert evaluate_sl(-1.0).action == "OK"
def test_sl_warning(): assert evaluate_sl(-2.5).action == "WARNING"
def test_sl_hard_close(): assert evaluate_sl(-3.0).action == "CLOSE"
def test_sl_emergency():
    action = evaluate_sl(-4.5)
    assert action.action == "CLOSE"
    assert "EMERGENCY" in action.reason
