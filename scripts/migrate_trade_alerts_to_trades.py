#!/usr/bin/env python3
"""
scripts/migrate_trade_alerts_to_trades.py — feat/pnl-nachreport (2026-07-28)

Einmal-Migration: LLM-Trade-Alerts, die faelschlich in #etoro-trading (MAIN)
landeten ("KI EXIT: …", "KI TIGHTEN: …", "LLM TIGHTEN (indirekt): …"),
werden nach #trades umgezogen — Discord kann Messages nicht verschieben,
daher: angereichert reposten (P/L aus trade_events/trades, falls ermittelbar)
und das Original loeschen.

Benoetigte Bot-Permissions in #etoro-trading: Read Message History +
Manage Messages (fuer DELETE).

Nutzung:
    python3 scripts/migrate_trade_alerts_to_trades.py            # Dry-Run
    python3 scripts/migrate_trade_alerts_to_trades.py --apply
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_trade_alerts")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

MAIN_CHANNEL = "1513971015108263957"    # #etoro-trading
V3_GOLIVE = datetime(2026, 6, 24, tzinfo=timezone.utc)

TITLE_MARKERS = ("KI EXIT:", "KI TIGHTEN:", "LLM TIGHTEN")
CLOSE_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%\s*der Position")
REASON_RE = re.compile(r"\*\*Grund:\*\*\s*(.+?)(?:\n|$)")


def snowflake_dt(message_id: str) -> datetime:
    """Discord-Snowflake → UTC-Zeitstempel."""
    return datetime.fromtimestamp(
        ((int(message_id) >> 22) + 1420070400000) / 1000.0, tz=timezone.utc
    )


def parse_candidate(msg: dict, bot_id: str) -> dict | None:
    """Message → Migrations-Kandidat (oder None). Pure, testbar."""
    if str(msg.get("author", {}).get("id")) != str(bot_id):
        return None
    embeds = msg.get("embeds") or []
    if not embeds:
        return None
    title = str(embeds[0].get("title") or "")
    if not any(m in title for m in TITLE_MARKERS):
        return None
    desc = str(embeds[0].get("description") or "")
    symbol = title.split(":", 1)[1].strip() if ":" in title else "?"
    m_pct = CLOSE_PCT_RE.search(desc)
    m_reason = REASON_RE.search(desc)
    kind = ("indirekt" if "indirekt" in title
            else "exit" if "KI EXIT" in title else "tighten")
    return {
        "message_id": str(msg["id"]),
        "ts": snowflake_dt(str(msg["id"])),
        "title": title,
        "symbol": symbol,
        "kind": kind,
        "close_pct": float(m_pct.group(1)) if m_pct else None,
        "reason": (m_reason.group(1).strip() if m_reason else desc[:120]),
    }


class DiscordAPI:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | list | None]:
        body = json.dumps(payload).encode() if payload is not None else None
        for _attempt in (1, 2, 3):
            try:
                conn = http.client.HTTPSConnection("discord.com", timeout=30)
                headers = {"Authorization": f"Bot {self.token}"}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read().decode("utf-8", errors="replace")
                conn.close()
            except (TimeoutError, OSError, http.client.HTTPException) as exc:
                logger.warning("HTTP-Fehler (%s %s, Versuch %d): %s — Retry",
                               method, path.split("?")[0], _attempt, exc)
                time.sleep(2.0 * _attempt)
                continue
            if resp.status == 429:
                try:
                    wait = float(json.loads(raw).get("retry_after", 1.0))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait, 10.0) + 0.1)
                continue
            data = None
            if raw:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = None
            return resp.status, data
        return 0, None  # alle Versuche fehlgeschlagen (Netz/429)


def fetch_all_candidates(api: DiscordAPI, bot_id: str) -> list[dict]:
    """#etoro-trading rueckwaerts paginieren bis v3-Go-Live."""
    candidates: list[dict] = []
    before = None
    while True:
        path = f"/api/v10/channels/{MAIN_CHANNEL}/messages?limit=100"
        if before:
            path += f"&before={before}"
        status, msgs = api.request("GET", path)
        if status != 200 or not isinstance(msgs, list) or not msgs:
            if status != 200:
                logger.error("History-Fetch %s: HTTP %s", path, status)
            break
        for msg in msgs:
            cand = parse_candidate(msg, bot_id)
            if cand:
                candidates.append(cand)
        before = msgs[-1]["id"]
        if snowflake_dt(before) < V3_GOLIVE:
            break
        time.sleep(0.4)
    return sorted(candidates, key=lambda c: c["ts"])


