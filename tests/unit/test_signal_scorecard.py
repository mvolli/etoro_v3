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
    # fix/scorecard-macd-split-scope (2026-09-14): TREND_PULLBACK,GOLDEN_CROSS
    # in ROWS ist KEIN Dip-Buy -> ausserhalb des Splits (war 3, ist jetzt 2).
    assert sc["macd_split"]["with"]["n"] == 2
    assert sc["macd_split"]["without"]["n"] == 2
    assert sc["macd_split"]["with"]["win_rate_pct"] == 50.0
    assert sc["macd_split_scope"] == "dip-buy"


def test_macd_split_scope_dipbuy_only():
    """fix/scorecard-macd-split-scope (2026-09-14): macd_split zaehlt NUR
    die Dip-Buy-Familie (eine DIPBUY_TYPES_DEFAULT-Komponente im Signal).
    Reine Trend-Kombos (TREND_PULLBACK+GOLDEN_CROSS) und CORE_SWEEP
    bleiben ausserhalb — sonst verurteilt der 'MACD senkt WR'-Merksatz
    trend-folgende Signale (Beleg: 30d 'with' n=103 30.1%/-69.50 USD
    statt des dip-buy-echten n=105 29.5%/-73.10 USD, floss in 194
    Veto-Entscheide in 7d)."""
    rows = [
        # Dip-Buys: WITH MACD (3) und WITHOUT MACD (2)
        ("MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING", 10.0, 2.0),
        ("MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING", -3.0, -2.5),
        ("RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20", 5.0, 1.0),
        ("BB_LOWER_RSI_OVERSOLD,BB_EXTREME_RSI_OVERSOLD", -20.0, -3.5),
        ("BB_LOWER_RSI_OVERSOLD,BB_EXTREME_RSI_OVERSOLD", -18.0, -3.0),
        # NICHT Dip-Buys (muessen aus dem Split ausserhalb bleiben):
        ("CORE_SWEEP", 50.0, 3.0),
        ("TREND_PULLBACK,GOLDEN_CROSS", 41.0, 6.3),
    ]
    sc = aggregate_scorecard(rows)
    assert sc["macd_split"]["with"]["n"] == 3
    assert sc["macd_split"]["without"]["n"] == 2
    assert sc["macd_split"]["with"]["pnl_usd"] == 12.0
    assert sc["macd_split"]["without"]["pnl_usd"] == -38.0
    # MACD_TURN+TREND_PULLBACK IST Dip-Buy (MACD_TURN_Komponente in der
    # Familie): landet im "with"-Bucket, nicht draussen.
    sc_x = aggregate_scorecard(
        rows + [("MACD_TURN_BELOW_SMA20,TREND_PULLBACK", 2.0, 1.0)])
    assert sc_x["macd_split"]["with"]["n"] == 4
    assert sc_x["macd_split"]["without"]["n"] == 2
    # Reine Trend-Kombos (TREND_PULLBACK+GOLDEN_CROSS) bleiben draussen:
    sc3 = aggregate_scorecard([("TREND_PULLBACK,GOLDEN_CROSS", 1.0, 1.0)] * 5)
    assert sc3["macd_split"]["with"]["n"] == 0
    assert sc3["macd_split"]["without"]["n"] == 0
    # ohne Dip-Buy-Anteil: beide Seiten leer
    sc2 = aggregate_scorecard([("CORE_SWEEP", 1.0, 0.5),
                               ("TREND_PULLBACK,GOLDEN_CROSS", -2.0, -1.0)])
    assert sc2["macd_split"]["with"]["n"] == 0
    assert sc2["macd_split"]["without"]["n"] == 0
    # combos/components bleiben vom Scope-Filter unbeeindruckt
    by = {c["signal"]: c for c in sc["combos"]}
    assert by["CORE_SWEEP"]["n"] == 1
    assert by["TREND_PULLBACK,GOLDEN_CROSS"]["n"] == 1
    assert sc["macd_split_scope"] == "dip-buy"


def test_rules_mention_knife_and_asymmetry():
    assert "Falling Knife" in STRATEGY_RULES
    assert "verstaerken NIE" in STRATEGY_RULES
