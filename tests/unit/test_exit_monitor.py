"""feat/exit-monitor-1h (2026-07-16): reine Erkennungslogik.

Synthetische 1H-Serien — kein Netz, keine Live-DB.
"""
import math

from bot.core.exit_monitor import RSI_MAX, detect_trend_kipp_1h


def _uptrend(n=60):
    return [100.0 + i * 0.5 for i in range(n)]


def _kipp_series():
    """Anstieg, dann harter Bruch — MACD kreuzt im Abstieg bearish, waehrend
    der RSI bereits unter 50 liegt (flacher Anstieg + steiler Abfall; bei
    zu sanftem Bruch faellt der Cross in ein Fenster mit RSI>50 und das
    2-Bar-Erkennungsfenster verstreicht — empirisch kalibriert)."""
    up = [100.0 + i * 0.3 for i in range(50)]
    down = [up[-1] - i * 2.0 for i in range(1, 18)]
    return up + down


def test_no_kipp_in_pure_uptrend():
    closes = _uptrend()
    for cut in range(40, len(closes) + 1):
        assert detect_trend_kipp_1h(closes[:cut]) is None


def test_kipp_detected_somewhere_in_decline():
    closes = _kipp_series()
    hits = [
        detect_trend_kipp_1h(closes[:cut])
        for cut in range(52, len(closes) + 1)
    ]
    found = [h for h in hits if h]
    assert found, "Bear-Cross im Abstieg muss mindestens einmal erkannt werden"
    assert all(h["rsi"] < RSI_MAX for h in found)
    assert all(h["macd_hist"] < 0 for h in found)
    assert all(math.isfinite(h["rsi"]) for h in found)


def test_too_short_series_is_none():
    assert detect_trend_kipp_1h([100.0] * 10) is None
    assert detect_trend_kipp_1h([]) is None


def test_rsi_max_parameter_respected():
    closes = _kipp_series()
    # Mit rsi_max=0 kann nie ein Treffer entstehen
    for cut in range(52, len(closes) + 1):
        assert detect_trend_kipp_1h(closes[:cut], rsi_max=0.0) is None


# ── OSS-Harvest (2026-07-16): Candles-Parsing, Spread-Gate, Earnings-Trigger ─

from bot.core.exit_monitor import closes_from_candles
from bot.core.risk import check_spread_gate
from bot.core.earnings_exit import should_trigger

_EE_CFG = {"exits": {"earnings_exit": {"days_before": 2, "min_exposure_pct": 5.0}}}


def test_closes_from_candles_drops_partial_bar():
    candles = [{"close": 1.0}, {"close": 2.0}, {"close": 3.0}]
    s = closes_from_candles(candles)
    assert list(s) == [1.0, 2.0]  # letzte (angebrochene) Bar weg
    assert len(closes_from_candles([])) == 0
    assert len(closes_from_candles([{"close": 5.0}])) == 0


def test_spread_gate_blocks_wide_spread():
    ok, pct = check_spread_gate({"bid": 100.0, "ask": 103.0}, 1.5)
    assert ok is False and pct is not None and pct > 2.9


def test_spread_gate_passes_tight_and_fails_open():
    ok, pct = check_spread_gate({"bid": 100.0, "ask": 100.5}, 1.5)
    assert ok is True and pct is not None
    assert check_spread_gate(None, 1.5) == (True, None)
    assert check_spread_gate({"bid": 0, "ask": 100}, 1.5) == (True, None)
    assert check_spread_gate({"bid": 101, "ask": 100}, 1.5) == (True, None)


def test_earnings_trigger_matrix():
    assert should_trigger(1, 6.0, _EE_CFG) is True
    assert should_trigger(0, 5.0, _EE_CFG) is True
    assert should_trigger(3, 10.0, _EE_CFG) is False   # zu weit weg
    assert should_trigger(1, 4.9, _EE_CFG) is False    # Position zu klein
    assert should_trigger(None, 10.0, _EE_CFG) is False
    assert should_trigger(-1, 10.0, _EE_CFG) is False  # Earnings vorbei
