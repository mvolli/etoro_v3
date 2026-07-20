"""feat/signal-scorecard: deterministische Aggregation."""
from bot.core.signal_scorecard import STRATEGY_RULES, aggregate_scorecard


ROWS = [
    ("RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20", 12.0, 2.5),
    ("RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20", -4.0, -1.0),
    ("BB_LOWER_RSI_OVERSOLD,BB_EXTREME_RSI_OVERSOLD", -20.0, -3.5),
    ("BB_LOWER_RSI_OVERSOLD,BB_EXTREME_RSI_OVERSOLD", -18.0, -3.0),
    ("TREND_PULLBACK,GOLDEN_CROSS", 41.0, 6.3),
]


def test_combo_aggregation():
    sc = aggregate_scorecard(ROWS)
    by = {c["signal"]: c for c in sc["combos"]}
    knife = by["BB_LOWER_RSI_OVERSOLD,BB_EXTREME_RSI_OVERSOLD"]
    assert knife["n"] == 2 and knife["win_rate_pct"] == 0.0 and knife["sl_kills"] == 2
    # schlechteste Kombo steht vorn (nach PnL sortiert)
    assert sc["combos"][0]["signal"] == "BB_LOWER_RSI_OVERSOLD,BB_EXTREME_RSI_OVERSOLD"


def test_component_split():
    sc = aggregate_scorecard(ROWS)
    by = {c["signal"]: c for c in sc["components"]}
    assert by["RSI_EXTREME_OVERSOLD"]["n"] == 2
    assert by["BB_LOWER_RSI_OVERSOLD"]["n"] == 2
    assert by["GOLDEN_CROSS"]["win_rate_pct"] == 100.0


def test_macd_split():
    sc = aggregate_scorecard(ROWS)
    assert sc["macd_split"]["with"]["n"] == 2
    assert sc["macd_split"]["without"]["n"] == 3
    assert sc["macd_split"]["with"]["win_rate_pct"] == 50.0


def test_rules_mention_knife_and_asymmetry():
    assert "Falling Knife" in STRATEGY_RULES
    assert "verstaerken NIE" in STRATEGY_RULES
