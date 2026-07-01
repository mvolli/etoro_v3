#!/usr/bin/env python3
"""Discovery Cron — Läuft 4x/Tag, cacht OHLCV + generiert Signale + postet Embed.

Region-Schedule (UTC):
  22:00 → ASIA (Japan, HK, Australia)
  08:00 → EUROPE
  14:00 → US_OVERLAP (US + EU)
  02:00 → NIGHT_SCAN (Crypto)

V5: Nutzt signals.py (yfinance + ta library) statt signal_generator.py.
Signale werden über repo.py in die V5-signals-Tabelle geschrieben.
"""

import sys, os, json, time, sqlite3, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from bot.core.ohlcv_cache import get_db as get_ohlcv_db, bulk_ensure_ohlcv
from bot.core.signals import analyze_batch
from bot.db.connection import DB
from bot.db.repo import SignalRepo

TRADES_CHANNEL = "1514786489110630600"  # #etoro-trades

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', 'src', '..')
DB_PATH = os.path.join(PROJECT_ROOT, 'data', 'trading.db')


def get_current_region():
    """Bestimme Region basierend auf aktueller UTC-Zeit."""
    now = datetime.datetime.utcnow().hour
    if 21 <= now or now < 1:      # 21:00-00:59
        return "ASIA"
    elif 1 <= now < 3:            # 01:00-02:59
        return "CRYPTO"
    elif 7 <= now < 11:           # 07:00-10:59
        return "EUROPE"
    elif 13 <= now < 17:          # 13:00-16:59
        return "US_OVERLAP"
    else:
        return None  # Zwischendurch nicht relevant


REGION_QUERIES = {
    "ASIA": {
        "where": "market_region IN ('ASIA_JP', 'ASIA_CN', 'ASIA_AU') AND asset_class = 'stock'",
        "label": "🌏 Asien-Pazifik",
        "limit": 50,
    },
    "EUROPE": {
        "where": "market_region = 'EU' AND asset_class = 'stock'",
        "label": "🇪🇺 Europa",
        "limit": 80,
    },
    "US_OVERLAP": {
        "where": "market_region IN ('US', 'EU') AND asset_class = 'stock'",
        "label": "🇺🇸 US + Europa Overlap",
        "limit": 100,
    },
    "CRYPTO": {
        "where": "asset_class = 'crypto'",
        "label": "🌙 Crypto Night Scan",
        "limit": 30,
    },
}


def run_discovery(conn, region):
    """Discovery für eine Region: OHLCV cachen + Signale generieren."""
    config = REGION_QUERIES.get(region)
    if not config:
        return None

    where = config["where"]
    limit = config["limit"]
    label = config["label"]

    print(f"\n{'='*60}")
    print(f"{label} Discovery")
    print(f"{'='*60}")

    # Hole Top-Instrumente für Region — priorisiere kuratierte Mappings
    c = conn.cursor()
    c.execute(f"""
        SELECT instrument_id, symbol, name, yfinance_symbol
        FROM instruments
        WHERE {where}
          AND is_active = 1
          AND yfinance_symbol IS NOT NULL AND yfinance_symbol != ''
          AND COALESCE(yahoo_status, 'unknown') != 'delisted'
        ORDER BY
          CASE WHEN yfinance_symbol LIKE '%.DE' OR yfinance_symbol LIKE '%.AS' OR yfinance_symbol LIKE '%.PA' OR yfinance_symbol LIKE '%.SW' OR yfinance_symbol LIKE '%.L' THEN 0 ELSE 1 END,
          LENGTH(name) DESC, name ASC
        LIMIT ?
    """, (limit,))

    instruments = []
    for row in c.fetchall():
        inst = dict(row)
        if not inst['yfinance_symbol']:
            inst['yfinance_symbol'] = inst['symbol']
        instruments.append(inst)

    if not instruments:
        print(f"  Keine Instrumente für {region}")
        return {"region": region, "label": label, "cached": 0, "signals": []}

    # OHLCV cachen
    start = time.time()
    results = bulk_ensure_ohlcv(conn, instruments, required_days=30, batch_size=10)
    elapsed = time.time() - start
    conn.commit()  # Ensure OHLCV writes are committed before signal storage

    success = sum(1 for v in results.values() if v['has_data'])
    days = sum(v['days'] for v in results.values())

    print(f"  ✓ {success}/{len(instruments)} mit OHLCV ({days} Tage, {elapsed:.1f}s)")

    # Signale generieren (V5: signals.py via yfinance)
    symbols = [inst['yfinance_symbol'] or inst['symbol'] for inst in instruments if results.get(inst['instrument_id'], {}).get('has_data')]
    symbol_to_iid = {inst['yfinance_symbol'] or inst['symbol']: inst['instrument_id'] for inst in instruments}

    if symbols:
        start_sig = time.time()
        signal_results = analyze_batch(symbols)
        elapsed_sig = time.time() - start_sig

        # In V5-signals-Tabelle schreiben via repo.py
        db = DB(DB_PATH)
        signal_repo = SignalRepo(db)

        stored = 0
        signals_list = []
        for sym, result in signal_results.items():
            iid = symbol_to_iid.get(sym)
            if iid is None:
                continue

            signal_types_str = ",".join(result.signal_types) if result.signal_types else result.direction
            try:
                signal_repo.create(
                    instrument_id=iid,
                    signal_type=signal_types_str,
                    conviction=result.conviction,
                    score=result.score,
                    rsi=result.rsi,
                    macd_hist=result.macd_hist,
                    bb_pct=result.bb_pct,
                    price=result.price,
                    ttl_minutes=120,  # 2h TTL für Discovery-Signale
                )
                stored += 1
                # Zum Embed formatieren (kompatibel zum alten Format)
                signals_list.append({
                    'symbol': sym,
                    'signal_type': result.direction,
                    'conviction': result.conviction,
                    'score': result.score,
                    'price': result.price or 0,
                    'rsi': result.rsi or 0,
                    'macd_hist': result.macd_hist or 0,
                })
            except Exception as e:
                print(f"  ⚠ Signal speichern fehlgeschlagen {sym}: {e}")

        # Nothing to close — DB uses context manager pattern
        pass  # db will be garbage collected; connection closes on GC
        print(f"  📊 {stored} Signale gespeichert ({elapsed_sig:.1f}s)")
    else:
        signals_list = []

    buys = [s for s in signals_list if s['signal_type'] == 'BUY']
    sells = [s for s in signals_list if s['signal_type'] == 'SELL']

    print(f"  📊 {len(signals_list)} Signale: 🟢{len(buys)} BUY 🔴{len(sells)} SELL")

    return {
        "region": region,
        "label": label,
        "instruments_scanned": len(instruments),
        "cached": success,
        "days_cached": days,
        "elapsed": round(elapsed, 1),
        "total_signals": len(signals_list),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "top_buys": buys[:5],
        "top_sells": sells[:3],
    }


