#!/usr/bin/env python3
"""SELL-Signal-Exits — Trading Bible V4 SELL Rule 1.

fix/sell-signal-exits: generate_signal() produziert SELL-Signale
(BB-Upper + RSI > 70 → Überhitzung), data_worker speichert sie — aber
kein Worker konsumierte sie: signal_worker filtert SELL explizit raus,
Exits passierten ausschließlich über SL und Profit-Level. Ein
Überhitzungs-Exit auf gehaltene Positionen fand nie statt.

Dieses Modul schließt die Lücke: der risk_worker matcht FRESH
SELL/OVERBOUGHT-Signale gegen offene Positionen und realisiert per
Partial-Close (SELL_EXIT_CLOSE_PCT) Gewinne. Konsumierte Signale werden
CONSUMED markiert — kein Endlos-Feuer über den 6h-TTL.

Regeln:
- Nur PROFITABLE Positionen (PnL > 0) werden de-riskt — ein
  Überhitzungssignal auf einer Verlust-Position erzwingt keinen Verkauf
  (dafür sind SL/BE-Enforcement zuständig).
- Pro Signal wird das GRÖSSTE profitable Fragment des Instruments
  teilgeschlossen (eToro schließt pro Position, nicht pro Instrument).
- Läuft in ALLEN Regimes — Überhitzungs-Gewinnmitnahme ist De-Risking.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SELL_EXIT_CLOSE_PCT = 50.0   # % der Position, die ein SELL-Signal realisiert
SELL_SIGNAL_MARKERS = ("SELL", "OVERBOUGHT")

# fix/sell-exit-cooldown (KTA.DE-Vorfall 2026-07-06): CONSUMED-Markierung
# allein reicht NICHT — data_worker erzeugt bei anhaltender Überhitzung auf
# Tages-Bars alle 5 min ein NEUES FRESH-Signal (39 Stück an einem Vormittag),
# und jeder risk_worker-Zyklus halbierte die Position erneut. Ein SELL-Exit
# ist EIN De-Risking pro Überhitzungs-Episode: nach einem Exit ist das
# Instrument für SELL_EXIT_COOLDOWN_H gesperrt (persistiert in
# position_state.sell_exit_at, überlebt Worker-Neustarts).
SELL_EXIT_COOLDOWN_H = 24.0


def load_blocked_instruments(db: Any, positions: list[dict],
                             cooldown_h: float = SELL_EXIT_COOLDOWN_H) -> set[int]:
    """Instrument-IDs, deren Positionen innerhalb der Cooldown-Frist bereits
    einen SELL-Exit hatten. Fail-open (leere Menge) bei DB-Problemen — die
    CONSUMED-Markierung bleibt als erste Verteidigungslinie bestehen."""
    if db is None or not positions:
        return set()
    pos_to_instr: dict[str, int] = {}
    for p in positions:
        pid = str(p.get("positionID") or p.get("positionId") or "")
        iid = p.get("instrumentID") or p.get("instrumentId")
        if pid and iid is not None:
            pos_to_instr[pid] = int(iid)
    if not pos_to_instr:
        return set()
    try:
        placeholders = ",".join("?" * len(pos_to_instr))
        rows = db.fetchall(
            f"SELECT position_id FROM position_state "
            f"WHERE position_id IN ({placeholders}) "
            f"  AND sell_exit_at IS NOT NULL "
            f"  AND sell_exit_at > datetime('now', ?, 'utc')",
            list(pos_to_instr) + [f"-{cooldown_h:.4f} hours"],
        )
        return {pos_to_instr[r[0]] for r in rows if r[0] in pos_to_instr}
    except Exception as exc:
        logger.warning("[sell_exits] load_blocked_instruments fehlgeschlagen: %s", exc)
        return set()


def mark_sell_exit(db: Any, position_id: str, symbol: str) -> None:
    """Persistiert den SELL-Exit-Zeitpunkt (startet die Cooldown-Frist)."""
    if db is None or not position_id:
        return
    try:
        from bot.core.trailing_stop import _ensure_position_state_table
        _ensure_position_state_table(db)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, sell_exit_at, updated_at)
            VALUES (?, ?, datetime('now','utc'), datetime('now','utc'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                sell_exit_at = excluded.sell_exit_at,
                updated_at = excluded.updated_at
        """, (position_id, symbol))
    except Exception as exc:
        logger.warning("[sell_exits] mark_sell_exit(%s) fehlgeschlagen: %s", position_id, exc)


