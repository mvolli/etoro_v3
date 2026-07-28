#!/usr/bin/env python3
"""daily_report_worker.py — Taeglicher Trade-Report → #reports.

feat/daily-report (2026-07-28): fasst alle Trade-Events der letzten 24h
zusammen (Opens / Teilverkaeufe / Closes aus trade_events), realisiertes
P/L, Win-Rate, nachgereichte P/Ls und den offenen Portfolio-Stand — plus
Chart-Grid (bis 4 Tages-Closes mit Entry/Exit-Markern).

Cron: 23:15 Europe/Berlin (nach US-Close; Reconciler hatte ~1h zum
Finalisieren der Tages-P/Ls). Idempotenz via system_state-Key
DAILY_REPORT_DATE — Doppel-Fires posten nicht doppelt.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("daily_report_worker")

# ── Discord Embeds (gleiche Modulinstanz-Konvention wie andere Worker) ────────
try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..'))
    import discord_embeds as _DE
except Exception:
    _DE = None


def _fmt_pnl(pnl_usd, pnl_pct) -> str:
    if pnl_usd is None:
        return "P/L folgt"
    txt = f"${pnl_usd:+.2f}"
    if pnl_pct is not None:
        txt += f" ({pnl_pct:+.1f}%)"
    return txt


def _hhmm(ts: str | None) -> str:
    s = str(ts or "")
    return s[11:16] if len(s) >= 16 else "--:--"


def build_report_data(events: list[dict], filled: list[dict],
                      snapshots: list[dict]) -> dict:
    """Pure Aggregation der Report-Zahlen + Abschnittszeilen (testbar).

    events: trade_events der letzten 24h; filled: Events mit pnl_filled_at
    im Fenster (Nachreports); snapshots: portfolio_snapshot (offene Pos.).
    """
    opens = [e for e in events if e["event_type"] == "OPEN"]
    partials = [e for e in events if e["event_type"] == "PARTIAL_CLOSE"]
    closes = [e for e in events if e["event_type"] == "CLOSE"]

    close_pnls = [e["pnl_usd"] for e in closes + partials if e["pnl_usd"] is not None]
    realized = sum(close_pnls) if close_pnls else None
    unconfirmed = sum(1 for e in closes + partials if e["pnl_usd"] is None)
    wins = sum(1 for e in closes if e["pnl_usd"] is not None and e["pnl_usd"] >= 0)
    losses = sum(1 for e in closes if e["pnl_usd"] is not None and e["pnl_usd"] < 0)

    open_count = len(snapshots)
    open_exposure = sum(float(s.get("amount_usd") or 0) for s in snapshots)
    unrealized = sum(float(s.get("unrealized_pnl") or 0) for s in snapshots)

    sections: list[tuple[str, list[str]]] = []
    if opens:
        sections.append((f"🟢 Eröffnungen ({len(opens)})", [
            f"`{e['symbol']:<10}` ${float(e['amount_usd'] or 0):,.0f}"
            + (f" @ {float(e['price']):g}" if e.get("price") else "")
            + f" · {_hhmm(e['event_at'])}"
            for e in opens
        ]))
    if partials:
        sections.append((f"✂️ Teilverkäufe ({len(partials)})", [
            f"`{e['symbol']:<10}` -{float(e['close_pct'] or 0):.0f}% · "
            f"{_fmt_pnl(e['pnl_usd'], e['pnl_pct'])} · {_hhmm(e['event_at'])}"
            for e in partials
        ]))
    if closes:
        sections.append((f"🏁 Closes ({len(closes)})", [
            f"`{e['symbol']:<10}` {_fmt_pnl(e['pnl_usd'], e['pnl_pct'])}"
            + (f" — {str(e['reason'])[:60]}" if e.get("reason") else "")
            for e in closes
        ]))
    # Nachreports: im Fenster finalisierte P/Ls, die NICHT eh schon als
    # frischer Close oben stehen
    event_ids = {e["id"] for e in closes + partials}
    late = [e for e in filled if e["id"] not in event_ids]
    if late:
        sections.append((f"📋 P/L nachgetragen ({len(late)})", [
            f"`{e['symbol']:<10}` {_fmt_pnl(e['pnl_usd'], e['pnl_pct'])} "
            f"(Event vom {str(e['event_at'])[:10]})"
            for e in late
        ]))
    if snapshots:
        top = max(snapshots, key=lambda s: float(s.get("unrealized_pnl_pct") or 0))
        flop = min(snapshots, key=lambda s: float(s.get("unrealized_pnl_pct") or 0))
        sections.append(("💼 Portfolio", [
            f"Best: `{top.get('symbol')}` {float(top.get('unrealized_pnl_pct') or 0):+.1f}% · "
            f"Worst: `{flop.get('symbol')}` {float(flop.get('unrealized_pnl_pct') or 0):+.1f}%",
        ]))

    return {
        "realized_pnl_usd": realized,
        "wins": wins,
        "losses": losses,
        "unconfirmed": unconfirmed,
        "open_count": open_count,
        "open_exposure_usd": open_exposure,
        "unrealized_pnl_usd": unrealized,
        "sections": sections,
        "closes": closes,
        "partials": partials,
        "has_activity": bool(opens or partials or closes or late),
    }


def _build_chart(client, db, data: dict) -> bytes | None:
    """Grid der Tages-Closes (max 4, nach |PnL|) mit Event-Markern."""
    try:
        from bot.core.candle_chart import daily_grid_png, pick_story_interval
        from bot.db.repo import TradeEventRepo

        event_repo = TradeEventRepo(db)
        ranked = sorted(
            [e for e in data["closes"] + data["partials"] if e.get("instrument_id")],
            key=lambda e: -abs(e.get("pnl_usd") or 0),
        )[:4]
        stories = []
        for ev in ranked:
            try:
                # Alle Events der Position als Marker (Entry + Partials + Exit)
                pos_events = (event_repo.get_by_position(ev["position_id"])
                              if ev.get("position_id") else [ev])
                chart_events = []
                opened_at = None
                for pe in pos_events:
                    if pe["event_type"] == "OPEN":
                        opened_at = pe["event_at"]
                    if pe.get("price"):
                        etype = {"OPEN": "ENTRY", "PARTIAL_CLOSE": "PARTIAL_CLOSE",
                                 "CLOSE": "EXIT"}[pe["event_type"]]
                        chart_events.append({"ts": pe["event_at"], "type": etype,
                                             "price": float(pe["price"]),
                                             "label": ""})
                days = None
                if opened_at:
                    try:
                        _o = datetime.fromisoformat(
                            str(opened_at)[:19].replace(" ", "T")
                        ).replace(tzinfo=timezone.utc)
                        days = (datetime.now(timezone.utc) - _o).total_seconds() / 86400.0
                    except ValueError:
                        pass
                interval, count, _lbl = pick_story_interval(days)
                candles = client.get_candles(int(ev["instrument_id"]), interval, count)
                pnl_txt = (f"{ev['pnl_pct']:+.1f}%" if ev.get("pnl_pct") is not None
                           else (f"${ev['pnl_usd']:+.2f}" if ev.get("pnl_usd") is not None else ""))
                stories.append({
                    "title": f"{ev['symbol']} {pnl_txt}".strip(),
                    "up": (ev.get("pnl_usd") or 0) >= 0,
                    "candles": candles,
                    "events": chart_events,
                })
            except Exception:
                continue
        return daily_grid_png(stories)
    except Exception as exc:
        logger.debug("Daily-Chart fehlgeschlagen: %s", exc)
        return None


def run(dry_run: bool = False) -> int:
    from bot.db.connection import DB
    from bot.db.repo import PortfolioRepo, StateRepo, TradeEventRepo, LogRepo

    db = DB(db_path=PROJECT_ROOT / "data" / "trading.db")
    state_repo = StateRepo(db)
    event_repo = TradeEventRepo(db)
    log_repo = LogRepo(db)

    today = datetime.now(timezone.utc).date().isoformat()
    if not dry_run and state_repo.get("DAILY_REPORT_DATE") == today:
        logger.warning("Daily Report fuer %s bereits gepostet — skip", today)
        return 0

    now = datetime.now(timezone.utc)
    frm = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    to = now.strftime("%Y-%m-%d %H:%M:%S")

    events = event_repo.get_events_between(frm, to)
    filled = event_repo.get_filled_between(frm, to)
    snapshots = PortfolioRepo(db).get_all()

    data = build_report_data(events, filled, snapshots)

    # API-Client fuer Charts (optional — Report geht auch ohne)
    client = None
    try:
        from bot.api.client import ClientConfig, EToroClient
        from bot.config import load_config
        api_key = os.environ.get("ETORO_API_KEY", "")
        user_key = os.environ.get("ETORO_USER_KEY", "")
        if api_key and user_key:
            cfg = load_config()
            api_cfg = (cfg.api if isinstance(cfg.api, dict)
                       else vars(cfg.api) if hasattr(cfg, "api") else {})
            client = EToroClient(api_key=api_key, user_key=user_key,
                                 config=ClientConfig.from_dict(api_cfg))
    except Exception as exc:
        logger.warning("API-Client nicht verfuegbar (Report ohne Chart): %s", exc)

    png = _build_chart(client, db, data) if client else None
    if png and _DE and hasattr(_DE, "attach_chart"):
        _DE.attach_chart(png)

    ok = False
    if _DE and hasattr(_DE, "post_daily_report_embed"):
        ok = _DE.post_daily_report_embed(
            report_date=today,
            realized_pnl_usd=data["realized_pnl_usd"],
            wins=data["wins"], losses=data["losses"],
            unconfirmed=data["unconfirmed"],
            open_count=data["open_count"],
            open_exposure_usd=data["open_exposure_usd"],
            unrealized_pnl_usd=data["unrealized_pnl_usd"],
            sections=data["sections"],
            dry_run=dry_run,
        )

    if ok and not dry_run:
        state_repo.set("DAILY_REPORT_DATE", today)
        try:
            log_repo.write("INFO", "daily_report",
                           f"Daily Report {today} gepostet "
                           f"(realisiert={data['realized_pnl_usd']}, "
                           f"chart={'ja' if png else 'nein'})")
        except Exception:
            pass
    logger.warning("Daily Report %s: posted=%s chart=%s aktivitaet=%s",
                   today, bool(ok), bool(png), data["has_activity"])
    return 0 if ok or dry_run else 1


def main() -> int:
    from bot.core.worker_lock import worker_lock
    dry_run = "--dry-run" in sys.argv
    with worker_lock("daily_report_worker") as acquired:
        if not acquired:
            logger.warning("daily_report_worker laeuft bereits — skip")
            return 0
        return run(dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main())
