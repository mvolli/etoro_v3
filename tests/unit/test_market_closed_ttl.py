"""fix/market-closed-ttl: Zeit-Cap fuer haengende APPROVED-Trades."""
from datetime import datetime, timezone, timedelta

from bot.workers.execution_worker import market_closed_too_old


def _t(hours_ago):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {"approved_at": ts}


def test_young_trade_not_expired():
    assert market_closed_too_old(_t(1.0), 4.0) is False


def test_old_trade_expired():
    assert market_closed_too_old(_t(5.0), 4.0) is True


def test_boundary():
    assert market_closed_too_old(_t(3.9), 4.0) is False
    assert market_closed_too_old(_t(4.1), 4.0) is True


def test_missing_and_broken_timestamp_failsafe():
    assert market_closed_too_old({}, 4.0) is False
    assert market_closed_too_old({"approved_at": "kaputt"}, 4.0) is False


def test_created_at_fallback():
    ts = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    assert market_closed_too_old({"created_at": ts}, 4.0) is True

def test_created_at_is_primary_anchor():
    """created_at (Signalalter) hat Vorrang vor approved_at — ein alt
    erstellter, spaet approvter Trade gilt als veraltet (BTC #472)."""
    now = datetime.now(timezone.utc)
    trade = {
        "created_at": (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
        "approved_at": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    assert market_closed_too_old(trade, 4.0) is True

