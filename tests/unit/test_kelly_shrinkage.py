#!/usr/bin/env python3
"""Unit tests — feat/kelly-shrinkage (2026-08-24).

Die bisherige Logik hatte eine Klippe: ab ``kelly_min_trades`` wurde die
eigene Schaetzung zu 100 % geglaubt, darunter gar nicht. Bei den real
vorkommenden Stichproben (n = 26..73) ist das zu scharf — eine
Trefferquote aus 26 Trades hat noch rund +-10 Prozentpunkte Streuung.

Jetzt Empirical-Bayes-Schrumpfung ueber drei Ebenen (exakte Combo ->
Komponenten-Pool -> Gesamtmittel) mit alpha = k0 / (n + k0).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bot.core.sizing import _kelly_for_signal, _kelly_fraction, DEFAULT_SHRINK_K0

MT = 25


def _rows(spec):
    """spec: [(signal_type, pnl_pct, count), ...] -> Zeilenliste."""
    out = []
    for st, pnl, cnt in spec:
        out.extend([(st, pnl)] * cnt)
    return out


# ─── Datengrundlage bleibt wie bisher ───────────────────────────────────────

def test_returns_none_without_any_rows():
    assert _kelly_for_signal("A", [], MT) is None


def test_returns_none_below_min_trades():
    """Weder exakte Combo noch Pool erreichen min_trades -> keine Skalierung."""
    rows = _rows([("A,B", 2.0, 5), ("C,D", -1.0, 5)])
    assert _kelly_for_signal("A,B", rows, MT) is None


# ─── Schrumpfung: Richtung und Staerke ──────────────────────────────────────

def test_own_estimate_is_pulled_toward_the_pool():
    """Gewinner-Combo in einem verlustreichen Pool wird nach unten gezogen."""
    rows = _rows([("A,B", 5.0, 30),      # eigene Combo: nur Gewinner
                  ("A,C", -5.0, 200)])   # Pool ueber Komponente A: nur Verlierer
    raw = _kelly_fraction([5.0] * 30)
    shrunk = _kelly_for_signal("A,B", rows, MT)
    assert shrunk is not None
    assert shrunk < raw, "die eigene Schaetzung muss gedaempft werden"
    assert shrunk < 1.0


def test_shrinkage_never_raises_a_measured_loser():
    """Einseitigkeit: ein gemessener Verlustbringer wird NICHT rehabilitiert.

    Die Schrumpfung ist ein Sicherheitsabschlag gegen ueberschaetzte Edges.
    In der Gegenrichtung waere sie schaedlich — sie wuerde die Position
    eines belegten Verlierers vergroessern, nur weil das Umfeld besser
    aussieht. Realfall: BB_LOWER+BB_EXTREME mit 1 Gewinner aus 37 Trades
    waere von Faktor 0.150 auf 0.317 gehoben worden.
    """
    rows = _rows([("A,B", -5.0, 30), ("A,C", 5.0, 200)])
    raw = _kelly_fraction([-5.0] * 30)
    shrunk = _kelly_for_signal("A,B", rows, MT)
    assert shrunk == pytest.approx(raw), "darf nicht angehoben werden"


def test_shrinkage_is_never_larger_than_the_raw_estimate():
    """Allgemein: das Ergebnis liegt nie ueber der ungeschrumpften Schaetzung."""
    for spec in ([("A,B", 5.0, 30), ("A,C", -5.0, 200)],
                 [("A,B", -5.0, 30), ("A,C", 5.0, 200)],
                 [("A,B", 3.0, 60), ("A,C", 1.0, 60)],
                 [("A,B", -1.0, 40), ("A,C", -3.0, 90)]):
        rows = _rows(spec)
        own = [p for st, p in rows if st == "A,B"]
        raw = _kelly_fraction(own)
        got = _kelly_for_signal("A,B", rows, MT)
        assert got <= raw + 1e-9, f"{spec}: {got} > {raw}"


def test_more_trades_means_less_shrinkage():
    """Je groesser die eigene Stichprobe, desto naeher am eigenen Wert."""
    def run(n):
        rows = _rows([("A,B", 5.0, n), ("A,C", -5.0, 300)])
        return _kelly_for_signal("A,B", rows, MT)
    assert run(200) > run(50) > run(30), "Vertrauen muss mit n wachsen"


def test_alpha_is_one_half_at_n_equals_k0():
    """Bei n = k0 liegt das Ergebnis genau zwischen eigener Schaetzung und Pool."""
    k0 = 40.0
    n = int(k0)
    rows = _rows([("A,B", 5.0, n), ("A,C", -5.0, 100000)])
    own = _kelly_fraction([5.0] * n)
    got = _kelly_for_signal("A,B", rows, MT, k0)
    # Pool/Gesamt sind bei dieser Uebermacht praktisch -1.0
    expected = 0.5 * own + 0.5 * -1.0
    assert got == pytest.approx(expected, abs=0.02)


def test_shrink_zero_restores_old_behaviour():
    """k0 = 0 schaltet die Schrumpfung ab — die eigene Schaetzung gilt voll."""
    rows = _rows([("A,B", 5.0, 30), ("A,C", -5.0, 200)])
    assert _kelly_for_signal("A,B", rows, MT, 0.0) == pytest.approx(
        _kelly_fraction([5.0] * 30))


# ─── Rueckwaertskompatibilitaet ─────────────────────────────────────────────

def test_uniform_sample_is_unaffected_by_shrinkage():
    """Sind alle Ebenen gleich, ist die Schrumpfung mathematisch wirkungslos.

    Genau deshalb bleiben die bestehenden Sizing-Tests gueltig: dort hat
    jede Mock-Zeile denselben Signaltyp.
    """
    rows = _rows([("A,B", 2.0, 40)])
    assert _kelly_for_signal("A,B", rows, MT) == pytest.approx(1.0)
    rows = _rows([("A,B", -2.0, 40)])
    assert _kelly_for_signal("A,B", rows, MT) == pytest.approx(-1.0)


def test_pool_fallback_still_used_when_exact_is_thin():
    """Duenne exakte Combo -> Pool-Ebene, wie bisher."""
    rows = _rows([("A,B", 2.0, 2), ("A,C", 2.0, 40)])
    got = _kelly_for_signal("A,B", rows, MT)
    assert got == pytest.approx(1.0)


def test_result_stays_within_kelly_bounds():
    for spec in ([("A,B", 9.0, 60), ("A,C", -9.0, 60)],
                 [("A,B", -9.0, 60), ("A,C", 9.0, 60)],
                 [("A,B", 0.5, 30), ("A,C", -0.5, 30)]):
        got = _kelly_for_signal("A,B", _rows(spec), MT)
        assert got is not None
        assert -1.0 <= got <= 1.0


def test_default_k0_is_fifty():
    assert DEFAULT_SHRINK_K0 == 50.0
