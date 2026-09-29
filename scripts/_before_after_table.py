"""Vorher/Nachher: Sigma-USD pro Signaltyp, naiv (trades.pnl_usd) vs realized
(all Tranchen, realized_by_trade — das Masse, das Kelly benutzt).
Fenster: CLOSED, created_at >= ZAESUR_DATE (2026-07-26).
Temp-Skript fuer die Self-Improvement/Ratsche Maesswechsel-Entscheidung.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bot.db.connection import DB  # noqa: E402
from bot.core.trade_pnl import realized_by_trade  # noqa: E402

ZAESUR = "2026-07-26"
dbp = ROOT / "data" / "trading.db"

with DB(str(dbp)) as db:
    # naiv: trades.pnl_usd pro signal_type
    rows_naiv = db.fetchall(
        """
        SELECT s.signal_type,
               COUNT(*)            AS n,
               ROUND(SUM(t.pnl_usd),2) AS sum_naiv
        FROM trades t
        JOIN signals s ON t.signal_id = s.id
        WHERE t.status='CLOSED'
          AND t.created_at >= ?
          AND t.pnl_usd IS NOT NULL
        GROUP BY s.signal_type
        ORDER BY sum_naiv ASC
        """,
        (ZAESUR,),
    )

    # realized: realized_by_trade (kein since = kumulativ = alle Tranchen des Trades)
    per_trade = realized_by_trade(db)

    # closed post-Zaesur trades mit signal_id + trade id
    rows_closed = db.fetchall(
        """
        SELECT t.id AS tid, s.signal_type, t.pnl_usd
        FROM trades t
        JOIN signals s ON t.signal_id = s.id
        WHERE t.status='CLOSED'
          AND t.created_at >= ?
        """,
        (ZAESUR,),
    )

real: dict[str, dict] = {}
n_by_sig: dict[str, int] = {}
for r in rows_closed:
    st = r["signal_type"]
    n_by_sig[st] = n_by_sig.get(st, 0) + 1
    slot = per_trade.get(int(r["tid"]))
    if slot:
        val = slot["realized_usd"]
    else:
        # kein Event-Ledger -> Fallback auf trades.pnl_usd (wie Kelly)
        val = r["pnl_usd"] if r["pnl_usd"] is not None else 0.0
    s = real.setdefault(st, {"realized": 0.0, "from_ledger": 0, "fallback": 0})
    s["realized"] += val
    s["from_ledger"] += 1 if slot else 0
    s["fallback"] += 0 if slot else 1

# merge tables
all_sigs = sorted(set([r["signal_type"] for r in rows_naiv]) | set(real.keys()))
print(f"{'signal_type':45s} {'n':>4s} {'sum_naiv':>11s} {'sum_real':>11s} {'ledger':>7s} {'fb':>3s}")
tot_n = tot_naiv = tot_real = 0
for st in all_sigs:
    n = n_by_sig.get(st, 0)
    naiv = None
    for r in rows_naiv:
        if r["signal_type"] == st:
            naiv = r["sum_naiv"]
    if naiv is None:
        naiv = 0.0
    rr = real.get(st, {}).get("realized", 0.0)
    led = real.get(st, {}).get("from_ledger", 0)
    fb = real.get(st, {}).get("fallback", 0)
    tot_n += n; tot_naiv += naiv; tot_real += rr
    print(f"{st[:45]:45s} {n:>4d} {naiv:>11.2f} {rr:>11.2f} {led:>7d} {fb:>3d}")
print("-" * 90)
print(f"{'GESAMT':45s} {tot_n:>4d} {tot_naiv:>11.2f} {tot_real:>11.2f}")
