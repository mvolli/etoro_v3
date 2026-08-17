"""Tests feat/corporate-action-guard (2026-08-17).

Split-/Sonderdividenden-Artefakte in Yahoo-Kursreihen erkennen und daraus
entstehende BUY-Signale unterdruecken. Anlass: Johnson Matthey (JMAT.L),
Sonderdividende 476,5 p + Zusammenlegung 3-fuer-4 zum 2026-08-17.
"""

import pandas as pd
import pytest

from bot.core import corporate_actions as ca
from bot.core import signals as sig
from bot.core.corporate_actions import (
    _nearest_split_ratio,
    is_corporate_action_artifact,
    scan_price_gaps,
)
from bot.core.signals import compute_indicators, generate_signal


# ── _nearest_split_ratio ─────────────────────────────────────────────────────

def test_ratio_matches_reverse_split():
    assert _nearest_split_ratio(4 / 3) == "4:3"      # JMAT-Zusammenlegung
    assert _nearest_split_ratio(2.0) == "2:1"
    assert _nearest_split_ratio(10.0) == "10:1"


def test_ratio_matches_forward_split():
    assert _nearest_split_ratio(0.5) == "1:2"
    assert _nearest_split_ratio(0.1) == "1:10"


def test_ratio_tolerance_band():
    # 1 % daneben trifft noch, 5 % daneben nicht mehr
    assert _nearest_split_ratio(2.0 * 1.01) == "2:1"
    assert _nearest_split_ratio(2.0 * 1.05) is None


def test_ratio_ignores_ordinary_moves():
    assert _nearest_split_ratio(1.07) is None
    assert _nearest_split_ratio(0.58) is None


# ── scan_price_gaps ──────────────────────────────────────────────────────────

def _make_df(closes, highs=None, lows=None, volumes=None):
    n = len(closes)
    return pd.DataFrame({
        "Open": closes,
        "High": highs or [c * 1.01 for c in closes],
        "Low": lows or [c * 0.99 for c in closes],
        "Close": closes,
        "Volume": volumes or [1_000_000] * n,
    })


def test_no_gap_in_calm_series():
    closes = [100.0 + i * 0.1 for i in range(65)]
    assert scan_price_gaps(_make_df(closes)) == {}


def test_detects_reverse_split_gap():
    # 60 flache Tage, dann Zusammenlegung 4:3 → Kurs springt auf 133.33
    closes = [100.0] * 60 + [133.333, 133.5, 133.2]
    gaps = scan_price_gaps(_make_df(closes))
    assert gaps["ca_gap_split_label"] == "4:3"
    assert gaps["ca_gap_pct"] == pytest.approx(33.33, abs=0.1)
    assert gaps["ca_gap_bars_ago"] == 2
    # Die Sprung-Bar selbst ist normal breit → sie erklaert den Sprung nicht
    assert gaps["ca_gap_explained_frac"] < 0.5


def test_detects_forward_split_gap():
    closes = [200.0] * 60 + [20.0] * 3        # 1:10
    gaps = scan_price_gaps(_make_df(closes))
    assert gaps["ca_gap_split_label"] == "1:10"
    assert gaps["ca_gap_pct"] == pytest.approx(-90.0, abs=0.1)


def test_gap_below_threshold_ignored():
    closes = [100.0] * 60 + [115.0] * 3       # +15 % < CA_MIN_GAP_PCT
    assert scan_price_gaps(_make_df(closes)) == {}


def test_gap_outside_scan_window_ignored():
    # Sprung ganz am Anfang, danach 60 ruhige Bars → ausserhalb CA_SCAN_BARS
    closes = [100.0] * 3 + [200.0] * 60
    assert scan_price_gaps(_make_df(closes)) == {}


def test_largest_gap_wins():
    closes = [100.0] * 20 + [125.0] * 10 + [250.0] * 10
    gaps = scan_price_gaps(_make_df(closes))
    assert gaps["ca_gap_pct"] == pytest.approx(100.0, abs=0.1)


def test_scan_survives_missing_high_low():
    closes = [100.0] * 60 + [200.0] * 3
    df = pd.DataFrame({"Close": closes, "Volume": [1] * len(closes)})
    gaps = scan_price_gaps(df)
    assert gaps["ca_gap_split_label"] == "2:1"
    assert gaps["ca_gap_explained_frac"] is None


# ── is_corporate_action_artifact ─────────────────────────────────────────────

