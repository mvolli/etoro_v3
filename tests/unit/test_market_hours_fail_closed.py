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


# ── resolve_market_fields: die eine Quelle fuer die Boersenfelder ────────────

class _FakeDB:
    """Minimal-DB: sqlite3.Row wird durch dict nachgebildet (row["spalte"])."""

    def __init__(self, rows: dict):
        self.rows = rows

    def fetchone(self, sql, params=None):
        return self.rows.get(int(params[0])) if params else None


def test_resolve_market_fields_liefert_alle_drei_felder():
    db = _FakeDB({7111: {"symbol": "BHP.ASX", "yfinance_symbol": "BHP.AX",
                         "asset_class": "stock"}})
    # 'stock' hat bewusst KEINE Kategorie — fuer Aktien entscheidet der Suffix
    assert mh.resolve_market_fields(db, 7111) == ("BHP.ASX", "BHP.AX", "")


def test_resolve_market_fields_uebersetzt_asset_class():
    db = _FakeDB({1: {"symbol": "EURJPY", "yfinance_symbol": "",
                      "asset_class": "Forex"}})
    assert mh.resolve_market_fields(db, 1) == ("EURJPY", "", "forex")


def test_resolve_market_fields_meldet_luecken_als_none():
    """None statt ("","",""), damit der Aufrufer die Richtung waehlen kann:
    risk_worker gibt bei fehlender Zeile fail-open auf, andere fragen weiter."""
    assert mh.resolve_market_fields(_FakeDB({}), 999) is None
    assert mh.resolve_market_fields(None, 1) is None
    assert mh.resolve_market_fields(_FakeDB({}), None) is None

    class Broken:
        def fetchone(self, *a, **k):
            raise RuntimeError("db weg")

    assert mh.resolve_market_fields(Broken(), 1) is None


def test_ohne_zusatzfelder_waere_forex_eine_us_aktie():
    """Warum execution_worker/llm_execution die Felder brauchen.

    'EURJPY' hat keinen Suffix — allein betrachtet also eine US-Aktie, die
    am Wochenende und nachts als geschlossen gilt. Beide Zusatzfelder retten
    es unabhaengig voneinander.
    """
    assert mh.get_instrument_market_key("EURJPY", "", "") == "US"
    assert mh.get_instrument_market_key("EURJPY", "EURJPY=X", "") == "FOREX"
    assert mh.get_instrument_market_key("EURJPY", "", "forex") == "FOREX"


def test_widerspruechliches_yfinance_symbol_wird_verworfen():
    """instruments.yfinance_symbol ist streckenweise kaputt.

    149 aktive Aktien tragen einen Krypto-Ticker, 118 davon DENSELBEN:
    FNMA/USB/FITB/GBCI stehen alle auf 'BNT-USD', 00285.HK/669.HK/STMMI.MI
    auf 'TRX-USD'. Ungefiltert macht die '-USD'-Heuristik in _get_market_key
    daraus CRYPTO — US-Bankaktien liefen dann am BUY-Gate rund um die Uhr.
    Für eine Aktie ist so ein Ticker beweisbar falsch: lieber kein
    yf_symbol als ein falsches.
    """
    db = _FakeDB({
        1: {"symbol": "FNMA", "yfinance_symbol": "BNT-USD", "asset_class": "stock"},
        2: {"symbol": "00285.HK", "yfinance_symbol": "TRX-USD", "asset_class": "stock"},
        3: {"symbol": "SPY", "yfinance_symbol": "^GSPC", "asset_class": "etf"},
    })
    for iid, erwarteter_key in ((1, "US"), (2, "APAC_HK_GROUP"), (3, "US")):
        fields = mh.resolve_market_fields(db, iid)
        assert fields[1] == "", f"kaputtes yf_symbol bei iid={iid} nicht verworfen"
        assert mh.get_instrument_market_key(*fields) == erwarteter_key


def test_echte_krypto_und_forex_behalten_ihr_yf_symbol():
    """Die Härtung greift NUR bei asset_class stock/etf — sonst wäre sie
    genau der Filter, der Krypto und Forex an US-Börsenzeiten bindet."""
    db = _FakeDB({
        1: {"symbol": "BCH", "yfinance_symbol": "BCH-USD", "asset_class": "crypto"},
        2: {"symbol": "EURUSD", "yfinance_symbol": "EURUSD=X", "asset_class": "forex"},
        3: {"symbol": "GOLD", "yfinance_symbol": "GC=F", "asset_class": "commodity"},
    })
    assert mh.get_instrument_market_key(*mh.resolve_market_fields(db, 1)) == "CRYPTO"
    assert mh.get_instrument_market_key(*mh.resolve_market_fields(db, 2)) == "FOREX"
    assert mh.get_instrument_market_key(*mh.resolve_market_fields(db, 3)) == "COMMODITIES"


def test_plausibles_yf_symbol_einer_aktie_bleibt():
    """Die Härtung darf den eigentlichen Zweck nicht kaputtmachen:
    BHP.ASX braucht sein BHP.AX, um die ASX zu finden."""
    db = _FakeDB({1: {"symbol": "BHP.ASX", "yfinance_symbol": "BHP.AX",
                      "asset_class": "stock"}})
    fields = mh.resolve_market_fields(db, 1)
    assert fields == ("BHP.ASX", "BHP.AX", "")
    assert mh.get_instrument_market_key(*fields) == "APAC_AU"


def test_asset_class_map_deckt_alle_kategorie_keys():
    """Jede uebersetzte Kategorie muss CATEGORY_TO_MARKET auch kennen —
    sonst laeuft die Uebersetzung ins Leere und der US-Default greift."""
    for asset_class, category in mh.ASSET_CLASS_TO_CATEGORY.items():
        assert category in mh.CATEGORY_TO_MARKET, (
            f"{asset_class} → {category} fehlt in CATEGORY_TO_MARKET"
        )


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
