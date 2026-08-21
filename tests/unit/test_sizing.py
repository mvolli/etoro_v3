#!/usr/bin/env python3
"""Unit tests — src/bot/core/sizing.py (Half-Kelly position sizing)."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock


def _make_db(rows: list[dict], st: str | None = None) -> MagicMock:
    """Creates a mock DB that returns the given rows for fetchall().

    fix/kelly-components (2026-07-26): die Query liefert jetzt auch den
    signal_type ('st') jeder Zeile — Rows ohne eigenes 'st' bekommen den
    uebergebenen Default (Tests simulieren damit den Exact-Match-Fall).
    """
    db = MagicMock()
    # sqlite3.Row-like: support both r["pnl_pct"] and r[0]
    mock_rows = []
    for r in rows:
        _r = {"st": st, **r}
        mr = MagicMock()
        mr.__getitem__ = lambda self, k, _r=_r: _r[k]
        mock_rows.append(mr)
    db.fetchall.return_value = mock_rows
    return db


from bot.core.sizing import kelly_size_factor


class TestKellySizeFactor:
    """Half-Kelly factor edge-case tests."""

    def test_insufficient_data_returns_neutral(self):
        """Fewer than min_trades → 1.0 (no change to sizing)."""
        db = _make_db([{"pnl_pct": 1.5}, {"pnl_pct": -0.5}], st="BB_LOWER_RSI_OVERSOLD")
        assert kelly_size_factor("BB_LOWER_RSI_OVERSOLD", db, min_trades=10) == 1.0

    def test_all_winners_returns_max(self):
        """All profitable trades → 1.5 (max factor)."""
        rows = [{"pnl_pct": 2.0 + i * 0.1} for i in range(15)]
        db = _make_db(rows, st="GOLDEN_CROSS")
        assert kelly_size_factor("GOLDEN_CROSS", db, min_trades=10) == 1.5

    def test_all_losers_returns_min(self):
        """All losing trades → 0.5 (min factor, never fully suppressed).

        fix/kelly-centering: floor moved 0.3 → 0.5, so all-losers no longer
        means a 30x shrinkage on top of every other dampener."""
        rows = [{"pnl_pct": -(1.0 + i * 0.1)} for i in range(15)]
        db = _make_db(rows, st="TREND_PULLBACK")
        assert kelly_size_factor("TREND_PULLBACK", db, min_trades=10) == 0.5

    def test_negative_avg_pnl_clamped_to_min(self):
        """Strongly negative Kelly value is clamped to the 0.5 floor."""
        # 30% win rate, avg_win=1% vs avg_loss=5% → f = 0.3 - 0.7/0.2 = -3.2
        wins = [{"pnl_pct": 1.0}] * 3
        losses = [{"pnl_pct": -5.0}] * 7
        db = _make_db(wins + losses, st="BAD_SIGNAL")
        factor = kelly_size_factor("BAD_SIGNAL", db, min_trades=5)
        assert factor == pytest.approx(0.5)
        assert factor >= 0.5

    def test_good_edge_boosts_size(self):
        """Strong edge (70% win, avg_win=2%, avg_loss=1%) → factor > 1.0."""
        wins = [{"pnl_pct": 2.0}] * 7
        losses = [{"pnl_pct": -1.0}] * 3
        db = _make_db(wins + losses, st="BB_EXTREME_RSI_OVERSOLD")
        factor = kelly_size_factor("BB_EXTREME_RSI_OVERSOLD", db, min_trades=5)
        # win=7/10=0.7, avg_win=2, avg_loss=1 → f = 0.7 - 0.3/2 = 0.55
        # centered: 1 + 2*(0.5*0.55) = 1.55 → capped at 1.5
        assert factor == pytest.approx(1.5)

    def test_excellent_edge_near_cap(self):
        """Excellent edge approaches but stays ≤ 1.5."""
        # 80% win rate, avg_win=4%, avg_loss=1%
        wins = [{"pnl_pct": 4.0}] * 8
        losses = [{"pnl_pct": -1.0}] * 2
        db = _make_db(wins + losses, st="RSI_EXTREME_OVERSOLD")
        factor = kelly_size_factor("RSI_EXTREME_OVERSOLD", db, min_trades=5)
        # f = 0.8 - 0.2/(4/1) = 0.75, half = 0.375 → centered
        # 1 + 2*0.375 = 1.75 → capped at 1.5
        assert 0.5 <= factor <= 1.5
        assert factor == pytest.approx(1.5, abs=0.01)

    def test_db_error_returns_neutral(self):
        """DB exception → graceful fallback to 1.0."""
        db = MagicMock()
        db.fetchall.side_effect = Exception("DB locked")
        assert kelly_size_factor("ANY_SIGNAL", db) == 1.0

    def test_result_always_in_range(self):
        """Output is always in [0.5, 1.5] regardless of input distribution."""
        import random
        random.seed(42)
        for _ in range(50):
            rows = [{"pnl_pct": random.gauss(0.5, 3.0)} for _ in range(20)]
            db = _make_db(rows, st="ANY")
            f = kelly_size_factor("ANY", db, min_trades=5)
            assert 0.5 <= f <= 1.5, f"factor {f} out of [0.5, 1.5]"
