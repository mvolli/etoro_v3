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


def test_suffixlose_ticker_bleiben_us():
    """Präzisierte Invariante (war: „Symbole ohne Suffix → 'US'").

    Der US-Default gilt weiterhin — aber nur noch für Symbole, die wie ein
    Ticker aussehen. 'US' ist ein definierter Key, fail_closed ändert an
    ihnen also nichts. Die Ausnahme steht in
    test_reine_instrument_id_ist_unknown.
    """
    for sym in ("SOMENEWSTOCK", "AAPL", "TSLA", "SPY", "BRK.B"):
        assert mh.get_instrument_market_key(sym) == "US"
    assert "US" in mh.MARKET_DEFINITIONS


def test_adr_folgt_us_zeiten_nicht_dem_heimatmarkt():
    """Der yfinance-Suffix-Fallback darf suffixlose Symbole nicht bewegen.

    HSBC/SONY/TM/BHP sind an US-Börsen gelistete ADRs; ihr yfinance_symbol
    zeigt auf London/Tokio/Sydney. Ein Fallback, der auch ohne eToro-Suffix
    greift, zöge 26 solcher Papiere auf die falschen Handelszeiten.
    """
    assert mh.get_instrument_market_key("HSBC", "HSBA.L", "") == "US"
    assert mh.get_instrument_market_key("SONY", "6758.T", "") == "US"
    assert mh.get_instrument_market_key("TM", "7203.T", "") == "US"
    assert mh.get_instrument_market_key("BHP", "BHP.AX", "") == "US"


def test_reine_instrument_id_ist_unknown():
    """Neue Invariante (9633.HK-Restlücke, 2026-08-17).

    Ein rein numerisches Symbol ist kein Ticker, sondern eine durchgereichte
    eToro-instrumentID (trailing_stop.load_symbols() fällt darauf zurück,
    wenn die ID nicht auflösbar ist). Dafür 'US' zu liefern hieße: der Guard
    beantwortet still „ist die NYSE offen?" statt „ist die richtige Börse
    offen?". UNKNOWN_MARKET_KEY reicht die Entscheidung an fail_open weiter.
    """
    assert mh.get_instrument_market_key("9999") == mh.UNKNOWN_MARKET_KEY
    assert mh.UNKNOWN_MARKET_KEY not in mh.MARKET_DEFINITIONS


def test_unknown_key_laesst_fail_open_entscheiden():
    # Daten- und Exit-Pfade: offen annehmen (Verlustschutz nie blockieren).
    assert is_market_open("9999") is True
    # BUY-/Execution-Boundary: geschlossen annehmen (DEFER statt Ghost-Order).
    assert is_market_open("9999", fail_open=False) is False


def test_unknown_ist_letzter_ausweg_nicht_erster():
    """Solange IRGENDEIN Namespace die Börse kennt, wird sie benutzt."""
    # yfinance_symbol rettet die rohe ID (zweite Verteidigungslinie neben
    # load_symbols() aus fix/trailing-symbol-resolution)
    assert mh.get_instrument_market_key("3364", "9633.HK", "") == "APAC_HK_GROUP"
    # Kategorie rettet sie ebenfalls
    assert mh.get_instrument_market_key("9999", "", "crypto") == "CRYPTO"


def test_etoro_namespace_suffixe_werden_aufgeloest():
    """instruments.symbol trägt den eToro-Namespace, nicht den yfinance-.

    Dessen Suffixe fehlten in SUFFIX_TO_MARKET — 751 tradable Instrumente
    (382 .ASX, 113 .OL, 89 .CO, 62 .HE, 55 .ZU …) beantworteten dadurch die
    NYSE-Frage. Belegt: BHP.ASX wurde 8x mit „Markt >4h geschlossen" verworfen.
    """
    assert mh.get_instrument_market_key("BHP.ASX", "BHP.AX", "") == "APAC_AU"
    assert mh.get_instrument_market_key("GIVN.ZU", "GIVN.SW", "") == "EU"
    assert mh.get_instrument_market_key("YAR.OL", "YAR.OL", "") == "EU"
    assert mh.get_instrument_market_key("STERV.HE", "STERV.HE", "") == "EU"
    assert mh.get_instrument_market_key("NOVO-B.CO", "NOVO-B.CO", "") == "EU"
    assert mh.get_instrument_market_key("EMAAR.AE", "", "") == "APAC_AE"
    # eToro-Suffixe, die tatsächlich US bedeuten (CVX.US → CVX, PG.RTH → PG)
    assert mh.get_instrument_market_key("CVX.US", "CVX", "") == "US"
    assert mh.get_instrument_market_key("PG.RTH", "PG", "") == "US"


def test_unbekannter_etoro_suffix_faellt_auf_yfinance_zurueck():
    # .XYZ ist nirgends gemappt — der yfinance-Suffix liefert die Börse nach
    assert mh.get_instrument_market_key("FOO.XYZ", "FOO.HK", "") == "APAC_HK_GROUP"


def test_apac_ae_handelt_montags_bis_freitags():
    """VAE-Börsen (ADX/DFM) seit dem Mo–Fr-Arbeitswochen-Wechsel 2022.

    Ältere Broker-Seiten nennen noch So–Do; träfe das zu, bräuchte
    _is_market_open_now() eine eigene Wochenend-Logik.
    """
    from datetime import datetime, timezone
    # Mo 2026-08-17, 08:00 UTC = 12:00 GST → offen
    assert mh.is_market_key_open_at(
        "APAC_AE", datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)) is True
    # So 2026-08-16, 08:00 UTC = 12:00 GST → zu (kein So–Do-Handel)
    assert mh.is_market_key_open_at(
        "APAC_AE", datetime(2026, 8, 16, 8, 0, tzinfo=timezone.utc)) is False
    # Mo 2026-08-17, 03:00 UTC = 07:00 GST → vor Eröffnung
    assert mh.is_market_key_open_at(
        "APAC_AE", datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)) is False
