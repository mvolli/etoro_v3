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

import logging
logger = logging.getLogger(__name__)

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
from bot.core.regime import detect_regime, update_regime
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


def is_fresh_fill_protected(confirmed_at: str | None, orphan_cutoff: str) -> bool:
    """True when an ACTIVE trade was confirmed too recently to orphan-close.

    fix/fresh-fill-orphan-guard (audit crit #4): a just-filled trade has no
    portfolio_snapshot row yet (execution_worker writes none on ACTIVE), so a
    single missing cycle must not close it. Timestamps are lexicographically
    comparable ISO-8601 strings; confirmed_at > orphan_cutoff means "within
    the last ORPHAN_THRESHOLD_MINUTES".
    """
    return bool(confirmed_at and confirmed_at > orphan_cutoff)


def classify_stale_submitting(
    pos_id: str | None,
    live_position_ids: set[str] | list[str],
    submitted_at: str | None,
    orphan_cutoff: str,
) -> str | None:
    """Recovery verdict for a stranded SUBMITTING trade (audit crit #6).

    Returns 'ACTIVE' (a confirmed position id is live → the fill went through,
    only the status transition was interrupted), 'FAILED' (past the orphan
    window with no live position → interrupted submit), or None (still within
    the grace window — execution_worker may still be verifying; leave it).
    """
    if pos_id and pos_id in live_position_ids:
        return "ACTIVE"
    if submitted_at and submitted_at < orphan_cutoff:
        return "FAILED"
    return None


LATE_FILL_WINDOW_HOURS = 48
LATE_FILL_AMOUNT_TOLERANCE = 0.25  # ±25% (Spread/Fees/Rundung)


def match_late_fill(
    trade_amount_usd: float,
    unclaimed_positions: list[dict],
) -> dict | None:
    """fix/late-fill-recovery (KTA.DE-Klasse, 2026-07-06): eToro füllt Orders
    teils NACH dem 120s-Verify-Fenster des execution_workers. Der Trade wurde
    dann fälschlich als Ghost FAILED klassifiziert, obwohl die Position real
    entstand — sie lief 'untracked' (10 Positionen/~$2.700 gefunden) und der
    Ghost-Blacklist-Zähler wurde unverdient erhöht.

    Pure Matching-Regel (bewusst konservativ):
      - GENAU EINE unbeanspruchte Live-Position des Instruments (eindeutig),
      - Betrag innerhalb ±LATE_FILL_AMOUNT_TOLERANCE des Trade-Betrags
        (eine bereits teilverkaufte oder fremde Position matcht nicht).
    Sonst None — im Zweifel kein Auto-Repair.
    """
    if len(unclaimed_positions) != 1 or trade_amount_usd <= 0:
        return None
    pos = unclaimed_positions[0]
    pos_amount = float(pos.get("amount_usd") or 0.0)
    if pos_amount <= 0:
        return None
    if abs(pos_amount - trade_amount_usd) / trade_amount_usd > LATE_FILL_AMOUNT_TOLERANCE:
        return None
    return pos


def match_late_fill_multi(
    ghost_failed_trades: list[dict],
    unclaimed_positions: list[dict],
) -> list[dict]:
    """fix/late-fill-multi (BOKU.L, 2026-07-14): Recover Ghost-Order-Failed
    Trades wenn 2+ Positionen des Instruments existieren.

    Beispiel: Trade 416 ($652.99) + Trade 418 ($325.88) → 2 Live-Positionen
    ($652.99 + $325.88). match_late_fill() braucht EXAKT 1 → scheitert.
    Diese Funktion matcht jeden Trade mit der best-passenden Position.

    Returns list of (trade, position) tuples that were successfully matched.
    Each matched position is removed from unclaimed_positions (by-side effect).
    """
    if not ghost_failed_trades or not unclaimed_positions:
        return []

    matched = []
    remaining_positions = list(unclaimed_positions)  # copy — we'll remove matched ones

    for trade in ghost_failed_trades:
        trade_amount = float(trade.get("amount_usd") or 0)
        if trade_amount <= 0:
            continue

        best_match = None
        best_diff = float('inf')

        for i, pos in enumerate(remaining_positions):
            pos_amount = float(pos.get("amount_usd") or 0)
            if pos_amount <= 0:
                continue
            diff = abs(pos_amount - trade_amount) / trade_amount
            if diff <= LATE_FILL_AMOUNT_TOLERANCE and diff < best_diff:
                best_match = (i, pos)
                best_diff = diff

        if best_match is not None:
            idx, pos = best_match
            remaining_positions.pop(idx)  # remove so it's not matched again
            matched.append((trade, pos))

    return matched


