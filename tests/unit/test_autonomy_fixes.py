#!/usr/bin/env python3
"""Unit tests — fix/autonomy-hardening batch.

Covers:
  - check_slippage_gate (all pass/block/skip branches)
  - get_max_slippage_pct (config override + crypto detection)
  - check_daily_loss_breach (breach / no breach / disabled / edge cases)
  - heartbeat.is_stale + get_stale_workers (incl. deploy grace period)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.core.heartbeat import (
    EXPECTED_INTERVALS_MIN,
    STALE_FACTOR,
    get_stale_workers,
    is_stale,
    record_heartbeat,
)
from bot.core.risk import (
    DAILY_LOSS_LIMIT_PCT_DEFAULT,
    WEEKLY_LOSS_LIMIT_PCT_DEFAULT,
    MONTHLY_LOSS_LIMIT_PCT_DEFAULT,
    MAX_SLIPPAGE_PCT_CRYPTO,
    MAX_SLIPPAGE_PCT_DEFAULT,
    check_daily_loss_breach,
    check_trailing_loss_breach,
    check_slippage_gate,
    get_max_slippage_pct,
)

TS_FMT = "%Y-%m-%d %H:%M:%S"


# ─── Slippage Gate ────────────────────────────────────────────────────────────

class TestSlippageGate:
    def test_pass_within_tolerance(self):
        g = check_slippage_gate("AAPL", signal_price=100.0, current_price=101.0,
                                max_slippage_pct=1.5)
        assert g.allowed

    def test_block_price_ran_up(self):
        g = check_slippage_gate("AAPL", signal_price=100.0, current_price=102.0,
                                max_slippage_pct=1.5)
        assert not g.allowed
        assert "Slippage-Gate" in g.summary()

    def test_block_price_collapsed(self):
        # A collapse below signal price means the setup broke — also blocked.
        g = check_slippage_gate("AAPL", signal_price=100.0, current_price=97.0,
                                max_slippage_pct=1.5)
        assert not g.allowed

    def test_exact_boundary_passes(self):
        # deviation == max → not strictly greater → pass
        g = check_slippage_gate("AAPL", signal_price=100.0, current_price=101.5,
                                max_slippage_pct=1.5)
        assert g.allowed

    def test_skip_no_signal_price(self):
        assert check_slippage_gate("AAPL", None, 100.0, 1.5).allowed
        assert check_slippage_gate("AAPL", 0.0, 100.0, 1.5).allowed

    def test_skip_no_current_price(self):
        assert check_slippage_gate("AAPL", 100.0, None, 1.5).allowed
        assert check_slippage_gate("AAPL", 100.0, 0.0, 1.5).allowed

    def test_crypto_default_tolerance_via_symbol(self):
        # BTC at +2.5% with defaults: crypto max is 3.0 → pass
        g = check_slippage_gate("BTC", signal_price=100.0, current_price=102.5)
        assert g.allowed
        # Stock at +2.5% with defaults: 1.5 → block
        g2 = check_slippage_gate("AAPL", signal_price=100.0, current_price=102.5)
        assert not g2.allowed


class TestMaxSlippageConfig:
    def test_defaults(self):
        assert get_max_slippage_pct("AAPL") == MAX_SLIPPAGE_PCT_DEFAULT
        assert get_max_slippage_pct("BTC-USD") == MAX_SLIPPAGE_PCT_CRYPTO
        assert get_max_slippage_pct("btc") == MAX_SLIPPAGE_PCT_CRYPTO

    def test_config_override(self):
        cfg = {"trading": {"max_slippage_pct": 2.0, "max_slippage_pct_crypto": 5.0}}
        assert get_max_slippage_pct("AAPL", cfg) == 2.0
        assert get_max_slippage_pct("ETH", cfg) == 5.0

    def test_empty_config_falls_back(self):
        assert get_max_slippage_pct("AAPL", {}) == MAX_SLIPPAGE_PCT_DEFAULT
        assert get_max_slippage_pct("AAPL", {"trading": {}}) == MAX_SLIPPAGE_PCT_DEFAULT


# ─── Daily-Loss Breach ────────────────────────────────────────────────────────

class TestDailyLossBreach:
    def test_no_breach(self):
        breached, pnl = check_daily_loss_breach(10_000.0, 9_700.0, 5.0)
        assert not breached
        assert pnl == pytest.approx(-3.0)

    def test_breach(self):
        breached, pnl = check_daily_loss_breach(10_000.0, 9_400.0, 5.0)
        assert breached
        assert pnl == pytest.approx(-6.0)

    def test_exact_limit_breaches(self):
        breached, pnl = check_daily_loss_breach(10_000.0, 9_500.0, 5.0)
        assert breached
        assert pnl == pytest.approx(-5.0)

    def test_gain_never_breaches(self):
        breached, _ = check_daily_loss_breach(10_000.0, 11_000.0, 5.0)
        assert not breached

    def test_disabled_via_zero_limit(self):
        breached, _ = check_daily_loss_breach(10_000.0, 1_000.0, 0.0)
        assert not breached

    def test_negative_limit_treated_as_abs(self):
        breached, _ = check_daily_loss_breach(10_000.0, 9_400.0, -5.0)
        # limit_pct <= 0 disables — documented behaviour
        assert not breached

    def test_invalid_equities_never_breach(self):
        assert not check_daily_loss_breach(0.0, 9_000.0, 5.0)[0]
        assert not check_daily_loss_breach(10_000.0, 0.0, 5.0)[0]
        assert not check_daily_loss_breach(-1.0, -1.0, 5.0)[0]

    def test_default_constant_sane(self):
        assert 0.0 < DAILY_LOSS_LIMIT_PCT_DEFAULT <= 10.0


class TestTrailingLossBreach:
    """fix/multi-horizon-loss-limits: hard weekly/monthly circuit breakers,
    measured as max drawdown from the trailing-window equity high."""

    def test_no_breach(self):
        breached, dd = check_trailing_loss_breach(10_000.0, 9_500.0, 8.0)
        assert not breached
        assert dd == pytest.approx(-5.0)

    def test_weekly_breach(self):
        # -8.5% below the 7-day peak, limit 8% → breach
        breached, dd = check_trailing_loss_breach(10_000.0, 9_150.0, 8.0)
        assert breached
        assert dd == pytest.approx(-8.5)

    def test_exact_limit_breaches(self):
        breached, dd = check_trailing_loss_breach(10_000.0, 8_800.0, 12.0)
        assert breached
        assert dd == pytest.approx(-12.0)

    def test_slow_bleed_scenario(self):
        # The audit failure case: -4.9%/day compounding never trips the
        # intraday DAILY gate, but the weekly peak-drawdown catches it.
        peak = 10_000.0
        eq = peak
        for _ in range(3):
            eq *= (1 - 0.049)
        # eq ~= 8600 → -14% from the weekly peak, well past the 8% weekly limit
        breached, dd = check_trailing_loss_breach(peak, eq, 8.0)
        assert breached
        assert dd < -13.0

    def test_gain_never_breaches(self):
        breached, _ = check_trailing_loss_breach(10_000.0, 10_500.0, 8.0)
        assert not breached

    def test_disabled_via_zero_limit(self):
        breached, _ = check_trailing_loss_breach(10_000.0, 5_000.0, 0.0)
        assert not breached

    def test_negative_limit_disables(self):
        breached, _ = check_trailing_loss_breach(10_000.0, 8_000.0, -8.0)
        assert not breached

    def test_invalid_equities_never_breach(self):
        assert not check_trailing_loss_breach(0.0, 9_000.0, 8.0)[0]
        assert not check_trailing_loss_breach(10_000.0, 0.0, 8.0)[0]

    def test_default_constants_ordered(self):
        # daily < weekly < monthly — a longer horizon must tolerate a
        # larger drawdown, else the shorter one is redundant.
        assert DAILY_LOSS_LIMIT_PCT_DEFAULT < WEEKLY_LOSS_LIMIT_PCT_DEFAULT
        assert WEEKLY_LOSS_LIMIT_PCT_DEFAULT < MONTHLY_LOSS_LIMIT_PCT_DEFAULT


# ─── Heartbeat / Dead-Man's Switch ───────────────────────────────────────────

class FakeStateRepo:
    """Minimal in-memory stand-in for StateRepo."""
    def __init__(self, data: dict | None = None):
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class TestHeartbeat:
    def test_record_and_not_stale(self):
        repo = FakeStateRepo()
        record_heartbeat(repo, "risk_worker")
        last = repo.get("LAST_RUN_RISK_WORKER")
        assert last is not None
        assert not is_stale(last, interval_min=5)

    def test_stale_after_window(self):
        now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        old = (now - timedelta(minutes=5 * STALE_FACTOR + 1)).strftime(TS_FMT)
        assert is_stale(old, interval_min=5, now=now)

    def test_fresh_within_window(self):
        now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        fresh = (now - timedelta(minutes=5 * STALE_FACTOR - 1)).strftime(TS_FMT)
        assert not is_stale(fresh, interval_min=5, now=now)

    def test_missing_or_garbage_is_stale(self):
        assert is_stale(None, 5)
        assert is_stale("", 5)
        assert is_stale("not-a-timestamp", 5)

    def test_deploy_grace_first_run_reports_nothing(self):
        repo = FakeStateRepo()  # no HEARTBEAT_DEPLOYED_AT yet
        assert get_stale_workers(repo) == []
        assert repo.get("HEARTBEAT_DEPLOYED_AT") is not None

    def test_stale_workers_reported_after_grace(self):
        now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        deployed = (now - timedelta(hours=2)).strftime(TS_FMT)
        fresh = (now - timedelta(minutes=1)).strftime(TS_FMT)
        stale_ts = (now - timedelta(hours=1)).strftime(TS_FMT)
        repo = FakeStateRepo({
            "HEARTBEAT_DEPLOYED_AT": deployed,
            "LAST_RUN_RISK_WORKER": fresh,
            "LAST_RUN_DATA_WORKER": stale_ts,
            # reconciler / signal / execution never ran → also stale
        })
        stale = get_stale_workers(repo, now=now)
        joined = " ".join(stale)
        assert "data_worker" in joined
        assert "reconciler" in joined
        assert "risk_worker" not in joined
        # never-ran workers should reference the deploy marker
        assert any("noch nie gelaufen" in s for s in stale)

    def test_all_fresh_reports_nothing(self):
        now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
        fresh = (now - timedelta(minutes=1)).strftime(TS_FMT)
        data = {"HEARTBEAT_DEPLOYED_AT": fresh}
        for w in EXPECTED_INTERVALS_MIN:
            data[f"LAST_RUN_{w.upper()}"] = fresh
        repo = FakeStateRepo(data)
        assert get_stale_workers(repo, now=now) == []