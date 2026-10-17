#!/usr/bin/env python3
"""Pruefung der ersten Kaeufe der Analysten-Strategie (VoLLi 2026-10-17: „Montag pruefen“).

Nur lesend gegen DB und Broker. Prueft fuer Q2/S1-Trades seit --since:
  - Status, Pre-Trade-Veto-Urteil (veto_decision/-reason)
  - Broker-Position: Stop-Loss ~ 0,85 x Einstieg, KEIN Take-Profit
  - Lauf des analyst_worker (system_log, Cron-Ausgabe)
Ausgabe auf stdout; mit --post zusaetzlich als Embed nach #etoro-trades.

  PYTHONPATH=src /usr/bin/python3 scripts/check_analyst_first_buys.py --since 2026-10-19 [--post]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
DB = ROOT / "data" / "trading.db"
CRON_OUT = Path.home() / ".hermes" / "cron" / "output"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--post", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    lines: list[str] = []
    ok = True

    trades = [dict(r) for r in con.execute(
        "SELECT t.id, t.symbol, t.status, t.amount_usd, t.api_position_id, t.rejection_reason, "
        "t.veto_decision, t.veto_reason, t.created_at, s.signal_type "
        "FROM trades t JOIN signals s ON s.id = t.signal_id "
        "WHERE s.signal_type IN ('SCREEN_Q2','EARN_S1','CORE_INDEX') AND t.created_at >= ? ORDER BY t.id",
        (args.since,))]
    logs = [dict(r) for r in con.execute(
        "SELECT ts, level, substr(message, 1, 160) AS m FROM system_log "
        "WHERE worker IN ('analyst_worker') AND ts >= ? ORDER BY id", (args.since,))]

    runs = sorted((CRON_OUT / "8b5d87d54042").glob(f"{args.since}*")) if (CRON_OUT / "8b5d87d54042").exists() else []
    if runs:
        txt = runs[-1].read_text(errors="ignore")
        tail = [ln for ln in txt.splitlines() if "analyst_worker:" in ln or "Traceback" in ln or "Error" in ln]
        lines.append(f"**Lauf analyst_worker:** {runs[-1].name[:16]} — " + (" | ".join(tail[-3:]) or "ohne Ausgabe"))
        if any("Traceback" in ln for ln in tail):
            ok = False
    else:
        lines.append("**Lauf analyst_worker:** keine Cron-Ausgabe gefunden ⚠️")
        ok = False
    for lg in logs[-6:]:
        lines.append(f"`{lg['ts'][11:16]}` {lg['level']} {lg['m']}")

    pos = {}
    try:
        import yaml
        from bot.api.client import ClientConfig, EToroClient
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
        client = EToroClient(api_key=os.environ["ETORO_BOT_API_KEY"], user_key=os.environ["ETORO_BOT_USER_KEY"],
                             config=ClientConfig.from_dict(cfg.get("api", {})))
        cp = client.get_portfolio().get("clientPortfolio", {})
        pos = {str(p.get("positionID")): p for p in (cp.get("positions") or [])}
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Broker nicht lesbar: {exc}")
        ok = False

    if not trades:
        lines.append("**Käufe:** keine Strategie-Trades seit " + args.since
                     + " (kein S1-Kandidat? siehe Lauf oben)")
    for t in trades:
        s = (f"**{t['symbol']}** {t['signal_type']} ${float(t['amount_usd'] or 0):.0f} · {t['status']} · "
             f"Veto {t['veto_decision'] or '—'}")
        if t["rejection_reason"]:
            s += f" · {t['rejection_reason'][:90]}"
        p = pos.get(str(t["api_position_id"] or ""))
        if p:
            op, sl, tp = float(p.get("openRate") or 0), float(p.get("stopLossRate") or 0), float(p.get("takeProfitRate") or 0)
            sl_pct = (1 - sl / op) * 100 if op and sl else None
            want = 25.0 if t["signal_type"] == "CORE_INDEX" else 15.0
            sl_ok = sl_pct is not None and abs(sl_pct - want) < 1.5
            tp_ok = (not tp) or bool(p.get("isNoTakeProfit")) or (op and tp / op > 50)
            s += (f"\n  Broker: Einstieg {op:.2f}, SL {sl:.2f} ({sl_pct:.1f} % {'✅' if sl_ok else '⚠️'}), "
                  f"TP {'keiner ✅' if tp_ok else f'{tp:.2f} ⚠️'}" if sl_pct is not None else
                  f"\n  Broker: Einstieg {op:.2f}, **kein SL ⚠️** (Software-Netz greift täglich)")
            ok = ok and sl_ok and bool(tp_ok)
        elif t["status"] == "ACTIVE":
            s += "\n  Broker-Position nicht gefunden ⚠️"
            ok = False
        if t["veto_reason"]:
            s += f"\n  Veto-Grund: {t['veto_reason'][:120]}"
        lines.append(s)

    title = f"{'✅' if ok else '⚠️'} Prüfung Analysten-Strategie — erste Käufe seit {args.since}"
    body = "\n".join(lines)
    print(title); print(body)
    if args.post:
        from bot.discord_embeds import post_alert_embed
        post_alert_embed(title=title, description=body[:1900], severity="INFO" if ok else "WARNING",
                         channel="trades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
