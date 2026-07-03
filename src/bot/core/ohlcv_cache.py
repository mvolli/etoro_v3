#!/usr/bin/env python3
"""OHLCV Cache mit Check-Fetch-Store Pattern.

CHECK: SELECT MAX(date) FROM ohlcv_daily WHERE instrument_id = ?
FETCH: yfinance.download() nur für fehlende Tage
STORE: INSERT OR REPLACE in ohlcv_daily
"""
import re
import sqlite3
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Any
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DB_PATH = str(PROJECT_ROOT / 'data' / 'trading.db')

# ── Ticker alias map: eToro symbol → correct yfinance ticker ─────────────────
# fix/yfinance-ticker-resolution: several instruments have a different ticker
# on Yahoo Finance than their eToro display symbol. This map is the CURATED
# first candidate in generate_symbol_candidates(); fetch_ohlcv() tries all
# candidates with retries (fix/yfinance-fallback-resolution).
#
# Known mismatches (verified 2026-07-03):
#   00027.HK / 0027.HK → 0728.HK  (China Telecom HK; 0027.HK = Galaxy Ent!)
#   CVX.US             → CVX      (Chevron; Yahoo Finance has no .US suffix)
#   AAPL.US etc.       → AAPL     (US stocks — eToro adds .US, Yahoo doesn't need it)
#   06881.HK           → 6881.HK  (stored yfinance_symbol was 'GALA-USD' — crypto,
#                                  unrelated instrument; open position, was firing
#                                  "possibly delisted" every data_worker cycle)
#   HMC.ASX            → HMC.AX   (stored yfinance_symbol 'HCL.AX' = HighCom Ltd,
#                                  a DIFFERENT company — HMC Capital Ltd is HMC.AX)
#   PNI.ASX            → PNI.AX   (stored yfinance_symbol 'PIM.AX' = Pinnacle
#                                  Minerals Ltd, a DIFFERENT company — Pinnacle
#                                  Investment Management Group is PNI.AX)
YFINANCE_TICKER_MAP: dict[str, str] = {
    # Hong Kong stocks (eToro uses leading zeros, Yahoo strips them)
    "00027.HK": "0728.HK",   # China Telecom HK — 0027.HK is Galaxy Entertainment!
    "0027.HK":  "0728.HK",   # Same instrument under wrong DB yfinance_symbol
    "06881.HK": "6881.HK",   # stored yfinance_symbol was 'GALA-USD' (crypto!)
    # US stocks with .US suffix (eToro adds it, Yahoo doesn't need it)
    "CVX.US":   "CVX",       # Chevron
    "AAPL.US":  "AAPL",
    "MSFT.US":  "MSFT",
    "GOOGL.US": "GOOGL",
    "AMZN.US":  "AMZN",
    "META.US":  "META",
    "TSLA.US":  "TSLA",
    "NVDA.US":  "NVDA",
    # ASX stocks — stored yfinance_symbol resolved to a DIFFERENT company
    "HMC.ASX":  "HMC.AX",    # HMC Capital Ltd — stored value was HighCom Ltd
    "PNI.ASX":  "PNI.AX",    # Pinnacle Investment Mgmt — stored value was Pinnacle Minerals
}


def _resolve_yf_symbol(symbol: str) -> str:
    """Resolve an eToro symbol to the correct yfinance ticker.

    Checks YFINANCE_TICKER_MAP first, then falls back to the raw symbol.
    This prevents sending wrong tickers (e.g. CVX.US instead of CVX) to Yahoo.
    """
    return YFINANCE_TICKER_MAP.get(symbol, symbol)


# fix/eu-yfinance-fallback: EU exchange suffixes, checked structurally in
# _add_structural_variants(). Verified live 2026-07-03 on a 20-instrument
# sample of DB rows marked yahoo_status='delisted': 12/20 resolved directly
# under the plain eToro symbol (the stored yfinance_symbol was simply wrong —
# e.g. BMWA.DE / MRA.DE / FS&.DE instead of the correct BMW.DE / MUV2.DE /
# FRE.DE), 3 more needed the .ZU→.SW swap (Zurich tickers are .SW on Yahoo),
# 2 more needed a hyphen before the Nordic share-class letter (ELUXB.ST →
# ELUX-B.ST). The remaining 3/20 were genuine delistings/malformed symbols —
# no structural rule can or should rescue those.
EU_SUFFIXES = ('.DE', '.PA', '.L', '.ST', '.MI', '.CO', '.SW', '.LS', '.ZU', '.AS', '.BR', '.OL', '.MC')
NORDIC_SHARE_CLASS_SUFFIXES = ('.ST', '.CO', '.OL')

