#!/usr/bin/env python3
"""Unit tests — fix/market-hours-fail-closed.

An unknown/unmapped market key stays fail-open on data paths (default)
but counts as CLOSED at the BUY boundary (fail_open=False). Also pins the
mapping-consistency invariant: every suffix/category/override key must
resolve to a defined market.
"""
from __future__ import annotations

import bot.core.market_hours as mh
from bot.core.market_hours import is_market_open


def test_mapping_consistency_no_holes():
    # Invariante: jeder gemappte Market-Key existiert in MARKET_DEFINITIONS.
    # Bricht dieser Test, produziert der fail-open-Datenpfad stille Fehler.
    for mapping in (mh.SUFFIX_TO_MARKET, mh.CATEGORY_TO_MARKET, mh.YF_SYMBOL_MARKET_OVERRIDE):
        for key, market in mapping.items():
            assert market in mh.MARKET_DEFINITIONS, f"{key} → {market} fehlt in MARKET_DEFINITIONS"


def test_unknown_market_fail_open_by_default(monkeypatch):
    # Mapping-Loch simulieren: Suffix zeigt auf nicht definierten Markt
    monkeypatch.setitem(mh.SUFFIX_TO_MARKET, ".XX", "MARS_EXCHANGE")
    assert is_market_open("FOO.XX") is True


def test_unknown_market_fail_closed_at_buy_boundary(monkeypatch):
    monkeypatch.setitem(mh.SUFFIX_TO_MARKET, ".XX", "MARS_EXCHANGE")
    assert is_market_open("FOO.XX", fail_open=False) is False


def test_known_markets_unaffected_by_fail_closed():
    # Crypto ist 24/7 — fail_open=False darf bekannte Märkte nicht blocken
    assert is_market_open("BTC-USD", fail_open=False) is True


def test_us_default_key_is_defined():
    # Symbole ohne Suffix → 'US', das definiert ist: fail_closed ändert
    # deren Verhalten nicht (nur echte Mapping-Löcher blocken)
    assert mh.get_instrument_market_key("SOMENEWSTOCK") in mh.MARKET_DEFINITIONS
