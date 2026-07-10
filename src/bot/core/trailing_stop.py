#!/usr/bin/env python3
"""Trailing Stop Manager — Trading Bible V5.

Monitors open positions for profit-taking opportunities.
Runs inside Risk Worker after SL enforcement.

Note: eToro has no SL-update endpoint. Break-even enforcement
requires Close+Reopen (blocked in DEFENSIVE/CRITICAL).
Partial profit-taking uses units-based close (see EToroClient.close_position).
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Profit-Taking Thresholds (Trading Bible V5) ──────────────────────────────
# fix/be-trigger-lowered: war 5.0. Bei SL=3% blieb eine Position bis +5%
# vollstaendig ungeschuetzt und konnte von +4.9% direkt auf -3% durchrutschen,
# bevor ueberhaupt ein Boden eingezogen wurde. 3.0 = Position muss sich weiter
# in unsere Richtung bewegt haben als das SL riskiert, bevor wir sie sichern.
BREAK_EVEN_TRIGGER_PCT = 3.0    # +3% PnL → move SL to entry (software tracking)
# fix/break-even-enforcement: Schwelle, unter die eine BE-armierte Position
# nicht zurückfallen darf. Leicht über 0, damit Spread/Fees den Close nicht
# in einen Mini-Verlust drehen. eToro hat kein SL-Update-Endpoint, daher
# Software-Enforcement: BE aktiv + PnL ≤ Floor → Full Close.
BREAK_EVEN_FLOOR_PCT = 0.3

# Fixed fallback ladder — used when an instrument has no ATR% on file yet
# (fresh instrument, data_worker hasn't run a cycle for it). Same values as
# the original Trading Bible V5 ladder.
PROFIT_TAKE_LEVELS = [
    {'threshold': 15.0, 'close_pct': 20},   # +15% → close 20% of position
    {'threshold': 25.0, 'close_pct': 20},   # +25% → close another 20%
    {'threshold': 50.0, 'close_pct': 30},   # +50% → close 30%
]

# fix/atr-adaptive-profit-levels: ein Blue-Chip (ATR ~1-1.5%) erreicht real
# selten ein flaches +15%; eine Crypto/High-Beta-Position (ATR ~4-6%) durch-
# schlaegt +50% oft als reines Intraday-Rauschen, ohne dass es einen echten
# Trend bedeutet. Die Level werden daher als ATR%-Vielfache skaliert statt
# fix — je Position EINMAL beim ersten Erreichen der Gewinnzone berechnet und
# in position_state eingefroren (siehe load_profit_levels/save_profit_levels),
# damit ein spaeter driftender ATR-Wert nie ein bereits genommenes Level neu
# triggert (Doppel-Verkaufs-Risiko).
ATR_PROFIT_LEVELS = [
    {'atr_mult': 6.0,  'close_pct': 20, 'min_pct': 6.0,  'max_pct': 30.0},
    {'atr_mult': 10.0, 'close_pct': 20, 'min_pct': 10.0, 'max_pct': 50.0},
    {'atr_mult': 18.0, 'close_pct': 30, 'min_pct': 18.0, 'max_pct': 90.0},
]


# ── Dynamic Quick-Profit (Stufe 1) ────────────────────────────────────────────
# Zwei Mechaniken, beide auf der LIVE-PnL jedes risk_worker-Zyklus (~5 min) —
# kein Intraday-Feed noetig, greift daher sofort auch fuer Bestandspositionen.
#
# ① MOMENTUM-FADE (universell): trackt das PnL-Hoch je Position (peak_pnl_pct).
#    Baut eine Position Gewinn auf und gibt ihn wieder ab, wird EINMALIG ein
#    Teil realisiert + Break-Even auf dem Rest armiert. Fuellt genau die Luecke
#    zwischen BE-Floor (+0.3%) und der ersten Profit-Stufe (+6%), die heute
#    voellig ungeschuetzt ist.
# ② SCALP-TIER (opt-in per strategy='scalp'): eine sehr fruehe erste Profit-
#    Stufe (ATR×2, clamp [2%,5%]), damit ein bewusst kurzfristiger Trade schnell
#    einen Teilgewinn sichert, statt auf die Swing-Leiter (+6/+10/+18%) zu warten.
MOMENTUM_FADE_ENABLED = True
MOMENTUM_ARM_PCT = 2.0          # Peak muss dieses PnL erreichen, bevor Fade-Schutz armiert
MOMENTUM_RETRACE_FRAC = 0.40    # Rueckgabe dieses Anteils vom Peak → feuert
MOMENTUM_MIN_LOCK_PCT = 1.0     # unter diesem aktuellen PnL nie feuern (BE/SL-Revier)
MOMENTUM_FADE_CLOSE_PCT = 50.0  # % der Position, das ein Fade realisiert

SCALP_ENABLED = True
SCALP_ATR_MULT = 2.0
SCALP_MIN_PCT = 2.0
SCALP_MAX_PCT = 5.0
SCALP_CLOSE_PCT = 25


def apply_config(cfg: dict) -> None:
    """Wire the `trailing:` config block into module thresholds (idempotent).

    Called once per risk_worker run before evaluate_trailing(). Missing keys
    keep the conservative code defaults above.
    """
    global MOMENTUM_FADE_ENABLED, MOMENTUM_ARM_PCT, MOMENTUM_RETRACE_FRAC
    global MOMENTUM_MIN_LOCK_PCT, MOMENTUM_FADE_CLOSE_PCT
    global SCALP_ENABLED, SCALP_ATR_MULT, SCALP_MIN_PCT, SCALP_MAX_PCT, SCALP_CLOSE_PCT
    t = ((cfg or {}).get('trailing') or {})
    mf = (t.get('momentum_fade') or {})
    if 'enabled' in mf:
        MOMENTUM_FADE_ENABLED = bool(mf['enabled'])
    MOMENTUM_ARM_PCT = float(mf.get('arm_pct', MOMENTUM_ARM_PCT))
    MOMENTUM_RETRACE_FRAC = float(mf.get('retrace_frac', MOMENTUM_RETRACE_FRAC))
    MOMENTUM_MIN_LOCK_PCT = float(mf.get('min_lock_pct', MOMENTUM_MIN_LOCK_PCT))
    MOMENTUM_FADE_CLOSE_PCT = float(mf.get('close_pct', MOMENTUM_FADE_CLOSE_PCT))
    sc = (t.get('scalp') or {})
    if 'enabled' in sc:
        SCALP_ENABLED = bool(sc['enabled'])
    SCALP_ATR_MULT = float(sc.get('atr_mult', SCALP_ATR_MULT))
    SCALP_MIN_PCT = float(sc.get('min_pct', SCALP_MIN_PCT))
    SCALP_MAX_PCT = float(sc.get('max_pct', SCALP_MAX_PCT))
    SCALP_CLOSE_PCT = float(sc.get('close_pct', SCALP_CLOSE_PCT))


def should_momentum_fade(pnl_pct: float, peak_pnl_pct: float, already_faded: bool) -> bool:
    """Pure decision: has a built-up gain faded enough to lock a partial?

    True iff momentum-fade is enabled, the position isn't already faded, the
    peak reached the arm threshold, current PnL is still a real gain (≥ min-lock)
    AND has given back at least RETRACE_FRAC of the peak. Kept pure/side-effect-
    free so the trigger can be unit-tested without a DB or the eToro API.
    """
    if not MOMENTUM_FADE_ENABLED or already_faded:
        return False
    if peak_pnl_pct < MOMENTUM_ARM_PCT:
        return False
    if pnl_pct < MOMENTUM_MIN_LOCK_PCT:
        return False
    floor = peak_pnl_pct * (1.0 - MOMENTUM_RETRACE_FRAC)
    return pnl_pct <= floor


def _scalp_rung(atr_pct: float | None) -> dict:
    """First, early profit rung for a scalp-tagged position (ATR-scaled)."""
    base = (atr_pct if atr_pct and atr_pct > 0 else SCALP_MIN_PCT) * SCALP_ATR_MULT
    threshold = min(max(base, SCALP_MIN_PCT), SCALP_MAX_PCT)
    return {'threshold': round(threshold, 2), 'close_pct': SCALP_CLOSE_PCT}


def _resolve_profit_levels(atr_pct: float | None, strategy: str = 'swing') -> list[dict]:
    """Return the profit-take ladder to use for a position.

    ATR-scaled when *atr_pct* is known (> 0), else the fixed fallback ladder.
    A scalp-tagged position gets an extra early first rung (deduped/sorted so a
    scalp rung never sits at or above the first swing rung).
    """
    if not atr_pct or atr_pct <= 0:
        base = list(PROFIT_TAKE_LEVELS)
    else:
        base = []
        for lv in ATR_PROFIT_LEVELS:
            threshold = min(max(lv['atr_mult'] * atr_pct, lv['min_pct']), lv['max_pct'])
            base.append({'threshold': round(threshold, 2), 'close_pct': lv['close_pct']})
    if strategy == 'scalp' and SCALP_ENABLED:
        scalp = _scalp_rung(atr_pct)
        # Only prepend if it's genuinely earlier than the first swing rung.
        if not base or scalp['threshold'] < base[0]['threshold']:
            base = [scalp] + base
    return base

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
    # Migration for existing installs (idempotent). Each ALTER is isolated so
    # one already-existing column never blocks the others.
    for ddl in (
        "ALTER TABLE position_state ADD COLUMN profit_levels_json TEXT",
        "ALTER TABLE position_state ADD COLUMN peak_pnl_pct REAL NOT NULL DEFAULT 0",
        "ALTER TABLE position_state ADD COLUMN momentum_faded INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE position_state ADD COLUMN strategy TEXT NOT NULL DEFAULT 'swing'",
        # fix/sell-exit-cooldown: Zeitstempel des letzten SELL-Exits pro
        # Position — verhindert das Endlos-Zerlegen einer Position, wenn
        # data_worker die Überhitzungs-Bedingung jeden Zyklus neu signalisiert
        # (KTA.DE-Vorfall 2026-07-06: 39 Signale, Position in 50%-Schritten
        # von ~$500 auf $14.75 zerlegt).
        "ALTER TABLE position_state ADD COLUMN sell_exit_at TEXT",
    ):
        try:
            db.execute(ddl)
        except Exception:
            pass  # column already exists


def load_atr_pct(db: Any, instrument_ids: list[int]) -> dict[int, float]:
    """Return {instrument_id: atr_pct} for the given ids (data_worker-populated)."""
    ids = [i for i in instrument_ids if i]
    if db is None or not ids:
        return {}
    try:
        placeholders = ",".join("?" * len(ids))
        rows = db.fetchall(
            f"SELECT instrument_id, atr_pct FROM instruments "
            f"WHERE instrument_id IN ({placeholders}) AND atr_pct IS NOT NULL",
            ids,
        )
        return {int(row[0]): float(row[1]) for row in rows}
    except Exception as exc:
        logger.warning("[trailing] load_atr_pct failed: %s", exc)
        return {}


def load_profit_levels(db: Any, position_ids: list[str]) -> dict[str, list[dict]]:
    """Return the frozen profit-take ladder already snapshot for each position."""
    if db is None or not position_ids:
        return {}
    try:
        _ensure_position_state_table(db)
        placeholders = ",".join("?" * len(position_ids))
        rows = db.fetchall(
            f"SELECT position_id, profit_levels_json FROM position_state "
            f"WHERE position_id IN ({placeholders})",
            list(position_ids),
        )
        result: dict[str, list[dict]] = {}
        for pid, levels_json in rows:
            if levels_json:
                try:
                    result[pid] = json.loads(levels_json)
                except Exception:
                    continue
        return result
    except Exception as exc:
        logger.warning("[trailing] load_profit_levels failed: %s", exc)
        return {}


def save_profit_levels(db: Any, position_id: str, symbol: str, levels: list[dict]) -> None:
    """Freeze *levels* for *position_id* — first write wins (never overwrite)."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        levels_json = json.dumps(levels)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, profit_levels_json, updated_at)
            VALUES (?, ?, ?, datetime('now','utc'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                profit_levels_json = COALESCE(position_state.profit_levels_json, excluded.profit_levels_json),
                updated_at = excluded.updated_at
        """, (position_id, symbol, levels_json))
    except Exception as exc:
        logger.warning("[trailing] save_profit_levels(%s) failed: %s", position_id, exc)


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


def load_position_dynamic(db: Any, position_ids: list[str]) -> dict[str, dict]:
    """Return {position_id: {'peak': float, 'faded': bool, 'strategy': str}}.

    Powers momentum-fade (peak/faded) and the scalp ladder (strategy). Positions
    with no state row default to peak 0 / not-faded / 'swing' at the call site.
    """
    if db is None or not position_ids:
        return {}
    try:
        _ensure_position_state_table(db)
        placeholders = ",".join("?" * len(position_ids))
        rows = db.fetchall(
            f"SELECT position_id, peak_pnl_pct, momentum_faded, strategy "
            f"FROM position_state WHERE position_id IN ({placeholders})",
            list(position_ids),
        )
        result: dict[str, dict] = {}
        for pid, peak, faded, strat in rows:
            result[pid] = {
                'peak': float(peak or 0.0),
                'faded': bool(faded),
                'strategy': (strat or 'swing'),
            }
        return result
    except Exception as exc:
        logger.warning("[trailing] load_position_dynamic failed: %s", exc)
        return {}


def update_peak_pnl(db: Any, position_id: str, symbol: str, pnl_pct: float) -> None:
    """Raise the stored PnL high-water-mark for *position_id* (never lowers it)."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, peak_pnl_pct, updated_at)
            VALUES (?, ?, ?, datetime('now','utc'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                peak_pnl_pct = MAX(position_state.peak_pnl_pct, excluded.peak_pnl_pct),
                updated_at = excluded.updated_at
        """, (position_id, symbol, float(pnl_pct)))
    except Exception as exc:
        logger.warning("[trailing] update_peak_pnl(%s) failed: %s", position_id, exc)


def mark_momentum_faded(db: Any, position_id: str, symbol: str) -> None:
    """Persist that momentum-fade has fired once for *position_id* (one-shot)."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, momentum_faded, updated_at)
            VALUES (?, ?, 1, datetime('now','utc'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                momentum_faded = 1,
                updated_at = excluded.updated_at
        """, (position_id, symbol))
    except Exception as exc:
        logger.warning("[trailing] mark_momentum_faded(%s) failed: %s", position_id, exc)


def set_strategy(db: Any, position_id: str, symbol: str, strategy: str) -> None:
    """Tag *position_id* as 'scalp' or 'swing' (drives the early scalp rung).

    Resets the frozen profit_levels_json so the ladder re-resolves under the new
    strategy on the next cycle — otherwise a position frozen as 'swing' would
    never gain the early scalp rung when retro-tagged. Levels already TAKEN stay
    recorded, so re-resolving cannot re-fire a rung that was already realized.
    """
    if db is None or not position_id or strategy not in ('scalp', 'swing'):
        return
    try:
        _ensure_position_state_table(db)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, strategy, profit_levels_json, updated_at)
            VALUES (?, ?, ?, NULL, datetime('now','utc'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                strategy = excluded.strategy,
                profit_levels_json = NULL,
                updated_at = excluded.updated_at
        """, (position_id, symbol, strategy))
    except Exception as exc:
        logger.warning("[trailing] set_strategy(%s,%s) failed: %s", position_id, strategy, exc)


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
    frozen_levels = load_profit_levels(db, pos_ids)
    dynamic = load_position_dynamic(db, pos_ids)
    instrument_ids = [
        int(p.get('instrumentID') or p.get('instrumentId') or 0) for p in positions
    ]
    atr_by_instrument = load_atr_pct(db, instrument_ids)

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

        # ── Momentum-Fade state: peak high-water-mark je Position ─────────────
        meta = dynamic.get(pos_id, {})
        prev_peak = float(meta.get('peak', 0.0))
        faded = bool(meta.get('faded', False))
        strategy = meta.get('strategy', 'swing')
        peak = max(prev_peak, pnl_pct)
        if peak > prev_peak:
            update_peak_pnl(db, pos_id, symbol, pnl_pct)  # SQL raises the mark

        atr_pct = atr_by_instrument.get(instrument_id)
        # Scalp-Positionen duerfen ihren fruehen ersten Rung unterhalb des
        # normalen BE-Trigger-Gates (3%) nehmen — sonst waere ein Scalp-Rung
        # < 3% praktisch tot. Swing bleibt exakt beim bisherigen 3%-Gate.
        is_scalp = strategy == 'scalp' and SCALP_ENABLED
        effective_gate = BREAK_EVEN_TRIGGER_PCT
        if is_scalp:
            effective_gate = min(effective_gate, _scalp_rung(atr_pct)['threshold'])

        def _fade_action() -> TrailingAction:
            return TrailingAction(
                action='MOMENTUM_FADE',
                symbol=symbol,
                position_id=pos_id,
                pnl_pct=pnl_pct,
                reason=(
                    f"Momentum-Fade: Peak +{peak:.1f}% → jetzt {pnl_pct:+.1f}% "
                    f"(≥{MOMENTUM_RETRACE_FRAC*100:.0f}% abgegeben) — {MOMENTUM_FADE_CLOSE_PCT:.0f}% sichern + BE"
                ),
                close_pct=MOMENTUM_FADE_CLOSE_PCT,
                instrument_id=instrument_id,
                amount_usd=amount,
                open_rate=open_rate,
            )

        if pnl_pct < effective_gate:
            # fix/break-even-enforcement: eine BE-armierte Position (war
            # schon ≥ BREAK_EVEN_TRIGGER_PCT) darf nicht zurück unter Entry
            # fallen — Full Close am Floor, statt bis zum Hard-SL durchzurutschen.
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
            # Quick-profit lock in the +min_lock..+BE_trigger gap that the
            # ladder never reaches — the whole point of momentum-fade.
            elif should_momentum_fade(pnl_pct, peak, faded):
                actions.append(_fade_action())
            continue  # No structured profit-taking below the BE trigger

        taken = levels_taken.get(pos_id, set())

        # ATR-adaptive Ladder: einmal beim ersten Erreichen der Gewinnzone
        # bestimmt und in position_state eingefroren (siehe save_profit_levels),
        # damit ein spaeter aktualisierter ATR-Wert das Level fuer diese
        # Position NICHT mehr verschiebt — sonst koennte ein bereits als
        # "genommen" markiertes Level bei naechster Berechnung einen leicht
        # anderen threshold ergeben und erneut feuern (Doppel-Verkauf).
        profit_levels = frozen_levels.get(pos_id)
        if profit_levels is None:
            profit_levels = _resolve_profit_levels(atr_pct, strategy)
            save_profit_levels(db, pos_id, symbol, profit_levels)

        # Fälliges, noch NICHT genommenes Level suchen — das NIEDRIGSTE zuerst
        # (Bible-Reihenfolge: springt der PnL direkt auf das oberste Level,
        # nimmt dieser Zyklus das unterste, der naechste 5-min-Zyklus das naechste).
        pending = [
            lv for lv in sorted(profit_levels, key=lambda x: x['threshold'])
            if pnl_pct >= lv['threshold'] and lv['threshold'] not in taken
        ]
        if pending:
            # Structured ladder profit-taking takes priority over the fade.
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
        elif should_momentum_fade(pnl_pct, peak, faded):
            # No ladder level due, but a built-up gain is fading back → lock it.
            actions.append(_fade_action())
        elif pnl_pct >= BREAK_EVEN_TRIGGER_PCT:
            # Only break-even (BE-trigger..first-rung range, or all due levels taken)
            actions.append(TrailingAction(
                action='BREAK_EVEN',
                symbol=symbol,
                position_id=pos_id,
                pnl_pct=pnl_pct,
                reason=f"+{pnl_pct:.1f}% ≥ +{BREAK_EVEN_TRIGGER_PCT:.0f}% — break-even tracked",
                instrument_id=instrument_id,
            ))
        # else: scalp position in [scalp_gate, BE_trigger) with its rung already
        # taken and no fade due → nothing to do this cycle.
    return actions


# Modul-Cache für discord_embeds — vorher wurde das ~1700-Zeilen-Modul bei
# JEDEM Close per importlib neu von der Platte geladen und ausgeführt.
# False = Laden bereits fehlgeschlagen (nicht erneut versuchen).
_DISCORD_EMBEDS_CACHE: Any = None


def _get_discord_embeds() -> Any:
    """Load discord_embeds once per process. Returns module or None."""
    global _DISCORD_EMBEDS_CACHE
    if _DISCORD_EMBEDS_CACHE is not None:
        return _DISCORD_EMBEDS_CACHE or None
    try:
        from pathlib import Path as _Path
        import importlib.util
        _embed_file = str(_Path(__file__).resolve().parent.parent / 'discord_embeds.py')
        spec = importlib.util.spec_from_file_location('discord_embeds', _embed_file)
        de = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(de)
        _DISCORD_EMBEDS_CACHE = de
        return de
    except Exception:
        _DISCORD_EMBEDS_CACHE = False
        return None


def _post_closed_embed(symbol: str, position_id: str, reason: str,
                       pnl_pct: float = 0.0, amount_usd: float = 0.0,
                       close_pct: float = 100.0) -> None:
    """Best-effort Discord embed for a (partial) close. Never raises.

    fix/embed-real-amounts (KTA.DE 2026-07-06): amount_usd war hartkodiert 0 —
    jedes Close-Embed zeigte '$0.00 Betrag / $+0.00 Gewinn' und erweckte den
    Eindruck, die Position existiere nicht. Jetzt: tatsächlich geschlossener
    Anteil (amount × close_pct) + daraus abgeleiteter realisierter Gewinn.
    """
    try:
        closed_amount = float(amount_usd) * float(close_pct) / 100.0
        pnl_usd = closed_amount * float(pnl_pct) / 100.0
        de = _get_discord_embeds()
        if de is not None and hasattr(de, 'post_position_closed_embed'):
            de.post_position_closed_embed(
                symbol=symbol,
                amount_usd=closed_amount,
                position_id=position_id,
                pnl_usd=pnl_usd,
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
    max_attempts: int = 8,
    initial_wait_s: float = 3.0,
) -> tuple[bool, str, dict | None]:
    """Poll until a position after a full-close has actually disappeared,
    instead of trusting the immediate 200 response (which only means the
    order was accepted). For SL-close (risk_worker) and concentration-close.

    Returns (confirmed, detail, pnl_data) where pnl_data is a dict with
    exit_price, pnl_usd, pnl_pct if available, else None.
    
    fix/sl-close-embed: increased max_attempts from 6 to 8 for ~165s total
    (was 105s) to handle HK/ASIA markets with slower API response times.
    """
    import time as _time

    waited = 0.0
    final_pnl_data = None
    
    for attempt in range(max_attempts):
        wait_s = min(initial_wait_s * (2 ** attempt), 30)
        _time.sleep(wait_s)
        waited += wait_s
        pos = _find_position(client, instrument_id, position_id)
        if pos is None:
            # Position gone — close confirmed. Try to get final PnL from the
            # last known state (caller should pass it). Return None here;
            # risk_worker fills it from the pre-close snapshot.
            return True, f"Full-close CONFIRMED after {waited:.0f}s", final_pnl_data
    return False, f"Full-close NOT confirmed after {waited:.0f}s — position may still be open", None


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
    BREAK_EVEN: arms persistent break-even state (position was ≥ BREAK_EVEN_TRIGGER_PCT).
    BE_CLOSE: full close because an armed position fell back to entry —
    executes in ALL regimes (loss protection, not profit-taking).
    """
    import time
    stats = {'partial_closes': 0, 'break_evens': 0, 'be_closes': 0,
             'momentum_fades': 0, 'errors': []}

    for action in actions:
        if action.action == 'BREAK_EVEN':
            # fix/break-even-enforcement: persist armed state — the next
            # cycles enforce the entry floor via BE_CLOSE (eToro has no
            # SL-update endpoint, so this is software enforcement).
            mark_break_even_active(db, action.position_id, action.symbol)
            logger.debug('[trailing] BREAK-EVEN armed: %s %+.1f%% — floor at entry (+%.1f%%)', action.symbol, action.pnl_pct, BREAK_EVEN_FLOOR_PCT)
            stats['break_evens'] += 1
            continue

        if action.action == 'BE_CLOSE':
            # Loss protection — runs in ALL regimes (unlike profit-taking).
            logger.info('[trailing] BE_CLOSE: %s %+.1f%% — %s', action.symbol, action.pnl_pct, action.reason)
            if dry_run:
                stats['be_closes'] += 1
                continue
            try:
                result = client.close_position(
                    position_id=action.position_id,
                    instrument_id=action.instrument_id,
                )
                if result:
                    verified, detail, _pnl_data = verify_full_close(
                        client, action.instrument_id, action.position_id
                    )
                    if verified:
                        logger.info('[trailing] BE_CLOSE verified: %s', detail)
                        stats['be_closes'] += 1
                        _post_closed_embed(
                            action.symbol, action.position_id,
                            f'Break-Even-Schutz: {action.reason}',
                            pnl_pct=action.pnl_pct,
                            amount_usd=action.amount_usd,   # Full Close
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

        if action.action in ('PARTIAL_CLOSE', 'MOMENTUM_FADE'):
            is_fade = action.action == 'MOMENTUM_FADE'
            # Structured ladder-taking (PARTIAL_CLOSE) is suppressed in stressed
            # regimes ("let winners run"). MOMENTUM_FADE is protective de-risking
            # — locking a gain that is actively fading — so it runs in ALL
            # regimes, like BE_CLOSE and SELL-exits.
            if not is_fade and regime in ('DEFENSIVE', 'CRITICAL'):
                logger.debug('[trailing] PARTIAL_CLOSE skipped in %s: %s %+.1f%%', regime, action.symbol, action.pnl_pct)
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

            logger.info('[trailing] %s %s%%: %s %+.1f%% — %s (units=%.6f)', action.action, action.close_pct, action.symbol, action.pnl_pct, action.reason, units_to_deduct)

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
                    # State SOFORT persistieren (Order wurde von eToro
                    # akzeptiert) — verhindert Endlos-Feuer im nächsten
                    # 5-min-Zyklus, selbst wenn die Verifikation unten wegen
                    # Settlement-Latenz fehlschlägt.
                    if is_fade:
                        # One-shot: nie erneut faden; Rest per BE absichern.
                        mark_momentum_faded(db, action.position_id, action.symbol)
                        mark_break_even_active(db, action.position_id, action.symbol)
                    elif action.level_threshold > 0:
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
                        if is_fade:
                            stats['momentum_fades'] += 1
                    else:
                        logger.warning('[trailing] %s', detail)
                        stats['errors'].append(detail)
                    # Post Discord embed
                    _post_closed_embed(
                        action.symbol, action.position_id,
                        f'Profit-Taking: {action.reason}'
                        + ('' if verified else ' [UNVERIFIED — siehe Log]'),
                        pnl_pct=action.pnl_pct,
                        amount_usd=action.amount_usd,
                        close_pct=action.close_pct,
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
