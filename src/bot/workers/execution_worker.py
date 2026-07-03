#!/usr/bin/env python3
"""eToro Trading Bot V3 — Execution Worker
src/bot/workers/execution_worker.py

Runs every 15 minutes at :04.
Executes APPROVED trades via the eToro API.

Schedule: 4,19,34,49 * * * * cd /path/to/etoro_v3 && python3 -m bot.workers.execution_worker
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("execution_worker")


# ── Discord Embeds ─────────────────────────────────────────────────────────
try:
    from pathlib import Path as _Path
    _bot_dir = str(_Path(__file__).resolve().parent.parent)
    import sys as _sys
    if _bot_dir not in _sys.path:
        _sys.path.insert(0, _bot_dir)
    import discord_embeds as _DE
except Exception:
    _DE = None

def _post(fn_name: str, **kwargs) -> None:
    """Best-effort Discord post. Never raises."""
    try:
        if _DE and hasattr(_DE, fn_name):
            getattr(_DE, fn_name)(**kwargs)
    except Exception as _e:
        pass


def _load_config() -> dict:
    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def _load_env() -> None:
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        logger.warning(".env not found at %s — relying on existing environment", env_path)
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _position_ids_for_instrument(positions: list, instrument_id) -> set[str]:
    """Return the positionIDs of all positions for *instrument_id*."""
    ids: set[str] = set()
    for pos in positions:
        pos_iid = pos.get("instrumentID") or pos.get("instrumentId")
        if pos_iid == instrument_id:
            pid = str(pos.get("positionID") or pos.get("positionId") or "")
            if pid:
                ids.add(pid)
    return ids


def _find_new_position(positions: list, instrument_id, pre_existing_ids: set[str]):
    """Find a position for *instrument_id* whose positionID was NOT present
    before the order was submitted.

    fix/ghost-order-id-diff: the old check matched ANY position with the
    right instrument_id. With pyramiding (multiple fragments per instrument
    allowed in NORMAL/CAUTION), the OLD fragment matched immediately — a
    ghost order was booked as FILLED with the old position's entry price.
    Comparing against the pre-submit snapshot only accepts a genuinely NEW
    position. Positions without a positionID are only accepted when the
    instrument had no positions before (conservative fallback).
    """
    for pos in positions:
        pos_iid = pos.get("instrumentID") or pos.get("instrumentId")
        if pos_iid != instrument_id:
            continue
        pid = str(pos.get("positionID") or pos.get("positionId") or "")
        if pid:
            if pid not in pre_existing_ids:
                return pos
        elif not pre_existing_ids:
            return pos
    return None


def main() -> None:
    # ── Worker lock: prevent overlapping cron invocations ────────────────────
    from bot.core.worker_lock import worker_lock

    with worker_lock("execution_worker") as acquired:
        if not acquired:
            logger.warning("ExecutionWorker: previous run still active — skipping this cycle")
            print("ExecutionWorker: SKIPPED (already running)")
            return

        # ── 1. Setup ──────────────────────────────────────────────────────────────
        _load_env()
        cfg = _load_config()
    
        from bot.api.client import APIError, ClientConfig, EToroClient
        from bot.core.market_hours import is_market_open, get_market_status
        from bot.core.risk import apply_config as _apply_risk_config
        from bot.db.connection import DB
        from bot.db.repo import LogRepo, StateRepo, TradeRepo
        _apply_risk_config(cfg)  # fix/risk-config-wiring
    
        db_path = PROJECT_ROOT / cfg["db"]["path"]
        busy_timeout = cfg["db"].get("busy_timeout_ms", 5000)
        db = DB(db_path=db_path, busy_timeout_ms=busy_timeout)
    
        trade_repo = TradeRepo(db)
        state_repo = StateRepo(db)
        log_repo = LogRepo(db)

        # ── Heartbeat (dead-man's switch) ─────────────────────────────────────────
        from bot.core.heartbeat import record_heartbeat
        record_heartbeat(state_repo, "execution_worker")

        # ── Kill Switch check (fix/autonomy-hardening) ────────────────────────────
        # Previously the execution worker relied ONLY on the DB regime, which
        # the risk worker sets with up to 5 minutes latency. In that window,
        # already-APPROVED trades would execute despite an active kill switch.
        # Check the flag file directly AND reject all pending approvals so
        # stale APPROVED trades cannot fire later once the switch is lifted.
        from bot.core.kill_switch import is_kill_switch_active, get_reason
        if is_kill_switch_active():
            ks_reason = get_reason() or "Manual kill switch"
            stale_approved = trade_repo.get_by_status("APPROVED")
            for t in stale_approved:
                trade_repo.update_status(
                    t["id"],
                    "REJECTED",
                    rejection_reason=f"Kill switch active: {ks_reason}",
                )
            logger.warning(
                "ExecutionWorker: KILL SWITCH ACTIVE (%s) — %d APPROVED trade(s) rejected, exiting",
                ks_reason, len(stale_approved),
            )
            log_repo.write(
                "WARN",
                "execution_worker",
                f"Kill switch active — {len(stale_approved)} APPROVED trade(s) rejected ({ks_reason})",
            )
            print(f"ExecutionWorker: KILL SWITCH — {len(stale_approved)} approvals rejected, no execution")
            return

        api_key = os.environ.get("ETORO_API_KEY", "")
        user_key = os.environ.get("ETORO_USER_KEY", "")
        client_cfg = ClientConfig.from_dict(cfg.get("api", {}))
        client = EToroClient(api_key=api_key, user_key=user_key, config=client_cfg)
    
        # ── 2. Fetch all APPROVED trades ──────────────────────────────────────────
        approved_trades = trade_repo.get_by_status("APPROVED")
    
        if not approved_trades:
            logger.info("ExecutionWorker: no APPROVED trades to process")
            print("ExecutionWorker: 0 trades processed, 0 filled, 0 failed")
            client.close()
            return
    
        logger.info("ExecutionWorker: found %d APPROVED trade(s)", len(approved_trades))
    
        # Also expire old signals (soft delete)
        from bot.db.repo import SignalRepo
        signal_repo = SignalRepo(db)
        expired_count = signal_repo.expire_old()
        if expired_count > 0:
            logger.info("ExecutionWorker: expired %d stale signals", expired_count)
    
        processed_count = 0
        filled_count = 0
        failed_count = 0
    
        # ── c) Duplicate-instrument guard ───────────────────────────────────────
        # get_by_status("APPROVED") can legitimately contain two different trade
        # rows for the same instrument_id (e.g. approved in two separate
        # signal_worker cycles before either got executed). Without a per-run
        # dedup, both would be submitted back-to-back, doubling exposure beyond
        # what sizing intended and increasing ghost-order surface area.
        # get_by_status() orders by created_at ASC, so the oldest (first
        # approved) trade per instrument executes; later duplicates are
        # rejected for this run.
        seen_instrument_ids: set = set()
    
        # ── 3. Process each APPROVED trade ────────────────────────────────────────
        for trade in approved_trades:
            trade_id = trade["id"]
            instrument_id = trade["instrument_id"]
            symbol = trade.get("symbol", str(instrument_id))
            amount_usd = float(trade.get("amount_usd", 0.0))
            stop_loss_pct = float(trade.get("stop_loss_pct") or cfg.get("sl", {}).get("default_pct", 3.0))
    
            if instrument_id in seen_instrument_ids:
                logger.warning(
                    "ExecutionWorker: trade #%d (%s) DUPLICATE instrument_id=%s "
                    "already processed this run — rejecting to avoid double exposure",
                    trade_id, symbol, instrument_id,
                )
                trade_repo.update_status(
                    trade_id,
                    "REJECTED",
                    rejection_reason=f"Duplicate instrument_id={instrument_id} in same execution batch",
                )
                log_repo.write(
                    "WARN",
                    "execution_worker",
                    f"Trade #{trade_id} REJECTED — duplicate instrument {symbol} in same batch",
                    {"symbol": symbol, "instrument_id": instrument_id},
                )
                continue
            seen_instrument_ids.add(instrument_id)
    
            # a. Atomic lock: APPROVED → SUBMITTING
            locked = trade_repo.lock_for_submission(trade_id)
            if not locked:
                logger.debug(
                    "ExecutionWorker: trade #%d already being processed — skipping", trade_id
                )
                continue
    
            processed_count += 1
            trade_repo.update_status(trade_id, "SUBMITTING", submitted_at=_utcnow())
    
            # b. Double-safety regime check (V5: NORMAL / CAUTION / DEFENSIVE / CRITICAL)
            regime = state_repo.get_regime()
            if regime in ("DEFENSIVE", "CRITICAL"):
                logger.warning(
                    "ExecutionWorker: %s regime at execution time — rejecting trade #%d (%s)",
                    regime, trade_id, symbol,
                )
                trade_repo.update_status(
                    trade_id,
                    "REJECTED",
                    rejection_reason=f"{regime} regime at execution time",
                )
                log_repo.write(
                    "WARN",
                    "execution_worker",
                    f"Trade #{trade_id} REJECTED — {regime} regime at execution time",
                    {"symbol": symbol, "amount_usd": amount_usd},
                )
                failed_count += 1
                continue
    
            # c. Market hours gate — prevent ghost orders on closed markets.
            #    fail_open=False: ein unbekannter/unmappbarer Markt gilt am
            #    BUY-Boundary als GESCHLOSSEN (fix/market-hours-fail-closed).
            if not is_market_open(symbol, fail_open=False):
                mkt_status = get_market_status(symbol)
                logger.warning(
                    'ExecutionWorker: %s market closed (%s) — skipping BUY to prevent ghost order',
                    symbol, mkt_status,
                )
                trade_repo.update_status(
                    trade_id,
                    'FAILED',
                    rejection_reason=f'Market closed: {mkt_status}',
                )
                log_repo.write(
                    'WARN',
                    'execution_worker',
                    f'Trade #{trade_id} FAILED — market closed: {mkt_status}',
                    {'symbol': symbol, 'amount_usd': amount_usd},
                )
                _post('post_trade_failed_embed',
                    symbol=symbol,
                    direction='BUY',
                    amount_usd=amount_usd,
                    error=f'Market closed: {mkt_status}',
                    dry_run=False,
                )
                failed_count += 1
                continue
    
            # c2. Slippage gate (fix/autonomy-hardening) — the signal price is
            #     stored at approval time but was previously never used. Block
            #     execution when the live price has drifted too far from it.
            from bot.core.risk import check_slippage_gate, get_max_slippage_pct
            signal_price = float(trade.get("signal_price") or 0.0)
            current_price = client.get_current_price(instrument_id)
            slippage_gate = check_slippage_gate(
                symbol=symbol,
                signal_price=signal_price if signal_price > 0 else None,
                current_price=current_price,
                max_slippage_pct=get_max_slippage_pct(symbol, cfg),
            )
            if not slippage_gate.allowed:
                reason = slippage_gate.summary()
                logger.warning(
                    "ExecutionWorker: trade #%d (%s) BLOCKED by slippage gate — %s",
                    trade_id, symbol, reason,
                )
                trade_repo.update_status(
                    trade_id,
                    "REJECTED",
                    rejection_reason=reason[:200],
                )
                log_repo.write(
                    "WARN",
                    "execution_worker",
                    f"Trade #{trade_id} REJECTED — slippage gate: {symbol}",
                    {
                        "symbol": symbol,
                        "signal_price": signal_price,
                        "current_price": current_price,
                        "reason": reason,
                    },
                )
                _post('post_trade_failed_embed',
                    symbol=symbol,
                    direction='BUY',
                    amount_usd=amount_usd,
                    error=f"Slippage gate: {reason[:150]}",
                    dry_run=False,
                )
                failed_count += 1
                continue

            # d0. Snapshot existing positionIDs for this instrument BEFORE
            #     submitting (fix/ghost-order-id-diff). Verification below
            #     only accepts a position whose ID is NOT in this set —
            #     an existing pyramiding fragment must never confirm a
            #     ghost order as FILLED.
            pre_existing_pos_ids: set[str] = set()
            try:
                _pre_positions = (
                    client.get_portfolio()
                    .get("clientPortfolio", {})
                    .get("positions", [])
                )
                pre_existing_pos_ids = _position_ids_for_instrument(
                    _pre_positions, instrument_id
                )
            except Exception as _pre_exc:
                # Fail-open: empty set = legacy behaviour (any position matches).
                logger.warning(
                    "ExecutionWorker: pre-submit portfolio snapshot failed (%s) — "
                    "falling back to any-position match for trade #%d",
                    _pre_exc, trade_id,
                )

            # d. Call eToro API to open the position
            try:
                result = client.open_position(
                    instrument_id=instrument_id,
                    amount_usd=amount_usd,
                    stop_loss_pct=stop_loss_pct,
                    symbol=symbol,
                )
    
                # d1. BLOCKED path — open_position() returned a soft-block dict
                #     (allowOpenPosition=false, market closed, SL gate, price=0, etc.)
                #     Fast-fail: skip polling entirely, mark FAILED with the specific reason.
                if result.get("success") is False:
                    block_error = result.get("error", "Unknown block reason")
                    logger.warning(
                        "ExecutionWorker: trade #%d (%s) BLOCKED by open_position(): %s",
                        trade_id, symbol, block_error,
                    )
                    trade_repo.update_status(
                        trade_id,
                        "FAILED",
                        rejection_reason=f"Blocked: {block_error}",
                    )
                    log_repo.write(
                        "WARN",
                        "execution_worker",
                        f"Trade #{trade_id} BLOCKED: {symbol} — {block_error}",
                        {"symbol": symbol, "block_reason": block_error},
                    )
                    _post('post_trade_failed_embed',
                        symbol=symbol,
                        direction='BUY',
                        amount_usd=amount_usd,
                        error=f"Pre-flight block: {block_error}",
                        dry_run=False,
                    )
                    failed_count += 1
                    continue
    
                # d2. Success — extract result fields
                # eToro v2 /trading/execution/orders returns ONLY the order ID on success.
                api_position_id = str(
                    result.get("positionId")
                    or result.get("position_id")
                    or result.get("orderId")      # v2 API returns orderId
                    or result.get("id")
                    or ""
                )
    
                # d3. Empty response guard — catches {} or unexpected API shape
                if not api_position_id:
                    logger.warning(
                        "ExecutionWorker: trade #%d (%s) open_position() returned no order ID (empty/unexpected response)",
                        trade_id, symbol,
                    )
    
                # Store order_id immediately so we can track it even if position verification fails
                if api_position_id:
                    trade_repo.update_status(
                        trade_id,
                        "SUBMITTING",
                        order_id=api_position_id,
                        submitted_at=_utcnow(),
                    )
    
                # e. CRITICAL: Verify the order actually materialized as a position
                # Use exponential backoff polling instead of fixed 5s sleep.
                # Futures and some crypto instruments need more time to process.
                import time as _time
    
                max_attempts = 6          # Check up to 6 times
                initial_wait_s = 3        # Start at 3 seconds
                max_total_wait_s = 120    # Cap total wait at 2 minutes
    
                matching_pos = None
                total_waited = 0
    
                for attempt in range(max_attempts):
                    wait_time = min(initial_wait_s * (2 ** attempt), 30)  # Cap individual wait at 30s
                    if total_waited + wait_time > max_total_wait_s:
                        logger.info(
                            "ExecutionWorker: trade #%d reached max total wait (%ds), final check",
                            trade_id, max_total_wait_s,
                        )
                        break
    
                    _time.sleep(wait_time)
                    total_waited += wait_time
    
                    portfolio = client.get_portfolio()
                    positions = portfolio.get("clientPortfolio", {}).get("positions", [])

                    # Check if a NEW position appeared for this instrument
                    # (positionID not in the pre-submit snapshot — an existing
                    # pyramiding fragment must not count as fill confirmation)
                    matching_pos = _find_new_position(
                        positions, instrument_id, pre_existing_pos_ids
                    )

                    if matching_pos:
                        logger.info(
                            "ExecutionWorker: trade #%d position verified after %.1fs (%d attempts)",
                            trade_id, total_waited, attempt + 1,
                        )
                        break
    
                    logger.debug(
                        "ExecutionWorker: trade #%d position not yet visible (attempt %d/%d, waited %.1fs)",
                        trade_id, attempt + 1, max_attempts, total_waited,
                    )
    
                if not matching_pos:
                    # No position found after polling — treat as ghost order regardless of orderId
                    # This catches both: (a) API returned orderId but no position, AND
                    # (b) API returned silently/empty but we got no exception (allowOpenPosition=false pattern)
                    ghost_count, bl_status = trade_repo.record_ghost_failure(instrument_id)
    
                    ghost_detail = f"orderId={api_position_id}" if api_position_id else "no orderId returned (silent block)"
                    logger.warning(
                        "ExecutionWorker: trade #%d (%s) GHOST ORDER (%s) — "
                        "failure #%d for this instrument (blacklist: %s)",
                        trade_id, symbol, ghost_detail, ghost_count, bl_status,
                    )
                    trade_repo.update_status(
                        trade_id,
                        "FAILED",
                        rejection_reason=(
                            f"Ghost order: {ghost_detail} but position "
                            f"never materialized (failure #{ghost_count}, blacklist: {bl_status})"
                        ),
                    )
                    log_repo.write(
                        "WARN",
                        "execution_worker",
                        f"Trade #{trade_id} GHOST ORDER: {symbol} {ghost_detail} no position created (#{ghost_count}, bl={bl_status})",
                        {"symbol": symbol, "api_position_id": api_position_id or "", "ghost_count": ghost_count, "blacklist_status": bl_status},
                    )
    
                    # c) Eskalierter Alert ab #5 — separates, lautereres Embed
                    if ghost_count >= 5:
                        _post(
                            'post_watchdog_alert_embed',
                            alert_type='GHOST_ORDER_ESCALATION',
                            symbol=symbol,
                            message=(
                                f"⚠️ {symbol}: {ghost_count}+ konsekutive Ghost-Failures — "
                                f"vermutlich strukturelles Problem, manuelle Prüfung nötig"
                            ),
                            severity='critical' if ghost_count >= 9 else 'high',
                            details=(
                                f"Blacklist: {bl_status} | "
                                f"Last orderId: {api_position_id or 'N/A'} | "
                                f"Instrument ID: {instrument_id}"
                            ),
                        )
    
                    _post('post_trade_failed_embed',
                        symbol=symbol,
                        direction='BUY',
                        amount_usd=amount_usd,
                        error=f"Ghost order: {ghost_detail} but no position created (#{ghost_count}, blacklist: {bl_status})",
                        dry_run=False,
                    )
                    failed_count += 1
                    continue
    
                # Position verified — update trade with real data
                entry_price = 0.0  # default; will be overwritten if matching_pos exists
                if matching_pos:
                    entry_price = float(matching_pos.get("openRate", 0) or 0.0)
                    api_position_id = str(matching_pos.get("positionID", matching_pos.get("positionId", api_position_id)))
                    # Reset ghost failure counter on success
                    trade_repo.reset_ghost_failures(instrument_id)
    
                trade_repo.update_status(
                    trade_id,
                    "ACTIVE",
                    api_position_id=api_position_id,
                    order_id=api_position_id,
                    entry_price=entry_price if entry_price else None,
                    confirmed_at=_utcnow(),
                )
                filled_count += 1
    
                fill_info = {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "instrument_id": instrument_id,
                    "amount_usd": amount_usd,
                    "entry_price": entry_price,
                    "api_position_id": api_position_id,
                    "stop_loss_pct": stop_loss_pct,
                }
    
                # Discord embed for filled trade
                _post('post_trade_filled_embed',
                    symbol=symbol,
                    direction='BUY',
                    amount_usd=amount_usd,
                    position_id=api_position_id,
                    entry_price=entry_price,
                    sl_pct=stop_loss_pct,
                    dry_run=False
                )
    
                log_repo.write(
                    "INFO",
                    "execution_worker",
                    f"Trade #{trade_id} ACTIVE: {symbol} BUY ${amount_usd:.2f} @ {entry_price}",
                    fill_info,
                )
                logger.info(
                    "ExecutionWorker: trade #%d FILLED — %s $%.2f @ %.6f",
                    trade_id, symbol, amount_usd, entry_price,
                )
    
            except APIError as exc:
                err_str = str(exc)
                logger.error(
                    "ExecutionWorker: API error on trade #%d (%s) — %s",
                    trade_id, symbol, exc,
                )
    
                # Classify error: DRAWDOWN/regime signal → REJECTED, otherwise FAILED
                if "DRAWDOWN" in err_str.upper() or "regime" in err_str.lower():
                    trade_repo.update_status(
                        trade_id,
                        "REJECTED",
                        rejection_reason=f"APIError (regime): {err_str[:200]}",
                    )
                    log_repo.write(
                        "WARN",
                        "execution_worker",
                        f"Trade #{trade_id} REJECTED via API regime error: {err_str[:200]}",
                        {"symbol": symbol, "status_code": exc.status_code},
                    )
                else:
                    trade_repo.update_status(
                        trade_id,
                        "FAILED",
                        rejection_reason=f"APIError: {err_str[:200]}",
                    )
                    log_repo.write(
                        "ERROR",
                        "execution_worker",
                        f"Trade #{trade_id} FAILED: {err_str[:200]}",
                        {"symbol": symbol, "status_code": exc.status_code},
                    )
                    # Discord embed for failed trade
                    _post('post_trade_failed_embed',
                        symbol=symbol,
                        direction='BUY',
                        amount_usd=amount_usd,
                        error=err_str[:200],
                        dry_run=False
                    )
                failed_count += 1
    
            except Exception as exc:
                logger.error(
                    "ExecutionWorker: Unexpected error on trade #%d (%s) — %s",
                    trade_id, symbol, exc,
                )
                trade_repo.update_status(
                    trade_id,
                    "FAILED",
                    rejection_reason=f"Unexpected error: {str(exc)[:200]}",
                )
                log_repo.write(
                    "ERROR",
                    "execution_worker",
                    f"Trade #{trade_id} FAILED (unexpected): {str(exc)[:200]}",
                    {"symbol": symbol},
                )
                # Discord embed for unexpected failure
                _post('post_trade_failed_embed',
                    symbol=symbol,
                    direction='BUY',
                    amount_usd=amount_usd,
                    error=str(exc)[:200],
                    dry_run=False
                )
                failed_count += 1
    
        # ── 4. Summary ────────────────────────────────────────────────────────────
        print(
            f"ExecutionWorker: {processed_count} trades processed, "
            f"{filled_count} filled, {failed_count} failed"
        )
        log_repo.write(
            "INFO",
            "execution_worker",
            f"Run complete: processed={processed_count} filled={filled_count} failed={failed_count}",
        )
    
        client.close()
    
    
if __name__ == "__main__":
    main()
