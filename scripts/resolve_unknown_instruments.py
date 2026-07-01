#!/usr/bin/env python3
"""
Resolve UNKNOWN instrument IDs from portfolio_snapshot via live eToro API.

Usage:
    python3 scripts/resolve_unknown_instruments.py [--dry-run]

This script:
1. Queries portfolio_snapshot for positions with symbol starting with "UNKNOWN_"
2. Calls the eToro API to resolve each unknown instrument_id → symbol
3. Updates data/instrument_map.json with new mappings (atomic write)
4. Updates the instruments table in trading.db
5. Updates portfolio_snapshot symbols from UNKNOWN to resolved

Requires ETORO_API_KEY and ETORO_USER_KEY env vars or ~/.hermes/.env.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── path bootstrap ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml  # type: ignore[import]

from bot.api.client import APIError, ClientConfig, EToroClient
from bot.db.connection import DB


def _load_env_keys() -> tuple[str, str]:
    """Load eToro API credentials from ~/.hermes/.env or environment."""
    import os
    import re

    env_path = Path.home() / ".hermes" / ".env"
    env_vars: dict[str, str] = {}

    if env_path.exists():
        with env_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^([A-Z0-9_]+)\s*=\s*(.+)$", line)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
                    env_vars[key] = val

    api_key = env_vars.get("ETORO_API_KEY") or os.environ.get("ETORO_API_KEY", "")
    user_key = env_vars.get("ETORO_USER_KEY") or os.environ.get("ETORO_USER_KEY", "")

    if not api_key:
        raise RuntimeError("ETORO_API_KEY not found in ~/.hermes/.env or environment")
    if not user_key:
        raise RuntimeError("ETORO_USER_KEY not found in ~/.hermes/.env or environment")

    return api_key, user_key


def _load_instrument_map() -> dict[int, str]:
    """Load existing instrument map from JSON file."""
    map_path = PROJECT_ROOT / "data" / "instrument_map.json"
    if map_path.exists():
        try:
            with map_path.open() as fh:
                raw: dict = json.load(fh)
            data = raw.get("map", raw)
            data = {k: v for k, v in data.items() if not k.startswith("_")}
            return {int(k): v for k, v in data.items()}
        except Exception as exc:
            print(f"WARNING: Failed to load instrument_map.json ({exc})")
    return {}


def _save_instrument_map(instrument_map: dict[int, str]) -> None:
    """Atomically save instrument map to JSON file."""
    cache_file = PROJECT_ROOT / "data" / "instrument_map.json"
    tmp_file = cache_file.with_suffix(".tmp")
    payload = {
        "_meta": {"saved_at": datetime.now(timezone.utc).isoformat(), "source": "resolve_unknown_instruments.py"},
        "map": {str(k): v for k, v in instrument_map.items()},
    }
    tmp_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_file.rename(cache_file)  # atomic on same filesystem


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve UNKNOWN instrument IDs via eToro API")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be resolved without saving")
    args = parser.parse_args()

    # ── 1. Load config & DB ────────────────────────────────────────────────
    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    if not cfg_path.exists():
        print("FATAL: config/config.yaml not found")
        return 1

    with cfg_path.open() as fh:
        cfg = yaml.safe_load(fh)

    db_path = PROJECT_ROOT / cfg.get("db", {}).get("path", "data/trading.db")
    db = DB(db_path=db_path, busy_timeout_ms=5000)

    # ── 2. Find UNKNOWN positions ──────────────────────────────────────────
    unknown_rows = db.fetchall(
        """SELECT DISTINCT instrument_id, symbol FROM portfolio_snapshot
           WHERE symbol LIKE 'UNKNOWN_%' AND instrument_id IS NOT NULL"""
    )

    if not unknown_rows:
        print("✅ No UNKNOWN instruments found — all good!")
        return 0

    unknown_ids = [(row["instrument_id"], row["symbol"]) for row in unknown_rows]
    print(f"Found {len(unknown_ids)} UNKNOWN instrument(s) to resolve:")
    for iid, sym in unknown_ids:
        print(f"  ID {iid} → {sym}")

    # ── 3. Load existing map & API client ──────────────────────────────────
    instrument_map = _load_instrument_map()

    try:
        api_key, user_key = _load_env_keys()
    except RuntimeError as exc:
        print(f"FATAL: {exc}")
        return 1

    api_cfg = cfg.get("api", {})
    client_config = ClientConfig.from_dict(api_cfg)
    client = EToroClient(api_key=api_key, user_key=user_key, config=client_config)

    # ── 4. Resolve each unknown ID via API ────────────────────────────────
    resolved: dict[int, str] = {}
    failed: list[int] = []

    for iid, old_sym in unknown_ids:
        # Skip if already known (shouldn't happen but safety check)
        if iid in instrument_map:
            print(f"  {iid} → already in map as {instrument_map[iid]}, skipping")
            continue

        try:
            meta = client.get_instrument_metadata(iid)
            if meta:
                live_symbol = (
                    meta.get("symbolFull")
                    or meta.get("internalSymbolFull")
                    or meta.get("symbol")
                    or meta.get("ticker")
                    or meta.get("displayName")
                    or ""
                )
                if live_symbol:
                    instrument_map[iid] = live_symbol
                    resolved[iid] = live_symbol
                    print(f"  ✓ {iid} → {live_symbol} (was {old_sym})")
                else:
                    failed.append(iid)
                    print(f"  ✗ {iid} → no symbol in API response")
            else:
                failed.append(iid)
                print(f"  ✗ {iid} → empty API response")
        except Exception as exc:
            failed.append(iid)
            print(f"  ✗ {iid} → ERROR: {exc}")

        # Rate limiting — don't hammer the API
        time.sleep(0.3)

    client.close()

    # ── 5. Persist results ────────────────────────────────────────────────
    if not resolved:
        print(f"\n⚠️  No instruments could be resolved ({len(failed)} failed)")
        return 1

    if args.dry_run:
        print(f"\n📋 Dry run — would resolve {len(resolved)} instrument(s):")
        for iid, sym in resolved.items():
            print(f"  {iid} → {sym}")
        return 0

    # Save to instrument_map.json
    _save_instrument_map(instrument_map)
    print(f"\n✅ Updated instrument_map.json with {len(resolved)} new entries")

    # Update instruments table in trading.db
    for iid, sym in resolved.items():
        if any(sym.endswith(suffix) for suffix in ("-USD", "/USD")):
            asset_class = "CRYPTO"
        elif "=" in sym:
            asset_class = "FOREX"
        else:
            asset_class = "STOCK"

        db.execute(
            """INSERT OR IGNORE INTO instruments (instrument_id, symbol, asset_class, last_updated)
               VALUES (?, ?, ?, ?)""",
            (iid, sym, asset_class, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        )

    # Update portfolio_snapshot symbols from UNKNOWN to resolved
    for iid, sym in resolved.items():
        db.execute(
            "UPDATE portfolio_snapshot SET symbol = ? WHERE instrument_id = ?",
            (sym, iid),
        )

    print(f"✅ Updated instruments table + portfolio_snapshot")

    if failed:
        print(f"\n⚠️  {len(failed)} ID(s) could not be resolved: {failed}")
        return 1

    print(f"\n🎉 All {len(resolved)} UNKNOWN instruments resolved successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
