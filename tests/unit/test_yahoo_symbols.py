"""fix/yahoo-symbols (2026-10-17): eine Aufloesung des Yahoo-Symbols fuer alle Leser."""
import pytest

from bot.core.yahoo_symbols import for_instrument, resolve, resolve_row


@pytest.mark.parametrize("sym,stored,ac,expected", [
    ("CLPT", "CNI", "stock", "CLPT"),          # fremde Firma gespeichert -> Ticker
    ("CI", "CG", "stock", "CI"),
    ("UMC", None, "stock", "UMC"),             # leer -> Ticker
    ("SONY", "6758.T", "stock", "SONY"),       # US-ADR, nicht Tokio
    ("BP", "BP.L", "stock", "BP.L"),           # belegte Ausnahme: eToro BP = London
    ("SAP.DE", "SAP.DE", "stock", "SAP.DE"),   # Suffix -> gespeichertes
    ("CAR.ASX", "CAR.AX", "stock", "CAR.AX"),
    ("D&C", "DE", "stock", "DE"),              # Sonderzeichen -> gespeichertes
    ("DDOld", "DU-PON", "stock", "DU-PON"),    # Platzhalter (Kleinbuchstaben)
    ("BTC", "BTC-USD", "crypto", "BTC-USD"),
    ("EURNOK", "EURNOK=X", "forex", "EURNOK=X"),
    ("SPY", "SPY", "etf", "SPY"),
    ("US2000", "^RUT", "index", "^RUT"),
    ("XYZ", None, "etf", "XYZ"),
])
def test_regel(sym, stored, ac, expected):
    assert resolve(sym, stored, ac) == expected


def test_row_und_db(tmp_path):
    from bot.db.connection import DB
    d = DB(db_path=tmp_path / "t.db")
    d.execute("CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT, "
              "yfinance_symbol TEXT, asset_class TEXT)")
    d.execute("INSERT INTO instruments VALUES (1, 'XPO', 'XLI', 'stock')")
    assert for_instrument(d, 1) == "XPO"
    assert for_instrument(d, 2) is None
    assert resolve_row({"symbol": "TSN", "yfinance_symbol": "TFI", "asset_class": "stock"}) == "TSN"
