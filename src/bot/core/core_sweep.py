"""feat/core-sweep (2026-07-22): liquides Cash-Deployment.

Reiner, testbarer Planer: entkoppelt das Deployment ueberschuessigen Cashs vom
illiquiden Small-Cap-Signalfluss. Wenn Cash ueber dem Reserve-Ziel liegt, wird
der Ueberschuss in einen kuratierten Korb hochliquider Large-Caps/ETFs
("Core") gesweept — grosse, diversifizierte, stop-losste Positionen in Titeln,
die grosse Groessen sicher aufnehmen.

Die Funktion plant nur (keine Seiteneffekte, keine Order): der signal_worker
setzt den Plan ueber dieselbe create->APPROVED->execution-Bahn wie normale
Signale um und erbt damit SL-Clamp, Market-Open-Guard und Ghost-Order-Pipeline.

hybrid-whitelist (fix/core-sweep-auto-discovery 2026-07-22):
Core-Sweep liest Kandidaten aus zwei Quellen zusammen:
  1. Config-Whitelist (statisch, pinned, expires=NULL)
  2. DB-Tabelle core_sweep_whitelist (dynamisch, discovery-geschrieben,
     expires_at TTL 24h)

Discovery-Worker schreibt FRESH-Signale (conviction HIGH/MEDIUM, score >= 35,
rsi < 75) automatisch in die DB-Whitelist — Core-Sweep findet so Instrumente,
die nicht in der statischen Whitelist stehen, aber gerade ein starkes Signal
liefern.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class SweepOrder:
    symbol: str
    instrument_id: int
    amount_usd: float
    atr_pct: float | None = None


def _cfg_block(cfg: dict) -> dict:
    return ((cfg or {}).get("trading", {}) or {}).get("core_sweep", {}) or {}


def is_enabled(cfg: dict) -> bool:
    return bool(_cfg_block(cfg).get("enabled", False))


def _ensure_core_sweep_whitelist_table(db: Any) -> None:
    """Lazy migration: core_sweep_whitelist-Tabelle anlegen (idempotent)."""
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS core_sweep_whitelist (
                instrument_id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'config',
                score REAL,
                conviction TEXT,
                added_at TEXT NOT NULL DEFAULT (datetime('now','utc')),
                expires_at TEXT,
                UNIQUE(instrument_id, source)
            )
        """)
    except Exception:
        pass  # table already exists or similar


def _load_db_whitelist(db: Any) -> dict[str, int]:
    """Lade dynamische Core-Sweep-Whitelist aus DB.

    Filtert abgelaufene Einträge (expires_at < now) und gibt
    {symbol: instrument_id} fuer noch gueltige Einzurueck.
    """
    _ensure_core_sweep_whitelist_table(db)
    try:
        rows = db.fetchall("""
            SELECT symbol, instrument_id
            FROM core_sweep_whitelist
            WHERE source = 'discovery'
              AND (expires_at IS NULL OR expires_at > datetime('now','utc'))
        """)
        return {row["symbol"]: int(row["instrument_id"]) for row in rows}
    except Exception:
        return {}


def _prune_expired_db_whitelist(db: Any) -> int:
    """Loesche abgelaufene Eintraege aus core_sweep_whitelist.

    Gibt Anzahl der geloeschten Eintraege zurueck.
    """
    _ensure_core_sweep_whitelist_table(db)
    try:
        cur = db.execute("""
            DELETE FROM core_sweep_whitelist
            WHERE source = 'discovery'
              AND expires_at IS NOT NULL
              AND expires_at <= datetime('now','utc')
        """)
        return cur.rowcount if hasattr(cur, "rowcount") else 0
    except Exception:
        return 0


