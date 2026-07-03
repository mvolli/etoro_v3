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
# fix/break-even-enforcement: Schwelle, unter die eine BE-armierte Position
# nicht zurückfallen darf. Leicht über 0, damit Spread/Fees den Close nicht
# in einen Mini-Verlust drehen. eToro hat kein SL-Update-Endpoint, daher
# Software-Enforcement: BE aktiv + PnL ≤ Floor → Full Close.
BREAK_EVEN_FLOOR_PCT = 0.3
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
    level_threshold: float = 0.0  # which PROFIT_TAKE_LEVEL fired (for persistence)


# ── Position-State Persistenz ─────────────────────────────────────────────────
# fix/partial-close-level-tracking: evaluate_trailing() hatte KEIN Gedächtnis,
# welche Profit-Level bereits realisiert wurden. Eine Position bei +16% PnL
# feuerte bei JEDEM risk_worker-Lauf (5min) erneut "PARTIAL_CLOSE 20%" — der
# PnL-Prozentsatz des Rests bleibt ja ~gleich — und wurde so in 20%-Schritten
# zwangsliquidiert statt einmalig 20% zu realisieren (Bible: +15% → EINMAL 20%).
# position_state persistiert die genommenen Level pro position_id.

def _ensure_position_state_table(db: Any) -> None:
    """Create the position_state table if it doesn't exist (lazy, idempotent)."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS position_state (
            position_id     TEXT PRIMARY KEY,
            symbol          TEXT,
            levels_taken    TEXT NOT NULL DEFAULT '',
            be_active       INTEGER NOT NULL DEFAULT 0,
            be_triggered_at TEXT,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)


def load_levels_taken(db: Any, position_ids: list[str]) -> dict[str, set[float]]:
    """Return {position_id: {threshold, ...}} for the given positions."""
    if db is None or not position_ids:
        return {}
    try:
        _ensure_position_state_table(db)
        placeholders = ",".join("?" * len(position_ids))
        rows = db.fetchall(
            f"SELECT position_id, levels_taken FROM position_state "
            f"WHERE position_id IN ({placeholders})",
            list(position_ids),
        )
        result: dict[str, set[float]] = {}
        for row in rows:
            pid, levels_csv = row[0], row[1] or ""
            result[pid] = {float(x) for x in levels_csv.split(",") if x.strip()}
        return result
    except Exception as exc:
        logger.warning("[trailing] load_levels_taken failed: %s", exc)
        return {}


def mark_level_taken(db: Any, position_id: str, symbol: str, threshold: float) -> None:
    """Persist that *threshold* has been realized for *position_id*."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        existing = load_levels_taken(db, [position_id]).get(position_id, set())
        existing.add(threshold)
        levels_csv = ",".join(f"{t:g}" for t in sorted(existing))
        db.execute("""
            INSERT INTO position_state (position_id, symbol, levels_taken, updated_at)
            VALUES (?, ?, ?, datetime('now','utc'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                levels_taken = excluded.levels_taken,
                updated_at = excluded.updated_at
        """, (position_id, symbol, levels_csv))
    except Exception as exc:
        logger.warning("[trailing] mark_level_taken(%s, %.0f) failed: %s",
                       position_id, threshold, exc)


def load_be_active(db: Any, position_ids: list[str]) -> set[str]:
    """Return the subset of *position_ids* whose break-even is armed."""
    if db is None or not position_ids:
        return set()
    try:
        _ensure_position_state_table(db)
        placeholders = ",".join("?" * len(position_ids))
        rows = db.fetchall(
            f"SELECT position_id FROM position_state "
            f"WHERE be_active = 1 AND position_id IN ({placeholders})",
            list(position_ids),
        )
        return {row[0] for row in rows}
    except Exception as exc:
        logger.warning("[trailing] load_be_active failed: %s", exc)
        return set()


