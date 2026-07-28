#!/usr/bin/env python3
"""Unit tests — feat/pnl-nachreport: History-Index + Matcher (pnl_backfill).

Die eToro-History hat EINE Row pro Partial Close — by_position haelt Listen,
die letzte Row ist der finale Full-Close. pnl_pct ist None (nicht 0), wenn
investment fehlt.
"""
from __future__ import annotations

import pytest

from bot.core.pnl_backfill import (
    HistoryIndex,
    fetch_history_index,
    match_close,
    match_partial,
    pnl_from_row,
)


def _row(pos, order, close_ts, net=1.0, invest=100.0, units=10.0,
         open_rate=10.0, close_rate=10.5):
    return {
        "positionId": pos, "orderId": order,
        "closeTimestamp": close_ts, "openTimestamp": "2026-07-20T10:00:00Z",
        "netProfit": net, "investment": invest, "units": units,
        "openRate": open_rate, "closeRate": close_rate, "isBuy": True,
    }


# Position 111: zwei Partials + Full Close; Position 222: einfacher Close
ROWS = [
    _row(111, 9001, "2026-07-22T10:00:00Z", net=2.0, invest=50.0, units=5.0),
    _row(111, 9001, "2026-07-24T15:00:00Z", net=3.5, invest=40.0, units=4.0),
    _row(111, 9001, "2026-07-26T09:00:00Z", net=-1.0, invest=30.0, units=3.0),
    _row(222, 9002, "2026-07-25T12:00:00Z", net=7.7, invest=200.0, units=20.0),
]


class FakeClient:
    def __init__(self, rows, page_size_served=100):
        self.rows = rows
        self.calls = []

    def get_trade_history(self, min_date=None, page=1, page_size=100):
        self.calls.append((min_date, page, page_size))
        start = (page - 1) * page_size
        return self.rows[start:start + page_size]


@pytest.fixture
def index():
    idx = HistoryIndex()
    # absichtlich unsortiert einfuegen
    for row in [ROWS[1], ROWS[3], ROWS[0], ROWS[2]]:
        idx.add(row)
    idx.sort()
    return idx


def test_fetch_paginates_until_short_page():
    client = FakeClient(ROWS)
    idx = fetch_history_index(client, "2026-07-01", page_size=3)
    assert idx.row_count == 4
    assert [c[1] for c in client.calls] == [1, 2]   # Seite 2 war kurz → Stop
    assert len(idx.by_position[111]) == 3


def test_fetch_survives_api_error():
    class Boom:
        def get_trade_history(self, **k):
            raise RuntimeError("500")

    idx = fetch_history_index(Boom(), None)
    assert idx.row_count == 0


def test_match_close_returns_last_row(index):
    row = match_close(index, 111)
    assert row["closeTimestamp"] == "2026-07-26T09:00:00Z"
    assert pnl_from_row(row)["pnl_usd"] == pytest.approx(-1.0)


def test_match_close_falls_back_to_order_id(index):
    assert match_close(index, None, order_id=9002)["positionId"] == 222
    assert match_close(index, "999", order_id="9002")["positionId"] == 222
    assert match_close(index, None, None) is None
    assert match_close(index, "abc", "xyz") is None


def test_match_partial_by_time(index):
    row = match_partial(index, 111, "2026-07-24 15:10:00")
    assert row["closeTimestamp"] == "2026-07-24T15:00:00Z"
    # ausserhalb Toleranz
    assert match_partial(index, 111, "2026-07-24 18:00:00") is None
    assert match_partial(index, 111, None) is None
    assert match_partial(index, 999, "2026-07-24 15:00:00") is None


def test_match_partial_units_tiebreak():
    idx = HistoryIndex()
    # zwei Partials im 10-Minuten-Abstand — Zeit allein ist ambivalent
    idx.add(_row(5, 1, "2026-07-24T15:00:00Z", units=5.0, net=1.0))
    idx.add(_row(5, 1, "2026-07-24T15:10:00Z", units=20.0, net=2.0))
    idx.sort()
    row = match_partial(idx, 5, "2026-07-24T15:05:00Z", units=19.5)
    assert row["units"] == 20.0


def test_pnl_from_row_missing_investment_gives_none_pct():
    row = _row(1, 1, "2026-07-24T15:00:00Z", net=5.0, invest=None)
    row["initialInvestment"] = None
    nums = pnl_from_row(row)
    assert nums["pnl_usd"] == pytest.approx(5.0)
    assert nums["pnl_pct"] is None        # NICHT 0.0 (Alt-Bug in 9d)


def test_pnl_from_row_pct():
    nums = pnl_from_row(ROWS[3])
    assert nums["pnl_pct"] == pytest.approx(3.85)
    assert nums["entry"] == pytest.approx(10.0)
    assert nums["exit"] == pytest.approx(10.5)
    assert nums["units"] == pytest.approx(20.0)
