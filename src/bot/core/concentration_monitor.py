#!/usr/bin/env python3
"""Concentration Monitor — Trading Bible V5, P3.

Post-trade monitoring: detects and corrects concentration violations.
Runs as part of risk_worker every 5 minutes.

V5 rules:
  - LIFO fragment closure (newest first)
  - >25% over limit: immediate close
  - <25% over limit: WARNING + tighter monitoring
  - Pyramiding check: no new fragments in DEFENSIVE/CRITICAL
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot.core.risk import INSTRUMENT_LIMITS, DEFAULT_INSTRUMENT_LIMIT

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


def get_symbol_from_instrument_id(instrument_id: int, instrument_map: dict) -> str:
    """Resolve instrument_id → symbol."""
    return instrument_map.get(instrument_id, f"ID{instrument_id}")


def check_concentration_violations(
    positions: list[dict],
    equity: float,
    instrument_map: dict,
) -> list[dict]:
    """Detect concentration violations across all positions.

    Args:
        positions: Live positions from eToro API
        equity: Current total equity
        instrument_map: {instrument_id: symbol}

    Returns:
        List of violations: [{symbol, total_amount, limit_pct, actual_pct,
                               breach_pct, severity, fragments, action}]
    """
    if equity <= 0:
        return []

    # Aggregate by symbol
    symbol_positions: dict[str, list[dict]] = {}
    for pos in positions:
        iid = int(pos.get("instrumentID", 0))
        sym = get_symbol_from_instrument_id(iid, instrument_map)
        if sym not in symbol_positions:
            symbol_positions[sym] = []
        symbol_positions[sym].append(pos)

    violations = []
    for sym, sym_positions in symbol_positions.items():
        total_amount = sum(float(p.get("amount", 0)) for p in sym_positions)
        actual_pct = (total_amount / equity) * 100
        limit_pct = INSTRUMENT_LIMITS.get(sym.upper(), DEFAULT_INSTRUMENT_LIMIT)

        if actual_pct > limit_pct:
            breach_pct = actual_pct - limit_pct
            excess_amount = total_amount - (equity * limit_pct / 100)

            # Severity: >25% over limit = IMMEDIATE, <25% = WARNING
            severity = "IMMEDIATE" if breach_pct > limit_pct * 0.25 else "WARNING"

            # Sort fragments LIFO (newest first = highest openDateTime)
            sorted_frags = sorted(
                sym_positions,
                key=lambda p: p.get("openDateTime", ""),
                reverse=True,
            )

            violations.append({
                "symbol": sym,
                "total_amount": total_amount,
                "actual_pct": actual_pct,
                "limit_pct": limit_pct,
                "breach_pct": breach_pct,
                "excess_amount": excess_amount,
                "severity": severity,
                "fragments": sorted_frags,  # LIFO sorted
                "fragment_count": len(sym_positions),
                "action": "CLOSE_EXCESS" if severity == "IMMEDIATE" else "WARN",
            })

    return violations


def close_concentration_excess(
    client: Any,
    violations: list[dict],
    dry_run: bool = False,
) -> dict:
    """Close excess fragments to restore concentration limits (LIFO order).

    Args:
        client: EToroClient
        violations: From check_concentration_violations()
        dry_run: If True, only log what would be done

    Returns:
        Stats dict: {closed, warned, errors}
    """
    stats = {"closed": 0, "warned": 0, "errors": []}

    for v in violations:
        sym = v["symbol"]
        severity = v["severity"]

        if severity == "WARNING":
            print(
                f"[concentration] ⚠️ WARNING: {sym} at {v['actual_pct']:.1f}% "
                f"(limit {v['limit_pct']:.0f}%) — {v['breach_pct']:.1f}% over"
            )
            stats["warned"] += 1
            continue

        # IMMEDIATE: close newest fragments until back within limit
        excess = v["excess_amount"]
        print(
            f"[concentration] 🔴 IMMEDIATE: {sym} at {v['actual_pct']:.1f}% "
            f"(limit {v['limit_pct']:.0f}%) — closing ${excess:.2f} excess (LIFO)"
        )

        closed_amount = 0.0
        for frag in v["fragments"]:  # Already LIFO sorted
            if closed_amount >= excess:
                break

            pos_id = str(frag.get("positionID", ""))
            iid = int(frag.get("instrumentID", 0))
            frag_amount = float(frag.get("amount", 0))
            open_dt = frag.get("openDateTime", "?")[:10]
            no_sl = frag.get("isNoStopLoss", True)

            print(
                f"  → Close {sym} pos={pos_id} ${frag_amount:.0f} "
                f"(opened {open_dt}, noSL={no_sl})"
            )

            if dry_run:
                closed_amount += frag_amount
                stats["closed"] += 1
                continue

            try:
                client.close_position(pos_id, iid)
                closed_amount += frag_amount
                stats["closed"] += 1

                # Post Discord embed
                try:
                    upnl = frag.get("unrealizedPnL") or {}
                    _discord(
                        "post_position_closed_embed",
                        symbol=sym,
                        amount_usd=frag_amount,
                        position_id=pos_id,
                        entry_price=float(frag.get("openRate", 0)),
                        close_price=float(upnl.get("closeRate", 0)),
                        pnl_usd=float(upnl.get("pnL", 0)),
                        reason=f"Konzentrations-Bereinigung: {sym} war {v['actual_pct']:.1f}% (Limit {v['limit_pct']:.0f}%)",
                    )
                except Exception:
                    pass

                time.sleep(0.5)  # Rate limit
            except Exception as e:
                stats["errors"].append(f"{sym} pos={pos_id}: {e}")
                print(f"  ❌ Close failed: {e}")

    return stats
