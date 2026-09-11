#!/usr/bin/env python3
"""Run one leftover-close slot: all open/deferred positions for a given market slot.

Usage: python3 scripts/close_leftover_slot.py <ASX|TOKYO|HK|EU|US>

DEFERRED handling (Deferred-Order-Regel): a position whose previous close was
accepted-but-queued is only re-sent when the queued order is gone (verified
via trade history) AND the position is still open. Otherwise it is left
untouched and reported.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
import close_leftover as cl  # noqa: E402


def handle_deferred(pid: str, spec: dict, prev: dict, client) -> int:
    """Returns 0=closed, 2=still deferred, 3=failed."""
    order_id = prev.get("order_id")
    pos = cl.pos_by_id(cl.portfolio_positions(client), pid)
    if pos is None:
        print(f"{pid} ({spec['symbol']}): deferred close landed — position gone.", flush=True)
        cl.mark(json.loads(cl.STATE_FILE.read_text()), pid, "CLOSED",
                order_id=order_id, note="deferred verified on later slot run")
        cl._cleanup_state_row(pid)
        return 0
    if order_id and cl.order_gone_check(client, pid, order_id):
        print(f"{pid}: queued order {order_id} resolved, position still open → re-sending.", flush=True)
        return cl.run_close(pid)
    print(f"{pid} ({spec['symbol']}): still queued/deferred (order {order_id}) — not re-sending.", flush=True)
    return 2


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1].upper() not in ("ASX", "TOKYO", "HK", "EU", "US"):
        raise SystemExit(__doc__)
    slot = sys.argv[1].upper()
    state = json.loads(cl.STATE_FILE.read_text())
    targets = {pid: sp for pid, sp in state["spec"]["positions"].items()
               if sp["market"] == slot}
    pending = []
    for pid, spec in targets.items():
        res = state["results"].get(pid, {})
        if res.get("outcome") == "CLOSED":
            continue
        pending.append((pid, spec, res))
    if not pending:
        print(f"slot {slot}: nothing pending.")
        return 0

    client = cl.get_client()
    codes: dict = {}

    # First: re-check ALL DEFERRED positions (any market) — recovery path for
    # kills/timeouts mid-run and for queued orders that filled in-market.
    for pid, sp in state["spec"]["positions"].items():
        res = state["results"].get(pid, {})
        if res.get("outcome") == "DEFERRED":
            print(f"--- deferred re-check {pid} ({sp['symbol']}) ---", flush=True)
            codes[pid] = handle_deferred(pid, sp, res, client)

    for pid, spec, res in pending:
        if res.get("outcome") == "DEFERRED":
            codes[pid] = handle_deferred(pid, spec, res, client)
        else:
            print(f"--- {pid} ({spec['symbol']}) ---", flush=True)
            codes[pid] = cl.run_close(pid)
    summary = {pid: {0: "CLOSED", 2: "DEFERRED", 3: "FAILED"}.get(c, f"code={c}")
               for pid, c in codes.items()}
    print(f"SLOT {slot} SUMMARY: {json.dumps(summary)}")
    return 0 if all(c in (0, 2) for c in codes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
