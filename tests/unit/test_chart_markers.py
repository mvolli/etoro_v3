#!/usr/bin/env python3
"""Unit tests — feat/trade-event-marker: Zeitachsen-Marker in Candle-Charts.

Kontrakt bleibt: bytes oder None, wirft nie — auch bei Garbage-Events.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.core.candle_chart import (
    _event_index,
    _parse_ts,
    _stagger_y,
    daily_grid_png,
    render_candles_png,
    trade_story_png,
    trade_story_png_v2,
)


def _mk_candles(n: int = 24, start: str = "2026-07-27T00:00:00Z",
                base: float = 100.0) -> list[dict]:
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    out = []
    for i in range(n):
        px = base + i * 0.5
        out.append({
            "fromDate": (t0 + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
            "open": px, "high": px + 1.0, "low": px - 1.0, "close": px + 0.4,
            "volume": 1000,
        })
    return out


EVENTS = [
    {"ts": "2026-07-27T03:00:00Z", "type": "ENTRY", "price": 101.5,
     "label": "Entry 101.50"},
    {"ts": "2026-07-27T10:00:00Z", "type": "PARTIAL_CLOSE", "price": 105.0,
     "label": "-25% @105.00"},
    {"ts": "2026-07-27T20:00:00Z", "type": "EXIT", "price": 110.0,
     "label": "Exit 110.00 +8.4%"},
]


def test_render_with_events_returns_png():
    png = render_candles_png(_mk_candles(), title="T", events=EVENTS)
    assert isinstance(png, bytes) and png[:4] == b"\x89PNG"


def test_render_without_events_unchanged():
    png = render_candles_png(_mk_candles(), title="T", entry=101.0, sl=98.0)
    assert isinstance(png, bytes)


def test_garbage_events_never_raise():
    garbage = [
        {"ts": "not-a-date", "type": "ENTRY", "price": "NaN?"},
        {"type": "UNKNOWN_TYPE", "price": 100.0},
        {"ts": None, "type": "EXIT", "price": None},
        "not-even-a-dict-key-access-would-fail" and {},
    ]
    png = render_candles_png(_mk_candles(), events=garbage)
    assert png is None or isinstance(png, bytes)


def test_event_index_maps_and_clamps():
    times = [datetime(2026, 7, 27, h, tzinfo=timezone.utc) for h in range(10)]
    at = lambda h: datetime(2026, 7, 27, h, tzinfo=timezone.utc)
    assert _event_index(at(3), times, 10) == 3
    # vor dem Fenster → Index 0
    assert _event_index(datetime(2026, 7, 20, tzinfo=timezone.utc), times, 10) == 0
    # nach dem Fenster → letzter Index
    assert _event_index(datetime(2026, 7, 30, tzinfo=timezone.utc), times, 10) == 9
    # ohne Timestamp → letzter Index
    assert _event_index(None, times, 10) == 9


def test_parse_ts_variants():
    assert _parse_ts("2026-07-27T03:00:00Z") is not None
    assert _parse_ts("2026-07-27 03:00:00") is not None
    assert _parse_ts("garbage") is None
    assert _parse_ts(None) is None


def test_stagger_separates_close_labels():
    items = [{"price": 100.0}, {"price": 100.1}, {"price": 100.2}]
    _stagger_y(items, price_range=10.0, min_gap_pct=0.07)
    ys = sorted(i["y_text"] for i in items)
    assert ys[1] - ys[0] >= 0.69 and ys[2] - ys[1] >= 0.69


class FakeClient:
    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    def get_candles(self, instrument_id, interval, count):
        self.calls.append((instrument_id, interval, count))
        return self.candles


def test_trade_story_v2_bumps_count_to_cover_entry():
    client = FakeClient(_mk_candles(80))
    opened = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    png = trade_story_png_v2(client, 1001, "AAPL",
                             events=EVENTS, opened_at=opened)
    assert isinstance(png, bytes)
    _, interval, count = client.calls[0]
    assert interval == "OneHour"
    assert count >= 48          # 2 Tage × 24h muessen abgedeckt sein


def test_trade_story_wrapper_builds_events():
    client = FakeClient(_mk_candles(80))
    png = trade_story_png(client, 1001, "AAPL", entry=101.0,
                          exit_price=108.0, opened_at="2026-07-27T00:00:00Z")
    assert isinstance(png, bytes)


def test_trade_story_v2_fail_open():
    assert trade_story_png_v2(None, None, "X", events=EVENTS) is None

    class Boom:
        def get_candles(self, *a):
            raise RuntimeError("api down")

    assert trade_story_png_v2(Boom(), 1, "X", events=EVENTS) is None


def test_daily_grid_png():
    stories = [
        {"title": "AAPL +2.1%", "up": True,
         "candles": _mk_candles(40), "events": EVENTS},
        {"title": "MSI -1.2%", "up": False,
         "candles": _mk_candles(40, base=50), "events": []},
        {"title": "leer", "up": True, "candles": [], "events": []},  # skipped
    ]
    png = daily_grid_png(stories)
    assert isinstance(png, bytes) and png[:4] == b"\x89PNG"
    assert daily_grid_png([]) is None
    assert daily_grid_png(None) is None
