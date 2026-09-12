"""feat/ohlcv-from-scan (2026-09-12): der Discovery-Scan schreibt seine
bereits geladenen OHLCV-Frames nach ohlcv_daily.

Hintergrund: ohlcv_daily stand 73 Tage still (letztes Datum 2026-07-01),
weil ihr einziger Schreiber — `bulk_ensure_ohlcv` in `discovery_cron.py` —
am deaktivierten Cron "eToro Discovery Pipeline" haengt. Der aktive
discovery_worker laedt alle 2h drei Monate OHLCV fuer sein ganzes
Scan-Universum und warf die Frames danach weg.

Der Krypto-Kontaminations-Guard muss auf diesem neuen Pfad genauso
greifen wie auf dem alten — sonst waere er ein Schlupfloch fuer genau die
Verseuchung, die fix/crypto-symbol-contamination aufgeraeumt hat.
"""
import sqlite3

import pandas as pd
import pytest

from bot.core.ohlcv_cache import store_scan_frame, store_scan_frames


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE instruments (
            instrument_id INTEGER PRIMARY KEY, symbol TEXT,
            yfinance_symbol TEXT, asset_class TEXT, name TEXT,
            yahoo_status TEXT DEFAULT 'ok', yahoo_fail_count INTEGER DEFAULT 0,
            last_updated TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE ohlcv_daily (
            instrument_id INTEGER, date TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume INTEGER, adjusted_close REAL,
            PRIMARY KEY (instrument_id, date)
        )
    """)
    yield c
    c.close()


def _instrument(conn, iid, symbol, yf, ac="stock", name="Test Corp."):
    conn.execute(
        "INSERT INTO instruments "
        "(instrument_id, symbol, yfinance_symbol, asset_class, name) "
        "VALUES (?,?,?,?,?)", (iid, symbol, yf, ac, name))
    conn.commit()


def _frame(n=3, close=100.0):
    """Frame im yf.download-Format: DatetimeIndex + grossgeschriebene Spalten."""
    idx = pd.date_range("2026-09-01", periods=n, freq="D")
    return pd.DataFrame({
        "Open": [close - 1] * n, "High": [close + 2] * n,
        "Low": [close - 2] * n, "Close": [close] * n,
        "Volume": [1_000_000] * n,
    }, index=idx)


# ── Grundfall ────────────────────────────────────────────────────────────────

def test_frame_landet_in_ohlcv_daily(conn):
    _instrument(conn, 1, "AAPL", "AAPL")
    assert store_scan_frame(conn, 1, "AAPL", _frame(3)) == 3
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM ohlcv_daily "
        "WHERE instrument_id=1 ORDER BY date").fetchall()
    assert [r["date"] for r in rows] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert rows[0]["close"] == 100.0 and rows[0]["volume"] == 1_000_000
    assert rows[0]["high"] == 102.0 and rows[0]["low"] == 98.0


def test_auto_adjust_close_wird_als_adjusted_close_gespiegelt(conn):
    # _batch_fetch nutzt auto_adjust=True — Close IST bereits adjustiert.
    _instrument(conn, 1, "AAPL", "AAPL")
    store_scan_frame(conn, 1, "AAPL", _frame(1, close=42.5))
    row = conn.execute("SELECT close, adjusted_close FROM ohlcv_daily").fetchone()
    assert row["close"] == row["adjusted_close"] == 42.5


def test_erneuter_lauf_ueberschreibt_statt_zu_duplizieren(conn):
    _instrument(conn, 1, "AAPL", "AAPL")
    store_scan_frame(conn, 1, "AAPL", _frame(3, close=100.0))
    store_scan_frame(conn, 1, "AAPL", _frame(3, close=111.0))
    rows = conn.execute("SELECT close FROM ohlcv_daily WHERE instrument_id=1").fetchall()
    assert len(rows) == 3 and all(r["close"] == 111.0 for r in rows)


# ── Krypto-Guard gilt auch hier ──────────────────────────────────────────────

def test_krypto_guard_blockt_verseuchtes_stock_instrument(conn):
    # STMicroelectronics→TRX-USD: Fuzzy-Match-Fehlgriff aus dem Juni-Backfill.
    _instrument(conn, 7, "STM", "TRX-USD", ac="stock", name="STMicroelectronics")
    assert store_scan_frame(conn, 7, "TRX-USD", _frame(3)) == 0
    assert conn.execute("SELECT COUNT(*) c FROM ohlcv_daily").fetchone()["c"] == 0


def test_echtes_krypto_instrument_darf_schreiben(conn):
    _instrument(conn, 8, "BTC", "BTC-USD", ac="crypto", name="Bitcoin")
    assert store_scan_frame(conn, 8, "BTC-USD", _frame(3)) == 3


def test_krypto_etp_darf_schreiben(conn):
    _instrument(conn, 9, "BITC.L", "BTC-USD", ac="etf",
                name="CoinShares Physical Bitcoin")
    assert store_scan_frame(conn, 9, "BTC-USD", _frame(3)) == 3


# ── Robustheit: Datensammlung darf den Handelslauf nie kippen ────────────────

def test_leerer_frame_ist_kein_fehler(conn):
    _instrument(conn, 1, "AAPL", "AAPL")
    assert store_scan_frame(conn, 1, "AAPL", pd.DataFrame()) == 0
    assert store_scan_frame(conn, 1, "AAPL", None) == 0


def test_zeilen_ohne_gueltigen_close_werden_uebersprungen(conn):
    _instrument(conn, 1, "AAPL", "AAPL")
    df = _frame(3)
    df.loc[df.index[1], "Close"] = 0.0     # 0 = kein echter Kurs
    df.loc[df.index[2], "Close"] = float("nan")
    assert store_scan_frame(conn, 1, "AAPL", df) == 1


def test_bulk_zaehlt_instrumente_und_zeilen(conn):
    _instrument(conn, 1, "AAPL", "AAPL")
    _instrument(conn, 2, "MSFT", "MSFT")
    _instrument(conn, 7, "STM", "TRX-USD", ac="stock", name="STMicroelectronics")
    n_inst, n_rows = store_scan_frames(conn, {
        1: ("AAPL", _frame(3)),
        2: ("MSFT", _frame(5)),
        7: ("TRX-USD", _frame(4)),   # vom Guard abgewiesen
    })
    assert (n_inst, n_rows) == (2, 8)


def test_bulk_ueberspringt_kaputtes_instrument_statt_abzubrechen(conn):
    _instrument(conn, 1, "AAPL", "AAPL")
    _instrument(conn, 2, "MSFT", "MSFT")
    n_inst, n_rows = store_scan_frames(conn, {
        1: ("AAPL", _frame(2)),
        2: ("MSFT", "kein DataFrame"),   # wuerde intern werfen
    })
    assert (n_inst, n_rows) == (1, 2)
