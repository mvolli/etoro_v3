#!/usr/bin/env python3
"""
scripts/sync_instrument_catalog.py
Einmaliger / seltener Abgleich des eToro-Instrument-Katalogs gegen die lokale DB.

Was es tut:
  1. GET /market-data/instruments (kein Parameter) → kompletter eToro-Katalog (~15k Instrumente)
  2. Neue Instrumente (in API, nicht in DB) → INSERT mit is_active=0, is_tradable=NULL
     → werden beim nächsten wöchentlichen Tradability-Sync automatisch geprüft
  3. Weggefallene Instrumente (in DB aber nicht mehr in API, is_active=1) → is_active=0
  4. Bericht über Änderungen

Wann ausführen:
  - Manuell nach eToro-Ankündigungen neuer Märkte/Instrumente
  - Oder monatlich (2x Monat genügt — eToro erweitert Katalog selten)
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("catalog_sync")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ── instrumentTypeID → asset_class ───────────────────────────────────────────
TYPE_TO_ASSET_CLASS: dict[int, str] = {
    1:  "forex",
    2:  "commodity",
    4:  "index",
    5:  "stock",
    6:  "etf",
    10: "crypto",
}

# ── instrumentTypeID → market_region ─────────────────────────────────────────
TYPE_TO_MARKET_REGION: dict[int, str] = {
    1:  "FOREX",
    2:  "GLOBAL",
    4:  "US",       # meistens US-Indizes; Einzelfälle werden von market_hours.py erkannt
    5:  "US",       # Default — Exchange-Suffix überschreibt im Discovery-Worker
    6:  "US",
    10: "CRYPTO",
}

# ── Bekannte EU-Suffixe → market_region ──────────────────────────────────────
EU_SUFFIXES = {".DE", ".L", ".PA", ".MI", ".AS", ".MC", ".ST", ".SW",
               ".LS", ".OL", ".CO", ".HE", ".IR", ".PR", ".VI", ".WA",
               ".BD", ".BR", ".AT", ".BE"}
APAC_SUFFIXES = {".T", ".HK", ".SS", ".SZ", ".AX", ".NS", ".KS", ".KQ",
                 ".TW", ".SI", ".ASX", ".MOEX"}


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


def _infer_exchange_suffix(symbol: str) -> str:
    """Extrahiert den Exchange-Suffix aus dem Symbol (z.B. 'VOW3.DE' → '.DE')."""
    if "." in symbol:
        suffix = "." + symbol.rsplit(".", 1)[-1].upper()
        if len(suffix) <= 6:          # .DE, .L, .AX, .MOEX — aber keine URLs
            return suffix
    return ""


def _infer_market_region(symbol: str, type_id: int) -> str:
    """Leitet market_region aus Symbol-Suffix und type_id ab."""
    suffix = _infer_exchange_suffix(symbol).upper()
    if suffix in EU_SUFFIXES:
        return "EU"
    if suffix in APAC_SUFFIXES:
        return "APAC"
    return TYPE_TO_MARKET_REGION.get(type_id, "US")


def main() -> int:
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
    db      = DB(db_path=PROJECT_ROOT / cfg.db.path)
    api_cfg = cfg.api if isinstance(cfg.api, dict) else vars(cfg.api) if hasattr(cfg, "api") else {}
    client  = EToroClient(api_key=api_key, user_key=user_key,
                          config=ClientConfig.from_dict(api_cfg))

    # ── 1. API-Katalog abrufen ────────────────────────────────────────────────
    logger.info("Lade eToro-Instrument-Katalog...")
    resp = client.get("/market-data/instruments")
    api_items = resp.get("instrumentDisplayDatas", [])
    api_by_id: dict[int, dict] = {int(i["instrumentID"]): i for i in api_items}
    logger.info("API-Katalog: %d Instrumente", len(api_by_id))

    # ── 2. DB-Stand laden ─────────────────────────────────────────────────────
    db_rows   = db.fetchall("SELECT instrument_id, symbol, is_active FROM instruments")
    db_by_id  = {int(r["instrument_id"]): r for r in db_rows}
    logger.info("DB: %d Instrumente (%d aktiv)",
                len(db_by_id),
                sum(1 for r in db_rows if r["is_active"]))

    # ── 3. Delta berechnen ────────────────────────────────────────────────────
    api_ids = set(api_by_id.keys())
    db_ids  = set(db_by_id.keys())

    new_ids     = api_ids - db_ids                     # neu in API, fehlen in DB
    removed_ids = db_ids - api_ids                     # in DB aber nicht mehr in API

    logger.info("Neu in API: %d | Nicht mehr in API: %d", len(new_ids), len(removed_ids))

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # ── 4. Neue Instrumente einfügen ──────────────────────────────────────────
    inserted = 0
    for iid in sorted(new_ids):
        item     = api_by_id[iid]
        symbol   = item.get("symbolFull") or item.get("instrumentDisplayName", f"UNKNOWN_{iid}")
        name     = item.get("instrumentDisplayName", "")
        type_id  = item.get("instrumentTypeID")
        asset    = TYPE_TO_ASSET_CLASS.get(type_id, "stock")
        suffix   = _infer_exchange_suffix(symbol)
        region   = _infer_market_region(symbol, type_id)

        db.execute(
            """
            INSERT OR IGNORE INTO instruments
                (instrument_id, symbol, name, asset_class, instrument_type_id,
                 exchange_suffix, market_region,
                 is_active, is_tradable, tradability_checked_at, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?)
            """,
            (iid, symbol, name, asset, type_id, suffix, region, now_iso),
        )
        inserted += 1
        logger.debug("  NEU: id=%d  %-25s  %s  (%s)", iid, symbol, name[:40], asset)

    logger.info("Eingefügt: %d neue Instrumente (is_active=0, is_tradable=NULL)", inserted)

    # ── 5. Weggefallene Instrumente deaktivieren ──────────────────────────────
    deactivated = 0
    for iid in sorted(removed_ids):
        row = db_by_id[iid]
        if row["is_active"]:
            db.execute(
                "UPDATE instruments SET is_active=0, last_updated=? WHERE instrument_id=?",
                (now_iso, iid),
            )
            deactivated += 1
            logger.warning("DEAKTIVIERT (nicht mehr in API): id=%d  %s", iid, row["symbol"])

    if deactivated:
        logger.warning("%d Instrumente deaktiviert (nicht mehr in eToro-Katalog)", deactivated)
    else:
        logger.info("Keine Instrumente weggefallen.")

    # ── 6. Abschlussbericht ───────────────────────────────────────────────────
    row = db.fetchone("SELECT COUNT(*) as n FROM instruments")
    total_db = dict(row).get("n", 0) if row else 0
    row2 = db.fetchone("SELECT COUNT(*) as n FROM instruments WHERE is_active=0 AND tradability_checked_at IS NULL")
    pending_check = dict(row2).get("n", 0) if row2 else 0

    logger.info(
        "Fertig — DB gesamt: %d | Neu eingefügt: %d | Deaktiviert: %d | "
        "Warten auf Tradability-Check: %d",
        total_db, inserted, deactivated, pending_check,
    )

    if inserted > 0:
        logger.info(
            "Tipp: 'python3 scripts/sync_instrument_tradability.py' prüft die neuen "
            "Instrumente (NULL tradability_checked_at hat Priorität)."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
