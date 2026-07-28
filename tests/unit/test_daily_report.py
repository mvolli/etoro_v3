#!/usr/bin/env python3
"""Unit tests — feat/daily-report: pure Aggregation des Tagesreports."""
from __future__ import annotations

import pytest

from bot.workers.daily_report_worker import build_report_data


def _ev(eid, etype, symbol, pnl_usd=None, pnl_pct=None, close_pct=None,
        price=None, amount=100.0, at="2026-07-28 12:00:00", reason=None):
    return {"id": eid, "event_type": etype, "symbol": symbol,
            "pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "close_pct": close_pct,
            "price": price, "amount_usd": amount, "event_at": at,
            "reason": reason, "instrument_id": 1, "position_id": str(eid)}


def test_empty_day():
    data = build_report_data([], [], [])
    assert data["realized_pnl_usd"] is None
    assert data["wins"] == 0 and data["losses"] == 0
    assert data["has_activity"] is False
    assert data["sections"] == []


def test_full_day_aggregation():
    events = [
        _ev(1, "OPEN", "AAPL", price=324.28, at="2026-07-28 09:00:00"),
        _ev(2, "PARTIAL_CLOSE", "COA.L", pnl_usd=4.2, pnl_pct=5.1, close_pct=25.0),
        _ev(3, "CLOSE", "MSI", pnl_usd=-4.79, pnl_pct=-3.2, reason="SL"),
        _ev(4, "CLOSE", "HLAG.DE", pnl_usd=41.23, pnl_pct=6.4),
        _ev(5, "CLOSE", "XYZ", pnl_usd=None),      # P/L folgt
    ]
    filled = [
        # Event von gestern, heute nachgetragen → "P/L nachgetragen"
        _ev(99, "CLOSE", "NRC.OL", pnl_usd=-18.46, pnl_pct=-2.8,
            at="2026-07-18 10:00:00"),
        # Event 4 wurde heute auch finalisiert — darf nicht doppelt erscheinen
        _ev(4, "CLOSE", "HLAG.DE", pnl_usd=41.23, pnl_pct=6.4),
    ]
    snaps = [
        {"symbol": "AAPL", "amount_usd": 137.58, "unrealized_pnl": 6.35,
         "unrealized_pnl_pct": 4.6},
        {"symbol": "ROVI.MC", "amount_usd": 88.67, "unrealized_pnl": -2.0,
         "unrealized_pnl_pct": -2.2},
    ]

    data = build_report_data(events, filled, snaps)
    assert data["realized_pnl_usd"] == pytest.approx(4.2 - 4.79 + 41.23)
    assert data["wins"] == 1 and data["losses"] == 1   # nur Closes mit PnL
    assert data["unconfirmed"] == 1                     # XYZ
    assert data["open_count"] == 2
    assert data["open_exposure_usd"] == pytest.approx(226.25)
    assert data["unrealized_pnl_usd"] == pytest.approx(4.35)
    assert data["has_activity"] is True

    names = [name for name, _ in data["sections"]]
    assert names == ["🟢 Eröffnungen (1)", "✂️ Teilverkäufe (1)",
                     "🏁 Closes (3)", "📋 P/L nachgetragen (1)", "💼 Portfolio"]
    late_lines = dict(data["sections"])["📋 P/L nachgetragen (1)"]
    assert "NRC.OL" in late_lines[0] and len(late_lines) == 1
    close_lines = dict(data["sections"])["🏁 Closes (3)"]
    assert any("P/L folgt" in line for line in close_lines)
    pf = dict(data["sections"])["💼 Portfolio"]
    assert "AAPL" in pf[0] and "ROVI.MC" in pf[0]
