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

from bot.core.risk import (
    INSTRUMENT_LIMITS,
    DEFAULT_INSTRUMENT_LIMIT,
    ASSET_CLASS_LIMITS,
    ASSET_CLASS_DEFAULT_LIMIT_PCT,
    ASSET_CLASS_MAP,
)

# ── Discord Embeds ─────────────────────────────────────────────────────────────
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..'))
    import discord_embeds as _DE
except Exception:
    _DE = None

def _discord(fn_name: str, **kwargs):
    """Best-effort Discord post. Never raises. Returns Embed-Resultat."""
    try:
        if _DE and hasattr(_DE, fn_name):
            return getattr(_DE, fn_name)(**kwargs)
    except Exception:
        pass
    return None


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


def check_asset_class_violations(
    positions: list[dict],
    equity: float,
    instrument_map: dict,
) -> list[dict]:
    """Detect ASSET-CLASS-level concentration drift (audit H7).

    check_asset_class_gate blocks NEW buys that would breach an asset-class
    cap, but a portfolio can still drift past the cap purely via price
    appreciation after entry — which nothing detected post-trade (only the
    per-instrument check_concentration_violations existed).

    Detection-only, WARNING severity: unlike the per-instrument monitor this
    does NOT auto-close. Forcing sells to rebalance a whole asset class is a
    materially bigger, behaviour-changing lever than trimming a single
    over-limit instrument — it should surface for a human decision, not fire
    automatically. Returns [{asset_class, actual_pct, limit_pct, breach_pct,
    total_amount, symbols}].
    """
    if equity <= 0:
        return []

    class_totals: dict[str, float] = {}
    class_symbols: dict[str, set[str]] = {}
    for pos in positions:
        iid = int(pos.get("instrumentID", 0))
        sym = get_symbol_from_instrument_id(iid, instrument_map)
        asset_class = ASSET_CLASS_MAP.get(sym.upper())
        if not asset_class:
            continue  # unmapped symbol → no asset-class attribution
        amt = float(pos.get("amount", 0))
        class_totals[asset_class] = class_totals.get(asset_class, 0.0) + amt
        class_symbols.setdefault(asset_class, set()).add(sym)

    violations = []
    for asset_class, total in class_totals.items():
        actual_pct = (total / equity) * 100
        limit_pct = ASSET_CLASS_LIMITS.get(asset_class, ASSET_CLASS_DEFAULT_LIMIT_PCT)
        if actual_pct > limit_pct:
            violations.append({
                "asset_class": asset_class,
                "total_amount": total,
                "actual_pct": actual_pct,
                "limit_pct": limit_pct,
                "breach_pct": actual_pct - limit_pct,
                "symbols": sorted(class_symbols[asset_class]),
            })
    return violations


def check_total_exposure_drift(
    positions: list[dict],
    equity: float,
    max_exposure_pct: float,
) -> dict | None:
    """Detect TOTAL portfolio exposure drifting past the cap (post-trade).

    fix/exposure-drift-monitor (2026-08-12): check_exposure_gate is a pure
    PRE-trade gate — nothing ever re-checked total exposure after entry. Same
    blind spot check_asset_class_violations was added for, one level up.

    The drift direction is counter-intuitive and worth stating: `amount` is
    INVESTED CAPITAL, not market value, so exposure% rises when EQUITY FALLS,
    not when prices rise. The live case on 2026-08-12: $10.000 -> $8.668
    equity at a ~flat $7.100 invested pushed 71% -> 81.9% without a single
    new buy. That makes an unmonitored cap a loss amplifier: the deeper the
    drawdown, the further past the cap the book sits.

    Detection-only, like check_asset_class_violations: no auto-close. Forcing
    sells to rebalance the whole book is a materially bigger lever than
    trimming one over-limit instrument — it surfaces for a human decision.

    Returns None when within the cap, else a dict with the breach details.
    """
    if equity <= 0 or max_exposure_pct <= 0:
        return None

    total = sum(float(p.get("amount", 0) or 0) for p in positions)
    actual_pct = (total / equity) * 100.0
    if actual_pct <= max_exposure_pct:
        return None

    cap_usd = equity * max_exposure_pct / 100.0
    return {
        "total_amount": total,
        "equity": equity,
        "actual_pct": actual_pct,
        "limit_pct": float(max_exposure_pct),
        "breach_pct": actual_pct - float(max_exposure_pct),
        "excess_amount": total - cap_usd,
        "position_count": len(positions),
        "severity": "WARNING",
    }


