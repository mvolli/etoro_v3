"""Unit tests fuer fix/core-sweep-portfolio-gates (2026-08-12).

Core-Sweep rief NIE check_buy_gate und pruefte damit ausschliesslich Cash- und
Einzeltitel-Grenzen — keine einzige Pruefung betrachtete das Portfolio als
Ganzes. Belegt am 2026-08-12 05:19:29: derselbe signal_worker-Lauf blockte
ROVI.MC mit "Exposure-Gate: 79.5% > 75% Max" und eroeffnete zeitgleich
2883.HK ($345.90) via Core-Sweep.

Getestet werden die beiden neuen Portfolio-Grenzen:
  1. exposure_headroom deckelt `deployable` (Sizing, NICHT Binaer-Block)
  2. correlation_gate filtert gleichlaufende Kandidaten (injiziert, fail-open)

Plus die Rueckwaerts-Kompatibilitaet: ohne die neuen Argumente muss sich die
Planung exakt wie vorher verhalten.
"""
from __future__ import annotations

from bot.core.core_sweep import plan_core_sweep


def _cfg(**over) -> dict:
    block = {
        "enabled": True,
        "reserve_target_pct": 15.0,
        "reserve_floor_pct": 10.0,
        "per_position_pct": 4.0,
        "max_position_pct": 6.0,
        "max_sweeps_per_run": 4,
        "rsi_overbought": 75.0,
        "regimes": ["NORMAL", "CAUTION"],
        "whitelist": {"SPY": 3000, "AAPL": 1001, "MSFT": 1004, "AMZN": 1005},
    }
    block.update(over)
    return {"trading": {"core_sweep": block}}


# Equity 10k, Cash 3k → Ueberschuss ueber Reserve 1.5k, Tranche 400.
_EQUITY = 10_000.0
_CASH = 3_000.0


# ── Exposure-Headroom ─────────────────────────────────────────────────────────

def test_ohne_exposure_argumente_unveraendertes_verhalten():
    """Rueckwaerts-Kompatibilitaet: alte Aufrufer bekommen das alte Ergebnis."""
    orders, _ = plan_core_sweep(_cfg(), equity=_EQUITY, cash=_CASH, regime="NORMAL")
    # deployable = min(Cash-Reserve_target, Cash-Reserve_floor) = 1500 → 3 Tranchen a 400
    assert len(orders) == 3
    assert all(o.amount_usd == 400.0 for o in orders)


def test_headroom_deckelt_tranchenzahl_statt_alles_zu_blocken():
    """Kernverhalten: knapper Headroom → WENIGER Sweeps, nicht null.

    Das ist der autonomie-erhaltende Teil — der Bot regelt die Groesse selbst
    herunter und bleibt handlungsfaehig, statt stumm zu stoppen.
    """
    # Cap 75% von 10k = 7500; bereits 6600 investiert → Headroom 900 = 2 Tranchen
    orders, reasons = plan_core_sweep(
        _cfg(), equity=_EQUITY, cash=_CASH, regime="NORMAL",
        total_exposed=6_600.0, max_exposure_pct=75.0,
    )
    assert len(orders) == 2
    assert sum(o.amount_usd for o in orders) <= 900.0
    assert any("Exposure" in r for r in reasons)


def test_headroom_erschoepft_keine_orders_und_eigene_begruendung():
    """Am Cap: kein Sweep — und die Meldung nennt Exposure, nicht 'kein Cash'.

    Der Live-Zustand am 2026-08-12: 81.9% Exposure bei $1.413 freiem Cash.
    Ohne eigene Meldung sucht der Operator den Fehler beim Cash.
    """
    orders, reasons = plan_core_sweep(
        _cfg(), equity=_EQUITY, cash=_CASH, regime="NORMAL",
        total_exposed=8_190.0, max_exposure_pct=75.0,
    )
    assert orders == []
    joined = " ".join(reasons)
    assert "Exposure" in joined and "Cap" in joined
    assert "kein Ueberschuss" not in joined


