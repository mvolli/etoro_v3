#!/usr/bin/env python3
"""Fetch 1y daily bars for all traded instruments (26.07-Zaesur window).

Source: yfinance (Yahoo namespace = instruments.yfinance_symbol, curated per
instrument — NEVER eToro symbol for HK/AU/... names). Output:
data/exit_replay_bars/<sym>.csv with columns date,open,high,low,close,volume,adj_close
(auto_adjust=True so splits/dividends are baked into the OHLC).
Failures go to data/exit_replay_bars/_failures.json.
"""
import json, os, sqlite3, sys, time

import pandas as pd

DB = "/home/mvolli/.hermes/workspace/etoro_v3/data/trading.db"
OUT = "/home/mvolli/.hermes/workspace/etoro_v3/data/exit_replay_bars"
os.makedirs(OUT, exist_ok=True)

import yfinance as yf

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute(
    """SELECT DISTINCT i.instrument_id, i.symbol, i.yfinance_symbol, i.asset_class
       FROM trades t JOIN instruments i ON i.instrument_id = t.instrument_id
       WHERE t.created_at >= '2026-07-26' AND t.status = 'CLOSED'""")]
print(f"distinct traded instruments: {len(rows)}", flush=True)

# sanity: flag suspicious yfinance symbols (AGENTS: -USD/=X/=F/^ on stocks)
sus = [r for r in rows if r["asset_class"] in ("stock", "etf")
       and r["yfinance_symbol"] and (
           r["yfinance_symbol"].endswith("-USD") or "=" in r["yfinance_symbol"]
           or r["yfinance_symbol"].startswith("^"))]
print(f"suspicious yfinance symbols on stock/etf: {len(sus)}", flush=True)
for r in sus[:10]:
    print("  SUSPECT", r, flush=True)

fails = {}
# batch in chunks of 40 to be gentle on Yahoo
syms = {}
for r in rows:
    y = r["yfinance_symbol"]
    if y and y not in fails:
        syms.setdefault(y, r)
print(f"unique yfinance symbols: {len(syms)}", flush=True)

t0 = time.time()
for i in range(0, len(syms), 40):
    chunk = list(syms.items())[i:i + 40]
    tickers = [y for y, _ in chunk]
    try:
        data = yf.download(tickers, period="1y", interval="1d", auto_adjust=True,
                           group_by="ticker", threads=True, progress=False)
    except Exception as e:
        print(f"chunk {i} failed: {e}", flush=True)
        for y, _ in chunk:
            fails[y] = str(e)
        continue
    for y, r in chunk:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                cols0 = data.columns.get_level_values(0)
                df = data[y] if y in cols0 else None
            else:
                df = data
            if df is None or len(df) < 60:
                raise ValueError(f"too few rows: {0 if df is None else len(df)}")
            df = df.dropna(subset=["Close"])
            out = df.reset_index()
            cols = {"Date": "date", "Open": "open", "High": "high", "Low": "low",
                    "Close": "close", "Volume": "volume"}
            if "Adj Close" in out.columns:
                cols["Adj Close"] = "adj_close"
            out = out.rename(columns=cols)
            keep = [c for c in ["date", "open", "high", "low", "close", "volume", "adj_close"]
                    if c in out.columns]
            out[keep].to_csv(f"{OUT}/{y.replace('/', '_')}.csv", index=False)
        except Exception as e:
            fails[y] = str(e)
    print(f"chunk {i}-{i+len(chunk)}: {len(syms) - min(i+len(chunk), len(syms))} left "
          f"({time.time()-t0:.0f}s)", flush=True)

with open(f"{OUT}/_failures.json", "w") as f:
    json.dump(fails, f, indent=2)
n_csv = len([f for f in os.listdir(OUT) if f.endswith(".csv")])
print(f"DONE: {n_csv} csv files, {len(fails)} failures in {time.time()-t0:.0f}s", flush=True)
for y, e in list(fails.items())[:20]:
    print("  FAIL", y, "->", e[:100], flush=True)
