#!/usr/bin/env python3
"""Unit tests — fix/reconciler-live-price.

Covers _build_snapshot_record()'s price/PnL% extraction. Root cause: the
live /trading/info/real/pnl payload has NO unrealizedPnL.pnLPct/pnlPct field
(verified 2026-07-03 against all 17 open positions incl. crypto) — the old
code read a field that doesn't exist, always got None, and hardcoded
current_price to open_rate with the comment "no live price in this
endpoint." That was wrong: the endpoint's real live price lives at
unrealizedPnL.closeRate (the same field risk_worker.py already reads).
Reconciler runs at :02, two minutes after data_worker's :00 real yfinance
price write, so the placeholder overwrote a good price every cycle and
permanently flatlined unrealized_pnl_pct at 0%/blank for every position.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from bot.workers.reconciler import _build_snapshot_record


def _pos(instrument_id=1003, open_rate=562.53, close_rate=584.66, pnl=7.23,
         amount=183.87, position_id="p1", **overrides):
    base = {
        "positionID": position_id,
        "instrumentID": instrument_id,
        "openRate": open_rate,
        "amount": amount,
        "unrealizedPnL": {"pnL": pnl, "closeRate": close_rate},
        "stopLossRate": 534.41,
        "isNoStopLoss": False,
    }
    base.update(overrides)
    return base


def _map(instrument_id=1003, symbol="META"):
    return {instrument_id: symbol}


def test_current_price_uses_live_close_rate_not_open_rate():
    record, _ = _build_snapshot_record(_pos(), _map())
    assert record["current_price"] == 584.66
    assert record["current_price"] != record["open_price"]


def test_pnl_pct_derived_from_rates_matches_real_gain():
    # 584.66 / 562.53 - 1 = +3.934...%
    record, _ = _build_snapshot_record(_pos(), _map())
    assert record["unrealized_pnl_pct"] is not None
    assert round(record["unrealized_pnl_pct"], 2) == 3.93


def test_losing_position_pnl_pct_is_negative():
    # MMM-style: openRate 160.33 -> closeRate 159.79 (real 2026-07-03 sample)
    record, _ = _build_snapshot_record(
        _pos(instrument_id=1026, open_rate=160.33, close_rate=159.79, pnl=-1.67, amount=495.23),
        _map(1026, "MMM"),
    )
    assert record["unrealized_pnl_pct"] < 0
    assert round(record["unrealized_pnl_pct"], 2) == round((159.79 / 160.33 - 1) * 100, 2)


def test_crypto_position_also_gets_live_price():
    # BTC-USD sample: openRate 58307.10 -> closeRate 61914.16
    record, _ = _build_snapshot_record(
        _pos(instrument_id=100000, open_rate=58307.10, close_rate=61914.16, pnl=34.66, amount=560.21),
        _map(100000, "BTC-USD"),
    )
    assert record["current_price"] == 61914.16
    assert record["unrealized_pnl_pct"] > 0


def test_missing_close_rate_falls_back_to_open_rate():
    # Defensive: if the API ever omits closeRate, don't crash or null out —
    # fall back to the old (safe, if stale) open_rate behavior.
    pos = _pos()
    pos["unrealizedPnL"] = {"pnL": 7.23}  # no closeRate at all
    record, _ = _build_snapshot_record(pos, _map())
    assert record["current_price"] == pos["openRate"]


def test_missing_close_rate_falls_back_to_pnl_over_amount():
    pos = _pos(pnl=9.53, amount=560.4)
    pos["unrealizedPnL"] = {"pnL": 9.53}  # no closeRate
    record, _ = _build_snapshot_record(pos, _map())
    assert round(record["unrealized_pnl_pct"], 2) == round(9.53 / 560.4 * 100, 2)


def test_flat_close_rate_field_also_recognized():
    # Some endpoints might report closeRate flat on the position instead of
    # nested under unrealizedPnL — the fallback lookup must catch that too.
    pos = _pos()
    pos["unrealizedPnL"] = {"pnL": 7.23}
    pos["closeRate"] = 584.66
    record, _ = _build_snapshot_record(pos, _map())
    assert record["current_price"] == 584.66


def test_no_price_data_at_all_yields_none_pnl_pct():
    pos = {
        "positionID": "p1",
        "instrumentID": 1003,
        "openRate": None,
        "amount": None,
        "unrealizedPnL": {},
        "isNoStopLoss": False,
    }
    record, _ = _build_snapshot_record(pos, _map())
    assert record["current_price"] is None
    assert record["unrealized_pnl_pct"] is None
