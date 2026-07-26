"""Tests feat/falling-knife-gate + feat/analyst-targets (2026-07-26).

Quantitative Messer-Erkennung (signals.py) und Analysten-Kursziel-Flags
(news_flags_worker.py).
"""

import pandas as pd
import pytest

from bot.core import signals as sig
from bot.core.signals import compute_indicators, generate_signal, is_falling_knife
from bot.workers.news_flags_worker import _evaluate_analyst_target


# ── is_falling_knife: Kriterien ──────────────────────────────────────────────

def test_no_knife_on_empty_indicators():
    assert is_falling_knife({}) == (False, "")


def test_knife_on_consecutive_down_days():
    knife, reason = is_falling_knife({"consecutive_down_days": 4})
    assert knife and "4 rote Tage" in reason
    assert is_falling_knife({"consecutive_down_days": 3})[0] is False


def test_knife_on_roc_5d():
    knife, reason = is_falling_knife({"roc_5d_pct": -15.0})
    assert knife and "ROC" in reason
    assert is_falling_knife({"roc_5d_pct": -8.0})[0] is False


def test_knife_on_atr_distance_below_sma20():
    # Preis 90, SMA20 100, ATR 3 → 3.33 ATR unter SMA20 = Messer
    knife, reason = is_falling_knife({"price": 90.0, "sma20": 100.0, "atr": 3.0})
    assert knife and "ATR unter SMA20" in reason
    # Preis 95, SMA20 100, ATR 3 → 1.67 ATR = kein Messer
    assert is_falling_knife({"price": 95.0, "sma20": 100.0, "atr": 3.0})[0] is False
    # Preis UEBER SMA20: nie Messer ueber dieses Kriterium
    assert is_falling_knife({"price": 105.0, "sma20": 100.0, "atr": 3.0})[0] is False


def test_knife_gate_can_be_disabled(monkeypatch):
    monkeypatch.setattr(sig, "KNIFE_GATE_ENABLED", False)
    assert is_falling_knife({"consecutive_down_days": 9})[0] is False


# ── compute_indicators: neue Metriken ────────────────────────────────────────

def _make_df(closes, volumes=None):
    n = len(closes)
    volumes = volumes or [1_000_000] * n
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": volumes,
    })


def test_compute_indicators_knife_metrics():
    # 60 flache Tage, dann 5 fallende
    closes = [100.0] * 60 + [98.0, 96.0, 94.0, 92.0, 88.0]
    ind = compute_indicators(_make_df(closes))
    assert ind["consecutive_down_days"] == 5
    # ROC 5d: 88 vs. 100 → -12%
    assert ind["roc_5d_pct"] == pytest.approx(-12.0, abs=0.01)


def test_compute_indicators_no_down_streak():
    closes = [100.0] * 64 + [101.0]
    ind = compute_indicators(_make_df(closes))
    assert ind["consecutive_down_days"] == 0
    assert ind["roc_5d_pct"] == pytest.approx(1.0, abs=0.01)


# ── generate_signal: Gate blockt Dip-Regeln ──────────────────────────────────

def _oversold_indicators(**overrides):
    """Indikator-Set, das ohne Messer Rule 3 (RSI_EXTREME_OVERSOLD HIGH) feuert."""
    base = {
        "rsi": 22.0, "macd_hist": -0.5, "macd_hist_prev": -0.6,
        "bb_pct": 0.5, "price": 100.0, "sma20": 101.0, "sma50": 99.0,
        "atr": 2.0, "vol_ratio": 1.0,
    }
    base.update(overrides)
    return base


def test_oversold_fires_without_knife():
    result = generate_signal("TEST", _oversold_indicators())
    assert "RSI_EXTREME_OVERSOLD" in (result.signal_types or [])


def test_knife_blocks_oversold_rules():
    result = generate_signal(
        "TEST", _oversold_indicators(consecutive_down_days=6)
    )
    assert "RSI_EXTREME_OVERSOLD" not in (result.signal_types or [])


def test_knife_does_not_block_macd_turn():
    """Rule 4 (MACD-Wende) bleibt im Messer erlaubt — sie IST die Bestaetigung."""
    ind = _oversold_indicators(
        consecutive_down_days=6, price=95.0, sma20=100.0,
        macd_hist=-0.3, macd_hist_prev=-0.6,
    )
    result = generate_signal("TEST", ind)
    types = result.signal_types or []
    assert "MACD_TURN_BELOW_SMA20" in types
    assert "RSI_EXTREME_OVERSOLD" not in types


def test_rule3_distribution_volume_demotes_to_medium():
    """feat/falling-knife-gate: vol_ratio >= 1.5 (Distribution) drueckt Rule 3
    auf MEDIUM — vorher hatte Rule 3 keinerlei Volumenfilter."""
    hi = generate_signal("TEST", _oversold_indicators(vol_ratio=1.0))
    lo = generate_signal("TEST", _oversold_indicators(vol_ratio=2.0))
    assert hi.conviction == "HIGH"
    assert lo.conviction == "MEDIUM"


# ── Analysten-Kursziele ──────────────────────────────────────────────────────

def test_analyst_target_no_data_no_flag():
    assert _evaluate_analyst_target(None, 100.0) is None
    assert _evaluate_analyst_target(100.0, None) is None
    assert _evaluate_analyst_target(0.0, 100.0) is None


def test_analyst_target_below_mean_no_flag():
    # Preis unter Kursziel = Upside laut Street → kein Flag (nie boosten)
    assert _evaluate_analyst_target(80.0, 100.0) is None
    assert _evaluate_analyst_target(104.0, 100.0) is None  # +4% < 5% Toleranz


def test_analyst_target_caution_and_avoid():
    caution = _evaluate_analyst_target(110.0, 100.0)
    assert caution["flag"] == "CAUTION" and caution["source"] == "analyst_target"
    avoid = _evaluate_analyst_target(130.0, 100.0)
    assert avoid["flag"] == "AVOID"
    assert "30%" in avoid["reason"]
