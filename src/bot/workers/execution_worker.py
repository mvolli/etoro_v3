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


def main() -> None:
    # ── 1. Setup ──────────────────────────────────────────────────────────────
    _load_env()
    cfg = _load_config()

    from bot.api.client import APIError, ClientConfig, EToroClient
    from bot.core.market_hours import is_market_open, get_market_status
    from bot.db.connection import DB
    from bot.db.repo import LogRepo, StateRepo, TradeRepo

    db_path = PROJECT_ROOT / cfg["db"]["path"]
    busy_timeout = cfg["db"].get("busy_timeout_ms", 5000)
    db = DB(db_path=db_path, busy_timeout_ms=busy_timeout)

    api_key = os.environ.get("ETORO_API_KEY", "")
    user_key = os.environ.get("ETORO_USER_KEY", "")
    client_cfg = ClientConfig.from_dict(cfg.get("api", {}))
    client = EToroClient(api_key=api_key, user_key=user_key, config=client_cfg)

    trade_repo = TradeRepo(db)
    state_repo = StateRepo(db)
    log_repo = LogRepo(db)

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

    # ── 3. Process each APPROVED trade ────────────────────────────────────────
    for trade in approved_trades:
        trade_id = trade["id"]
        instrument_id = trade["instrument_id"]
        symbol = trade.get("symbol", str(instrument_id))
        amount_usd = float(trade.get("amount_usd", 0.0))
        stop_loss_pct = float(trade.get("stop_loss_pct") or cfg.get("sl", {}).get("default_pct", 3.0))

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

        # c. Market hours gate — prevent ghost orders on closed markets
        if not is_market_open(symbol):
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

        # d. Call eToro API to open the position
        try:
            result = client.open_position(
                instrument_id=instrument_id,
                amount_usd=amount_usd,
                stop_loss_pct=stop_loss_pct,
            )

            # d. Success — extract result fields
            # eToro v2 /trading/execution/orders returns ONLY the order ID on success.
            api_position_id = str(
                result.get("positionId")
                or result.get("position_id")
                or result.get("orderId")      # v2 API returns orderId
                or result.get("id")
                or ""
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
            # Some instruments (e.g. crypto futures) return orderId but never create
            # a position — the order stays "pending" or gets silently cancelled.
            import time as _time
            _time.sleep(5)  # Give eToro time to process
            portfolio = client.get_portfolio()
            positions = portfolio.get("clientPortfolio", {}).get("positions", [])

            # Check if a new position appeared for this instrument
            matching_pos = None
            for pos in positions:
                pos_iid = pos.get("instrumentID") or pos.get("instrumentId")
                if pos_iid == instrument_id:
                    matching_pos = pos
                    break

            if not matching_pos and api_position_id:
                # Ghost order: accepted but no position — record for blacklist tracking
                trade_repo.record_ghost_failure(instrument_id)
                ghost_count = trade_repo.get_ghost_failure_count(instrument_id)

                logger.warning(
                    "ExecutionWorker: trade #%d (%s) GHOST ORDER (orderId=%s) — "
                    "failure #%d for this instrument",
                    trade_id, symbol, api_position_id, ghost_count,
                )
                trade_repo.update_status(
                    trade_id,
                    "FAILED",
                    rejection_reason=(
                        f"Ghost order: orderId={api_position_id} but position "
                        f"never materialized (failure #{ghost_count})"
                    ),
                )
                log_repo.write(
                    "WARN",
                    "execution_worker",
                    f"Trade #{trade_id} GHOST ORDER: {symbol} orderId={api_position_id} no position created (#{ghost_count})",
                    {"symbol": symbol, "api_position_id": api_position_id, "ghost_count": ghost_count},
                )
                _post('post_trade_failed_embed',
                    symbol=symbol,
                    direction='BUY',
                    amount_usd=amount_usd,
                    error=f"Ghost order: orderId={api_position_id} but no position created (#{ghost_count})",
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
