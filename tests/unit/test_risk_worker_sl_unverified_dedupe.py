"""fix/sl-close-unverified-dedupe (2026-08-21): Regressionstest fuer die
per-Position Alert-Dedupe im risk_worker.

Root cause (Live-DB 2026-08-21): a verified=False SL-close fired 1 ERROR +
1 CRITICAL Discord alert on EVERY 5-minute risk cycle per stuck position
(2600.HK: 105 alerts in ~11h ≈ 144 alerts/day). The state is already
persisted (trades.verification_status='PENDING', Reconciler finalizes it),
so this is a notification problem. Fix: per-position cap (3) + cooldown
(6h), CRITICAL→WARNING downgrade after the first hit, both the provisional
CLOSE embed and the CRITICAL alert share one gate.

This test drives the pure helper `_unverified_close_alert_due` against a
real SQLite-backed StateRepo (the exact production code path), plus
`_clear_unverified_close_state` for the verified-close reset.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.db.repo import DB, StateRepo
from bot.workers.risk_worker import (
    SL_UNVERIFIED_COOLDOWN_HOURS,
    SL_UNVERIFIED_MAX_ALERTS,
    _clear_unverified_close_state,
    _unverified_close_alert_due,
)


@pytest.fixture()
def repo(tmp_path):
    """Real StateRepo on a temp DB — exact production persistence path."""
    d = DB(db_path=tmp_path / "state.db")
    d.execute("""
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)
    yield StateRepo(d)
    d.close()


def _stored_count(repo, position_id) -> int:
    raw = repo.get(f"SL_UNVERIFIED_{position_id}")
    assert raw is not None, "state must be persisted"
    return int(json.loads(raw)["count"])


# ── First hit fires ────────────────────────────────────────────────────────────

def test_first_hit_fires(repo):
    due, count = _unverified_close_alert_due(repo, "12345", "2600.HK")
    assert due is True, "first unverified-close must always alert"
    assert count == 1
    assert _stored_count(repo, "12345") == 1


# ── Cooldown suppresses repeats ───────────────────────────────────────────────

def test_repeated_within_cooldown_suppressed(repo):
    _unverified_close_alert_due(repo, "12345", "2600.HK")  # fires (count 1)
    for i in range(2, 9):  # same cycle-window: 7 more hits
        due, count = _unverified_close_alert_due(repo, "12345", "2600.HK")
        assert due is False, f"hit {i} inside cooldown must be suppressed"
        assert count == i
    # State kept counting (cap tracking still runs even while suppressed)
    assert _stored_count(repo, "12345") == 8


# ── Cap after MAX_ALERTS ──────────────────────────────────────────────────────

def test_cap_never_fires_after_max(repo):
    # Backdate last_at past cooldown so each hit is "fresh", force count past cap
    for i in range(1, SL_UNVERIFIED_MAX_ALERTS + 3):
        due, count = _unverified_close_alert_due(repo, "42", "AAPL")
        if i <= SL_UNVERIFIED_MAX_ALERTS:
            assert due is True, f"hit {i} (count {count}) must fire"
        else:
            assert due is False, f"hit {i} (count {count}) over cap must not fire"
        # push last_at back so the next hit isn't cooldown-suppressed
        key = "SL_UNVERIFIED_42"
        info = json.loads(repo.get(key))
        past = (datetime.now(timezone.utc) - timedelta(hours=SL_UNVERIFIED_COOLDOWN_HOURS + 1)).isoformat()
        repo.set(key, json.dumps({"count": count, "last_at": past, "symbol": "AAPL"}))


# ── Cooldown expiry re-fires ───────────────────────────────────────────────────

def test_cooldown_expiry_refires(repo):
    _unverified_close_alert_due(repo, "12345", "2600.HK")  # count=1, fires
    # Backdate last_at 7h ago (past 6h cooldown)
    key = "SL_UNVERIFIED_12345"
    info = json.loads(repo.get(key))
    past = (datetime.now(timezone.utc) - timedelta(hours=SL_UNVERIFIED_COOLDOWN_HOURS + 1)).isoformat()
    repo.set(key, json.dumps({"count": info["count"], "last_at": past, "symbol": "2600.HK"}))
    due, count = _unverified_close_alert_due(repo, "12345", "2600.HK")
    assert due is True, "cooldown expired → may fire again"
    assert count == 2


# ── Independent positions ──────────────────────────────────────────────────────

def test_positions_are_independent(repo):
    d1, c1 = _unverified_close_alert_due(repo, "111", "AAA")
    d2, c2 = _unverified_close_alert_due(repo, "222", "BBB")
    assert d1 is True and c1 == 1
    assert d2 is True and c2 == 1, "each position tracks its own counter"
    # Position 111 again is suppressed, but 222 is unaffected
    d1b, c1b = _unverified_close_alert_due(repo, "111", "AAA")
    assert d1b is False and c1b == 2


# ── Corrupt state → safe reset, only first fires ─────────────────────────────

def test_corrupt_state_is_safe(repo):
    repo.set("SL_UNVERIFIED_999", "{not valid json")
    due, count = _unverified_close_alert_due(repo, "999", "XXX")
    assert due is True, "unparsable state → treat as first hit"
    assert count == 1


# ── _clear_unverified_close_state (verified close resets) ─────────────────────

def test_clear_resets_state(repo):
    _unverified_close_alert_due(repo, "12345", "2600.HK")
    _unverified_close_alert_due(repo, "12345", "2600.HK")
    assert repo.get("SL_UNVERIFIED_12345") is not None
    _clear_unverified_close_state(repo, "12345")
    assert repo.get("SL_UNVERIFIED_12345") is None, "state must be cleared"
    # After a verified close + reset, the next unverified-close fires again
    due, count = _unverified_close_alert_due(repo, "12345", "2600.HK")
    assert due is True and count == 1


# ── Clear on unknown position is a no-op (never raises) ────────────────────────

def test_clear_unknown_position_noop(repo):
    _clear_unverified_close_state(repo, "does-not-exist")  # must not raise
