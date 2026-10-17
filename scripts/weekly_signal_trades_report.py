#!/usr/bin/env python3
"""Wochenbericht Signal-Trades -> Discord #reports (feat/signal-trades, VoLLi 2026-10-17).

VoLLi hat statt einer Abbruchregel einen Wochenbericht gewaehlt. Basis ist die
Broker-Wahrheit (`/trading/info/trade/history`, gruppiert ueber orderId):
netProfit enthaelt KEINE Provision, deshalb wird je Order $1 bzw. $2
abgezogen (eToro Cost&Charges: Oeffnen UND Schliessen, Teilverkauf ebenso).
Vergleich: ACWI (Index-Kern ISAC.L = MSCI ACWI) ueber dieselben Haltetage.

  PYTHONPATH=src /usr/bin/python3 scripts/weekly_signal_trades_report.py --dry-run
  PYTHONPATH=src /usr/bin/python3 scripts/weekly_signal_trades_report.py          # posten
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bot.core import signal_trades as st  # noqa: E402
from bot.core.outcome_refresh import TWO_DOLLAR_SUFFIXES  # noqa: E402

DB = ROOT / "data" / "trading.db"
START = "2026-10-17"          # Signal-Trades live seit 17.10.


def _client(cfg):
    from bot.api.client import ClientConfig, EToroClient
    return EToroClient(api_key=os.environ["ETORO_BOT_API_KEY"], user_key=os.environ["ETORO_BOT_USER_KEY"],
                       config=ClientConfig.from_dict(cfg.get("api", {})))


def _history(client, since: str) -> list[dict]:
    out, page = [], 1
    while True:
        h = client.get_trade_history(min_date=since, page=page, page_size=100, timeout=60) or []
        out += h
        if len(h) < 100:
            return out
        page += 1


def _acwi(d0: str, d1: str):
    try:
        import yfinance as yf
        df = yf.download("ACWI", start=d0, end=(datetime.fromisoformat(d1) + timedelta(days=4)).strftime("%Y-%m-%d"),
                         auto_adjust=True, progress=False)
        if hasattr(df.columns, "levels"):
            df.columns = [c[0] for c in df.columns]
        return {i.strftime("%Y-%m-%d"): (float(r.Open), float(r.Close)) for i, r in df.iterrows()}
    except Exception:  # noqa: BLE001
        return {}


def _bench(bars: dict, d0: str, d1: str):
    keys = sorted(bars)
    a = [k for k in keys if k >= d0]
    b = [k for k in keys if k <= d1]
    if not a or not b or b[-1] < a[0]:
        return None
    return bars[b[-1]][1] / bars[a[0]][0] - 1


def build(cfg, client, today: datetime) -> dict:
    stc = st.load_config(cfg)
    fams = stc["family_sets"]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    by_oid, by_pid = {}, {}
    for r in con.execute("""SELECT t.order_id, t.api_position_id, s.signal_type, t.symbol
                            FROM trades t JOIN signals s ON s.id = t.signal_id"""):
        if st.components(r["signal_type"]) not in fams:
            continue
        if r["order_id"]:
            by_oid[str(r["order_id"])] = (r["signal_type"], r["symbol"])
        if r["api_position_id"]:
            by_pid[str(r["api_position_id"])] = (r["signal_type"], r["symbol"])
    grp = defaultdict(list)
    for h in _history(client, START):
        if h.get("openTimestamp", "")[:10] >= START:
            grp[str(h["orderId"])].append(h)
    week0 = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    rows = []
    for oid, v in grp.items():
        m = by_oid.get(oid) or next((by_pid.get(str(h["positionId"])) for h in v
                                     if by_pid.get(str(h["positionId"]))), None)
        if not m:
            continue
        sig, sym = m
        fee = 2.0 if (sym or "").upper().endswith(TWO_DOLLAR_SUFFIXES) else 1.0
        v.sort(key=lambda h: h["closeTimestamp"])
        inv = float(v[0]["initialInvestment"])
        if abs(sum(float(h["investment"]) for h in v) - inv) > 0.05:
            continue                                   # noch Teil offen
        gross = sum(float(h["netProfit"]) for h in v)
        fees = fee * (1 + len(v))
        rows.append(dict(sym=sym, sig=sig, inv=inv, gross=gross, fees=fees, net=gross - fees,
                         d0=v[0]["openTimestamp"][:10], d1=v[-1]["closeTimestamp"][:10]))
    bars = _acwi(START, today.strftime("%Y-%m-%d")) if rows else {}
    for r in rows:
        b = _bench(bars, r["d0"], r["d1"])
        r["bench_usd"] = None if b is None else r["inv"] * b
    open_n = st.open_count(_DBWrap(con), stc)
    return dict(all=rows, week=[r for r in rows if r["d1"] >= week0], open_n=open_n,
                max_open=stc.get("max_open"), week0=week0)


class _DBWrap:
    def __init__(self, con):
        self.con = con

    def fetchall(self, q, p=()):
        return self.con.execute(q, p).fetchall()


def _summary(rows):
    if not rows:
        return "keine geschlossenen Trades"
    n = len(rows); net = sum(r["net"] for r in rows); inv = sum(r["inv"] for r in rows)
    fees = sum(r["fees"] for r in rows); wr = sum(1 for r in rows if r["net"] > 0) / n * 100
    b = [r for r in rows if r.get("bench_usd") is not None]
    vs = sum(r["net"] - r["bench_usd"] for r in b)
    return (f"{n} Trades · netto **{net:+.2f} $** ({net / inv * 100:+.2f} % je Einsatz) · "
            f"Provision {fees:.0f} $ · Gewinner {wr:.0f} %"
            + (f"\nGegen ACWI (gleiche Haltetage): **{vs:+.2f} $**" if b else ""))


def veto_summary(path=ROOT / "data" / "llm_veto_log.json") -> str:
    """Pre-Trade-Veto nach 28 Tagen (Zeitstopp der Kaeufe): Kurs seit Ablehnung.

    Negativ = Veto/Verkleinerung lag richtig (Kurs fiel). Seit 17.10. mit
    Regel-Vetos und allen Kaufwegen; Bewertung `delta_28d_pct` im Veto-Worker.
    """
    import json
    try:
        entries = json.loads(path.read_text()).get("entries", [])
    except Exception:  # noqa: BLE001
        return "Veto-Log nicht lesbar"
    out = []
    for dec in ("VETO", "REDUCE"):
        d = [e["delta_28d_pct"] for e in entries
             if e.get("decision") == dec and e.get("delta_28d_pct") is not None]
        if d:
            good = sum(1 for x in d if x < 0)
            out.append(f"{dec}: n={len(d)}, Kurs danach Ø {sum(d) / len(d):+.1f} %, richtig {good}/{len(d)}")
    return " · ".join(out) or "noch keine 28-Tage-Bewertungen"


def embed(rep: dict, today: datetime) -> dict:
    lines = [f"`{r['sym'][:10]:10s} {r['net']:+7.2f} $  {r['d0'][5:]}→{r['d1'][5:]}`"
             for r in sorted(rep["week"], key=lambda r: r["d1"])][:20]
    return {
        "title": f"📈 Signal-Trades — Woche bis {today:%d.%m.%Y}",
        "description": (f"**Diese Woche:** {_summary(rep['week'])}\n"
                        f"**Seit Start {START[8:10]}.{START[5:7]}.:** {_summary(rep['all'])}\n"
                        f"Offen: {rep['open_n']}/{rep['max_open']}\n"
                        f"**Pre-Trade-Veto (28 Tage):** {veto_summary()}"),
        "color": 0x2ECC71 if sum(r["net"] for r in rep["week"]) >= 0 else 0xE67E22,
        "fields": [{"name": "Geschlossen diese Woche", "value": "\n".join(lines) or "—", "inline": False}],
        "footer": {"text": "Broker-netProfit minus Provision $1/$2 je Order · VoLLi 17.10.: keine Abbruchregel"},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text()) or {}
    today = datetime.now(timezone.utc)
    rep = build(cfg, _client(cfg), today)
    e = embed(rep, today)
    print(e["title"]); print(e["description"]); print(e["fields"][0]["value"])
    if args.dry_run:
        return 0
    from bot.discord_embeds import DISCORD_REPORTS_CHANNEL, _post_embed
    return 0 if _post_embed(e, DISCORD_REPORTS_CHANNEL) else 1


if __name__ == "__main__":
    raise SystemExit(main())
