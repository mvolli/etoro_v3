"""fix/crypto-symbol-contamination (2026-08-21): DB-Repair.

Ein Backfill-Skript (25.06.2026) hat per Name-Fuzzy-Match 180 Aktien/ETFs auf
echte Krypto-Ticker gemappt (STMicroelectronics -> TRX-USD, First Bancorp ->
BNT-USD, ...). Der Preis-Pfad hat seither Krypto-Kurse in die ohlcv_daily
dieser stock/etf-Instruments geschrieben.

Reparatur (idempotent, --apply to write):
  A) NULL yfinance_symbol + reset yahoo_fail_count for the contaminated set.
     -> normal resolution retries via the raw eToro symbol on next fetch.
  B) DELETE the ohlcv_daily rows stored under those wrong tickers.

Contaminated set = asset_class IN ('stock','etf') AND yfinance_symbol is a
real crypto asset ticker. Legit Krypto-ETPs (CoinShares/Grayscale/21Shares/
iShares Bitcoin/Ethereum/Solana ... BTC-USD/ETH-USD) are EXCLUDED.
Commodity-futures and forex rows that also carry '-USD' are a SEPARATE,
pre-existing naming quirk and are NOT touched here.
"""
import sqlite3, sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "trading.db"
APPLY = "--apply" in sys.argv

# Real crypto-asset base tickers seen in the contaminated rows.
CRYPTO = {"BTC", "ETH", "SOL", "BNT", "TRX", "GALA", "XLM", "AVAX",
          "CELO", "ATOM", "DOGE", "DOT", "LTC", "LINK", "SHIB", "MIOTA"}

# Legit Krypto-ETP name keywords -> BTC-USD/ETH-USD/SOL-USD is their reference.
ETP_KW = ("coinshares", "grayscale", "21shares", "ishares", "abrdn",
          "hashdex", "van eck", "fidelity", "winklev", "teucrium")
CRYPTO_WORDS = ("bitcoin", "ethereum", "solana", "crypto", "blockchain", "etp")


def is_etp(name):
    n = (name or "").lower()
    return any(k in n for k in ETP_KW) and any(w in n for w in CRYPTO_WORDS)


def is_crypto_ticker(yf):
    return (yf or "").upper().replace("-USD", "").strip() in CRYPTO


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT instrument_id, symbol, name, asset_class, yfinance_symbol AS yf "
        "FROM instruments WHERE yfinance_symbol IS NOT NULL AND yfinance_symbol != '' "
        "AND upper(yfinance_symbol) LIKE '%-USD'")]

    polluted, legit = [], []
    for r in rows:
        if (r["asset_class"] or "").lower() in ("stock", "etf") and is_crypto_ticker(r["yf"]):
            if is_etp(r["name"]):
                legit.append(r)
            else:
                polluted.append(r)

    print("stock/etf w/ real crypto ticker: %d  (polluted=%d, legit-ETP=%d)"
          % (len(polluted) + len(legit), len(polluted), len(legit)))
    for r in legit:
        print("   KEEP %s %s | %s -> %s" % (r["instrument_id"], r["symbol"],
                                            (r["name"] or "")[:44], r["yf"]))
    if not polluted:
        print("\nNothing to repair (already clean).")
        return

    ids = [r["instrument_id"] for r in polluted]
    ph = ",".join("?" * len(ids))
    cnt, mn, mx = conn.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM ohlcv_daily WHERE instrument_id IN (%s)" % ph,
        ids).fetchone()
    pos = conn.execute(
        "SELECT COUNT(*) FROM portfolio_snapshot WHERE instrument_id IN (%s)" % ph,
        ids).fetchone()[0]
    print("ohlcv_daily rows to delete: %s  (date range %s .. %s)" % (cnt, mn, mx))
    print("open positions on set (must be 0): %s" % pos)

    if not APPLY:
        print("\n[dry-run] re-run with --apply")
        return

    cur = conn.cursor()
    for r in polluted:
        cur.execute(
            "UPDATE instruments SET yfinance_symbol = NULL, yahoo_fail_count = 0, "
            "last_updated = CURRENT_TIMESTAMP WHERE instrument_id = ?",
            (r["instrument_id"],))
        assert cur.rowcount == 1, "update failed for %s" % r["instrument_id"]
    cur.execute("DELETE FROM ohlcv_daily WHERE instrument_id IN (%s)" % ph, ids)
    deleted = cur.rowcount
    conn.commit()
    print("\n[applied] NULLed %d yfinance_symbol, deleted %d ohlcv_daily rows"
          % (len(polluted), deleted))
    left = conn.execute(
        "SELECT COUNT(*) FROM instruments WHERE yfinance_symbol IS NOT NULL "
        "AND yfinance_symbol != '' AND upper(yfinance_symbol) LIKE '%%-USD' "
        "AND lower(asset_class) IN ('stock','etf')").fetchone()[0]
    print("verify: stock/etf rows with -USD left (incl. legit ETPs): %d" % left)


if __name__ == "__main__":
    main()
