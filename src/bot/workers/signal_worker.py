#!/usr/bin/env python3
"""eToro Trading Bot V3 — Signal Worker
src/bot/workers/signal_worker.py

Runs every 15 minutes at :03.
Reads fresh signals, applies all risk gates, and creates APPROVED trades.

Schedule: 3,18,33,48 * * * * cd /path/to/etoro_v3 && python3 -m bot.workers.signal_worker
"""
from __future__ import annotations

import logging
import os
import sys
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
logger = logging.getLogger("signal_worker")

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


def main() -> None:
    # ── Worker lock: prevent overlapping cron invocations ────────────────────
    from bot.core.worker_lock import worker_lock

    with worker_lock("signal_worker") as acquired:
        if not acquired:
            logger.warning("SignalWorker: previous run still active — skipping this cycle")
            print("SignalWorker: SKIPPED (already running)")
            return

        # ── 1. Setup ──────────────────────────────────────────────────────────────
        _load_env()
        cfg = _load_config()
    
        # ── Kill Switch check (V5) — abort immediately if active ──────────────────
        from bot.core.kill_switch import is_kill_switch_active, KILL_SWITCH_FILE
        if is_kill_switch_active():
            _ks_reason = KILL_SWITCH_FILE.read_text().strip() if KILL_SWITCH_FILE.exists() else 'Manual kill switch'
            print(f'SignalWorker: KILL SWITCH ACTIVE — no signals generated ({_ks_reason})')
            logger.warning('SignalWorker: KILL SWITCH ACTIVE — exiting without generating signals (%s)', _ks_reason)
            sys.exit(0)
    
        from bot.core.market_hours import is_market_open
        from bot.core.regime import get_regime_params
        from bot.core.risk import check_buy_gate, get_score_boost
        from bot.db.connection import DB
        from bot.db.repo import LogRepo, PortfolioRepo, SignalRepo, StateRepo, TradeRepo
    
        db_path = PROJECT_ROOT / cfg["db"]["path"]
        busy_timeout = cfg["db"].get("busy_timeout_ms", 5000)
        db = DB(db_path=db_path, busy_timeout_ms=busy_timeout)
    
        trade_repo = TradeRepo(db)
        signal_repo = SignalRepo(db)
        portfolio_repo = PortfolioRepo(db)
        state_repo = StateRepo(db)
        log_repo = LogRepo(db)
    
        # ── 2. Check regime — V5: 4-level system, no hard block except legacy ────
        regime = state_repo.get_regime()
        from bot.core.regime import get_regime_params, get_risk_scalar, get_min_conviction
    
        regime_params = get_regime_params(regime)
        risk_scalar = get_risk_scalar(regime)
        min_conviction_for_regime = get_min_conviction(regime)
    
        # Log regime status
        print(f"SignalWorker: regime={regime} risk_scalar={risk_scalar:.2f} "
              f"min_conviction={min_conviction_for_regime}")
        log_repo.write("INFO", "signal_worker",
                       f"Regime: {regime} | scalar={risk_scalar:.2f} | min={min_conviction_for_regime}")
    
        # ── 3. Fetch fresh BUY signals — filtered by regime min conviction ────────
        all_signals = signal_repo.get_fresh(min_conviction=min_conviction_for_regime)
        # Filter to BUY signals only: exclude SELL signals (signal_type contains 'SELL' or 'OVERBOUGHT')
        buy_signals = [
            s for s in all_signals
            if 'SELL' not in (s.get('signal_type') or '').upper()
               and 'OVERBOUGHT' not in (s.get('signal_type') or '').upper()
        ]
    
        if not buy_signals:
            logger.info("SignalWorker: no fresh BUY signals with %s+ conviction", min_conviction_for_regime)
            print(f"SignalWorker: 0 signals evaluated, 0 trades approved")
            log_repo.write("INFO", "signal_worker",
                           f"No fresh BUY signals with {min_conviction_for_regime}+ conviction")
            _post('post_alert_embed',
                title=f'⚪ Signal Worker: No signals ({regime})',
                description=(
                    f'Regime: **{regime}** | min_conviction={min_conviction_for_regime} | scalar={risk_scalar:.2f}\n'
                    f'No fresh BUY signals found above conviction threshold.'
                ),
                severity='INFO',
                dry_run=False
            )
            return
    
        # ── 4. Current portfolio state ────────────────────────────────────────────
        equity = state_repo.get_equity()
        if equity <= 0.0:
            equity = 10_000.0
            logger.warning("SignalWorker: equity=0 in state, using default $10,000")
    
        total_exposure = portfolio_repo.get_total_exposure()
        position_count = portfolio_repo.get_position_count()
        cash_estimate = max(0.0, equity - total_exposure)
    
        logger.info(
            "SignalWorker: equity=%.2f exposure=%.2f cash=%.2f positions=%d regime=%s scalar=%.2f",
            equity, total_exposure, cash_estimate, position_count, regime, risk_scalar,
        )
    
        # ── Sizing config — V5: apply risk_scalar (replaces buy_aggressiveness) ───
        sizing = cfg.get("sizing", {})
        conviction_pct: dict[str, float] = {
            "VERY_HIGH": sizing.get("very_high_pct", 8.0),
            "HIGH":      sizing.get("high_pct",      7.0),
            "MEDIUM":    sizing.get("medium_pct",     6.0),
            "LOW":       sizing.get("low_pct",        2.0),
        }
        # V5: risk_scalar replaces buy_aggressiveness (never >1.0 — no revenge trading)
        buy_aggressiveness: float = min(risk_scalar, 1.0)
    
        # ── 5. Rank & filter candidates BEFORE slicing to top-3 ────────────────────
        # V5 fix: market-open and blacklist checks used to run *inside* the loop
        # over the already-sliced top-3-by-score signals. That meant a closed
        # market (e.g. crypto, which is always "fresh") or a blacklisted
        # instrument could occupy one of only 3 scarce slots per 15-min cycle,
        # starving open/tradable equity markets of any chance to be evaluated —
        # even though their signals sat unused in the FRESH pool until the 6h TTL
        # expired. Filtering BEFORE ranking+slicing fixes this.
    
        def _resolve_symbol(instrument_id: int) -> str:
            """Look up ticker symbol for an instrument_id (signals table has none)."""
            try:
                inst_row = db.fetchone(
                    "SELECT symbol FROM instruments WHERE instrument_id=?",
                    (instrument_id,),
                )
                if inst_row:
                    return inst_row["symbol"] if isinstance(inst_row, dict) else inst_row[0]
            except Exception:
                pass
            snap = portfolio_repo.get_by_instrument(instrument_id)
            if snap:
                sym = snap[0].get("symbol", "")
                if sym:
                    return sym
            return str(instrument_id)
    
        skipped_closed: list[str] = []
        eligible: list[tuple[dict, str]] = []  # (signal, symbol) — open market, not blacklisted
    
        for signal in buy_signals:
            instrument_id = signal["instrument_id"]
            signal_id = signal.get("id")
    
            # Ghost blacklist check — skip blacklisted instruments
            if trade_repo.is_instrument_blacklisted(instrument_id):
                ghost_count = trade_repo.get_ghost_failure_count(instrument_id)
                logger.info(
                    "SignalWorker: %s BLACKLISTED (%d consecutive ghost failures) — skipping",
                    instrument_id, ghost_count,
                )
                signal_repo.update_signal_status(signal_id, "REJECTED")
                continue
    
            symbol = _resolve_symbol(instrument_id)
    
            # Market hours check (Crypto = always open)
            if not is_market_open(symbol):
                skipped_closed.append(f'{symbol} (market closed)')
                continue
    
            eligible.append((signal, symbol))
    
        # Sort by boosted score descending — only among OPEN, non-blacklisted
        # signals. The boost (get_score_boost) prioritizes stocks/ETFs over
        # crypto/commodities/indices when raw scores are close, without
        # changing the underlying exposure caps (ASSET_CLASS_LIMITS in
        # risk.py still applies at the gate stage further down).
        eligible.sort(
            key=lambda t: float(t[0].get("score", 0)) * get_score_boost(t[1]),
            reverse=True,
        )
    
        # Deduplicate: keep only the highest-score signal per instrument_id
        seen_instruments = set()
        unique_candidates: list[tuple[dict, str]] = []
        for signal, symbol in eligible:
            inst_id = signal["instrument_id"]
            if inst_id not in seen_instruments:
                seen_instruments.add(inst_id)
                unique_candidates.append((signal, symbol))
    
        # Take max 3 unique candidates — guaranteed open-market, non-blacklisted
        candidates = unique_candidates[:3]
    
        evaluated_count = 0
        approved_count = 0
        blocked_reasons: list[str] = []
    
        # Fetch open positions once for asset-class gate (list of {symbol, amount_usd})
        open_positions_raw = portfolio_repo.get_all()
        open_positions = [
            {"symbol": p.get("symbol", ""), "amount_usd": float(p.get("amount_usd") or 0.0)}
            for p in open_positions_raw
        ]
    
        for signal, symbol in candidates:
            instrument_id = signal["instrument_id"]
            conviction = signal.get("conviction", "MEDIUM")
            score = float(signal.get("score", 0))
            signal_id = signal.get("id")
    
            # a. Current amount + fragment count for pyramiding check
            snap_rows = portfolio_repo.get_by_instrument(instrument_id)
            current_symbol_amount = sum(
                float(r.get("amount_usd") or 0.0) for r in snap_rows
            )
            existing_fragments = len(snap_rows)
    
            # b. Buy amount based on conviction × risk_scalar (V5)
            pct = conviction_pct.get(conviction.upper(), conviction_pct["MEDIUM"])
            buy_amount = round((pct / 100.0) * equity * buy_aggressiveness, 2)
    
            # Enforce minimum from regime params
            min_buy = regime_params.get("min_buy_usd", 50.0)
            if buy_amount < min_buy:
                logger.info(
                    "SignalWorker: %s buy_amount $%.2f < regime min $%.2f — skipped",
                    symbol, buy_amount, min_buy,
                )
                continue
    
            # c. Run master buy gate V5
            gate = check_buy_gate(
                symbol=symbol,
                buy_amount=buy_amount,
                equity=equity,
                cash=cash_estimate,
                regime=regime,
                open_count=position_count,
                current_symbol_amount=current_symbol_amount,
                total_exposed=total_exposure,
                has_stop_loss=True,
                open_positions=open_positions,
                conviction=conviction,                   # V5: conviction gate
                existing_fragments=existing_fragments,   # V5: pyramiding gate
            )
    
            if gate.allowed:
                evaluated_count += 1
                # d. Get signal price for execution (yfinance data)
                signal_price = float(signal.get("price") or 0.0) if signal.get("price") else None
    
                # e. Create trade PENDING_APPROVAL → immediately APPROVED
                trade_id = trade_repo.create(
                    instrument_id=instrument_id,
                    symbol=symbol,
                    direction="BUY",
                    amount_usd=buy_amount,
                    stop_loss_pct=cfg.get("sl", {}).get("default_pct", 3.0),
                    signal_id=signal_id,
                    signal_price=signal_price,
                )
                from datetime import datetime, timezone
                trade_repo.update_status(
                    trade_id,
                    "APPROVED",
                    approved_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                )
                # Mark signal as consumed so it won't be re-processed
                signal_repo.update_signal_status(signal_id, "CONSUMED")
                approved_count += 1
    
                # Update running totals so subsequent signals see projected state
                total_exposure += buy_amount
                cash_estimate -= buy_amount
                position_count += 1
                open_positions.append({"symbol": symbol, "amount_usd": buy_amount})
    
                logger.info(
                    "SignalWorker: APPROVED trade #%d — %s %s $%.2f (conviction=%s score=%.2f signal_price=%.4f)",
                    trade_id, "BUY", symbol, buy_amount, conviction, score, signal_price or 0,
                )
                log_repo.write(
                    "INFO",
                    "signal_worker",
                    f"Trade APPROVED: {symbol} BUY ${buy_amount:.2f}",
                    {
                        "trade_id": trade_id,
                        "instrument_id": instrument_id,
                        "conviction": conviction,
                        "score": score,
                        "signal_price": signal_price,
                        "gate_reasons": gate.reasons,
                    },
                )
            else:
                evaluated_count += 1
                reason = gate.summary()
                blocked_reasons.append(f'{symbol}: {reason}')
                # Mark signal as rejected so it won't be re-processed
                signal_repo.update_signal_status(signal_id, "REJECTED")
                logger.info(
                    "SignalWorker: BLOCKED %s $%.2f — %s",
                    symbol, buy_amount, reason,
                )
                logger.info('SignalWorker: %s BLOCKED — %s', symbol, ', '.join(gate.reasons))
                log_repo.write(
                    "INFO",
                    "signal_worker",
                    f"Signal BLOCKED: {symbol}",
                    {
                        "instrument_id": instrument_id,
                        "conviction": conviction,
                        "score": score,
                        "reason": reason,
                    },
                )
    
        # ── 6. Summary ────────────────────────────────────────────────────────────
        print(f"SignalWorker: {evaluated_count} signals evaluated, {approved_count} trades approved")
        log_repo.write(
            "INFO",
            "signal_worker",
            f"Run complete: evaluated={evaluated_count} approved={approved_count} regime={regime}",
        )
    
        # ── 7. Discord summary ────────────────────────────────────────────────────
        if approved_count > 0:
            _post('post_alert_embed',
                title=f'🟢 Signal Worker: {approved_count} Trade(s) approved',
                description=(
                    f'Regime: **{regime}** | scalar={risk_scalar:.2f}\n'
                    f'Evaluated: {evaluated_count} | Approved: {approved_count}\n'
                    f'Equity: ${equity:,.0f} | Cash: ${cash_estimate:,.0f} ({cash_estimate/equity*100:.1f}%)'
                ),
                severity='INFO',
                dry_run=False
            )
        elif evaluated_count == 0 and skipped_closed:
            # All candidates had closed markets — post with context
            _post('post_alert_embed',
                title=f'🔴 Signal Worker: All markets closed ({regime})',
                description=(
                    f'Regime: **{regime}** | scalar={risk_scalar:.2f}\n'
                    f'Signals available: {len(buy_signals)} BUY signals (top 3 evaluated)\n'
                    f'Markets closed: {", ".join(skipped_closed[:5])}\n'
                    f'No trades — waiting for market open.'
                ),
                severity='INFO',
                dry_run=False
            )
        elif evaluated_count > 0 and approved_count == 0:
            # Signals evaluated but all blocked by risk gates
            _post('post_alert_embed',
                title=f'🟡 Signal Worker: All signals blocked ({regime})',
                description=(
                    f'Regime: **{regime}** | scalar={risk_scalar:.2f}\n'
                    f'Evaluated: {evaluated_count} | Approved: 0\n'
                    f'Equity: ${equity:,.0f} | Cash: ${cash_estimate:,.0f} ({cash_estimate/equity*100:.1f}%)\n'
                    f'Blocked reasons:\n' + "\n".join(f'• {r}' for r in blocked_reasons[:5])
                ),
                severity='INFO',
                dry_run=False
            )
        elif evaluated_count == 0:
            _post('post_alert_embed',
                title=f'⚪ Signal Worker: No signals ({regime})',
                description=f'Regime: **{regime}** | min_conviction={min_conviction_for_regime} | scalar={risk_scalar:.2f}',
                severity='INFO',
                dry_run=False
            )
    
    
if __name__ == "__main__":
    main()
