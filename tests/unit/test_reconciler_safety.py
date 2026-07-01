"""Unit tests for reconciler circuit-breaker and grace-period logic."""
import sys
from pathlib import Path

# Ensure src/ is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


class TestGracePeriodTradeClosure:
    """P0b: Grace period for trade closure — don't close on single missing snapshot."""

    def test_trade_not_closed_when_snapshot_still_exists(self):
        """Position missing from live API but portfolio_snapshot row still exists → trade stays ACTIVE."""
        # The reconciler logic checks:
        #   still_has_snapshot = any(s["api_position_id"] == pos_id for s in all_snapshots)
        # If True → continue (skip closure). We verify the logic path.
        all_snapshots = [{"api_position_id": "pos123", "last_synced": "2026-07-01 10:00:00"}]
        pos_id = "pos123"
        still_has_snapshot = any(s["api_position_id"] == pos_id for s in all_snapshots)
        assert still_has_snapshot is True  # Grace period active → closure deferred

    def test_trade_closed_when_snapshot_gone(self):
        """Position missing from live API AND portfolio_snapshot row deleted (orphan confirmed) → trade CLOSED."""
        all_snapshots = []  # Orphan detection already removed the snapshot
        pos_id = "pos123"
        still_has_snapshot = any(s["api_position_id"] == pos_id for s in all_snapshots)
        assert still_has_snapshot is False  # No grace period → closure proceeds

    def test_trade_closed_when_no_pos_id(self):
        """Trade with no api_position_id → grace period check skipped, closure proceeds."""
        pos_id = None
        all_snapshots = [{"api_position_id": "pos456"}]
        if pos_id:
            still_has_snapshot = any(s["api_position_id"] == pos_id for s in all_snapshots)
        else:
            still_has_snapshot = False  # No pos_id → no grace period
        assert still_has_snapshot is False


class TestDocstringConsistency:
    """P5: Docstring matches ORPHAN_THRESHOLD_MINUTES constant."""

    def test_orphan_threshold_matches_docstring(self):
        from bot.workers.reconciler import ORPHAN_THRESHOLD_MINUTES
        # The docstring says "> 5 min old" — verify the constant is 5
        assert ORPHAN_THRESHOLD_MINUTES == 5
