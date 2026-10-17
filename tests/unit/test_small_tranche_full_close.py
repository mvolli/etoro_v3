#!/usr/bin/env python3
"""feat/small-tranche-full-close (VoLLi 2026-10-17).

Ein Teilverkauf unter partial_close.min_tranche_usd schliesst die GANZE
Position, statt unterdrueckt zu werden. Broker-Abgleich 10.09.-16.10.
(136 Positionen mit Teilverkauf): tatsaechlich -$265, nur unterdruecken
-$115, ganz schliessen -$8.
"""
from __future__ import annotations

import pytest

from bot.core import partial_close_policy as pcp
from bot.core import sell_exits as se
from bot.core import trailing_stop as ts


def _cfg(action="full_close", min_tr=100.0):
    c = dict(pcp.DEFAULT_CONFIG)
    c.update({"min_tranche_usd": min_tr, "small_tranche_action": action})
    return c


@pytest.fixture
def full_close(monkeypatch):
    monkeypatch.setattr(pcp, "_config_cache", _cfg())
    monkeypatch.setattr(ts, "_action_market_open", lambda *a, **k: True)
    yield
    pcp._config_cache = None


# ── Policy ────────────────────────────────────────────────────────────────────

def test_default_bleibt_unterdruecken():
    assert pcp.DEFAULT_CONFIG["small_tranche_action"] == "suppress"


def test_kleine_tranche_wird_umgewandelt(full_close):
    d = pcp.evaluate(5.0, 25.0, mode="current", amount_usd=200.0)
    assert d.allowed is False and d.convert_to_full is True
    assert d.suppressed_reason.startswith("min_tranche_full_close:50.00<100.00")


def test_grosse_tranche_bleibt_teilverkauf(full_close):
    d = pcp.evaluate(5.0, 50.0, mode="current", amount_usd=400.0)
    assert d.allowed is True and d.convert_to_full is False


def test_suppress_modus_wandelt_nicht_um(monkeypatch):
    monkeypatch.setattr(pcp, "_config_cache", _cfg(action="suppress"))
    d = pcp.evaluate(5.0, 25.0, mode="current", amount_usd=200.0)
    assert d.allowed is False and d.convert_to_full is False
    pcp._config_cache = None


def test_unbekannte_aktion_ist_suppress(monkeypatch):
    monkeypatch.setattr(pcp, "_config_cache", _cfg(action="quatsch"))
    assert pcp.small_tranche_action() == "suppress"
    pcp._config_cache = None


def test_entscheidungsstufe_unterdrueckt_nicht_mehr(full_close):
    """Der KI-Profit-Advisor soll das Signal sehen; umgewandelt wird erst in
    der Ausfuehrung."""
    assert pcp.below_min_tranche(200.0, 25.0) is False


# ── Trailing ──────────────────────────────────────────────────────────────────

def _trail(action="MOMENTUM_FADE", amount=60.0, close_pct=25.0):
    return ts.TrailingAction(
        action=action, symbol="TINY", position_id="p1", pnl_pct=4.0,
        reason="Fade", close_pct=close_pct, instrument_id=1,
        amount_usd=amount, open_rate=10.0)


@pytest.mark.parametrize("kind", ["MOMENTUM_FADE", "PARTIAL_CLOSE"])
def test_trailing_teilverkauf_wird_vollausstieg(full_close, kind):
    stats = ts.execute_trailing_actions(None, [_trail(kind)], dry_run=True)
    assert stats["full_exits"] == 1
    assert stats["partial_closes"] == 0
    assert stats.get("small_tranche_full_closes") == 1


def test_trailing_grosse_tranche_bleibt_teilverkauf(full_close):
    stats = ts.execute_trailing_actions(None, [_trail(amount=1000.0, close_pct=25.0)],
                                        dry_run=True)
    assert stats["full_exits"] == 0
    assert stats.get("small_tranche_full_closes", 0) == 0


# ── SELL-Exit ─────────────────────────────────────────────────────────────────

class _Client:
    def get_position_units(self, _pid):
        return 6.0


def test_sell_exit_teilverkauf_wird_vollausstieg(full_close):
    a = se.SellExitAction(signal_id=1, symbol="TINY", position_id="p1", instrument_id=1,
                          amount_usd=60.0, open_rate=10.0, pnl_pct=3.0, close_pct=50.0,
                          reason="SELL-Signal")
    stats = se.execute_sell_exits(_Client(), None, [a], dry_run=True)
    assert stats["closed"] == 1 and stats["full_closes"] == 1


def test_sell_exit_grosse_tranche_bleibt_teilverkauf(full_close):
    a = se.SellExitAction(signal_id=1, symbol="BIG", position_id="p2", instrument_id=1,
                          amount_usd=1000.0, open_rate=10.0, pnl_pct=3.0, close_pct=50.0,
                          reason="SELL-Signal")
    stats = se.execute_sell_exits(_Client(), None, [a], dry_run=True)
    assert stats["closed"] == 1 and stats["full_closes"] == 0