# fix/asx-yfinance-suffix: eToro uses '.ASX' for Australian stocks; Yahoo uses
# '.AX'. ALL 436 ASIA_AU instruments in the DB have symbol != yfinance_symbol
# (same systemic-seeding bug as the EU rows) — two currently-held positions
# (HMC.ASX, PNI.ASX) had a stored yfinance_symbol resolving to a DIFFERENT
# ASX company entirely (see YFINANCE_TICKER_MAP comments above).
ASX_SUFFIX = '.ASX'
ASX_YAHOO_SUFFIX = '.AX'


def _add_structural_variants(symbol: str, add) -> None:
    """Add identity-preserving spelling variants of *symbol* via *add(s)*.

    HK zero-padding / .HKG swap, .US-suffix strip, EU suffix fixes, ASX
    suffix swap.
    """
    m = re.match(r'^(\d+)\.(HK|HKG)$', symbol, re.IGNORECASE)
    if m:
        digits = m.group(1)
        add(f"{int(digits):04d}.HK")   # Yahoo standard: 4-stellig, Codenummer bleibt gleich
        add(f"{digits}.HK")
        add(f"{digits}.HKG")
    if symbol.upper().endswith('.US'):
        add(symbol[:-3])
    if symbol.upper().endswith(ASX_SUFFIX):
        add(f"{symbol[:-len(ASX_SUFFIX)]}{ASX_YAHOO_SUFFIX}")
    for suf in EU_SUFFIXES:
        if symbol.upper().endswith(suf):
            base = symbol[: -len(suf)]
            if suf == '.ZU':
                add(f"{base}.SW")
            if (
                suf in NORDIC_SHARE_CLASS_SUFFIXES
                and len(base) >= 3
                and base[-1] in ('A', 'B')
                and '-' not in base
            ):
                add(f"{base[:-1]}-{base[-1]}{suf}")
            break


# ── Fallback symbol resolution ────────────────────────────────────────────────
# fix/yfinance-fallback-resolution: when the primary yfinance_symbol fails
# ("possibly delisted"), alternative ticker spellings are tried before the
# instrument is written off. Successful resolutions are logged, cached for the
# session and persisted (symbol_resolutions table + instruments.yfinance_symbol).

MAX_FETCH_RETRIES = 2        # extra attempts per candidate on transient errors
RETRY_BACKOFF_S = 2.0        # backoff base: 2s, 4s, ...

# Session cache: original symbol → ticker that actually worked.
_RESOLVED_SYMBOL_CACHE: dict[str, str] = {}


def generate_symbol_candidates(symbol: str, original_symbol: str | None = None) -> list[str]:
    """Return ordered, deduplicated ticker candidates for a symbol.

    Order: session-cached resolution, static alias map, the raw symbol,
    then structural variants (HK zero-padding, .HKG/.HK swap, .US strip, EU
    suffix fixes) — and, if the caller passes the known-correct eToro
    original_symbol and it differs from *symbol*, that symbol and ITS
    structural variants too (fix/eu-yfinance-fallback: for many EU
    instruments the stored yfinance_symbol was simply wrong in a way no
    structural rule on it alone can derive — the original eToro symbol is
    the highest-confidence candidate in that case).

    WICHTIG: Für Symbole mit kuratiertem Alias in YFINANCE_TICKER_MAP gibt es
    KEINE strukturellen Rate-Kandidaten. Ein kuratierter Eintrag existiert
    genau dann, wenn die naive Umformung falsch wäre (00027.HK ist bei eToro
    China Telecom, aber 0027.HK ist auf Yahoo Galaxy Entertainment!) — ein
    struktureller Fallback würde dort die FALSCHE Firma liefern.
    Für ungemappte Symbole sind die Varianten identitätserhaltend (gleiche
    HK-Codenummer, gleicher US-Ticker ohne Suffix, gleiche EU-Aktie).
    """
    candidates: list[str] = []

    def add(s: str) -> None:
        if s and s not in candidates:
            candidates.append(s)

    cached = _RESOLVED_SYMBOL_CACHE.get(symbol)
    if cached:
        add(cached)

    if symbol in YFINANCE_TICKER_MAP:
        add(_resolve_yf_symbol(symbol))
        add(symbol)
        return candidates   # kuratierte Zuordnung — keine strukturellen Ratereien

    add(symbol)
    _add_structural_variants(symbol, add)

    if original_symbol and original_symbol != symbol:
        add(original_symbol)
        _add_structural_variants(original_symbol, add)

    return candidates


