#!/usr/bin/env python3
"""feat/conviction-aware-weights (2026-09-11).

Der LLM-Review vom 2026-09-11 daempfte 'TREND_PULLBACK,GOLDEN_CROSS' auf
0.25 und begruendete das woertlich mit

    "HIGH conviction variant has 25.8% win rate and negative avg PnL
     (-1.43%), indicating poor entry quality for THIS SPECIFIC
     conviction level."

Ausdruecken konnte das Schema die Einschraenkung nicht: der Schluessel war
der nackte Signaltyp, und _get_signal_score_multiplier() kannte die
Conviction gar nicht. Gemessen post-Zaesur, realisiert ueber ALLE Tranchen
(realized_by_trade — die Spalte trades.pnl_usd haelt bei gestaffelten
Schliessungen nur die letzte und haette in die Gegenrichtung gezeigt):

    HIGH    n=31   -151.84 USD   Dollar-Trefferquote 41.9 %
    MEDIUM  n=32     +3.75 USD   Dollar-Trefferquote 53.1 %

Beide liefen mit demselben Faktor. `by_conviction` macht die Unterscheidung
darstellbar; ohne den Schluessel bleibt alles exakt wie zuvor.
"""
from __future__ import annotations

import pytest

from bot.workers.signal_worker import (_adj_multiplier,
                                       _get_signal_score_multiplier)

TYP = "TREND_PULLBACK,GOLDEN_CROSS"


@pytest.fixture
def weights():
    return {"adjustments": {
        TYP: {"score_multiplier": 1.0,
              "by_conviction": {"HIGH": 0.25},
              "skip": False, "reason": "nur HIGH ist kaputt"},
    }}


# ── der eigentliche Zweck ───────────────────────────────────────────────────

def test_high_bekommt_die_daempfung(weights):
    assert _get_signal_score_multiplier(TYP, weights, "HIGH") == 0.25


def test_medium_bleibt_unbehelligt(weights):
    """Die Variante, die Geld macht, darf nicht mitgedaempft werden."""
    assert _get_signal_score_multiplier(TYP, weights, "MEDIUM") == 1.0


def test_ohne_conviction_gilt_der_basiswert(weights):
    """Aufrufer ohne Conviction verhalten sich wie bisher."""
    assert _get_signal_score_multiplier(TYP, weights) == 1.0


# ── Rueckwaertskompatibilitaet ──────────────────────────────────────────────

def test_eintrag_ohne_by_conviction_unveraendert():
    """Der Altbestand: eine typweite Daempfung gilt weiter fuer jede Stufe."""
    w = {"adjustments": {TYP: {"score_multiplier": 0.25, "skip": False}}}
    for conv in ("HIGH", "MEDIUM", "LOW", None):
        assert _get_signal_score_multiplier(TYP, w, conv) == 0.25


def test_leere_weights_geben_eins():
    assert _get_signal_score_multiplier(TYP, {}, "HIGH") == 1.0


# ── Never-Boost gilt auch pro Conviction ────────────────────────────────────

def test_by_conviction_kann_nicht_verstaerken():
    """Asymmetrische Rechte: daempfen ja, verstaerken nie — auch hier nicht."""
    w = {"adjustments": {TYP: {"score_multiplier": 0.5,
                               "by_conviction": {"HIGH": 2.0}}}}
    assert _get_signal_score_multiplier(TYP, w, "HIGH") == 1.0


def test_kaputter_wert_faellt_auf_den_basiswert_zurueck():
    w = {"adjustments": {TYP: {"score_multiplier": 0.5,
                               "by_conviction": {"HIGH": "viel"}}}}
    assert _get_signal_score_multiplier(TYP, w, "HIGH") == 0.5


def test_conviction_ist_gross_klein_egal():
    w = {"adjustments": {TYP: {"score_multiplier": 1.0,
                               "by_conviction": {"HIGH": 0.25}}}}
    assert _get_signal_score_multiplier(TYP, w, "high") == 0.25


