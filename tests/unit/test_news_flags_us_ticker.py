"""fix/yf-us-ticker (2026-10-17): News-Flags fragen US-Aktien unter dem eToro-Ticker ab.

instruments.yfinance_symbol ist fuer US-Aktien oft eine ANDERE Firma
(gemessen: CI -> 'CG' Carlyle statt Cigna, TSN -> 'TFI'). Ein News-AVOID aus
fremden Headlines blockt Kaeufe auf allen Wegen und verkauft Positionen der
Analysten-Strategie.
"""
from bot.db.connection import DB
from bot.workers.news_flags_worker import _gather_symbols


def test_us_aktie_nutzt_ticker_auslaendische_das_gespeicherte(tmp_path):
    d = DB(db_path=tmp_path / "t.db")
    d.execute("CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT, "
              "yfinance_symbol TEXT, name TEXT, asset_class TEXT)")
    d.execute("CREATE TABLE portfolio_snapshot (instrument_id INTEGER)")
    d.execute("CREATE TABLE signals (instrument_id INTEGER, status TEXT, expires_at TEXT)")
    d.execute("INSERT INTO instruments VALUES (1, 'CI', 'CG', 'The Cigna Group', 'stock')")
    d.execute("INSERT INTO instruments VALUES (2, 'CAR.ASX', 'CAR.AX', 'CAR Group', 'stock')")
    d.execute("INSERT INTO instruments VALUES (3, 'UMC', NULL, 'United Microelectronics', 'stock')")
    d.execute("INSERT INTO instruments VALUES (4, 'BTC', 'BTC-USD', 'Bitcoin', 'crypto')")
    for i in (1, 2, 3, 4):
        d.execute("INSERT INTO portfolio_snapshot VALUES (?)", (i,))
    got = {r["symbol"]: r["yf"] for r in _gather_symbols(d)}
    assert got == {"CI": "CI", "CAR.ASX": "CAR.AX", "UMC": "UMC", "BTC": "BTC-USD"}
