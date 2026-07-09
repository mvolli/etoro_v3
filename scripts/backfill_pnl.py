#!/usr/bin/env python3
"""
scripts/backfill_pnl.py
One-time backfill of pnl_pct/pnl_usd/exit_price for CLOSED trades that
currently have NULL pnl_pct.

Two strategies:
  A. eToro trade history API (90 days, paginated) — real fills → exact PnL
  B. Ghost-order closes (no entry_price, no api_position_id) — pnl=0 (never filled)
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_pnl")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


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

    cfg    = load_config()
    db     = DB(db_path=PROJECT_ROOT / cfg.db.path)
    api_cfg = cfg.api if isinstance(cfg.api, dict) else vars(cfg.api) if hasattr(cfg, "api") else {}
    client = EToroClient(api_key=api_key, user_key=user_key,
                         config=ClientConfig.from_dict(api_cfg))

    # ── 1. Alle CLOSED Trades ohne pnl_pct laden ─────────────────────────────
    raw = db.fetchall(
        "SELECT id, symbol, api_position_id, order_id, entry_price, amount_usd "
        "FROM trades WHERE status='CLOSED' AND pnl_pct IS NULL"
    )
    rows = [dict(r) for r in raw]
    logger.info("%d CLOSED Trades ohne pnl_pct", len(rows))
    if not rows:
        logger.info("Nichts zu tun.")
        return 0

    # Echte Trades (haben position_id oder entry_price) vs Ghost-Closes (weder noch)
    real_trades  = [r for r in rows if r.get("api_position_id") or r.get("entry_price")]
    ghost_closes = [r for r in rows if not r.get("api_position_id") and not r.get("entry_price")]
    logger.info("  Echte Trades (mit Position-ID oder Entry-Price): %d", len(real_trades))
    logger.info("  Ghost-Order-Closes (kein Fill, kein Entry-Price): %d", len(ghost_closes))

    # ── 2. eToro Trade History holen (90 Tage, bis zu 3 Seiten × 100) ────────
    logger.info("Lade eToro Trade History (90 Tage, bis zu 300 Eintraege)...")
    history: list[dict] = []
    for page in range(1, 4):
        batch = client.get_trade_history(page=page, page_size=100)
        if not batch:
            break
        history.extend(batch)
        logger.info("  Seite %d: %d Eintraege (Gesamt: %d)", page, len(batch), len(history))
        if len(batch) < 100:
            break

    logger.info("Trade History geladen: %d Eintraege", len(history))

    # Index nach positionId und orderId
    by_pos_id: dict[str, dict] = {}
    by_ord_id: dict[str, dict] = {}
    for ht in history:
        pid = ht.get("positionId")
        oid = ht.get("orderId")
        if pid is not None:
            by_pos_id[str(pid)] = ht
        if oid is not None:
            by_ord_id[str(oid)] = ht

    # ── 3. Echte Trades gegen History matchen ────────────────────────────────
    matched = 0
    unmatched_real = 0

    for trade in real_trades:
        t_id   = trade["id"]
        pos_id = str(trade["api_position_id"]) if trade.get("api_position_id") else ""
        ord_id = str(trade["order_id"]) if trade.get("order_id") else ""

        ht = by_pos_id.get(pos_id) or by_ord_id.get(ord_id)
        if ht:
            net_profit = float(ht.get("netProfit") or 0)
            investment = float(
                ht.get("investment") or ht.get("initialInvestment") or trade.get("amount_usd") or 1
            )
            close_rate = float(ht.get("closeRate") or 0) or None

            pnl_pct = round(net_profit / investment * 100, 6) if investment else 0.0
            pnl_usd = round(net_profit, 2)

            db.execute(
                "UPDATE trades SET pnl_pct=?, pnl_usd=?, exit_price=? WHERE id=?",
                (pnl_pct, pnl_usd, close_rate, t_id),
            )
            matched += 1
            logger.info(
                "  #%d %s: PnL=%.2f%% ($%.2f) close=%.4f — History-Match",
                t_id, trade["symbol"], pnl_pct, pnl_usd, close_rate or 0,
            )
        else:
            unmatched_real += 1
            logger.warning(
                "  #%d %s: kein History-Match (pos=%s ord=%s)",
                t_id, trade["symbol"], pos_id or "NULL", ord_id or "NULL",
            )

    # ── 4. Ghost-Order-Closes: pnl=0 setzen ─────────────────────────────────
    # Diese Trades hatten nie eine echte Position — kein Geld bewegt.
    ghost_marked = 0
    for trade in ghost_closes:
        db.execute(
            "UPDATE trades SET pnl_pct=0.0, pnl_usd=0.0 WHERE id=?",
            (trade["id"],),
        )
        ghost_marked += 1

    logger.info(
        "Fertig: %d History-Matches, %d Ghost-Closes pnl=0, %d unmatched",
        matched, ghost_marked, unmatched_real,
    )
    if unmatched_real:
        logger.warning(
            "%d Trades ohne History-Match (evtl. aelter als 90 Tage oder "
            "positionId nicht in History-Endpoint)", unmatched_real,
        )

    # ── 5. Verifikation ───────────────────────────────────────────────────────
    row = db.fetchone("SELECT COUNT(*), COUNT(pnl_pct) FROM trades WHERE status='CLOSED'")
    if row:
        total, with_pnl = row[0], row[1]
        pct = with_pnl / total * 100 if total else 0
        logger.info(
            "Ergebnis: %d/%d CLOSED Trades haben pnl_pct (%.0f%%)",
            with_pnl, total, pct,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