@dataclass
class SellExitAction:
    signal_id: int
    symbol: str
    position_id: str
    instrument_id: int
    amount_usd: float
    open_rate: float
    pnl_pct: float
    close_pct: float
    reason: str


def is_sell_signal(signal: dict) -> bool:
    """True wenn signal_type einen SELL/OVERBOUGHT-Marker enthält."""
    sig_type = (signal.get("signal_type") or "").upper()
    return any(marker in sig_type for marker in SELL_SIGNAL_MARKERS)


def evaluate_sell_exits(
    fresh_signals: list[dict],
    positions: list[dict],
    blocked_instruments: set[int] | frozenset = frozenset(),
) -> list[SellExitAction]:
    """Match FRESH SELL-Signale gegen offene Positionen (pure Logik).

    Args:
        fresh_signals: SignalRepo.get_fresh()-Zeilen (dicts)
        positions: Raw eToro-Positionen (clientPortfolio.positions)
        blocked_instruments: Instrumente in der SELL-Exit-Cooldown-Frist
            (bereits de-riskt — nicht erneut zerlegen)
    Returns:
        Eine Aktion pro Instrument mit SELL-Signal und profitabler Position.
    """
    # Positionen nach instrument_id gruppieren
    by_instrument: dict[int, list[dict]] = {}
    for pos in positions:
        iid = pos.get("instrumentID") or pos.get("instrumentId")
        if iid is None:
            continue
        by_instrument.setdefault(int(iid), []).append(pos)

    actions: list[SellExitAction] = []
    seen_instruments: set[int] = set()

    for signal in fresh_signals:
        if not is_sell_signal(signal):
            continue
        iid = signal.get("instrument_id")
        if iid is None or int(iid) in seen_instruments:
            continue
        iid = int(iid)
        if iid in blocked_instruments:
            # Cooldown aktiv: diese Episode wurde bereits de-riskt.
            continue

        candidates = []
        for pos in by_instrument.get(iid, []):
            amount = float(pos.get("amount", 0) or 0)
            if amount <= 0:
                continue
            upnl = pos.get("unrealizedPnL") or {}
            pnl_usd = float(upnl.get("pnL", 0)) if isinstance(upnl, dict) else 0.0
            pnl_pct = (pnl_usd / amount) * 100
            if pnl_pct <= 0:
                continue  # Verlust-Positionen: SL/BE-Enforcement zuständig
            candidates.append((amount, pnl_pct, pos))

        if not candidates:
            continue

        # Größtes profitables Fragment de-risken
        amount, pnl_pct, pos = max(candidates, key=lambda c: c[0])
        pos_id = str(pos.get("positionID") or pos.get("positionId") or "")
        if not pos_id:
            continue
        symbol = pos.get("symbol") or str(iid)
        seen_instruments.add(iid)

        actions.append(SellExitAction(
            signal_id=int(signal.get("id") or 0),
            symbol=symbol,
            position_id=pos_id,
            instrument_id=iid,
            amount_usd=amount,
            open_rate=float(pos.get("openRate", 0) or 0),
            pnl_pct=pnl_pct,
            close_pct=SELL_EXIT_CLOSE_PCT,
            reason=(
                f"SELL-Signal ({signal.get('signal_type', '?')}): "
                f"Überhitzung — {SELL_EXIT_CLOSE_PCT:.0f}% Gewinnmitnahme "
                f"bei {pnl_pct:+.1f}%"
            ),
        ))

    return actions


