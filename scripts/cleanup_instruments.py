#!/usr/bin/env python3
"""cleanup_instruments.py — fix/instrument-db-cleanup.

One-shot cleanup of the `instruments` table: removes/deactivates
delisted corpses, placeholder IDs and empty yfinance mappings, so the
data pipeline stops re-checking dead instruments every cooldown cycle.

SAFE BY DEFAULT:
  - Dry-run unless --apply is given.
  - --apply creates a timestamped DB backup first (data/backups/).
  - Nothing is DELETEd unless --verify-etoro confirmed eToro doesn't
    know the ID AND the row has zero references in
    trades/signals/portfolio_snapshot. Everything else is deactivated.
  - Aborts if the plan would delete >60% of the table (override: --force).

Usage:
    python3 scripts/cleanup_instruments.py                     # dry-run, conservative
    python3 scripts/cleanup_instruments.py --verify-etoro      # dry-run + live check
    python3 scripts/cleanup_instruments.py --verify-etoro --apply
    python3 scripts/cleanup_instruments.py --audit-json audit_results.json --verify-etoro --apply
    python3 scripts/cleanup_instruments.py --verify-etoro --apply --vacuum
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import yaml  # noqa: E402

from bot.core.instrument_cleanup import (  # noqa: E402
    ACTION_DEACTIVATE,
    ACTION_DELETE,
    ACTION_REVIEW,
    build_plan,
    load_audit_corrections,
)
from bot.db.connection import DB  # noqa: E402

DELETE_GUARDRAIL_PCT = 60.0  # abort if plan deletes more than this share


# ── schema introspection ──────────────────────────────────────────────────────

def _table_exists(db: DB, name: str) -> bool:
    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return row is not None


def _columns(db: DB, table: str) -> set[str]:
    return {r["name"] for r in db.fetchall(f"PRAGMA table_info({table})")}


def _ensure_is_active_column(db: DB, apply: bool) -> bool:
    """Make sure instruments.is_active exists (needed for DEACTIVATE)."""
    if "is_active" in _columns(db, "instruments"):
        return True
    if not apply:
        print("HINWEIS: Spalte instruments.is_active fehlt — würde bei --apply angelegt")
        return False
    db.execute("ALTER TABLE instruments ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    print("✓ Spalte instruments.is_active angelegt (DEFAULT 1)")
    return True


# ── data loading ──────────────────────────────────────────────────────────────

def _load_instrument_rows(db: DB) -> list[dict]:
    cols = _columns(db, "instruments")
    wanted = ["instrument_id", "symbol"]
    for opt in ("name", "yfinance_symbol", "yahoo_status", "is_active"):
        if opt in cols:
            wanted.append(opt)
    rows = db.fetchall(f"SELECT {', '.join(wanted)} FROM instruments")
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        # Only active rows are cleanup candidates; already-inactive rows
        # are left alone (they cause no overhead).
        if "is_active" in d and d.get("is_active") is not None and int(d["is_active"]) == 0:
            continue
        out.append(d)
    return out


def _load_referenced_ids(db: DB) -> set[int]:
    """All instrument_ids referenced by history/live tables — never deletable."""
    refs: set[int] = set()
    for table in ("trades", "signals", "portfolio_snapshot"):
        if not _table_exists(db, table):
            continue
        for r in db.fetchall(
            f"SELECT DISTINCT instrument_id FROM {table} WHERE instrument_id IS NOT NULL"
        ):
            try:
                refs.add(int(r["instrument_id"]))
            except (TypeError, ValueError):
                pass
    return refs


def _apply_audit_corrections(db: DB, corrections: dict[int, dict], apply: bool) -> int:
    """UPDATE instruments from the audit-scan result file. Symbol renames
    are skipped when they'd violate the UNIQUE(symbol) constraint."""
    cols = _columns(db, "instruments")
    n = 0
    for iid, fields in corrections.items():
        sets, params = [], []
        for col, val in fields.items():
            if col not in cols:
                continue
            if col == "symbol":
                clash = db.fetchone(
                    "SELECT instrument_id FROM instruments WHERE symbol=? AND instrument_id<>?",
                    (val, iid),
                )
                if clash:
                    print(f"  SKIP Rename ID {iid} → '{val}': Symbol bereits an "
                          f"ID {clash['instrument_id']} vergeben (UNIQUE)")
                    continue
            sets.append(f"{col}=?")
            params.append(val)
        if not sets:
            continue
        n += 1
        if apply:
            params.append(iid)
            db.execute(f"UPDATE instruments SET {', '.join(sets)} WHERE instrument_id=?", params)
    return n


# ── eToro live verification ───────────────────────────────────────────────────