def _persist_resolution(conn, instrument_id: Optional[int], original: str, resolved: str) -> None:
    """Persist a successful fallback resolution.

    Audit-Log (symbol_resolutions) immer; instruments.yfinance_symbol wird nur
    für KURATIERTE Aliase (YFINANCE_TICKER_MAP) überschrieben. Strukturelle
    Varianten bleiben Session-Wissen — eine geratene Schreibweise darf die
    DB-Wahrheit nicht dauerhaft ersetzen (Fehlgriff wäre selbstverstärkend).
    """
    _RESOLVED_SYMBOL_CACHE[original] = resolved
    if conn is None:
        return
    curated = YFINANCE_TICKER_MAP.get(original) == resolved
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS symbol_resolutions (
                original_symbol TEXT NOT NULL,
                resolved_symbol TEXT NOT NULL,
                instrument_id   INTEGER,
                curated         INTEGER NOT NULL DEFAULT 0,
                resolved_at     TEXT NOT NULL DEFAULT (datetime('now','utc')),
                PRIMARY KEY (original_symbol, resolved_symbol)
            )
        """)
        c.execute("""
            INSERT OR REPLACE INTO symbol_resolutions
                (original_symbol, resolved_symbol, instrument_id, curated)
            VALUES (?, ?, ?, ?)
        """, (original, resolved, instrument_id, int(curated)))
        if curated and instrument_id is not None:
            c.execute(
                "UPDATE instruments SET yfinance_symbol = ?, last_updated = CURRENT_TIMESTAMP"
                " WHERE instrument_id = ?",
                (resolved, instrument_id),
            )
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not persist symbol resolution {original} → {resolved}: {e}")


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


def is_delisted_error(yf_symbol: str, error_msg: str) -> bool:
    """Check ob der Error auf ein delistetes Symbol hindeutet."""
    delisted_keywords = [
        'no data found', 'delisted', 'not found', 'quote not found',
        'possibly delisted', '404'
    ]
    error_lower = str(error_msg).lower()
    return any(kw in error_lower for kw in delisted_keywords)


def update_yahoo_status(conn, instrument_id: int, yf_symbol: str, success: bool):
    """Update yahoo_status + fail_count für ein Instrument.
    
    Nach 3+ fehlgeschlagenen Versuchen mit delisted-Error → 'delisted'.
    Bei Erfolg → zurück auf 'ok', fail_count=0.
    """
    c = conn.cursor()
    
    if success:
        c.execute("""
            UPDATE instruments 
            SET yahoo_status = 'ok', 
                yahoo_fail_count = 0,
                last_updated = CURRENT_TIMESTAMP
            WHERE instrument_id = ?
        """, (instrument_id,))
    else:
        # Increment fail count
        c.execute("""
            UPDATE instruments 
            SET yahoo_fail_count = COALESCE(yahoo_fail_count, 0) + 1,
                last_updated = CURRENT_TIMESTAMP
            WHERE instrument_id = ?
        """, (instrument_id,))
        
        # Nach 3+ Fehlschlägen → delisted
        c.execute("""
            UPDATE instruments 
            SET yahoo_status = 'delisted',
                last_updated = CURRENT_TIMESTAMP
            WHERE instrument_id = ? AND COALESCE(yahoo_fail_count, 0) >= 3
        """, (instrument_id,))
        
    conn.commit()


def _fetch_yf_once(ticker_symbol: str, start_date: str, end_date: str) -> tuple[Optional[Any], str]:
    """Single yfinance attempt for one concrete ticker.

    Returns (df_or_None, outcome):
      'ok'        — Daten erhalten
      'no_data'   — leere Antwort (falscher Ticker ODER nur keine Handelstage)
      'delisted'  — expliziter delisted/not-found Fehler von Yahoo
      'transient' — Netzwerk/Rate-Limit, Retry auf demselben Ticker sinnvoll
    """
    try:
        import yfinance as yf

        ticker = yf.Ticker(ticker_symbol)
        df = ticker.history(start=start_date, end=end_date, interval='1d')

        if df.empty:
            logger.debug(f"yfinance returned empty for {ticker_symbol} ({start_date} to {end_date})")
            return None, 'no_data'

        # Normalize column names
        df.columns = [col.lower() for col in df.columns]
        df.index.name = 'date'
        df = df.reset_index()

        # Ensure date is string format
        df['date'] = df['date'].dt.strftime('%Y-%m-%d')

        return df, 'ok'
    except Exception as e:
        delisted = is_delisted_error(ticker_symbol, str(e))
        level = logger.debug if delisted else logger.warning
        level(f"yfinance error for {ticker_symbol}: {e}" + (" [DELISTED]" if delisted else ""))
        return None, 'delisted' if delisted else 'transient'


def fetch_ohlcv(yf_symbol: str, start_date: str, end_date: str) -> tuple[Optional[Any], bool, Optional[str]]:
    """FETCH: Hole OHLCV-Daten via yfinance, mit Fallback-Symbol-Resolution.

    Probiert alle Kandidaten aus generate_symbol_candidates() der Reihe nach.
    Pro Kandidat bis zu MAX_FETCH_RETRIES Wiederholungen bei transienten
    Fehlern (Netzwerk/Rate-Limit); leere/"delisted"-Antworten führen sofort
    zum nächsten Kandidaten.

    Returns tuple: (df_or_None, is_delisted, resolved_symbol_or_None)
    is_delisted=True nur bei explizitem delisted-Fehler (leere Antworten
    zählen NICHT — die laufen über die 3-Strikes-Regel in update_yahoo_status).
    resolved_symbol ist der Ticker, der tatsächlich Daten geliefert hat.
    """
    candidates = generate_symbol_candidates(yf_symbol)
    any_delisted = False

    for cand in candidates:
        outcome = None
        for attempt in range(MAX_FETCH_RETRIES + 1):
            df, outcome = _fetch_yf_once(cand, start_date, end_date)

            if outcome == 'ok':
                if cand != yf_symbol and _RESOLVED_SYMBOL_CACHE.get(yf_symbol) != cand:
                    logger.info(f"✅ Symbol-Resolution: {yf_symbol} → {cand} (Kandidat lieferte Daten)")
                _RESOLVED_SYMBOL_CACHE[yf_symbol] = cand
                return df, False, cand

            if outcome == 'delisted':
                any_delisted = True
                break  # expliziter Yahoo-Fehler → nächster Kandidat
            if outcome == 'no_data':
                break  # leere Antwort → Retry hilft nicht, nächster Kandidat

            if attempt < MAX_FETCH_RETRIES:
                wait = RETRY_BACKOFF_S * (attempt + 1)
                logger.debug(f"{cand}: transient error, retry {attempt + 1}/{MAX_FETCH_RETRIES} in {wait:.0f}s")
                time.sleep(wait)

        if outcome == 'transient':
            # Rate-Limit/Netzwerkproblem trifft ALLE Kandidaten gleichermaßen —
            # weitere Kandidaten würden den Rate-Limiter nur zusätzlich hämmern.
            logger.warning(f"{yf_symbol}: transient errors exhausted on '{cand}' — aborting remaining candidates")
            break

    logger.warning(
        f"yfinance: no data for {yf_symbol} after trying {len(candidates)} candidate(s): "
        f"{', '.join(candidates)}" + (" [DELISTED]" if any_delisted else "")
    )
    return None, any_delisted, None


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


def get_cached_ohlcv_df(conn, instrument_id: int, days: int = 90) -> Optional[Any]:
    """Return cached OHLCV data as a pandas DataFrame (for signals.py).

    fix/double-fetch: allows analyze_batch() to use already-cached DB data
    instead of re-downloading from yfinance. Returns None if insufficient data.
    """
    try:
        import pandas as pd
    except ImportError:
        return None

    rows = get_ohlcv(conn, instrument_id, days)
    if len(rows) < 30:
        return None

    df = pd.DataFrame(rows)
    # Convert numeric columns
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    # Capitalize column names to match signals.py expectations
    df.columns = [col.capitalize() if col != 'date' else 'Date' for col in df.columns]
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    return df[['Open', 'High', 'Low', 'Close', 'Volume']]


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
        c.execute("SELECT COUNT(*) as cnt FROM ohlcv_daily WHERE instrument_id = ? AND date >= ?", (instrument_id, needed_start))
        existing_count = c.fetchone()['cnt']
        
        if existing_count >= required_days:
            update_yahoo_status(conn, instrument_id, yf_symbol, success=True)
            return True, existing_count

        # Nur fehlende Tage holen
        today = datetime.utcnow().strftime('%Y-%m-%d')
        df, delisted, resolved = fetch_ohlcv(yf_symbol, needed_start, today)
    else:
        # Kein Cache – alles holen
        end_date = datetime.utcnow().strftime('%Y-%m-%d')
        start_date = (datetime.utcnow() - timedelta(days=required_days + 10)).strftime('%Y-%m-%d')
        df, delisted, resolved = fetch_ohlcv(yf_symbol, start_date, end_date)

    # Update Yahoo Status
    if df is not None and not df.empty:
        # Fallback-Resolution persistieren (Audit-Log + instruments.yfinance_symbol)
        if resolved and resolved != yf_symbol:
            _persist_resolution(conn, instrument_id, yf_symbol, resolved)
        # STORE
        stored = store_ohlcv(conn, instrument_id, df)
        update_yahoo_status(conn, instrument_id, yf_symbol, success=True)
        
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM ohlcv_daily WHERE instrument_id = ?", (instrument_id,))
        total = c.fetchone()['cnt']
        
        if total >= required_days:
            return True, total
        
        logger.warning(f"{yf_symbol}: only {total} days available after fetch (needed {required_days})")
        return total > 0, total
    
    # Kein Daten → fail count erhöhen
    update_yahoo_status(conn, instrument_id, yf_symbol, success=False)
    
    # Wenn delisted, sofort markieren (fail_count auf 3 setzen)
    if delisted:
        c = conn.cursor()
        c.execute("""
            UPDATE instruments 
            SET yahoo_fail_count = MAX(COALESCE(yahoo_fail_count, 0), 3),
                yahoo_status = 'delisted',
                last_updated = CURRENT_TIMESTAMP
            WHERE instrument_id = ?
        """, (instrument_id,))
        conn.commit()
        logger.warning(f"{yf_symbol}: marked as DELISTED (Yahoo returns no data)")
    
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
    
    # Skip delisted instrumente
    c = conn.cursor()
    delisted_ids = set()
    for inst in instruments:
        iid = inst['instrument_id']
        c.execute("SELECT COALESCE(yahoo_status, 'unknown') FROM instruments WHERE instrument_id = ?", (iid,))
        row = c.fetchone()
        if row and row[0] == 'delisted':
            delisted_ids.add(iid)
    
    skipped_delisted = 0
    
    for i in range(0, total, batch_size):
        batch = instruments[i:i + batch_size]
        
        for inst in batch:
            iid = inst['instrument_id']
            
            # Skip delisted
            if iid in delisted_ids:
                results[iid] = {'has_data': False, 'days': 0, 'error': 'yahoo_delisted'}
                skipped_delisted += 1
                continue
            
            yf_sym = inst.get('yfinance_symbol', inst.get('symbol'))

            if not yf_sym:
                results[iid] = {'has_data': False, 'days': 0, 'error': 'no_yf_symbol'}
                continue

            # Alias/Fallback-Resolution passiert in fetch_ohlcv() — hier das
            # DB-Original durchreichen, damit Resolutionen korrekt persistiert werden.
            has_data, days = ensure_ohlcv(conn, iid, yf_sym, required_days)
            results[iid] = {'has_data': has_data, 'days': days}
            
            progress = min(i + len(batch), total)
            if progress % 50 == 0 or progress == total:
                logger.info(f"OHLCV Cache: {progress}/{total} instruments processed")
    
    if skipped_delisted > 0:
        logger.info(f"OHLCV Cache: skipped {skipped_delisted} delisted instruments")
    
    return results
