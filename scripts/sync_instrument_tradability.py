#!/usr/bin/env python3
"""
scripts/sync_instrument_tradability.py
Täglicher Batch-Check aller Instrumente gegen eToro API (allowOpenPosition).
Fügt is_tradable / tradability_checked_at Spalten ein falls nötig.

Strategie (kein API-Hammering):
  - Max 200 Instrumente pro Run → 4 Batch-Requests à 50 IDs → ~4 Sekunden
  - Priorität: nie geprüft > älteste Prüfung > nicht-handelbare zuerst
  - TTL: 7 Tage — stabile Instrumente werden nur 1x/Woche geprüft
  - Rate-Limit: 1s sleep zwischen Batches

Schedule: 1x wöchentlich Sonntag 03:30 UTC — Maintenance: neue/geblockte Instrumente
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
logger = logging.getLogger("tradability_sync")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

BATCH_SIZE            = 100   # IDs per API-Request (max laut API-Fehler)
MAX_PER_RUN           = 500   # Maintenance: neue + geblockte Instrumente (~5 Batches à 100)
TTL_DAYS              = 30    # Handelbarkeit ändert sich selten — monatliche Re-Checks genügen
SLEEP_BETWEEN_BATCHES = 3.0   # Rate-Limit-Puffer (~20 req/min beobachtet)


def _load_env() -> None:
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return
    with env_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def _ensure_columns(db) -> None:
    """Fügt is_tradable / tradability_checked_at hinzu falls nicht vorhanden."""
    existing = {row["name"] for row in db.fetchall("PRAGMA table_info(instruments)")}
    if "is_tradable" not in existing:
        db.execute("ALTER TABLE instruments ADD COLUMN is_tradable INTEGER DEFAULT 1")
        logger.info("Schema-Migration: Spalte instruments.is_tradable hinzugefügt")
    if "tradability_checked_at" not in existing:
        db.execute("ALTER TABLE instruments ADD COLUMN tradability_checked_at TEXT")
        logger.info("Schema-Migration: Spalte instruments.tradability_checked_at hinzugefügt")


def _pick_instruments(db) -> list[dict]:
    """Wählt zu prüfende Instrumente nach Priorität (nie geprüft > älteste > nicht-handelbar)."""
    rows = db.fetchall(
        """
        SELECT instrument_id, symbol, is_tradable, tradability_checked_at
        FROM instruments
        WHERE is_active = 1
        ORDER BY
            CASE WHEN tradability_checked_at IS NULL THEN 0 ELSE 1 END,
            COALESCE(is_tradable, 1) ASC,
            tradability_checked_at ASC
        LIMIT ?
        """,
        (MAX_PER_RUN,),
    )
    return [dict(r) for r in rows]


def _run_batch_check(client, instruments: list[dict], db) -> tuple[int, int, int]:
    """Prüft Handelbarkeit via Eligibility-API. Gibt (geprüft, neu_geblockt, neu_freigegeben) zurück.

    Verwendet POST /api/v2/trading/info/eligibility (nicht /market-data/instruments —
    letzteres unterstützt keine Batch-IDs und liefert kein allowOpenPosition-Feld).
    """
    ids = [row["instrument_id"] for row in instruments]
    id_to_sym = {row["instrument_id"]: row["symbol"] for row in instruments}
    id_to_current = {row["instrument_id"]: row.get("is_tradable", 1) for row in instruments}

    checked = newly_blocked = newly_unblocked = 0

    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start: start + BATCH_SIZE]
        try:
            resp = client.post(
                "/trading/info/eligibility",
                {"instrumentIds": chunk, "currency": "USD"},
                v2=True,
            )
        except Exception as exc:
            # Rate-Limit / Netzwerkfehler — Chunk überspringen.
            # Instrumente NICHT als non-tradable markieren: API-Fehler ≠ nicht handelbar.
            logger.warning(
                "Eligibility-API Fehler (chunk %d, IDs %s…): %s — übersprungen",
                start // BATCH_SIZE + 1, chunk[:3], exc,
            )
            time.sleep(SLEEP_BETWEEN_BATCHES)
            continue

        # {instrument_id: allowOpenPosition bool}
        elig_map: dict[int, bool] = {}
        for e in resp.get("eligibilities", []):
            iid = e.get("instrumentId")
            if iid is not None:
                elig_map[int(iid)] = bool(e.get("allowOpenPosition", True))

        not_found = set(resp.get("notFoundInstrumentIds", []))

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for iid in chunk:
            if iid in not_found:
                # Explizit als nicht gefunden gemeldet — könnte delistet sein.
                logger.info("  %s (id=%d): notFoundInstrumentIds → is_tradable=0", id_to_sym.get(iid, "?"), iid)
                is_tradable = 0
            elif iid not in elig_map:
                # Weder in eligibilities noch in notFound — Antwort unvollständig, überspringen.
                logger.debug("  %s (id=%d): nicht in Eligibility-Response — unverändert", id_to_sym.get(iid, "?"), iid)
                checked += 1
                continue
            else:
                is_tradable = 1 if elig_map[iid] else 0
                if not is_tradable:
                    logger.info(
                        "  %s (id=%d): allowOpenPosition=false → is_tradable=0",
                        id_to_sym.get(iid, "?"), iid,
                    )

            db.execute(
                "UPDATE instruments SET is_tradable=?, tradability_checked_at=? WHERE instrument_id=?",
                (is_tradable, now_iso, iid),
            )
            checked += 1

            current_val = id_to_current.get(iid, 1)
            if current_val == 1 and is_tradable == 0:
                newly_blocked += 1
                logger.warning("NEU GEBLOCKT: %s (id=%d)", id_to_sym.get(iid, "?"), iid)
            elif current_val == 0 and is_tradable == 1:
                newly_unblocked += 1
                logger.info("WIEDER HANDELBAR: %s (id=%d)", id_to_sym.get(iid, "?"), iid)

        if start + BATCH_SIZE < len(ids):
            time.sleep(SLEEP_BETWEEN_BATCHES)

    return checked, newly_blocked, newly_unblocked


def main() -> int:
    t_start = time.monotonic()
    _load_env()

    api_key  = os.environ.get("ETORO_API_KEY", "")
    user_key = os.environ.get("ETORO_USER_KEY", "")
    if not api_key or not user_key:
        logger.critical("ETORO_API_KEY / ETORO_USER_KEY fehlen — Abbruch")
        return 1

    from bot.api.client import ClientConfig, EToroClient
    from bot.config import load_config
    from bot.db.connection import DB

    cfg     = load_config()
    db_path = PROJECT_ROOT / cfg.db.path
    db      = DB(db_path=db_path)

    api_cfg = {}
    if hasattr(cfg, "api"):
        api_cfg = cfg.api if isinstance(cfg.api, dict) else vars(cfg.api)
    client = EToroClient(api_key=api_key, user_key=user_key,
                         config=ClientConfig.from_dict(api_cfg))

    # Schema-Migration (idempotent)
    _ensure_columns(db)

    # Instrumente nach Priorität wählen
    instruments = _pick_instruments(db)
    if not instruments:
        logger.info("Keine Instrumente zu prüfen.")
        return 0

    # Instrumente die noch innerhalb TTL liegen herausfiltern
    ttl_cutoff = (datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    to_check = [
        i for i in instruments
        if not i.get("tradability_checked_at")
        or i["tradability_checked_at"] < ttl_cutoff
    ]

    if not to_check:
        logger.info("Alle %d Instrumente wurden kürzlich geprüft (TTL %dd) — nichts zu tun.", len(instruments), TTL_DAYS)
        return 0

    logger.info(
        "Tradability-Sync: %d Instrumente zu prüfen (%d Batches à %d)",
        len(to_check), (len(to_check) + BATCH_SIZE - 1) // BATCH_SIZE, BATCH_SIZE,
    )

    checked, newly_blocked, newly_unblocked = _run_batch_check(client, to_check, db)

    _blocked_row = db.fetchone("SELECT COUNT(*) as n FROM instruments WHERE is_tradable=0")
    total_blocked = (dict(_blocked_row).get("n", 0) if _blocked_row else 0)

    elapsed = time.monotonic() - t_start
    logger.info(
        "Fertig in %.1fs — geprüft=%d neu_geblockt=%d wieder_handelbar=%d gesamt_geblockt=%d",
        elapsed, checked, newly_blocked, newly_unblocked, total_blocked,
    )

    # Discord-Summary (best-effort)
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src" / "bot"))
        import discord_embeds as _DE
        icon = "🟡" if newly_blocked > 0 else "🟢"
        _DE.post_alert_embed(
            title=f"{icon} Tradability Sync — {checked} geprüft",
            description=(
                f"**Geprüft:** {checked} Instrumente\n"
                f"**Neu geblockt:** {newly_blocked}\n"
                f"**Wieder handelbar:** {newly_unblocked}\n"
                f"**Gesamt nicht-handelbar:** {total_blocked}\n"
                f"**Dauer:** {elapsed:.1f}s"
            ),
            severity="WARNING" if newly_blocked > 0 else "INFO",
        )
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