def test_negativer_headroom_bricht_nicht():
    """Exposure ueber dem Cap darf keine negativen Groessen erzeugen."""
    orders, reasons = plan_core_sweep(
        _cfg(), equity=_EQUITY, cash=_CASH, regime="NORMAL",
        total_exposed=9_500.0, max_exposure_pct=75.0,
    )
    assert orders == []
    assert all("-$" not in r for r in reasons)


def test_headroom_groesser_als_cash_aendert_nichts():
    """Viel Luft im Portfolio → Cash bleibt die bindende Grenze."""
    orders, _ = plan_core_sweep(
        _cfg(), equity=_EQUITY, cash=_CASH, regime="NORMAL",
        total_exposed=1_000.0, max_exposure_pct=75.0,
    )
    assert len(orders) == 3


def test_equity_null_ignoriert_headroom_ohne_division():
    orders, _ = plan_core_sweep(
        _cfg(), equity=0.0, cash=_CASH, regime="NORMAL",
        total_exposed=100.0, max_exposure_pct=75.0,
    )
    assert orders == []


# ── Korrelations-Gate ─────────────────────────────────────────────────────────

def test_korrelations_gate_filtert_kandidaten():
    blocked = {"SPY", "AAPL"}

    def gate(sym, _open):
        if sym in blocked:
            return False, f"Korrelation r>=0.80 zu Bestand ({sym})"
        return True, "ok"

    orders, reasons = plan_core_sweep(
        _cfg(), equity=_EQUITY, cash=_CASH, regime="NORMAL",
        open_positions=[{"symbol": "QQQ", "amount_usd": 500.0}],
        correlation_gate=gate,
    )
    syms = {o.symbol for o in orders}
    assert syms.isdisjoint(blocked)
    assert any("Korrelation" in r for r in reasons)


def test_korrelations_gate_fail_open_bei_exception():
    """Eine yfinance-Stoerung darf das Cash-Deployment nicht lahmlegen."""
    def broken_gate(_sym, _open):
        raise RuntimeError("yfinance down")

    orders, reasons = plan_core_sweep(
        _cfg(), equity=_EQUITY, cash=_CASH, regime="NORMAL",
        correlation_gate=broken_gate,
    )
    assert len(orders) == 3          # unveraendert durchgelassen
    assert any("uebersprungen" in r for r in reasons)


def test_alle_kandidaten_korreliert_ergibt_keine_orders():
    orders, reasons = plan_core_sweep(
        _cfg(), equity=_EQUITY, cash=_CASH, regime="NORMAL",
        correlation_gate=lambda _s, _o: (False, "Korrelation r=0.95"),
    )
    assert orders == []
    assert any("keine freien Core-Titel" in r for r in reasons)


# ── Zusammenspiel ─────────────────────────────────────────────────────────────

def test_headroom_und_korrelation_kombiniert():
    """Beide Grenzen greifen gleichzeitig — Headroom nach Korrelations-Filter."""
    orders, _ = plan_core_sweep(
        _cfg(), equity=_EQUITY, cash=_CASH, regime="NORMAL",
        total_exposed=6_600.0, max_exposure_pct=75.0,   # Headroom = 2 Tranchen
        correlation_gate=lambda s, _o: (s != "SPY", "Korrelation"),
    )
    assert len(orders) == 2
    assert "SPY" not in {o.symbol for o in orders}


def test_regime_gate_hat_weiterhin_vorrang():
    """DEFENSIVE pausiert Core-Sweep — vor jeder Headroom-Rechnung."""
    orders, reasons = plan_core_sweep(
        _cfg(), equity=_EQUITY, cash=_CASH, regime="DEFENSIVE",
        total_exposed=0.0, max_exposure_pct=75.0,
    )
    assert orders == []
    assert any("Regime" in r for r in reasons)
