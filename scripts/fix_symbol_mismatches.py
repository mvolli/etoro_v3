#!/usr/bin/env python3
"""
Fix symbol mismatches in the `instruments` table of data/trading.db.

Data sources:
  - data/audit_instrument_symbols_report.csv
        instrument_id, local_symbol, live_symbol
        (every row is a mismatch: local symbol != eToro live symbol)
  - data/audit_instrument_symbols_conflicts.csv
        instrument_id, target_symbol, conflict_id
        (target_symbol maps to two different instrument_ids)
  - data/conflict_resolution_report.csv
        recommendation for each conflict pair

Resolution rules (per task):
  * "id=X ist vermutlich tot"  -> update the SURVIVING id to its live_symbol,
                                  deactivate the dead id (id=X).
  * "BEIDE eligible"           -> keep the LOWER instrument_id as primary
                                  (update to its live_symbol), deactivate the higher.
  * "KEINE eligible"           -> deactivate BOTH ids.
  * "UNKLAR"                   -> skip (no changes).
  * mismatch WITHOUT a conflict -> UPDATE symbol = live_symbol.

A summary is printed first; changes are then applied inside a single
transaction (unless --dry-run is given). A timestamped backup copy of the
database is created before anything is written.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = "data/trading.db"
AUDIT_CSV = "data/audit_instrument_symbols_report.csv"
CONFLICTS_CSV = "data/audit_instrument_symbols_conflicts.csv"
RESOLUTION_CSV = "data/conflict_resolution_report.csv"

DEAD_RE = re.compile(r"id=(\d+)\s+ist vermutlich tot")


def load_audit(path: str) -> dict[int, str]:
    """instrument_id -> live_symbol (only mismatches are present)."""
    live = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            live[int(row["instrument_id"])] = row["live_symbol"].strip()
    return live


def load_resolutions(path: str) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def classify(recommendation: str) -> str:
    r = recommendation
    if "ist vermutlich tot" in r:
        return "TOT"
    if "BEIDE eligible" in r:
        return "BEIDE"
    if "KEINE eligible" in r:
        return "KEINE"
    if "UNKLAR" in r:
        return "UNKLAR"
    return "OTHER"


def build_plan(live: dict[int, str], resolutions: list[dict]):
    """Return (updates, deactivate, skips, warnings, conflict_ids).

    updates:    dict id -> final_symbol
    deactivate: set of ids to set is_active = 0
    skips:      list of (id, reason) that were intentionally left untouched
    """
    updates: dict[int, str] = {}
    deactivate: set[int] = set()
    skips: list[tuple[int, str]] = []
    warnings: list[str] = []
    conflict_ids: set[int] = set()

    def target_for(iid: int, fallback: str | None) -> str | None:
        """Preferred symbol for a surviving id = its own live_symbol."""
        return live.get(iid, fallback)

    for row in resolutions:
        iid = int(row["instrument_id"])
        cid = int(row["conflict_id"])
        target = row["target_symbol"].strip()
        kind = classify(row["recommendation"])
        conflict_ids.update((iid, cid))

        if kind == "UNKLAR" or kind == "OTHER":
            skips.append((iid, f"{kind}: {row['recommendation'][:50]}"))
            continue

        if kind == "KEINE":
            deactivate.update((iid, cid))
            continue

        if kind == "BEIDE":
            primary, dead = (iid, cid) if iid < cid else (cid, iid)
        elif kind == "TOT":
            m = DEAD_RE.search(row["recommendation"])
            if not m:
                warnings.append(f"Could not parse dead id from: {row['recommendation']}")
                skips.append((iid, "TOT unparseable"))
                continue
            dead = int(m.group(1))
            if dead not in (iid, cid):
                warnings.append(
                    f"Dead id {dead} not in pair ({iid},{cid}); skipping")
                skips.append((iid, "TOT id mismatch"))
                continue
            primary = cid if dead == iid else iid
        else:  # pragma: no cover
            continue

        deactivate.add(dead)
        sym = target_for(primary, target)
        if sym is None:
            warnings.append(f"No live_symbol known for surviving id {primary}")
        else:
            updates[primary] = sym

    # --- Resolve any id that is both a survivor and a death: death wins ---
    for iid in list(updates):
        if iid in deactivate:
            warnings.append(
                f"id {iid} marked as both survivor and dead -> keeping deactivation")
            del updates[iid]

    # --- Plain (non-conflict) mismatches ---
    for iid, sym in live.items():
        if iid in conflict_ids:
            continue
        updates[iid] = sym

    return updates, deactivate, skips, warnings, conflict_ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + print plan but do not write to the DB")
    args = ap.parse_args()

    for p in (DB_PATH, AUDIT_CSV, CONFLICTS_CSV, RESOLUTION_CSV):
        if not os.path.exists(p):
            print(f"ERROR: required file not found: {p}", file=sys.stderr)
            return 1

    live = load_audit(AUDIT_CSV)
    resolutions = load_resolutions(RESOLUTION_CSV)

    updates, deactivate, skips, warnings, conflict_ids = build_plan(live, resolutions)

    conflict_survivor_updates = {i for i in updates if i in conflict_ids}
    plain_updates = {i for i in updates if i not in conflict_ids}

    print("=" * 70)
    print("SYMBOL MISMATCH FIX — PLAN SUMMARY")
    print("=" * 70)
    print(f"  Audit mismatches loaded ........ {len(live):>6}")
    print(f"  Conflict resolution rows ....... {len(resolutions):>6}")
    print(f"  Distinct ids in conflicts ...... {len(conflict_ids):>6}")
    print("-" * 70)
    print(f"  UPDATES (total) ................ {len(updates):>6}")
    print(f"      - plain mismatch updates ... {len(plain_updates):>6}")
    print(f"      - conflict survivor updates. {len(conflict_survivor_updates):>6}")
    print(f"  DEACTIVATIONS .................. {len(deactivate):>6}")
    print(f"  SKIPS (UNKLAR / unparseable) ... {len(skips):>6}")
    print("=" * 70)

    # Show the specific critical case (1029 / 2223)
    print("Critical case check:")
    print(f"    1029 -> {updates.get(1029, '(no update)')} "
          f"| deactivate 1029? {1029 in deactivate}")
    print(f"    2223 -> {updates.get(2223, '(no update)')} "
          f"| deactivate 2223? {2223 in deactivate}")
    print("=" * 70)

    if warnings:
        print(f"WARNINGS ({len(warnings)}):")
        for w in warnings[:30]:
            print("   !", w)
        if len(warnings) > 30:
            print(f"   ... and {len(warnings) - 30} more")
        print("=" * 70)

    if args.dry_run:
        print("DRY-RUN: no changes written.")
        return 0

    # ---- Backup ----
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = f"{DB_PATH}.bak_{ts}"
    shutil.copy2(DB_PATH, backup)
    print(f"Backup created: {backup}")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()

    applied_updates = 0
    applied_deactivations = 0
    renamed_deaths = 0
    failed: list[tuple[int, str, str]] = []

    try:
        cur.execute("BEGIN")

        # Current symbols for every id we touch
        touched = set(updates) | deactivate
        current: dict[int, tuple[str, int]] = {}
        for iid in touched:
            r = cur.execute(
                "SELECT symbol, is_active FROM instruments WHERE instrument_id=?",
                (iid,)).fetchone()
            if r is not None:
                current[iid] = (r[0], r[1])

        target_symbols = set(updates.values())

        # 1) Deactivate dead ids; free any symbol a survivor needs by renaming.
        for iid in sorted(deactivate):
            if iid not in current:
                continue  # id not present in DB
            sym, _ = current[iid]
            if sym in target_symbols and updates.get(iid) != sym:
                new_sym = f"{sym}__DEAD_{iid}"
                cur.execute(
                    "UPDATE instruments SET symbol=?, is_active=0, "
                    "last_updated=datetime('now','utc') WHERE instrument_id=?",
                    (new_sym, iid))
                renamed_deaths += 1
            else:
                cur.execute(
                    "UPDATE instruments SET is_active=0, "
                    "last_updated=datetime('now','utc') WHERE instrument_id=?",
                    (iid,))
            applied_deactivations += cur.rowcount

        # 2) Two-phase symbol update to survive swaps/cycles under UNIQUE.
        #    Phase A: park every updated id on a unique temp symbol.
        for iid in updates:
            if iid not in current:
                continue
            cur.execute(
                "UPDATE instruments SET symbol=? WHERE instrument_id=?",
                (f"__TMP_{iid}", iid))

        #    Phase B: assign final symbols.
        for iid, sym in updates.items():
            if iid not in current:
                failed.append((iid, sym, "instrument_id not in DB"))
                continue
            try:
                cur.execute(
                    "UPDATE instruments SET symbol=?, "
                    "last_updated=datetime('now','utc') WHERE instrument_id=?",
                    (sym, iid))
                applied_updates += 1
            except sqlite3.IntegrityError as e:
                failed.append((iid, sym, str(e)))

        if failed:
            # Restore original symbols for the ids we could not finalise so we
            # never leave a __TMP_ symbol behind.
            for iid, _sym, _err in failed:
                if iid in current:
                    cur.execute(
                        "UPDATE instruments SET symbol=? WHERE instrument_id=?",
                        (current[iid][0], iid))

        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        print("ERROR: transaction rolled back, DB unchanged.", file=sys.stderr)
        raise

    conn.close()

    print("=" * 70)
    print("RESULTS (applied)")
    print("=" * 70)
    print(f"  Symbol updates applied ......... {applied_updates:>6}")
    print(f"  Deactivations applied .......... {applied_deactivations:>6}")
    print(f"  Dead ids renamed (freed symbol). {renamed_deaths:>6}")
    print(f"  Failed / skipped updates ....... {len(failed):>6}")
    if failed:
        print("-" * 70)
        print("  Failures (kept original symbol):")
        for iid, sym, err in failed[:40]:
            print(f"    id={iid} -> '{sym}'  [{err}]")
        if len(failed) > 40:
            print(f"    ... and {len(failed) - 40} more")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
