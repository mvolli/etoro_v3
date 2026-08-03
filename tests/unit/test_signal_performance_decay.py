"""Tests for _signal_performance_decay (fix/signal-performance-decay)."""
import pytest
from pathlib import Path
from bot.workers.signal_worker import _signal_performance_decay


@pytest.fixture
def tmp_db(tmp_path: Path):
    """Erzeugt eine leere SQLite-DB mit trades + signals Tabellen."""
    import sqlite3
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY,
            signal_type TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            signal_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            pnl_usd REAL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return db


class TestSignalPerformanceDecay:
    """_signal_performance_decay: Decay [0.3..1.0] basierend auf WR + avg_pnl."""

    def test_no_trades_returns_one(self, tmp_db: Path):
        """Keine Trades -> 1.0 (fail-open)."""
        result = _signal_performance_decay("CORE_SWEEP", tmp_db)
        assert result == 1.0

    def test_below_min_trades_returns_one(self, tmp_db: Path):
        """Weniger als min_trades (5) -> 1.0."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'CORE_SWEEP')")
        for i in range(3):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', 10.0, datetime('now', '-1 day'))",
                (i + 1,),
            )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("CORE_SWEEP", tmp_db)
        assert result == 1.0

    def test_good_performance_returns_one(self, tmp_db: Path):
        """WR >= 40% AND avg_pnl >= 0 -> 1.0."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'CORE_SWEEP')")
        # 10 Trades: 8 wins (+10), 2 losses (-5) -> WR=80%, avg_pnl=+5.0
        for i in range(8):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', 10.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        for i in range(8, 10):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', -5.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("CORE_SWEEP", tmp_db)
        assert result == 1.0

    def test_very_bad_performance_returns_03(self, tmp_db: Path):
        """WR < 30% -> 0.3."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'CORE_SWEEP')")
        # 10 Trades: 2 wins (+10), 8 losses (-5) -> WR=20%
        for i in range(2):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', 10.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        for i in range(2, 10):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', -5.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("CORE_SWEEP", tmp_db)
        assert result == 0.3

    def test_medium_performance_returns_05(self, tmp_db: Path):
        """WR 30-35% -> 0.5."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'CORE_SWEEP')")
        # 20 Trades: 6 wins (+10), 14 losses (-5) -> WR=30%
        for i in range(6):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', 10.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        for i in range(6, 20):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', -5.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("CORE_SWEEP", tmp_db)
        assert result == 0.5

    def test_below_35_wr_returns_07(self, tmp_db: Path):
        """WR 35-40% -> 0.7."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'CORE_SWEEP')")
        # 20 Trades: 7 wins (+10), 13 losses (-5) -> WR=35%
        for i in range(7):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', 10.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        for i in range(7, 20):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', -5.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("CORE_SWEEP", tmp_db)
        assert result == 0.7

    def test_extra_penalty_for_bad_avg_pnl(self, tmp_db: Path):
        """avg_pnl < -2.0% -> extra 0.8 Multiplikator."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'CORE_SWEEP')")
        # 10 Trades: 3 wins (+1), 7 losses (-5) -> WR=30%, avg_pnl=-3.3
        for i in range(3):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', 1.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        for i in range(3, 10):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', -5.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("CORE_SWEEP", tmp_db)
        # WR=30% -> 0.5, avg_pnl=-3.3 < -2.0 -> *0.8 = 0.4
        assert result == 0.4

    def test_signal_type_mismatch_returns_one(self, tmp_db: Path):
        """Signal-Typ existiert nicht in trades -> keine Trades -> 1.0."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'CORE_SWEEP')")
        conn.execute(
            "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
            "VALUES (1, 1, 'CLOSED', 10.0, datetime('now', '-1 day'))"
        )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("NONEXISTENT", tmp_db)
        assert result == 1.0

    def test_old_trades_ignored(self, tmp_db: Path):
        """Trades außerhalb des Lookback-Fensters werden ignoriert."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'CORE_SWEEP')")
        # 10 Trades vor 30 Tagen (außerhalb 14-Tage-Fenster)
        for i in range(10):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', 10.0, datetime('now', '-30 days'))",
                (i + 1,),
            )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("CORE_SWEEP", tmp_db)
        assert result == 1.0  # Keine Trades im Lookback-Fenster

    def test_combo_signal_type(self, tmp_db: Path):
        """Combo-Signal-Typ mit Komma wird korrekt gematcht."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute(
            "INSERT INTO signals (id, signal_type) VALUES (1, 'TREND_PULLBACK,GOLDEN_CROSS')"
        )
        for i in range(10):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', -5.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("TREND_PULLBACK,GOLDEN_CROSS", tmp_db)
        # WR=0% < 30% -> 0.3
        assert result == 0.3

    def test_min_trades_custom(self, tmp_db: Path):
        """min_trades=3: nur 3 Trades genügen."""
        conn = __import__("sqlite3").connect(str(tmp_db))
        conn.execute("INSERT INTO signals (id, signal_type) VALUES (1, 'CORE_SWEEP')")
        for i in range(3):
            conn.execute(
                "INSERT INTO trades (id, signal_id, status, pnl_usd, created_at) "
                "VALUES (?, 1, 'CLOSED', -5.0, datetime('now', '-2 days'))",
                (i + 1,),
            )
        conn.commit()
        conn.close()
        result = _signal_performance_decay("CORE_SWEEP", tmp_db, min_trades=3)
        assert result < 1.0  # Sollte gedämpft sein
