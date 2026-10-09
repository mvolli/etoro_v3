#!/usr/bin/env python3
"""
scripts/backfill_rejection_learning.py

Idempotentes Backfill: wendet die reaktive Rejection-Lerner-Logik aus
execution_worker._learn_from_rejection auf HISTORISCHE FAILED-Trades an.

Warum (fix/rejection-learning-backfill, 2026-10-09):
  Der Rejection-Lerner (fix/units-only-tradability, 0bf2e23, 2026-10-05) ist
  rein reaktiv — er lernt nur aus NEUEN Ablehnungen. Instrumente, die vor dem
  Lerner FAILED sind, haben ihre Lernwerte nie erhalten und laufen damit aktiv
  weiter durch den Order-Pfad:
    - ABB.ST  (is_tradable=1, min_position_amount leer): 3x UnitsOnlyMinShare
      am 29./30.09. -> laeuft aktiv weiter und scheitert bei jedem Order-Versuch.
    - SLV     (is_tradable=1, stale): 2x "not found (not tradable)" 01./02.10.
      -> is_tradable nie auf 0 gesetzt.
  Dieses Script schliesst die Lücke EINMALIG (oder bei Bedarf wiederholbar):
  es liest alle FAILED-Trades und schreibt dieselben Werte, die der Lerner bei
  einem LIVE-Ablehnungsfall schreiben wuerde.

Idempotenz:
  - is_tradable=0: nur setzen, wenn aktuell nicht 0. Der woechentliche
    Tradability-Sync bleibt die autoritative Quelle und setzt bei erlaubter
    Wiederbelebung wieder auf 1.
  - min_position_amount: nur setzen, wenn der ermittelte Wert groesser als der
    aktuell gespeicherte ist ODER noch nichts gesetzt ist.
  - UnitsOnlyMinShare-Anteilswert ist ein KURS -> wird mit
    min_position_amount_learned_at=datetime('now') geschrieben, damit
    effective_broker_min() ihn nach 7 Tagen fail-open veraltet (selbe Semantik
    wie der LIVE-Lerner).
NIE angefasst: kill_switch, BIBLE_HARD_LIMITS, DB-Schema, .env.

Aufruf:
  PYTHONPATH=src /usr/bin/python3 scripts/backfill_rejection_learning.py [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_rejection_learning")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _load_env() -> None:
    try:
        from bot.config import load_hermes_env
        load_hermes_env()
    except Exception:
        pass



def aggregate_failed(rows: list) -> tuple[dict, dict]:
    """Pure Aggregation: FAILED-Trades -> (tradable_zero, min_amount).

    tradable_zero: {instrument_id: symbol}  — Instrument mit dauerhafterm Block
    min_amount:    {instrument_id: (value, is_units_only)} — hoechster ermittelter
                   Broker-Minimum-Wert (720 statisch ODER UnitsOnly-Anteilswert).

    Reines, DB-freies Aggregieren — testbar ohne Live-DB (Harte Regel 1).
    Verwendet die GLEICHEN pure Detector-/Parse-Functions wie der LIVE-Lerner.
    """
    from bot.workers.execution_worker import (
        parse_min_position_amount,
        is_internal_only_error,
        is_not_eligible_error,
        is_instrument_not_found_error,
        is_units_only_min_share_error,
        parse_units_only_price,
    )

    tradable_zero: dict[int, str] = {}
    min_amount: dict[int, tuple[float, bool]] = {}

    for r in rows:
        iid = r["instrument_id"]
        sym = r["symbol"]
        reason = r["rejection_reason"] or ""

        if (is_instrument_not_found_error(reason)
                or is_internal_only_error(reason)
                or is_not_eligible_error(reason)):
            tradable_zero[iid] = sym

        broker_min = parse_min_position_amount(reason)
        if broker_min:
            prev = min_amount.get(iid, (0.0, False))
            if broker_min >= prev[0]:
                min_amount[iid] = (broker_min, False)

        if is_units_only_min_share_error(reason):
            _shares, price = parse_units_only_price(reason)
            if price:
                prev = min_amount.get(iid, (0.0, False))
                if price >= prev[0]:
                    min_amount[iid] = (price, True)

    return tradable_zero, min_amount


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="nur zeigen, nicht schreiben")
    args = ap.parse_args()
    _load_env()

    from bot.config import load_config
    from bot.db.connection import DB

    cfg = load_config()
    db_path = PROJECT_ROOT / cfg.db.path
    db = DB(db_path=db_path)

    rows = db.fetchall(
        "SELECT instrument_id, symbol, rejection_reason FROM trades "
        "WHERE status='FAILED' AND rejection_reason IS NOT NULL AND rejection_reason != ''"
    )
    if not rows:
        logger.info("Keine FAILED-Trades mit rejection_reason — nichts zu tun.")
        return 0

    tradable_zero, min_amount = aggregate_failed(rows)

    changes_tradable = 0
    changes_min = 0

    for iid, sym in tradable_zero.items():
        cur = db.fetchone("SELECT is_tradable FROM instruments WHERE instrument_id=?", (iid,))
        cur_val = cur["is_tradable"] if cur else None
        if cur_val == 0:
            continue
        if args.dry_run:
            logger.info("[DRY-RUN] %s (id=%d): is_tradable %s -> 0 (dauerhafter Block in Historie)", sym, iid, cur_val)
        else:
            db.execute(
                "UPDATE instruments SET is_tradable=0, tradability_checked_at=datetime('now') WHERE instrument_id=?",
                (iid,),
            )
            logger.info("%s (id=%d): is_tradable %s -> 0", sym, iid, cur_val)
        changes_tradable += 1

    for iid, (val, is_units) in min_amount.items():
        cur = db.fetchone(
            "SELECT symbol, min_position_amount FROM instruments WHERE instrument_id=?", (iid,)
        )
        if not cur:
            continue
        sym = cur["symbol"]
        cur_val = cur["min_position_amount"]
        cur_val = float(cur_val) if cur_val is not None else 0.0
        if cur_val > 0 and val <= cur_val:
            continue
        tag = " (unitsOnly/KURS)" if is_units else ""
        if args.dry_run:
            logger.info("[DRY-RUN] %s (id=%d): min_position_amount %s -> %.2f%s", sym, iid, cur_val, val, tag)
        else:
            if is_units:
                db.execute(
                    "UPDATE instruments SET min_position_amount=?, min_position_amount_learned_at=datetime('now') WHERE instrument_id=?",
                    (val, iid),
                )
            else:
                db.execute("UPDATE instruments SET min_position_amount=? WHERE instrument_id=?", (val, iid))
            logger.info("%s (id=%d): min_position_amount %s -> %.2f%s", sym, iid, cur_val, val, tag)
        changes_min += 1

    logger.info(
        "Fertig%s — FAILED-Trades gepraet=%d, is_tradable=0 gesetzt=%d, min_position_amount gesetzt=%d",
        " (DRY-RUN)" if args.dry_run else "",
        len(rows), changes_tradable, changes_min,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
