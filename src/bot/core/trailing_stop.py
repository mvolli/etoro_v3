#!/usr/bin/env python3
"""Trailing Stop Manager — Trading Bible V5.

Monitors open positions for profit-taking opportunities.
Runs inside Risk Worker after SL enforcement.

Note: eToro has no SL-update endpoint. Break-even enforcement
requires Close+Reopen (blocked in DEFENSIVE/CRITICAL).
Partial profit-taking uses units-based close.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

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
    close_pct: float = 0.0  # for PARTIAL_CLOSE


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
        amount = float(pos.get('amount', 0))
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

            print(f'[trailing] PARTIAL_CLOSE {action.close_pct}%: {action.symbol} {action.pnl_pct:+.1f}% — {action.reason}')

            if dry_run:
                stats['partial_closes'] += 1
                continue

            try:
                # Get current position to calculate units to close
                # close_pct of position = close_pct/100 of units
                result = client.close_position(
                    position_id=action.position_id,
                    instrument_id=0,  # will be resolved by client
                    units_to_deduct_pct=action.close_pct,  # % of units
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
                stats['errors'].append(f'{action.symbol}: {e}')

    return stats
