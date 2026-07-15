#!/usr/bin/env python3
"""Tests — fix/stale-exit (2026-07-15): stagnierende Positionen erkennen.

Getestet werden die harten Entscheidungsregeln (is_stale_candidate) und die
Integration in evaluate_trailing (Dry-Log vs. enabled, Grace, fail-safe)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import bot.core.trailing_stop as ts
from bot.core.trailing_stop import evaluate_trailing, is_stale_candidate


@pytest.fixture(autouse=True)
def _stale_defaults(monkeypatch):
    """Deterministische Parameter + kein echtes HOLD-File lesen."""
    monkeypatch.setattr(ts, "STALE_EXIT_ENABLED", True)
    monkeypatch.setattr(ts, "STALE_MIN_DAYS", 10)
    monkeypatch.setattr(ts, "STALE_PNL_BAND_PCT", 1.5)
    monkeypatch.setattr(ts, "STALE_MIN_PEAK_PCT", 2.0)
    monkeypatch.setattr(ts, "STALE_LLM_HOLD_GRACE_H", 24.0)
    monkeypatch.setattr(ts, "STALE_MAX_DAYS", 20)
    monkeypatch.setattr(ts, "_load_fresh_llm_holds", lambda *_: set())


# ── is_stale_candidate (pure) ────────────────────────────────────────────────

def test_stale_candidate_happy_path():
    assert is_stale_candidate(pnl_pct=0.4, peak_pnl_pct=0.9, days_held=12) is True


def test_too_young_is_not_stale():
    assert is_stale_candidate(0.4, 0.9, days_held=9) is False


def test_pnl_outside_band_is_not_stale():
    assert is_stale_candidate(1.6, 0.9, 12) is False
    assert is_stale_candidate(-1.6, 0.9, 12) is False


def test_peak_in_fade_territory_is_not_stale():
    # Peak >= 2.0 = Momentum-Fade-Revier — Disjunktheit garantiert
    assert is_stale_candidate(0.4, 2.0, 12) is False


def test_broken_open_date_is_fail_safe():
    assert is_stale_candidate(0.4, 0.9, days_held=None) is False


# ── evaluate_trailing-Integration (db=None, stateless) ───────────────────────

def _pos(days_old: int, pnl_usd: float = 0.5, amount: float = 100.0):
    opened = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {
        "positionID": "777", "symbol": "STALE.X", "instrumentID": 42,
        "amount": amount, "openRate": 10.0,
        "unrealizedPnL": {"pnL": pnl_usd},
        "openDateTime": opened,
    }


def test_evaluate_emits_stale_exit_when_enabled():
    actions = evaluate_trailing([_pos(days_old=12)], db=None)
    stale = [a for a in actions if a.action == "STALE_EXIT"]
    assert len(stale) == 1
    assert "12d seitwaerts" in stale[0].reason
    assert stale[0].instrument_id == 42


def test_evaluate_dry_logs_when_disabled(monkeypatch):
    monkeypatch.setattr(ts, "STALE_EXIT_ENABLED", False)
    actions = evaluate_trailing([_pos(days_old=12)], db=None)
    assert not [a for a in actions if a.action == "STALE_EXIT"]


def test_llm_hold_grace_skips_until_max_days(monkeypatch):
    monkeypatch.setattr(ts, "_load_fresh_llm_holds", lambda *_: {"STALE.X"})
    # 12d < max_days 20 → Grace greift
    actions = evaluate_trailing([_pos(days_old=12)], db=None)
    assert not [a for a in actions if a.action == "STALE_EXIT"]
    # 21d >= max_days → Deckel ueberstimmt HOLD
    actions = evaluate_trailing([_pos(days_old=21)], db=None)
    assert [a for a in actions if a.action == "STALE_EXIT"]


def test_missing_open_date_no_exit():
    pos = _pos(days_old=12)
    del pos["openDateTime"]
    actions = evaluate_trailing([pos], db=None)
    assert not [a for a in actions if a.action == "STALE_EXIT"]


def test_profitable_position_untouched():
    # +5% PnL: laeuft im Profit-Ladder-Pfad, nie Stale
    actions = evaluate_trailing([_pos(days_old=15, pnl_usd=5.0)], db=None)
    assert not [a for a in actions if a.action == "STALE_EXIT"]


def test_apply_config_wires_stale_block(monkeypatch):
    ts.apply_config({"trailing": {"stale_exit": {
        "enabled": True, "min_days": 7, "pnl_band_pct": 2.0,
        "min_peak_pct": 1.5, "llm_hold_grace_h": 12, "max_days": 14,
    }}})
    assert ts.STALE_EXIT_ENABLED is True
    assert ts.STALE_MIN_DAYS == 7
    assert ts.STALE_PNL_BAND_PCT == 2.0
    assert ts.STALE_MIN_PEAK_PCT == 1.5
    assert ts.STALE_MAX_DAYS == 14
