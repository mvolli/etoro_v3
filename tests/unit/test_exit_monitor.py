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
