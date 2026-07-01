#!/usr/bin/env python3
"""Trailing Stop Manager — Trading Bible V5.

Monitors open positions for profit-taking opportunities.
Runs inside Risk Worker after SL enforcement.

Note: eToro has no SL-update endpoint. Break-even enforcement
requires Close+Reopen (blocked in DEFENSIVE/CRITICAL).
Partial profit-taking uses units-based close (see EToroClient.close_position).
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Profit-Taking Thresholds (Trading Bible V5) ──────────────────────────────
BREAK_EVEN_TRIGGER_PCT = 5.0    # +5% PnL → move SL to entry (software tracking)
PROFIT_TAKE_LEVELS = [
    {'threshold': 15.0, 'close_pct': 20},   # +15% → close 20% of position
    {'threshold': 25.0, 'close_pct': 20},   # +25% → close another 20%
    {'threshold': 50.0, 'close_pct': 30},   # +50% → close 30%
]

@dataclass
class TrailingAction:
    action: str          # 'BREAK_EVEN' | 'PARTIAL_CLOSE' | 'OK'
    symbol: str
    position_id: str
    pnl_pct: float
    reason: str
    close_pct: float = 0.0     # for PARTIAL_CLOSE — target % of position to close
    instrument_id: int = 0     # needed for close_position() body
    amount_usd: float = 0.0    # position size in USD — used to derive units
    open_rate: float = 0.0     # entry price — used to derive units


def evaluate_trailing(
    positions: list[dict],
    regime: str = 'NORMAL',
) -> list[TrailingAction]:
    """Evaluate all positions for trailing stop opportunities.

    Args:
        positions: Raw positions from eToro API (clientPortfolio.positions)
        regime: Current trading regime
    Returns:
        List of TrailingActions to execute
    """
    actions = []
    for pos in positions:
        pos_id = str(pos.get('positionID', ''))
        symbol = pos.get('symbol', str(pos.get('instrumentID', '')))
        instrument_id = int(pos.get('instrumentID') or pos.get('instrumentId') or 0)
        amount = float(pos.get('amount', 0))
        open_rate = float(pos.get('openRate', 0) or 0)
        upnl = pos.get('unrealizedPnL') or {}
        pnl_usd = float(upnl.get('pnL', 0)) if isinstance(upnl, dict) else 0.0

        if amount <= 0:
            continue
        pnl_pct = (pnl_usd / amount) * 100

        if pnl_pct < BREAK_EVEN_TRIGGER_PCT:
            continue  # No action needed

        # Check profit-taking levels (highest first)
        for level in sorted(PROFIT_TAKE_LEVELS, key=lambda x: x['threshold'], reverse=True):
            if pnl_pct >= level['threshold']:
                actions.append(TrailingAction(
                    action='PARTIAL_CLOSE',
                    symbol=symbol,
                    position_id=pos_id,
                    pnl_pct=pnl_pct,
                    reason=f"+{pnl_pct:.1f}% ≥ +{level['threshold']:.0f}% profit target",
                    close_pct=level['close_pct'],
                    instrument_id=instrument_id,
                    amount_usd=amount,
                    open_rate=open_rate,
                ))
                break
        else:
            # Only break-even (5-15% range)
            if pnl_pct >= BREAK_EVEN_TRIGGER_PCT:
                actions.append(TrailingAction(
                    action='BREAK_EVEN',
                    symbol=symbol,
                    position_id=pos_id,
                    pnl_pct=pnl_pct,
                    reason=f"+{pnl_pct:.1f}% ≥ +{BREAK_EVEN_TRIGGER_PCT:.0f}% — break-even tracked",
                    instrument_id=instrument_id,
                ))
    return actions


def _find_position(client: Any, instrument_id: int, position_id: str) -> dict | None:
    """Look up a position by instrument_id (+ position_id if present) in the
    live eToro portfolio. Used to verify a partial-close actually took
    effect, since eToro's close-order response only confirms the order was
    ACCEPTED (statusID=1), not that it has been applied yet — verified via
    a live test on 2026-07-01: a partial-close response arrived instantly
    with statusID=1, but the portfolio amount only reflected the reduction
    after ~9s of polling.
    """
    try:
        portfolio = client.get_portfolio()
    except Exception:
        return None
    positions = (
        portfolio.get("clientPortfolio", {}).get("positions")
        or portfolio.get("positions")
        or []
    )
    for pos in positions:
        pid = str(pos.get("positionID") or pos.get("positionId") or "")
        iid = pos.get("instrumentID") or pos.get("instrumentId")
        if position_id and pid == str(position_id):
            return pos
        if not position_id and iid is not None and int(iid) == int(instrument_id):
            return pos
    return None


def _verify_partial_close(
    client: Any,
    action: "TrailingAction",
    max_attempts: int = 6,
    initial_wait_s: float = 3.0,
) -> tuple[bool, str]:
    """Poll the live portfolio with exponential backoff until the position's
    amount actually reflects the expected reduction, instead of trusting
    close_position()'s immediate 200/statusID=1 response.

    Mirrors the ghost-order verification pattern already used in
    execution_worker.py (open-side) — this is the same check for the
    close/partial-close side, which previously had none.

    Returns (verified, detail).
    """
    import time as _time

    expected_amount = action.amount_usd * (1 - action.close_pct / 100.0)
    tolerance_pct = 5.0  # allow rounding/spread drift, matches manual test tolerance
    waited = 0.0

    for attempt in range(max_attempts):
        wait_s = min(initial_wait_s * (2 ** attempt), 30)
        _time.sleep(wait_s)
        waited += wait_s

        pos = _find_position(client, action.instrument_id, action.position_id)

        if pos is None:
            # Position fully gone — could mean the WHOLE position closed
            # instead of just close_pct% of it. That is a worse outcome
            # than "nothing happened", not a success — never count it.
            return False, (
                f"{action.symbol}: position vanished entirely after partial-close "
                f"(expected ~${expected_amount:.2f} remaining, position not found "
                f"after {waited:.0f}s) — possible FULL close instead of partial"
            )

        actual_amount = float(pos.get("amount", 0))
        if abs(actual_amount - action.amount_usd) < 0.01:
            continue  # amount hasn't moved yet — keep polling

        diff_pct = abs(actual_amount - expected_amount) / max(expected_amount, 0.01) * 100
        if diff_pct < tolerance_pct:
            return True, (
                f"{action.symbol}: partial-close CONFIRMED after {waited:.0f}s — "
                f"${action.amount_usd:.2f} → ${actual_amount:.2f} "
                f"(expected ${expected_amount:.2f}, diff {diff_pct:.1f}%)"
            )
        # Amount changed but not to the expected value — record and keep
        # polling in case it's still settling, but don't return success yet.
        logger.debug(
            "[trailing] %s: amount changed to $%.2f (expected $%.2f) after %.0fs, "
            "still polling", action.symbol, actual_amount, expected_amount, waited,
        )

    # Exhausted all attempts without a confirmed match
    final_pos = _find_position(client, action.instrument_id, action.position_id)
    final_amount = float(final_pos.get("amount", 0)) if final_pos else 0.0
    return False, (
        f"{action.symbol}: partial-close NOT CONFIRMED after {waited:.0f}s "
        f"— amount is ${final_amount:.2f}, expected ~${expected_amount:.2f} "
        f"(started at ${action.amount_usd:.2f})"
    )


def verify_full_close(
    client: Any,
    instrument_id: int,
    position_id: str,
    max_attempts: int = 6,
    initial_wait_s: float = 3.0,
) -> tuple[bool, str]:
    """Poll until a position after a full-close has actually disappeared,
    instead of trusting the immediate 200 response (which only means the
    order was accepted). For SL-close (risk_worker) and concentration-close.

    Returns (confirmed, detail).
    """
    import time as _time

    waited = 0.0
    for attempt in range(max_attempts):
        wait_s = min(initial_wait_s * (2 ** attempt), 30)
        _time.sleep(wait_s)
        waited += wait_s
        pos = _find_position(client, instrument_id, position_id)
        if pos is None:
            return True, f"Full-close CONFIRMED after {waited:.0f}s"
    return False, f"Full-close NOT confirmed after {waited:.0f}s — position may still be open"


def execute_trailing_actions(
    client: Any,
    actions: list[TrailingAction],
    regime: str = 'NORMAL',
    dry_run: bool = False,
) -> dict:
    """Execute trailing stop actions.

    PARTIAL_CLOSE: Closes a percentage of the position via API.
    BREAK_EVEN: Software tracking only (logged, no API call — eToro has no SL update).
    """
    import time
    stats = {'partial_closes': 0, 'break_evens': 0, 'errors': []}

    for action in actions:
        if action.action == 'BREAK_EVEN':
            # Software tracking — log only, no API call
            print(f'[trailing] BREAK-EVEN tracked: {action.symbol} {action.pnl_pct:+.1f}% — SL conceptually at entry')
            stats['break_evens'] += 1
            continue

        if action.action == 'PARTIAL_CLOSE':
            if regime in ('DEFENSIVE', 'CRITICAL'):
                # In stressed regimes, let existing winners run — don't force sells
                print(f'[trailing] PARTIAL_CLOSE skipped in {regime}: {action.symbol} {action.pnl_pct:+.1f}%')
                continue

            # ── Convert target % into absolute units (eToro API expects
            #    UnitsToDeduct as a unit count, not a percentage) ──────────
            if action.open_rate <= 0:
                msg = (
                    f'{action.symbol}: cannot compute partial-close units '
                    f'(missing open_rate={action.open_rate}) — skipped, no order sent'
                )
                logger.warning('[trailing] %s', msg)
                stats['errors'].append(msg)
                continue

            total_units = action.amount_usd / action.open_rate
            units_to_deduct = round(total_units * (action.close_pct / 100.0), 8)

            if units_to_deduct <= 0:
                msg = f'{action.symbol}: computed units_to_deduct <= 0 — skipped'
                logger.warning('[trailing] %s', msg)
                stats['errors'].append(msg)
                continue

            print(f'[trailing] PARTIAL_CLOSE {action.close_pct}%: {action.symbol} {action.pnl_pct:+.1f}% — {action.reason} (units={units_to_deduct:.6f})')

            if dry_run:
                stats['partial_closes'] += 1
                continue

            try:
                result = client.close_position(
                    position_id=action.position_id,
                    instrument_id=action.instrument_id,
                    units_to_deduct=units_to_deduct,
                )
                if result:
                    # ── Verify the partial-close actually took effect ──────
                    # close_position() returning 200 only means the order was
                    # ACCEPTED (statusID=1), not applied — confirmed via live
                    # test 2026-07-01 (amount only updated after ~9s poll).
                    # Don't count it as a success until we've seen it reflected
                    # in the actual portfolio.
                    verified, detail = _verify_partial_close(client, action)
                    if verified:
                        logger.info('[trailing] %s', detail)
                        stats['partial_closes'] += 1
                    else:
                        logger.warning('[trailing] %s', detail)
                        stats['errors'].append(detail)
                    # Post Discord embed
                    try:
                        from pathlib import Path as _Path
                        _embed_file = str(_Path(__file__).resolve().parent.parent / 'discord_embeds.py')
                        import importlib.util
                        spec = importlib.util.spec_from_file_location(
                            'discord_embeds',
                            _embed_file
                        )
                        de = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(de)
                        if hasattr(de, 'post_position_closed_embed'):
                            de.post_position_closed_embed(
                                symbol=action.symbol,
                                amount_usd=0,
                                position_id=action.position_id,
                                reason=f'Profit-Taking: {action.reason}'
                                + ('' if verified else ' [UNVERIFIED — siehe Log]'),
                            )
                    except Exception:
                        pass
                else:
                    stats['errors'].append(
                        f'{action.symbol}: close_position() returned empty/falsy result'
                    )
                time.sleep(0.5)
            except Exception as e:
                msg = f'{action.symbol}: partial-close API call failed — {e}'
                logger.error('[trailing] %s', msg)
                stats['errors'].append(msg)

    return stats
