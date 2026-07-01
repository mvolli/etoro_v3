#!/usr/bin/env python3
"""eToro Trading Bot V3 — Risk Worker
src/bot/workers/risk_worker.py

Runs every 5 minutes at :01.
Enforces stop-loss rules on live positions and updates regime.

Schedule: */5 * * * * cd /path/to/etoro_v3 && python3 -m bot.workers.risk_worker
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml

# ── Path setup ────────────────────────────────────────────────────────────────
# Allow running directly or via -m from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
# src/ is inside PROJECT_ROOT
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

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

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("risk_worker")


def _load_config() -> dict:
    """Load config/config.yaml relative to project root."""
    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def _load_env() -> None:
    """Load API keys from ~/.hermes/.env into os.environ."""
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


def main() -> None:
    # ── 1. Setup ──────────────────────────────────────────────────────────────
    _load_env()
    cfg = _load_config()

    from bot.api.client import APIError, ClientConfig, EToroClient
    from bot.core.regime import update_regime
    from bot.core.risk import evaluate_sl
    from bot.db.connection import DB
    from bot.db.repo import LogRepo, PortfolioRepo, StateRepo

    db_path = PROJECT_ROOT / cfg["db"]["path"]
    busy_timeout = cfg["db"].get("busy_timeout_ms", 5000)
    db = DB(db_path=db_path, busy_timeout_ms=busy_timeout)

    api_key = os.environ.get("ETORO_API_KEY", "")
    user_key = os.environ.get("ETORO_USER_KEY", "")
    client_cfg = ClientConfig.from_dict(cfg.get("api", {}))
    client = EToroClient(api_key=api_key, user_key=user_key, config=client_cfg)

    portfolio_repo = PortfolioRepo(db)
    state_repo = StateRepo(db)
    log_repo = LogRepo(db)

    closed_count = 0
    checked_count = 0
    regime = "NORMAL"

    # ── 2. Fetch live positions from eToro ────────────────────────────────────
    try:
        portfolio = client.get_portfolio()
    except APIError as exc:
        logger.error("RiskWorker: API failure fetching portfolio — %s", exc)
        log_repo.write("ERROR", "risk_worker", f"API failure: {exc}")
        sys.exit(1)
    except Exception as exc:
        logger.error("RiskWorker: Unexpected error fetching portfolio — %s", exc)
        log_repo.write("ERROR", "risk_worker", f"Unexpected error: {exc}")
        sys.exit(1)

    # eToro nests positions under clientPortfolio.positions
    client_portfolio = portfolio.get("clientPortfolio", {})
    raw_positions: list[dict] = (
        client_portfolio.get("positions")
        or client_portfolio.get("openPositions")
        or portfolio.get("positions")   # fallback if already unwrapped
        or []
    )

    # ── 3. Evaluate stop-loss for each position ────────────────────────────────
    for pos in raw_positions:
        checked_count += 1

        position_id = pos.get("positionId") or pos.get("id") or pos.get("position_id")
        instrument_id = pos.get("instrumentId") or pos.get("instrument_id")
        symbol = pos.get("symbol") or str(instrument_id)

        # Extract pnl_pct: unrealizedPnL.pnLPct or flat pnLPct
        unrealized = pos.get("unrealizedPnL") or {}
        if isinstance(unrealized, dict):
            raw_pnl_pct = unrealized.get("pnLPct") or unrealized.get("pnlPct") or 0.0
        else:
            raw_pnl_pct = float(unrealized) if unrealized else 0.0

        # Fallback: flat field on position
        if raw_pnl_pct == 0.0:
            raw_pnl_pct = (
                pos.get("pnLPct")
                or pos.get("pnlPct")
                or pos.get("unrealized_pnl_pct")
                or 0.0
            )

        # Normalise: if absolute value < 1.0, it's a decimal fraction → multiply by 100
        try:
            raw_pnl_pct = float(raw_pnl_pct)
        except (TypeError, ValueError):
            raw_pnl_pct = 0.0

        if abs(raw_pnl_pct) < 1.0 and raw_pnl_pct != 0.0:
            pnl_pct = raw_pnl_pct * 100.0
        else:
            pnl_pct = raw_pnl_pct

        sl_action = evaluate_sl(pnl_pct)

        if sl_action.action == "CLOSE":
            logger.warning(
                "RiskWorker: SL CLOSE triggered for %s (pos=%s) — %s",
                symbol, position_id, sl_action.reason,
            )
            log_repo.write(
                "WARN",
                "risk_worker",
                f"SL CLOSE: {symbol} position {position_id}",
                {"reason": sl_action.reason, "pnl_pct": pnl_pct},
            )

            try:
                client.close_position(position_id, instrument_id)
                # Remove from local portfolio snapshot
                db.execute(
                    "DELETE FROM portfolio_snapshot WHERE api_position_id = ?",
                    (str(position_id),),
                )
                closed_count += 1
                logger.warning(
                    "RiskWorker: Closed position %s (%s) — pnl=%.2f%%",
                    position_id, symbol, pnl_pct,
                )
                # ── Discord: CLOSE Embed → #etoro-trades ─────────────────
                try:
                    upnl = pos.get("unrealizedPnL") or {}
                    _discord(
                        "post_position_closed_embed",
                        symbol=symbol,
                        amount_usd=float(pos.get("amount", 0)),
                        position_id=str(position_id),
                        entry_price=float(pos.get("openRate", 0)),
                        close_price=float(upnl.get("closeRate", 0)),
                        pnl_usd=float(upnl.get("pnL", 0)),
                        pnl_pct=pnl_pct,
                        reason=sl_action.reason,
                    )
                except Exception as _emb_exc:
                    logger.debug("Discord close embed failed: %s", _emb_exc)
            except APIError as exc:
                logger.error(
                    "RiskWorker: Failed to close position %s — %s", position_id, exc
                )
                log_repo.write(
                    "ERROR",
                    "risk_worker",
                    f"Failed to close position {position_id} ({symbol}): {exc}",
                )

        elif sl_action.action == "WARNING":
            logger.info(
                "RiskWorker: SL WARNING for %s (pos=%s) — %s",
                symbol, position_id, sl_action.reason,
            )
            log_repo.write(
                "INFO",
                "risk_worker",
                f"SL WARNING: {symbol} position {position_id}",
                {"reason": sl_action.reason, "pnl_pct": pnl_pct},
            )

    # ── 4. Kill Switch check (V5) — BEFORE regime detection ──────────────────
    from bot.core.kill_switch import is_kill_switch_active, KILL_SWITCH_FILE
    if is_kill_switch_active():
        _ks_reason = KILL_SWITCH_FILE.read_text().strip() if KILL_SWITCH_FILE.exists() else 'Manual kill switch'
        logger.warning('RiskWorker: KILL SWITCH ACTIVE — forcing CRITICAL regime (%s)', _ks_reason)
        state_repo.set_regime('CRITICAL')
        state_repo.set('RISK_SCALAR', '0.25')
        log_repo.write('WARNING', 'kill_switch', f'Kill switch active: {_ks_reason}')
        # Post Discord alert (best-effort)
        # Try kill switch embed first; fall back to regime change embed
        if _DE and hasattr(_DE, 'post_kill_switch_embed'):
            _discord('post_kill_switch_embed', reason=_ks_reason)
        else:
            _discord(
                'post_regime_change_embed',
                old_regime=state_repo.get_regime() or 'UNKNOWN',
                new_regime='CRITICAL',
                drawdown_pct=0.0,
                equity=state_repo.get_equity() or 0.0,
                peak_equity=state_repo.get_equity() or 0.0,
                reason=f'🔴 KILL SWITCH AKTIV: {_ks_reason}',
            )
        print(f'RiskWorker: KILL SWITCH — CRITICAL regime forced ({_ks_reason})')
        regime = 'CRITICAL'
        # Fall through: still run SL checks on existing positions (already done above)

    # ── 5. Update regime (skipped if kill switch forced CRITICAL) ─────────────
    equity = state_repo.get_equity()
    if equity <= 0.0:
        # Fall back to portfolio equity from API response if available
        equity = float(
            portfolio.get("equity")
            or portfolio.get("totalEquity")
            or portfolio.get("netEquity")
            or 10_000.0
        )
        if equity > 0.0:
            state_repo.set("CURRENT_EQUITY", str(equity))

    if not is_kill_switch_active():
        previous_regime = state_repo.get_regime()
        regime, regime_changed = update_regime(state_repo, equity)

        if regime_changed:
            logger.info("RiskWorker: Regime changed → %s (equity=%.2f)", regime, equity)
            log_repo.write(
                "INFO",
                "risk_worker",
                f"Regime change → {regime}",
                {"equity": equity},
            )
            _discord(
                'post_regime_change_embed',
                old_regime=previous_regime or 'UNKNOWN',
                new_regime=regime,
                drawdown_pct=float(state_repo.get("DRAWDOWN_PCT") or 0.0),
                reason=state_repo.get("DRAWDOWN_REASON") or f"Regime changed to {regime}",
            )
    else:
        # Kill switch forces CRITICAL — do not allow update_regime() to overwrite
        regime = 'CRITICAL'
        regime_changed = False

    # ── P3 V5: Post-Trade Concentration Monitoring ────────────────────────────
    try:
        from bot.core.concentration_monitor import (
            check_concentration_violations,
            close_concentration_excess,
        )
        # Load instrument map for symbol resolution
        instrument_map: dict = {}
        try:
            import json
            map_path = PROJECT_ROOT / "data" / "instrument_map.json"
            if map_path.exists():
                raw = json.loads(map_path.read_text())
                data = raw.get("map", raw)
                instrument_map = {
                    int(k): v for k, v in data.items()
                    if not k.startswith("_") and str(k).isdigit()
                }
        except Exception:
            pass

        violations = check_concentration_violations(raw_positions, equity, instrument_map)
        if violations:
            conc_stats = close_concentration_excess(client, violations)
            if conc_stats["closed"] > 0:
                closed_count += conc_stats["closed"]
                logger.warning(
                    "RiskWorker: Concentration violations fixed: %d closed, %d warned",
                    conc_stats["closed"], conc_stats["warned"],
                )
            elif conc_stats["warned"] > 0:
                logger.info(
                    "RiskWorker: %d concentration warnings (below immediate threshold)",
                    conc_stats["warned"],
                )
    except Exception as _conc_exc:
        logger.debug("RiskWorker: Concentration check skipped: %s", _conc_exc)

    # ── V5: Trailing Stop / Profit-Taking ─────────────────────────────────────
    try:
        from bot.core.trailing_stop import evaluate_trailing, execute_trailing_actions
        trailing_actions = evaluate_trailing(raw_positions, regime=regime)
        if trailing_actions:
            ts_stats = execute_trailing_actions(client, trailing_actions, regime=regime)
            if ts_stats['partial_closes'] > 0:
                logger.info('RiskWorker: Trailing Stop: %d partial closes, %d break-evens',
                           ts_stats['partial_closes'], ts_stats['break_evens'])
            if ts_stats.get('errors'):
                for err in ts_stats['errors']:
                    logger.warning('RiskWorker: Trailing Stop error: %s', err)
                log_repo.write(
                    'WARN',
                    'risk_worker',
                    f"Trailing Stop: {len(ts_stats['errors'])} partial-close error(s)",
                    {'errors': ts_stats['errors']},
                )
                _discord(
                    'post_alert_embed',
                    title=f"🟠 Trailing Stop: {len(ts_stats['errors'])} Fehler",
                    description=(
                        f"Gewinne wurden NICHT teilweise realisiert.\n"
                        + "\n".join(f'• {e}' for e in ts_stats['errors'][:5])
                    ),
                    severity='WARNING',
                    dry_run=False,
                )
    except Exception as _ts_exc:
        logger.error('RiskWorker: Trailing stop failed: %s', _ts_exc)
        log_repo.write('ERROR', 'risk_worker', f'Trailing stop crashed: {_ts_exc}')

    # ── 5. Summary ────────────────────────────────────────────────────────────
    print(f"RiskWorker: checked {checked_count} positions, closed {closed_count}, regime={regime}")
    log_repo.write(
        "INFO",
        "risk_worker",
        f"Run complete: checked={checked_count} closed={closed_count} regime={regime}",
    )

    client.close()


if __name__ == "__main__":
    main()
