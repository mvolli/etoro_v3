#!/usr/bin/env python3
"""Monitor Worker — Discord Heartbeat & Portfolio Report.

Runs every 30 minutes. Posts portfolio status embed to #etoro-trading.
Reuses discord_embeds.py from old system for proven embed formatting.

Schedule: */30 * * * *
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Project root
_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from bot.config import load_config
from bot.db.connection import DB
from bot.db.repo import PortfolioRepo, StateRepo, LogRepo


# ── Discord integration (reuse old embed module) ───────────────────────────────

def _try_import_embeds():
    """Import discord_embeds from V3 src/bot/ (no infrastructure_module dependency)."""
    v3_bot = Path(__file__).parent.parent  # src/bot/
    if str(v3_bot) not in sys.path:
        sys.path.insert(0, str(v3_bot))
    try:
        import discord_embeds
        return discord_embeds
    except ImportError:
        pass
    return None


def _get_and_increment_tick(state_repo) -> int:
    """Persistent tick counter stored in system_state. Survives cron restarts."""
    try:
        current = int(state_repo.get("MONITOR_TICK") or "0")
        next_tick = current + 1
        state_repo.set("MONITOR_TICK", str(next_tick))
        return next_tick
    except Exception:
        return 0


def _post_heartbeat(
    embeds_module,
    equity: float,
    cash: float,
    position_count: int,
    drawdown_pct: float,
    regime: str,
    peak_equity: float,
    tick: int = 0,
) -> bool:
    """Post heartbeat embed using discord_embeds module."""
    if embeds_module is None:
        return False

    # Determine severity for embed color
    if drawdown_pct >= 8.0:
        severity = "CRITICAL"
    elif drawdown_pct >= 4.0:
        severity = "WARNING"
    else:
        severity = "OK"

    cb_active = regime in ("DEFENSIVE", "CRITICAL")
    cb_status = {
        "regime": regime,
        "drawdown_pct": drawdown_pct,
        "peak_equity": peak_equity,
    }

    try:
        return embeds_module.post_heartbeat_embed(
            tick=tick,
            equity=equity,
            cash=cash,
            position_count=position_count,
            drawdown_pct=drawdown_pct,
            severity=severity,
            cb_active=cb_active,
            elapsed_s=0.0,
            cb_status=cb_status,
        )
    except Exception as e:
        print(f"[monitor] Heartbeat embed failed: {e}")
        return False


def _post_alert_embed(embeds_module, title: str, description: str, severity: str = "WARNING") -> bool:
    """Post alert embed."""
    if embeds_module is None:
        return False
    try:
        return embeds_module.post_alert_embed(
            title=title,
            description=description,
            severity=severity,
        )
    except Exception as e:
        print(f"[monitor] Alert embed failed: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    # ── Worker lock: prevent overlapping cron invocations ────────────────────
    from bot.core.worker_lock import worker_lock

    with worker_lock("monitor_worker") as acquired:
        if not acquired:
            print("MonitorWorker: SKIPPED (already running)")
            return 0

        t_start = time.time()
        cfg = load_config()
    
        db = DB(cfg.db.abs_path)
        portfolio_repo = PortfolioRepo(db)
        state_repo = StateRepo(db)
        log_repo = LogRepo(db)
    
        # Load Discord embed module
        embeds = _try_import_embeds()
        if embeds is None:
            print("[monitor] discord_embeds not available — printing to stdout only")
    
        # ── Collect portfolio state ──────────────────────────────────────────────
        equity = state_repo.get_equity()
        peak_equity = state_repo.get_peak_equity()
        drawdown_pct = state_repo.get_drawdown_pct()
        regime = state_repo.get_regime()
        position_count = portfolio_repo.get_position_count()
        total_exposure = portfolio_repo.get_total_exposure()
        cash = max(0.0, equity - total_exposure)
        cash_pct = (cash / equity * 100) if equity > 0 else 0.0
    
        elapsed = time.time() - t_start
    
        # ── Print status ─────────────────────────────────────────────────────────
        print(f"[monitor] Equity: ${equity:.2f} | Cash: ${cash:.2f} ({cash_pct:.1f}%)")
        print(f"[monitor] Positions: {position_count} | Drawdown: {drawdown_pct:.2f}% | Regime: {regime}")
        print(f"[monitor] Peak: ${peak_equity:.2f} | Exposure: ${total_exposure:.2f}")
    
        # ── Post heartbeat embed ─────────────────────────────────────────────────
        tick = _get_and_increment_tick(state_repo)
        ok = _post_heartbeat(
            embeds_module=embeds,
            equity=equity,
            cash=cash,
            position_count=position_count,
            drawdown_pct=drawdown_pct,
            regime=regime,
            peak_equity=peak_equity,
            tick=tick,
        )
        print(f"[monitor] Tick #{tick} | Discord embed: {'OK ✓' if ok else 'SKIP (no embeds module)' if embeds is None else 'FAILED'}")
    
        # ── Dead-man's switch: alert on stale workers (fix/autonomy-hardening) ───
        try:
            from bot.core.heartbeat import get_stale_workers
            from bot.core.kill_switch import is_kill_switch_active
            stale = get_stale_workers(state_repo)
            if stale and is_kill_switch_active():
                # Kill switch legitimately silences signal/execution — only
                # report the always-on workers to avoid alert noise.
                stale = [s for s in stale
                         if not s.startswith(("signal_worker", "execution_worker"))]
            if stale:
                stale_desc = "\n".join(f"• {s}" for s in stale)
                print(f"[monitor] STALE WORKERS:\n{stale_desc}")
                log_repo.write("ERROR", "monitor_worker",
                               f"Stale workers detected: {len(stale)}",
                               {"stale": stale})
                _post_alert_embed(
                    embeds,
                    title="🔴 Dead-Man's-Switch: Worker ohne Heartbeat",
                    description=(
                        f"{stale_desc}\n\n"
                        f"Positionen laufen ggf. unüberwacht (nur Broker-SL aktiv). "
                        f"Cron/WSL prüfen!"
                    ),
                    severity="CRITICAL",
                )
        except Exception as _hb_exc:
            print(f"[monitor] Heartbeat check failed: {_hb_exc}")

        # ── Alert on DEFENSIVE / CRITICAL regime (V5) ────────────────────────────
        if regime in ("DEFENSIVE", "CRITICAL"):
            risk_scalar = float(state_repo.get("RISK_SCALAR") or "0.5")
            _post_alert_embed(
                embeds,
                title=f"{'🔴' if regime == 'CRITICAL' else '🟠'} {regime}-Regime aktiv",
                description=(
                    f"Drawdown: **{drawdown_pct:.2f}%** | risk_scalar={risk_scalar:.2f}\n"
                    f"Equity: **${equity:.2f}** | Peak: **${peak_equity:.2f}**\n"
                    f"{'Nur VERY_HIGH Signale' if regime == 'CRITICAL' else 'Nur HIGH+ Signale'} — kein Pyramiding."
                ),
                severity="CRITICAL" if regime == "CRITICAL" else "WARNING",
            )
    
        # ── Log to DB ────────────────────────────────────────────────────────────
        log_repo.write(
            "INFO",
            "monitor_worker",
            f"Heartbeat: equity=${equity:.2f}, positions={position_count}, "
            f"drawdown={drawdown_pct:.2f}%, regime={regime}, "
            f"embed={'OK' if ok else 'SKIP'}",
        )
    
        print(f"[monitor] Done in {time.time() - t_start:.1f}s")
        return 0
    
    
if __name__ == "__main__":
    sys.exit(main())
