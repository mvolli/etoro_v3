#!/usr/bin/env python3
"""Unit tests — feat/full-exit (2026-08-24): Vollausstieg statt Teilverkauf-Kaskade.

Gemessen vor der Umstellung: offene Positionen erreichten im Median 9.9 %
Peak-PnL, realisiert wurden nur 0.27 % (Median der Gewinner). Vier
Teilverkaufsmechanismen nahmen je ~25 % und liessen im Median 11 % der
Einstiegsgroesse uebrig.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import bot.core.trailing_stop as TS


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    """Tests gegen die Modul-Defaults, nicht gegen das repo's config.yaml."""
    monkeypatch.setattr(TS, "FULL_EXIT_ENABLED", True)
    monkeypatch.setattr(TS, "FULL_EXIT_ATR_MULT", 2.0)
    monkeypatch.setattr(TS, "FULL_EXIT_MIN_PCT", 4.0)
    monkeypatch.setattr(TS, "FULL_EXIT_MAX_PCT", 10.0)


# ─── Schwelle ───────────────────────────────────────────────────────────────

def test_threshold_scales_with_atr():
    assert TS.full_exit_threshold(2.5) == pytest.approx(5.0)
    assert TS.full_exit_threshold(3.0) == pytest.approx(6.0)


def test_threshold_is_clamped_at_both_ends():
    assert TS.full_exit_threshold(0.5) == pytest.approx(4.0), "ruhige Titel: Untergrenze"
    assert TS.full_exit_threshold(20.0) == pytest.approx(10.0), "volatile Titel: Obergrenze"


def test_threshold_falls_back_when_atr_missing():
    """Kein ATR bekannt -> Untergrenze x mult, aber geklemmt: nie unter min_pct."""
    for bad in (None, 0.0, -1.0):
        assert TS.full_exit_threshold(bad) >= 4.0


# ─── Ausloesung ─────────────────────────────────────────────────────────────

def test_fires_at_and_above_the_threshold():
    assert TS.should_full_exit(5.0, 2.5) is True
    assert TS.should_full_exit(9.9, 2.5) is True


def test_does_not_fire_below_the_threshold():
    assert TS.should_full_exit(4.99, 2.5) is False
    assert TS.should_full_exit(0.27, 2.5) is False, "der bisherige Median-Gewinn"


def test_does_not_fire_on_losses():
    assert TS.should_full_exit(-3.0, 2.5) is False


def test_disabled_never_fires(monkeypatch):
    monkeypatch.setattr(TS, "FULL_EXIT_ENABLED", False)
    assert TS.should_full_exit(50.0, 2.5) is False


def test_a_volatile_position_needs_a_bigger_move():
    """Gleicher PnL, unterschiedliche Volatilitaet -> unterschiedliche Wertung."""
    assert TS.should_full_exit(6.0, 2.0) is True    # Ziel 4.0
    assert TS.should_full_exit(6.0, 4.0) is False   # Ziel 8.0


# ─── Zusammenspiel mit dem Momentum-Fade ────────────────────────────────────

def test_fade_no_longer_arms_on_noise(monkeypatch):
    """arm_pct 2.0 -> 5.0: ein 2-%-Peak loest keinen Teilverkauf mehr aus."""
    monkeypatch.setattr(TS, "MOMENTUM_FADE_ENABLED", True)
    monkeypatch.setattr(TS, "MOMENTUM_ARM_PCT", 5.0)
    monkeypatch.setattr(TS, "MOMENTUM_MIN_LOCK_PCT", 2.0)
    monkeypatch.setattr(TS, "MOMENTUM_RETRACE_FRAC", 0.30)
    monkeypatch.setattr(TS, "MOMENTUM_MAX_RETRACE_ABS", 4.0)
    # Peak 2 %, PnL faellt auf 1.4 % — frueher ein Fade, jetzt nicht mehr
    assert TS.should_momentum_fade(1.4, 2.0, False) is False
    # Substanzieller Gewinn faedt weiterhin ab
    assert TS.should_momentum_fade(5.0, 10.0, False) is True


def test_full_exit_target_sits_below_typical_peaks():
    """Kalibrierung: 76 % der offenen Positionen erreichten Peak >= 5 %.

    Das Ziel muss in dieser Zone liegen, sonst greift es nie — genau das
    Problem der alten Profit-Leiter, die im Median bei +17 % lag und nie feuerte.
    """
    for atr in (1.0, 2.0, 2.5, 3.0):
        assert 4.0 <= TS.full_exit_threshold(atr) <= 6.0
