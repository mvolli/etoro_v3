#!/usr/bin/env python3
"""E10 — Daten fuer den Earnings-Katalysator in Europa (nur lesend gegen trading.db).

Schritte (keine Renditen, nur Datenaufbau):
  universe  Indexlisten von Wikipedia (DAX, MDAX, CAC 40, FTSE 100/250, AEX, AMX,
            IBEX 35, FTSE MIB, SMI, OMX S30/C25/H25, OBX, BEL 20, ATX) -> Yahoo-Symbole,
            Zuordnung zu handelbaren eToro-Instrumenten, Identitaet je Titel am Kurs
            geprueft (eToro-Rate vs. Yahoo, +-15 %, Pence-Faktor 100 erlaubt).
  bars      OHLCV ab 2006 (yfinance, auto_adjust) nach data/e10_cache/bars/
  earn      get_earnings_dates(limit=100) nach data/e10_cache/earn/

  PYTHONPATH=src /usr/bin/python3 scripts/e10_fetch_eu.py universe|bars|earn
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "trading.db"
OUT = ROOT / "data" / "e10_cache"

# (Wikipedia-Seite, Yahoo-Suffix fuer Ticker ohne Suffix, Land fuer Kaufsteuer)
INDICES = [
    ("DAX", ".DE", "DE"), ("MDAX", ".DE", "DE"), ("CAC_40", ".PA", "FR"),
    ("FTSE_100_Index", ".L", "UK"), ("FTSE_250_Index", ".L", "UK"),
    ("AEX_index", ".AS", "NL"), ("AMX_index", ".AS", "NL"), ("IBEX_35", ".MC", "ES"),
    ("FTSE_MIB", ".MI", "IT"), ("Swiss_Market_Index", ".SW", "CH"),
    ("OMX_Stockholm_30", ".ST", "SE"), ("OMX_Copenhagen_25", ".CO", "DK"),
    ("OMX_Helsinki_25", ".HE", "FI"), ("OBX_Index", ".OL", "NO"), ("BEL_20", ".BR", "BE"),
    ("Austrian_Traded_Index", ".VI", "AT"),
]
SUFFIX_COUNTRY = {".DE": "DE", ".PA": "FR", ".L": "UK", ".AS": "NL", ".MC": "ES", ".MI": "IT",
                  ".SW": "CH", ".ST": "SE", ".CO": "DK", ".HE": "FI", ".OL": "NO", ".BR": "BE",
                  ".VI": "AT", ".IR": "IE", ".LS": "PT"}
TICKER_COLS = ("Ticker", "Ticker symbol", "Symbol", "EPIC", "Ticker Symbol", "Code")


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 etoro-bot research"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")


def _norm(t: str, suffix: str) -> str | None:
    t = str(t).strip().upper().replace(" ", " ")
    t = re.sub(r"\[.*?\]", "", t).strip()
    if not t or t in ("NAN", "—", "-"):
        return None
    t = t.split(":")[-1].strip()                     # "LSE: AZN" -> AZN
    t = t.replace(" ", "-")                          # "ERIC B" -> ERIC-B
    if t.endswith("."):
        t = t[:-1]
    if "." in t:
        base, suf = t.rsplit(".", 1)
        return f"{base}.{suf}" if f".{suf}" in SUFFIX_COUNTRY else None
    return t + suffix


def universe() -> int:
    import pandas as pd
    yahoo: dict[str, str] = {}
    for page, suffix, _ in INDICES:
        try:
            tables = pd.read_html(io.StringIO(_get(f"https://en.wikipedia.org/wiki/{page}")))
        except Exception as exc:  # noqa: BLE001
            print(f"  {page}: Fehler {exc}"); continue
        n = 0
        for tb in tables:
            col = next((c for c in tb.columns if str(c).strip() in TICKER_COLS), None)
            if col is None or len(tb) < 10:
                continue
            for v in tb[col]:
                y = _norm(v, suffix)
                if y and y not in yahoo:
                    yahoo[y] = page; n += 1
        print(f"  {page}: {n} Titel", flush=True)
        time.sleep(0.5)
    print(f"Index-Titel gesamt: {len(yahoo)}")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    inst = [dict(r) for r in con.execute(
        "SELECT instrument_id, symbol, yfinance_symbol, name FROM instruments "
        "WHERE asset_class='stock' AND is_tradable=1 AND symbol LIKE '%.%'")]
    # Zuordnung: zuerst exaktes eToro-Symbol (Yahoo-Suffix bzw. eToro-Variante:
    # .AS -> .NV, .SW -> .ZU), nur dann das gespeicherte yfinance_symbol mit
    # gleichem Basis-Kuerzel. Sonst trifft AZN.L die ADR/den Stockholm-Zweitlisting.
    by_sym = {str(r["symbol"]).upper(): r for r in inst}
    by_yf: dict[str, list[dict]] = {}
    for r in inst:
        if r["yfinance_symbol"]:
            by_yf.setdefault(str(r["yfinance_symbol"]).upper(), []).append(r)
    variants = {".AS": [".NV"], ".SW": [".ZU"]}
    cand = {}
    for y in yahoo:
        base, suf = y.rsplit(".", 1)
        suf = "." + suf
        hit = None
        for s_ in [suf] + variants.get(suf, []):
            for b in (base, base.replace("-", "")):
                hit = hit or by_sym.get(f"{b}{s_}")
        if hit is None:
            same = [r for r in by_yf.get(y.upper(), [])
                    if str(r["symbol"]).upper().split(".")[0].replace("-", "") == base.replace("-", "")
                    and "." in str(r["symbol"])]
            hit = sorted(same, key=lambda r: r["instrument_id"])[0] if same else None
        if hit:
            cand[y] = hit
    print(f"bei eToro handelbar (Kandidaten): {len(cand)}")

    # Identitaet am Kurs pruefen
    import yaml
    import yfinance as yf
    sys.path.insert(0, str(ROOT / "src"))
    from bot.api.client import ClientConfig, EToroClient
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
    client = EToroClient(api_key=os.environ["ETORO_BOT_API_KEY"], user_key=os.environ["ETORO_BOT_USER_KEY"],
                         config=ClientConfig.from_dict(cfg.get("api", {})))
    rates = client.get_rates_batch([r["instrument_id"] for r in cand.values()])
    ys = sorted(cand)
    last = {}
    for k in range(0, len(ys), 40):
        ch = ys[k:k + 40]
        try:
            df = yf.download(ch, period="5d", auto_adjust=False, progress=False, group_by="ticker", threads=False)
            for s in ch:
                try:
                    c = (df[s] if len(ch) > 1 else df)["Close"].dropna()
                    if len(c):
                        last[s] = float(c.iloc[-1])
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            print("  Kursblock", k, exc)
        time.sleep(1.5)
    out, bad = {}, []
    for y, r in cand.items():
        rt = rates.get(int(r["instrument_id"])) or {}
        e = next((float(rt[k]) for k in ("lastExecution", "bid", "ask") if rt.get(k)), None)
        p = last.get(y)
        ok = False
        if e and p:
            for f in (1.0, 100.0, 0.01):
                if abs(math.log(p / (e * f))) <= math.log(1.15):
                    ok = True
        if ok:
            suf = "." + y.rsplit(".", 1)[1]
            out[y] = {"instrument_id": r["instrument_id"], "etoro": r["symbol"], "name": r["name"],
                      "country": SUFFIX_COUNTRY.get(suf), "index": yahoo[y]}
        else:
            bad.append((y, r["symbol"], e, p))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "universe.json").write_text(json.dumps(out, indent=0, sort_keys=True))
    print(f"Universum (Kurs-Identitaet bestaetigt): {len(out)}; verworfen {len(bad)}: {bad[:15]}")
    from collections import Counter
    print("je Land:", dict(Counter(v["country"] for v in out.values())))
    return 0


def bars() -> int:
    import yfinance as yf
    uni = json.loads((OUT / "universe.json").read_text())
    (OUT / "bars").mkdir(parents=True, exist_ok=True)
    todo = [t for t in list(uni) + ["^STOXX", "EXSA.DE"] if not (OUT / "bars" / f"{t}.csv").exists()]
    for k in range(0, len(todo), 40):
        ch = todo[k:k + 40]
        df = yf.download(ch, start="2006-01-01", end="2026-10-17", auto_adjust=True, progress=False,
                         group_by="ticker", threads=False)
        for t in ch:
            try:
                sub = (df[t] if len(ch) > 1 else df)[["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(sub) > 50:
                    sub.to_csv(OUT / "bars" / f"{t}.csv")
            except Exception:  # noqa: BLE001
                pass
        print(f"  Kurse {min(k + 40, len(todo))}/{len(todo)}", flush=True)
        time.sleep(1.5)
    return 0


def earn() -> int:
    import yfinance as yf
    uni = json.loads((OUT / "universe.json").read_text())
    (OUT / "earn").mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(sorted(uni)):
        p = OUT / "earn" / f"{t}.csv"
        if p.exists() and p.stat().st_size > 0:
            continue
        for attempt in range(3):
            try:
                ed = yf.Ticker(t).get_earnings_dates(limit=100)
                if ed is not None and len(ed):
                    ed.to_csv(p)
                else:
                    p.write_text("")
                break
            except Exception:  # noqa: BLE001
                time.sleep(5 * (attempt + 1))
        if i % 50 == 0:
            print(f"  Earnings {i}/{len(uni)}", flush=True)
        time.sleep(0.6)
    print("Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit({"universe": universe, "bars": bars, "earn": earn}[sys.argv[1]]())
