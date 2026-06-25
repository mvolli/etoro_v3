"""Signal Generator — RSI + Bollinger Bands + MACD auf OHLCV-Daten.

Erzeugt Buy/Sell/Hold-Signale basierend auf technischen Indikatoren.
Wird vom Discovery Worker nach dem Filter-Funnel aufgerufen.

Signale werden in der DB gespeichert und via Discord Embed gepostet.
"""

import sqlite3
import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("/home/mvolli/.hermes/workspace/etoro_v3/data/trading.db")


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ─── Technische Indikatoren ──────────────────────────────────────────

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (RSI)."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_bollinger_bands(close: pd.Series, period: int = 20, std_dev: float = 2.0):
    """Bollinger Bands → (upper, middle, lower, bandwidth)."""
    middle = close.rolling(window=period, min_periods=period).mean()
    std = close.rolling(window=period, min_periods=period).std()
    upper = middle + (std * std_dev)
    lower = middle - (std * std_dev)
    bandwidth = (upper - lower) / middle.replace(0, np.nan) * 100
    return upper, middle, lower, bandwidth


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD → (macd_line, signal_line, histogram)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=period).mean()
    return atr


# ─── Signal-Logik ────────────────────────────────────────────────────

def generate_signals(conn: sqlite3.Connection, instrument_ids: list = None):
    """Generiert Signale für alle Instrumente mit ≥20 OHLCV-Tagen.
    
    Returns list of signal dicts sorted by composite_score desc.
    """
    query = """
        SELECT i.instrument_id, i.symbol, i.name, i.asset_class, i.market_region
        FROM instruments i
        JOIN ohlcv_daily o ON i.instrument_id = o.instrument_id
        WHERE i.is_active = 1
    """
    params = []
    
    if instrument_ids:
        placeholders = ','.join('?' for _ in instrument_ids)
        query += f" AND i.instrument_id IN ({placeholders})"
        params = instrument_ids
    
    query += """
        GROUP BY i.instrument_id
        HAVING COUNT(o.date) >= 20
        ORDER BY i.symbol
    """
    
    c = conn.cursor()
    c.execute(query, params)
    instruments = c.fetchall()
    
    signals = []
    
    for inst in instruments:
        iid = inst['instrument_id']
        symbol = inst['symbol']
        
        # Hole OHLCV-Daten
        c.execute("""
            SELECT date, open, high, low, close, volume 
            FROM ohlcv_daily 
            WHERE instrument_id = ? 
            ORDER BY date ASC
        """, (iid,))
        
        rows = c.fetchall()
        if len(rows) < 20:
            continue
        
        df = pd.DataFrame([dict(r) for r in rows])
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        
        try:
            signal = _analyze_instrument(symbol, inst['name'], inst['asset_class'], 
                                         inst['market_region'], iid, df)
            if signal:
                signals.append(signal)
        except Exception as e:
            print(f"  ⚠ {symbol}: Error — {e}")
            continue
    
    # Sortiere nach composite_score (höchste zuerst)
    signals.sort(key=lambda s: s['composite_score'], reverse=True)
    return signals


def _analyze_instrument(symbol, name, asset_class, region, iid, df):
    """Analyse eines einzelnen Instruments → Signal-Dict oder None."""
    
    close = df['close']
    current_price = close.iloc[-1]
    
    # ── Indikatoren berechnen ──
    rsi = calc_rsi(close, 14)
    upper, middle, lower, bandwidth = calc_bollinger_bands(close, 20, 2.0)
    macd_line, signal_line, histogram = calc_macd(close)
    atr = calc_atr(df, 14)
    
    # Aktuelle Werte (letzte Zeile)
    rsi_val = rsi.iloc[-1]
    macd_val = macd_line.iloc[-1]
    signal_val = signal_line.iloc[-1]
    hist_val = histogram.iloc[-1]
    atr_val = atr.iloc[-1]
    bb_upper = upper.iloc[-1]
    bb_lower = lower.iloc[-1]
    bb_mid = middle.iloc[-1]
    
    # RSI-Change (letzte 3 Tage)
    rsi_change = rsi.iloc[-1] - rsi.iloc[-4] if len(rsi) >= 4 else 0
    
    # Price position in BB (0=untere Band, 1=obere Band)
    bb_range = bb_upper - bb_lower
    bb_pos = (current_price - bb_lower) / bb_range if bb_range > 0 else 0.5
    
    # ── Scoring ──
    buy_score = 0.0
    sell_score = 0.0
    reasons = []
    
    # RSI-Score
    if rsi_val < 30:
        buy_score += 3
        reasons.append(f"RSI oversold ({rsi_val:.1f})")
    elif rsi_val < 40:
        buy_score += 1.5
        reasons.append(f"RSI low ({rsi_val:.1f})")
    elif rsi_val > 70:
        sell_score += 3
        reasons.append(f"RSI overbought ({rsi_val:.1f})")
    elif rsi_val > 60:
        sell_score += 1.5
        reasons.append(f"RSI high ({rsi_val:.1f})")
    
    # RSI-Momentum (steigend = bullish)
    if rsi_change > 3:
        buy_score += 1.5
        reasons.append(f"RSI momentum +{rsi_change:.1f}")
    elif rsi_change < -3:
        sell_score += 1.5
        reasons.append(f"RSI momentum {rsi_change:.1f}")
    
    # BB-Score
    if bb_pos < 0.2:
        buy_score += 2
        reasons.append(f"Price near lower BB ({bb_pos:.0%})")
    elif bb_pos < 0.4:
        buy_score += 0.5
    elif bb_pos > 0.8:
        sell_score += 2
        reasons.append(f"Price near upper BB ({bb_pos:.0%})")
    elif bb_pos > 0.6:
        sell_score += 0.5
    
    # BB-Bandwidth (enge Bänder = Ausbruch kommt)
    if len(bandwidth.dropna()) >= 10:
        bw_current = bandwidth.iloc[-1]
        bw_25pct = bandwidth.quantile(0.25)
        if pd.notna(bw_current) and pd.notna(bw_25pct) and bw_current < bw_25pct:
            reasons.append("BB squeezing → Ausbruch möglich")
            buy_score += 0.5
    
    # MACD-Score
    if hist_val > 0 and histogram.iloc[-2] < 0:
        buy_score += 3
        reasons.append("MACD bullish crossover")
    elif hist_val < 0 and histogram.iloc[-2] > 0:
        sell_score += 3
        reasons.append("MACD bearish crossover")
    elif hist_val > 0:
        buy_score += 1
        reasons.append("MACD bullish")
    elif hist_val < 0:
        sell_score += 1
        reasons.append("MACD bearish")
    
    # ── Composite Score ──
    composite = buy_score - sell_score
    
    # Signal bestimmen
    if composite >= 4:
        signal_type = "STRONG_BUY"
    elif composite >= 2:
        signal_type = "BUY"
    elif composite <= -4:
        signal_type = "STRONG_SELL"
    elif composite <= -2:
        signal_type = "SELL"
    else:
        signal_type = "HOLD"
    
    # Price change (7d, 30d)
    price_7d_ago = close.iloc[-8] if len(close) >= 8 else current_price
    price_30d_ago = close.iloc[-31] if len(close) >= 31 else current_price
    change_7d = (current_price - price_7d_ago) / price_7d_ago * 100
    change_30d = (current_price - price_30d_ago) / price_30d_ago * 100
    
    # ATR% 
    atr_pct = (atr_val / current_price * 100) if current_price > 0 else 0
    
    return {
        'instrument_id': iid,
        'symbol': symbol,
        'name': name,
        'asset_class': asset_class,
        'region': region,
        'signal_type': signal_type,
        'composite_score': round(composite, 1),
        'buy_score': round(buy_score, 1),
        'sell_score': round(sell_score, 1),
        'price': round(current_price, 2),
        'rsi': round(rsi_val, 1),
        'macd_hist': round(hist_val, 4),
        'bb_pos': round(bb_pos, 3),
        'atr_pct': round(atr_pct, 2),
        'change_7d': round(change_7d, 2),
        'change_30d': round(change_30d, 2),
        'reasons': reasons,
        'days_data': len(df),
    }


