#!/usr/bin/env python3
"""Unit tests — backtest/autoresearch.py (Karpathy-Loop-Bewertung).

Quelle: Moon-Dev-Video "Andrej Karpathy's AI Trading Loop" (2026-09-09).
Uebernommen sind Bewertung (K), Guards und Lock-Window — NICHT die
44.975 %, die das Terminal im Video selbst als "in-sample only" nach 95
Durchlaeufen ausweist.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backtest"))

from autoresearch import (
    MIN_TRADES, check_guards, degradation_pct, k_score, split_trades,
)


# ── K = ln(1 + ROI) * Sharpe ─────────────────────────────────────────────────

@pytest.mark.parametrize("roi,sharpe,erwartet", [
    (44975, 1.810, 11.06),   # iter 95 — der "keeper" aus dem Video
    (43816, 1.806, 10.99),   # iter 80
    (37997, 1.779, 10.57),   # iter 75
    (30937, 1.772, 10.17),   # iter 66 — Platz 10
])
def test_k_gegen_die_veroeffentlichten_zahlen(roi, sharpe, erwartet):
    """Die Formel ist gegen die Top-10-Tabelle verifiziert, nicht geraten."""
    assert k_score(roi, sharpe) == pytest.approx(erwartet, abs=0.01)


def test_negativer_sharpe_macht_k_negativ():
    """keeper-or-revert: eine Variante mit negativem Sharpe faellt raus."""
    assert k_score(50.0, -0.8) < 0


def test_verlust_senkt_k_unter_null():
    assert k_score(-30.0, 1.5) < 0


def test_totalverlust_hat_kein_k():
    """ln(0) ist nicht definiert — None statt Ausnahme."""
    assert k_score(-100.0, 1.5) is None
    assert k_score(-150.0, 1.5) is None


@pytest.mark.parametrize("roi,sh", [(None, 1.0), (10.0, None), (None, None)])
def test_fehlende_eingaben(roi, sh):
    assert k_score(roi, sh) is None


def test_muell_wirft_nicht():
    assert k_score("viel", "gut") is None


# ── Guards ───────────────────────────────────────────────────────────────────

def test_zu_wenige_trades_fallen_raus():
    ok, gruende = check_guards(12, None)
    assert ok is False and "12" in gruende[0]


def test_genau_die_schwelle_besteht():
    assert check_guards(MIN_TRADES, None)[0] is True


def test_vol_guard_erkennt_den_hebel_regler():
    """"Bigger bets raises return on the exact same trades." """
    ok, gruende = check_guards(100, 140.0, baseline_vol_pct=116.0)
    assert ok is False
    assert "Hebel" in gruende[0]


def test_vol_im_band_besteht():
    """Im Video bleibt Vol% ueber die Top 10 zwischen 107.6 und 116.9."""
    assert check_guards(100, 107.6, baseline_vol_pct=116.9)[0] is True


def test_ohne_baseline_kein_vol_urteil():
    assert check_guards(100, 999.0)[0] is True


def test_beide_guards_melden_gemeinsam():
    ok, gruende = check_guards(5, 200.0, baseline_vol_pct=100.0)
    assert ok is False and len(gruende) == 2


# ── Lock-Window ──────────────────────────────────────────────────────────────

def _t(d):
    return {"entry_date": d}


def test_teilung_am_einstiegsdatum():
    trades = [_t("2026-01-15"), _t("2026-03-31"), _t("2026-04-01"), _t("2026-06-01")]
    ins, oos = split_trades(trades, "2026-04-01")
    assert len(ins) == 2 and len(oos) == 2
    assert oos[0]["entry_date"] == "2026-04-01", "Grenztag gehoert ins OOS"


def test_ohne_lock_window_ist_alles_in_sample():
    trades = [_t("2026-01-15"), _t("2026-06-01")]
    ins, oos = split_trades(trades, None)
    assert len(ins) == 2 and oos == []


def test_fehlendes_datum_landet_in_sample():
    ins, oos = split_trades([{"entry_date": None}], "2026-04-01")
    assert len(ins) == 1


# ── Degradation ──────────────────────────────────────────────────────────────

def test_ueberlebensanteil():
    assert degradation_pct(18.26, 0.74) == pytest.approx(4.05, abs=0.1)


def test_negative_basis_ergibt_keine_aussage():
    """-1.54 / -0.51 waere "302 % ueberlebt" — eine Kante, die es nicht gab,
    kann nicht ueberleben."""
    assert degradation_pct(-0.51, -1.54) is None


def test_nullbasis_ergibt_keine_aussage():
    assert degradation_pct(0.0, 5.0) is None


# ── fix/k-sign (2026-09-09) ──────────────────────────────────────────────────
# ln(1+r) * sharpe wird bei Verlust UND negativem Sharpe wieder positiv.
# Im eigenen Lauf: V2 out-of-sample -1.54 % bei Sharpe -0.06 -> K +0.001,
# Urteil "keeper" fuer eine Variante, die in beiden Fenstern verliert.

def test_verlust_mit_negativem_sharpe_bleibt_negativ():
    """Der eigentliche Fund: minus mal minus darf kein keeper werden."""
    k = k_score(-1.54, -0.06)
    assert k is not None and k < 0, f"K={k} — Verlierer darf nicht positiv sein"


def test_verlust_mit_positivem_sharpe_bleibt_negativ():
    assert k_score(-5.0, 0.9) < 0


def test_gewinn_mit_negativem_sharpe_bleibt_negativ():
    assert k_score(20.0, -0.5) < 0


def test_gewinn_mit_positivem_sharpe_bleibt_positiv():
    assert k_score(20.0, 0.5) > 0


def test_rangfolge_unter_verlierern_bleibt_erhalten():
    """Groessenordnung ueberlebt das Vorzeichen — der schlimmere ist kleiner."""
    schlimm = k_score(-20.0, -1.0)
    milde = k_score(-2.0, -0.1)
    assert schlimm < milde < 0


def test_die_veroeffentlichten_werte_sind_unberuehrt():
    """Der Fix darf die verifizierten Gewinner-Werte nicht verschieben."""
    assert k_score(44975, 1.810) == pytest.approx(11.06, abs=0.01)
