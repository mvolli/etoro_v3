"""Unit tests fuer die Movers-Grafik des Hauptkonto-Reports.

Zwei Dinge, die beim ersten Live-Lauf aufgefallen sind:

1. Das Embed enthielt KEINE Grafik — korrekt, weil der Baseline-Lauf keine
   Bewegungen kennt. Der Test unten pinnt, dass die Grafik entsteht, sobald
   welche da sind, damit "kein Chart" nie zur stillen Regression wird.

2. Die Reihenfolge las sich als Zickzack (+6.8 / -5.4 / +4.1 / -3.7 ...),
   weil diff_snapshots nach BETRAG sortiert liefert. Gewuenscht ist eine
   Rangliste: bester Titel oben, schlechtester unten.
"""
from __future__ import annotations

import pytest

from bot.core.candle_chart import movers_bar_png

pytest.importorskip("matplotlib", reason="Chart-Rendering braucht matplotlib")


def _m(sym, pct, delta=0.0):
    return {"symbol": sym, "change_pct": pct, "pnl_delta": delta}


# nach BETRAG sortiert — so liefert diff_snapshots es
NACH_BETRAG = [_m("A", 6.8), _m("B", -5.4), _m("C", 4.1), _m("D", -3.7),
               _m("E", 2.9), _m("F", -1.6), _m("G", 1.2), _m("H", -0.9)]


def _reihenfolge(movers, **kw):
    """Liest die tatsaechlich gezeichnete Reihenfolge aus der Achse."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    erfasst = {}
    orig = plt.subplots

    def spion(*a, **k):
        fig, ax = orig(*a, **k)
        erfasst["ax"] = ax
        return fig, ax

    plt.subplots = spion
    try:
        movers_bar_png(movers, **kw)
    finally:
        plt.subplots = orig
    # barh zeichnet Index 0 unten -> Labels von oben nach unten umdrehen
    return [t.get_text() for t in erfasst["ax"].get_yticklabels()][::-1]


def test_rangliste_von_oben_gewinner_nach_unten_verlierer():
    assert _reihenfolge(NACH_BETRAG) == ["A", "C", "E", "G", "H", "F", "D", "B"]


def test_prozente_fallen_monoton():
    """Der eigentliche Vertrag: keine Zickzack-Sprünge mehr."""
    import matplotlib
    matplotlib.use("Agg")
    reihen = _reihenfolge(NACH_BETRAG)
    werte = {m["symbol"]: m["change_pct"] for m in NACH_BETRAG}
    folge = [werte[s] for s in reihen]
    assert folge == sorted(folge, reverse=True)


def test_beide_enden_kommen_vor_auch_bei_engem_top_n():
    """Bei top_n=4 duerfen nicht nur Gewinner erscheinen — sonst
    verschwaenden an einem guten Tag alle Verluste aus der Grafik."""
    reihen = _reihenfolge(NACH_BETRAG, top_n=4)
    assert len(reihen) == 4
    assert reihen[0] == "A"     # staerkster Gewinner
    assert reihen[-1] == "B"    # staerkster Verlierer


def test_duenne_seite_gibt_platz_ab():
    """Nur ein Verlierer -> die Gewinner duerfen den Rest fuellen."""
    movers = [_m("G1", 5.0), _m("G2", 4.0), _m("G3", 3.0), _m("V1", -1.0)]
    reihen = _reihenfolge(movers, top_n=4)
    assert reihen == ["G1", "G2", "G3", "V1"]


def test_nur_gewinner_bricht_nicht():
    assert _reihenfolge([_m("A", 3.0), _m("B", 1.0)]) == ["A", "B"]


def test_nur_verlierer_bricht_nicht():
    assert _reihenfolge([_m("A", -1.0), _m("B", -3.0)]) == ["A", "B"]


def test_leere_liste_ergibt_keine_grafik():
    """Genau der Baseline-Fall: kein Chart, aber auch kein Absturz."""
    assert movers_bar_png([]) is None


def test_grafik_entsteht_sobald_bewegungen_da_sind():
    png = movers_bar_png(NACH_BETRAG)
    assert png and png[:4] == b"\x89PNG"
