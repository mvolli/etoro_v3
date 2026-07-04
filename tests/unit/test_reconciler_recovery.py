#!/usr/bin/env python3
"""Unit tests — reconciler recovery guards (audit crit #4 + #6).

- is_fresh_fill_protected: a just-confirmed ACTIVE trade must not be
  orphan-closed on a single missing API cycle (it has no snapshot yet).
- classify_stale_submitting: a trade stranded in SUBMITTING by a crash
  between submit and the ACTIVE/FAILED transition gets recovered.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from bot.workers.reconciler import is_fresh_fill_protected, classify_stale_submitting

# Fixed reference window: cutoff = "5 minutes ago". ISO-8601 strings compare
# lexicographically, so "later" timestamps sort greater.
CUTOFF = "2026-07-04 12:00:00"
RECENT = "2026-07-04 12:03:00"   # after cutoff → within the window
OLD = "2026-07-04 11:55:00"      # before cutoff → past the window


# ── is_fresh_fill_protected (crit #4) ─────────────────────────────────────────

def test_fresh_fill_within_window_is_protected():
    assert is_fresh_fill_protected(RECENT, CUTOFF) is True


def test_old_fill_not_protected():
    assert is_fresh_fill_protected(OLD, CUTOFF) is False


def test_missing_confirmed_at_not_protected():
    # No confirmation timestamp → not a fresh fill → normal orphan rules apply.
    assert is_fresh_fill_protected(None, CUTOFF) is False
    assert is_fresh_fill_protected("", CUTOFF) is False


# ── classify_stale_submitting (crit #6) ───────────────────────────────────────

def test_submitting_with_live_position_recovers_to_active():
    assert classify_stale_submitting("pos123", {"pos123", "pos9"}, OLD, CUTOFF) == "ACTIVE"


def test_submitting_live_position_wins_even_if_recent():
    # A live position is proof of a fill regardless of age → ACTIVE, not None.
    assert classify_stale_submitting("pos123", {"pos123"}, RECENT, CUTOFF) == "ACTIVE"


def test_stale_submitting_no_position_fails():
    assert classify_stale_submitting("posX", set(), OLD, CUTOFF) == "FAILED"


def test_stale_submitting_no_pos_id_fails():
    assert classify_stale_submitting(None, {"pos123"}, OLD, CUTOFF) == "FAILED"


def test_recent_submitting_left_alone():
    # Within the grace window and no confirmed live position → leave for
    # execution_worker's own verify loop; do not prematurely fail it.
    assert classify_stale_submitting("posX", set(), RECENT, CUTOFF) is None


def test_recent_submitting_no_pos_id_left_alone():
    assert classify_stale_submitting(None, set(), RECENT, CUTOFF) is None
