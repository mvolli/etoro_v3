#!/usr/bin/env python3
"""
close_violations.py
──────────────────
Closes 4 Trading Bible rule-violating positions:
  1. ENI.MI (instrumentID=6700) - Rule 1 EMERGENCY: -6.03%
  2. NVDA oldest fragment (instrumentID=1004, openRate≈380.50) - Rule 1 EMERGENCY: -4.05%
  3. XRP-USD (instrumentID=100000) - Rule 1 Hard Close: -3.37%
  4. TSLA excess (instrumentID=1246 or 3006) - close $1,749 worth, no-SL first
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# ── path bootstrap ────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bot.config import load_config
from bot.api.client import EToroClient, ClientConfig, APIError

# ── setup ─────────────────────────────────────────────────────────────────────
cfg = load_config()
client = EToroClient(cfg.api_key, cfg.user_key, ClientConfig.from_dict(vars(cfg.api)))

print("=" * 70)
print("eToro V3 — Trading Bible Violation Closer")
print("=" * 70)

# ── 1. Fetch live positions ───────────────────────────────────────────────────
print("\n[STEP 1] Fetching live positions from eToro API...")
try:
    data = client.get("/trading/info/real/pnl")
except APIError as e:
    print(f"  ERROR: Failed to fetch portfolio: {e}")
    sys.exit(1)

cp = data.get("clientPortfolio", {})
positions = cp.get("positions", [])
print(f"  → {len(positions)} live positions found")

# Extract portfolio equity
credit = float(cp.get("credit", 0) or 0)
total_amount = sum(float(p.get("amount", 0)) for p in positions)
unrealized_pnl = float(cp.get("unrealizedPnL", 0) or 0)
equity = cp.get("equity") or (credit + total_amount + unrealized_pnl)
equity = float(equity)
print(f"  → Current equity: ${equity:,.2f}")
print(f"  → Cash (credit):  ${credit:,.2f}")
print(f"  → Invested:       ${total_amount:,.2f}")

# ── 2. Display all positions for audit ────────────────────────────────────────
print("\n[STEP 2] All open positions:")
print(f"  {'positionID':<15} {'instrID':<10} {'amount':>10} {'openRate':>12} {'pnlPct':>10} {'noSL':<6}")
print("  " + "-" * 68)
for p in sorted(positions, key=lambda x: str(x.get("instrumentID", ""))):
    iid = p.get("instrumentID", "?")
    pid = p.get("positionID", "?")
    amt = float(p.get("amount", 0))
    rate = p.get("openRate", 0)
    unr = p.get("unrealizedPnL", {}) or {}
    pnl_pct = unr.get("pnLPct") or unr.get("pnlPct") or 0
    no_sl = p.get("isNoStopLoss", False)
    print(f"  {str(pid):<15} {str(iid):<10} ${amt:>9.2f} {str(rate):>12} {float(pnl_pct):>9.2f}% {'YES' if no_sl else 'no':<6}")

# ── Helper: verify position closed ────────────────────────────────────────────
def verify_position_gone(position_id: str) -> bool:
    """Re-fetch portfolio and confirm positionID no longer present."""
    try:
        live = client.get("/trading/info/real/pnl")
        live_positions = live.get("clientPortfolio", {}).get("positions", [])
        ids = {str(p.get("positionID", "")) for p in live_positions}
        return str(position_id) not in ids
    except Exception as e:
        print(f"    [WARN] Could not verify: {e}")
        return False

# ── Helper: close one position ────────────────────────────────────────────────
total_realized_pnl = 0.0
closed_positions = []
close_errors = []

def close_one(pos: dict, label: str) -> bool:
    global total_realized_pnl
    pid = str(pos.get("positionID", ""))
    iid = int(pos.get("instrumentID", 0))
    amt = float(pos.get("amount", 0))
    unr = pos.get("unrealizedPnL", {}) or {}
    pnl_usd = float(unr.get("pnL") or unr.get("pnl") or 0)
    pnl_pct = float(unr.get("pnLPct") or unr.get("pnlPct") or 0)
    rate = pos.get("openRate", "?")

    print(f"\n  → CLOSING {label}")
    print(f"    positionID={pid}  instrID={iid}  amount=${amt:.2f}")
    print(f"    openRate={rate}  pnl={pnl_pct:.2f}% (${pnl_usd:.2f})")

    try:
        result = client.close_position(position_id=pid, instrument_id=iid)
        print(f"    ✅ CLOSED — API response: {result}")
        closed_positions.append({
            "label": label,
            "positionID": pid,
            "instrumentID": iid,
            "amount": amt,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
        })
        total_realized_pnl += pnl_usd
    except APIError as e:
        print(f"    ❌ ERROR closing {pid}: {e}")
        close_errors.append({"label": label, "positionID": pid, "error": str(e)})
        return False

    # Wait 1 second then verify
    time.sleep(1)
    gone = verify_position_gone(pid)
    if gone:
        print(f"    ✅ VERIFIED: position {pid} no longer in live portfolio")
    else:
        print(f"    ⚠️  WARNING: position {pid} still shows in portfolio (may be pending)")
    return True

# ── 3. ENI.MI — close ALL fragments ──────────────────────────────────────────
print("\n" + "=" * 70)
print("[CLOSE 1] ENI.MI — instrumentID=6700 (Rule 1 EMERGENCY: -6.03%)")
print("=" * 70)

eni_positions = [p for p in positions if int(p.get("instrumentID", 0)) == 6700]
print(f"  Found {len(eni_positions)} ENI.MI position(s)")
if not eni_positions:
    print("  ⚠️  No ENI.MI positions found!")
for i, pos in enumerate(eni_positions, 1):
    close_one(pos, f"ENI.MI fragment {i}/{len(eni_positions)}")

# ── 4. NVDA — close oldest fragment (openRate ≈ 380.50) ──────────────────────
print("\n" + "=" * 70)
print("[CLOSE 2] NVDA — instrumentID=1004 (Rule 1 EMERGENCY: -4.05%, entry~$380.50)")
print("=" * 70)

nvda_positions = [p for p in positions if int(p.get("instrumentID", 0)) == 1004]
print(f"  Found {len(nvda_positions)} NVDA position(s):")
for p in nvda_positions:
    print(f"    positionID={p.get('positionID')}  openRate={p.get('openRate')}  amount=${float(p.get('amount',0)):.2f}")

if not nvda_positions:
    print("  ⚠️  No NVDA positions found!")
else:
    # Sort by openRate DESC, close the one closest to 380.50
    nvda_sorted = sorted(nvda_positions, key=lambda x: float(x.get("openRate", 0)), reverse=True)

    # Find the position with openRate closest to 380.50
    target_rate = 380.50
    nvda_target = min(nvda_positions, key=lambda x: abs(float(x.get("openRate", 0)) - target_rate))
    actual_rate = float(nvda_target.get("openRate", 0))
    print(f"  Target openRate: ${target_rate} → best match: ${actual_rate}")

    close_one(nvda_target, f"NVDA oldest fragment (openRate={actual_rate})")

# ── 5. XRP-USD — close ALL fragments ─────────────────────────────────────────
print("\n" + "=" * 70)
print("[CLOSE 3] XRP-USD — instrumentID=100000 (Rule 1 Hard Close: -3.37%)")
print("=" * 70)

xrp_positions = [p for p in positions if int(p.get("instrumentID", 0)) == 100000]
print(f"  Found {len(xrp_positions)} XRP-USD position(s)")
if not xrp_positions:
    print("  ⚠️  No XRP-USD positions found!")
for i, pos in enumerate(xrp_positions, 1):
    close_one(pos, f"XRP-USD fragment {i}/{len(xrp_positions)}")

# ── 6. TSLA excess — close $1,749 worth, no-SL first ─────────────────────────
print("\n" + "=" * 70)
print("[CLOSE 4] TSLA excess — instrumentID=1246 or 3006")
print("  Target: reduce from $2,218 → $469 (close $1,749 worth)")
print("=" * 70)

tsla_positions = [p for p in positions if int(p.get("instrumentID", 0)) in (1246, 3006)]
total_tsla = sum(float(p.get("amount", 0)) for p in tsla_positions)
print(f"  Found {len(tsla_positions)} TSLA position(s), total: ${total_tsla:.2f}")
for p in tsla_positions:
    no_sl = p.get("isNoStopLoss", False)
    print(f"    positionID={p.get('positionID')}  instrID={p.get('instrumentID')}  "
          f"amount=${float(p.get('amount',0)):.2f}  noSL={'YES' if no_sl else 'no'}")

TARGET_TSLA = 469.0
CURRENT_TSLA = total_tsla
NEED_TO_CLOSE = CURRENT_TSLA - TARGET_TSLA
print(f"\n  Current TSLA total: ${CURRENT_TSLA:.2f}")
print(f"  Target TSLA total:  ${TARGET_TSLA:.2f}")
print(f"  Need to close:      ${NEED_TO_CLOSE:.2f}")

if NEED_TO_CLOSE <= 0:
    print("  ✅ TSLA already within limits — no action needed")
else:
    # Sort: no-SL first (most dangerous), then by amount DESC
    tsla_sorted = sorted(
        tsla_positions,
        key=lambda x: (0 if x.get("isNoStopLoss", False) else 1, -float(x.get("amount", 0)))
    )

    print(f"\n  Closing order (no-SL first, then by size):")
    for p in tsla_sorted:
        no_sl = p.get("isNoStopLoss", False)
        print(f"    positionID={p.get('positionID')}  amount=${float(p.get('amount',0)):.2f}  "
              f"noSL={'YES' if no_sl else 'no'}")

    still_to_close = NEED_TO_CLOSE
    tsla_closed_amount = 0.0

    for i, pos in enumerate(tsla_sorted, 1):
        if still_to_close <= 0:
            print(f"  ✅ TSLA target reached — stopping")
            break
        amt = float(pos.get("amount", 0))
        no_sl = pos.get("isNoStopLoss", False)
        print(f"\n  Fragment {i}: ${amt:.2f} (noSL={no_sl}) — still_to_close=${still_to_close:.2f}")
        success = close_one(pos, f"TSLA fragment {i} (${amt:.2f}, noSL={no_sl})")
        if success:
            tsla_closed_amount += amt
            still_to_close -= amt
            print(f"  Running TSLA closed: ${tsla_closed_amount:.2f} / ${NEED_TO_CLOSE:.2f}")

    print(f"\n  TSLA close summary: closed ${tsla_closed_amount:.2f} of ${NEED_TO_CLOSE:.2f} target")

# ── 7. Final summary ─────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("FINAL SUMMARY")
print("=" * 70)

print(f"\n  Positions closed ({len(closed_positions)}):")
for c in closed_positions:
    print(f"  ✅ {c['label']:<45} posID={c['positionID']:<12} "
          f"${c['amount']:>8.2f}  {c['pnl_pct']:>7.2f}%  PnL=${c['pnl_usd']:>8.2f}")

if close_errors:
    print(f"\n  Errors ({len(close_errors)}):")
    for e in close_errors:
        print(f"  ❌ {e['label']}: {e['error']}")

print(f"\n  Total realized PnL from closes: ${total_realized_pnl:,.2f}")
print(f"  Previous equity estimate:       ${equity:,.2f}")
new_equity_estimate = equity + total_realized_pnl - sum(c['amount'] for c in closed_positions)
# Note: closed positions release cash (principal + pnl returned)
# More accurate: equity stays similar, just positions are liquidated
positions_closed_principal = sum(c['amount'] for c in closed_positions)
print(f"  Total principal released:       ${positions_closed_principal:,.2f}")
print(f"  Net PnL realized:               ${total_realized_pnl:,.2f}")
print()

client.close()
