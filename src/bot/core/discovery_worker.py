#!/usr/bin/env python3
"""Discovery Worker V2 — Region-basierter Filter-Funnel.

Universum (14.787) → Aktive Watchlist (region-based SQL) → 
OHLCV Cache → Liquidität/Preis/Volatilität-Filter (~200 Candidates) → Signale
"""
import sqlite3
import logging
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = str(PROJECT_ROOT / 'data' / 'trading.db')

# Region-Schedule: 4 Discovery-Läufe pro Tag
REGION_SCHEDULE = {
    'ASIA': {
        'utc_hour': 22,   # 22:00 UTC = 01:00 JST (Asien-Öffnung)
        'regions': ['ASIA_JP', 'ASIA_CN', 'ASIA_AU'],
        'label': '🌏 Asien-Pazifik',
    },
    'EUROPE': {
        'utc_hour': 8,    # 08:00 UTC = 09:00 CET (Europa-Öffnung)
        'regions': ['EU'],
        'label': '🇪🇺 Europa',
    },
    'US_OVERLAP': {
        'utc_hour': 14,   # 14:00 UTC = 09:30 EST (US-Öffnung + EU-US Overlap)
        'regions': ['US', 'EU'],
        'label': '🇺🇸 US + Europa-Overlap',
    },
    'NIGHT_SCAN': {
        'utc_hour': 2,    # 02:00 UTC (Crypto über Nacht + Asien)
        'regions': ['ASIA_JP', 'ASIA_CN', 'ASIA_AU'],
        'asset_classes': ['crypto'],
        'label': '🌙 Night Scan (Crypto)',
    },
}

# Filter-Schwellenwerte
FILTER_CONFIG = {
    'min_avg_volume_usd': 500_000,   # Min. durchschnittliches Handelsvolumen in USD
    'min_price': 1.0,                # Kein Cent-Stock unter $1
    'max_price': 10_000,             # Kein Instrument über $10k (z.B. BTC)
    'min_atr_pct': 0.5,              # Min. ATR(14)/Close als % — muss sich lohnen
    'max_atr_pct': 20.0,             # Max. ATR — keine extremen Volatilitäts-Monster
    'required_ohlcv_days': 30,       # Mindestens 30 Tage Daten für Indikatoren
}


