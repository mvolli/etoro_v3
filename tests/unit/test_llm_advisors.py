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


# ── Diversity-Kategorien: Komma-Kombos (fix/diversity-combo-types) ───────────

from bot.workers.signal_worker import _get_signal_category


def test_category_single_type():
    assert _get_signal_category('GOLDEN_CROSS') == 'TREND_FOLLOWING'
    assert _get_signal_category('RSI_EXTREME_OVERSOLD') == 'MEAN_REVERSION'


def test_category_combo_same_family():
    assert _get_signal_category('TREND_PULLBACK,GOLDEN_CROSS') == 'TREND_FOLLOWING'
    assert _get_signal_category('RSI_EXTREME_OVERSOLD,BB_LOW_MACD_IMPROVING') == 'MEAN_REVERSION'


def test_category_combo_mixed_families():
    assert _get_signal_category('RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20') == 'MIXED'


def test_category_unknown_and_empty():
    assert _get_signal_category('BRANDNEW_SIGNAL') == 'UNKNOWN'
    assert _get_signal_category('') == 'UNKNOWN'
    assert _get_signal_category('BRANDNEW,GOLDEN_CROSS') == 'TREND_FOLLOWING'


# ── Review-Runde 2: Deadline + Config-Eindeutigkeit + atomarer Write ─────────

from datetime import datetime, timezone

from bot.workers.trade_veto_worker import _seconds_until_execution


def test_deadline_at_04_is_120s():
    now = datetime(2026, 7, 14, 10, 4, 0, tzinfo=timezone.utc)
    assert _seconds_until_execution(now) == 120.0


def test_deadline_at_19_30_is_90s():
    now = datetime(2026, 7, 14, 10, 19, 30, tzinfo=timezone.utc)
    assert _seconds_until_execution(now) == 90.0


def test_deadline_exactly_at_slot_rolls_to_next():
    now = datetime(2026, 7, 14, 10, 6, 0, tzinfo=timezone.utc)
    assert _seconds_until_execution(now) == 900.0


def test_read_config_value_rejects_duplicate_key(fake_config):
    fake_config.write_text(
        fake_config.read_text(encoding='utf-8') + 'extra:\n  default_pct: 9.9\n',
        encoding='utf-8',
    )
    assert _read_config_value('sl.default_pct') is None
    assert _apply_config_value('sl.default_pct', 3.0) is False


def test_commented_key_does_not_confuse_regex(fake_config):
    fake_config.write_text(
        '# default_pct: 99.0  # alter Wert, kommentiert\n'
        + fake_config.read_text(encoding='utf-8'),
        encoding='utf-8',
    )
    assert _read_config_value('sl.default_pct') == 2.5
    assert _apply_config_value('sl.default_pct', 3.0) is True
    assert _read_config_value('sl.default_pct') == 3.0


# ── Tier-1: yfinance-Symbol aus instruments-Tabelle (fix/tier1-yfinance-symbol) ──

from bot.workers.data_worker import _get_portfolio_symbols


def test_tier1_resolves_yfinance_symbol_from_db(tmp_path):
    db = DB(db_path=tmp_path / 'p.db')
    db.execute('CREATE TABLE portfolio_snapshot (api_position_id TEXT, symbol TEXT, instrument_id INTEGER)')
    db.execute('CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT, yfinance_symbol TEXT)')
    db.execute("INSERT INTO instruments VALUES (5804, 'ALC.ZU', 'ALC.SW')")
    db.execute("INSERT INTO instruments VALUES (7111, 'PXA.ASX', 'PXA.AX')")
    db.execute("INSERT INTO instruments VALUES (9999, 'AAPL', NULL)")
    db.execute("INSERT INTO portfolio_snapshot VALUES ('a1', 'ALC.ZU', 5804)")
    db.execute("INSERT INTO portfolio_snapshot VALUES ('a2', 'PXA.ASX', 7111)")
    db.execute("INSERT INTO portfolio_snapshot VALUES ('a3', 'AAPL', 9999)")
    items = {i['symbol']: i for i in _get_portfolio_symbols(db)}
    assert items['ALC.ZU']['yf_symbol'] == 'ALC.SW'
    assert items['PXA.ASX']['yf_symbol'] == 'PXA.AX'
    assert items['AAPL']['yf_symbol'] == 'AAPL'   # NULL → Alias-Fallback
    assert items['ALC.ZU']['instrument_id'] == 5804


# ── Correlation-Gate: yfinance-Symbol-Aufloesung (fix/correlation-yf-symbol) ──

import sqlite3 as _sqlite3

from bot.core.correlation import _resolve_yf_symbols


def test_correlation_resolves_yf_symbols(tmp_path):
    conn = _sqlite3.connect(tmp_path / 'c.db')
    conn.execute('CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT, yfinance_symbol TEXT)')
    conn.execute("INSERT INTO instruments VALUES (5804, 'ALC.ZU', 'ALC.SW')")
    conn.execute("INSERT INTO instruments VALUES (3000, 'SPY', 'SPY')")
    conn.execute("INSERT INTO instruments VALUES (12665, 'SHUR.BR', NULL)")
    m = _resolve_yf_symbols(conn, ['ALC.ZU', 'SPY', 'SHUR.BR', 'UNBEKANNT'])
    assert m['ALC.ZU'] == 'ALC.SW'
    assert m['SPY'] == 'SPY'
    assert m['SHUR.BR'] == 'SHUR.BR'     # NULL → Rohsymbol (fail-open)
    assert m['UNBEKANNT'] == 'UNBEKANNT'  # nicht in DB → Rohsymbol


