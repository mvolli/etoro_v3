"""refactor/extract-last-chance-resolver: Regressionstest fuer die
Instrument-ID-Last-Chance-Aufloesung (NOKIA.HE-Kollision + Semantik)."""
import pytest

from bot.db.repo import DB
from bot.workers.discovery_worker import resolve_instrument_id_last_chance


@pytest.fixture()
def db(tmp_path):
    d = DB(db_path=tmp_path / "inst.db")
    d.execute("""
        CREATE TABLE instruments (
            instrument_id INTEGER PRIMARY KEY,
            symbol TEXT,
            yfinance_symbol TEXT,
            is_tradable INTEGER
        )
    """)
    # Realer NOKIA.HE-Kollisionsfall (3 Instrumente teilen yfinance='NOKIA.HE')
    rows = [
        (1115,    "NOK",         "NOKIA.HE", 1),   # US-ADR, handelbar
        (13030,   "NOKIA.PA",    "NOKIA.HE", 0),   # nicht handelbar
        (1017859, "NOKIASEK.ST", "NOKIA.HE", 1),   # Stockholm, hoehere ID
        (500,     "AAPL",        "AAPL",     1),   # exakter-symbol-Fall
        (400,     "AAPL",        "AAPL",     1),   # zweite AAPL, niedrigere ID
        (700,     "DEAD",        "DEAD.X",   0),   # nur nicht-handelbar
    ]
    for r in rows:
        d.execute(
            "INSERT INTO instruments (instrument_id,symbol,yfinance_symbol,is_tradable) "
            "VALUES (?,?,?,?)", r,
        )
    return d


def test_nokia_he_resolves_to_tradable_lowest_id(db):
    # yfinance-Fallback: 1115 (handelbar, niedrigste) statt 13030 (nicht
    # handelbar) oder 1017859 (hoehere ID)
    assert resolve_instrument_id_last_chance(db, "NOKIA.HE") == 1115


def test_exact_symbol_wins_and_lowest_id(db):
    # exakter symbol-Match hat Vorrang, niedrigste ID gewinnt (400 < 500)
    assert resolve_instrument_id_last_chance(db, "AAPL") == 400


def test_non_tradable_only_yfinance_returns_none(db):
    # DEAD.X existiert nur als nicht-handelbar -> kein Fallback-Treffer
    assert resolve_instrument_id_last_chance(db, "DEAD.X") is None


def test_unknown_symbol_returns_none(db):
    assert resolve_instrument_id_last_chance(db, "GIBTESNICHT.XY") is None


def test_exact_symbol_beats_yfinance_of_other(db):
    # 'NOK' als exaktes Symbol -> 1115 direkt ueber Pfad 1 (nicht ueber yf)
    assert resolve_instrument_id_last_chance(db, "NOK") == 1115
