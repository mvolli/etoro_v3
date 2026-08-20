#!/usr/bin/env python3
"""
scripts/sync_instrument_sectors.py
Backfill fuer `instruments.sector` aus yfinance (Yahoo-Sektor-Taxonomie).

WARUM (Analyse 2026-08-12, docs/analysis/bot-review-2026-08-12.md):
`instruments.sector` war bei 0 von 15.501 Instrumenten gefuellt. Gleichzeitig
faellt check_asset_class_gate fuer jedes Symbol ausserhalb des hartkodierten
ASSET_CLASS_MAP (~65 US-Ticker) fail-open durch — gemessen 43 von 54 gehaltenen
Symbolen = $6.433 = 74.2% des Equity ohne jede Sektor-Grenze. Es gab schlicht
keine Datenquelle, aus der ein echtes Sektor-Limit haette gespeist werden
koennen. Dieses Script ist diese Quelle.

Strategie (kein API-Hammering, Vorbild sync_instrument_tradability.py):
  - Prioritaet: GEHALTENE Positionen zuerst (die brauchen den Wert sofort),
    dann Core-Sweep-Whitelist, dann nie geprueft, dann aelteste Pruefung
  - MAX_PER_RUN begrenzt die Last; ~11 Tage fuer eine Vollrotation bei 400/Tag
  - TTL 90 Tage — der Sektor eines Unternehmens aendert sich praktisch nie
  - Sleep zwischen Calls; yfinance .info kostet ~0.3-1.1s pro Symbol

NAMESPACE (AGENTS.md-Invariante, 2196.HK-Incident): yfinance wird IMMER mit
`instruments.yfinance_symbol` befragt, nie mit `instruments.symbol` (dem
eToro-Namespace). Fehlt yfinance_symbol, faellt das Script auf `symbol`
zurueck — das ist fuer US-Ticker korrekt und fuer den Rest ein Miss, der als
'unknown' vermerkt wird statt falsch zu raten.

Schedule-Vorschlag: taeglich, ausserhalb der Handelsspitzen.
Manuell:  PYTHONPATH=src python3 scripts/sync_instrument_sectors.py
Dry-Run:  SECTORSYNC_DRY_RUN=1 SECTORSYNC_MAX_PER_RUN=20 PYTHONPATH=src \
              python3 scripts/sync_instrument_sectors.py
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sector_sync")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# yfinance .info kostet gemessen ~5.6s/Symbol (Rate-Limiting greift schnell;
# die ersten Calls einer Session laufen mit ~0.4s, danach drosselt Yahoo).
# 200/Run entspricht damit ~19 Minuten Laufzeit und bei taeglichem Lauf einer
# Vollrotation der ~4.161 handelbaren Instrumente in ~21 Tagen. Die GEHALTENEN
# Positionen sind durch die Prioritaets-Sortierung schon nach dem ersten Lauf
# vollstaendig — der lange Schwanz ist Vorratsdatenhaltung, kein Blocker.
MAX_PER_RUN = int(os.environ.get("SECTORSYNC_MAX_PER_RUN", "200"))
TTL_DAYS = int(os.environ.get("SECTORSYNC_TTL_DAYS", "90"))
SLEEP_BETWEEN = float(os.environ.get("SECTORSYNC_SLEEP", "0.35"))
DRY_RUN = os.environ.get("SECTORSYNC_DRY_RUN", "0") == "1"

DB_PATH = PROJECT_ROOT / "data" / "trading.db"

# Sektoren, die yfinance nicht kennt (Forex, Rohstoffe, Indizes, Krypto),
# werden aus asset_class abgeleitet (bot.core.sector_taxonomy) statt als
# 'unknown' markiert — sonst laufen sie im Hauptkonto-Report in "Sektor
# noch nicht abgerufen" (fix/forex-sector-unknown 2026-08-20). Der Read-Path
# behandelt 'unknown' wie NULL (fail-open), das Feld dient nur der
# Rotations-Buchhaltung.
UNKNOWN = "unknown"


def _ensure_columns(db) -> None:
    """Idempotente Migration — jede Spalte einzeln in try/except (AGENTS.md)."""
    for stmt in (
        "ALTER TABLE instruments ADD COLUMN sector_checked_at TEXT",
        "ALTER TABLE instruments ADD COLUMN industry TEXT",
    ):
        try:
            db.execute(stmt)
        except Exception:
            pass  # Spalte existiert bereits


def _select_candidates(db, limit: int) -> list[dict]:
    """Instrumente in Prioritaets-Reihenfolge.

    1. aktuell gehalten (portfolio_snapshot)   — brauchen den Wert sofort
    2. Core-Sweep-Whitelist                    — naechste Kaufkandidaten
    3. nie geprueft (sector_checked_at IS NULL)
    4. aelteste Pruefung ausserhalb der TTL
    """
    ttl_cutoff = (datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    rows = db.fetchall(
        """
        SELECT i.instrument_id, i.symbol, i.yfinance_symbol, i.sector,
               i.asset_class, i.sector_checked_at,
               CASE
                   WHEN p.instrument_id IS NOT NULL THEN 0
                   WHEN w.instrument_id IS NOT NULL THEN 1
                   WHEN i.sector_checked_at IS NULL THEN 2
                   ELSE 3
               END AS prio
        FROM instruments i
        LEFT JOIN (SELECT DISTINCT instrument_id FROM portfolio_snapshot) p
               ON p.instrument_id = i.instrument_id
        LEFT JOIN (SELECT DISTINCT instrument_id FROM core_sweep_whitelist) w
               ON w.instrument_id = i.instrument_id
        WHERE i.is_active = 1
          AND COALESCE(i.is_tradable, 1) = 1
          AND (i.sector_checked_at IS NULL OR i.sector_checked_at < ?)
        ORDER BY prio ASC, i.sector_checked_at ASC NULLS FIRST
        LIMIT ?
        """,
        (ttl_cutoff, limit),
    )
    return [dict(r) for r in (rows or [])]


def _fetch_sector(yf_symbol: str) -> tuple[str | None, str | None]:
    """(sector, industry) aus yfinance. Gibt (None, None) bei jedem Fehler."""
    try:
        import yfinance as yf

        info = yf.Ticker(yf_symbol).info or {}
        sector = (info.get("sector") or "").strip() or None
        industry = (info.get("industry") or "").strip() or None
        return sector, industry
    except Exception as exc:
        logger.debug("sector fetch failed for %s: %s", yf_symbol, exc)
        return None, None


def _resolve_sector(row: dict) -> tuple[str | None, str | None]:
    """(sector, industry) fuer ein Instrument.

    fix/forex-sector-unknown (2026-08-20): Forex/Rohstoffe/Indizes/Krypto
    haben bei yfinance KEINE Sektoren — die wurden bisher als 'unknown'
    markiert und fielen im Hauptkonto-Report in "Sektor noch nicht
    abgerufen" ($4.766 = 67.3% des Equity). Fuer diese Asset-Klassen wird
    der Sektor stattdessen direkt aus asset_class abgeleitet (kein
    yfinance-Call — spart Rate-Limiting und liefert sofortige Werte).
    """
    from bot.core.sector_taxonomy import derive_asset_class_sector

    derived = derive_asset_class_sector(row.get("asset_class"))
    if derived:
        return derived, None
    yf_sym = (row.get("yfinance_symbol") or "").strip() or row["symbol"]
    return _fetch_sector(yf_sym)


def main() -> int:
    from bot.core.worker_lock import worker_lock
    from bot.db.connection import DB

    # Eigener Lock-Name: das Script darf NIE parallel zu sich selbst laufen,
    # muss aber den data_worker nicht blockieren (nur Lesen + eigenes UPDATE).
    with worker_lock("sector_sync") as acquired:
        if not acquired:
            logger.info("sector_sync laeuft bereits — dieser Lauf wird uebersprungen.")
            return 0

        with DB(DB_PATH) as db:
            _ensure_columns(db)
            candidates = _select_candidates(db, MAX_PER_RUN)
            if not candidates:
                logger.info(
                    "Nichts zu tun — alle aktiven Instrumente innerhalb TTL (%dd).",
                    TTL_DAYS,
                )
                return 0

            logger.info(
                "%d Instrumente zu pruefen (max %d/Run, TTL %dd, dry_run=%s)",
                len(candidates), MAX_PER_RUN, TTL_DAYS, DRY_RUN,
            )

            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            filled = unknown = 0

            for idx, row in enumerate(candidates, 1):
                # fix/forex-sector-unknown: asset-class-derived Sektor
                # (Forex/Rohstoffe/Indizes/Krypto) hat Vorrang vor yfinance.
                sector, industry = _resolve_sector(row)

                if sector:
                    filled += 1
                else:
                    sector = UNKNOWN
                    unknown += 1

                if not DRY_RUN:
                    db.execute(
                        "UPDATE instruments SET sector = ?, industry = ?, "
                        "sector_checked_at = ? WHERE instrument_id = ?",
                        (sector, industry, now, row["instrument_id"]),
                    )

                if idx % 50 == 0:
                    logger.info("  ... %d/%d (%d Sektoren, %d unknown)",
                                idx, len(candidates), filled, unknown)
                if idx < len(candidates):
                    time.sleep(SLEEP_BETWEEN)

            logger.info(
                "Fertig: %d/%d Sektoren gesetzt, %d unknown%s",
                filled, len(candidates), unknown, " (DRY-RUN, nichts geschrieben)" if DRY_RUN else "",
            )

            if not DRY_RUN:
                remaining = db.fetchone(
                    "SELECT COUNT(*) AS n FROM instruments "
                    "WHERE is_active = 1 AND COALESCE(is_tradable,1) = 1 "
                    "AND sector_checked_at IS NULL"
                )
                logger.info("Noch offen (nie geprueft): %s", remaining["n"] if remaining else "?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
