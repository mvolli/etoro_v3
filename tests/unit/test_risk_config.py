#!/usr/bin/env python3
"""Unit tests — fix/risk-config-wiring.

apply_config() must actually override the risk constants (the old comment
"overridden by config" was a lie), mutate INSTRUMENT_LIMITS in place so
imported references see updates, and be robust against bad values.
"""
from __future__ import annotations

import pytest

import bot.core.risk as risk


@pytest.fixture(autouse=True)
def _restore_constants():
    saved = {
        "MAX_POSITIONS": risk.MAX_POSITIONS,
        "MIN_BUY_USD": risk.MIN_BUY_USD,
        "CASH_TARGET_MIN_PCT": risk.CASH_TARGET_MIN_PCT,
        "SL_HARD_CLOSE_PCT": risk.SL_HARD_CLOSE_PCT,
        "SL_EMERGENCY_PCT": risk.SL_EMERGENCY_PCT,
        "SL_WARNING_PCT": risk.SL_WARNING_PCT,
        "MAX_FRAGMENTS_PER_INSTRUMENT": risk.MAX_FRAGMENTS_PER_INSTRUMENT,
    }
    saved_limits = dict(risk.INSTRUMENT_LIMITS)
    yield
    for k, v in saved.items():
        setattr(risk, k, v)
    risk.INSTRUMENT_LIMITS.clear()
    risk.INSTRUMENT_LIMITS.update(saved_limits)


def test_trading_overrides():
    risk.apply_config({"trading": {
        "max_positions": 10, "min_buy_usd": 100.0,
        "cash_target_min_pct": 20.0, "max_fragments_per_instrument": 2,
    }})
    assert risk.MAX_POSITIONS == 10
    assert risk.MIN_BUY_USD == 100.0
    assert risk.CASH_TARGET_MIN_PCT == 20.0
    assert risk.MAX_FRAGMENTS_PER_INSTRUMENT == 2


def test_sl_overrides_become_negative_thresholds():
    risk.apply_config({"sl": {"default_pct": 2.5, "emergency_pct": 5.0, "warning_pct": 1.5}})
    assert risk.SL_HARD_CLOSE_PCT == -2.5
    assert risk.SL_EMERGENCY_PCT == -5.0
    assert risk.SL_WARNING_PCT == -1.5
    # evaluate_sl nutzt die neuen Schwellen zur Laufzeit
    assert risk.evaluate_sl(-2.6).action == "CLOSE"
    assert risk.evaluate_sl(-1.6).action == "WARNING"


def test_instrument_limits_merged_in_place():
    # concentration_monitor haelt eine importierte Referenz auf das Dict —
    # apply_config muss in-place mutieren, nicht rebinden
    ref = risk.INSTRUMENT_LIMITS
    risk.apply_config({"instrument_limits": {"nvda": 30.0, "NEWSTOCK": 7.5}})
    assert ref["NVDA"] == 30.0
    assert ref["NEWSTOCK"] == 7.5
    assert ref is risk.INSTRUMENT_LIMITS


def test_gates_use_overrides_at_runtime():
    risk.apply_config({"trading": {"max_positions": 5, "max_fragments_per_instrument": 1}})
    assert not risk.check_max_positions_gate(5).allowed
    # None-Default im Gate loest zur Laufzeit auf → Config-Override greift
    assert not risk.check_pyramiding_gate("NVDA", "NORMAL", 1).allowed


def test_empty_and_bad_config_are_safe():
    before = risk.MAX_POSITIONS
    risk.apply_config({})
    risk.apply_config(None)
    risk.apply_config({"trading": {"max_positions": "kaputt"}})
    assert risk.MAX_POSITIONS == before


def test_cash_emergency_floor():
    # < 10% → EMERGENCY-Meldung; 10–15% → normaler Soft-Floor-Block; ≥15% → OK
    emergency = risk.check_cash_gate(cash=900, equity=10_000)
    assert not emergency.allowed
    assert "EMERGENCY" in emergency.summary()

    soft = risk.check_cash_gate(cash=1_200, equity=10_000)
    assert not soft.allowed
    assert "EMERGENCY" not in soft.summary()

    assert risk.check_cash_gate(cash=1_600, equity=10_000).allowed


def test_cash_emergency_configurable():
    saved = risk.CASH_EMERGENCY_PCT
    try:
        risk.apply_config({"trading": {"cash_emergency_pct": 5.0}})
        assert risk.CASH_EMERGENCY_PCT == 5.0
        # 8% liegt jetzt über dem Hard-Floor → normaler Soft-Floor-Block
        result = risk.check_cash_gate(cash=800, equity=10_000)
        assert not result.allowed
        assert "EMERGENCY" not in result.summary()
    finally:
        risk.CASH_EMERGENCY_PCT = saved


def test_real_config_yaml_roundtrip():
    import yaml
    from pathlib import Path
    cfg_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    risk.apply_config(cfg)
    assert risk.MAX_POSITIONS == int(cfg["trading"]["max_positions"])
    assert risk.SL_HARD_CLOSE_PCT == -abs(float(cfg["sl"]["default_pct"]))
    assert risk.INSTRUMENT_LIMITS["NVDA"] == float(cfg["instrument_limits"]["NVDA"])
