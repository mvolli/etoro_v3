"""
Unit tests for the MA200 trend gate (feat/ma200-filter, 2026-09-29, advisor-frozen).

Covers:
- ma200_decision: tuple return (blocked, detail), fail-open on insufficient
  history, correct below/above-MA classification, decision bar = last provided
  close (lag=0, no look-ahead).
- evaluate() Gate #7: SHADOW mode records a would-be block (size_mult 0.0 in
  the ledger) WITHOUT touching live_effective_mult (execution invariant);
  LIVE mode applies the configured 0.25x soft-gate.
- ma200_history: ensure_table idempotency, upsert idempotency (PK
  symbol+date), get_daily_closes ordering (oldest -> newest),
  df_to_closed_pairs drops today's in-progress bar.

No live-DB writes: all tests use a temp-file DB via the bot DB wrapper.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot.core import entry_quality
from bot.core import ma200_history
from bot.db.connection import DB


def _temp_db(tmp_path) -> DB:
    return DB(str(tmp_path / "test.db"), busy_timeout_ms=2000)


def _cfg(gate_mode: str = "shadow") -> dict:
    """Minimal config: EQ enabled, ma200 gate enabled in ``gate_mode``."""
    return {
        "trading": {
            "entry_quality": {
                "gates": {
                    "ma200_trend": {"mode": gate_mode},
                },
            }
        }
    }


def _closes_below() -> list[float]:
    return [100.0] * 199 + [10.0]  # last close far below the trailing SMA


# ── ma200_decision ───────────────────────────────────────────────────────────

def test_decision_insufficient_fails_open():
    # 199 closes < window 200 -> (False, insufficient)
    blocked, detail = entry_quality.ma200_decision([1.0] * 199, window=200, lag=0)
    assert blocked is False
    assert detail["status"] == "insufficient"
    assert entry_quality.ma200_decision(None, window=200, lag=0)[0] is False
    assert entry_quality.ma200_decision([], window=200, lag=0)[0] is False


def test_decision_above_ma():
    blocked, detail = entry_quality.ma200_decision([100.0] * 200, window=200, lag=0)
    assert blocked is False
    assert detail["status"] == "ok"
    assert detail["sma200"] == pytest.approx(100.0)


def test_decision_below_ma():
    blocked, detail = entry_quality.ma200_decision(_closes_below(), window=200, lag=0)
    assert blocked is True
    assert detail["sma200"] == pytest.approx((199 * 100.0 + 10.0) / 200.0)
    assert detail["close"] == 10.0


def test_decision_no_lookahead_lag0():
    # lag=0: decision index = last provided close. Adding bars BEFORE the
    # decision window shifts nothing about what bar is decided on.
    closes = _closes_below()
    _, detail = entry_quality.ma200_decision(closes, window=200, lag=0)
    assert detail["close"] == closes[-1]
    # Same decision with a longer prefix (window still the last 200):
    longer = [50.0] * 50 + closes
    _, detail2 = entry_quality.ma200_decision(longer, window=200, lag=0)
    assert detail2["close"] == closes[-1]
    assert detail2["sma200"] == pytest.approx(detail["sma200"])


# ── evaluate() shadow invariant ──────────────────────────────────────────────

def test_evaluate_shadow_hit_keeps_execution_untouched(tmp_path):
    db = _temp_db(tmp_path)
    entry_quality.ensure_table(db)
    ev = entry_quality.evaluate(
        _cfg("shadow"), symbol="TEST", signal_type="TREND_PULLBACK",
        indicators={}, regime="NORMAL", daily_closes=_closes_below(),
    )
    assert ev.ma200_shadow is True
    assert ev.ma200_live is False
    assert ev.blocked is True            # ledger: would-be block (size_mult 0.0)
    assert ev.live_effective_mult == 1.0  # execution: UNTOUCHED (the invariant)
    entry_quality.record(db, ev, mode="shadow", applied=False,
                         signal_id=1001, instrument_id=None,
                         entry_price=123.45)
    row = db.execute(
        "SELECT mode, size_mult, blocked, entry_price, hits FROM entry_quality_events "
        "WHERE signal_id=1001"
    ).fetchone()
    assert row is not None
    assert row["mode"] == "shadow"
    # size_mult column = EXECUTION-effective mult (record() stores
    # ev.live_effective_mult) -> 1.0 for a shadow hit. The would-block
    # (0.0) is preserved in the hits JSON; blocked=1 is the marker.
    assert row["size_mult"] == 1.0
    assert row["blocked"] == 1
    assert row["entry_price"] == 123.45
    assert "ma200_trend" in row["hits"]
    assert entry_quality.latest_size_mult(db, 1001) == 1.0


def test_evaluate_pass_no_hit(tmp_path):
    ev = entry_quality.evaluate(
        _cfg("shadow"), symbol="TEST", signal_type="TREND_PULLBACK",
        indicators={}, regime="NORMAL", daily_closes=[100.0] * 200,
    )
    assert not ev.ma200_shadow
    assert not ev.ma200_live
    assert not ev.blocked
    assert ev.size_mult == 1.0
    assert ev.live_effective_mult == 1.0


def test_evaluate_live_mode_applies_soft_gate(tmp_path):
    ev = entry_quality.evaluate(
        _cfg("live"), symbol="TEST", signal_type="TREND_PULLBACK",
        indicators={}, regime="NORMAL", daily_closes=_closes_below(),
    )
    assert ev.ma200_live is True
    assert ev.ma200_shadow is False
    assert ev.size_mult == pytest.approx(0.25)
    assert ev.live_effective_mult == pytest.approx(0.25)


def test_evaluate_insufficient_fails_open(tmp_path):
    ev = entry_quality.evaluate(
        _cfg("shadow"), symbol="TEST", signal_type="TREND_PULLBACK",
        indicators={}, regime="NORMAL", daily_closes=[1.0] * 50,
    )
    assert not ev.ma200_shadow
    assert ev.ma200_detail["status"] == "insufficient"
    assert ev.live_effective_mult == 1.0
    ev_none = entry_quality.evaluate(
        _cfg("shadow"), symbol="TEST", signal_type="TREND_PULLBACK",
        indicators={}, regime="NORMAL", daily_closes=None,
    )
    assert not ev_none.ma200_shadow
    assert ev_none.live_effective_mult == 1.0


def test_evaluate_disabled_no_hit(tmp_path):
    cfg = {"trading": {"entry_quality": {"enabled": False}}}
    ev = entry_quality.evaluate(
        cfg, symbol="TEST", signal_type="TREND_PULLBACK",
        indicators={}, regime="NORMAL", daily_closes=_closes_below(),
    )
    assert not ev.ma200_shadow and not ev.ma200_live
    assert ev.live_effective_mult == 1.0


# ── ma200_history ────────────────────────────────────────────────────────────

def test_ensure_table_idempotent(tmp_path):
    db = _temp_db(tmp_path)
    ma200_history.ensure_table(db)
    ma200_history.ensure_table(db)  # must not raise
    assert db.fetchone("SELECT COUNT(*) AS c FROM ma200_daily")["c"] == 0


def test_upsert_idempotent_and_ordered(tmp_path):
    db = _temp_db(tmp_path)
    ma200_history.ensure_table(db)
    pairs = [(f"2026-01-{d:02d}", 100.0 + d) for d in range(1, 21)]
    ma200_history.upsert_closes(db, "TEST", pairs)
    ma200_history.upsert_closes(db, "TEST", pairs)  # no-op (PK symbol+date)
    closes = ma200_history.get_daily_closes(db, "TEST", limit=400)
    assert len(closes) == 20
    assert closes == sorted(closes)  # oldest -> newest
    assert closes[0] == 101.0 and closes[-1] == 120.0


def test_upsert_empty_returns_zero(tmp_path):
    db = _temp_db(tmp_path)
    ma200_history.ensure_table(db)
    assert ma200_history.upsert_closes(db, "TEST", []) == 0
    assert ma200_history.upsert_closes(db, "", [("2026-01-01", 1.0)]) == 0


def test_get_daily_closes_missing_symbol_empty(tmp_path):
    db = _temp_db(tmp_path)
    ma200_history.ensure_table(db)
    assert ma200_history.get_daily_closes(db, "NOSUCH") == []


def test_df_to_closed_pairs_drops_today():
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0]},
        index=pd.to_datetime(["2026-09-27", "2026-09-28", "2026-09-29"]),
    )
    pairs = ma200_history.df_to_closed_pairs(df, today_str="2026-09-29")
    assert pairs == [("2026-09-27", 100.0), ("2026-09-28", 101.0)]
    assert ma200_history.df_to_closed_pairs(None) == []
