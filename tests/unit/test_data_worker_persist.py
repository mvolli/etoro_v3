"""fix/ohlcv-from-data-worker (2026-10-17): der data_worker speichert geladene Frames.

Devisen/Futures/Rohstoffe der Watchlist standen in ohlcv_daily seit 25.06. still,
weil nur der Discovery-Scan (Aktien) schrieb.
"""
import pandas as pd

from bot.db.connection import DB
from bot.workers.data_worker import _persist_frames


def _frame(n, start="2026-07-01"):
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.05, "Volume": 10}, index=idx)


def test_speichert_volle_reihe_bei_veraltet_sonst_nur_die_letzten_10(tmp_path):
    d = DB(db_path=tmp_path / "t.db")
    d.execute("CREATE TABLE ohlcv_daily (id INTEGER PRIMARY KEY, instrument_id INTEGER, date TEXT, open REAL, "
              "high REAL, low REAL, close REAL, volume INTEGER, adjusted_close REAL, UNIQUE(instrument_id, date))")
    d.execute("CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT, asset_class TEXT, "
              "yfinance_symbol TEXT)")
    d.execute("INSERT INTO instruments VALUES (60, 'EURNOK', 'forex', 'EURNOK=X')")
    d.execute("INSERT INTO instruments VALUES (61, 'EURSEK', 'forex', 'EURSEK=X')")
    d.execute("INSERT INTO ohlcv_daily (instrument_id, date, close) VALUES (61, date('now'), 1.0)")
    n = _persist_frames(d, {"EURNOK=X": _frame(60), "EURSEK=X": _frame(60), "X": _frame(5)},
                        [{"yf_symbol": "EURNOK=X", "instrument_id": 60},
                         {"yf_symbol": "EURSEK=X", "instrument_id": 61}])
    cnt = {r[0]: r[1] for r in d.fetchall("SELECT instrument_id, COUNT(*) FROM ohlcv_daily GROUP BY 1")}
    assert cnt[60] == 60            # veraltet/leer -> ganzer Frame
    assert cnt[61] == 1 + 10        # aktuell -> nur die letzten 10
    assert n == 70
