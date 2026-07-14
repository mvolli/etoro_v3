"""Tests für yfinance_symbol Denormalisierung in portfolio_snapshot.

fix/yf-symbol-denormalization (2026-07-14):
  portfolio_snapshot.yfinance_symbol ist jetzt ein denormalisierter Cache
  der instruments.yfinance_symbol (SSOT). Alle yfinance-Calls lesen direkt
  aus portfolio_snapshot — kein JOIN mehr im hot path.
"""
import pytest
import sqlite3
from pathlib import Path

# Ensure project root is on path
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root / "src") not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_project_root / "src"))


DB_FIXTURE = Path(__file__).parent.parent.parent / "data" / "trading.db"


@pytest.fixture
def db():
    """DB-Objekt für Tests."""
    from bot.db.connection import DB
    db_obj = DB(db_path=str(DB_FIXTURE))
    yield db_obj
    db_obj.close()


class TestYfSymbolColumn:
    """Tests für die yfinance_symbol Spalte in portfolio_snapshot."""

    def test_column_exists(self, db):
        """yfinance_symbol Spalte existiert in portfolio_snapshot."""
        columns = [row["name"] for row in db.execute(
            "PRAGMA table_info(portfolio_snapshot)"
        ).fetchall()]
        assert "yfinance_symbol" in columns, "yfinance_symbol Spalte fehlt in portfolio_snapshot"

    def test_all_positions_have_yf_symbol(self, db):
        """Alle portfolio_snapshot Einträge haben yfinance_symbol."""
        null_count = db.execute(
            "SELECT COUNT(*) as cnt FROM portfolio_snapshot WHERE yfinance_symbol IS NULL"
        ).fetchone()["cnt"]
        assert null_count == 0, f"{null_count} Einträge haben NULL yfinance_symbol"

    def test_yf_symbol_matches_instruments(self, db):
        """yfinance_symbol in portfolio_snapshot stimmt mit instruments überein."""
        mismatches = db.execute("""
            SELECT ps.api_position_id, ps.symbol, ps.yfinance_symbol as ps_yf,
                   i.yfinance_symbol as i_yf
            FROM portfolio_snapshot ps
            JOIN instruments i ON ps.instrument_id = i.instrument_id
            WHERE ps.yfinance_symbol != i.yfinance_symbol
        """).fetchall()
        assert len(mismatches) == 0, (
            f"Diskrepanzen portfolio_snapshot vs instruments:\n" +
            "\n".join(
                f"  {m['symbol']}: ps={m['ps_yf']} vs i={m['i_yf']}"
                for m in mismatches
            )
        )

    def test_non_empty_yf_symbols(self, db):
        """Keine leeren yfinance_symbols."""
        empty_count = db.execute(
            "SELECT COUNT(*) as cnt FROM portfolio_snapshot WHERE yfinance_symbol = ''"
        ).fetchone()["cnt"]
        assert empty_count == 0, f"{empty_count} Einträge haben leeren yfinance_symbol"


class TestDataWorkerReadsYfSymbol:
    """Tests dass data_worker yfinance_symbol korrekt aus portfolio_snapshot liest."""

    def test_get_portfolio_symbols_returns_yf_symbol(self, db):
        """_get_portfolio_symbols gibt yf_symbol zurück."""
        from bot.workers.data_worker import _get_portfolio_symbols

        items = _get_portfolio_symbols(db)
        assert len(items) > 0, "Keine Portfolio-Symbole gefunden"

        for item in items:
            assert "yf_symbol" in item, f"yf_symbol fehlt in item: {item}"
            assert item["yf_symbol"] is not None, f"yf_symbol ist None für {item['symbol']}"
            assert item["yf_symbol"] != "", f"yf_symbol ist leer für {item['symbol']}"

    def test_portfolio_items_have_instrument_id(self, db):
        """Alle portfolio items haben instrument_id."""
        from bot.workers.data_worker import _get_portfolio_symbols

        items = _get_portfolio_symbols(db)
        for item in items:
            assert item.get("instrument_id") is not None, (
                f"instrument_id fehlt für {item['symbol']}"
            )


class TestCorrelationResolve:
    """Tests für correlation._resolve_yf_symbols."""

    def test_resolve_returns_mapping(self, db):
        """_resolve_yf_symbols gibt korrektes Mapping zurück."""
        from bot.core.correlation import _resolve_yf_symbols

        # Hole echte Portfolio-Symbole
        symbols = [row["symbol"] for row in db.execute(
            "SELECT DISTINCT symbol FROM portfolio_snapshot WHERE symbol IS NOT NULL"
        ).fetchall()]
        assert len(symbols) > 0

        mapping = _resolve_yf_symbols(db, symbols)

        for sym in symbols:
            if sym in mapping:
                # Wenn yfinance_symbol in instruments existiert, muss Mapping korrekt sein
                row = db.execute(
                    "SELECT yfinance_symbol FROM instruments WHERE symbol = ?",
                    (sym,)
                ).fetchone()
                if row and row["yfinance_symbol"]:
                    assert mapping[sym] == row["yfinance_symbol"], (
                        f"Mapping-Fehler für {sym}: {mapping[sym]} != {row['yfinance_symbol']}"
                    )