def plan_core_sweep(
    cfg: dict,
    equity: float,
    cash: float,
    regime: str,
    held_instrument_ids: set[int] | None = None,
    atr_by_id: dict[int, float] | None = None,
    rsi_by_id: dict[int, float] | None = None,
    db: Any | None = None,
) -> tuple[list[SweepOrder], list[str]]:
    """Plane Core-Sweep-Orders fuer ueberschuessiges Cash.

    Rein & seiteneffektfrei — eignet sich fuer Dry-Log UND Live. Gibt
    (orders, reasons) zurueck; leere orders + genau ein reason erklaeren,
    warum nicht gesweept wurde.

    Sizing: per_position_pct*equity je Titel, geclampt auf max_position_pct
    und auf den deploybaren Rest. Nie unter reserve_floor_pct Cash. Bis
    max_sweeps_per_run Titel pro Lauf ("zuegig"). Kandidaten = Whitelist-Titel,
    die noch nicht gehalten werden (kein Core-Pyramiding — Diversifikation),
    optional RSI-gefiltert (nicht in einen extended Titel kaufen). Sortiert
    nach ATR aufsteigend: die stabilsten Anker (SPY) zuerst.

    Hybrid-Whitelist (fix/core-sweep-auto-discovery 2026-07-22):
    Kombiniert config.yaml-Whitelist mit DB-Tabelle core_sweep_whitelist
    (Discovery-geschrieben, FRESH-Signale). Config-Werte haben Vorrang
    (kein Duplikat bei gleicher instrument_id).
    """
    cs = _cfg_block(cfg)
    reasons: list[str] = []
    held = set(held_instrument_ids or set())
    atr_by_id = atr_by_id or {}
    rsi_by_id = rsi_by_id or {}

    if equity <= 0:
        return [], ["Core-Sweep: equity <= 0 (fail-closed)"]

    # ── Regime-Gate ──────────────────────────────────────────────────────────
    allowed_regimes = [str(r).upper() for r in cs.get("regimes", ["NORMAL", "CAUTION"])]
    if str(regime).upper() not in allowed_regimes:
        return [], [f"Core-Sweep: Regime {regime} nicht in {allowed_regimes} — pausiert"]

    reserve_target_pct = float(cs.get("reserve_target_pct", 15.0))
    reserve_floor_pct = float(cs.get("reserve_floor_pct", 10.0))
    per_position_pct = float(cs.get("per_position_pct", 4.0))
    max_position_pct = float(cs.get("max_position_pct", 6.0))
    max_sweeps = int(cs.get("max_sweeps_per_run", 4))
    rsi_overbought = float(cs.get("rsi_overbought", 75.0))
    config_whitelist: dict = cs.get("whitelist", {}) or {}

    # ── Hybrid-Whitelist: Config + DB zusammenfuegen ─────────────────────────
    whitelist: dict[str, int] = dict(config_whitelist)  # copy
    if db is not None:
        # Prune expired entries (silent)
        _pruned = _prune_expired_db_whitelist(db)
        if _pruned:
            reasons.append(f"Core-Sweep: {_pruned} abgelaufene DB-Einträge entfernt")

        # Load DB whitelist
        db_whitelist = _load_db_whitelist(db)
        for sym, iid in db_whitelist.items():
            if sym not in whitelist:  # config hat Vorrang
                whitelist[sym] = iid

    reserve_target = equity * reserve_target_pct / 100.0
    reserve_floor = equity * reserve_floor_pct / 100.0
    target_size = round(equity * per_position_pct / 100.0, 2)
    max_size = equity * max_position_pct / 100.0

    excess = cash - reserve_target
    above_floor = cash - reserve_floor
    deployable = min(excess, above_floor)

    if target_size <= 0:
        return [], ["Core-Sweep: per_position_pct=0 — nichts zu tun"]
    if deployable < target_size:
        return [], [
            f"Core-Sweep: kein Ueberschuss (Cash ${cash:.0f}, Ziel-Reserve "
            f"${reserve_target:.0f}, deploybar ${deployable:.0f} < Tranche ${target_size:.0f})"
        ]

    # ── Kandidaten: Whitelist-Titel, die noch NICHT gehalten werden ──────────
    candidates: list[tuple[str, int, float | None]] = []
    for sym, iid in whitelist.items():
        try:
            iid = int(iid)
        except (TypeError, ValueError):
            continue
        if iid in held:
            continue  # kein Core-Pyramiding
        rsi = rsi_by_id.get(iid)
        if rsi is not None and rsi > rsi_overbought:
            reasons.append(f"{sym}: RSI {rsi:.0f} > {rsi_overbought:.0f} — nicht extended kaufen")
            continue
        # Broker-Minimum-Check (fix/core-sweep-min-exposure 2026-07-27):
        # Orders unter minPositionExposure werden von eToro mit 720 abgelehnt.
        if db is not None:
            try:
                _min_row = db.fetchone(
                    "SELECT min_position_amount FROM instruments WHERE instrument_id = ?",
                    (iid,),
                )
                if _min_row and _min_row["min_position_amount"]:
                    _broker_min = float(_min_row["min_position_amount"])
                    _per_pos = equity * per_position_pct / 100.0
                    if _per_pos < _broker_min:
                        reasons.append(f"{sym}: ${_per_pos:.2f} < Broker-Min ${_broker_min:.0f} — SKIP")
                        continue
            except Exception:
                pass  # Spalte fehlt -> fail-open
        candidates.append((sym, iid, atr_by_id.get(iid)))

    if not candidates:
        reasons.insert(0, "Core-Sweep: keine freien Core-Titel (alle gehalten/gefiltert)")
        return [], reasons

    # Stabilste Anker zuerst (ATR aufsteigend; None ans Ende)
    candidates.sort(key=lambda c: (c[2] is None, c[2] if c[2] is not None else 0.0))

    orders: list[SweepOrder] = []
    remaining = deployable
    for sym, iid, atr in candidates:
        if len(orders) >= max_sweeps:
            break
        if remaining < target_size:
            break
        size = round(min(target_size, max_size, remaining), 2)
        orders.append(SweepOrder(symbol=sym, instrument_id=iid, amount_usd=size, atr_pct=atr))
        remaining -= size

    reasons.insert(
        0,
        f"Core-Sweep: Cash ${cash:.0f} > Reserve ${reserve_target:.0f} → "
        f"{len(orders)} Sweep(s) á ~${target_size:.0f} geplant (deploybar ${deployable:.0f})",
    )
    return orders, reasons
