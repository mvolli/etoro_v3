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
    level=logging.WARNING,  # INFO→Embed via Discord; nur Warnings/Errors auf stdout
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
    # ── Worker lock: prevent overlapping cron invocations ────────────────────
    from bot.core.worker_lock import worker_lock

    with worker_lock("risk_worker") as acquired:
        if not acquired:
            logger.warning("RiskWorker: previous run still active — skipping this cycle")
            print("RiskWorker: SKIPPED (already running)")
            return

        # ── 1. Setup ──────────────────────────────────────────────────────────────
        _load_env()
        cfg = _load_config()
    
        from bot.api.client import APIError, ClientConfig, EToroClient
        from bot.core.regime import update_regime
        from bot.core.risk import apply_config, evaluate_sl
        from bot.core.regime import apply_config as apply_regime_config
        apply_config(cfg)  # fix/risk-config-wiring: SL-Schwellen/Limits aus config.yaml
        apply_regime_config(cfg)  # fix/regime-config-wiring: Drawdown-Regime-Schwellen
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

        # ── Heartbeat (dead-man's switch) ─────────────────────────────────────────
        from bot.core.heartbeat import record_heartbeat
        record_heartbeat(state_repo, "risk_worker")

        closed_count = 0
        checked_count = 0
        sl_warning_count = 0
        trailing_be_count = 0
        trailing_partial_count = 0
        trailing_error_list: list[str] = []
        sell_exit_closed = 0
        conc_closed = 0
        conc_warned = 0
        regime = "NORMAL"
        positions_summary: list[dict] = []
    
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
        # hotfix/risk-worker-position-id: the /trading/info/real/pnl payload
        # uses capital-ID field names (positionID, instrumentID) — the old
        # lowercase-first lookups returned None for EVERY position, so SL
        # closes were fired as close_position(None, ...) → HTTP 400.
        # Same extraction order as reconciler._build_snapshot_record().
        for pos in raw_positions:
            checked_count += 1
    
            position_id = (
                pos.get("positionID")
                or pos.get("positionId")
                or pos.get("id")
                or pos.get("position_id")
            )
            instrument_id = (
                pos.get("instrumentID")
                or pos.get("instrumentId")
                or pos.get("instrument_id")
            )
            symbol = pos.get("symbol") or ""
            if not symbol and instrument_id is not None:
                # Payload carries no symbol — resolve via portfolio_snapshot,
                # then instruments table (raw IDs in Discord/logs vermeiden)
                try:
                    _sym_row = db.fetchone(
                        "SELECT symbol FROM portfolio_snapshot "
                        "WHERE instrument_id = ? AND symbol IS NOT NULL "
                        "ORDER BY last_synced DESC LIMIT 1",
                        (int(instrument_id),),
                    )
                    if not _sym_row:
                        _sym_row = db.fetchone(
                            "SELECT symbol FROM instruments "
                            "WHERE instrument_id = ? AND symbol IS NOT NULL",
                            (int(instrument_id),),
                        )
                    if _sym_row:
                        symbol = _sym_row["symbol"]
                except Exception:
                    pass
            if not symbol:
                symbol = str(instrument_id)
    
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
    
            try:
                raw_pnl_pct = float(raw_pnl_pct)
            except (TypeError, ValueError):
                raw_pnl_pct = 0.0

            # fix/autonomy-hardening: the old "abs < 1.0 → ×100" heuristic was
            # a real false-positive path — a genuine −0.8% position became
            # −80% and was closed instantly. Primary source is now the
            # rate-derived PnL (openRate vs. live closeRate, long-only bot);
            # the raw API field is the fallback and is taken AS-IS (eToro
            # reports pnLPct in percent). Ambiguous small values are logged
            # instead of being silently rescaled.
            pnl_pct = raw_pnl_pct
            _open_rate = float(pos.get("openRate", 0) or 0)
            _close_rate = 0.0
            if isinstance(unrealized, dict):
                _close_rate = float(unrealized.get("closeRate", 0) or 0)
            if _close_rate <= 0:
                _close_rate = float(pos.get("closeRate", 0) or pos.get("currentRate", 0) or 0)

            if _open_rate > 0 and _close_rate > 0:
                rate_pnl_pct = (_close_rate / _open_rate - 1.0) * 100.0
                if raw_pnl_pct != 0.0 and abs(rate_pnl_pct - raw_pnl_pct) > 1.0:
                    logger.warning(
                        "RiskWorker: %s PnL-Diskrepanz — API=%.2f%% vs. ratenbasiert=%.2f%% "
                        "(nutze ratenbasiert)",
                        symbol, raw_pnl_pct, rate_pnl_pct,
                    )
                pnl_pct = rate_pnl_pct
            elif raw_pnl_pct != 0.0 and abs(raw_pnl_pct) < 1.0:
                logger.info(
                    "RiskWorker: %s PnL %.4f%% ist klein — wird als Prozentwert "
                    "interpretiert (keine ×100-Reskalierung mehr)",
                    symbol, raw_pnl_pct,
                )

            sl_action = evaluate_sl(pnl_pct)
    
            if sl_action.action == "CLOSE":
                # hotfix/risk-worker-position-id: never send a close order
                # without a valid position id — the API rejects 'None' with
                # HTTP 400 and the position stays open while looking handled.
                if not position_id or str(position_id).lower() == "none":
                    logger.error(
                        "RiskWorker: SL CLOSE für %s NICHT ausführbar — position_id fehlt "
                        "im API-Payload (Felder: %s)",
                        symbol, sorted(pos.keys()),
                    )
                    log_repo.write(
                        "ERROR",
                        "risk_worker",
                        f"SL CLOSE blockiert: {symbol} ohne position_id (pnl={pnl_pct:.2f}%)",
                        {"payload_keys": sorted(pos.keys()), "pnl_pct": pnl_pct},
                    )
                    _discord(
                        "post_alert_embed",
                        title="🔴 RiskWorker: SL-Close ohne position_id",
                        description=(
                            f"{symbol}: SL bei {pnl_pct:.2f}% ausgelöst, aber API-Payload "
                            f"enthält keine positionID — Position bleibt offen, manuelle Prüfung!"
                        ),
                        severity="CRITICAL",
                    )
                    continue

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
    
                    # ── Verify the full-close actually took effect ──────────────
                    from bot.core.trailing_stop import verify_full_close
                    verified, detail = verify_full_close(client, int(instrument_id or 0), str(position_id))
                    if verified:
                        closed_count += 1
                        logger.warning("RiskWorker: %s", detail)
                        # Remove from local portfolio snapshot
                        db.execute(
                            "DELETE FROM portfolio_snapshot WHERE api_position_id = ?",
                            (str(position_id),),
                        )
                        logger.warning(
                            "RiskWorker: Closed position %s (%s) — pnl=%.2f%%",
                            position_id, symbol, pnl_pct,
                        )
                        # ── Discord: CLOSE Embed → #etoro-trades ────────────────
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
                    else:
                        logger.error("RiskWorker: SL-Close NOT verified — %s", detail)
                        log_repo.write(
                            "ERROR",
                            "risk_worker",
                            f"SL-Close unverified: {symbol} ({position_id}) — {detail}",
                        )
                        _discord(
                            "post_alert_embed",
                            title="🔴 SL-Close unverifiziert",
                            description=f"{symbol}: {detail}",
                            severity="CRITICAL",
                        )
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
                sl_warning_count += 1
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

            # ── Collect position summary for embed ────────────────────────────
            positions_summary.append({
                "symbol": symbol,
                "pnl_pct": pnl_pct,
                "amount_usd": float(pos.get("amount", 0)),
                "trailing_status": sl_action.reason if sl_action.action in ("WARNING", "CLOSE") else "",
            })

        # ── 4. Kill Switch check (V5) — BEFORE regime detection ──────────────────
        from bot.core.kill_switch import is_kill_switch_active, KILL_SWITCH_FILE
        if is_kill_switch_active():
            _ks_reason = KILL_SWITCH_FILE.read_text().strip() if KILL_SWITCH_FILE.exists() else 'Manual kill switch'
            logger.warning('RiskWorker: KILL SWITCH ACTIVE — forcing CRITICAL regime (%s)', _ks_reason)
            # fix/autonomy-hardening: capture the regime BEFORE overwriting it,
            # otherwise the embed always showed CRITICAL→CRITICAL.
            _old_regime = state_repo.get_regime() or 'UNKNOWN'
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
                    old_regime=_old_regime,
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
            # Fall back to portfolio equity from the API response if available.
            # fix/autonomy-hardening: NO fabricated $10,000 default anymore —
            # if equity is genuinely unknown, we skip regime/drawdown math
            # (fail-closed) instead of computing it on a fantasy number.
            equity = float(
                portfolio.get("equity")
                or portfolio.get("totalEquity")
                or portfolio.get("netEquity")
                or 0.0
            )
            if equity > 0.0:
                state_repo.set("CURRENT_EQUITY", str(equity))
            else:
                logger.error(
                    "RiskWorker: Equity unbekannt (State leer, API-Payload ohne Equity) "
                    "— Regime-Update übersprungen (fail-closed)"
                )
                log_repo.write("ERROR", "risk_worker",
                               "Equity unbekannt — Regime-Update übersprungen (fail-closed)")

        # ── fix/autonomy-hardening: Daily-Loss Auto-Kill-Switch ───────────────────
        # Regime scaling only reduces sizing; nothing previously STOPPED the
        # bot automatically. Track day-start equity (UTC) and trip the kill
        # switch when the intraday drop exceeds risk.daily_loss_limit_pct.
        if equity > 0.0:
            from datetime import datetime as _dt, timezone as _tz
            from bot.core.risk import (
                check_daily_loss_breach,
                check_trailing_loss_breach,
                DAILY_LOSS_LIMIT_PCT_DEFAULT,
                WEEKLY_LOSS_LIMIT_PCT_DEFAULT,
                MONTHLY_LOSS_LIMIT_PCT_DEFAULT,
            )
            from bot.core.regime import get_rolling_peak
            from bot.core import kill_switch as _ks

            _today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
            _day_date = state_repo.get("DAY_START_DATE")
            if _day_date != _today:
                state_repo.set("DAY_START_DATE", _today)
                state_repo.set("DAY_START_EQUITY", str(equity))
                logger.info("RiskWorker: neuer Handelstag %s — DAY_START_EQUITY=%.2f", _today, equity)

            _risk_cfg = cfg.get("risk", {})
            _day_start_equity = state_repo.get_float("DAY_START_EQUITY", 0.0)

            # Evaluate all three horizons. Weekly/monthly use the trailing
            # equity high (7d / 30d) from equity_history via get_rolling_peak,
            # so they measure max drawdown over the window, not intraday.
            # fix/multi-horizon-loss-limits.
            _daily_limit = float(_risk_cfg.get("daily_loss_limit_pct", DAILY_LOSS_LIMIT_PCT_DEFAULT))
            _weekly_limit = float(_risk_cfg.get("weekly_loss_limit_pct", WEEKLY_LOSS_LIMIT_PCT_DEFAULT))
            _monthly_limit = float(_risk_cfg.get("monthly_loss_limit_pct", MONTHLY_LOSS_LIMIT_PCT_DEFAULT))

            _breaches: list[str] = []

            _d_breached, _day_pnl_pct = check_daily_loss_breach(
                _day_start_equity, equity, _daily_limit
            )
            if _d_breached:
                _breaches.append(
                    f"Tagesverlust {_day_pnl_pct:.2f}% > -{_daily_limit:.1f}% "
                    f"(Start ${_day_start_equity:.2f} → ${equity:.2f})"
                )

            try:
                _week_peak = get_rolling_peak(state_repo.db, equity, days=7)
                _month_peak = get_rolling_peak(state_repo.db, equity, days=30)
            except Exception as _peak_exc:
                logger.warning("RiskWorker: get_rolling_peak fehlgeschlagen (%s) — "
                               "Wochen/Monats-Check übersprungen", _peak_exc)
                _week_peak = _month_peak = 0.0

            _w_breached, _week_dd = check_trailing_loss_breach(_week_peak, equity, _weekly_limit)
            if _w_breached:
                _breaches.append(
                    f"Wochenverlust {_week_dd:.2f}% > -{_weekly_limit:.1f}% "
                    f"(7-Tage-Hoch ${_week_peak:.2f} → ${equity:.2f})"
                )

            _m_breached, _month_dd = check_trailing_loss_breach(_month_peak, equity, _monthly_limit)
            if _m_breached:
                _breaches.append(
                    f"Monatsverlust {_month_dd:.2f}% > -{_monthly_limit:.1f}% "
                    f"(30-Tage-Hoch ${_month_peak:.2f} → ${equity:.2f})"
                )

            if _breaches and not is_kill_switch_active():
                _reason = "AUTO: " + " | ".join(_breaches)
                logger.critical("RiskWorker: %s — Kill Switch wird aktiviert", _reason)
                _ks.activate(_reason)
                state_repo.set_regime("CRITICAL")
                state_repo.set("RISK_SCALAR", "0.25")
                log_repo.write("CRITICAL", "risk_worker", f"Auto-Kill-Switch: {_reason}")
                _discord(
                    "post_alert_embed",
                    title="🛑 AUTO-KILL-SWITCH ausgelöst",
                    description=(
                        f"{_reason}\n"
                        f"Alle neuen Trades gestoppt. Reaktivierung: "
                        f"`rm data/kill_switch.flag` nach manueller Prüfung."
                    ),
                    severity="CRITICAL",
                )
                regime = "CRITICAL"

        if equity > 0.0 and not is_kill_switch_active():
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
            # Kill switch forces CRITICAL — do not allow update_regime() to
            # overwrite. If we got here because equity is unknown (fail-closed
            # skip), keep the last known regime instead of forcing CRITICAL.
            if is_kill_switch_active():
                regime = 'CRITICAL'
            else:
                regime = state_repo.get_regime() or 'NORMAL'
            regime_changed = False
    
        # ── P3 V5: Post-Trade Concentration Monitoring ────────────────────────────
        try:
            from bot.core.concentration_monitor import (
                check_concentration_violations,
                close_concentration_excess,
                check_asset_class_violations,
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
                conc_closed += conc_stats["closed"]
                conc_warned += conc_stats["warned"]
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

            # fix/asset-class-concentration (audit H7): detect asset-class
            # drift post-trade (price appreciation past a sector cap). Warn
            # only — no auto-close; surfaces for a human rebalance decision.
            ac_violations = check_asset_class_violations(raw_positions, equity, instrument_map)
            for _acv in ac_violations:
                _acmsg = (
                    f"{_acv['asset_class']} at {_acv['actual_pct']:.1f}% "
                    f"(limit {_acv['limit_pct']:.0f}%, {_acv['breach_pct']:.1f}% over) — "
                    f"{', '.join(_acv['symbols'])}"
                )
                logger.warning("RiskWorker: asset-class concentration drift — %s", _acmsg)
                log_repo.write("WARN", "risk_worker",
                               f"Asset-Klassen-Konzentration: {_acmsg}", _acv)
                _discord(
                    "post_alert_embed",
                    title="⚠️ Asset-Klassen-Konzentration über Limit",
                    description=(
                        f"{_acmsg}\n\nKein Auto-Close — Rebalancing ist eine "
                        f"manuelle Entscheidung. Prüfen, ob Position(en) getrimmt werden sollen."
                    ),
                    severity="WARNING",
                )
        except Exception as _conc_exc:
            logger.debug("RiskWorker: Concentration check skipped: %s", _conc_exc)
    
        # ── V5: Trailing Stop / Profit-Taking ─────────────────────────────────────
        try:
            from bot.core.trailing_stop import (
                apply_config as apply_trailing_config,
                cleanup_position_state,
                evaluate_trailing,
                execute_trailing_actions,
            )
            apply_trailing_config(cfg)  # wire trailing.momentum_fade / trailing.scalp

            # Stale State-Zeilen geschlossener Positionen entsorgen
            _live_ids = {
                str(p.get("positionID") or p.get("positionId") or "")
                for p in raw_positions
            }
            cleanup_position_state(db, _live_ids)

            trailing_actions = evaluate_trailing(raw_positions, regime=regime, db=db)
            if trailing_actions:
                ts_stats = execute_trailing_actions(client, trailing_actions, regime=regime, db=db)
                trailing_be_count += ts_stats.get('break_evens', 0)
                trailing_partial_count += ts_stats['partial_closes']
                if ts_stats['partial_closes'] > 0 or ts_stats.get('be_closes', 0) > 0:
                    logger.info('RiskWorker: Trailing Stop: %d partial closes (%d momentum-fades), %d BE-closes, %d break-evens',
                               ts_stats['partial_closes'], ts_stats.get('momentum_fades', 0),
                               ts_stats.get('be_closes', 0), ts_stats['break_evens'])
                    closed_count += ts_stats.get('be_closes', 0)
                if ts_stats.get('errors'):
                    for err in ts_stats['errors']:
                        trailing_error_list.append(str(err))
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

        # ── SELL-Signal-Exits (Bible V4 SELL Rule 1) ──────────────────────────────
        # fix/sell-signal-exits: FRESH SELL/OVERBOUGHT-Signale auf gehaltene
        # Instrumente → Partial-Close (Gewinnmitnahme bei Überhitzung).
        # Vorher wurden SELL-Signale generiert und gespeichert, aber von
        # keinem Worker konsumiert.
        try:
            from bot.core.sell_exits import process_sell_exits
            from bot.db.repo import SignalRepo as _SignalRepo
            sell_stats = process_sell_exits(client, _SignalRepo(db), raw_positions)
            if sell_stats['closed'] > 0:
                sell_exit_closed += sell_stats['closed']
                closed_count += sell_stats['closed']
                logger.info('RiskWorker: SELL-Exits: %d Partial-Close(s) ausgeführt',
                            sell_stats['closed'])
                log_repo.write('INFO', 'risk_worker',
                               f"SELL-Exits: {sell_stats['closed']} Partial-Close(s)")
            if sell_stats.get('errors'):
                for err in sell_stats['errors']:
                    logger.warning('RiskWorker: SELL-Exit error: %s', err)
                log_repo.write('WARN', 'risk_worker',
                               f"SELL-Exits: {len(sell_stats['errors'])} Fehler",
                               {'errors': sell_stats['errors']})
        except Exception as _se_exc:
            logger.error('RiskWorker: SELL-Exits failed: %s', _se_exc)
            log_repo.write('ERROR', 'risk_worker', f'SELL-Exits crashed: {_se_exc}')

        # ── 5. Summary + Discord Embed ────────────────────────────────────────────
        logger.warning("RiskWorker: checked %d positions, closed %d, regime=%s", checked_count, closed_count, regime)
        log_repo.write(
            "INFO",
            "risk_worker",
            f"Run complete: checked={checked_count} closed={closed_count} regime={regime}",
        )

        # ── Post Risk Worker Embed → #etoro-trading ──────────────────────────────
        _ks_active = is_kill_switch_active()
        try:
            _discord(
                "post_risk_worker_embed",
                checked=checked_count,
                closed=closed_count,
                regime=regime,
                equity=equity,
                trailing_break_evens=trailing_be_count,
                trailing_partials=trailing_partial_count,
                trailing_errors=trailing_error_list,
                sl_warnings=sl_warning_count,
                sell_exits_closed=sell_exit_closed,
                concentration_closed=conc_closed,
                concentration_warned=conc_warned,
                kill_switch_active=_ks_active,
                positions_summary=positions_summary,
            )
        except Exception as _emb_exc:
            logger.debug("RiskWorker: Discord embed failed: %s", _emb_exc)
    
        client.close()


if __name__ == "__main__":
    main()