# ── Partial-Close-Sicherheit (fix/tighten-full-close, fix/stale-price-trailing) ──

from types import SimpleNamespace

from bot.core.trailing_stop import _action_market_open


def test_action_market_open_crypto_always_true(tmp_path):
    db = DB(db_path=tmp_path / 'm.db')
    db.execute('CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT, yfinance_symbol TEXT, asset_class TEXT)')
    db.execute("INSERT INTO instruments VALUES (100000, 'BTC', 'BTC-USD', 'crypto')")
    action = SimpleNamespace(symbol='BTC', instrument_id=100000)
    assert _action_market_open(db, action) is True


def test_action_market_open_fails_open_without_db():
    action = SimpleNamespace(symbol='SOMESTOCK', instrument_id=1)
    # Ohne DB: Aufloesung scheitert still, Ergebnis haengt nur von
    # market_hours ab — darf jedenfalls nicht crashen
    assert _action_market_open(None, action) in (True, False)


def test_get_position_units_parses_client_portfolio():
    from bot.api.client import EToroClient
    client = EToroClient.__new__(EToroClient)  # ohne __init__ (kein API-Setup)
    client.get_portfolio = lambda: {'clientPortfolio': {'positions': [
        {'positionID': 3513174003, 'units': 4.87677},
        {'positionID': 111, 'units': 0},
    ]}}
    assert EToroClient.get_position_units(client, '3513174003') == 4.87677
    assert EToroClient.get_position_units(client, 111) is None      # 0 units
    assert EToroClient.get_position_units(client, 999) is None      # unbekannt


def test_get_position_units_fails_open_on_api_error():
    from bot.api.client import EToroClient
    client = EToroClient.__new__(EToroClient)
    def _boom():
        raise RuntimeError('API down')
    client.get_portfolio = _boom
    assert EToroClient.get_position_units(client, 1) is None


# ── Mehrheitsregel fuer Diversity-Kategorien (fix/diversity-majority) ────────

def test_category_majority_mr_wins():
    # 2x Mean-Reversion + 1x Trend-Following → MR-Entry mit Trend-Bestaetigung
    assert _get_signal_category(
        'RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING'
    ) == 'MEAN_REVERSION'
    # 3x MR + 1x TF
    assert _get_signal_category(
        'BB_LOWER_RSI_OVERSOLD,RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING'
    ) == 'MEAN_REVERSION'


def test_category_tie_stays_mixed():
    assert _get_signal_category('RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20') == 'MIXED'


# ── Deployment-Boost-Gates (fix/cash-deployment 2026-07-15) ──────────────────

from bot.workers.signal_worker import _deployment_boost_applies


def test_deployment_boost_all_conditions_met():
    assert _deployment_boost_applies(35.0, 30.0, 'NORMAL', 1.0, False) is True


def test_deployment_boost_blocked_individually():
    assert _deployment_boost_applies(25.0, 30.0, 'NORMAL', 1.0, False) is False
    assert _deployment_boost_applies(35.0, 30.0, 'CAUTION', 1.0, False) is False
    assert _deployment_boost_applies(35.0, 30.0, 'NORMAL', 0.85, False) is False
    assert _deployment_boost_applies(35.0, 30.0, 'NORMAL', 1.0, True) is False


# ── Sizing-Ladder-Guard (fix/sizing-ladder-guard 2026-07-16) ─────────────────

from bot.workers.llm_review_worker import _ladder_violation, _read_sizing_ladder

_LADDER_OK = {
    "sizing.very_high_pct": 8.0, "sizing.high_pct": 7.0,
    "sizing.medium_pct": 5.0, "sizing.low_pct": 2.0,
}


def test_ladder_guard_blocks_inversion():
    # Der reale Vorfall: very_high 8 -> 6 bei high=7
    v = _ladder_violation("sizing.very_high_pct", 6.0, _LADDER_OK)
    assert v is not None and "invertiert" in v


def test_ladder_guard_allows_monotone_changes():
    assert _ladder_violation("sizing.very_high_pct", 10.0, _LADDER_OK) is None
    assert _ladder_violation("sizing.medium_pct", 3.0, _LADDER_OK) is None
    # Absenkung auf Gleichstand ist erlaubt (>=, nicht >)
    assert _ladder_violation("sizing.very_high_pct", 7.0, _LADDER_OK) is None


def test_ladder_guard_ignores_other_keys_and_incomplete():
    assert _ladder_violation("sl.default_pct", 99.0, _LADDER_OK) is None
    incomplete = dict(_LADDER_OK, **{"sizing.low_pct": None})
    assert _ladder_violation("sizing.very_high_pct", 1.0, incomplete) is None


def test_read_sizing_ladder_parses_yaml_text():
    content = (
        "sizing:\n"
        "  very_high_pct: 8.0          # kommentar\n"
        "  high_pct: 7.0\n"
        "  medium_pct: 5.0\n"
        "  low_pct: 2.0\n"
    )
    vals = _read_sizing_ladder(content)
    assert vals["sizing.very_high_pct"] == 8.0
    assert vals["sizing.low_pct"] == 2.0
