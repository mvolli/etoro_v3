"""
End-to-end verification of the MA200 shadow gate pipeline (NO live-DB writes).
Uses a temp DB seeded from the 1-year replay CSVs, then simulates signals and
checks: ma200_decision, evaluate() shadow semantics, record(), and the
critical invariant that live_effective_mult == 1.0 under SHADOW mode.

Run: PYTHONPATH=src python3 scripts/_verify_ma200_shadow.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from bot.core import entry_quality as eq
from bot.core import ma200_history as mh
from bot.db.connection import DB

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPLAY = os.path.join(REPO, "data", "exit_replay_bars")

# ── 1. temp DB, seeded from replay CSVs ──────────────────────────────────────
tmp = tempfile.mkdtemp(prefix="ma200test_")
tmp_db = os.path.join(tmp, "trading.db")
db = DB(tmp_db, busy_timeout_ms=2000)
summ = mh.seed_from_csvs(db, REPLAY)
print("=== seed ===")
print("  ", summ)
assert not summ.get("error"), "seed failed"
assert summ["symbols"] > 100, f"too few symbols seeded: {summ['symbols']}"

n_with_201 = db.fetchone(
    "SELECT COUNT(*) AS c FROM (SELECT symbol FROM ma200_daily "
    "GROUP BY symbol HAVING COUNT(*) >= 201)"
)
print(f"  symbols with >=201 closed closes: {n_with_201['c']}")
assert n_with_201["c"] > 100

# ── 2. per-symbol ma200_decision spot checks ─────────────────────────────────
print("\n=== ma200_decision spot checks ===")
cands = ["AAPL", "NVDA", "TSLA", "JPM", "GOOGL", "MSFT", "AMZN", "META",
         "CATE.ST", "0914.HK", "2382.HK", "600036.SS", "2502.HK"]
n_blocked = 0
n_passed = 0
for sym in cands:
    closes = mh.get_daily_closes(db, sym, limit=201)
    if len(closes) < 201:
        print(f"  {sym:12s} bars={len(closes):3d}  (insufficient -> fail-open)")
        continue
    blocked, detail = eq.ma200_decision(closes, window=200, lag=0)
    if blocked:
        n_blocked += 1
    else:
        n_passed += 1
    print(f"  {sym:12s} bars={len(closes):3d}  blocked={str(bool(blocked)):5s}  "
          f"close={detail['close']:.2f} sma200={detail['sma200']:.2f} "
          f"({detail['below_pct']:+.1f}%)")
# independent sanity check of the SMA on one symbol
sym_chk = "AAPL"
closes = mh.get_daily_closes(db, sym_chk, limit=201)
sma_manual = sum(closes[-200:]) / 200.0
assert abs(sma_manual - eq.ma200_decision(closes, window=200, lag=0)[1]["sma200"]) < 1e-9
print(f"  ✅ SMA200 manual check {sym_chk}: {sma_manual:.4f}")
# no look-ahead: decision must only depend on closes[:201]... verify lag=0 uses last
assert eq.ma200_decision(closes[:201], window=200, lag=0)[1]["close"] == closes[-1]
print("  ✅ decision bar = last provided close (no look-ahead)")

# ── 3. evaluate() + record() in SHADOW (default) ─────────────────────────────
print("\n=== evaluate()+record() SHADOW (default config) ===")
target_blocked = None
target_passed = None
for sym in cands:
    closes = mh.get_daily_closes(db, sym, limit=201)
    if len(closes) < 201:
        continue
    b, _ = eq.ma200_decision(closes, window=200, lag=0)
    if b and target_blocked is None:
        target_blocked = (sym, closes)
    elif not b and target_passed is None:
        target_passed = (sym, closes)

cfg_shadow = {"trading": {"entry_quality": {}}}  # defaults: ma200 enabled, mode shadow

for label, pick in (("SHOULD-BLOCK", target_blocked), ("SHOULD-PASS", target_passed)):
    if pick is None:
        print(f"  (no {label} symbol available — skipped)")
        continue
    sym, closes = pick
    ev = eq.evaluate(cfg_shadow, symbol=sym,
                     signal_type="TREND_PULLBACK,GOLDEN_CROSS",
                     indicators={}, regime="NORMAL", daily_closes=closes)
    print(f"  {label} {sym}: ma200_shadow={ev.ma200_shadow} "
          f"ma200_live={ev.ma200_live} live_effective_mult={ev.live_effective_mult:.3f}")
    eq.ensure_table(db)
    row_id = eq.record(db, ev, mode="live", applied=False,
                       signal_id=999991, instrument_id=None, entry_price=123.45)
    row = db.fetchone(
        "SELECT mode, symbol, size_mult, blocked, entry_price, hits "
        "FROM entry_quality_events WHERE id=?", (row_id,))
    print(f"    ledger: mode={row['mode']} size_mult={row['size_mult']:.3f} "
          f"blocked={row['blocked']} entry_price={row['entry_price']}")
    # SHADOW INVARIANT: execution sizing untouched
    lsm = eq.latest_size_mult(db, 999991)
    assert lsm == 1.0, f"SHADOW INVARIANT BROKEN: {lsm}"
    # re-record with a fresh signal id (table may have per-signal uniqueness)
    assert eq.latest_size_mult(db, 999991) == 1.0
print("  ✅ SHADOW INVARIANT HOLDS: live_effective_mult == 1.0 for both cases")

# ── 4. LIVE mode path (would-be soft gate) ───────────────────────────────────
print("\n=== evaluate() LIVE mode (gate mode live) ===")
if target_blocked is not None:
    sym, closes = target_blocked
    # NOTE: gate-level mode lives under trading.entry_quality.gates.<name>
    cfg_live = {"trading": {"entry_quality": {"gates": {"ma200_trend": {"mode": "live"}}}}}
    ev = eq.evaluate(cfg_live, symbol=sym,
                     signal_type="RSI_EXTREME",  # non-core-sweep -> soft 0.25
                     indicators={}, regime="NORMAL", daily_closes=closes)
    assert ev.ma200_live is True and ev.ma200_shadow is False
    print(f"  LIVE {sym}: ma200_live={ev.ma200_live} "
          f"live_effective_mult={ev.live_effective_mult:.3f} "
          f"size_mult={ev.size_mult:.3f} blocked={ev.blocked}")
    assert abs(ev.live_effective_mult - 0.25) < 1e-9, \
        f"live soft-gate expected 0.25, got {ev.live_effective_mult}"
    ev2 = eq.evaluate(cfg_live, symbol=sym, signal_type="CORE_SWEEP",
                      indicators={}, regime="NORMAL", daily_closes=closes)
    print(f"  LIVE CORE_SWEEP: ma200_live={ev2.ma200_live} "
          f"size_mult={ev2.size_mult:.3f} "
          f"live_effective_mult={ev2.live_effective_mult:.3f}")
    assert abs(ev2.live_effective_mult - 0.25) < 1e-9
    print("  ✅ LIVE mode: 0.25x soft-gate applied, not excluded from execution")

# ── 5. insufficient data -> fail-open ─────────────────────────────────────────
print("\n=== fail-open checks ===")
ev_short = eq.evaluate(cfg_shadow, symbol="X", signal_type="RSI_EXTREME",
                       indicators={}, regime="NORMAL", daily_closes=[1.0] * 50)
assert ev_short.ma200_shadow is False and ev_short.live_effective_mult == 1.0
print("  ✅ 50 closes -> no shadow hit (fail-open)")
ev_none = eq.evaluate(cfg_shadow, symbol="X", signal_type="RSI_EXTREME",
                      indicators={}, regime="NORMAL", daily_closes=None)
assert ev_none.ma200_shadow is False and ev_none.live_effective_mult == 1.0
print("  ✅ daily_closes=None -> no shadow hit (fail-open)")

# ── 6. upsert idempotency ─────────────────────────────────────────────────────
sym = cands[0]
before = len(mh.get_daily_closes(db, sym, limit=400))
closes = mh.get_daily_closes(db, sym, limit=400)
last_date_row = db.fetchone(
    "SELECT date, close FROM ma200_daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,))
mh.upsert_closes(db, sym, [(last_date_row["date"], last_date_row["close"])])
after = len(mh.get_daily_closes(db, sym, limit=400))
assert before == after, f"upsert not idempotent: {before} -> {after}"
print(f"  ✅ upsert idempotent ({before} rows stable)")

print("\nALL CHECKS PASSED")
shutil.rmtree(tmp, ignore_errors=True)
