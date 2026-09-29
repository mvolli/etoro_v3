"""Fee analysis A vs B."""
import sqlite3, sys
sys.path.insert(0, "/home/mvolli/.hermes/workspace/etoro_v3/src")
from bot.core.trade_pnl import realized_by_trade

class DB:
    def __init__(s, p):
        s.c = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        s.c.row_factory = sqlite3.Row
    def fetchall(s, q, p=()):
        return s.c.execute(q, p).fetchall()

db = DB("/home/mvolli/.hermes/workspace/etoro_v3/data/trading.db")

for label, since in (("A", "2026-09-10"), ("B", "2026-09-20")):
    rows = db.c.execute(
        "SELECT t.id, t.symbol, t.amount_usd FROM trades t WHERE t.status='CLOSED' AND t.created_at>=?",
        (since,),
    ).fetchall()
    real = realized_by_trade(db, since=since)
    gross_open = 0.0
    tot_real = 0.0
    fee_hf = 0.0
    fee_base = 0.0
    for r in rows:
        rr = real.get(r["id"], {})
        usd = rr.get("realized_usd") if rr and rr.get("realized_usd") is not None else 0.0
        tot_real += usd
        gross_open += r["amount_usd"]
        high = r["symbol"].endswith((".AX", ".HK", ".T"))
        # open fee + close fee on full notional
        if high:
            fee_hf += r["amount_usd"] * 0.02 * 2
        else:
            fee_base += r["amount_usd"] * 0.01 * 2
    est = fee_hf + fee_base
    print(f"{label}: closed={len(rows)} realized={tot_real:+.2f} est_roundtrip_fees={est:.2f} ({100*est/gross_open:.1f}% of open notional)")

print("=== high-fee share of OPENED trades by day ===")
for r in db.c.execute("""
    SELECT date(created_at) d,
    SUM(CASE WHEN symbol LIKE '%.AX' OR symbol LIKE '%.HK' OR symbol LIKE '%.T' THEN 1 ELSE 0 END) hf,
    COUNT(*) n
    FROM trades WHERE status IN ('ACTIVE','CLOSED') AND created_at>='2026-09-14'
    GROUP BY 1 ORDER BY 1"""):
    print(f"  {r['d']}: hf={r['hf']}/{r['n']} = {100*r['hf']/r['n']:.0f}%")

print("=== realized fee drag per closed trade (B) ===")
# per-trade realized vs fee estimate
rows = db.c.execute("SELECT t.id,t.symbol,t.amount_usd FROM trades t WHERE t.status='CLOSED' AND t.created_at>='2026-09-20'").fetchall()
real = realized_by_trade(db, since="2026-09-20")
fees = sum(r["amount_usd"] * (0.04 if r["symbol"].endswith((".AX",".HK",".T")) else 0.02) for r in rows)
print(f"  B total fees ~{fees:.2f} vs realized {sum((real.get(r['id'],{}) or {}).get('realized_usd',0) or 0 for r in rows):+.2f}")
