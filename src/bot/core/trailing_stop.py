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
                    stats['partial_closes'] += 1
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
                                reason=f'Profit-Taking: {action.reason}',
                            )
                    except Exception:
                        pass
                time.sleep(0.5)
            except Exception as e:
                msg = f'{action.symbol}: partial-close API call failed — {e}'
                logger.error('[trailing] %s', msg)
                stats['errors'].append(msg)

    return stats
