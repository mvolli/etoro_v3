"""feat/strategy-gates: ATR-adaptiver SL (pure Logik)."""
from bot.core.risk import adaptive_sl_pct


def test_no_atr_keeps_default():
    assert adaptive_sl_pct(3.0, None) == 3.0
    assert adaptive_sl_pct(3.0, 0) == 3.0
    assert adaptive_sl_pct(3.0, "kaputt") == 3.0


def test_low_atr_keeps_default():
    # ATR 1.2% * 1.5 = 1.8% < Default 3.0 -> Default haelt
    assert adaptive_sl_pct(3.0, 1.2) == 3.0


def test_high_atr_widens_sl():
    # LUS1.DE-Fall: ATR 7.4% -> 1.5*7.4=11.1 -> Cap 6.0
    assert adaptive_sl_pct(3.0, 7.4) == 6.0
    # BOKU.L: ATR 3.0 -> 4.5, unter Cap
    assert adaptive_sl_pct(3.0, 3.0) == 4.5


def test_cap_respected():
    assert adaptive_sl_pct(3.0, 50.0, multiple=1.5, max_pct=6.0) == 6.0