def save_signals(conn: sqlite3.Connection, signals: list):
    """Speichert Signale in der DB (upsert)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id INTEGER NOT NULL,
            signal_type TEXT NOT NULL,
            composite_score REAL NOT NULL,
            buy_score REAL,
            sell_score REAL,
            price REAL,
            rsi REAL,
            macd_hist REAL,
            bb_pos REAL,
            atr_pct REAL,
            change_7d REAL,
            change_30d REAL,
            reasons TEXT,
            days_data INTEGER,
            generated_at TEXT NOT NULL,
            UNIQUE(instrument_id, generated_at)
        )
    """)
    
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    
    for s in signals:
        conn.execute("""
            INSERT OR REPLACE INTO signals 
            (instrument_id, signal_type, composite_score, buy_score, sell_score,
             price, rsi, macd_hist, bb_pos, atr_pct, change_7d, change_30d,
             reasons, days_data, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            s['instrument_id'], s['signal_type'], s['composite_score'],
            s['buy_score'], s['sell_score'], s['price'], s['rsi'],
            s['macd_hist'], s['bb_pos'], s['atr_pct'],
            s['change_7d'], s['change_30d'],
            json.dumps(s['reasons']), s['days_data'], now
        ))
    
    conn.commit()


if __name__ == "__main__":
    import json
    
    conn = get_db()
    
    print("=" * 60)
    print("📊 Signal Generator — Discovery Candidates")
    print("=" * 60)
    
    signals = generate_signals(conn)
    save_signals(conn, signals)
    
    if not signals:
        print("\nKeine Signale generiert (zu wenig OHLCV-Daten)")
        conn.close()
        exit(0)
    
    # Zusammenfassung
    buy_count = sum(1 for s in signals if 'BUY' in s['signal_type'])
    sell_count = sum(1 for s in signals if 'SELL' in s['signal_type'])
    hold_count = sum(1 for s in signals if s['signal_type'] == 'HOLD')
    
    print(f"\n{len(signals)} Instrumente analysiert:")
    print(f"  🟢 BUY/STRONG_BUY: {buy_count}")
    print(f"  🔴 SELL/STRONG_SELL: {sell_count}")
    print(f"  ⚪ HOLD: {hold_count}")
    
    # Top-Buy Signale
    print(f"\n{'='*60}")
    print("🏆 TOP BUY SIGNALS")
    print("=" * 60)
    
    for s in signals[:15]:
        emoji = "🟢" if 'BUY' in s['signal_type'] else ("🔴" if 'SELL' in s['signal_type'] else "⚪")
        print(f"\n{emoji} {s['symbol']} ({s['name']}) — ${s['price']}")
        print(f"   Signal: {s['signal_type']} (Score: {s['composite_score']:+.1f})")
        print(f"   RSI: {s['rsi']} | MACD: {s['macd_hist']:+.4f} | ATR: {s['atr_pct']}%")
        print(f"   7d: {s['change_7d']:+.1f}% | 30d: {s['change_30d']:+.1f}%")
        if s['reasons']:
            print(f"   Gründe: {'; '.join(s['reasons'][:3])}")
    
    conn.close()
