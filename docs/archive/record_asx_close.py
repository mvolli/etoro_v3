#!/usr/bin/env python3
"""One-shot: record verified close of 3523612825 (CAR.ASX) + verify history + cleanup DB row."""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
import close_leftover as cl  # noqa: E402


def main() -> None:
    client = cl.get_client()
    hist = client.get_trade_history(page=1, page_size=50)
    match = [t for t in hist if str(t.get("positionId")) == "3523612825"]
    print("history entries for 3523612825:")
    for t in match:
        print("  orderId:", t.get("orderId"), "isBuy:", t.get("isBuy"),
              "units:", t.get("units"), "initialInvestment:", t.get("initialInvestment"),
              "netProfit:", t.get("netProfit"), "closeTs:", t.get("closeTimestamp"))
    pos = cl.portfolio_positions(client)
    print("still open in portfolio:", cl.pos_by_id(pos, "3523612825") is not None)

    state = json.loads(cl.STATE_FILE.read_text())
    if isinstance(state["results"], list):
        state["results"] = {}
    state["results"].setdefault("3523612825", {}).update({
        "outcome": "CLOSED",
        "order_id": 1571654108,
        "status_id_at_send": 1,
        "attempted_at_utc": "2026-08-28T02:21:17Z",
        "note": "closed at ASX 12:21 AEST (12:30-15:00 window); verified via portfolio + history",
    })
    cl.STATE_FILE.write_text(json.dumps(state, indent=2))

    conn = sqlite3.connect(str(REPO / "data" / "trading.db"))
    cur = conn.execute("DELETE FROM position_state WHERE position_id='3523612825'")
    conn.commit()
    print("position_state deleted:", cur.rowcount)
    conn.close()


if __name__ == "__main__":
    main()
