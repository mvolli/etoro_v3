"""feat/pulse-charts: Grid-PNG der Sharp Movers."""
import numpy as np
import pandas as pd

from bot.core.candle_chart import pulse_grid_png


def _df(n=40, start=100.0):
    rng = np.random.default_rng(7)
    close = start + np.cumsum(rng.normal(0, 1.0, n))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close,
         "Volume": np.full(n, 1000.0)}
    )


def test_grid_renders_png():
    png = pulse_grid_png([("HMI.L", -11.1, _df()), ("BIRD.L", 8.3, _df())])
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_grid_caps_at_five_panels():
    movers = [(f"S{i}", 5.0 + i, _df()) for i in range(8)]
    assert pulse_grid_png(movers) is not None


def test_grid_empty_and_short_df():
    assert pulse_grid_png([]) is None
    assert pulse_grid_png([("X", 5.0, _df(3))]) is None
    assert pulse_grid_png([("X", 5.0, None)]) is None