def execute_sell_exits(
    client: Any,
    signal_repo: Any,
    actions: list[SellExitAction],
    dry_run: bool = False,
    db: Any = None,
) -> dict:
    """Execute SELL-Exit-Aktionen mit Verifikation.

    Signal wird CONSUMED markiert sobald eToro die Order AKZEPTIERT —
    Settlement-Latenz darf dasselbe Signal im nächsten 5-min-Zyklus nicht
    erneut feuern lassen (Doppel-Verkauf schlimmer als ein verlorener Exit).
    Bei API-Fehler bleibt das Signal FRESH (Retry im nächsten Lauf).
    """
    import time

    from bot.core.trailing_stop import TrailingAction, _post_closed_embed, _verify_partial_close

    stats = {"closed": 0, "errors": []}

    for action in actions:
        if action.open_rate <= 0:
            msg = f"{action.symbol}: SELL-Exit ohne open_rate — übersprungen"
            logger.warning("[sell_exits] %s", msg)
            stats["errors"].append(msg)
            continue

        total_units = action.amount_usd / action.open_rate
        units_to_deduct = round(total_units * (action.close_pct / 100.0), 8)
        if units_to_deduct <= 0:
            stats["errors"].append(f"{action.symbol}: units_to_deduct <= 0 — übersprungen")
            continue

        logger.info("[sell_exits] %s: %s (units=%.6f)", action.symbol, action.reason, units_to_deduct)

        if dry_run:
            stats["closed"] += 1
            continue

        try:
            result = client.close_position(
                position_id=action.position_id,
                instrument_id=action.instrument_id,
                units_to_deduct=units_to_deduct,
            )
            if not result:
                stats["errors"].append(
                    f"{action.symbol}: close_position() returned empty/falsy result"
                )
                continue

            # Signal SOFORT konsumieren (Order akzeptiert) — kein Endlos-Feuer
            try:
                signal_repo.update_signal_status(action.signal_id, "CONSUMED")
            except Exception as exc:
                logger.warning("[sell_exits] Signal %d CONSUMED-Markierung fehlgeschlagen: %s",
                               action.signal_id, exc)

            # Cooldown SOFORT starten (Order akzeptiert) — auch wenn die
            # Verifikation unten wegen Settlement-Latenz scheitert, darf der
            # nächste 5-min-Zyklus dieselbe Position nicht erneut halbieren.
            mark_sell_exit(db, action.position_id, action.symbol)

            # Verifikation über das bestehende Partial-Close-Polling
            verify_action = TrailingAction(
                action="PARTIAL_CLOSE",
                symbol=action.symbol,
                position_id=action.position_id,
                pnl_pct=action.pnl_pct,
                reason=action.reason,
                close_pct=action.close_pct,
                instrument_id=action.instrument_id,
                amount_usd=action.amount_usd,
                open_rate=action.open_rate,
            )
            verified, detail = _verify_partial_close(client, verify_action)
            if verified:
                logger.info("[sell_exits] %s", detail)
                stats["closed"] += 1
            else:
                logger.warning("[sell_exits] %s", detail)
                stats["errors"].append(detail)

            _post_closed_embed(
                action.symbol, action.position_id,
                action.reason + ("" if verified else " [UNVERIFIED — siehe Log]"),
                pnl_pct=action.pnl_pct,
                amount_usd=action.amount_usd,
                close_pct=action.close_pct,
            )
            time.sleep(0.5)
        except Exception as exc:
            msg = f"{action.symbol}: SELL-Exit API call failed — {exc}"
            logger.error("[sell_exits] %s", msg)
            stats["errors"].append(msg)

    return stats


def process_sell_exits(
    client: Any,
    signal_repo: Any,
    positions: list[dict],
    dry_run: bool = False,
    db: Any = None,
) -> dict:
    """Convenience-Wrapper für den risk_worker: fetch → evaluate → execute."""
    try:
        fresh = signal_repo.get_fresh()
    except Exception as exc:
        logger.warning("[sell_exits] get_fresh() fehlgeschlagen: %s", exc)
        return {"closed": 0, "errors": [f"get_fresh failed: {exc}"]}

    blocked = load_blocked_instruments(db, positions)
    actions = evaluate_sell_exits(fresh, positions, blocked_instruments=blocked)
    if not actions:
        return {"closed": 0, "errors": []}
    return execute_sell_exits(client, signal_repo, actions, dry_run=dry_run, db=db)
