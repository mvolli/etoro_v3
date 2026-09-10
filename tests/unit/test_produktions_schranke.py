#!/usr/bin/env python3
"""fix/tests-schreiben-in-produktion (2026-09-10) — die Schranke selbst.

Zwei Lecks liefen wochenlang unbemerkt, weil beide Male eine Fixture nach
NAMEN stumm schaltete und den falschen Namen erwischte bzw. eine zweite
Konstante uebersah:

  test_llm_tighten_remaining  540 Zeilen in system_log + ebenso viele echte
                              Posts nach #etoro-trades
  test_llm_advisors           ueberschrieb llm_decision_log.json bei jedem
                              Lauf; 110 echte Eintraege mussten aus der
                              Git-Historie zurueckgeholt werden

Die Schranke in conftest.py setzt deshalb am Systemaufruf an statt am
Namen. Diese Tests halten fest, dass sie greift — und dass Lesen erlaubt
bleibt.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

PROD = (Path(__file__).resolve().parents[2] / "data").resolve()
LIVE_DB = PROD / "trading.db"


def test_schreibendes_open_wird_abgefangen():
    with pytest.raises(AssertionError, match="Produktionsdaten"):
        open(PROD / "darf_nicht_entstehen.json", "w")
    assert not (PROD / "darf_nicht_entstehen.json").exists()


def test_write_text_wird_abgefangen():
    with pytest.raises(AssertionError, match="Produktionsdaten"):
        (PROD / "darf_nicht_entstehen.txt").write_text("x")
    assert not (PROD / "darf_nicht_entstehen.txt").exists()


def test_sqlite_schreibverbindung_wird_abgefangen():
    if not LIVE_DB.exists():
        pytest.skip("keine Produktions-DB in dieser Umgebung")
    with pytest.raises(AssertionError, match="Produktionsdaten"):
        sqlite3.connect(str(LIVE_DB))


def test_lesen_bleibt_erlaubt():
    """Fixtures, die legitim aus der Produktion LESEN, duerfen nicht brechen."""
    if not LIVE_DB.exists():
        pytest.skip("keine Produktions-DB in dieser Umgebung")
    con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    con.execute("SELECT 1").fetchone()
    con.close()


def test_tmp_path_bleibt_unberuehrt(tmp_path):
    """Die Schranke gilt nur fuer data/ — sonst waere jeder Test lahmgelegt."""
    z = tmp_path / "ok.json"
    z.write_text("{}")
    assert z.read_text() == "{}"
