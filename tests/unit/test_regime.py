"""Unit tests for regime detection V5 — no DB, no API needed."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.bot.core.regime import detect_regime, get_regime_params, get_risk_scalar, aqr_risk_scalar
import pytest

# ─── Regime Detection ─────────────────────────────────────────────────────────

def test_normal(): assert detect_regime(9700, 10000)[0] == "NORMAL"
def test_caution(): assert detect_regime(9500, 10000)[0] == "CAUTION"   # 5% DD
def test_defensive(): assert detect_regime(9100, 10000)[0] == "DEFENSIVE"  # 9% DD
def test_critical(): assert detect_regime(8400, 10000)[0] == "CRITICAL"   # 16% DD

def test_hysteresis_caution_stays():
    # 5.5% from CAUTION: between 3.5% (exit) and 8% (defensive) → stays CAUTION
    regime, _ = detect_regime(9450, 10000, previous_regime="CAUTION")
    assert regime == "CAUTION"

def test_hysteresis_exits_to_normal():
    # 3.2% from CAUTION → exits to NORMAL (below 3.5% exit threshold)
    regime, _ = detect_regime(9680, 10000, previous_regime="CAUTION")
    assert regime == "NORMAL"

def test_hysteresis_defensive_stays():
    # 7.5% from DEFENSIVE: above 7.0% exit → stays DEFENSIVE
    regime, _ = detect_regime(9250, 10000, previous_regime="DEFENSIVE")
    assert regime == "DEFENSIVE"

def test_zero_peak(): assert detect_regime(9000, 0)[0] == "NORMAL"

# ─── Regime Parameters ────────────────────────────────────────────────────────

def test_params_normal():
    p = get_regime_params("NORMAL")
    assert p["cash_min_pct"] == 15.0
    assert p["buy_aggressiveness"] == 1.0
    assert p["allow_pyramiding"] == True
    assert p["min_conviction"] == "LOW"

def test_params_caution():
    p = get_regime_params("CAUTION")
    assert p["buy_aggressiveness"] == 0.75
    assert p["min_conviction"] == "MEDIUM"
    assert p["allow_pyramiding"] == True

def test_params_defensive():
    p = get_regime_params("DEFENSIVE")
    assert p["cash_min_pct"] == 25.0
    assert p["buy_aggressiveness"] == 0.50
    assert p["allow_pyramiding"] == False
    assert p["min_conviction"] == "HIGH"

def test_params_critical():
    p = get_regime_params("CRITICAL")
    assert p["buy_aggressiveness"] == 0.25
    assert p["allow_pyramiding"] == False
    assert p["min_conviction"] == "VERY_HIGH"

def test_params_invalid():
    with pytest.raises(ValueError): get_regime_params("DRAWDOWN")
    with pytest.raises(ValueError): get_regime_params("RECOVERY")

# ─── Risk Scalar ─────────────────────────────────────────────────────────────

def test_risk_scalars():
    assert get_risk_scalar("NORMAL") == 1.00
    assert get_risk_scalar("CAUTION") == 0.75
    assert get_risk_scalar("DEFENSIVE") == 0.50
    assert get_risk_scalar("CRITICAL") == 0.25

# ─── AQR Formula ─────────────────────────────────────────────────────────────

def test_aqr_no_drawdown(): assert aqr_risk_scalar(0) == 1.0
def test_aqr_10pct_dd(): assert abs(aqr_risk_scalar(10) - 0.80) < 0.01
def test_aqr_25pct_dd(): assert abs(aqr_risk_scalar(25) - 0.50) < 0.01
def test_aqr_minimum(): assert aqr_risk_scalar(50) == 0.25  # Capped at 0.25
