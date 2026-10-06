#!/usr/bin/env python3
"""One-off migration — fix/pc-shadow-dedup + fix/pc-shadow-null (2026-10-06).

Repairs the historical partial_close_shadow ledger that the LIVE code fixes
(forward-looking) now prevent:

  (a) Backfill amount_usd IS NULL rows from trades.amount_usd (matched by
      api_position_id), falling back to portfolio_snapshot.amount_usd; recompute
      pnl_usd_est from the backfilled value.

  (b) Collapse same-rung duplicates: within each (position_id, path,
      close_pct) group, forward-walk by (ts, id). A row whose pnl_pct is within
      TOL of the last KEPT stage is merged into that stage (the kept row is
      refreshed to the row's latest state, the duplicate is DELETEd). A row
      beyond TOL opens a new stage. This mirrors the live _dedup_refresh()
      sliding reference exactly — consecutive drift collapses, genuine PnL
      jumps are preserved.

Idempotent + safe:
  * run against a copy you control; the caller is expected to back up first.
  * everything runs in one transaction; any error rolls back.
  * prints before/after counts.

Usage:  PYTHONPATH=src python3 scripts/migrate_pc_shadow.py [db_path]
"""
import sys
import sqlite3
from pathlib import Path

TOL = 2.0  # percentage points — matches DEFAULT_CONFIG dedup_pnl_tolerance_pct


def _pnl_est(amount, pnl_pct, close_pct):
    if amount is not None and pnl_pct is not None and close_pct:
        return amount * (close_pct / 100.0) * (pnl_pct / 100.0)
    return None


def count(conn, where="", args=()):
    sql = "SELECT COUNT(*) FROM partial_close_shadow"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql, args).fetchone()[0]


def backfill_amounts(conn):
    """(a) Fill NULL amount_usd from trades, then snapshot. Returns (n, rows)."""
    nulls = conn.execute(
        "SELECT id, position_id, close_pct, pnl_pct FROM partial_close_shadow "
        "WHERE amount_usd IS NULL ORDER BY id"
    ).fetchall()
    filled = 0
    detail = []
    for rid, pid, close_pct, pnl_pct in nulls:
        amount = None
        if pid:
            t = conn.execute(
                "SELECT amount_usd FROM trades WHERE api_position_id=? "
                "ORDER BY created_at DESC LIMIT 1", (pid,)
            ).fetchone()
            if t and t[0] is not None:
                amount = float(t[0])
            if amount is None:
                s = conn.execute(
                    "SELECT amount_usd FROM portfolio_snapshot "
                    "WHERE api_position_id=? AND amount_usd IS NOT NULL "
                    "ORDER BY last_synced DESC LIMIT 1", (pid,)
                ).fetchone()
                if s and s[0] is not None:
                    amount = float(s[0])
        if amount is None:
            continue
        est = _pnl_est(amount, pnl_pct, close_pct)
        conn.execute(
            "UPDATE partial_close_shadow SET amount_usd=?, pnl_usd_est=? WHERE id=?",
            (amount, est, rid),
        )
        filled += 1
        detail.append((rid, pid, amount))
    return filled, detail


def collapse_duplicates(conn):
    """(b) Forward sliding-window collapse. Returns (groups, merged, kept)."""
    groups = conn.execute(
        "SELECT position_id, path, ROUND(close_pct,2) "
        "FROM partial_close_shadow GROUP BY position_id, path, ROUND(close_pct,2) "
        "HAVING COUNT(*) > 1"
    ).fetchall()
    merged = 0
    kept_total = 0
    for (pid, path, cp) in groups:
        rows = conn.execute(
            "SELECT id, ts, pnl_pct, amount_usd, pnl_usd_est, allowed, reason "
            "FROM partial_close_shadow "
            "WHERE position_id=? AND path=? AND ROUND(close_pct,2)=? "
            "ORDER BY ts ASC, id ASC",
            (pid, path, cp),
        ).fetchall()
        anchor = None  # id of the currently-kept stage row
        for r in rows:
            rid, ts, pnl_pct = r[0], r[1], r[2]
            amount, est, allowed, reason = r[3], r[4], r[5], r[6]
            if anchor is None:
                anchor = rid  # first row starts a stage
                continue
            if pnl_pct is None:
                # No PnL to compare: cannot confirm a duplicate -> keep as own
                # row (fail-safe: do not delete on a data gap).
                anchor = rid
                continue
            anchor_pnl = conn.execute(
                "SELECT pnl_pct FROM partial_close_shadow WHERE id=?", (anchor,)
            ).fetchone()[0]
            if anchor_pnl is not None and abs(anchor_pnl - pnl_pct) <= TOL:
                # Merge: refresh the anchor to THIS (later) row's state.
                conn.execute(
                    "UPDATE partial_close_shadow SET ts=?, pnl_pct=?, "
                    "amount_usd=?, pnl_usd_est=?, allowed=?, reason=? WHERE id=?",
                    (ts, pnl_pct, amount, est, allowed, reason, anchor),
                )
                conn.execute("DELETE FROM partial_close_shadow WHERE id=?", (rid,))
                merged += 1
            else:
                # Genuine jump -> open a new stage.
                anchor = rid
        kept = conn.execute(
            "SELECT COUNT(*) FROM partial_close_shadow "
            "WHERE position_id=? AND path=? AND ROUND(close_pct,2)=?",
            (pid, path, cp),
        ).fetchone()[0]
        kept_total += kept
        print(f"  group pos={pid} path={path} close={cp}% -> kept {kept} stage(s)")
    return len(groups), merged, kept_total


def main():
    if len(sys.argv) > 1:
        db_path = Path(sys.argv[1])
    else:
        db_path = Path(__file__).resolve().parent.parent / "data" / "trading.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000")
    print(f"DB: {db_path}")
    print("=== BEFORE ===")
    print(f"  total rows        : {count(conn)}")
    print(f"  ZM 3590737653     : {count(conn, 'position_id=?', ('3590737653',))}")
    print(f"  amount_usd NULL   : {count(conn, 'amount_usd IS NULL')}")

    try:
        filled, detail = backfill_amounts(conn)
        groups, merged, kept = collapse_duplicates(conn)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"ROLLBACK — error: {e}")
        raise

    print("=== ACTIONS ===")
    for rid, pid, amount in detail:
        print(f"  backfilled id={rid} pos={pid} amount_usd={amount}")
    print(f"  backfilled amounts : {filled}")
    print(f"  dup groups collapsed: {groups}, rows merged: {merged}")

    print("=== AFTER ===")
    print(f"  total rows        : {count(conn)}")
    print(f"  ZM 3590737653     : {count(conn, 'position_id=?', ('3590737653',))}")
    print(f"  amount_usd NULL   : {count(conn, 'amount_usd IS NULL')}")
    conn.close()


if __name__ == "__main__":
    main()