def test_no_artifact_on_empty_indicators():
    assert is_corporate_action_artifact({}) == (False, "")


def test_artifact_on_split_ratio_match():
    hit, reason = is_corporate_action_artifact({
        "ca_gap_pct": 33.3, "ca_gap_split_label": "4:3",
        "ca_gap_bars_ago": 1, "ca_gap_explained_frac": 0.05,
    })
    assert hit and "4:3" in reason


def test_artifact_on_extreme_unexplained_gap():
    # Krummer Faktor (Sonderdividende), kein Ratio-Treffer — Pfad B faengt ihn
    hit, reason = is_corporate_action_artifact({
        "ca_gap_pct": -41.0, "ca_gap_split_label": None,
        "ca_gap_bars_ago": 0, "ca_gap_explained_frac": 0.08,
    })
    assert hit and "unerklaert" in reason


def test_real_crash_with_wide_bar_is_not_an_artifact():
    # -42 %, aber die Bar hat die Strecke selbst ausgehandelt → echter Absturz,
    # dafuer ist das Falling-Knife-Gate zustaendig, nicht dieses hier.
    hit, _ = is_corporate_action_artifact({
        "ca_gap_pct": -42.0, "ca_gap_split_label": None,
        "ca_gap_bars_ago": 0, "ca_gap_explained_frac": 1.0,
    })
    assert hit is False


def test_moderate_gap_without_ratio_match_passes():
    hit, _ = is_corporate_action_artifact({
        "ca_gap_pct": 25.0, "ca_gap_split_label": None,
        "ca_gap_bars_ago": 4, "ca_gap_explained_frac": 0.1,
    })
    assert hit is False


def test_gate_can_be_disabled(monkeypatch):
    monkeypatch.setattr(ca, "CA_GATE_ENABLED", False)
    assert is_corporate_action_artifact({
        "ca_gap_pct": 900.0, "ca_gap_split_label": "10:1",
    })[0] is False


# ── compute_indicators liefert die Metriken ──────────────────────────────────

def test_compute_indicators_exposes_gap_metrics():
    closes = [100.0] * 60 + [133.333, 133.5, 133.2]
    ind = compute_indicators(_make_df(closes))
    assert ind["ca_gap_split_label"] == "4:3"


def test_compute_indicators_omits_keys_without_gap():
    closes = [100.0 + i * 0.1 for i in range(65)]
    ind = compute_indicators(_make_df(closes))
    assert "ca_gap_pct" not in ind


# ── generate_signal: Gate unterdrueckt BUY, nicht SELL ───────────────────────

def _oversold_indicators(**overrides):
    """Indikator-Set, das ohne Gate Rule 3 (RSI_EXTREME_OVERSOLD) feuert."""
    base = {
        "rsi": 20.0,
        "macd_hist": -0.5,
        "macd_hist_prev": -0.4,     # fallend → Rule 4 feuert nicht mit
        "bb_pct": 0.5,
        "price": 100.0,
        "sma20": 101.0,
        "sma50": 99.0,
        "atr": 2.0,
        "vol_ratio": 1.0,
    }
    base.update(overrides)
    return base


def test_buy_fires_without_corporate_action():
    res = generate_signal("TEST", _oversold_indicators())
    assert res.direction == "BUY"


def test_buy_suppressed_on_corporate_action():
    res = generate_signal("JMAT.L", _oversold_indicators(
        ca_gap_pct=33.3, ca_gap_split_label="4:3",
        ca_gap_bars_ago=1, ca_gap_explained_frac=0.05,
    ))
    assert res.direction == "HOLD"
    assert res.signal_types == []


def test_sell_not_suppressed_on_corporate_action():
    res = generate_signal("JMAT.L", _oversold_indicators(
        rsi=80.0, bb_pct=0.99,
        ca_gap_pct=33.3, ca_gap_split_label="4:3",
        ca_gap_bars_ago=1, ca_gap_explained_frac=0.05,
    ))
    assert res.direction == "SELL"


# ── Pfad C: bestaetigte Kapitalmassnahme schlaegt die Heuristik ──────────────