# ── Combo-Pfade erben die Conviction ────────────────────────────────────────

def test_komponenten_daempfung_ist_conviction_bewusst():
    """Kein Exact-Match: die Einzelkomponenten multiplizieren sich."""
    w = {"adjustments": {
        "TREND_PULLBACK": {"score_multiplier": 1.0,
                           "by_conviction": {"HIGH": 0.5}},
        "GOLDEN_CROSS": {"score_multiplier": 1.0,
                         "by_conviction": {"HIGH": 0.5}},
    }}
    assert _get_signal_score_multiplier(TYP, w, "HIGH") == 0.25
    assert _get_signal_score_multiplier(TYP, w, "MEDIUM") == 1.0


def test_adj_multiplier_direkt():
    assert _adj_multiplier({"score_multiplier": 0.4}, None) == 0.4
    assert _adj_multiplier({"score_multiplier": 0.4,
                            "by_conviction": {"LOW": 0.1}}, "LOW") == 0.1
    assert _adj_multiplier(None, "HIGH") == 1.0


# ── Produzentenseite: der Clamp muss die Ratsche ueberleben ─────────────────

def test_clamp_normalisiert_und_verwirft_unbekanntes():
    from bot.workers.llm_review_worker import _clamp_by_conviction as clamp
    assert clamp({"HIGH": 0.25, "medium": 1.5,
                  "BOGUS": 0.1, "LOW": "x"}) == {"HIGH": 0.25, "MEDIUM": 1.0}
    assert clamp(None) == {}
    assert clamp("kein dict") == {}


def test_ratsche_clampt_by_conviction(monkeypatch):
    """Zwei Schranken hintereinander, beide muessen greifen.

    1. Never-Boost: 3.0 -> 1.0 (wie fuer score_multiplier).
    2. Ratschen-Deckel: danach nicht ueber den CURRENT-Basiswert, sonst
       waere by_conviction ein Schleichweg an der Ratsche vorbei.

    Mit CURRENT 0.4 landen beide Werte bei 0.4 bzw. bleiben darunter.
    """
    import bot.workers.llm_review_worker as lrw
    monkeypatch.setattr(lrw, "_load_signal_weights",
                        lambda: {"adjustments": {TYP: {"score_multiplier": 0.4}}})
    adj = {TYP: {"score_multiplier": 0.4,
                 "by_conviction": {"HIGH": 3.0, "medium": 0.2}}}
    lrw._ratchet_signal_weights(adj, db_path=None)
    assert adj[TYP]["by_conviction"] == {"HIGH": 0.4, "MEDIUM": 0.2}


def test_ratsche_laesst_eintraege_ohne_by_conviction_in_ruhe():
    from bot.workers.llm_review_worker import _ratchet_signal_weights
    adj = {TYP: {"score_multiplier": 0.25}}
    _ratchet_signal_weights(adj, db_path=None)
    assert "by_conviction" not in adj[TYP]


# ── by_conviction darf kein Schleichweg an der Ratsche vorbei sein ──────────

def test_unverdiente_lockerung_per_conviction_wird_gedeckelt(tmp_path, monkeypatch):
    """Die Luecke, die dieses Feature selbst aufgerissen haette.

    Die Ratsche prueft nur score_multiplier. Ein Eintrag
    {"score_multiplier": 0.25, "by_conviction": {"MEDIUM": 1.0}} waere eine
    Vervierfachung der Gewichtung an genau der Stelle vorbei, die
    unverdiente Lockerungen verhindern soll.
    """
    import sqlite3
    from bot.workers.llm_review_worker import _ratchet_signal_weights
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, signal_id INTEGER,"
        " status TEXT, pnl_usd REAL, created_at TEXT);"
        "CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT);")
    con.commit(); con.close()

    import bot.workers.llm_review_worker as lrw
    monkeypatch.setattr(lrw, "_load_signal_weights",
                        lambda: {"adjustments": {TYP: {"score_multiplier": 0.25}}})
    adj = {TYP: {"score_multiplier": 0.25, "by_conviction": {"MEDIUM": 1.0}}}
    _ratchet_signal_weights(adj, db_path=db)
    assert adj[TYP]["by_conviction"]["MEDIUM"] == 0.25, \
        "unverdiente Conviction-Lockerung ist an der Ratsche vorbeigekommen"