def _verify_against_etoro(
    candidate_ids: list[int], cfg: dict
) -> tuple[dict[int, str], set[int]] | None:
    """Batch-resolve candidate IDs on the live eToro API.
    Returns ({id: symbolFull} for known IDs, {unverifiable ids}) — or None
    when the API is unusable entirely (missing keys) → caller falls back
    to conservative mode. Unverifiable IDs (transient chunk failures) must
    be KEPT by the caller, never treated as eToro-unknown."""
    from bot.api.client import APIError, ClientConfig, EToroClient
    from bot.core.instrument_verification import extract_live_symbol

    def _env_keys() -> tuple[str, str]:
        env_path = Path.home() / ".hermes" / ".env"
        env: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                m = re.match(r"^([A-Z0-9_]+)\s*=\s*(.+)$", line.strip())
                if m:
                    env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
        return (
            env.get("ETORO_API_KEY") or os.environ.get("ETORO_API_KEY", ""),
            env.get("ETORO_USER_KEY") or os.environ.get("ETORO_USER_KEY", ""),
        )

    api_key, user_key = _env_keys()
    if not api_key or not user_key:
        print("WARNUNG: keine API-Keys — --verify-etoro nicht möglich, konservativer Modus")
        return None

    client = EToroClient(
        api_key=api_key, user_key=user_key,
        config=ClientConfig.from_dict(cfg.get("api", {})),
    )
    known: dict[int, str] = {}
    try:
        # Batch endpoint: 50 IDs/request; APIError per chunk is tolerated —
        # a failed chunk's IDs simply stay 'unknown to eToro'? NO — that
        # would green-light deletion on a transient error. Failed chunks
        # are re-queried once; if they fail again, their IDs are marked
        # UNVERIFIABLE and the whole run degrades to conservative mode
        # for those IDs by injecting a sentinel.
        chunk_size = 50
        unverifiable: set[int] = set()
        max_retries = 3
        for i in range(0, len(candidate_ids), chunk_size):
            # Rate-limit protection: 1s Pause zwischen Batches
            if i > 0:
                time.sleep(1.0)
            chunk = candidate_ids[i:i + chunk_size]
            got: dict[int, dict] = {}
            for attempt in range(1, max_retries + 1):
                try:
                    got = client.get_instruments_metadata_batch(chunk, chunk_size=chunk_size)
                    break
                except APIError as exc:
                    wait = min(2 ** attempt, 30)  # exponential backoff: 2s, 4s, 8s...
                    print(f"  Batch {i//chunk_size+1}: APIError (HTTP {exc.status if hasattr(exc, 'status') else '?'}) — "
                          f"Versuch {attempt}/{max_retries}, warte {wait}s")
                    time.sleep(wait)
                    got = {}
            if not got and chunk:
                unverifiable.update(chunk)
                continue
            for iid, meta in got.items():
                sym = extract_live_symbol(meta)
                if sym:
                    known[int(iid)] = sym
            # IDs absent from a SUCCESSFUL batch response are genuinely
            # unknown to eToro — that's the deletable signal.
        if unverifiable:
            print(f"WARNUNG: {len(unverifiable)} IDs nicht verifizierbar (transiente "
                  f"API-Fehler) — werden behalten, nicht gelöscht")
    finally:
        try:
            client.close()
        except Exception:
            pass
    return (known, unverifiable)


# ── plan execution ────────────────────────────────────────────────────────────