def test_confirmed_action_is_artifact_without_ratio_match():
    """Der JMAT-Fall: -21,9 %, kein glattes Verhaeltnis, unter der Extremschwelle.

    Split (0.75) und Sonderdividende (476,5 p) wirkten gemeinsam — Pfad A und B
    lassen das durch, nur die bestaetigte Historie faengt es.
    """
    ind = {
        "ca_gap_pct": -21.94, "ca_gap_split_label": None,
        "ca_gap_bars_ago": 0, "ca_gap_explained_frac": 0.106,
    }
    assert is_corporate_action_artifact(ind)[0] is False       # Heuristik allein: blind
    ind["ca_confirmed"] = "Split 0.75x am 2026-08-17"
    hit, reason = is_corporate_action_artifact(ind)
    assert hit and "bestaetigte Kapitalmassnahme" in reason


def test_confirmed_action_needs_no_gap_metrics():
    hit, _ = is_corporate_action_artifact({"ca_confirmed": "Split 0.1x am 2026-08-01"})
    assert hit is True


def test_confirmed_action_respects_kill_switch(monkeypatch):
    monkeypatch.setattr(ca, "CA_GATE_ENABLED", False)
    assert is_corporate_action_artifact({"ca_confirmed": "Split 0.1x"})[0] is False


def test_buy_suppressed_on_confirmed_action():
    res = generate_signal("JMAT.L", _oversold_indicators(
        ca_gap_pct=-21.94, ca_gap_split_label=None,
        ca_gap_explained_frac=0.106,
        ca_confirmed="Split 0.75x am 2026-08-17",
    ))
    assert res.direction == "HOLD"


# ── needs_action_confirmation ────────────────────────────────────────────────

def test_confirmation_only_for_symbols_with_a_gap():
    assert ca.needs_action_confirmation({}) is False
    assert ca.needs_action_confirmation({"ca_gap_pct": 5.0}) is False
    assert ca.needs_action_confirmation({"ca_gap_pct": -21.94}) is True


# ── confirm_corporate_action (Netz gestubbt) ─────────────────────────────────

def _stub_yf(monkeypatch, splits=None, dividends=None, date="2026-08-17"):
    idx = pd.to_datetime([date], utc=True)
    empty = pd.Series(dtype=float)

    class _Ticker:
        def __init__(self, sym):
            self.splits = pd.Series([splits], index=idx) if splits is not None else empty
            self.dividends = pd.Series([dividends], index=idx) if dividends is not None else empty

    monkeypatch.setitem(
        __import__("sys").modules, "yfinance", type("M", (), {"Ticker": _Ticker})
    )


def test_confirm_reports_split(monkeypatch):
    _stub_yf(monkeypatch, splits=0.75)
    out = ca.confirm_corporate_action("JMAT.L", price=2282.0)
    assert out is not None and "Split 0.75x" in out


def test_confirm_reports_material_dividend(monkeypatch):
    # 476,5 auf 2282 = 20,9 % vom Kurs → materiell
    _stub_yf(monkeypatch, dividends=476.5)
    out = ca.confirm_corporate_action("JMAT.L", price=2282.0)
    assert out is not None and "20.9%" in out


def test_confirm_ignores_ordinary_dividend(monkeypatch):
    # 0,25 auf 305 = 0,08 % — jeder Quartalszahler. Ohne diesen Filter waere
    # das Gate fuer AAPL & Co. ein Zufallsgenerator.
    _stub_yf(monkeypatch, dividends=0.25)
    assert ca.confirm_corporate_action("AAPL", price=305.0) is None


def test_confirm_ignores_dividend_without_price(monkeypatch):
    # Ohne Kursbezug ist Materialitaet nicht beurteilbar → nicht bestaetigen
    _stub_yf(monkeypatch, dividends=476.5)
    assert ca.confirm_corporate_action("JMAT.L") is None


def test_confirm_ignores_actions_outside_lookback(monkeypatch):
    _stub_yf(monkeypatch, splits=0.5, date="2020-01-02")
    assert ca.confirm_corporate_action("OLD", price=100.0) is None


def test_confirm_returns_none_without_actions(monkeypatch):
    _stub_yf(monkeypatch)
    assert ca.confirm_corporate_action("AAPL", price=305.0) is None


def test_confirm_survives_broken_yfinance(monkeypatch):
    class _Boom:
        def __init__(self, sym):
            raise RuntimeError("Yahoo down")

    monkeypatch.setitem(
        __import__("sys").modules, "yfinance", type("M", (), {"Ticker": _Boom})
    )
    assert ca.confirm_corporate_action("ANY", price=100.0) is None


# ── ConfirmBudget ────────────────────────────────────────────────────────────

