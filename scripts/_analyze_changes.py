"""One-off analysis: performance before/after fix/fee-churn-minhold + fix/core-sweep-market-open."""
import sqlite3, sys
sys.path.insert(0, "/home/mvolli/.hermes/workspace/etoro_v3/src")
from bot.core.trade_pnl import realized_by_trade

class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.c.row_factory = sqlite3.Row
    def fetchall(self, sql, params=()):
        return self.c.execute(sql, params).fetchall()

db = DB("/home/mvolli/.hermes/workspace/etoro_v3/data/trading.db")

def cluster_map(since=None):
    q = "SELECT t.id, COALESCE(s.signal_type,'?') cluster, COALESCE(s.conviction,'?') conv FROM trades t LEFT JOIN signals s ON s.id=t.signal_id"
    p = []
    if since:
        q += " WHERE t.created_at>=?"; p.append(since)
    m = {}
    for r in db.c.execute(q, p):
        m[r["id"]] = (r["cluster"], r["conv"])
    return m

def usd_for(tid, real):
    r = real.get(tid)
    if r and r.get("realized_usd") is not None:
        return r["realized_usd"]
    tr = db.c.execute("SELECT pnl_usd, amount_usd, pnl_pct FROM trades WHERE id=?", (tid,)).fetchone()
    if tr["pnl_usd"] is not None:
        return tr["pnl_usd"]
    if tr["pnl_pct"] is not None:
        return tr["amount_usd"] * tr["pnl_pct"] / 100.0
    return 0.0

for label, since in (("A (09-10..09-20)", "2026-09-10"), ("B (09-20..now)", "2026-09-20")):
    real = realized_by_trade(db, since=since)
    cm = cluster_map(since)
    rows = db.c.execute(
        "SELECT id, amount_usd, confirmed_at FROM trades WHERE status='CLOSED' AND created_at>=?", (since,)
    ).fetchall()
    agg = {}
    convagg = {}
    total = 0.0
    for tr in rows:
        tid = tr["id"]
        cl, conv = cm.get(tid, ("?", "?"))
        usd = usd_for(tid, real)
        total += usd
        a = agg.setdefault(cl, [0, 0, 0.0, 0.0])
        a[0] += 1; a[1] += 1 if usd > 0 else 0; a[2] += usd; a[3] += tr["amount_usd"] or 0
        b = convagg.setdefault(conv, [0, 0.0])
        b[0] += 1; b[1] += usd
    print(f"=== {label}: {len(rows)} closed trades, realized total {total:+.2f} ===")
    for k, v in sorted(agg.items(), key=lambda kv: kv[1][2]):
        print(f"  {k[:48]:50s} n={v[0]:3d} wr={100*v[1]/v[0]:5.1f}% pnl={v[2]:10.2f} avg_inv={v[3]/v[0]:8.2f}")
    print("  by conviction:", {k: (v[0], round(v[1], 2)) for k, v in sorted(convagg.items())})
    # hold time check (confirmed_at -> closed_at)
    ht = db.c.execute(
        "SELECT AVG((julianday(closed_at)-julianday(confirmed_at))*24) h FROM trades WHERE status='CLOSED' AND created_at>=? AND confirmed_at IS NOT NULL", (since,)
    ).fetchone()
    print(f"  avg hold time: {ht['h']:.1f} h")
    # position sizes
    sz = db.c.execute(
        "SELECT COUNT(*) n, ROUND(AVG(amount_usd),2) avg_usd, ROUND(MIN(amount_usd),2) min_usd, ROUND(MAX(amount_usd),2) max_usd FROM trades WHERE created_at>=? AND status IN ('ACTIVE','CLOSED')", (since,)
    ).fetchone()
    print(f"  sizes: n={sz['n']} avg={sz['avg_usd']} min={sz['min_usd']} max={sz['max_usd']}")
    small = db.c.execute("SELECT COUNT(*) FROM trades WHERE created_at>=? AND status IN ('ACTIVE','CLOSED') AND amount_usd<100", (since,)).fetchone()
    print(f"  positions < $100: {small[0]} / {sz['n']}")
    # fee-tilt check: high-fee suffix share
    hf = db.c.execute(
        "SELECT SUM(CASE WHEN symbol LIKE '%.AX' OR symbol LIKE '%.HK' OR symbol LIKE '%.T' THEN 1 ELSE 0 END) hf, COUNT(*) n FROM trades WHERE created_at>=? AND status IN ('ACTIVE','CLOSED')", (since,)
    ).fetchone()
    print(f"  high-fee suffix (.AX/.HK/.T): {hf['hf']} / {hf['n']} = {100*hf['hf']/hf['n']:.0f}%")

# daily opens vs cap=30 (all statuses incl REJECTED that consumed slots)
print("=== opens per day (created_at, all statuses) ===")
for r in db.c.execute("SELECT date(created_at) d, SUM(status IN ('ACTIVE','CLOSED','CLOSING')) opened, SUM(status='REJECTED') rejected FROM trades WHERE created_at>='2026-09-19' GROUP BY 1 ORDER BY 1"):
    print(f"  {r['d']}: opened={r['opened']:3d} rejected={r['rejected']}")