def mark_break_even_active(db: Any, position_id: str, symbol: str) -> None:
    """Arm break-even for *position_id* (idempotent)."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, be_active, be_triggered_at, updated_at)
            VALUES (?, ?, 1, datetime('now','utc'), datetime('now','utc'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                be_active = 1,
                be_triggered_at = COALESCE(position_state.be_triggered_at, excluded.be_triggered_at),
                updated_at = excluded.updated_at
        """, (position_id, symbol))
    except Exception as exc:
        logger.warning("[trailing] mark_break_even_active(%s) failed: %s", position_id, exc)


def cleanup_position_state(db: Any, live_position_ids: set[str]) -> int:
    """Remove state rows for positions that no longer exist. Returns count."""
    if db is None:
        return 0
    try:
        _ensure_position_state_table(db)
        rows = db.fetchall("SELECT position_id FROM position_state")
        stale = [r[0] for r in rows if r[0] not in live_position_ids]
        for pid in stale:
            db.execute("DELETE FROM position_state WHERE position_id = ?", (pid,))
        return len(stale)
    except Exception as exc:
        logger.warning("[trailing] cleanup_position_state failed: %s", exc)
        return 0


def evaluate_trailing(
    positions: list[dict],
    regime: str = 'NORMAL',
    db: Any = None,
) -> list[TrailingAction]:
    """Evaluate all positions for trailing stop opportunities.

    Args:
        positions: Raw positions from eToro API (clientPortfolio.positions)
        regime: Current trading regime
        db: DB handle (bot.db.connection.DB) for level-taken persistence.
            None → stateless fallback (every level fires; only for tests).
    Returns:
        List of TrailingActions to execute
    """
    pos_ids = [str(p.get('positionID', '')) for p in positions if p.get('positionID')]
    levels_taken = load_levels_taken(db, pos_ids)
    be_armed = load_be_active(db, pos_ids)

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
            # fix/break-even-enforcement: eine BE-armierte Position (war
            # schon ≥ +5%) darf nicht zurück unter Entry fallen — Full
            # Close am Floor, statt bis zum -3%-Hard-SL durchzurutschen.
            if pos_id in be_armed and pnl_pct <= BREAK_EVEN_FLOOR_PCT:
                actions.append(TrailingAction(
                    action='BE_CLOSE',
                    symbol=symbol,
                    position_id=pos_id,
                    pnl_pct=pnl_pct,
                    reason=(
                        f"Break-Even-Enforcement: war ≥ +{BREAK_EVEN_TRIGGER_PCT:.0f}%, "
                        f"jetzt {pnl_pct:+.1f}% ≤ +{BREAK_EVEN_FLOOR_PCT:.1f}% Floor — Full Close"
                    ),
                    instrument_id=instrument_id,
                    amount_usd=amount,
                    open_rate=open_rate,
                ))
            continue  # No profit-taking below the BE trigger

        taken = levels_taken.get(pos_id, set())

        # Fälliges, noch NICHT genommenes Level suchen — das NIEDRIGSTE zuerst
        # (Bible-Reihenfolge: 15 → 25 → 50; springt der PnL direkt auf +30%,
        # nimmt dieser Zyklus Level 15, der nächste 5-min-Zyklus Level 25).
        pending = [
            lv for lv in sorted(PROFIT_TAKE_LEVELS, key=lambda x: x['threshold'])
            if pnl_pct >= lv['threshold'] and lv['threshold'] not in taken
        ]
        if pending:
            level = pending[0]
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
                level_threshold=level['threshold'],
            ))
        else:
            # Only break-even (5-15% range, or all due levels already taken)
            actions.append(TrailingAction(
                action='BREAK_EVEN',
                symbol=symbol,
                position_id=pos_id,
                pnl_pct=pnl_pct,
                reason=f"+{pnl_pct:.1f}% ≥ +{BREAK_EVEN_TRIGGER_PCT:.0f}% — break-even tracked",
                instrument_id=instrument_id,
            ))
    return actions


