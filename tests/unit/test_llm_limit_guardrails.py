#!/usr/bin/env python3
"""Unit tests — feat/llm-limit-guardrails (2026-10-16, VoLLi).

Die LLM darf Regime- und Verlustlimits weiter nachjustieren, aber:
Grenzen klammern VoLLis Werte ein (Woche 15 %, Monat 45 %), je Aenderung nur
ein kleiner Schritt, und jede Aenderung an risk.*/regime.* wird in Discord
gemeldet. Anlass: 09.07. regime.caution_pct 4 -> 3 unbemerkt.
"""
from __future__ import annotations

import pytest

from bot.workers import llm_review_worker as lr

CFG = """risk:
  daily_loss_limit_pct: 5.0   # Kommentar bleibt
  weekly_loss_limit_pct: 15.0 # Kommentar bleibt
  monthly_loss_limit_pct: 45.0 # Kommentar bleibt
sl:
  default_pct: 3.0
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CFG, encoding="utf-8")
    posts = []
    monkeypatch.setattr(lr, "CONFIG_YAML_PATH", cfg)
    monkeypatch.setattr(lr, "_load_decision_log", lambda: [])
    monkeypatch.setattr(lr, "_record_decision", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_active_experiment_param", lambda: None)
    monkeypatch.setattr(lr, "_discord", lambda fn, **kw: posts.append((fn, kw)))
    return cfg, posts


def test_volli_werte_liegen_in_den_grenzen():
    assert lr._validate_config_adjustment("risk.monthly_loss_limit_pct", 45.0)[0]
    assert lr._validate_config_adjustment("risk.weekly_loss_limit_pct", 15.0)[0]


def test_llm_kann_monat_nicht_unter_konto_stufen_druecken():
    ok, msg, _ = lr._validate_config_adjustment("risk.monthly_loss_limit_pct", 25.0)
    assert not ok and "Bible-Min" in msg
    ok, _, _ = lr._validate_config_adjustment("risk.weekly_loss_limit_pct", 8.0)
    assert not ok


def test_kleiner_schritt_wird_angewendet_und_gemeldet(env):
    cfg, posts = env
    applied = lr._update_config_yaml(
        {"risk.monthly_loss_limit_pct": {"value": 48.0, "reason": "Messung"}})
    assert applied and "monthly_loss_limit_pct: 48.0" in cfg.read_text()
    assert "# Kommentar bleibt" in cfg.read_text()
    assert posts and posts[0][0] == "post_alert_embed"
    assert "risk.monthly_loss_limit_pct" in posts[0][1]["description"]


def test_grosser_schritt_wird_abgelehnt(env):
    cfg, posts = env
    applied = lr._update_config_yaml(
        {"risk.monthly_loss_limit_pct": {"value": 38.0, "reason": "zu weit"}})
    assert applied == []
    assert "monthly_loss_limit_pct: 45.0" in cfg.read_text()
    assert posts == []


def test_tagesschritt_max_ein_prozentpunkt(env):
    cfg, _ = env
    assert lr._update_config_yaml({"risk.daily_loss_limit_pct": {"value": 7.0, "reason": "x"}}) == []
    assert lr._update_config_yaml({"risk.daily_loss_limit_pct": {"value": 6.0, "reason": "x"}})


def test_andere_keys_ohne_meldung(env):
    cfg, posts = env
    assert lr._update_config_yaml({"sl.default_pct": {"value": 3.5, "reason": "x"}})
    assert posts == []


def test_prompt_erklaert_neue_bedeutung():
    import inspect
    src = inspect.getsource(lr)
    assert "KONTO-Bremsstufen" in src and "20 / 30 / 40" in src
