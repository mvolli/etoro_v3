#!/usr/bin/env python3
"""
eToro Trading Bot V3 — Reconciler Worker
src/bot/workers/reconciler.py

Runs every 5 minutes (at :02 past each 5-min mark via cron/scheduler).
Syncs live eToro API positions with the local SQLite database.

Responsibilities:
  1. Fetch live positions from GET /trading/info/real/pnl
  2. Upsert positions into portfolio_snapshot
  3. Orphan detection: delete stale snapshots (> 10 min old)
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
ORPHAN_THRESHOLD_MINUTES = 10

# Hardcoded fallback instrument_id → symbol map (used when data/instrument_map.json absent)
# VERIFIED via eToro watchlist API on 2026-06-24
_FALLBACK_INSTRUMENT_MAP: dict[int, str] = {
    # Commodities/Indices
    17:     "OIL",
    18:     "GOLD",
    19:     "SILVER",
    22:     "NATGAS",
    # Stocks (confirmed via watchlist API)
    1001:   "AAPL",
    1002:   "GOOG",
    1003:   "META",
    1004:   "MSFT",   # was wrongly NVDA/TSLA — confirmed MSFT
    1005:   "AMZN",
    1008:   "AA",
    1033:   "RTX",
    1037:   "C",
    1111:   "TSLA",   # confirmed TSLA (was wrongly at 1004/1246)
    1137:   "NVDA",
    1246:   "TTE.PA", # TotalEnergies (was wrongly TSLA/NFLX)
    1283:   "ENI.MI", # Eni SpA (was wrongly at 6700)
    3000:   "SPY",
    3006:   "QQQ",
    6700:   "XPEV",   # XPeng Motors (was wrongly ENI.MI)
    # Crypto (confirmed via watchlist API)
    100000: "BTC-USD", # BTC — confirmed (was wrongly XRP-USD!)
    100001: "ETH-USD", # ETH — logical inference (100001 follows BTC=100000)
    100002: "BCH-USD", # Bitcoin Cash — confirmed
    100003: "XRP-USD", # XRP — confirmed
    # Large-cap IDs
    127256: "USO",
    309144: "BABA",
    440969: "SLB",
    665842: "PFE",
    834108: "SLV",
}


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


def _load_instrument_map() -> dict[int, str]:
    """
    Return instrument_id → symbol mapping.
    Prefers data/instrument_map.json; falls back to hardcoded dict.
    """
    map_path = PROJECT_ROOT / "data" / "instrument_map.json"
    if map_path.exists():
        try:
            with map_path.open() as fh:
                raw: dict = json.load(fh)
            # Support both formats:
            # 1. {"_meta": {...}, "map": {"1003": "META", ...}}  (instruments.py format)
            # 2. {"1003": "META", ...}  (direct format)
            data = raw.get("map", raw)
            data = {k: v for k, v in data.items() if not k.startswith("_")}
            return {int(k): v for k, v in data.items()}
        except Exception as exc:
            print(
                f"[{WORKER_NAME}] WARNING: Failed to load instrument_map.json "
                f"({exc}) — using fallback",
                file=sys.stderr,
            )
    return dict(_FALLBACK_INSTRUMENT_MAP)


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


def _build_snapshot_record(pos: dict, instrument_map: dict[int, str]) -> dict:
    """
    Map an eToro API position dict into the portfolio_snapshot schema.

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
    symbol = instrument_map.get(instr_id, f"UNKNOWN_{instr_id}") if instr_id else "UNKNOWN"
    open_rate = _float(pos.get("openRate"))

    return {
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


# ── main reconciliation logic ─────────────────────────────────────────────────

def main() -> int:
    """
    Entry point.  Returns 0 on success, 1 on failure.
    Logs all errors via LogRepo before exiting with code 1.
    """
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
    try:
        portfolio_payload = client.get_portfolio()
    except APIError as exc:
        msg = f"API call failed: GET /trading/info/real/pnl → {exc}"
        print(f"[{WORKER_NAME}] ERROR: {msg}", file=sys.stderr)
        log_repo.write("ERROR", WORKER_NAME, msg, {"status_code": exc.status_code, "endpoint": exc.endpoint})
        return 1
    except Exception as exc:
        msg = f"Unexpected error fetching portfolio: {exc}"
        print(f"[{WORKER_NAME}] ERROR: {msg}", file=sys.stderr)
        log_repo.write("ERROR", WORKER_NAME, msg)
        return 1
    finally:
        client.close()

    # ── 6. Extract positions + equity ─────────────────────────────────────────
    live_positions  = _extract_positions(portfolio_payload)
    current_equity  = _extract_equity(portfolio_payload)
    instrument_map  = _load_instrument_map()

    live_position_ids: set[str] = set()

    # ── 7. Upsert each live position into portfolio_snapshot ──────────────────
    synced_count = 0
    for pos in live_positions:
        record = _build_snapshot_record(pos, instrument_map)
        pos_id = record["api_position_id"]
        if not pos_id:
            continue  # skip malformed entries
        live_position_ids.add(pos_id)
        try:
            portfolio_repo.upsert(record)
            synced_count += 1
        except Exception as exc:
            msg = f"Failed to upsert position {pos_id}: {exc}"
            print(f"[{WORKER_NAME}] WARNING: {msg}", file=sys.stderr)
            log_repo.write("WARNING", WORKER_NAME, msg, {"position_id": pos_id})

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
            continue  # still live — leave alone

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
