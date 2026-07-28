#!/usr/bin/env python3
"""
scripts/backfill_trade_history.py — feat/pnl-nachreport (2026-07-28)

Einmal-Backfill ALLER unvollstaendigen CLOSED-Trades (fehlender entry_price,
exit_price oder pnl_usd) aus der eToro-Trade-History. Loest scripts/
backfill_pnl.py ab, dessen WHERE (pnl_pct IS NULL) nur 5 von ~48 Luecken fing.

Zusaetzlich werden trade_events-Zeilen angelegt (Full Close + aus der
History rekonstruierte Partial Closes — die API liefert eine Row pro
Teilverkauf), damit Tagesreport & Migration darauf aufbauen koennen.

Nutzung:
    python3 scripts/backfill_trade_history.py               # Dry-Run (Default)
    python3 scripts/backfill_trade_history.py --apply
    python3 scripts/backfill_trade_history.py --apply --post-summary
    python3 scripts/backfill_trade_history.py --min-date 2026-06-01

--post-summary postet EINEN Sammel-Nachreport nach #trades (Σ P/L, Win-Rate,
Best/Worst, Zeilen pro Trade, Unresolved-Liste).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_trade_history")

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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Aenderungen schreiben (Default: Dry-Run)")
    ap.add_argument("--post-summary", action="store_true",
                    help="Sammel-Nachreport nach #trades posten (nur mit --apply)")
    ap.add_argument("--min-date", default=None,
                    help="History-Startdatum YYYY-MM-DD (Default: aeltester Trade -3d)")
    ap.add_argument("--max-pages", type=int, default=30,
                    help="Max. History-Seiten a 100 Rows (Default 30)")
    args = ap.parse_args()
    dry_run = not args.apply

    _load_env()
    api_key = os.environ.get("ETORO_API_KEY", "")
    user_key = os.environ.get("ETORO_USER_KEY", "")
    if not api_key or not user_key:
        logger.critical("ETORO_API_KEY / ETORO_USER_KEY fehlen — Abbruch")
        return 1

    from bot.api.client import ClientConfig, EToroClient
    from bot.config import load_config
    from bot.core.pnl_backfill import fetch_history_index, match_close, pnl_from_row
    from bot.db.connection import DB
    from bot.db.repo import TradeEventRepo, TradeRepo

    cfg = load_config()
    db = DB(db_path=PROJECT_ROOT / cfg.db.path)
    api_cfg = cfg.api if isinstance(cfg.api, dict) else vars(cfg.api) if hasattr(cfg, "api") else {}
    client = EToroClient(api_key=api_key, user_key=user_key,
                         config=ClientConfig.from_dict(api_cfg))
    trade_repo = TradeRepo(db)          # stellt verify_attempts-Spalte sicher
    event_repo = TradeEventRepo(db)     # stellt trade_events-Tabelle sicher

    # ── 1. Unvollstaendige CLOSED-Trades ─────────────────────────────────────
    raw = db.fetchall(
        "SELECT id, symbol, instrument_id, api_position_id, order_id, "
        "       entry_price, exit_price, pnl_usd, pnl_pct, amount_usd, "
        "       created_at, confirmed_at, closed_at, verification_status "
        "FROM trades WHERE status='CLOSED' "
        "AND (entry_price IS NULL OR exit_price IS NULL OR pnl_usd IS NULL) "
        "ORDER BY closed_at"
    )
    rows = [dict(r) for r in raw]
    logger.info("%d unvollstaendige CLOSED-Trades gefunden", len(rows))
    if not rows:
        logger.info("Nichts zu tun.")
        return 0

    # ── 2. History EINMAL holen (bis Erschoepfung) ───────────────────────────
    min_date = args.min_date
    if not min_date:
        oldest = min((r["created_at"] or "9999") for r in rows)[:10]
        try:
            min_date = (datetime.fromisoformat(oldest)
                        - timedelta(days=3)).date().isoformat()
        except ValueError:
            min_date = None
    logger.info("History-Fetch ab %s (max %d Seiten)", min_date, args.max_pages)
    index = fetch_history_index(client, min_date, max_pages=args.max_pages)

    # ── 3. Matchen + (Dry-)Update ────────────────────────────────────────────
    filled: list[dict] = []
    unresolved: list[dict] = []
    partials_created = 0

    for t in rows:
        row = match_close(index, t["api_position_id"], t["order_id"])
        if not row:
            unresolved.append(t)
            continue
        nums = pnl_from_row(row)
        filled.append({**t, **nums})
        n_partials = max(0, len(index.by_position.get(
            int(t["api_position_id"]) if str(t["api_position_id"] or "").isdigit() else -1, [])) - 1)

        print(f"  ✔ #{t['id']:<4} {t['symbol']:<10} "
              f"entry {t['entry_price'] or '—':>10} → {nums['entry'] or '—':<10} "
              f"exit → {nums['exit'] or '—':<10} "
              f"pnl → {('%+.2f' % nums['pnl_usd']) if nums['pnl_usd'] is not None else '—':>8} "
              f"({('%+.1f%%' % nums['pnl_pct']) if nums['pnl_pct'] is not None else '—':>7}) "
              f"partials={n_partials}")

        if dry_run:
            partials_created += n_partials
            continue

        # History ist Ground Truth — ueberschreibt auch fehlerhafte Altwerte
        # (z.B. #485/#486: entry_price hielt faelschlich den amount_usd).
        trade_repo.update_status(
            t["id"], "CLOSED",
            entry_price=nums["entry"] if nums["entry"] is not None else t["entry_price"],
            exit_price=nums["exit"] if nums["exit"] is not None else t["exit_price"],
            pnl_usd=nums["pnl_usd"] if nums["pnl_usd"] is not None else t["pnl_usd"],
            pnl_pct=nums["pnl_pct"] if nums["pnl_pct"] is not None else t["pnl_pct"],
            verification_status="VERIFIED",
        )

        pos_id = str(t["api_position_id"] or "")
        pos_rows = index.by_position.get(int(pos_id) if pos_id.isdigit() else -1, [])
        # Full-Close-Event (falls nicht vorhanden)
        if pos_id and not event_repo.has_event(pos_id, "CLOSE"):
            close_ts = str(row.get("closeTimestamp") or "")[:19].replace("T", " ")
            event_repo.record(
                symbol=t["symbol"], event_type="CLOSE", source="history_backfill",
                trade_id=t["id"], position_id=pos_id,
                order_id=str(t["order_id"] or "") or None,
                instrument_id=t["instrument_id"],
                event_at=close_ts or (t["closed_at"] or None),
                units=nums["units"], price=nums["exit"],
                amount_usd=nums["investment"] or t["amount_usd"],
                pnl_usd=nums["pnl_usd"], pnl_pct=nums["pnl_pct"],
                pnl_source="api_history", reason="Einmal-Backfill aus API-History",
                reported_final=True,
            )
        # Partial-Close-Events rekonstruieren
        if pos_id and not event_repo.has_event(pos_id, "PARTIAL_CLOSE"):
            for prow in pos_rows[:-1]:
                p = pnl_from_row(prow)
                p_ts = str(prow.get("closeTimestamp") or "")[:19].replace("T", " ")
                event_repo.record(
                    symbol=t["symbol"], event_type="PARTIAL_CLOSE",
                    source="history_backfill", trade_id=t["id"],
                    position_id=pos_id, instrument_id=t["instrument_id"],
                    event_at=p_ts or None,
                    units=p["units"], price=p["exit"],
                    amount_usd=p["investment"],
                    pnl_usd=p["pnl_usd"], pnl_pct=p["pnl_pct"],
                    pnl_source="api_history",
                    reason="aus API-History rekonstruiert (Einmal-Backfill)",
                    reported_final=True,
                )
                partials_created += 1

    # UNRESOLVED nur, wenn der PnL selbst fehlt. Trades, denen nur Entry/Exit
    # fehlt (PnL vorhanden), bleiben VERIFIED — das ist eine kosmetische
    # Luecke, kein falscher Geldwert.
    for t in unresolved:
        pnl_missing = t["pnl_usd"] is None
        tag = "PnL FEHLT → UNRESOLVED" if pnl_missing else "nur Entry/Exit fehlt (PnL ok)"
        print(f"  ✖ #{t['id']:<4} {t['symbol']:<10} pos={t['api_position_id'] or '—'} "
              f"closed={str(t['closed_at'])[:10]} — kein History-Match · {tag}")
        if not dry_run and pnl_missing:
            trade_repo.update_status(t["id"], "CLOSED",
                                     verification_status="UNRESOLVED")

    pnls = [f["pnl_usd"] for f in filled if f["pnl_usd"] is not None]
    wins = sum(1 for p in pnls if p >= 0)
    print("\n──────────────────────────────────────────────")
    print(f"  {'DRY-RUN — ' if dry_run else ''}Matches: {len(filled)}/{len(rows)}   "
          f"Unresolved: {len(unresolved)}   Partial-Events: {partials_created}")
    if pnls:
        print(f"  Σ P/L nachgetragen: ${sum(pnls):+.2f}   "
              f"Win-Rate: {wins}/{len(pnls)} ({wins / len(pnls) * 100:.0f}%)")
    if dry_run:
        print("  → Mit --apply schreiben, mit --apply --post-summary inkl. Discord-Nachreport.")

    # ── 4. Sammel-Nachreport → #trades ───────────────────────────────────────
    if args.post_summary and not dry_run and filled:
        try:
            from bot import discord_embeds as de

            def _line(f: dict) -> str:
                pnl = (f"${f['pnl_usd']:+.2f}" if f["pnl_usd"] is not None else "–")
                pct = (f" ({f['pnl_pct']:+.1f}%)" if f["pnl_pct"] is not None else "")
                return f"`{f['symbol']:<10}` {pnl}{pct} · closed {str(f['closed_at'])[:10]}"

            lines = [_line(f) for f in sorted(
                filled, key=lambda x: -(x["pnl_usd"] or 0))]
            best = max(filled, key=lambda x: x["pnl_usd"] or float("-inf"))
            worst = min(filled, key=lambda x: x["pnl_usd"] or float("inf"))
            desc = (f"Σ **${sum(pnls):+.2f}** · Win-Rate {wins}/{len(pnls)} "
                    f"({wins / len(pnls) * 100:.0f}%)\n"
                    f"Best: {best['symbol']} ${best['pnl_usd']:+.2f} · "
                    f"Worst: {worst['symbol']} ${worst['pnl_usd']:+.2f}")

            fields = []
            chunk: list[str] = []
            size = 0
            for line in lines:
                if size + len(line) + 1 > 1000 and chunk:
                    fields.append({"name": f"📋 Nachgetragen ({len(fields) + 1})",
                                   "value": "\n".join(chunk), "inline": False})
                    chunk, size = [], 0
                chunk.append(line)
                size += len(line) + 1
            if chunk:
                fields.append({"name": "📋 Nachgetragen" if not fields
                               else f"📋 Nachgetragen ({len(fields) + 1})",
                               "value": "\n".join(chunk), "inline": False})
            if unresolved:
                fields.append({
                    "name": f"⚠️ Nicht ermittelbar ({len(unresolved)})",
                    "value": "\n".join(
                        f"`{u['symbol']}` closed {str(u['closed_at'])[:10]}"
                        for u in unresolved)[:1020],
                    "inline": False,
                })
            embed = {
                "title": f"📋 P/L Nachreport — {len(filled)} Trades nachgetragen",
                "description": desc,
                "color": de.COLOR_TEAL if sum(pnls) >= 0 else de.COLOR_RED,
                "fields": fields,
                "footer": {"text": "eToro RoBoCop · Einmal-Backfill (API-History)"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            ok = de._post_embed(embed, de.DISCORD_TRADE_CHANNEL)
            print(f"  Discord-Nachreport: {'gepostet' if ok else 'FEHLGESCHLAGEN'}")
        except Exception as exc:
            logger.error("Nachreport-Post fehlgeschlagen: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
