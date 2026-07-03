#!/usr/bin/env python3
"""Unit tests — fix/rolling-peak-drawdown.

Regime drawdown is computed against the 30-day rolling peak instead of the
all-time high; old peaks age out; all-time PEAK_EQUITY is still maintained
for reporting; DB failure falls back to the all-time peak.
"""
from __future__ import annotations

import pytest

from bot.core.regime import (
    ROLLING_PEAK_DAYS,
    get_rolling_peak,
    record_equity_snapshot,
    update_regime,
)
from bot.db.connection import DB


@pytest.fixture
def db(tmp_path):
    d = DB(db_path=tmp_path / "trading.db")
    d.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
        )
    """)
    return d


class StateRepoLite:
    """Minimal StateRepo — genug für update_regime()."""

    def __init__(self, db):
        self.db = db

    def get(self, key, default=None):
        row = self.db.fetchone("SELECT value FROM system_state WHERE key = ?", (key,))
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, datetime('now'))"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def get_float(self, key, default=0.0):
        v = self.get(key)
        try:
            return float(v) if v is not None else default
        except ValueError:
            return default

    def get_regime(self):
        return self.get("REGIME", "NORMAL")

    def set_regime(self, regime):
        self.set("REGIME", regime)


def _seed_history(db, rows):
    record_equity_snapshot(db, 1.0)  # Tabelle anlegen
    db.execute("DELETE FROM equity_history")
    for offset_days, equity in rows:
        db.execute(
            "INSERT INTO equity_history (date, equity_high) "
            "VALUES (date('now', ?), ?)",
            (f"-{offset_days} days", equity),
        )


def test_snapshot_keeps_daily_high(db):
    record_equity_snapshot(db, 10_000)
    record_equity_snapshot(db, 9_500)     # niedriger am selben Tag
    record_equity_snapshot(db, 10_200)    # neues Tageshoch
    assert get_rolling_peak(db, 0.0) == 10_200


def test_old_peak_ages_out(db):
    # Peak vor 45 Tagen liegt außerhalb des 30-Tage-Fensters
    _seed_history(db, [(45, 15_000), (10, 10_500)])
    assert get_rolling_peak(db, 10_000) == 10_500


def test_current_equity_counts_as_peak(db):
    _seed_history(db, [(5, 10_000)])
    assert get_rolling_peak(db, 11_000) == 11_000


def test_regime_uses_rolling_not_alltime(db):
    # All-Time-Peak 15k (vor 45 Tagen), 30d-Peak 10.5k, Equity 10k
    # → DD gegen All-Time wäre 33% (CRITICAL!), gegen Rolling 4.8% (CAUTION)
    _seed_history(db, [(45, 15_000), (10, 10_500)])
    repo = StateRepoLite(db)
    repo.set("PEAK_EQUITY", "15000")

    regime, _ = update_regime(repo, 10_000)
    assert regime == "CAUTION"
    assert float(repo.get("DRAWDOWN_PCT")) == pytest.approx(4.76, abs=0.1)


def test_alltime_peak_still_maintained_for_reporting(db):
    repo = StateRepoLite(db)
    repo.set("PEAK_EQUITY", "10000")
    update_regime(repo, 12_000)
    assert repo.get_float("PEAK_EQUITY") == 12_000
    assert repo.get("HIGH_WATERMARK_REACHED") == "true"


def test_high_watermark_false_below_rolling_peak(db):
    _seed_history(db, [(5, 11_000)])
    repo = StateRepoLite(db)
    repo.set("PEAK_EQUITY", "11000")
    update_regime(repo, 10_800)
    assert repo.get("HIGH_WATERMARK_REACHED") == "false"


def test_db_failure_falls_back_to_alltime(db):
    class BrokenDB:
        def execute(self, *a):
            raise RuntimeError("db down")

        def fetchone(self, *a):
            raise RuntimeError("db down")

    repo = StateRepoLite(db)
    repo.set("PEAK_EQUITY", "10000")
    repo.db_broken = BrokenDB()
    # update_regime nutzt state_repo.db für die History — kaputte DB simulieren,
    # indem wir sie nach dem Setup austauschen
    real_db = repo.db

    class HybridRepo(StateRepoLite):
        db = BrokenDB()

        def __init__(self):
            self._real = repo

        def get(self, k, d=None):
            return self._real.get(k, d)

        def set(self, k, v):
            self._real.set(k, v)

        def get_float(self, k, d=0.0):
            return self._real.get_float(k, d)

        def get_regime(self):
            return self._real.get_regime()

        def set_regime(self, r):
            self._real.set_regime(r)

    regime, _ = update_regime(HybridRepo(), 9_000)
    # Fallback: DD gegen All-Time 10k = 10% → DEFENSIVE
    assert regime == "DEFENSIVE"
