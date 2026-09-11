#!/usr/bin/env python3
"""Leftover-fragment closer (task: leftover-cleanup-2026-08-28).

Closes ONE eToro position fully during its mid-session window, post-flight
verifies the close, records the result in leftover_close_state.json and —
only on verified close — deletes the position_state row.

Usage: python3 scripts/close_leftover.py <position_id>
Deferred-Order-Regel: an ACCEPTED (statusID 1/queued) order is never blindly
re-sent by this script; it is marked DEFERRED and re-checked on the next run
(only re-sent if the API reports it gone / position still open).
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
STATE_FILE = REPO / "scripts" / "leftover_close_state.json"
DB = REPO / "data" / "trading.db"

from bot.api.client import EToroClient, ClientConfig  # noqa: E402


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


def load_env():
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_client() -> EToroClient:
    load_env()
    api_key = os.environ.get("ETORO_BOT_API_KEY", "")
    user_key = os.environ.get("ETORO_BOT_USER_KEY", "")
    if not (api_key and user_key):
        raise SystemExit("FATAL: ETORO_BOT_API_KEY / ETORO_BOT_USER_KEY missing")
    return EToroClient(api_key=api_key, user_key=user_key,
                       config=ClientConfig(timeout_read=60.0))


def portfolio_positions(client: EToroClient) -> list:
    """List of OPEN positions from the portfolio payload.

    eToro /trading/info/real/pnl nests them under
    clientPortfolio.positions with CAPITAL-ID field names
    (positionID, instrumentID) — verified against risk_worker / reconciler.
    """
    try:
        port = client.get_portfolio()
    except Exception as e:
        log(f"get_portfolio failed: {e!r}")
        return []
    if isinstance(port, dict):
        cp = port.get("clientPortfolio")
        if isinstance(cp, dict):
            for key in ("positions", "openPositions"):
                val = cp.get(key)
                if isinstance(val, list):
                    return val
        for key in ("positions", "openPositions", "items"):
            val = port.get(key)
            if isinstance(val, list):
                return val
    if isinstance(port, list):
        return port
    return []


def pos_by_id(positions: list, position_id: str) -> dict | None:
    for p in positions:
        if str(p.get("positionID", p.get("positionId"))) == position_id:
            return p
    return None


def order_gone_check(client: EToroClient, position_id: str, order_id) -> bool:
    """Check recent trade history for the queued order; True if resolved/gone."""
    try:
        hist = client.get_trade_history(page=1, page_size=50)
    except Exception as e:
        log(f"trade-history check failed: {e!r}")
        return False
    if not isinstance(hist, list):
        return False
    for t in hist:
        if t.get("positionId") in (position_id, int(position_id)) and t.get("orderId") == order_id:
            return True  # order resolved into a trade record
    return False


def mark(state: dict, position_id: str, outcome: str, **extra) -> None:
    if isinstance(state["results"], list):  # normalize legacy list → dict
        state["results"] = {}
    entry = state["results"].setdefault(position_id, {})
    entry.update({"outcome": outcome, "attempted_at_utc":
                 datetime.now(timezone.utc).isoformat(timespec="seconds"), **extra})
    STATE_FILE.write_text(json.dumps(state, indent=2))


def run_close(position_id: str) -> int:
    """Close one leftover position fully. Returns 0=CLOSED, 2=DEFERRED, 3=FAILED, 1=abort."""
    state = json.loads(STATE_FILE.read_text())
    if isinstance(state["results"], list):  # normalize legacy list → dict
        state["results"] = {}
        STATE_FILE.write_text(json.dumps(state, indent=2))
    spec = state["spec"]["positions"].get(position_id)
    if not spec:
        raise SystemExit(f"FATAL: {position_id} not in leftover-close state file")
    if position_id in state["results"] and state["results"][position_id].get("outcome") == "CLOSED":
        log(f"{position_id} already CLOSED — nothing to do.")
        return 0

    instrument_id = spec["instrument_id"]
    client = get_client()
    log(f"=== Leftover close: position={position_id} {spec['symbol']} (instr {instrument_id}) slot {spec['slot']}")

    # Pre-flight: position must still exist and be open
    positions = portfolio_positions(client)
    if not positions:
        log("WARNING: portfolio returned no positions — cannot verify; aborting without action")
        mark(state, position_id, "ABORT_NO_PORTFOLIO")
        return 1
    pos = pos_by_id(positions, position_id)
    if pos is None:
        log(f"position {position_id} not in portfolio (already closed?) — verifying via history")
        # Could have been closed between runs; treat as success if no open pos
        mark(state, position_id, "CLOSED", note="not present in portfolio at run start")
        _cleanup_state_row(position_id)
        return 0
    log(f"pre-flight: units={pos.get('units')} investment={pos.get('investment')} "
        f"currentValue={pos.get('currentValue')} status={pos.get('status')}")

    # Send full close (no UnitsToDeduct)
    resp = None
    try:
        resp = client.close_position(position_id, instrument_id)
        log(f"close_position response: {json.dumps(resp)[:400]}")
    except Exception as e:
        log(f"close_position raised: {e!r}")
        mark(state, position_id, "SEND_FAILED", error=repr(e))
        return 1

    # Parse statusID (numeric: 3=executed, 1=queued, 4=not executed)
    status_id = None
    order_id = None
    if isinstance(resp, dict):
        ofc = resp.get("orderForClose")
        if isinstance(ofc, dict):
            status_id = ofc.get("statusID")
            order_id = ofc.get("orderID")
        status_id = resp.get("statusID", resp.get("statusId")) or status_id
        order_id = resp.get("orderId", resp.get("orderID")) or order_id
        if isinstance(resp.get("positions"), list) and resp["positions"]:
            status_id = resp["positions"][0].get("statusID", status_id)
            order_id = resp["positions"][0].get("orderId", resp["positions"][0].get("orderID", order_id))
    log(f"parsed: statusID={status_id} orderId={order_id}")

    # Post-flight verification.
    # US positions close instantly while the market is open — one or two
    # polls are enough. For queued/deferred orders the DEFERRED re-check in
    # close_leftover_slot.py does the work on a later run (or next market
    # slot) — this script never blocks long on a market that is closed.
    verified = False
    for attempt in range(4):
        time.sleep(8)
        positions = portfolio_positions(client)
        if not positions:
            log("verification poll: portfolio empty — inconclusive, retrying")
            continue
        if pos_by_id(positions, position_id) is None:
            verified = True
            break
        log(f"verification poll {attempt + 1}: still open, waiting")

    if verified:
        log(f"VERIFY OK: position {position_id} ({spec['symbol']}) fully closed.")
        mark(state, position_id, "CLOSED", order_id=order_id,
             status_id_at_send=status_id,
             pnl_note="see eToro history / trade_events for final P/L")
        _cleanup_state_row(position_id)
        return 0

    # Executed at send time but still showing — one more poll round
    if status_id == 3:
        time.sleep(15)
        positions = portfolio_positions(client)
        if positions and pos_by_id(positions, position_id) is None:
            log(f"VERIFY OK (late): {position_id} closed.")
            mark(state, position_id, "CLOSED", order_id=order_id,
                 status_id_at_send=status_id, note="late-verified")
            _cleanup_state_row(position_id)
            return 0

    if order_id is not None and (status_id in (1, 3) or status_id is None):
        # Accepted/queued: Deferred-Order-Regel — NEVER blind-resend.
        # The slot runner re-checks: position gone → CLOSED; queued order
        # gone AND position still open → re-send once.
        log(f"DEFERRED: order {order_id} accepted (statusID={status_id}), "
            f"fill not yet visible — re-check on next run.")
        mark(state, position_id, "DEFERRED", order_id=order_id,
             status_id_at_send=status_id,
             note="accepted at send; verify on next slot run; resend ONLY if order gone AND position still open")
        return 2

    # Rejected or unparseable
    reason = None
    if isinstance(resp, dict):
        reason = resp.get("reason", resp.get("rejectionReason"))
        if not reason and isinstance(resp.get("positions"), list) and resp["positions"]:
            reason = resp["positions"][0].get("reason")
    log(f"NOT-CLOSED outcome: statusID={status_id} reason={reason}")
    mark(state, position_id, "FAILED", order_id=order_id,
         status_id_at_send=status_id, reason=reason,
         note="do not blind-resend; inspect before next attempt")
    return 3


def _cleanup_state_row(position_id: str) -> None:
    try:
        conn = sqlite3.connect(str(DB))
        cur = conn.execute(
            "DELETE FROM position_state WHERE position_id = ?", (str(position_id),))
        conn.commit()
        log(f"position_state row for {position_id}: deleted={cur.rowcount}")
        conn.close()
    except Exception as e:
        log(f"WARNING: position_state cleanup failed: {e!r}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.exit(run_close(sys.argv[1].strip()))