def post_embed(result):
    """Postet Discovery-Ergebnis als Discord Embed (V5)."""
    from bot.discord_embeds import post_alert_embed

    if not result or result['total_signals'] == 0:
        return

    buy_text = ""
    for s in result.get("top_buys", []):
        conv = s.get('conviction', '')
        emoji_map = {'VERY_HIGH': '💪', 'HIGH': '🟢', 'MEDIUM': '🔵', 'LOW': '⚪'}
        emoji = emoji_map.get(conv, '🟢')
        buy_text += f"{emoji} {s['symbol']:>8s} ${s['price']:>9.2f} | Score:{s['score']:.0f} ({conv}) RSI:{s['rsi']:5.1f}\n"

    sell_text = ""
    for s in result.get("top_sells", []):
        conv = s.get('conviction', '')
        emoji_map = {'VERY_HIGH': '🔴', 'HIGH': '⚠️', 'MEDIUM': '🟡', 'LOW': '⚪'}
        emoji = emoji_map.get(conv, '🔴')
        sell_text += f"{emoji} {s['symbol']:>8s} ${s['price']:>9.2f} | Score:{s['score']:.0f} ({conv})\n"

    post_alert_embed(
        title=f"{result['label']} Discovery",
        description=(
            f"**{result['instruments_scanned']}** Instrumente gescannt → "
            f"**{result['cached']}** mit OHLCV → **{result['total_signals']}** Signale"
        ),
        severity="INFO",
        fields=[
            {"name": "🟢 Buy Signale", "value": (
                "\`\`\`" + (buy_text or "Keine Buy-Signale") + "\`\`\`"
            ), "inline": False},
            {"name": "🔴 Sell Signale", "value": (
                "\`\`\`" + (sell_text or "Keine Sell-Signale") + "\`\`\`"
            ), "inline": False},
            {"name": "⏱️ Performance", "value": (
                f"• Scan: {result['elapsed']}s\n"
                f"• OHLCV: {result['days_cached']} Tage cached\n"
                f"• Region: {result['region']}"
            ), "inline": True},
        ],
    )


def main():
    region = get_current_region()
    if not region:
        print("Außerhalb der Discovery-Zeiten — skip")
        return

    conn = get_ohlcv_db()
    result = run_discovery(conn, region)
    conn.close()

    if result and result['total_signals'] > 0:
        post_embed(result)
        print("✓ Embed posted!")


if __name__ == "__main__":
    main()
