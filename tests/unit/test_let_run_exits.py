#!/usr/bin/env python3
"""feat/let-run-exits (VoLLi 2026-10-17, E5).

Ausstiege nur noch Stop-Loss, SELL-Signale und Zeitstopp: Break-Even-Lock,
Momentum-Fade, Scalp-Stufe, Stale-Exit und TIGHTEN-Orders aus. E5 (786 Titel,
2019-2026): "laufen lassen" schlug die heutigen Ausstiege in allen fuenf Familien.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

import bot.core.trailing_stop as ts
from bot.core.trailing_stop import evaluate_trailing, mark_break_even_active
from bot.db.connection import DB
from pathlib import Path

CFG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


@pytest.fixture
def db(tmp_path):
    return DB(db_path=tmp_path / "trading.db")


@pytest.fixture
def let_run(monkeypatch):
    monkeypatch.setattr(ts, "BREAK_EVEN_ENABLED", False)
    monkeypatch.setattr(ts, "MOMENTUM_FADE_ENABLED", False)
    monkeypatch.setattr(ts, "SCALP_ENABLED", False)
    monkeypatch.setattr(ts, "STALE_EXIT_ENABLED", False)
    monkeypatch.setattr(ts, "LADDER_ENABLED", False)
    monkeypatch.setattr(ts, "FULL_EXIT_ENABLED", False)
    monkeypatch.setattr(ts, "TIME_STOP_DAYS", 28)
    monkeypatch.setattr(ts, "MIN_HOLD_HOURS", 24.0)


def _pos(pnl_pct, days_old=5, pid="p1", amount=500.0):
    opened = (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"positionID": pid, "symbol": "ZM", "instrumentID": 42, "amount": amount,
            "openRate": 100.0, "openDateTime": opened,
            "unrealizedPnL": {"pnL": amount * pnl_pct / 100.0}}


def test_live_config_ist_laufen_lassen():
    ts_cfg = yaml.safe_load(CFG.read_text(encoding="utf-8"))
    t = ts_cfg["trailing"]
    assert t["break_even"]["enabled"] is False
    assert t["time_stop"]["days"] == 28
    assert t["momentum_fade"]["enabled"] is False
    assert t["stale_exit"]["enabled"] is False
    assert t["scalp"]["enabled"] is False
    assert ts_cfg["llm_execution"]["tighten_orders"] is False


def test_kein_break_even_mehr(db, let_run):
    """+4 % arm(ierte) frueher BE; jetzt keine Aktion."""
    assert evaluate_trailing([_pos(4.0)], db=db) == []


def test_bereits_armierte_position_wird_nicht_am_be_floor_geschlossen(db, let_run):
    mark_break_even_active(db, "p1", "ZM")
    assert evaluate_trailing([_pos(0.1)], db=db) == []


def test_zeitstopp_schliesst_ganz(db, let_run):
    acts = evaluate_trailing([_pos(2.0, days_old=29)], db=db)
    assert [a.action for a in acts] == ["FULL_EXIT"]
    assert "Zeitstopp" in acts[0].reason


def test_zeitstopp_auch_im_verlust(db, let_run):
    acts = evaluate_trailing([_pos(-2.5, days_old=30)], db=db)
    assert [a.action for a in acts] == ["FULL_EXIT"]


def test_vor_zeitstopp_keine_aktion(db, let_run):
    assert evaluate_trailing([_pos(-2.5, days_old=27)], db=db) == []


def test_zeitstopp_aus(db, let_run, monkeypatch):
    monkeypatch.setattr(ts, "TIME_STOP_DAYS", 0)
    assert evaluate_trailing([_pos(2.0, days_old=60)], db=db) == []


def test_apply_config_liest_schalter():
    cfg = {"trailing": {"break_even": {"enabled": False}, "time_stop": {"days": 28}}}
    old = (ts.BREAK_EVEN_ENABLED, ts.TIME_STOP_DAYS)
    try:
        ts.apply_config(cfg)
        assert ts.BREAK_EVEN_ENABLED is False and ts.TIME_STOP_DAYS == 28
    finally:
        ts.BREAK_EVEN_ENABLED, ts.TIME_STOP_DAYS = old
