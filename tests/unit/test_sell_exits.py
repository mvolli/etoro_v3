#!/usr/bin/env python3
"""Unit tests — fix/sell-signal-exits.

FRESH SELL/OVERBOUGHT-Signale auf gehaltene, profitable Positionen
produzieren genau einen Partial-Close pro Instrument; Verlust-Positionen
und Instrumente ohne Position werden ignoriert; Ausführung markiert das
Signal CONSUMED sobald die Order akzeptiert ist.
"""
from __future__ import annotations

import pytest

from bot.core.sell_exits import (
    SELL_EXIT_CLOSE_PCT,
    evaluate_sell_exits,
    execute_sell_exits,
    is_sell_signal,
)


def _signal(sig_id=1, iid=42, sig_type="BB_UPPER_RSI_OVERBOUGHT"):
    return {"id": sig_id, "instrument_id": iid, "signal_type": sig_type, "conviction": "HIGH"}


def _pos(pos_id="p1", iid=42, symbol="NVDA", amount=1000.0, pnl_pct=12.0):
    return {
        "positionID": pos_id,
        "instrumentID": iid,
        "symbol": symbol,
        "amount": amount,
        "openRate": 100.0,
        "unrealizedPnL": {"pnL": amount * pnl_pct / 100.0},
    }


def test_is_sell_signal():
    assert is_sell_signal(_signal(sig_type="BB_UPPER_RSI_OVERBOUGHT"))
    assert is_sell_signal(_signal(sig_type="SELL"))
    assert not is_sell_signal(_signal(sig_type="RSI_EXTREME_OVERSOLD"))
    assert not is_sell_signal({"signal_type": None})


def test_sell_signal_on_profitable_position():
    actions = evaluate_sell_exits([_signal()], [_pos(pnl_pct=12.0)])
    assert len(actions) == 1
    assert actions[0].close_pct == SELL_EXIT_CLOSE_PCT
    assert actions[0].signal_id == 1
    assert actions[0].position_id == "p1"


def test_losing_position_is_not_sold():
    actions = evaluate_sell_exits([_signal()], [_pos(pnl_pct=-2.0)])
    assert actions == []


def test_no_position_no_action():
    actions = evaluate_sell_exits([_signal(iid=99)], [_pos(iid=42)])
    assert actions == []


def test_buy_signals_are_ignored():
    actions = evaluate_sell_exits(
        [_signal(sig_type="RSI_EXTREME_OVERSOLD")], [_pos(pnl_pct=12.0)]
    )
    assert actions == []


def test_largest_profitable_fragment_wins():
    positions = [
        _pos(pos_id="small", amount=200.0, pnl_pct=8.0),
        _pos(pos_id="big", amount=900.0, pnl_pct=6.0),
        _pos(pos_id="losing", amount=2000.0, pnl_pct=-1.0),
    ]
    actions = evaluate_sell_exits([_signal()], positions)
    assert len(actions) == 1
    assert actions[0].position_id == "big"


def test_one_action_per_instrument():
    # Zwei SELL-Signale fürs gleiche Instrument → nur eine Aktion
    signals = [_signal(sig_id=1), _signal(sig_id=2)]
    actions = evaluate_sell_exits(signals, [_pos(pnl_pct=10.0)])
    assert len(actions) == 1


class FakeClient:
    def __init__(self):
        self.closed = []

    def close_position(self, position_id, instrument_id, units_to_deduct=None):
        self.closed.append((position_id, units_to_deduct))
        return {"orderId": "ok"}


class FakeSignalRepo:
    def __init__(self):
        self.status_updates = []

    def update_signal_status(self, signal_id, status):
        self.status_updates.append((signal_id, status))


