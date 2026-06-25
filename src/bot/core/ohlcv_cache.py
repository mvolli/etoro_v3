#!/usr/bin/env python3
"""OHLCV Cache mit Check-Fetch-Store Pattern.

CHECK: SELECT MAX(date) FROM ohlcv_daily WHERE instrument_id = ?
FETCH: yfinance.download() nur für fehlende Tage
STORE: INSERT OR REPLACE in ohlcv_daily
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, Any

logger = logging.getLogger(__name__)

DB_PATH = '/home/mvolli/.hermes/workspace/etoro_v3/data/trading.db'


def get_db():
    """SQLite connection mit RowFactory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def check_latest_date(conn, instrument_id: int) -> Optional[str]:
    """CHECK: Hole das neueste Datum für ein Instrument."""
    c = conn.cursor()
    c.execute("SELECT MAX(date) as max_date FROM ohlcv_daily WHERE instrument_id = ?", (instrument_id,))
    row = c.fetchone()
    return row['max_date'] if row and row['max_date'] else None


def fetch_ohlcv(yf_symbol: str, start_date: str, end_date: str) -> Optional[Any]:
    """FETCH: Hole OHLCV-Daten via yfinance für den fehlenden Zeitraum."""
    try:
        import pandas as pd
        import yfinance as yf
        
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(start=start_date, end=end_date, interval='1d')
        
        if df.empty:
            logger.warning(f"yfinance returned empty for {yf_symbol} ({start_date} to {end_date})")
            return None
        
        # Normalize column names
        df.columns = [col.lower() for col in df.columns]
        df.index.name = 'date'
        df = df.reset_index()
        
        # Ensure date is string format
        df['date'] = df['date'].dt.strftime('%Y-%m-%d')
        
        return df
    except Exception as e:
        logger.error(f"yfinance error for {yf_symbol}: {e}")
        return None


def store_ohlcv(conn, instrument_id: int, df) -> int:
    """STORE: Schreibe OHLCV-Daten in DB (INSERT OR REPLACE)."""
    if df is None or df.empty:
        return 0
    
    c = conn.cursor()
    stored = 0
    
    for _, row in df.iterrows():
        try:
            c.execute("""
                INSERT OR REPLACE INTO ohlcv_daily 
                (instrument_id, date, open, high, low, close, volume, adjusted_close)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                instrument_id,
                row['date'],
                float(row.get('open', 0)),
                float(row.get('high', 0)),
                float(row.get('low', 0)),
                float(row.get('close', 0)),
                int(row.get('volume', 0)),
                float(row.get('adjusted close', row.get('close', 0)))
            ))
            stored += 1
        except (KeyError, ValueError) as e:
            logger.debug(f"Skipping row for {instrument_id}: {e}")
    
    conn.commit()
    return stored


def get_ohlcv(conn, instrument_id: int, days: int = 50) -> list:
    """Hole die letzten N Tage OHLCV-Daten aus dem Cache."""
    c = conn.cursor()
    c.execute("""
        SELECT date, open, high, low, close, volume 
        FROM ohlcv_daily 
        WHERE instrument_id = ? 
        ORDER BY date DESC 
        LIMIT ?
    """, (instrument_id, days))
    
    rows = c.fetchall()
    # Reverse to get chronological order
    return [dict(row) for row in reversed(rows)]


def ensure_ohlcv(conn, instrument_id: int, yf_symbol: str, required_days: int = 50):
    """Check-Fetch-Store: Stelle sicher, dass mindestens N Tage Daten vorliegen.
    
    Returns: (has_data: bool, days_available: int)
    """
    # CHECK
    latest = check_latest_date(conn, instrument_id)
    
    if latest:
        latest_dt = datetime.strptime(latest, '%Y-%m-%d')
        needed_start = (latest_dt - timedelta(days=required_days)).strftime('%Y-%m-%d')
        
        # Prüfe ob wir genug Daten haben
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM ohlcv_daily WHERE instrument_id = ?", (instrument_id,))
        existing_count = c.fetchone()['cnt']
        
        if existing_count >= required_days:
            return True, existing_count
        
        # Nur fehlende Tage holen
        today = datetime.utcnow().strftime('%Y-%m-%d')
        df = fetch_ohlcv(yf_symbol, needed_start, today)
    else:
        # Kein Cache – alles holen
        end_date = datetime.utcnow().strftime('%Y-%m-%d')
        start_date = (datetime.utcnow() - timedelta(days=required_days + 10)).strftime('%Y-%m-%d')
        df = fetch_ohlcv(yf_symbol, start_date, end_date)
    
    if df is not None and not df.empty:
        # STORE
        stored = store_ohlcv(conn, instrument_id, df)
        
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM ohlcv_daily WHERE instrument_id = ?", (instrument_id,))
        total = c.fetchone()['cnt']
        
        if total >= required_days:
            return True, total
        
        logger.warning(f"{yf_symbol}: only {total} days available after fetch (needed {required_days})")
        return total > 0, total
    
    return False, 0


def bulk_ensure_ohlcv(conn, instruments: list, required_days: int = 50, batch_size: int = 10):
    """Check-Fetch-Store für eine Liste von Instrumenten in Batches.
    
    Args:
        conn: SQLite connection
        instruments: List of dicts mit keys: instrument_id, yfinance_symbol
        required_days: Anzahl Tage die verfügbar sein müssen
        batch_size: Batch-Größe für yfinance (vermeidet Rate-Limits)
    
    Returns:
        dict mit Status pro Instrument: {instrument_id: {'has_data': bool, 'days': int}}
    """
    results = {}
    total = len(instruments)
    
    for i in range(0, total, batch_size):
        batch = instruments[i:i + batch_size]
        
        for inst in batch:
            iid = inst['instrument_id']
            yf_sym = inst.get('yfinance_symbol', inst.get('symbol'))
            
            if not yf_sym:
                results[iid] = {'has_data': False, 'days': 0, 'error': 'no_yf_symbol'}
                continue
            
            has_data, days = ensure_ohlcv(conn, iid, yf_sym, required_days)
            results[iid] = {'has_data': has_data, 'days': days}
            
            progress = min(i + len(batch), total)
            if progress % 50 == 0 or progress == total:
                logger.info(f"OHLCV Cache: {progress}/{total} instruments processed")
    
    return results