def test_budget_skips_symbols_without_gap(monkeypatch):
    _stub_yf(monkeypatch, splits=0.75)
    budget = ca.ConfirmBudget()
    ind = {"price": 100.0}
    assert budget.annotate("JMAT.L", ind) is False
    assert budget.used == 0
    assert "ca_confirmed" not in ind


def test_budget_annotates_and_counts(monkeypatch):
    _stub_yf(monkeypatch, splits=0.75)
    budget = ca.ConfirmBudget()
    ind = {"price": 2282.0, "ca_gap_pct": -21.94}
    assert budget.annotate("JMAT.L", ind) is True
    assert ind["ca_confirmed"].startswith("Split")
    assert (budget.used, budget.hits) == (1, 1)


def test_budget_stops_at_limit(monkeypatch):
    _stub_yf(monkeypatch, splits=0.75)
    budget = ca.ConfirmBudget(limit=2)
    for _ in range(5):
        budget.annotate("X", {"price": 100.0, "ca_gap_pct": -50.0})
    assert budget.used == 2


def test_budget_leaves_indicators_clean_when_nothing_found(monkeypatch):
    _stub_yf(monkeypatch)
    budget = ca.ConfirmBudget()
    ind = {"price": 100.0, "ca_gap_pct": -50.0}
    assert budget.annotate("NOACTION", ind) is False
    assert "ca_confirmed" not in ind
    assert (budget.used, budget.hits) == (1, 0)


# ── Live-Regression 2026-08-17 (erster Produktivlauf des Guards) ─────────────
# Drei Faelle aus dem data_worker-Lauf 12:06. Sie halten die Trennschaerfe
# von Pfad A fest: das Verhaeltnis allein entscheidet NICHT, die Bar-Spanne
# muss den Sprung offenlassen.

def test_live_krs_pence_to_pound_flip():
    """KRS.L: 1.400 -> 0.014, Bar-Spanne exakt 0. GBp/GBP-Umstellung.

    Yahoo kennt zu diesem Datum keinen Split — nur die Heuristik faengt das.
    """
    hit, reason = is_corporate_action_artifact({
        "ca_gap_pct": -99.0, "ca_gap_ratio": 0.01,
        "ca_gap_split_label": "1:100", "ca_gap_bars_ago": 30,
        "ca_gap_explained_frac": 0.0,
    })
    assert hit and "1:100" in reason


def test_live_orca_real_move_is_not_suppressed():
    """ORCA.L: +49,0 % trifft 3:2 auf 0,67 % genau — aber echt gehandelt.

    Eroeffnung = Vortagesschluss (keine Luecke), intraday 13,275 -> 20,00,
    Volumen 426k -> 2,2M. Die Bar erklaert 107,6 % der Bewegung.
    Vor dem Fix hat der Guard hier 10 Wochen lang BUYs blockiert.
    """
    hit, _ = is_corporate_action_artifact({
        "ca_gap_pct": 49.02, "ca_gap_ratio": 1.4902,
        "ca_gap_split_label": "3:2", "ca_gap_bars_ago": 21,
        "ca_gap_explained_frac": 1.076,
    })
    assert hit is False


def test_ratio_match_alone_is_not_enough():
    """Kernregel: glattes Verhaeltnis + ausgehandelte Spanne = kein Artefakt."""
    base = {
        "ca_gap_pct": -50.0, "ca_gap_ratio": 0.5,
        "ca_gap_split_label": "1:2", "ca_gap_bars_ago": 3,
    }
    assert is_corporate_action_artifact({**base, "ca_gap_explained_frac": 0.9})[0] is False
    assert is_corporate_action_artifact({**base, "ca_gap_explained_frac": 0.1})[0] is True


def test_missing_bar_range_counts_as_teleport():
    """Ohne High/Low bleibt es beim Verdacht — Folge ist nur ein BUY weniger."""
    hit, _ = is_corporate_action_artifact({
        "ca_gap_pct": -50.0, "ca_gap_split_label": "1:2",
        "ca_gap_bars_ago": 3, "ca_gap_explained_frac": None,
    })
    assert hit is True


def test_confirmed_action_ignores_bar_range():
    """Pfad C ist Grundwahrheit — eine weite Bar hebt sie nicht auf."""
    hit, _ = is_corporate_action_artifact({
        "ca_gap_pct": 49.0, "ca_gap_split_label": "3:2",
        "ca_gap_explained_frac": 1.076,
        "ca_confirmed": "Split 1.5x am 2026-08-17",
    })
    assert hit is True