def test_execute_consumes_signal_and_closes(monkeypatch):
    import bot.core.sell_exits as se
    import bot.core.trailing_stop as ts
    monkeypatch.setattr(ts, "_verify_partial_close", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(ts, "_post_closed_embed", lambda *a, **k: None)

    client, repo = FakeClient(), FakeSignalRepo()
    actions = evaluate_sell_exits([_signal()], [_pos(pnl_pct=12.0)])
    stats = execute_sell_exits(client, repo, actions)

    assert stats["closed"] == 1
    assert len(client.closed) == 1
    # 50% von 1000$/100 = 10 units → 5 units
    assert client.closed[0][1] == pytest.approx(5.0)
    assert (1, "CONSUMED") in repo.status_updates


def test_execute_dry_run_touches_nothing():
    client, repo = FakeClient(), FakeSignalRepo()
    actions = evaluate_sell_exits([_signal()], [_pos(pnl_pct=12.0)])
    stats = execute_sell_exits(client, repo, actions, dry_run=True)
    assert stats["closed"] == 1
    assert client.closed == []
    assert repo.status_updates == []


def test_api_error_keeps_signal_fresh(monkeypatch):
    import bot.core.trailing_stop as ts
    monkeypatch.setattr(ts, "_post_closed_embed", lambda *a, **k: None)

    class FailingClient:
        def close_position(self, **kw):
            raise RuntimeError("API down")

    repo = FakeSignalRepo()
    actions = evaluate_sell_exits([_signal()], [_pos(pnl_pct=12.0)])
    stats = execute_sell_exits(FailingClient(), repo, actions)
    assert stats["closed"] == 0
    assert len(stats["errors"]) == 1
    assert repo.status_updates == []  # Signal bleibt FRESH → Retry


@__import__("pytest").fixture(autouse=True)
def _market_always_open_for_tests(monkeypatch):
    """fix/stale-price-trailing machte diese Tests wallclock-abhaengig
    (Market-Guard prueft echte Boersenzeiten — gruen mittags, rot abends).
    Der Guard hat eigene Tests (test_llm_advisors.py); hier wird er
    deterministisch auf 'offen' gepatcht."""
    import bot.core.trailing_stop as _ts
    monkeypatch.setattr(_ts, "_action_market_open", lambda *a, **k: True)


# ── feat/min-remaining (50%-Regel, 2026-08-27) ──────────────────────────────
# Ein Teilverkauf, der die Position (nach vorherigen Teilverkaufen) unter
# 50% der Oeffnungssumme druecken wuerde, wird zu einem Full Close. Die
# Restmenge (position_state.remaining_frac) wird nach jedem verifizierten
# Teilverkauf fortgeschrieben, damit Ladder-/Fade-/Sell-Exit-Logik gegen
# den echten Stand prueft — und Earnings-Exits (execute_sell_exits mit db)
# dieselbe Untergrenze respektieren.


def _fake_db(remaining_frac: float | None = None):
    """In-memory position_state (echtes SQLite, Bot-DB-Interface)."""
    import sqlite3

    import bot.core.trailing_stop as ts

    class FakeDB:
        def __init__(self):
            self._c = sqlite3.connect(":memory:")

        def execute(self, sql, params=None):
            return self._c.execute(sql, params or [])

        def fetchall(self, sql, params=None):
            return self._c.execute(sql, params or []).fetchall()

        def fetchone(self, sql, params=None):
            return self._c.execute(sql, params or []).fetchone()

        def commit(self):
            self._c.commit()

    db = FakeDB()
    ts._ensure_position_state_table(db)
    if remaining_frac is not None:
        db.execute(
            "INSERT INTO position_state (position_id, symbol, remaining_frac, "
            "updated_at) VALUES (?, ?, ?, datetime('now'))",
            ("p1", "NVDA", remaining_frac),
        )
    return db


def test_sell_exit_breach_forces_full_close(monkeypatch):
    """Rest 50% + 50% Partial wuerde 25% lassen → Full Close statt Partial."""
    import bot.core.trailing_stop as ts
    monkeypatch.setattr(ts, "verify_full_close",
                        lambda *a, **k: (True, "confirmed", None))
    monkeypatch.setattr(ts, "_post_closed_embed", lambda *a, **k: None)

    db = _fake_db(remaining_frac=0.5)
    client, repo = FakeClient(), FakeSignalRepo()
    actions = evaluate_sell_exits([_signal()], [_pos(pnl_pct=12.0)])
    stats = execute_sell_exits(client, repo, actions, db=db)

    assert stats["closed"] == 1
    assert stats["full_closes"] == 1
    assert len(client.closed) == 1
    assert client.closed[0][1] is None  # Full Close: UnitsToDeduct fehlt
    assert (1, "CONSUMED") in repo.status_updates


def test_sell_exit_partial_updates_remaining_frac(monkeypatch):
    """Frische Position: 50% Partial bleibt Partial und setzt Rest auf 0.5."""
    import bot.core.trailing_stop as ts
    monkeypatch.setattr(ts, "_verify_partial_close", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(ts, "_post_closed_embed", lambda *a, **k: None)

    db = _fake_db()
    client, repo = FakeClient(), FakeSignalRepo()
    actions = evaluate_sell_exits([_signal()], [_pos(pnl_pct=12.0)])
    stats = execute_sell_exits(client, repo, actions, db=db)

    assert stats["closed"] == 1
    assert stats["full_closes"] == 0
    assert client.closed[0][1] == pytest.approx(5.0)
    rows = db.fetchall(
        "SELECT remaining_frac FROM position_state WHERE position_id = 'p1'"
    )
    assert rows[0][0] == pytest.approx(0.5)


def test_second_partial_is_full_close(monkeypatch):
    """Stacking: 1x 50% Partial, danach next 50% Partial → Full Close."""
    import bot.core.trailing_stop as ts
    monkeypatch.setattr(ts, "_verify_partial_close", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(ts, "verify_full_close",
                        lambda *a, **k: (True, "confirmed", None))
    monkeypatch.setattr(ts, "_post_closed_embed", lambda *a, **k: None)

    db = _fake_db()
    client, repo = FakeClient(), FakeSignalRepo()
    first = evaluate_sell_exits([_signal(sig_id=1)], [_pos(pnl_pct=12.0)])
    execute_sell_exits(client, repo, first, db=db)
    second = evaluate_sell_exits([_signal(sig_id=2)], [_pos(pnl_pct=12.0)])
    stats = execute_sell_exits(client, repo, second, db=db)

    assert stats["closed"] == 1
    assert stats["full_closes"] == 1
    assert client.closed[-1][1] is None


def test_sell_exit_dry_run_breach_counts_full_close():
    """Dry-Run: kein API-Call, aber Full-Close-Eskalation wird gezahlt."""
    db = _fake_db(remaining_frac=0.5)
    client, repo = FakeClient(), FakeSignalRepo()
    actions = evaluate_sell_exits([_signal()], [_pos(pnl_pct=12.0)])
    stats = execute_sell_exits(client, repo, actions, dry_run=True, db=db)

    assert stats["closed"] == 1
    assert stats["full_closes"] == 1
    assert client.closed == []
    assert repo.status_updates == []
