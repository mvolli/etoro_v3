#!/usr/bin/env python3
"""Unit tests — feat/ladder-decouple + feat/sweep-fixed-tranche (2026-10-16, VoLLi).

Vor dem Umstieg aufs Marktregime: die Gewinnleiter und die CORE_SWEEP-Regeln
duerfen nicht mehr am Regime haengen. Sonst feuert die Leiter, sobald das
Regime NORMAL wird, und der Sweep kauft ~$352 statt $200 je Position.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bot.core.trailing_stop as ts
from bot.core.core_sweep import plan_core_sweep
from bot.core.trailing_stop import evaluate_trailing, save_profit_levels
from bot.db.connection import DB


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    monkeypatch.setattr(ts, "LADDER_ENABLED", True)
    monkeypatch.setattr(ts, "SUPPRESS_LADDER_REGIMES", ("DEFENSIVE", "CRITICAL"))
    monkeypatch.setattr(ts, "FULL_EXIT_ENABLED", False)
    monkeypatch.setattr(ts, "MOMENTUM_FADE_ENABLED", True)
    monkeypatch.setattr(ts, "MIN_REMAINING_PCT", 50.0)
    monkeypatch.setattr(ts, "STALE_EXIT_ENABLED", False)


@pytest.fixture
def db(tmp_path):
    return DB(db_path=tmp_path / "trading.db")


def _pos(pnl):
    return {"positionID": "p1", "symbol": "ZM", "instrumentID": 42, "amount": 1000.0,
            "openRate": 100.0, "unrealizedPnL": {"pnL": 1000.0 * pnl / 100.0}}


def _seed(db):
    save_profit_levels(db, "p1", "ZM", [{"threshold": 5.0, "close_pct": 25.0}])
    db.execute("UPDATE position_state SET peak_pnl_pct = 5.5, remaining_frac = 1.0, "
               "momentum_faded = 0 WHERE position_id = 'p1'")


# ── Leiter-Schalter ──────────────────────────────────────────────────────────
def test_config_schaltet_leiter_aus_und_regime_liste_leer():
    ts.apply_config({"trailing": {"profit_ladder": {"enabled": False},
                                  "ladder_suppressed_regimes": []}})
    assert ts.LADDER_ENABLED is False and ts.SUPPRESS_LADDER_REGIMES == ()


@pytest.mark.parametrize("regime", ["NORMAL", "CAUTION", "DEFENSIVE", "CRITICAL"])
def test_leiter_aus_in_jedem_regime(db, monkeypatch, regime):
    monkeypatch.setattr(ts, "LADDER_ENABLED", False)
    monkeypatch.setattr(ts, "SUPPRESS_LADDER_REGIMES", ())
    _seed(db)
    assert evaluate_trailing([_pos(5.5)], regime=regime, db=db) == []


@pytest.mark.parametrize("regime", ["NORMAL", "DEFENSIVE", "CRITICAL"])
def test_leiter_an_ist_regimeunabhaengig(db, monkeypatch, regime):
    monkeypatch.setattr(ts, "SUPPRESS_LADDER_REGIMES", ())
    _seed(db)
    assert [a.action for a in evaluate_trailing([_pos(5.5)], regime=regime, db=db)] == ["PARTIAL_CLOSE"]


def test_code_default_unveraendert():
    """Ohne Config: alte Semantik (an, in DEFENSIVE/CRITICAL gesperrt)."""
    assert ts.ladder_suppressed("NORMAL") is False
    assert ts.ladder_suppressed("DEFENSIVE") is True


def test_beide_pruefstellen_nutzen_den_schalter():
    src = Path(ts.__file__).read_text(encoding="utf-8")
    assert src.count("ladder_suppressed(regime)") == 2
    assert "regime in SUPPRESS_LADDER_REGIMES):" not in src


# ── D4-Abbruchregel regimeunabhaengig ────────────────────────────────────────
class _FakeDB:
    def __init__(self, pnls):
        self._pnls = pnls
    def fetchone(self, sql, params=()):
        return {"n": 0}
    def fetchall(self, sql, params=()):
        return [{"pnl_usd": p} for p in self._pnls]


def _cfg():
    return {"trading": {"core_sweep": {
        "enabled": True, "reserve_target_pct": 15.0, "reserve_floor_pct": 10.0,
        "per_position_pct": 4.0, "max_position_pct": 6.0, "max_sweeps_per_run": 4,
        "regimes": ["NORMAL", "CAUTION", "DEFENSIVE"], "max_open_positions": 3,
        "whitelist": {"AAPL": 1001}}}}


@pytest.mark.parametrize("regime", ["NORMAL", "CAUTION", "DEFENSIVE"])
def test_abbruchregel_greift_in_jedem_regime(regime):
    orders, reasons = plan_core_sweep(_cfg(), equity=10_000, cash=8_000, regime=regime,
                                      db=_FakeDB([-5.0] * 10))
    assert orders == []
    assert any("D4-Abort" in r and "pausiert" in r for r in reasons)


# ── Sweep fix $200 ───────────────────────────────────────────────────────────
def test_sweep_fixbetrag_vor_portfolio_gate():
    src = (Path(__file__).resolve().parents[2] / "src/bot/workers/signal_worker.py").read_text(encoding="utf-8")
    i_fix = src.index('get("fixed_tranche_usd", 0)')
    i_gate = src.index("_cs_ok, _cs_amt, _cs_gate_reason = _core_sweep_portfolio_gate(")
    i_eq = src.index("ENTRY-QUALITY CORE_SWEEP")
    assert i_eq < i_fix < i_gate


def test_config_sweep_fix_200():
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config/config.yaml").read_text())
    assert cfg["trading"]["core_sweep"]["fixed_tranche_usd"] == 200.0
    assert cfg["trailing"]["profit_ladder"]["enabled"] is False
    assert cfg["trailing"]["ladder_suppressed_regimes"] == []
