#!/usr/bin/env python3
"""Regressionsschutz — feat/core-sweep-news (2026-08-24).

Vorfall: Am 24.08. kaufte der Core-Sweep NVDA (News-Flag AVOID: Earnings am
26.08.) und JNJ (CAUTION: Talc-Rechtsrisiko) fuer je 162.30 USD. Im
Signal-Pfad waeren beide blockiert bzw. halbiert worden — der Sweep-Pfad
enthielt keine einzige Referenz auf die News-Flags.

Die Pruefung liegt inline in einer sehr langen Funktion, deshalb hier als
Struktur-Test: er haelt fest, DASS der Schutz im Sweep-Block steht.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = (REPO / "src" / "bot" / "workers" / "signal_worker.py").read_text(encoding="utf-8")


def _sweep_block() -> str:
    """Der Core-Sweep-Abschnitt des signal_worker."""
    start = SRC.index("for _o in _sweep_orders:")
    end = SRC.index("Core-Sweep-Pass uebersprungen", start)
    return SRC[start:end]


def test_sweep_reads_news_flags_at_all():
    """Der Sweep-Block hatte vorher NULL Referenzen auf _news_flags."""
    assert "_news_flags" in _sweep_block()


def test_sweep_skips_avoid_flagged_symbols():
    b = _sweep_block()
    assert '"AVOID"' in b
    assert "_cs_news_skipped" in b, "uebersprungene Titel muessen protokolliert werden"


def test_sweep_halves_on_caution():
    """CAUTION halbiert die Groesse, analog zum Signal-Pfad."""
    b = _sweep_block()
    assert '"CAUTION"' in b
    assert "_cs_amt * 0.5" in b


def test_signal_path_still_has_its_own_checks():
    """Die urspruenglichen Pruefungen im Signal-Pfad bleiben unangetastet."""
    assert SRC.count('_nf.get("flag") == "AVOID"') >= 1
    assert SRC.count('_nf.get("flag") == "CAUTION"') >= 1


def test_avoid_is_checked_before_the_dry_run_branch():
    """Ein AVOID-Titel darf nicht einmal als [DRY] Core-Sweep auftauchen."""
    b = _sweep_block()
    assert b.index('"AVOID"') < b.index("[DRY] Core-Sweep")
