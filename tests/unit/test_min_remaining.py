#!/usr/bin/env python3
"""Unit tests — feat/min-remaining (2026-08-24).

Die Profit-Leiter bleibt erhalten, darf eine Position aber nicht zerfasern.
Wuerde ein Teilverkauf die Restposition unter MIN_REMAINING_PCT druecken,
wird stattdessen ganz geschlossen.

Hintergrund: close_pct wirkt auf die AKTUELLE Groesse, die Kette lautet also
100 -> 75 -> 56 -> 42 %. Gemessen vor der Regel: Median 11 % Restposition bei
9.9 % Median-Peak-PnL.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import bot.core.trailing_stop as TS


@pytest.fixture(autouse=True)
def _floor_50(monkeypatch):
    monkeypatch.setattr(TS, "MIN_REMAINING_PCT", 50.0)


def test_first_two_partials_are_allowed():
    """Die Leiter darf arbeiten: 100 -> 75 -> 56 %."""
    assert TS.would_breach_min_remaining(1.00, 25.0) is False
    assert TS.would_breach_min_remaining(0.75, 25.0) is False


def test_third_partial_becomes_a_full_exit():
    """56.25 % x 0.75 = 42.2 % — unter der Marke, also ganz raus."""
    assert TS.would_breach_min_remaining(0.5625, 25.0) is True


def test_position_already_at_or_below_floor_exits_fully():
    """Eine bereits zerfaserte Position wird nicht weiter zersplittert."""
    assert TS.would_breach_min_remaining(0.50, 25.0) is True
    assert TS.would_breach_min_remaining(0.30, 10.0) is True
    assert TS.would_breach_min_remaining(0.11, 5.0) is True, "der alte Median-Rest"


def test_exactly_hitting_the_floor_is_still_allowed():
    """Genau 50 % ist die Marke, nicht darunter — der Teilverkauf darf laufen."""
    assert TS.would_breach_min_remaining(1.00, 50.0) is False


def test_a_large_partial_can_trigger_the_exit_immediately():
    assert TS.would_breach_min_remaining(0.75, 50.0) is True


def test_rule_is_disabled_at_zero(monkeypatch):
    monkeypatch.setattr(TS, "MIN_REMAINING_PCT", 0.0)
    assert TS.would_breach_min_remaining(0.05, 90.0) is False


def test_floor_is_configurable(monkeypatch):
    monkeypatch.setattr(TS, "MIN_REMAINING_PCT", 75.0)
    assert TS.would_breach_min_remaining(1.00, 25.0) is False   # -> 75 %, genau die Marke
    assert TS.would_breach_min_remaining(0.75, 25.0) is True    # -> 56 %, darunter


def test_at_most_two_partials_before_full_exit():
    """Gesamtverhalten: die Leiter nimmt hoechstens zweimal 25 %."""
    remaining, partials = 1.0, 0
    for _ in range(10):
        if TS.would_breach_min_remaining(remaining, 25.0):
            break
        remaining *= 0.75
        partials += 1
    assert partials == 2
    assert remaining == pytest.approx(0.5625)


def test_full_exit_trigger_is_off_by_default_in_shipped_config():
    """Regressionsschutz: das ATR-Ziel wuerde die Leiter aushebeln.

    Mit full_exit.enabled=true feuert der Vollausstieg schon bei 4-6 %, also
    vor der ersten Leiterstufe (~6 %) — die Leiter waere praktisch stillgelegt.
    Die Untergrenze uebernimmt den Vollausstieg stattdessen ohne diesen Effekt.
    """
    import yaml
    repo = Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((repo / "config" / "config.yaml").read_text(encoding="utf-8"))
    tr = cfg["trailing"]
    assert tr["full_exit"]["enabled"] is False
    assert float(tr["min_remaining_pct"]) > 0.0
