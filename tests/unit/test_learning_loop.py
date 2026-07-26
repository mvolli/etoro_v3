"""Tests Paket 4 Lernschleife (2026-07-26):

- fix/combo-conviction-min: Combos erben die SCHWAECHSTE Komponenten-Conviction
- fix/kelly-components: Kelly poolt auf Komponenten-Ebene wenn Exact-Match duenn
"""

import sqlite3

from bot.core.signals import generate_signal
from bot.core.sizing import kelly_size_factor


# ── Combo-Conviction = schwaechste Komponente ────────────────────────────────

def test_combo_inherits_weakest_conviction():
    """BB_LOWER (VERY_HIGH) + MACD_TURN (MEDIUM) → Combo MEDIUM, nicht VERY_HIGH."""
    ind = {
        # Rule 1 (VERY_HIGH): bb<0.1, rsi<30, price>sma50, vol_ratio<1.5
        # Rule 4 (MEDIUM): macd_hist improving & <0, price<sma20
        "rsi": 28.0, "macd_hist": -0.2, "macd_hist_prev": -0.4,
        "bb_pct": 0.05, "price": 100.0, "sma20": 102.0, "sma50": 98.0,
        "vol_ratio": 1.0, "atr": 1.0,
    }
    result = generate_signal("TEST", ind)
    types = result.signal_types or []
    assert "BB_LOWER_RSI_OVERSOLD" in types
    assert "MACD_TURN_BELOW_SMA20" in types
    assert result.conviction == "MEDIUM"


def test_single_signal_conviction_unchanged():
    """Einzelsignal: min = max = eigene Conviction (Rule 6 HIGH)."""
    ind = {
        "rsi": 45.0, "macd_hist": 0.5, "macd_hist_prev": 0.4,
        "bb_pct": 0.5, "price": 100.0, "sma20": 100.0, "sma50": 95.0,
        "vol_ratio": 1.0,
    }
    result = generate_signal("TEST", ind)
    assert result.signal_types == ["TREND_PULLBACK"]
    assert result.conviction == "HIGH"


# ── Kelly auf Komponenten-Ebene ──────────────────────────────────────────────

class _FakeDB:
    def __init__(self, rows):
        self._rows = rows  # list of (signal_type, pnl_pct)

    def fetchall(self, sql, params=()):
        return [{"st": st, "pnl_pct": p} for st, p in self._rows]


def test_kelly_exact_match_preferred():
    """Genug exakte Treffer → Komponenten-Pool wird ignoriert."""
    rows = [("A,B", 2.0)] * 12          # 12 exakte, alle Gewinner → 1.5
    rows += [("A,C", -5.0)] * 20        # giftiger Pool — darf nicht zaehlen
    f = kelly_size_factor("A,B", _FakeDB(rows), min_trades=10)
    assert f == 1.5


def test_kelly_falls_back_to_component_pool():
    """Exact-Match zu duenn (n=2) → Komponenten-Pool (n=22) wird genutzt."""
    rows = [("A,B", 2.0)] * 2                    # zu wenig exakt
    rows += [("A,C", -2.0)] * 10 + [("B,D", -3.0)] * 10  # Komponenten-Pool: alles Verlust
    f = kelly_size_factor("A,B", _FakeDB(rows), min_trades=10)
    assert f == 0.3  # Pool ist durchweg negativ → Minimum


def test_kelly_neutral_when_even_pool_too_small():
    rows = [("X,Y", 1.0)] * 3
    f = kelly_size_factor("A,B", _FakeDB(rows), min_trades=10)
    assert f == 1.0


def test_kelly_component_pool_requires_shared_component():
    """Nur Trades mit gemeinsamer Komponente zaehlen in den Pool."""
    rows = [("C,D", -9.0)] * 50  # keinerlei Ueberschneidung mit A,B
    f = kelly_size_factor("A,B", _FakeDB(rows), min_trades=10)
    assert f == 1.0


def test_kelly_db_error_is_neutral():
    class _Broken:
        def fetchall(self, *a, **k):
            raise RuntimeError("boom")

    assert kelly_size_factor("A,B", _Broken()) == 1.0
