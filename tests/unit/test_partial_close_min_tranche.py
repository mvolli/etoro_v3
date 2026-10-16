#!/usr/bin/env python3
"""Unit tests — feat/partial-min-tranche (2026-10-16).

eToro bucht je Order eine Flat-Fee ($1 / $2), auch auf jede Teilschliessung.
Seit Epoch 10.09. gab es 184 Teilschliessungen mit einer Ø-Tranche von $38.
`partial_close.min_tranche_usd` unterdrueckt Teilschliessungen unterhalb des
Betrags in jedem Modus. Default 0 = Verhalten wie bisher.
"""
from __future__ import annotations

import pytest

from bot.core import partial_close_policy as pcp


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    monkeypatch.setattr(pcp, "_config_cache", dict(pcp.DEFAULT_CONFIG))
    yield
    pcp._config_cache = None


def test_default_ist_aus_kein_verhaltenswechsel():
    assert pcp.DEFAULT_CONFIG["min_tranche_usd"] == 0.0
    d = pcp.evaluate(5.0, 25.0, mode="current", amount_usd=60.0)
    assert d.allowed is True and d.suppressed_reason is None


@pytest.mark.parametrize("mode", ["current", "no_partials", "loss_only"])
def test_kleine_tranche_wird_in_jedem_modus_unterdrueckt(mode):
    # 25 % von $200 = $50 < $100
    d = pcp.evaluate(5.0, 25.0, mode=mode, amount_usd=200.0, min_tranche_usd=100.0)
    assert d.allowed is False
    assert d.suppressed_reason.startswith("min_tranche:50.00<100.00")


def test_grosse_tranche_folgt_dem_modus():
    # 50 % von $400 = $200 >= $100 -> Modus entscheidet
    assert pcp.evaluate(5.0, 50.0, mode="current", amount_usd=400.0,
                        min_tranche_usd=100.0).allowed is True
    assert pcp.evaluate(5.0, 50.0, mode="no_partials", amount_usd=400.0,
                        min_tranche_usd=100.0).allowed is False


def test_grenzwert_genau_auf_der_schwelle_ist_erlaubt():
    d = pcp.evaluate(5.0, 50.0, mode="current", amount_usd=200.0, min_tranche_usd=100.0)
    assert d.allowed is True


def test_volle_schliessung_nie_beruehrt():
    d = pcp.evaluate(-3.0, 100.0, mode="current", amount_usd=10.0, min_tranche_usd=100.0)
    assert d.allowed is True


def test_ohne_positionswert_fail_open():
    d = pcp.evaluate(5.0, 25.0, mode="current", amount_usd=None, min_tranche_usd=100.0)
    assert d.allowed is True


def test_config_wert_wird_gelesen(monkeypatch):
    cfg = dict(pcp.DEFAULT_CONFIG, min_tranche_usd=75.0)
    monkeypatch.setattr(pcp, "_config_cache", cfg)
    assert pcp.evaluate(5.0, 25.0, mode="current", amount_usd=200.0).allowed is False
    assert pcp.evaluate(5.0, 50.0, mode="current", amount_usd=200.0).allowed is True


def test_unlesbarer_config_wert_fail_open(monkeypatch):
    cfg = dict(pcp.DEFAULT_CONFIG, min_tranche_usd="abc")
    monkeypatch.setattr(pcp, "_config_cache", cfg)
    assert pcp.evaluate(5.0, 25.0, mode="current", amount_usd=200.0).allowed is True


def test_check_reicht_positionswert_durch(monkeypatch):
    cfg = dict(pcp.DEFAULT_CONFIG, min_tranche_usd=100.0, ledger=False)
    monkeypatch.setattr(pcp, "_config_cache", cfg)
    d = pcp.check(None, path="trailing", symbol="X", position_id="1",
                  instrument_id=1, pnl_pct=4.0, close_pct=25.0, amount_usd=200.0)
    assert d.allowed is False
    d = pcp.check(None, path="trailing", symbol="X", position_id="1",
                  instrument_id=1, pnl_pct=4.0, close_pct=25.0, amount_usd=800.0)
    assert d.allowed is True
