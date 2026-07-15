"""Tests für .L-Suffix yfinance_symbol Fix.

fix/l-suffix-yf-fix (2026-07-15):
  284 .L-Instrumente hatten falsche yfinance_symbols ohne .L-Suffix
  (OGZDL.L → GO statt OGZDL.L, MGNTL.L → MP statt MGNTL.L, etc.)
  → Alle .L-Instrumente jetzt yfinance_symbol = symbol.
"""
import pytest
import sqlite3
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root / "src") not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_project_root / "src"))


DB_FIXTURE = Path(__file__).parent.parent.parent / "data" / "trading.db"


@pytest.fixture
def db():
    from bot.db.connection import DB
    db_obj = DB(db_path=str(DB_FIXTURE))
    yield db_obj
    db_obj.close()


class TestLSuffixFix:
    """Tests für .L-Suffix yfinance_symbol Fix."""

    def test_all_l_instruments_have_l_suffix_yf(self, db):
        """Alle .L-Instrumente haben yfinance_symbol mit .L-Suffix."""
        rows = db.execute("""
            SELECT instrument_id, symbol, yfinance_symbol
            FROM instruments
            WHERE symbol LIKE '%.L'
              AND yfinance_symbol IS NOT NULL
              AND yfinance_symbol != ''
              AND yfinance_symbol != symbol
        """).fetchall()
        
        assert len(rows) == 0, (
            f"{len(rows)} .L-Instrumente haben yfinance_symbol ohne .L-Suffix:\n" +
            "\n".join(f"  {r[1]} → {r[2]}" for r in rows[:10])
        )

    def test_london_brands_fixed(self, db):
        """IMB.L (Imperial Brands) hat korrektes yfinance_symbol IMB.L."""
        row = db.execute("""
            SELECT instrument_id, symbol, yfinance_symbol
            FROM instruments
            WHERE symbol = 'IMB.L'
        """).fetchone()
        
        assert row is not None, "IMB.L nicht in instruments gefunden"
        assert row[2] == "IMB.L", f"IMB.L yfinance_symbol = {row[2]} (erwartet IMB.L)"
        assert row[0] == 2043, f"IMB.L instrument_id = {row[0]} (erwartet 2043)"

    def test_no_wrong_yf_mappings(self, db):
        """Keine .L-Instrumente mit falschem yfinance_mapping (GO, MP, MO, etc.)."""
        wrong_mappings = db.execute("""
            SELECT instrument_id, symbol, yfinance_symbol
            FROM instruments
            WHERE symbol LIKE '%.L'
              AND yfinance_symbol IN ('GO', 'MP', 'MO', 'SOR', 'KEPJ', 'LO', 'MNNO', 'PO', 'RO', 'SP')
        """).fetchall()
        
        assert len(wrong_mappings) == 0, (
            f"Wrong yf mappings gefunden:\n" +
            "\n".join(f"  {m[1]} → {m[2]}" for m in wrong_mappings)
        )

    def test_l_instruments_count(self, db):
        """Alle .L-Instrumente haben yfinance_symbol."""
        total = db.execute("""
            SELECT COUNT(*) FROM instruments WHERE symbol LIKE '%.L'
        """).fetchone()[0]
        
        with_yf = db.execute("""
            SELECT COUNT(*) FROM instruments 
            WHERE symbol LIKE '%.L' 
              AND yfinance_symbol IS NOT NULL 
              AND yfinance_symbol != ''
        """).fetchone()[0]
        
        assert total == with_yf, f"{total} .L-Instrumente, aber nur {with_yf} mit yfinance_symbol"