def _post_closed_embed(symbol: str, position_id: str, reason: str, pnl_pct: float = 0.0) -> None:
    """Best-effort Discord embed for a (partial) close. Never raises."""
    try:
        from pathlib import Path as _Path
        import importlib.util
        _embed_file = str(_Path(__file__).resolve().parent.parent / 'discord_embeds.py')
        spec = importlib.util.spec_from_file_location('discord_embeds', _embed_file)
        de = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(de)
        if hasattr(de, 'post_position_closed_embed'):
            de.post_position_closed_embed(
                symbol=symbol,
                amount_usd=0,
                position_id=position_id,
                pnl_pct=pnl_pct,
                reason=reason,
            )
    except Exception:
        pass


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
    db: Any = None,
) -> dict:
    """Execute trailing stop actions.

    PARTIAL_CLOSE: Closes a percentage of the position via API. The fired
    level is persisted via mark_level_taken() as soon as eToro ACCEPTS the
    order (not only after verification) — if the close settles slowly, the
    next 5-min cycle must NOT fire the same level again (double-sell risk
    outweighs the risk of losing one level to a never-executed order).
    BREAK_EVEN: arms persistent break-even state (position was ≥ +5%).
    BE_CLOSE: full close because an armed position fell back to entry —
    executes in ALL regimes (loss protection, not profit-taking).
    """
    import time
    stats = {'partial_closes': 0, 'break_evens': 0, 'be_closes': 0, 'errors': []}

    for action in actions:
        if action.action == 'BREAK_EVEN':
            # fix/break-even-enforcement: persist armed state — the next
            # cycles enforce the entry floor via BE_CLOSE (eToro has no
            # SL-update endpoint, so this is software enforcement).
            mark_break_even_active(db, action.position_id, action.symbol)
            print(f'[trailing] BREAK-EVEN armed: {action.symbol} {action.pnl_pct:+.1f}% — floor at entry (+{BREAK_EVEN_FLOOR_PCT:.1f}%)')
            stats['break_evens'] += 1
            continue

        if action.action == 'BE_CLOSE':
            # Loss protection — runs in ALL regimes (unlike profit-taking).
            print(f'[trailing] BE_CLOSE: {action.symbol} {action.pnl_pct:+.1f}% — {action.reason}')
            if dry_run:
                stats['be_closes'] += 1
                continue
            try:
                result = client.close_position(
                    position_id=action.position_id,
                    instrument_id=action.instrument_id,
                )
                if result:
                    verified, detail = verify_full_close(
                        client, action.instrument_id, action.position_id
                    )
                    if verified:
                        logger.info('[trailing] BE_CLOSE verified: %s', detail)
                        stats['be_closes'] += 1
                        _post_closed_embed(
                            action.symbol, action.position_id,
                            f'Break-Even-Schutz: {action.reason}',
                            pnl_pct=action.pnl_pct,
                        )
                    else:
                        logger.warning('[trailing] BE_CLOSE unverified: %s', detail)
                        stats['errors'].append(f'{action.symbol}: BE_CLOSE unverified — {detail}')
                else:
                    stats['errors'].append(
                        f'{action.symbol}: BE_CLOSE close_position() returned empty/falsy result'
                    )
                time.sleep(0.5)
            except Exception as e:
                msg = f'{action.symbol}: BE_CLOSE API call failed — {e}'
                logger.error('[trailing] %s', msg)
                stats['errors'].append(msg)
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
                    # Level SOFORT als genommen markieren (Order wurde von
                    # eToro akzeptiert) — verhindert Endlos-Feuer desselben
                    # Levels im nächsten 5-min-Zyklus, selbst wenn die
                    # Verifikation unten wegen Settlement-Latenz fehlschlägt.
                    if action.level_threshold > 0:
                        mark_level_taken(db, action.position_id, action.symbol,
                                         action.level_threshold)
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
                    _post_closed_embed(
                        action.symbol, action.position_id,
                        f'Profit-Taking: {action.reason}'
                        + ('' if verified else ' [UNVERIFIED — siehe Log]'),
                        pnl_pct=action.pnl_pct,
                    )
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
