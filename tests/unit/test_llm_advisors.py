#!/usr/bin/env python3
"""Tests — LLM-Advisor-Paket 2026-07-14 (fix/llm-pretrade-veto,
fix/llm-news-flags, fix/llm-macro-advisor, fix/llm-config-experiments).

Getestet werden die HARTEN Schutzmechanismen, nicht die LLM-Aufrufe:
Veto darf nur APPROVED-Trades treffen, Reduce nur verkleinern, Clamps
und Whitelists muessen Halluzinationen abfangen.
"""
from __future__ import annotations

import pytest

from bot.db.connection import DB
from bot.workers.config_experiment_worker import (
    TUNABLE_PARAMS,
    _apply_config_value,
    _read_config_value,
    _validate_proposal,
)
from bot.workers.macro_regime_worker import _clamp_scalar
from bot.workers.news_flags_worker import _parse_llm_flags
from bot.workers.trade_veto_worker import _apply_decision

import bot.workers.config_experiment_worker as cew


# ── Pre-Trade-Veto: race-safe, asymmetrisch ──────────────────────────────────

@pytest.fixture()
def trade_db(tmp_path):
    db = DB(db_path=tmp_path / "t.db")
    db.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, symbol TEXT, amount_usd REAL,
            status TEXT, rejection_reason TEXT
        )
    """)
    db.execute("INSERT INTO trades VALUES (1, 'AAPL', 400.0, 'APPROVED', NULL)")
    db.execute("INSERT INTO trades VALUES (2, 'MSFT', 400.0, 'ACTIVE', NULL)")
    return db


def _trade(id_, amount=400.0):
    return {"id": id_, "symbol": "X", "amount_usd": amount}


def test_veto_rejects_approved_trade(trade_db):
    out = _apply_decision(trade_db, _trade(1), {"decision": "VETO", "reason": "Test"}, 50.0)
    assert out == "VETO"
    row = trade_db.fetchone("SELECT status, rejection_reason FROM trades WHERE id=1")
    assert row["status"] == "REJECTED"
    assert row["rejection_reason"].startswith("LLM-Veto:")


def test_veto_loses_race_against_execution(trade_db):
    # Trade 2 ist schon ACTIVE (execution_worker war schneller) → NOOP
    out = _apply_decision(trade_db, _trade(2), {"decision": "VETO", "reason": "zu spaet"}, 50.0)
    assert out == "NOOP"
    row = trade_db.fetchone("SELECT status FROM trades WHERE id=2")
    assert row["status"] == "ACTIVE"


def test_reduce_shrinks_amount_within_bounds(trade_db):
    out = _apply_decision(trade_db, _trade(1), {"decision": "REDUCE", "reduce_to_pct": 50}, 50.0)
    assert out == "REDUCE"
    row = trade_db.fetchone("SELECT amount_usd, status FROM trades WHERE id=1")
    assert row["amount_usd"] == 200.0 and row["status"] == "APPROVED"


def test_reduce_is_clamped_cannot_boost(trade_db):
    # LLM halluziniert 500% → Clamp auf 75% Maximum, nie Vergroesserung
    out = _apply_decision(trade_db, _trade(1), {"decision": "REDUCE", "reduce_to_pct": 500}, 50.0)
    assert out == "REDUCE"
    row = trade_db.fetchone("SELECT amount_usd FROM trades WHERE id=1")
    assert row["amount_usd"] == 300.0  # 75% von 400


def test_reduce_below_min_buy_becomes_veto(trade_db):
    trade_db.execute("UPDATE trades SET amount_usd=100.0 WHERE id=1")
    out = _apply_decision(trade_db, _trade(1, 100.0),
                          {"decision": "REDUCE", "reduce_to_pct": 25}, 50.0)
    assert out == "VETO"
    row = trade_db.fetchone("SELECT status FROM trades WHERE id=1")
    assert row["status"] == "REJECTED"


def test_unknown_decision_is_approve(trade_db):
    out = _apply_decision(trade_db, _trade(1), {"decision": "MOONSHOT_BUY_MORE"}, 50.0)
    assert out == "APPROVE"
    row = trade_db.fetchone("SELECT status, amount_usd FROM trades WHERE id=1")
    assert row["status"] == "APPROVED" and row["amount_usd"] == 400.0


# ── Makro-Scalar: harter Clamp [0.5, 1.0] ────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (0.7, 0.7), (1.0, 1.0),
    (1.5, 1.0),      # LLM will boosten → gekappt
    (0.1, 0.5),      # LLM uebertreibt Panik → Boden
    ("kaputt", 1.0), (None, 1.0),  # unparsbar → neutral
])
def test_macro_scalar_clamp(raw, expected):
    assert _clamp_scalar(raw) == expected


# ── News-Flags: nur AVOID/CAUTION ueberleben die Validierung ─────────────────

def test_news_flags_validation_drops_hallucinations():
    result = _parse_llm_flags({"flags": {
        "AAPL": {"flag": "AVOID", "reason": "Gewinnwarnung"},
        "MSFT": {"flag": "CAUTION", "reason": "Downgrade"},
        "NVDA": {"flag": "BUY_NOW", "reason": "to the moon"},   # ungueltig
        "TSLA": "kein dict",                                      # ungueltig
    }})
    assert set(result) == {"AAPL", "MSFT"}
    assert result["AAPL"]["severity"] == "HIGH"
    assert result["MSFT"]["severity"] == "MEDIUM"


def test_news_flags_empty_or_broken_input():
    assert _parse_llm_flags(None) == {}
    assert _parse_llm_flags({"quatsch": 1}) == {}


# ── Config-Experimente: Whitelist + Bounds + YAML-Roundtrip ──────────────────

@pytest.fixture()
def fake_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sl:\n"
        "  default_pct: 2.5            # Kommentar bleibt\n"
        "trading:\n"
        "  max_slippage_pct: 1.5       # stocks\n"
        "  max_slippage_pct_crypto: 3.0  # darf NICHT mitgetroffen werden\n"
        "  cash_target_max_pct: 30.0\n"
        "sizing:\n"
        "  high_pct: 7.0\n"
        "  medium_pct: 5.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cew, "CONFIG_PATH", cfg)
    return cfg


def test_validate_rejects_unknown_param(fake_config):
    assert _validate_proposal({"param": "risk.daily_loss_limit_pct", "new_value": 99}) is None
    assert _validate_proposal({"param": "kill_switch", "new_value": 0}) is None


def test_validate_clamps_to_bounds(fake_config):
    param, value = _validate_proposal({"param": "sl.default_pct", "new_value": 99.0})
    assert param == "sl.default_pct"
    assert value == TUNABLE_PARAMS["sl.default_pct"]["max"]


def test_validate_rejects_no_change(fake_config):
    assert _validate_proposal({"param": "sl.default_pct", "new_value": 2.5}) is None
    assert _validate_proposal({"param": "sl.default_pct", "new_value": "abc"}) is None


def test_apply_config_roundtrip_preserves_rest(fake_config):
    assert _read_config_value("sl.default_pct") == 2.5
    assert _apply_config_value("sl.default_pct", 3.0) is True
    content = fake_config.read_text(encoding="utf-8")
    assert "default_pct: 3.0" in content
    assert "# Kommentar bleibt" in content
    assert "max_slippage_pct_crypto: 3.0" in content  # Nachbar unangetastet
    # Rollback
    assert _apply_config_value("sl.default_pct", 2.5) is True
    assert _read_config_value("sl.default_pct") == 2.5


def test_apply_config_slippage_does_not_hit_crypto_twin(fake_config):
    assert _apply_config_value("trading.max_slippage_pct", 2.0) is True
    content = fake_config.read_text(encoding="utf-8")
    assert "max_slippage_pct: 2.0" in content
    assert "max_slippage_pct_crypto: 3.0" in content
