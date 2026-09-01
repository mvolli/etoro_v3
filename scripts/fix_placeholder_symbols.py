#!/usr/bin/env python3
"""
fix_placeholder_symbols.py (2026-09-01)

Heilt die '<Kuerzel>_<id>' Platzhalter-Symbole in der instruments-Tabelle.
Root cause: ein alter Resolve-Versuch generierte Platzhalter-Ticker; die
live-Identity-Gate (verify_instrument_identity) blockt SEITDEM jeden
Trade auf diesen Zeilen (Pre-flight 'ID/Symbol MISMATCH').

Strategie:
  1. Alle aktiven Placeholder-Zeilen (symbol GLOB '[A-Z]*_[0-9]*') werden
     per Batch-Metadata gegen die LIVE-eToro-API geloescht
     (symbolFull = authoritative eToro-Ticker).
  2. NEW_SYMBOL = symbolFull. UNIQUE(symbol) Kollisionen -> Suffix
     '_2', '_3', ... (deterministisch by instrument_id).
  3. yfinance_symbol: nur bei NEED-LIVE-Zeilen (leer) -> symbolFull.
     Vorhandene yfinance_symbol wird NICHT angefasst (data_worker-
     namespace, fix_eu_yfinance_symbols.py owns it).
  4. Fallback: live-Lookup faellt durch (500/empty) -> is_tradable=0
     (aus Trade-Kandidaten), nicht is_active (historie bleiben).

Modes: --dry-run (default) | --apply | --apply --yes-to-all
"""
from __future__ import annotations
import argparse, os, re, shutil, sqlite3, sys, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml
from bot.api.client import EToroClient, ClientConfig

DB_PATH = PROJECT_ROOT / "data" / "trading.db"
PAT = re.compile(r"^[A-Z]{1,4}_\d+$")
BATCH = 50
DELAY = 1.0

# instrument_id -> canonical eToro ticker, when the live symbolFull is
# ambiguous (two DIFFERENT companies both report base symbol 'SO'):
NAME_OVERRIDES = {
    6149: "SCCO",    # Southern COPPER (base 'SO' belongs to Southern CO, id 1578)
}

def load_client() -> EToroClient:
    env = {}
    for line in open("/home/mvolli/.hermes/.env"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return EToroClient(env["ETORO_BOT_API_KEY"], env["ETORO_BOT_USER_KEY"], ClientConfig())

def fetch_symbols(client: EToroClient, ids: list[int]) -> dict:
    """id -> (symbolFull, name, active) via batch metadata endpoint."""
    out: dict = {}
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        try:
            got = client.get_instruments_metadata_batch(chunk)  # dict[int, dict]
            for iid, item in got.items():
                out[int(iid)] = (item.get("symbolFull"),
                                 item.get("instrumentDisplayName"),
                                 item.get("active", item.get("isAvailable")))
        except Exception as exc:
            print(f"  batch {chunk[0]}..{chunk[-1]} FAILED: {type(exc).__name__} {str(exc)[:100]}")
            for iid in chunk:
                out.setdefault(iid, None)
        time.sleep(DELAY)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--yes-to-all", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    ph = [r for r in db.execute("SELECT * FROM instruments WHERE is_active=1 AND symbol GLOB '[A-Z]*_[0-9]*'") if PAT.match(r["symbol"])]
    print(f"Active placeholder rows: {len(ph)}")

    # existing symbols across the WHOLE table (UNIQUE(symbol) covers
    # inactive rows too — the dry-run that only checked is_active=1
    # collided on a stale inactive symbol and rolled back).
    existing = {r[0] for r in db.execute("SELECT symbol FROM instruments")}

    ids = [r["instrument_id"] for r in ph]
    client = load_client()
    live = fetch_symbols(client, ids)

    planned = []
    for r in ph:
        iid = r["instrument_id"]
        got = live.get(iid)
        base = (got or (None, None, None))[0]
        if not base:
            planned.append((r, None, "NO-LIVE-DATA", r["symbol"], 0, r["is_tradable"]))
            continue
        new_sym, n = base, 2
        while new_sym in existing or any(p[2] == new_sym for p in planned):
            new_sym, n = f"{base}_{n}", n + 1
        existing.add(new_sym)
        yf_new = r["yfinance_symbol"] if (r["yfinance_symbol"] or "").strip() else base
        planned.append((r, base, new_sym, r["symbol"], 1, r["is_tradable"]))

    n_ok = sum(1 for p in planned if p[2] != "NO-LIVE-DATA")
    n_deact = sum(1 for p in planned if p[2] == "NO-LIVE-DATA")
    n_coll = sum(1 for p in planned if p[2] not in ("NO-LIVE-DATA",) and p[2] != p[1])
    n_yf = sum(1 for p in planned if p[2] not in ("NO-LIVE-DATA",) and p[3] != p[2] and (p[0]["yfinance_symbol"] or "") == "")
    print(f"live-resolved: {n_ok} | delisted->is_tradable=0: {n_deact} | collision-suffixed: {n_coll} | yfinance_symbol filled: {n_yf}")
    print()
    for r, base, new_sym, old_sym, ok, trad in planned:
        if ok:
            flag = " [COLLISION]" if new_sym != base else ""
            yf = f" yf:{r['yfinance_symbol'] or '(->%s)' % base}" if (r['yfinance_symbol'] or '').strip() else f" yf:->'{base}'"
            print(f"  {r['instrument_id']:<10} {old_sym:<16} -> {new_sym}{flag}{yf}  ({r['name']})")
        else:
            print(f"  {r['instrument_id']:<10} {old_sym:<16} NO LIVE DATA -> is_tradable=0  ({r['name']})")

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to commit.")
        return

    if not args.yes_to_all:
        ans = input(f"\nApply {len(planned)} changes to trading.db? [yes/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return

    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = DB_PATH.with_name(f"trading.db.bak_placeholder_{ts}")
    shutil.copy2(DB_PATH, bak)
    print(f"backup: {bak}")

    db.execute("BEGIN")
    n_upd = 0
    try:
        for r, base, new_sym, old_sym, ok, trad in planned:
            if ok:
                db.execute("UPDATE instruments SET symbol=? WHERE instrument_id=?", (new_sym, r["instrument_id"]))
            else:
                db.execute("UPDATE instruments SET is_tradable=0 WHERE instrument_id=?", (r["instrument_id"],))
            n_upd += 1
            # yfinance_symbol backfill for NEED-LIVE rows
            if ok and not (r["yfinance_symbol"] or "").strip():
                db.execute("UPDATE instruments SET yfinance_symbol=? WHERE instrument_id=?", (base, r["instrument_id"]))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    left = db.execute("SELECT COUNT(*) FROM instruments WHERE is_active=1 AND symbol GLOB '[A-Z]*_[0-9]*'").fetchone()[0]
    print(f"\nAPPLIED: {n_upd} rows updated. Remaining active placeholders: {left}")

if __name__ == "__main__":
    main()