def get_db():
    """SQLite connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_current_region() -> Optional[str]:
    """Bestimme die aktuelle Region basierend auf UTC-Stunde."""
    now_utc = datetime.utcnow().hour
    
    # Finde die passende Region (nächster Lauf)
    for region_key, config in REGION_SCHEDULE.items():
        if abs(now_utc - config['utc_hour']) <= 2:  # ±2 Stunden Window
            return region_key
    
    # Fallback: nächster Lauf
    min_diff = float('inf')
    next_region = None
    for region_key, config in REGION_SCHEDULE.items():
        diff = abs(now_utc - config['utc_hour'])
        if diff < min_diff:
            min_diff = diff
            next_region = region_key
    
    return next_region


def get_candidates_for_region(conn, region_key: str) -> list:
    """SQL-Query für die aktuelle Region — lade aktive Instrumente."""
    config = REGION_SCHEDULE[region_key]
    regions = config['regions']
    
    # SQL Query mit Regionen-Filter
    placeholders = ','.join(['?' for _ in regions])
    
    query = f"""
        SELECT instrument_id, symbol, name, asset_class, yfinance_symbol, exchange_suffix, market_region
        FROM instruments 
        WHERE is_active = 1 
          AND market_region IN ({placeholders})
          AND yfinance_symbol IS NOT NULL
          AND yfinance_symbol != ''
        ORDER BY asset_class, name
    """
    
    c = conn.cursor()
    c.execute(query, regions)
    return [dict(row) for row in c.fetchall()]


def filter_liquidity(conn, candidates: list) -> list:
    """Liquiditäts-Filter: Avg(Close × Volume) > Schwellenwert."""
    if not candidates:
        return []
    
    filtered = []
    min_vol = FILTER_CONFIG['min_avg_volume_usd']
    
    for inst in candidates:
        iid = inst['instrument_id']
        
        # Hole letzten Monat OHLCV
        c = conn.cursor()
        c.execute("""
            SELECT close, volume FROM ohlcv_daily 
            WHERE instrument_id = ? AND close > 0 AND volume > 0
            ORDER BY date DESC LIMIT 30
        """, (iid,))
        
        rows = c.fetchall()
        if len(rows) < 10:  # Zu wenig Daten
            continue
        
        avg_vol_usd = sum(row['close'] * row['volume'] for row in rows) / len(rows)
        
        if avg_vol_usd >= min_vol:
            inst['_avg_volume_usd'] = avg_vol_usd
            filtered.append(inst)
    
    return filtered


def filter_price(candidates: list) -> list:
    """Preis-Filter: Kein Pennystock, kein extrem teures Instrument."""
    min_p = FILTER_CONFIG['min_price']
    max_p = FILTER_CONFIG['max_price']
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    filtered = []
    for inst in candidates:
        iid = inst['instrument_id']
        c.execute("""
            SELECT close FROM ohlcv_daily 
            WHERE instrument_id = ? AND close > 0 
            ORDER BY date DESC LIMIT 1
        """, (iid,))
        
        row = c.fetchone()
        if not row:
            continue
        
        price = row[0]
        if min_p <= price <= max_p:
            inst['_current_price'] = price
            filtered.append(inst)
    
    conn.close()
    return filtered


def calculate_atr(conn, instrument_id: int, period: int = 14) -> Optional[float]:
    """Berechne ATR(14) als Prozentsatz des aktuellen Preises."""
    c = conn.cursor()
    c.execute("""
        SELECT high, low, close FROM ohlcv_daily 
        WHERE instrument_id = ? AND close > 0
        ORDER BY date DESC LIMIT ?
    """, (instrument_id, period + 1))
    
    rows = c.fetchall()
    if len(rows) < period:
        return None
    
    # Reverse to chronological
    rows = list(reversed(rows))
    
    # True Range calculation
    tr_values = []
    for i in range(1, len(rows)):
        high = rows[i]['high']
        low = rows[i]['low']
        prev_close = rows[i-1]['close']
        
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        tr_values.append(tr)
    
    if not tr_values:
        return None
    
    atr = sum(tr_values[-period:]) / period
    current_price = rows[-1]['close']
    
    if current_price > 0:
        return (atr / current_price) * 100  # as percentage
    
    return None


def filter_volatility(conn, candidates: list) -> list:
    """Volatilitäts-Filter: ATR(14)/Close muss im Bereich sein."""
    min_atr = FILTER_CONFIG['min_atr_pct']
    max_atr = FILTER_CONFIG['max_atr_pct']
    
    filtered = []
    for inst in candidates:
        atr_pct = calculate_atr(conn, inst['instrument_id'])
        
        if atr_pct is None:
            continue
        
        if min_atr <= atr_pct <= max_atr:
            inst['_atr_pct'] = round(atr_pct, 2)
            filtered.append(inst)
    
    return filtered


def run_discovery(region_key: Optional[str] = None):
    """Führe einen kompletten Discovery-Lauf durch.
    
    Returns: dict mit Status und Kandidaten
    """
    if region_key is None:
        region_key = get_current_region() or 'US_OVERLAP'  # Default fallback
    
    config = REGION_SCHEDULE[region_key]
    label = config['label']
    
    logger.info(f"=== Discovery Start: {label} ===")
    
    conn = get_db()
    result = {
        'region': region_key,
        'label': label,
        'timestamp': datetime.utcnow().isoformat(),
        'stages': {},
    }
    
    # Stage 1: SQL Query — Kandidaten laden
    candidates = get_candidates_for_region(conn, region_key)
    result['stages']['loaded'] = len(candidates)
    logger.info(f"  Stage 1: {len(candidates)} Instrumente geladen")
    
    if not candidates:
        logger.warning("  Keine Kandidaten für diese Region!")
        conn.close()
        return result
    
    # Stage 2: Liquiditäts-Filter
    candidates = filter_liquidity(conn, candidates)
    result['stages']['after_liquidity'] = len(candidates)
    logger.info(f"  Stage 2 (Liquidität): {len(candidates)} verbleibend")
    
    # Stage 3: Preis-Filter
    candidates = filter_price(candidates)
    result['stages']['after_price'] = len(candidates)
    logger.info(f"  Stage 3 (Preis): {len(candidates)} verbleibend")
    
    # Stage 4: Volatilitäts-Filter
    candidates = filter_volatility(conn, candidates)
    result['stages']['after_volatility'] = len(candidates)
    logger.info(f"  Stage 4 (Volatilität): {len(candidates)} CANDIDATES")
    
    # Sortiere nach ATR (höchste Volatilität zuerst — interessante Bewegungen)
    candidates.sort(key=lambda x: x.get('_atr_pct', 0), reverse=True)
    
    # Top-30 Kandidaten für Embed
    top_candidates = []
    for inst in candidates[:30]:
        top_candidates.append({
            'symbol': inst['symbol'],
            'name': inst['name'],
            'asset_class': inst['asset_class'],
            'price': round(inst.get('_current_price', 0), 2),
            'atr_pct': inst.get('_atr_pct', 0),
            'avg_vol_usd': round(inst.get('_avg_volume_usd', 0), 0),
        })
    
    result['candidates'] = top_candidates
    result['total_candidates'] = len(candidates)
    
    conn.close()
    logger.info(f"=== Discovery Done: {len(candidates)} Candidates ===")
    
    return result


def main():
    """CLI Entry Point."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    
    region = sys.argv[1] if len(sys.argv) > 1 else None
    result = run_discovery(region)
    
    # JSON-Output für Cron-Jobs
    import json
    print(json.dumps({
        'region': result['region'],
        'label': result['label'],
        'stages': result['stages'],
        'total_candidates': result['total_candidates'],
        'top_10': result['candidates'][:10],
    }, indent=2))


if __name__ == '__main__':
    main()