def enrich_from_db(db, cand: dict) -> dict:
    """P/L-Anreicherung: trade_events (±60 min), Fallback trades-Tabelle."""
    out = {**cand, "pnl_usd": None, "pnl_pct": None, "position_id": None,
           "entry_price": None, "exit_price": None}
    try:
        frm = (cand["ts"] - timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")
        to = (cand["ts"] + timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")
        row = db.fetchone(
            "SELECT position_id, pnl_usd, pnl_pct, price FROM trade_events "
            "WHERE symbol = ? AND event_type IN ('PARTIAL_CLOSE','CLOSE') "
            "AND event_at BETWEEN ? AND ? ORDER BY ABS(julianday(event_at) - julianday(?)) LIMIT 1",
            (cand["symbol"], frm, to, cand["ts"].strftime("%Y-%m-%d %H:%M:%S")),
        )
        if row:
            out.update(position_id=row["position_id"], pnl_usd=row["pnl_usd"],
                       pnl_pct=row["pnl_pct"], exit_price=row["price"])
            return out
        trow = db.fetchone(
            "SELECT api_position_id, entry_price, exit_price, pnl_usd, pnl_pct "
            "FROM trades WHERE symbol = ? AND status='CLOSED' "
            "AND closed_at BETWEEN ? AND ? LIMIT 1",
            (cand["symbol"], frm, to),
        )
        if trow:
            out.update(position_id=trow["api_position_id"],
                       entry_price=trow["entry_price"],
                       exit_price=trow["exit_price"],
                       pnl_usd=trow["pnl_usd"], pnl_pct=trow["pnl_pct"])
    except Exception as exc:
        logger.debug("Enrichment %s: %s", cand["symbol"], exc)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Reposten + Originale LOESCHEN (Default: Dry-Run)")
    args = ap.parse_args()
    dry_run = not args.apply

    from bot import discord_embeds as de
    from bot.db.connection import DB

    token = de._read_token()
    if not token:
        logger.critical("Kein DISCORD_BOT_TOKEN — Abbruch")
        return 1
    api = DiscordAPI(token)

    # Preflight: Bot-ID + Leserechte
    status, me = api.request("GET", "/api/v10/users/@me")
    if status != 200 or not isinstance(me, dict):
        logger.critical("users/@me fehlgeschlagen (HTTP %s)", status)
        return 1
    bot_id = str(me["id"])
    status, _ = api.request("GET", f"/api/v10/channels/{MAIN_CHANNEL}/messages?limit=1")
    if status == 403:
        logger.critical("403 — Bot braucht 'Read Message History' (+ 'Manage "
                        "Messages' fuers Loeschen) in #etoro-trading")
        return 1

    logger.info("Scanne #etoro-trading nach Trade-Alerts (Bot-ID %s)…", bot_id)
    candidates = fetch_all_candidates(api, bot_id)
    logger.info("%d Kandidaten gefunden", len(candidates))
    if not candidates:
        return 0

    db = DB(db_path=PROJECT_ROOT / "data" / "trading.db")
    enriched = [enrich_from_db(db, c) for c in candidates]

    print(f"\n{'DRY-RUN — ' if dry_run else ''}Zu migrierende Messages "
          f"(#etoro-trading → #trades):")
    for c in enriched:
        pnl = (f"${c['pnl_usd']:+.2f}" if c["pnl_usd"] is not None else "P/L unbekannt")
        print(f"  {c['ts']:%Y-%m-%d %H:%M}  [{c['kind']:<8}] {c['symbol']:<10} "
              f"{('-%.0f%%' % c['close_pct']) if c['close_pct'] else '     '} "
              f"{pnl:<14} msg={c['message_id']}")
    if dry_run:
        print(f"\n  {len(enriched)} Messages. Mit --apply reposten + Originale loeschen.")
        return 0

    moved = deleted = 0
    for c in enriched:
        # Idempotenz bei Re-Runs: bereits repostete Messages (system_log
        # traegt die Original-ID) nicht erneut posten — nur Delete nachholen.
        try:
            already = db.fetchone(
                "SELECT 1 FROM system_log WHERE worker='migrate_trade_alerts' "
                "AND message LIKE ? LIMIT 1", (f"%orig {c['message_id']}%",))
        except Exception:
            already = None
        if already:
            status, _ = api.request(
                "DELETE", f"/api/v10/channels/{MAIN_CHANNEL}/messages/{c['message_id']}")
            if status in (200, 204):
                deleted += 1
            logger.info("Skip %s (bereits repostet) — Delete-Status %s",
                        c["message_id"], status)
            time.sleep(1.0)
            continue
        orig_date = f"{c['ts']:%Y-%m-%d %H:%M}"
        if c["kind"] == "indirekt":
            ok = de.post_alert_embed(
                title=c["title"],
                description=(f"**Grund:** {c['reason']}\nmomentum_faded gesetzt.\n"
                             f"_(migriert aus #etoro-trading, Original {orig_date})_"),
                severity="INFO", channel="trades",
            )
        else:
            reason = (f"KI {'EXIT' if c['kind'] == 'exit' else 'TIGHTEN'} "
                      f"(migriert aus #etoro-trading, Original {orig_date}): {c['reason']}")
            ok = de.post_position_closed_embed(
                symbol=c["symbol"],
                amount_usd=0.0,
                position_id=str(c["position_id"] or ""),
                entry_price=float(c["entry_price"] or 0),
                close_price=float(c["exit_price"] or 0),
                pnl_usd=c["pnl_usd"], pnl_pct=c["pnl_pct"],
                reason=reason,
                close_pct=float(c["close_pct"] or 100.0),
            )
        if not ok:
            logger.error("Repost fehlgeschlagen fuer %s (%s) — Original bleibt",
                         c["symbol"], c["message_id"])
            continue
        moved += 1
        # SOFORT loggen (Re-Run-Idempotenz-Anker), bevor der Delete laeuft
        de.insert_system_log(
            "INFO", "migrate_trade_alerts",
            f"Migriert: {c['symbol']} [{c['kind']}] {orig_date} → #trades "
            f"(orig {c['message_id']} repostet)",
        )
        time.sleep(1.0)

        status, _ = api.request(
            "DELETE", f"/api/v10/channels/{MAIN_CHANNEL}/messages/{c['message_id']}")
        if status in (200, 204):
            deleted += 1
        else:
            logger.error("DELETE %s: HTTP %s (Manage Messages fehlt?)",
                         c["message_id"], status)
        time.sleep(1.0)

    print(f"\n  Migriert: {moved}/{len(enriched)} · Originale geloescht: {deleted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