def _backup_db(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"trading-{ts}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(backup_path))
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    return backup_path


def _execute_plan(db: DB, plan, mark_permanent_failed: bool) -> None:
    watchlist_tables = [t for t in ("watchlist", "watchlist_multiasset") if _table_exists(db, t)]
    failed_has_permanent = (
        _table_exists(db, "failed_symbols") and "permanent" in _columns(db, "failed_symbols")
    )

    for d in plan.delete:
        for wt in watchlist_tables:
            db.execute(f"DELETE FROM {wt} WHERE instrument_id=?", (d.instrument_id,))
        db.execute("DELETE FROM instruments WHERE instrument_id=?", (d.instrument_id,))

    for d in plan.deactivate + plan.review:
        if d.action == ACTION_REVIEW:
            continue  # review rows are report-only; audit decides
        db.execute("UPDATE instruments SET is_active=0 WHERE instrument_id=?", (d.instrument_id,))
        for wt in watchlist_tables:
            db.execute(f"DELETE FROM {wt} WHERE instrument_id=?", (d.instrument_id,))
        if mark_permanent_failed and failed_has_permanent and d.symbol:
            now = datetime.now().isoformat(sep=" ", timespec="seconds")
            db.execute(
                """INSERT INTO failed_symbols (symbol, first_failed_at, last_failed_at,
                                               failure_count, permanent)
                   VALUES (?, ?, ?, 1, 1)
                   ON CONFLICT(symbol) DO UPDATE SET permanent=1, last_failed_at=excluded.last_failed_at""",
                (d.symbol, now, now),
            )


def _print_plan(plan) -> None:
    print(f"\n═══ Cleanup-Plan: {plan.total} Kandidaten geprüft ═══")
    print(f"  KEEP:       {len(plan.keep):>6}")
    print(f"  DELETE:     {len(plan.delete):>6}  (unreferenziert + eToro-unbekannt)")
    print(f"  DEACTIVATE: {len(plan.deactivate):>6}  (referenziert oder unverifiziert)")
    print(f"  REVIEW:     {len(plan.review):>6}  (eToro-Symbol ≠ lokal → Symbol-Audit)")

    def _sample(items, label):
        if items:
            print(f"\n  Beispiele {label}:")
            for d in items[:10]:
                extra = f" [eToro: {d.etoro_symbol}]" if d.etoro_symbol else ""
                print(f"    ID {d.instrument_id:<8} {d.symbol:<14}{extra} — {d.reason[:90]}")

    _sample(plan.delete, "DELETE")
    _sample(plan.review, "REVIEW")
    _sample(plan.deactivate, "DEACTIVATE")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Instruments-Tabelle aufräumen (dry-run default)")
    p.add_argument("--apply", action="store_true", help="Änderungen wirklich schreiben (mit Backup)")
    p.add_argument("--verify-etoro", action="store_true",
                   help="Kandidaten live gegen eToro verifizieren (Voraussetzung für DELETE)")
    p.add_argument("--audit-json", type=Path, default=None,
                   help="Audit-Scan-Ergebnisse (Korrekturen) vor der Klassifikation anwenden")
    p.add_argument("--vacuum", action="store_true", help="VACUUM nach dem Cleanup")
    p.add_argument("--force", action="store_true",
                   help=f"Guardrail (max {DELETE_GUARDRAIL_PCT:.0f}%% Deletes) übersteuern")
    args = p.parse_args()

    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    db_path = PROJECT_ROOT / cfg.get("db", {}).get("path", "data/trading.db")
    if not db_path.exists():
        print(f"FATAL: DB nicht gefunden: {db_path}")
        return 1
    db = DB(db_path=db_path, busy_timeout_ms=5000)

    # 0. Audit-Korrekturen zuerst — sie können 'delisted' in gesund verwandeln
    if args.audit_json:
        try:
            raw = json.loads(args.audit_json.read_text())
        except Exception as exc:
            print(f"FATAL: --audit-json nicht lesbar: {exc}")
            return 1
        corrections = load_audit_corrections(raw)
        n = _apply_audit_corrections(db, corrections, apply=args.apply)
        print(f"Audit-Korrekturen: {n} Zeilen "
              f"{'angewendet' if args.apply else 'würden angewendet (dry-run)'}")

    # 1. Kandidaten + Referenzen laden
    rows = _load_instrument_rows(db)
    referenced = _load_referenced_ids(db)
    print(f"Instruments (aktiv): {len(rows)} | referenzierte IDs: {len(referenced)}")

    # 2. Optional: Live-Verifikation nur für Corpse-Kandidaten
    from bot.core.instrument_cleanup import is_corpse_candidate
    candidate_ids = [int(r["instrument_id"]) for r in rows if is_corpse_candidate(r)[0]]
    print(f"Corpse-Kandidaten: {len(candidate_ids)}")

    etoro_symbols: dict[int, str] | None = None
    if args.verify_etoro and candidate_ids:
        est_req = (len(candidate_ids) + 49) // 50
        print(f"Verifiziere {len(candidate_ids)} IDs gegen eToro (~{est_req} Batch-Requests)…")
        result = _verify_against_etoro(candidate_ids, cfg)
        if result is not None:
            etoro_symbols, unverifiable_ids = result
            print(f"eToro kennt {len(etoro_symbols)} der Kandidaten-IDs")
            # Unverifiable IDs: inject their LOCAL symbol so
            # classify_instrument() sees a match and KEEPs them —
            # a transient API failure must never green-light a DELETE.
            if unverifiable_ids:
                local_sym = {int(r["instrument_id"]): (r.get("symbol") or "") for r in rows}
                for iid in unverifiable_ids:
                    etoro_symbols.setdefault(iid, local_sym.get(iid, ""))

    # 3. Plan bauen
    plan = build_plan(rows, referenced, etoro_symbols)
    _print_plan(plan)

    # 4. Guardrail
    if rows and len(plan.delete) / len(rows) * 100.0 > DELETE_GUARDRAIL_PCT and not args.force:
        print(f"\nABBRUCH: Plan würde {len(plan.delete)}/{len(rows)} "
              f"(> {DELETE_GUARDRAIL_PCT:.0f}%) löschen — --force nötig")
        return 2

    if not args.apply:
        print("\nDry-run — nichts geändert. Mit --apply ausführen.")
        return 0

    # 5. Backup + Ausführung
    backup = _backup_db(db_path)
    print(f"\n✓ Backup: {backup}")
    _execute_plan(db, plan, mark_permanent_failed=True)
    print(f"✓ Ausgeführt: {len(plan.delete)} gelöscht, "
          f"{len(plan.deactivate)} deaktiviert, {len(plan.review)} für Audit markiert (Report)")

    if args.vacuum:
        sqlite3.connect(str(db_path)).execute("VACUUM").close()
        print("✓ VACUUM abgeschlossen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