def close_concentration_excess(
    client: Any,
    violations: list[dict],
    dry_run: bool = False,
    db: Any = None,
) -> dict:
    """Close excess fragments to restore concentration limits (LIFO order).

    Args:
        client: EToroClient
        violations: From check_concentration_violations()
        dry_run: If True, only log what would be done
        db: optional DB-Handle fuer das trade_events-Log (feat/pnl-nachreport)

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

                # ── Verify the full-close actually took effect ──────────────
                # fix/verify-close-arity (2026-07-28): verify_full_close gibt
                # seit dem PnL-Umbau ein 3-Tupel zurueck — das alte 2-Tupel-
                # Unpacking warf ValueError und buchte VERIFIZIERTE Closes
                # als Fehler (except unten schluckte alles).
                from bot.core.trailing_stop import verify_full_close
                verified, detail, _pnl_data = verify_full_close(client, iid, pos_id)
                if verified:
                    closed_amount += frag_amount
                    stats["closed"] += 1

                    # Post Discord embed (feat/pnl-nachreport: unbekanntes
                    # PnL als None statt 0.0; Chart mit Entry/Exit-Markern)
                    try:
                        upnl = frag.get("unrealizedPnL") or {}
                        _pnl_usd = (float(upnl.get("pnL")) if isinstance(upnl, dict)
                                    and upnl.get("pnL") is not None else None)
                        _close_price = (float(upnl.get("closeRate", 0))
                                        if isinstance(upnl, dict) else 0.0)
                        _entry = float(frag.get("openRate", 0) or 0)
                        _pnl_pct = None
                        if _pnl_usd is not None and frag_amount > 0:
                            _pnl_pct = _pnl_usd / frag_amount * 100.0
                        try:
                            from bot.core.candle_chart import trade_story_png
                            if _DE is not None and hasattr(_DE, "attach_chart"):
                                _png = trade_story_png(
                                    client, iid, sym,
                                    entry=_entry or None,
                                    exit_price=_close_price or None,
                                    opened_at=frag.get("openDateTime"),
                                )
                                if _png:
                                    _DE.attach_chart(_png)
                        except Exception:
                            pass
                        _reason = (f"Konzentrations-Bereinigung: {sym} war "
                                   f"{v['actual_pct']:.1f}% (Limit {v['limit_pct']:.0f}%)")
                        _post_ok = _discord(
                            "post_position_closed_embed",
                            symbol=sym,
                            amount_usd=frag_amount,
                            position_id=pos_id,
                            entry_price=_entry,
                            close_price=_close_price,
                            pnl_usd=_pnl_usd,
                            pnl_pct=_pnl_pct,
                            reason=_reason,
                        )
                        if db is not None:
                            from bot.core.event_log import record_posted_event
                            record_posted_event(
                                db, _DE, symbol=sym, event_type="CLOSE",
                                source="concentration", post_result=_post_ok,
                                position_id=pos_id, instrument_id=iid or None,
                                price=_close_price or None,
                                amount_usd=frag_amount,
                                pnl_usd=_pnl_usd, pnl_pct=_pnl_pct,
                                pnl_source=("derived" if _pnl_usd is not None else None),
                                reason=_reason, reported_final=False,
                            )
                    except Exception:
                        pass

                    time.sleep(0.5)  # Rate limit
                else:
                    stats["errors"].append(f"{sym} pos={pos_id}: full-close NOT verified — {detail}")
                    print(f"  ❌ Close NOT verified: {detail}")
            except Exception as e:
                stats["errors"].append(f"{sym} pos={pos_id}: {e}")
                print(f"  ❌ Close failed: {e}")

    return stats