def infer_close_reason(matched: dict, trade_id: int) -> str:
    """Schliessungsgrund aus einem get_trade_history-Eintrag herleiten.

    feat/close-reason-inference (2026-07-20, User-Feedback MSI #464):
    "Finalisiert — API bestaetigt" sagt nur DASS zu, nicht WARUM. Die
    History-API liefert kein closeReason-Feld, aber stopLossRate/
    takeProfitRate + closeRate reichen fuer eine belastbare Herleitung:
    Fill am/unter dem Stop => Broker-SL (inkl. Gap-Ausweis), Fill am/
    ueber dem TP => Broker-TP, sonst extern (manuell/eToro-seitig) —
    Bot-initiierte Closes laufen ueber den sell_exits/trailing-Pfad
    und kommen hier gar nicht an.
    """
    def _f(key: str) -> float:
        try:
            return float(matched.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    close, sl, tp = _f("closeRate"), _f("stopLossRate"), _f("takeProfitRate")
    is_buy = bool(matched.get("isBuy", True))

    dur = ""
    try:
        from datetime import datetime as _dt
        o = _dt.fromisoformat(str(matched["openTimestamp"]).replace("Z", "+00:00"))
        c = _dt.fromisoformat(str(matched["closeTimestamp"]).replace("Z", "+00:00"))
        hours = (c - o).total_seconds() / 3600.0
        if hours >= 48:
            dur = f", Haltedauer {hours / 24:.1f}d"
        elif hours >= 0:
            dur = f", Haltedauer {hours:.0f}h"
    except Exception:
        pass

    base = f"Trade #{trade_id}, API-bestätigt via Reconciler{dur}"
    if not close:
        return f"✅ Finalisiert — {base}"

    tol = 0.005  # 0.5% Toleranz: Fills landen selten exakt auf dem Level
    sl_hit = sl and ((is_buy and close <= sl * (1 + tol)) or (not is_buy and close >= sl * (1 - tol)))
    tp_hit = tp and ((is_buy and close >= tp * (1 - tol)) or (not is_buy and close <= tp * (1 + tol)))
    if sl_hit:
        gap = (close / sl - 1) * 100 if is_buy else (sl / close - 1) * 100
        gap_txt = f" (Fill {gap:+.2f}% zum Stop = Gap/Slippage)" if abs(gap) > 0.05 else ""
        return f"🛑 Broker-Stop-Loss @ ${sl:g} ausgelöst{gap_txt} — {base}"
    if tp_hit:
        return f"🎯 Broker-Take-Profit @ ${tp:g} erreicht — {base}"
    return f"✋ Extern geschlossen (nicht vom Bot: manuell oder eToro-seitig) — {base}"


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
            logger.debug("[%s] DEBUG: API lookup for ID %s failed: %s", WORKER_NAME, instr_id, exc)

    # Tier 3: UNKNOWN fallback — fail-open
    return (f"UNKNOWN_{instr_id}", False)


def _build_snapshot_record(pos: dict, instrument_map: dict[int, str], client: EToroClient | None = None) -> tuple[dict, set[int]]:
    """
    Map an eToro API position dict into the portfolio_snapshot schema.

    Now uses a 3-tier symbol resolution (local map → live API → UNKNOWN).
    Returns (record, api_resolved_ids) where api_resolved_ids contains any
    instrument IDs that were resolved via the live API and should be persisted.

    API fields (verified live 2026-07-03 — pnLPct/pnlPct do not actually
    appear in this endpoint's response; closeRate is the live price):
      positionID, instrumentID, amount, openRate,
      unrealizedPnL.pnL, unrealizedPnL.closeRate,
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
    amount = _float(pos.get("amount"))

    # fix/reconciler-live-price: unrealizedPnL.pnLPct/pnlPct DO NOT EXIST in
    # the live /trading/info/real/pnl payload (verified 2026-07-03 against
    # all 17 open positions incl. crypto) — pnl_pct above was always None.
    # The live price the endpoint DOES provide is unrealizedPnL.closeRate
    # (same field risk_worker.py already reads to derive its own PnL%).
    # current_price was previously hardcoded to open_rate with the comment
    # "no live price in this endpoint" — that was wrong, and since this
    # worker runs at :02 (two minutes after data_worker's :00), it
    # overwrote data_worker's real yfinance-derived price with a stale
    # placeholder every single cycle, permanently flatlining
    # unrealized_pnl_pct at 0%/blank for every position in portfolio_snapshot.
    close_rate = _float(unrealized.get("closeRate")) if isinstance(unrealized, dict) else None
    if not close_rate:
        close_rate = _float(pos.get("closeRate")) or _float(pos.get("currentRate"))
    current_price = close_rate if close_rate else open_rate

    pnl_val = _float(pnl)
    if open_rate and close_rate:
        pnl_pct = (close_rate / open_rate - 1.0) * 100.0
    elif pnl_val is not None and amount:
        pnl_pct = (pnl_val / amount) * 100.0
    else:
        pnl_pct = _float(pnl_pct)  # last resort: the (usually absent) API field

    record = {
        "api_position_id":    str(pos.get("positionID") or pos.get("positionId", "")),
        "instrument_id":      instr_id,
        "symbol":             symbol,
        "is_buy":             1,
        "amount_usd":         amount,
        "open_price":         open_rate,
        "current_price":      current_price,
        "unrealized_pnl":     pnl_val,
        "unrealized_pnl_pct": pnl_pct,
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
        logger.info(f"[{WORKER_NAME}] Updated instrument_map.json with {len(new_ids)} new entries")
    except OSError as exc:
        logger.warning(f"[{WORKER_NAME}] WARNING: Failed to update instrument_map.json: {exc}")

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
        logger.info(f"[{WORKER_NAME}] Upserted {len(new_ids)} instruments into trading.db")
    except Exception as exc:
        logger.warning(f"[{WORKER_NAME}] WARNING: Failed to update instruments table: {exc}")


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
            logger.info(f"[{WORKER_NAME}] SKIPPED (already running)")
            return 0

        # ── Logging (WARNING only — INFO goes to Discord embed) ──────────────
        logging.basicConfig(
            level=logging.WARNING,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

        # ── 1. Load config ─────────────────────────────────────────────────────────
        try:
            import time as _time_dur
            _t_run_start = _time_dur.monotonic()
            cfg = _load_config()
        except Exception as exc:
            logger.critical(f"[{WORKER_NAME}] FATAL: Cannot load config: {exc}")
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
            logger.error("[%s] %s", WORKER_NAME, msg)
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
            logger.error(f"[{WORKER_NAME}] ERROR: {msg}")
            log_repo.write("ERROR", WORKER_NAME, msg, {"status_code": exc.status_code, "endpoint": exc.endpoint})
            client.close()
            return 1
        except Exception as exc:
            msg = f"Unexpected error fetching portfolio: {exc}"
            logger.error(f"[{WORKER_NAME}] ERROR: {msg}")
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
                logger.error(f"[{WORKER_NAME}] ERROR: {msg}")
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
            logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
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
                logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
                log_repo.write("WARNING", WORKER_NAME, msg, {"position_id": pos_id})

        # ── 7.5 Persist any newly resolved instrument IDs ────────────────────────
        if all_api_resolved_ids:
            _save_instrument_map_update(instrument_map, all_api_resolved_ids)
            logger.info(f"[{WORKER_NAME}] Resolved {len(all_api_resolved_ids)} new instrument IDs via API")

        # ── LLM-Empfehlungen autonom ausführen (vor client.close!) ──────────────
        try:
            from bot.core.llm_execution import execute_llm_recommendations
            llm_stats = execute_llm_recommendations(
                client=client,
                db=db,
                live_position_ids=live_position_ids,
                log_repo=log_repo,
            )
            if llm_stats["exit_count"] or llm_stats["tighten_count"]:
                logger.info(
                    "[%s] LLM-Execution: %d EXIT, %d TIGHTEN",
                    WORKER_NAME,
                    llm_stats["exit_count"],
                    llm_stats["tighten_count"],
                )
        except Exception as _llm_exc:
            logger.warning("[%s] LLM-Execution fehlgeschlagen (non-fatal): %s",
                           WORKER_NAME, _llm_exc)

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
                logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
                log_repo.write("WARNING", WORKER_NAME, msg, {"api_position_id": orphan_id})
            except Exception as exc:
                msg = f"Failed to delete orphan position {orphan_id}: {exc}"
                logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
                log_repo.write("WARNING", WORKER_NAME, msg)
    
        # ── 9. Trade reconciliation: mark ACTIVE trades CLOSED if no API match ────
        # ALSO backfill entry_price from portfolio_snapshot for ACTIVE trades that have None
        closed_trade_count = 0
        try:
            active_trades = trade_repo.get_by_status("ACTIVE")
        except Exception as exc:
            msg = f"Failed to query ACTIVE trades: {exc}"
            logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
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
                if snap and not trade.get("entry_price"):  # None ODER 0.0 (Post-flight schrieb frueher 0.0)
                    open_price = snap.get("open_price")
                    if open_price:
                        try:
                            trade_repo.update_status(
                                trade["id"],
                                "ACTIVE",
                                entry_price=float(open_price),
                            )
                            msg = f"Trade {trade['id']} ({trade.get('symbol')}) backfilled entry_price={open_price}"
                            logger.info(f"[{WORKER_NAME}] {msg}")
                            log_repo.write("INFO", WORKER_NAME, msg, {"trade_id": trade["id"], "entry_price": open_price})
                        except Exception as exc:
                            msg = f"Failed to backfill entry_price for trade {trade['id']}: {exc}"
                            logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
                            log_repo.write("WARNING", WORKER_NAME, msg)
                continue  # still live — leave alone
    
            # ── Fresh-fill grace (fix/fresh-fill-orphan-guard, audit crit #4) ─────
            # A trade confirmed within the orphan window must NEVER be closed on
            # a single missing cycle: execution_worker writes no portfolio_snapshot
            # row when it marks ACTIVE, so a just-filled trade has no snapshot yet.
            # If the live API momentarily omits its position (one transient hiccup,
            # not the all-empty case the circuit breaker guards), the old code fell
            # straight through to CLOSED with a fabricated $0 PnL while the position
            # was still open at the broker. Anchor the grace on confirmed_at age so
            # every fresh fill gets a full window regardless of snapshot presence.
            if is_fresh_fill_protected(trade.get("confirmed_at"), orphan_cutoff):
                logger.info(
                    "[%s] Trade %s (%s) confirmed %s — within %dmin orphan window, "
                    "too new to close on a missing cycle, deferring",
                    WORKER_NAME, trade["id"], trade.get("symbol"),
                    trade.get("confirmed_at"), ORPHAN_THRESHOLD_MINUTES,
                )
                continue

            # ── Grace-Period: only close if position is confirmed orphaned ────────
            # Position missing this cycle but portfolio_snapshot row still exists
            # (< ORPHAN_THRESHOLD_MINUTES old → not yet confirmed orphaned).
            # Skip closure this cycle — next cycle will decide.
            if pos_id:
                still_has_snapshot = any(
                    s["api_position_id"] == pos_id for s in all_snapshots
                )
                if still_has_snapshot:
                    logger.info(
                        "[%s] INFO: Trade %s (%s): pos_id=%s missing this cycle but not yet confirmed orphaned — deferring closure",
                        WORKER_NAME, trade['id'], trade.get('symbol'), pos_id
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
                else:
                    # Kein PnL aus Snapshot — step 9d holt reales netProfit aus API-History
                    extra["verification_status"] = "PENDING"
    
                trade_repo.update_status(trade_id, "CLOSED", **extra)
                closed_trade_count += 1
    
                msg = (
                    f"Trade {trade_id} ({trade.get('symbol')}) marked CLOSED "
                    f"— no matching API position (api_position_id={pos_id!r})"
                )
                logger.info(f"[{WORKER_NAME}] {msg}")
                log_repo.write(
                    "INFO", WORKER_NAME, msg,
                    {"trade_id": trade_id, "api_position_id": pos_id,
                     "pnl_usd": pnl_usd, "pnl_pct": pnl_pct},
                )
                # ── Discord: CLOSE Embed → #etoro-trades ─────────────────────
                # fix/duplicate-close-embed (2026-07-14): Wenn der Snapshot kein
                # PnL mehr hat (Position verschwand zwischen zwei Laeufen, z.B.
                # ueber Nacht), postete dieser Pfad "$0.00" und Minuten spaeter
                # postete 9d die echten API-Zahlen ("Finalisiert") — zwei
                # Embeds fuer einen Close, eins davon falsch (HLAG.DE
                # 2026-07-14: $0.00 vs real +$41.23). Jetzt: Embed nur bei
                # bekanntem PnL; sonst uebernimmt 9d (PENDING) die Meldung.
                if pnl_pct is not None:
                    _discord(
                        "post_position_closed_embed",
                        symbol=trade.get("symbol", "?"),
                        amount_usd=float(trade.get("amount_usd", 0)),
                        position_id=str(pos_id or ""),
                        pnl_usd=pnl_usd or 0.0,
                        pnl_pct=pnl_pct or 0.0,
                        reason="Position via Reconciler geschlossen (nicht mehr in API)",
                    )
                else:
                    logger.info(
                        f"[{WORKER_NAME}] Trade {trade_id}: PnL noch unbekannt — "
                        "Discord-Meldung folgt aus 9d-Finalisierung (API-History)"
                    )
            except Exception as exc:
                msg = f"Failed to close trade {trade_id}: {exc}"
                logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
                log_repo.write("WARNING", WORKER_NAME, msg, {"trade_id": trade_id})

        # ── 9b. Recover stranded SUBMITTING trades (fix/submitting-recovery, ─────────
        #        audit crit #6). execution_worker sets SUBMITTING before it
        #        confirms the fill; a crash/OOM/kill between there and the final
        #        ACTIVE/FAILED transition strands the trade forever — reconciler
        #        only ever queried ACTIVE, so nothing revisited it. Recover here:
        #        a confirmed position id that is live → ACTIVE; otherwise, once
        #        past the orphan window (execution's own verify loop caps at
        #        ~2min, so >5min is unambiguously stuck) → FAILED. If it did fill
        #        untracked, the position is still captured by the snapshot upsert
        #        above + risk_worker (which reads live positions, not trade rows).
        try:
            submitting_trades = trade_repo.get_by_status("SUBMITTING")
        except Exception as exc:
            logger.warning(f"[{WORKER_NAME}] WARNING: Failed to query SUBMITTING trades: {exc}")
            submitting_trades = []

        recovered_active = recovered_failed = 0
        for trade in submitting_trades:
            t_id = trade["id"]
            pos_id = trade.get("api_position_id")
            try:
                verdict = classify_stale_submitting(
                    pos_id, live_position_ids, trade.get("submitted_at"), orphan_cutoff
                )
                if verdict == "ACTIVE":
                    trade_repo.update_status(t_id, "ACTIVE", confirmed_at=_utcnow())
                    recovered_active += 1
                    msg = f"Trade {t_id} ({trade.get('symbol')}) recovered SUBMITTING→ACTIVE (position {pos_id} is live)"
                    logger.warning(f"[{WORKER_NAME}] {msg}")
                    log_repo.write("WARN", WORKER_NAME, msg, {"trade_id": t_id, "api_position_id": pos_id})
                elif verdict == "FAILED":
                    trade_repo.update_status(
                        t_id, "FAILED",
                        rejection_reason="Recovered from stale SUBMITTING — interrupted submit, no confirmed live position",
                        closed_at=_utcnow(),
                    )
                    recovered_failed += 1
                    msg = f"Trade {t_id} ({trade.get('symbol')}) recovered SUBMITTING→FAILED (stale, no live position)"
                    logger.warning(f"[{WORKER_NAME}] {msg}")
                    log_repo.write("WARN", WORKER_NAME, msg, {"trade_id": t_id, "submitted_at": trade.get("submitted_at")})
                # else (None): still within the grace window — execution_worker may be verifying; leave it.
            except Exception as exc:
                logger.warning(f"[{WORKER_NAME}] WARNING: SUBMITTING recovery failed for trade {t_id}: {exc}")
        if recovered_active or recovered_failed:
            logger.warning(
                "[%s] SUBMITTING recovery: %d→ACTIVE, %d→FAILED",
                WORKER_NAME, recovered_active, recovered_failed,
            )

        # ── 9c. Late-Fill-Recovery (fix/late-fill-recovery + fix/late-fill-multi) ──
        # FAILED-Ghost-Trades (<48h, mit order_id), deren Order sich NACH dem
        # Verify-Fenster doch materialisierte: die Live-Position existiert,
        # gehört aber keinem ACTIVE/CLOSING-Trade. Eindeutige Matches werden
        # auf ACTIVE repariert + der unverdiente Ghost-Zähler zurückgesetzt.
        #
        # fix/late-fill-multi (BOKU.L, 2026-07-14): Bei 2+ Positionen des
        # Instruments (z.B. Double-Execute von eToro) matcht match_late_fill()
        # nicht (braucht EXAKT 1). Daher: nach single-position Recovery wird
        # match_late_fill_multi() für verbleibende ungematchte Ghost-Trades
        # aufgerufen.
        late_fill_recovered = 0
        late_fill_multi_recovered = 0
        try:
            claimed_pos_ids = {
                str(t.get("api_position_id") or "")
                for t in trade_repo.get_by_status(["ACTIVE", "CLOSING"])
                if t.get("api_position_id")
            }
            _lf_cutoff = _utcnow_minus(LATE_FILL_WINDOW_HOURS * 60)
            ghost_failed = [
                t for t in trade_repo.get_by_status("FAILED")
                if (t.get("rejection_reason") or "").startswith("Ghost order")
                and t.get("order_id")
                and (t.get("created_at") or "") > _lf_cutoff
            ]

            # Phase 1: Single-position Recovery (wie bisher)
            unmatched_ghost_trades = []
            for trade in ghost_failed:
                iid = trade.get("instrument_id")
                unclaimed = [
                    s for s in all_snapshots
                    if s.get("instrument_id") == iid
                    and s["api_position_id"] in live_position_ids
                    and s["api_position_id"] not in claimed_pos_ids
                ]
                pos = match_late_fill(float(trade.get("amount_usd") or 0.0), unclaimed)
                if pos is not None:
                    t_id = trade["id"]
                    pos_id = pos["api_position_id"]
                    trade_repo.update_status(
                        t_id, "ACTIVE",
                        api_position_id=pos_id,
                        entry_price=float(pos.get("open_price") or 0.0) or None,
                        confirmed_at=_utcnow(),
                        rejection_reason=(
                            f"LATE FILL recovered: {(trade.get('rejection_reason') or '')[:120]}"
                        ),
                    )
                    if iid is not None:
                        trade_repo.reset_ghost_failures(int(iid))
                    claimed_pos_ids.add(pos_id)
                    late_fill_recovered += 1
                    msg = (
                        f"Trade {t_id} ({trade.get('symbol')}) LATE-FILL recovered "
                        f"FAILED→ACTIVE (position {pos_id}, ${pos.get('amount_usd'):.0f}) "
                        f"— Ghost-Zähler zurückgesetzt"
                    )
                    logger.warning(f"[{WORKER_NAME}] {msg}")
                    log_repo.write("WARN", WORKER_NAME, msg,
                                   {"trade_id": t_id, "api_position_id": pos_id})
                    _discord(
                        "post_alert_embed",
                        title=f"🔁 Late-Fill erkannt: {trade.get('symbol')}",
                        description=(
                            f"Order füllte NACH dem Verify-Fenster — Trade #{t_id} "
                            f"wurde FAILED→ACTIVE repariert (Position {pos_id}). "
                            f"Ghost-Blacklist-Zähler zurückgesetzt."
                        ),
                        severity="WARNING",
                    )
                else:
                    # Kein single-match — für multi-phase merken
                    unmatched_ghost_trades.append(trade)
                    # Instrument fuer Priority-Re-Check markieren
                    try:
                        iid = trade.get("instrument_id")
                        if iid:
                            db.execute(
                                "UPDATE instruments SET tradability_checked_at=NULL"
                                " WHERE instrument_id=? AND (is_tradable IS NULL OR is_tradable=1)",
                                (int(iid),),
                            )
                    except Exception:
                        pass

            # Phase 2: Multi-position Recovery (fix/late-fill-multi)
            # Wenn ghost_failed Trades übrig sind, die keine single-position hatten,
            # aber 2+ unclaimed positions existieren → match_late_fill_multi()
            if unmatched_ghost_trades:
                # Gruppieren nach instrument_id
                from collections import defaultdict
                by_instrument = defaultdict(list)
                for trade in unmatched_ghost_trades:
                    iid = trade.get("instrument_id")
                    if iid:
                        by_instrument[iid].append(trade)

                for iid, trades_in_instr in by_instrument.items():
                    unclaimed_for_instr = [
                        s for s in all_snapshots
                        if s.get("instrument_id") == iid
                        and s["api_position_id"] in live_position_ids
                        and s["api_position_id"] not in claimed_pos_ids
                    ]
                    if len(unclaimed_for_instr) < 2:
                        continue  # braucht mindestens 2 für multi-phase

                    matches = match_late_fill_multi(trades_in_instr, unclaimed_for_instr)
                    for trade, pos in matches:
                        t_id = trade["id"]
                        pos_id = pos["api_position_id"]
                        trade_repo.update_status(
                            t_id, "ACTIVE",
                            api_position_id=pos_id,
                            entry_price=float(pos.get("open_price") or 0.0) or None,
                            confirmed_at=_utcnow(),
                            rejection_reason=(
                                f"LATE-FILL MULTI recovered: {(trade.get('rejection_reason') or '')[:120]}"
                            ),
                        )
                        if iid is not None:
                            trade_repo.reset_ghost_failures(int(iid))
                        claimed_pos_ids.add(pos_id)
                        late_fill_multi_recovered += 1
                        msg = (
                            f"Trade {t_id} ({trade.get('symbol')}) LATE-FILL MULTI recovered "
                            f"FAILED→ACTIVE (position {pos_id}, ${pos.get('amount_usd'):.0f}) "
                            f"— {len(unclaimed_for_instr)} Positionen gematcht, Ghost-Zähler zurückgesetzt"
                        )
                        logger.warning(f"[{WORKER_NAME}] {msg}")
                        log_repo.write("WARN", WORKER_NAME, msg,
                                       {"trade_id": t_id, "api_position_id": pos_id, "multi_match": True})
                        _discord(
                            "post_alert_embed",
                            title=f"🔁 Late-Fill Multi erkannt: {trade.get('symbol')}",
                            description=(
                                f"eToro eröffnete {len(unclaimed_for_instr)} Trades — "
                                f"Trade #{t_id} wurde FAILED→ACTIVE repariert "
                                f"(Position {pos_id}). Ghost-Blacklist-Zähler zurückgesetzt."
                            ),
                            severity="WARNING",
                        )
        except Exception as exc:
            logger.warning(f"[{WORKER_NAME}] WARNING: Late-Fill-Recovery fehlgeschlagen: {exc}")

        # ── 9d. Finalize unverified closes (fix/sl-close-embed) ────────────────
        # Trades marked CLOSED with verification_status='PENDING' by risk_worker
        # when eToro API was too slow to confirm within the polling window.
        # Reconciler fetches final trade data from eToro history and updates DB.
        pending_verifications = trade_repo.get_pending_verification()
        finalized_count = 0
        for trade in pending_verifications:
            t_id = trade["id"]
            symbol = trade.get("symbol", "?")
            pos_id = trade.get("api_position_id")
            try:
                # Fetch trade history to find the exact close data.
                # Use default 90-day window (no min_date override) and paginate
                # to handle many recent closes. Page size capped at 100 by the API.
                history_trades = client.get_trade_history(page_size=100)
                # Fetch page 2 as well — avoids missing matches when many trades closed
                page2 = client.get_trade_history(page=2, page_size=100)
                if page2:
                    history_trades = history_trades + page2
                
                # Find matching trade by positionId or orderId
                matched = None
                if pos_id:
                    try:
                        _pos_id_int = int(pos_id)
                    except (ValueError, TypeError):
                        _pos_id_int = None
                    for ht in history_trades:
                        if _pos_id_int and ht.get("positionId") == _pos_id_int:
                            matched = ht
                            break
                
                if not matched and trade.get("order_id"):
                    for ht in history_trades:
                        if ht.get("orderId") == int(trade["order_id"]):
                            matched = ht
                            break
                
                if matched:
                    # We have the exact API data — use it!
                    final_pnl_usd = float(matched.get("netProfit", 0) or 0)
                    final_close_price = float(matched.get("closeRate", 0) or 0)
                    investment = float(matched.get("investment", 0) or 0)
                    final_pnl_pct = (final_pnl_usd / investment * 100) if investment > 0 else 0
                    
                    trade_repo.update_status(
                        t_id, "CLOSED",
                        exit_price=final_close_price or None,
                        pnl_usd=final_pnl_usd,
                        pnl_pct=final_pnl_pct,
                        verification_status="VERIFIED",
                    )
                    finalized_count += 1
                    
                    # Send finalization embed to Discord (+ Trade-Story-Chart)
                    try:
                        from bot.core.candle_chart import trade_story_png
                        import sys as _sys
                        from pathlib import Path as _P
                        _sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
                        import discord_embeds as _DE_st
                        _DE_st.attach_chart(trade_story_png(
                            client, trade.get("instrument_id"), symbol,
                            entry=float(matched.get("openRate", 0) or trade.get("entry_price") or 0) or None,
                            exit_price=final_close_price,
                            opened_at=trade.get("confirmed_at") or trade.get("created_at"),
                        ))
                    except Exception:
                        pass
                    _discord(
                        "post_position_closed_embed",
                        symbol=symbol,
                        amount_usd=investment or float(trade.get("amount_usd", 0)),
                        position_id=str(pos_id or ""),
                        entry_price=float(matched.get("openRate", 0) or trade.get("entry_price", 0) or 0),
                        close_price=final_close_price,
                        pnl_usd=final_pnl_usd,
                        pnl_pct=final_pnl_pct,
                        reason=infer_close_reason(matched, t_id),
                    )
                    
                    msg = f"Trade {t_id} ({symbol}) finalized from API: PnL=${final_pnl_usd:.2f} ({final_pnl_pct:+.2f}%)"
                    logger.info(f"[{WORKER_NAME}] {msg}")
                    log_repo.write("INFO", WORKER_NAME, msg, {"trade_id": t_id})
                    
                else:
                    # No history match yet — position may still be settling.
                    # Check if position is gone from live API as fallback
                    if not pos_id or pos_id not in live_position_ids:
                        # Position confirmed gone but no history yet — mark VERIFIED with estimate
                        trade_repo.update_status(
                            t_id, "CLOSED",
                            verification_status="VERIFIED",
                        )
                        finalized_count += 1
                        # fix/duplicate-close-embed: 9a postet ohne PnL kein
                        # Embed mehr — dieser Fallback ist dann die einzige
                        # Nutzer-Meldung. Ehrlich labeln statt $0.00 anzuzeigen.
                        try:
                            from bot.core.candle_chart import trade_story_png
                            import sys as _sys
                            from pathlib import Path as _P
                            _sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
                            import discord_embeds as _DE_st
                            _DE_st.attach_chart(trade_story_png(
                                client, trade.get("instrument_id"), symbol,
                                entry=float(trade.get("entry_price") or 0) or None,
                                exit_price=float(trade.get("exit_price") or 0) or None,
                                opened_at=trade.get("confirmed_at") or trade.get("created_at"),
                            ))
                        except Exception:
                            pass
                        _discord(
                            "post_position_closed_embed",
                            symbol=symbol,
                            amount_usd=float(trade.get("amount_usd", 0) or 0),
                            position_id=str(pos_id or ""),
                            pnl_usd=float(trade.get("pnl_usd") or 0.0),
                            pnl_pct=float(trade.get("pnl_pct") or 0.0),
                            reason="⚠️ Finalisiert ohne API-History — PnL unbestätigt (Schätzung)",
                        )
                        msg = f"Trade {t_id} ({symbol}) marked VERIFIED (position gone, no API history yet — using estimate)"
                        logger.info(f"[{WORKER_NAME}] {msg}")
                        log_repo.write("INFO", WORKER_NAME, msg, {"trade_id": t_id})
                    else:
                        # Position STILL exists — wait another cycle
                        msg = f"Trade {t_id} ({symbol}): position {pos_id} still in API — waiting another cycle"
                        logger.info(f"[{WORKER_NAME}] {msg}")
                        log_repo.write("INFO", WORKER_NAME, msg, {"trade_id": t_id})
                        continue
                
            except Exception as exc:
                msg = f"Failed to finalize trade {t_id} ({symbol}): {exc}"
                logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
                log_repo.write("WARNING", WORKER_NAME, msg, {"trade_id": t_id})

        if finalized_count > 0:
            logger.info(f"[{WORKER_NAME}] Finalized {finalized_count} pending verifications")

        # ── 10. Update system_state ────────────────────────────────────────────────
        now_str       = _utcnow()
        position_count = portfolio_repo.get_position_count()
    
        try:
            state_repo.set("CURRENT_EQUITY", str(current_equity))
            state_repo.set("LAST_RECONCILE", now_str)
            state_repo.set("POSITION_COUNT", str(position_count))
        except Exception as exc:
            msg = f"Failed to update system_state: {exc}"
            logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
            log_repo.write("WARNING", WORKER_NAME, msg)
    
        # ── 11. Update peak equity ─────────────────────────────────────────────────
        try:
            peak_equity = state_repo.get_float("PEAK_EQUITY", current_equity)
            if current_equity > peak_equity:
                state_repo.set("PEAK_EQUITY", str(current_equity))
                peak_equity = current_equity
        except Exception as exc:
            msg = f"Failed to update PEAK_EQUITY: {exc}"
            logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
            log_repo.write("WARNING", WORKER_NAME, msg)
            peak_equity = current_equity
    
        # ── 12. Update regime ─────────────────────────────────────────────────────
        # fix/reconciler-regime-consistency: use update_regime() (same as
        # risk_worker) for consistent 30-day rolling-peak drawdown and
        # automatic RISK_SCALAR update.
        drawdown_pct = 0.0
        try:
            regime, _ = update_regime(state_repo, current_equity)
            drawdown_pct = float(state_repo.get("DRAWDOWN_PCT") or 0.0)
        except Exception as exc:
            msg = f"Failed to update regime: {exc}"
            logger.warning(f"[{WORKER_NAME}] WARNING: {msg}")
            log_repo.write("WARNING", WORKER_NAME, msg)
            regime = state_repo.get_regime()
    
        # ── 13. Summary + Discord Embed ───────────────────────────────────────
        summary = (
            f"Reconciler: {synced_count} positions synced, "
            f"equity=${current_equity:.2f}, "
            f"regime={regime}"
        )

        # Build positions summary for embed
        positions_for_embed = []
        for snap in all_snapshots:
            positions_for_embed.append({
                "symbol": snap.get("symbol", "?"),
                "amount_usd": snap.get("amount_usd"),
                "unrealized_pnl_pct": snap.get("unrealized_pnl_pct"),
                "stop_loss_rate": snap.get("stop_loss_rate"),
                "is_no_stop_loss": snap.get("is_no_stop_loss", 0),
            })

        # Get available cash from state
        try:
            cash_val = float(state_repo.get("AVAILABLE_CASH") or current_equity)
        except (TypeError, ValueError):
            cash_val = current_equity

        # Discord embed → nur bei relevanten Ereignissen (monitor_worker übernimmt Routine)
        if closed_trade_count > 0 or orphan_count > 0:
         _discord(
            "post_reconciler_embed",
            equity=current_equity,
            peak_equity=peak_equity,
            position_count=position_count,
            synced_count=synced_count,
            orphan_count=orphan_count,
            trades_closed=closed_trade_count,
            regime=regime,
            drawdown_pct=drawdown_pct,
            available_cash=cash_val,
            positions_summary=positions_for_embed,
         )
    
        # ── 13b. 30-min-Portfolio-Heartbeat (fix/monitor-fold, 2026-07-15) ───────
        # Uebernommen vom aufgeloesten monitor_worker: der Reconciler hat hier
        # die frischesten Daten (AVAILABLE_CASH statt equity-exposure-
        # Schaetzung). Zeitbasiertes Gate statt Tick-Modulo — robust gegen
        # Lock-Skips. Worker-Staleness-Checks NICHT uebernommen: der
        # Kill-Switch-Watchdog ist die alleinige (externe, selbstheilende)
        # Instanz. WICHTIG: keine prints — Reconciler-stdout geht an Discord.
        try:
            from datetime import datetime as _hb_dt, timezone as _hb_tz
            _hb_due = True
            _hb_last = state_repo.get("HEARTBEAT_EMBED_AT") or ""
            if _hb_last:
                _last_dt = _hb_dt.fromisoformat(_hb_last)
                if _last_dt.tzinfo is None:
                    _last_dt = _last_dt.replace(tzinfo=_hb_tz.utc)
                _hb_due = (_hb_dt.now(_hb_tz.utc) - _last_dt).total_seconds() >= 29 * 60
            if _hb_due:
                state_repo.set("HEARTBEAT_EMBED_AT", _hb_dt.now(_hb_tz.utc).isoformat())
                try:
                    _tick = int(state_repo.get("MONITOR_TICK") or "0") + 1
                    state_repo.set("MONITOR_TICK", str(_tick))
                except Exception:
                    _tick = 0
                _cash_pct = (cash_val / current_equity * 100) if current_equity > 0 else 0.0
                _severity = ("CRITICAL" if drawdown_pct >= 8.0
                             else "WARNING" if drawdown_pct >= 4.0 else "OK")
                _discord(
                    "post_heartbeat_embed",
                    tick=_tick,
                    equity=current_equity,
                    cash=cash_val,
                    position_count=position_count,
                    drawdown_pct=drawdown_pct,
                    severity=_severity,
                    cb_active=regime in ("DEFENSIVE", "CRITICAL"),
                    elapsed_s=0.0,
                    positions_summary=positions_for_embed,  # feat/heartbeat-positions
                    cb_status={"regime": regime, "drawdown_pct": drawdown_pct,
                               "peak_equity": peak_equity},
                )
                # Cash-Emergency-Floor (aus monitor_worker uebernommen, aber
                # gegen den ECHTEN Cash-Wert AVAILABLE_CASH statt Schaetzung)
                _emergency_pct = float(cfg.get("trading", {}).get("cash_emergency_pct", 10.0))
                if current_equity > 0 and _cash_pct < _emergency_pct:
                    log_repo.write("ERROR", WORKER_NAME,
                                   f"Cash-Emergency: {_cash_pct:.1f}%",
                                   {"cash": cash_val, "equity": current_equity})
                    _discord(
                        "post_alert_embed",
                        title="🚨 Cash-Emergency-Floor verletzt",
                        description=(
                            f"Cash {_cash_pct:.1f}% liegt unter dem {_emergency_pct:.0f}%-Hard-Floor "
                            f"(${cash_val:.2f} / ${current_equity:.2f}). Der 15%-Soft-Floor haette "
                            f"Buys laengst blockieren muessen — Positionsgroessen/Reconcile pruefen!"
                        ),
                        severity="CRITICAL",
                    )
                # Regime-Alert (DEFENSIVE/CRITICAL) — 1x pro 30-min-Fenster
                if regime in ("DEFENSIVE", "CRITICAL"):
                    _risk_scalar = float(state_repo.get("RISK_SCALAR") or "0.5")
                    _discord(
                        "post_alert_embed",
                        title=f"{'🔴' if regime == 'CRITICAL' else '🟠'} {regime}-Regime aktiv",
                        description=(
                            f"Drawdown: **{drawdown_pct:.2f}%** | risk_scalar={_risk_scalar:.2f}\n"
                            f"Equity: **${current_equity:.2f}** | Peak: **${peak_equity:.2f}**\n"
                            f"{'Nur VERY_HIGH Signale' if regime == 'CRITICAL' else 'Nur HIGH+ Signale'} — kein Pyramiding."
                        ),
                        severity="CRITICAL" if regime == "CRITICAL" else "WARNING",
                    )
        except Exception as _hb_exc:
            logger.debug(f"[{WORKER_NAME}] Heartbeat-Embed uebersprungen: {_hb_exc}")

        try:
            from bot.core.heartbeat import record_duration as _rd
            _rd(state_repo, "reconciler", _time_dur.monotonic() - _t_run_start)
        except Exception:
            pass

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
