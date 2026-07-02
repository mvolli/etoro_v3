#!/usr/bin/env python3
"""
eToro Trading Bot V3 — Reconciler Worker
src/bot/workers/reconciler.py

Runs every 5 minutes (at :02 past each 5-min mark via cron/scheduler).
Syncs live eToro API positions with the local SQLite database.

Responsibilities:
  1. Fetch live positions from GET /trading/info/real/pnl
  2. Upsert positions into portfolio_snapshot
  3. Orphan detection: delete stale snapshots (> 5 min old)
  4. Trade reconciliation: mark ACTIVE trades CLOSED if no API position found
  5. Update system_state: CURRENT_EQUITY, LAST_RECONCILE, position count
  6. Update PEAK_EQUITY if new high
  7. Detect / update CURRENT_REGIME
  8. Print summary and persist structured log entry
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── path bootstrap ────────────────────────────────────────────────────────────
# This file lives at src/bot/workers/reconciler.py; parent.parent.parent = src/
# We need the *src/* directory on sys.path so `bot.*` imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# ── project root (for config + data) ─────────────────────────────────────────
# src/bot/workers/ → parent × 3 = project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

import yaml  # type: ignore[import]

from bot.api.client import APIError, ClientConfig, EToroClient
from bot.api.instruments import get_instrument_map
from bot.core.regime import detect_regime
from bot.db.connection import DB
from bot.db.repo import LogRepo, PortfolioRepo, StateRepo, TradeRepo

# ── Discord Embeds ─────────────────────────────────────────────────────────────
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..'))
    import discord_embeds as _DE
except Exception:
    _DE = None

def _discord(fn_name: str, **kwargs) -> None:
    """Best-effort Discord post. Never raises."""
    try:
        if _DE and hasattr(_DE, fn_name):
            getattr(_DE, fn_name)(**kwargs)
    except Exception:
        pass

# ── constants ─────────────────────────────────────────────────────────────────
WORKER_NAME = "reconciler"
ORPHAN_THRESHOLD_MINUTES = 5

# NOTE: The hand-maintained _FALLBACK_INSTRUMENT_MAP dict that used to live
# here has been removed (fix/consolidate-instrument-map-truth). It was a
# fourth, independent source of instrument_id->symbol truth alongside the
# `instruments` DB table, data/instrument_map.json, and watchlist_multiasset
# — its own comments documented a history of past errors ("was wrongly X")
# that a hardcoded, manually-updated dict has no mechanism to catch when it
# drifts again. Symbol resolution now goes through get_instrument_map()
# (bot.api.instruments), which reads from the same `instruments` DB table
# every other worker uses, with the live-API Tier 2 lookup in
# _resolve_symbol() below as the safety net for anything not yet in the DB.


# ── helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    """Return UTC timestamp as ISO-8601 string: '2024-01-15 09:30:00'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utcnow_minus(minutes: int) -> str:
    """Return UTC timestamp N minutes ago as ISO-8601 string."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _load_config() -> dict:
    """Load config/config.yaml relative to project root. Raises on missing file."""
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with config_path.open() as fh:
        return yaml.safe_load(fh)


def _load_env_keys() -> tuple[str, str]:
    """
    Read ETORO_API_KEY and ETORO_USER_KEY from ~/.hermes/.env.
    Falls back to environment variables if .env is absent.
    Raises RuntimeError if either key is missing.
    """
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


# NOTE: _load_instrument_map() has been removed — reconciler now calls
# bot.api.instruments.get_instrument_map() directly (see main(), step 6),
# which already implements file cache -> trading.db `instruments` table ->
# legacy DB -> empty, and is shared with data_worker instead of duplicated.


def _extract_positions(portfolio_payload: dict) -> list[dict]:
    """
    Extract the positions list from the /trading/info/real/pnl response.
    eToro nests positions under clientPortfolio.positions.
    """
    client_portfolio = portfolio_payload.get("clientPortfolio", {})
    positions = client_portfolio.get("positions", [])
    return positions if isinstance(positions, list) else []


def _extract_equity(portfolio_payload: dict) -> float:
    """Extract portfolio equity from /trading/info/real/pnl.

    eToro API structure:
    - clientPortfolio.equity: not always present
    - clientPortfolio.credit: available cash balance
    - positions[].amount: invested amount per position
    - clientPortfolio.unrealizedPnL: total unrealized PnL (float at top level)

    Equity = credit (cash) + sum(position amounts) + unrealizedPnL
    """
    cp = portfolio_payload.get("clientPortfolio", {})

    # Try direct equity field first
    equity = cp.get("equity")
    if equity is not None:
        try:
            return float(equity)
        except (TypeError, ValueError):
            pass

    # Correct calculation: cash + exposure + pnl
    credit = float(cp.get("credit", 0) or 0)
    positions = _extract_positions(portfolio_payload)
    total_amount = sum(float(p.get("amount", 0)) for p in positions)
    unrealized_pnl = float(cp.get("unrealizedPnL", 0) or 0)

    return credit + total_amount + unrealized_pnl


def _resolve_symbol(
    instr_id: int | None,
    instrument_map: dict[int, str],
    client: EToroClient | None = None,
) -> tuple[str, bool]:
    """
    Resolve an instrument_id to a symbol using a 3-tier lookup strategy.

    Tier 1: Local instrument_map (fastest — in-memory cache)
    Tier 2: Live eToro API via client.get_instrument_metadata()
    Tier 3: UNKNOWN fallback when both fail

    Returns (symbol, was_resolved_via_api) where was_resolved_via_api is True
    if we had to call the live API to resolve this ID. Callers can use this
    to update the persistent cache with newly discovered mappings.

    Fail-open: returns "UNKNOWN_{id}" on any failure — never raises.
    """
    if instr_id is None:
        return ("UNKNOWN", False)

    # Tier 1: Local map (fast path — no network call)
    symbol = instrument_map.get(instr_id)
    if symbol:
        return (symbol, False)

    # Tier 2: Live API lookup for unknown IDs
    if client is not None:
        try:
            meta = client.get_instrument_metadata(instr_id)
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
                    # Update the local map for future lookups in this cycle
                    instrument_map[instr_id] = live_symbol
                    return (live_symbol, True)
        except Exception as exc:
            print(
                f"[{WORKER_NAME}] DEBUG: API lookup for ID {instr_id} failed: {exc}",
                file=sys.stderr,
            )

    # Tier 3: UNKNOWN fallback — fail-open
    return (f"UNKNOWN_{instr_id}", False)


def _build_snapshot_record(pos: dict, instrument_map: dict[int, str], client: EToroClient | None = None) -> tuple[dict, set[int]]:
    """
    Map an eToro API position dict into the portfolio_snapshot schema.

    Now uses a 3-tier symbol resolution (local map → live API → UNKNOWN).
    Returns (record, api_resolved_ids) where api_resolved_ids contains any
    instrument IDs that were resolved via the live API and should be persisted.

    API fields:
      positionID, instrumentID, amount, openRate,
      unrealizedPnL.pnL, unrealizedPnL.pnLPct,
      stopLossRate, isNoStopLoss
    """
    instrument_id = pos.get("instrumentID") or pos.get("instrumentId")
    unrealized = pos.get("unrealizedPnL") or {}

    pnl = unrealized.get("pnL") or unrealized.get("pnl")
    pnl_pct = unrealized.get("pnLPct") or unrealized.get("pnlPct")

    # Safely coerce numeric types
    def _float(v: object) -> float | None:
        if v is None:
            return None
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _int(v: object) -> int | None:
        if v is None:
            return None
        try:
            return int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    instr_id = _int(instrument_id)
    symbol, api_resolved = _resolve_symbol(instr_id, instrument_map, client)
    open_rate = _float(pos.get("openRate"))

    record = {
        "api_position_id":    str(pos.get("positionID") or pos.get("positionId", "")),
        "instrument_id":      instr_id,
        "symbol":             symbol,
        "is_buy":             1,
        "amount_usd":         _float(pos.get("amount")),
        "open_price":         open_rate,
        "current_price":      open_rate,  # best available; no live price in this endpoint
        "unrealized_pnl":     _float(pnl),
        "unrealized_pnl_pct": _float(pnl_pct),
        "stop_loss_rate":     _float(pos.get("stopLossRate")),
        "is_no_stop_loss":    1 if pos.get("isNoStopLoss") else 0,
        "last_synced":        _utcnow(),
    }

    api_resolved_ids: set[int] = set()
    if api_resolved and instr_id is not None:
        api_resolved_ids.add(instr_id)

    return (record, api_resolved_ids)


def _save_instrument_map_update(instrument_map: dict[int, str], new_ids: dict[int, str]) -> None:
    """
    Atomically merge newly resolved instrument IDs into the persistent cache file.

    Uses atomic write (write to temp + rename) to avoid partial writes on crash.
    Also updates the instruments table in trading.db for each new entry.
    """
    if not new_ids:
        return

    # Merge with existing map
    merged = dict(instrument_map)
    merged.update(new_ids)

    # Atomic write to cache file
    cache_file = PROJECT_ROOT / "data" / "instrument_map.json"
    tmp_file = cache_file.with_suffix(".tmp")
    try:
        payload = {
            "_meta": {"saved_at": datetime.now(timezone.utc).isoformat()},
            "map": {str(k): v for k, v in merged.items()},
        }
        tmp_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp_file.rename(cache_file)  # atomic on same filesystem
        print(f"[{WORKER_NAME}] INFO: Updated instrument_map.json with {len(new_ids)} new entries")
    except OSError as exc:
        print(f"[{WORKER_NAME}] WARNING: Failed to update instrument_map.json: {exc}", file=sys.stderr)

    # Also persist to instruments table in trading.db
    try:
        db_cfg_path = PROJECT_ROOT / "config" / "config.yaml"
        if db_cfg_path.exists():
            with db_cfg_path.open() as fh:
                cfg = yaml.safe_load(fh)
        db_path = PROJECT_ROOT / (cfg.get("db", {}).get("path", "data/trading.db") if db_cfg_path.exists() else "data/trading.db")
        
        db = DB(db_path=db_path, busy_timeout_ms=5000)
        for iid, sym in new_ids.items():
            # Guess asset class from symbol patterns
            if any(sym.endswith(suffix) for suffix in ("-USD", "/USD")):
                asset_class = "CRYPTO"
            elif "=" in sym:
                asset_class = "FOREX"
            else:
                asset_class = "STOCK"
            
            db.execute(
                """INSERT OR IGNORE INTO instruments (instrument_id, symbol, asset_class, last_updated)
                   VALUES (?, ?, ?, ?)""",
                (iid, sym, asset_class, _utcnow()),
            )
        print(f"[{WORKER_NAME}] INFO: Upserted {len(new_ids)} instruments into trading.db")
    except Exception as exc:
        print(f"[{WORKER_NAME}] WARNING: Failed to update instruments table: {exc}", file=sys.stderr)


# ── main reconciliation logic ─────────────────────────────────────────────────

def main() -> int:
    """
    Entry point.  Returns 0 on success, 1 on failure.
    Logs all errors via LogRepo before exiting with code 1.
    """
    # ── Worker lock: prevent overlapping cron invocations ────────────────────
    from bot.core.worker_lock import worker_lock

    with worker_lock("reconciler") as acquired:
        if not acquired:
            print(f"[{WORKER_NAME}] SKIPPED (already running)")
            return 0

        # ── 1. Load config ─────────────────────────────────────────────────────────
        try:
            cfg = _load_config()
        except Exception as exc:
            print(f"[{WORKER_NAME}] FATAL: Cannot load config: {exc}", file=sys.stderr)
            return 1
    
        # ── 2. Initialise DB ───────────────────────────────────────────────────────
        db_cfg = cfg.get("db", {})
        db_path = PROJECT_ROOT / db_cfg.get("path", "data/trading.db")
        db = DB(
            db_path=db_path,
            busy_timeout_ms=db_cfg.get("busy_timeout_ms", 5000),
        )
    
        log_repo    = LogRepo(db)
        state_repo  = StateRepo(db)
        portfolio_repo = PortfolioRepo(db)
        trade_repo  = TradeRepo(db)

        # ── Heartbeat (dead-man's switch) ─────────────────────────────────────────
        from bot.core.heartbeat import record_heartbeat
        record_heartbeat(state_repo, "reconciler")
    
        # ── 3. Load API credentials ────────────────────────────────────────────────
        try:
            api_key, user_key = _load_env_keys()
        except RuntimeError as exc:
            msg = f"FATAL: Missing API credentials: {exc}"
            print(f"[{WORKER_NAME}] {msg}", file=sys.stderr)
            log_repo.write("ERROR", WORKER_NAME, msg)
            return 1
    
        # ── 4. Initialise API client ───────────────────────────────────────────────
        api_cfg = cfg.get("api", {})
        client_config = ClientConfig.from_dict(api_cfg)
        client = EToroClient(api_key=api_key, user_key=user_key, config=client_config)
    
        # ── 5. Fetch live positions from eToro API ─────────────────────────────────
        portfolio_payload = None
        try:
            portfolio_payload = client.get_portfolio()
        except APIError as exc:
            msg = f"API call failed: GET /trading/info/real/pnl → {exc}"
            print(f"[{WORKER_NAME}] ERROR: {msg}", file=sys.stderr)
            log_repo.write("ERROR", WORKER_NAME, msg, {"status_code": exc.status_code, "endpoint": exc.endpoint})
            client.close()
            return 1
        except Exception as exc:
            msg = f"Unexpected error fetching portfolio: {exc}"
            print(f"[{WORKER_NAME}] ERROR: {msg}", file=sys.stderr)
            log_repo.write("ERROR", WORKER_NAME, msg)
            client.close()
            return 1
    
        # ── 6. Extract positions + equity ─────────────────────────────────────────
        live_positions  = _extract_positions(portfolio_payload)
        current_equity  = _extract_equity(portfolio_payload)
        instrument_map  = get_instrument_map(client=client)
    
        live_position_ids: set[str] = set()
    
        # ── 6.5 Circuit Breaker: suddenly empty positions list is suspicious ─────
        # fix/autonomy-hardening: the breaker used to stall FOREVER when all
        # positions were legitimately closed (SL cascade, manual sell-off):
        # the API keeps returning 0 positions, POSITION_COUNT stays > 0, and
        # every cycle exits with code 1. Escape hatch: after
        # EMPTY_STREAK_ACCEPT_AFTER consecutive suspicious-empty cycles
        # (~15 min), accept the empty portfolio as real — with a CRITICAL
        # alert so a human still looks at it.
        EMPTY_STREAK_ACCEPT_AFTER = 3
        previous_position_count = int(state_repo.get("POSITION_COUNT") or 0)
        if not live_positions and previous_position_count > 0:
            empty_streak = int(state_repo.get("RECONCILER_EMPTY_STREAK") or 0) + 1
            state_repo.set("RECONCILER_EMPTY_STREAK", str(empty_streak))

            if empty_streak < EMPTY_STREAK_ACCEPT_AFTER:
                msg = (
                    f"SUSPICIOUS: API returned 0 positions, but POSITION_COUNT was "
                    f"{previous_position_count} last run (Streak {empty_streak}/"
                    f"{EMPTY_STREAK_ACCEPT_AFTER}). Skipping trade-closure and "
                    f"equity-update logic this cycle — likely transient API glitch."
                )
                print(f"[{WORKER_NAME}] ERROR: {msg}", file=sys.stderr)
                log_repo.write("ERROR", WORKER_NAME, msg,
                               {"previous_count": previous_position_count,
                                "empty_streak": empty_streak})
                _discord(
                    "post_alert_embed",
                    title="🔴 Reconciler: verdächtig leere API-Antwort",
                    description=msg,
                    severity="CRITICAL",
                )
                client.close()
                return 1

            # Streak limit reached → accept the empty portfolio as real
            msg = (
                f"API liefert seit {empty_streak} Zyklen 0 Positionen "
                f"(vorher {previous_position_count}) — akzeptiere leeres "
                f"Portfolio als real und synchronisiere. Manuelle Prüfung empfohlen!"
            )
            print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
            log_repo.write("WARNING", WORKER_NAME, msg,
                           {"previous_count": previous_position_count,
                            "empty_streak": empty_streak})
            _discord(
                "post_alert_embed",
                title="🟠 Reconciler: leeres Portfolio bestätigt",
                description=msg,
                severity="CRITICAL",
            )
            state_repo.set("RECONCILER_EMPTY_STREAK", "0")
        else:
            # Non-empty (or genuinely never had positions) → reset streak
            if int(state_repo.get("RECONCILER_EMPTY_STREAK") or 0) != 0:
                state_repo.set("RECONCILER_EMPTY_STREAK", "0")

        # ── 6.6 Persist REAL available cash (fix/autonomy-hardening) ─────────────
        # clientPortfolio.credit is the broker-side cash balance. The signal
        # worker prefers this over its equity−exposure estimate for the
        # cash gate.
        try:
            _credit = portfolio_payload.get("clientPortfolio", {}).get("credit")
            if _credit is not None:
                state_repo.set("AVAILABLE_CASH", str(float(_credit)))
        except (TypeError, ValueError):
            pass
    
        # ── 7. Upsert each live position into portfolio_snapshot ──────────────────
        synced_count = 0
        all_api_resolved_ids: dict[int, str] = {}  # id -> symbol for newly resolved
        for pos in live_positions:
            record, api_resolved_ids = _build_snapshot_record(pos, instrument_map, client)
            pos_id = record["api_position_id"]
            if not pos_id:
                continue  # skip malformed entries
            live_position_ids.add(pos_id)
            # Track newly resolved IDs for persistence
            for rid in api_resolved_ids:
                all_api_resolved_ids[rid] = record["symbol"]
            try:
                portfolio_repo.upsert(record)
                synced_count += 1
            except Exception as exc:
                msg = f"Failed to upsert position {pos_id}: {exc}"
                print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
                log_repo.write("WARNING", WORKER_NAME, msg, {"position_id": pos_id})

        # ── 7.5 Persist any newly resolved instrument IDs ────────────────────────
        if all_api_resolved_ids:
            _save_instrument_map_update(instrument_map, all_api_resolved_ids)
            print(f"[{WORKER_NAME}] INFO: Resolved {len(all_api_resolved_ids)} new instrument IDs via API")

        # Close client after all API lookups are done
        client.close()

        # ── 8. Orphan detection: delete stale snapshots not in live API ───────────
        # Any position last_synced > ORPHAN_THRESHOLD_MINUTES ago is an orphan
        orphan_cutoff = _utcnow_minus(ORPHAN_THRESHOLD_MINUTES)
        all_snapshots = portfolio_repo.get_all()
        orphan_ids: list[str] = [
            snap["api_position_id"]
            for snap in all_snapshots
            if snap["api_position_id"] not in live_position_ids
            and snap["last_synced"] < orphan_cutoff
        ]
    
        orphan_count = 0
        for orphan_id in orphan_ids:
            try:
                db.execute(
                    "DELETE FROM portfolio_snapshot WHERE api_position_id = ?",
                    (orphan_id,),
                )
                orphan_count += 1
                msg = f"Orphan position removed: {orphan_id}"
                print(f"[{WORKER_NAME}] WARNING: {msg}")
                log_repo.write("WARNING", WORKER_NAME, msg, {"api_position_id": orphan_id})
            except Exception as exc:
                msg = f"Failed to delete orphan position {orphan_id}: {exc}"
                print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
                log_repo.write("WARNING", WORKER_NAME, msg)
    
        # ── 9. Trade reconciliation: mark ACTIVE trades CLOSED if no API match ────
        # ALSO backfill entry_price from portfolio_snapshot for ACTIVE trades that have None
        closed_trade_count = 0
        try:
            active_trades = trade_repo.get_by_status("ACTIVE")
        except Exception as exc:
            msg = f"Failed to query ACTIVE trades: {exc}"
            print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
            log_repo.write("WARNING", WORKER_NAME, msg)
            active_trades = []
    
        for trade in active_trades:
            pos_id = trade.get("api_position_id")
            if pos_id and pos_id in live_position_ids:
                # Position still live — backfill entry_price if missing
                snap = next(
                    (s for s in all_snapshots if s["api_position_id"] == pos_id),
                    None,
                )
                if snap and trade.get("entry_price") is None:
                    open_price = snap.get("open_price")
                    if open_price:
                        try:
                            trade_repo.update_status(
                                trade["id"],
                                "ACTIVE",
                                entry_price=float(open_price),
                            )
                            msg = f"Trade {trade['id']} ({trade.get('symbol')}) backfilled entry_price={open_price}"
                            print(f"[{WORKER_NAME}] INFO: {msg}")
                            log_repo.write("INFO", WORKER_NAME, msg, {"trade_id": trade["id"], "entry_price": open_price})
                        except Exception as exc:
                            msg = f"Failed to backfill entry_price for trade {trade['id']}: {exc}"
                            print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
                            log_repo.write("WARNING", WORKER_NAME, msg)
                continue  # still live — leave alone
    
            # ── Grace-Period: only close if position is confirmed orphaned ────────
            # Position missing this cycle but portfolio_snapshot row still exists
            # (< ORPHAN_THRESHOLD_MINUTES old → not yet confirmed orphaned).
            # Skip closure this cycle — next cycle will decide.
            if pos_id:
                still_has_snapshot = any(
                    s["api_position_id"] == pos_id for s in all_snapshots
                )
                if still_has_snapshot:
                    print(
                        f"[{WORKER_NAME}] INFO: Trade {trade['id']} ({trade.get('symbol')}): "
                        f"pos_id={pos_id} missing this cycle but not yet confirmed orphaned — deferring closure"
                    )
                    continue
    
            # Trade is ACTIVE in DB but has no matching live position → mark CLOSED
            trade_id = trade["id"]
            try:
                # Attempt to calculate pnl from portfolio_snapshot if we have a record
                pnl_usd: float | None = None
                pnl_pct: float | None = None
                if pos_id:
                    snap = next(
                        (s for s in all_snapshots if s["api_position_id"] == pos_id),
                        None,
                    )
                    if snap:
                        pnl_usd = snap.get("unrealized_pnl")
                        pnl_pct = snap.get("unrealized_pnl_pct")
    
                extra: dict = {"closed_at": _utcnow()}
                if pnl_usd is not None:
                    extra["pnl_usd"] = pnl_usd
                if pnl_pct is not None:
                    extra["pnl_pct"] = pnl_pct
    
                trade_repo.update_status(trade_id, "CLOSED", **extra)
                closed_trade_count += 1
    
                msg = (
                    f"Trade {trade_id} ({trade.get('symbol')}) marked CLOSED "
                    f"— no matching API position (api_position_id={pos_id!r})"
                )
                print(f"[{WORKER_NAME}] INFO: {msg}")
                log_repo.write(
                    "INFO", WORKER_NAME, msg,
                    {"trade_id": trade_id, "api_position_id": pos_id,
                     "pnl_usd": pnl_usd, "pnl_pct": pnl_pct},
                )
                # ── Discord: CLOSE Embed → #etoro-trades ─────────────────────
                _discord(
                    "post_position_closed_embed",
                    symbol=trade.get("symbol", "?"),
                    amount_usd=float(trade.get("amount_usd", 0)),
                    position_id=str(pos_id or ""),
                    pnl_usd=pnl_usd or 0.0,
                    pnl_pct=pnl_pct or 0.0,
                    reason="Position via Reconciler geschlossen (nicht mehr in API)",
                )
            except Exception as exc:
                msg = f"Failed to close trade {trade_id}: {exc}"
                print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
                log_repo.write("WARNING", WORKER_NAME, msg, {"trade_id": trade_id})
    
        # ── 10. Update system_state ────────────────────────────────────────────────
        now_str       = _utcnow()
        position_count = portfolio_repo.get_position_count()
    
        try:
            state_repo.set("CURRENT_EQUITY", str(current_equity))
            state_repo.set("LAST_RECONCILE", now_str)
            state_repo.set("POSITION_COUNT", str(position_count))
        except Exception as exc:
            msg = f"Failed to update system_state: {exc}"
            print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
            log_repo.write("WARNING", WORKER_NAME, msg)
    
        # ── 11. Update peak equity ─────────────────────────────────────────────────
        try:
            peak_equity = state_repo.get_float("PEAK_EQUITY", current_equity)
            if current_equity > peak_equity:
                state_repo.set("PEAK_EQUITY", str(current_equity))
                peak_equity = current_equity
        except Exception as exc:
            msg = f"Failed to update PEAK_EQUITY: {exc}"
            print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
            log_repo.write("WARNING", WORKER_NAME, msg)
            peak_equity = current_equity
    
        # ── 12. Update regime ─────────────────────────────────────────────────────
        try:
            previous_regime = state_repo.get_regime()
            regime, regime_reason = detect_regime(current_equity, peak_equity, previous_regime)
            state_repo.set_regime(regime)
            state_repo.set("DRAWDOWN_REASON", regime_reason)
    
            # Also persist drawdown pct
            if peak_equity > 0:
                drawdown_pct = ((peak_equity - current_equity) / peak_equity) * 100.0
                state_repo.set("DRAWDOWN_PCT", f"{drawdown_pct:.4f}")
        except Exception as exc:
            msg = f"Failed to update regime: {exc}"
            print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
            log_repo.write("WARNING", WORKER_NAME, msg)
            regime = state_repo.get_regime()
    
        # ── 13. Summary ───────────────────────────────────────────────────────────
        summary = (
            f"Reconciler: {synced_count} positions synced, "
            f"equity=${current_equity:.2f}, "
            f"regime={regime}"
        )
        print(summary)
    
        # ── 14. Persist structured log entry ─────────────────────────────────────
        log_repo.write(
            "INFO",
            WORKER_NAME,
            summary,
            {
                "positions_synced":   synced_count,
                "orphans_removed":    orphan_count,
                "trades_closed":      closed_trade_count,
                "equity":             current_equity,
                "peak_equity":        peak_equity,
                "position_count":     position_count,
                "regime":             regime,
                "regime_reason":      regime_reason if 'regime_reason' in dir() else "",
                "run_at":             now_str,
            },
        )
    
        return 0
    
    
    # ── entry point ───────────────────────────────────────────────────────────────
    
if __name__ == "__main__":
    sys.exit(main())