def test_strengere_conviction_bleibt_erlaubt(tmp_path, monkeypatch):
    """Die Richtung, die die Ratsche ohnehin nicht schuetzt, bleibt offen."""
    import sqlite3
    from bot.workers.llm_review_worker import _ratchet_signal_weights
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, signal_id INTEGER,"
        " status TEXT, pnl_usd REAL, created_at TEXT);"
        "CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT);")
    con.commit(); con.close()

    import bot.workers.llm_review_worker as lrw
    monkeypatch.setattr(lrw, "_load_signal_weights",
                        lambda: {"adjustments": {TYP: {"score_multiplier": 0.5}}})
    adj = {TYP: {"score_multiplier": 0.5, "by_conviction": {"HIGH": 0.1}}}
    _ratchet_signal_weights(adj, db_path=db)
    assert adj[TYP]["by_conviction"]["HIGH"] == 0.1


# ── Merge: eine gesetzte Conviction-Daempfung darf nicht lautlos wegfallen ──

def test_merge_erhaelt_by_conviction_wenn_die_llm_es_nicht_nennt():
    """Der Merge ersetzt SCHLUESSELWEISE.

    Ein LLM-Vorschlag ohne `by_conviction` loeschte eine bestehende
    Conviction-Daempfung lautlos mit — dieselbe Klasse von stiller
    Lockerung, gegen die fix/llm-weights-merge-keep steht, nur eine Ebene
    tiefer. Nachgestellt wird hier die reine Merge-Semantik.
    """
    current = {TYP: {"score_multiplier": 1.0, "by_conviction": {"HIGH": 0.25}}}
    llm = {TYP: {"score_multiplier": 0.5, "reason": "neu"}}

    merged = dict(current)
    merged.update(llm)
    for sig, cur in current.items():
        if sig in llm and cur.get("by_conviction") and not llm[sig].get("by_conviction"):
            merged[sig]["by_conviction"] = cur["by_conviction"]

    assert merged[TYP]["by_conviction"] == {"HIGH": 0.25}
    assert merged[TYP]["score_multiplier"] == 0.5


def test_llm_darf_by_conviction_ueberschreiben_wenn_sie_es_nennt():
    current = {TYP: {"score_multiplier": 1.0, "by_conviction": {"HIGH": 0.25}}}
    llm = {TYP: {"score_multiplier": 1.0, "by_conviction": {"HIGH": 0.1}}}
    merged = dict(current)
    merged.update(llm)
    assert merged[TYP]["by_conviction"] == {"HIGH": 0.1}


# ── der real gesetzte Stand ─────────────────────────────────────────────────

def test_live_eintrag_gibt_nur_medium_frei():
    """Entscheid VoLLi 2026-09-11: MEDIUM frei, alles andere gedaempft."""
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "data" / "llm_signal_weights.json"
    if not p.exists():
        pytest.skip("keine Live-Gewichte in dieser Umgebung")
    w = json.loads(p.read_text(encoding="utf-8"))
    if TYP not in w.get("adjustments", {}):
        pytest.skip("Eintrag nicht (mehr) vorhanden")
    assert _get_signal_score_multiplier(TYP, w, "MEDIUM") == 1.0
    for conv in ("HIGH", "LOW", "VERY_HIGH"):
        assert _get_signal_score_multiplier(TYP, w, conv) == 0.25
