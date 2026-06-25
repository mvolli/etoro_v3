#!/usr/bin/env python3
"""
Fix instrument IDs in trading.db — one-shot migration.
Verified by watchlist API on 2026-06-24.
Run: python3 scripts/fix_instrument_ids.py
"""
import sqlite3
from datetime import datetime

DB_PATH = "data/trading.db"
now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA foreign_keys=OFF")

def show():
    rows = db.execute("SELECT instrument_id, symbol, asset_class FROM instruments ORDER BY instrument_id").fetchall()
    for r in rows:
        print(f"  {r[0]:>8}: {r[1]:<18} ({r[2]})")
    print(f"  [{len(rows)} total]")

print("=== BEFORE ===")
show()

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Simple symbol renames (no PK/UNIQUE collision possible)
# ─────────────────────────────────────────────────────────────────────────────
# 6700: ENI.MI → XPEV  (watchlist confirms ID 6700 = XPEV)
db.execute("UPDATE instruments SET symbol='XPEV', asset_class='INTL', last_updated=? WHERE instrument_id=6700", (now,))
print("✓ 6700: ENI.MI → XPEV")

# 1246: NFLX → TTE.PA  (watchlist confirms ID 1246 = TTE.PA)
db.execute("UPDATE instruments SET symbol='TTE.PA', asset_class='INTL', last_updated=? WHERE instrument_id=1246", (now,))
print("✓ 1246: NFLX → TTE.PA")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Delete fake placeholder IDs (hash-based, no real positions/trades)
# ─────────────────────────────────────────────────────────────────────────────
for fake_id, real_id, sym in [(3300, 100000, "BTC-USD"), (3301, 100001, "ETH-USD")]:
    t = db.execute("SELECT COUNT(*) FROM trades WHERE instrument_id=?", (fake_id,)).fetchone()[0]
    s = db.execute("SELECT COUNT(*) FROM signals WHERE instrument_id=?", (fake_id,)).fetchone()[0]
    if t: db.execute("UPDATE trades SET instrument_id=? WHERE instrument_id=?", (real_id, fake_id))
    if s: db.execute("UPDATE signals SET instrument_id=? WHERE instrument_id=?", (real_id, fake_id))
    db.execute("DELETE FROM instruments WHERE instrument_id=?", (fake_id,))
    print(f"✓ Deleted placeholder {sym}@{fake_id} (trades={t}, signals={s})")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Fix 100000: XRP-USD → BTC-USD (BTC@3300 now gone)
# ─────────────────────────────────────────────────────────────────────────────
db.execute("UPDATE instruments SET symbol='BTC-USD', asset_class='CRYPTO', last_updated=? WHERE instrument_id=100000", (now,))
print("✓ 100000: XRP-USD → BTC-USD")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Fix ETH-USD@100001 (already exists in DB, ensure correct values)
# ─────────────────────────────────────────────────────────────────────────────
if db.execute("SELECT 1 FROM instruments WHERE instrument_id=100001").fetchone():
    db.execute("UPDATE instruments SET symbol='ETH-USD', asset_class='CRYPTO', last_updated=? WHERE instrument_id=100001", (now,))
    print("✓ 100001: ETH-USD updated")
else:
    db.execute("INSERT INTO instruments (instrument_id, symbol, asset_class, last_updated) VALUES (100001,'ETH-USD','CRYPTO',?)", (now,))
    print("✓ 100001: ETH-USD inserted")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5: TSLA 1004→1111 and MSFT 2001→1004
#   Problem: symbol UNIQUE + instrument_id PK
#   Solution:
#     a) Get TSLA trade/signal counts at 1004
#     b) Delete row 1004 (TSLA) - symbol 'TSLA' is now free
#     c) Delete row 2001 (MSFT) - both slots free
#     d) Insert MSFT@1004
#     e) Insert TSLA@1111
#     f) Update trades/signals
# ─────────────────────────────────────────────────────────────────────────────
tsla_trades = db.execute("SELECT COUNT(*) FROM trades WHERE instrument_id=1004").fetchone()[0]
tsla_sigs   = db.execute("SELECT COUNT(*) FROM signals WHERE instrument_id=1004").fetchone()[0]
msft_trades = db.execute("SELECT COUNT(*) FROM trades WHERE instrument_id=2001").fetchone()[0]
msft_sigs   = db.execute("SELECT COUNT(*) FROM signals WHERE instrument_id=2001").fetchone()[0]

# Delete both rows
db.execute("DELETE FROM instruments WHERE instrument_id=1004")
db.execute("DELETE FROM instruments WHERE instrument_id=2001")
# Insert at correct IDs
db.execute("INSERT INTO instruments (instrument_id, symbol, asset_class, last_updated) VALUES (1004,'MSFT','US_TECH',?)", (now,))
db.execute("INSERT INTO instruments (instrument_id, symbol, asset_class, last_updated) VALUES (1111,'TSLA','US_TECH',?)", (now,))
# Migrate trades/signals
if tsla_trades: db.execute("UPDATE trades SET instrument_id=1111 WHERE instrument_id=1004")
if tsla_sigs:   db.execute("UPDATE signals SET instrument_id=1111 WHERE instrument_id=1004")
if msft_trades: db.execute("UPDATE trades SET instrument_id=1004 WHERE instrument_id=2001")
if msft_sigs:   db.execute("UPDATE signals SET instrument_id=1004 WHERE instrument_id=2001")
print(f"✓ TSLA: 1004→1111 (trades={tsla_trades}), MSFT: 2001→1004 (trades={msft_trades})")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6: Add ENI.MI@1283 (correct ID per watchlist)
# ─────────────────────────────────────────────────────────────────────────────
if not db.execute("SELECT 1 FROM instruments WHERE instrument_id=1283").fetchone():
    if not db.execute("SELECT 1 FROM instruments WHERE symbol='ENI.MI'").fetchone():
        db.execute("INSERT INTO instruments (instrument_id, symbol, asset_class, last_updated) VALUES (1283,'ENI.MI','INTL',?)", (now,))
        print("✓ Added ENI.MI@1283")
    else:
        db.execute("UPDATE instruments SET instrument_id=1283, last_updated=? WHERE symbol='ENI.MI'", (now,))
        print("✓ ENI.MI moved to 1283")
else:
    print("~ ENI.MI@1283 already present")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7: Add XRP-USD@100003 and BCH-USD@100002 (confirmed via watchlist)
# ─────────────────────────────────────────────────────────────────────────────
db.execute("INSERT OR IGNORE INTO instruments (instrument_id, symbol, asset_class, last_updated) VALUES (100002,'BCH-USD','CRYPTO',?)", (now,))
db.execute("INSERT OR IGNORE INTO instruments (instrument_id, symbol, asset_class, last_updated) VALUES (100003,'XRP-USD','CRYPTO',?)", (now,))
print("✓ BCH-USD@100002, XRP-USD@100003")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8: Add additional watchlist-confirmed instruments (skip on conflict)
# ─────────────────────────────────────────────────────────────────────────────
new_entries = [
    (17,   'OIL',    'COMMODITY'),
    (18,   'GOLD',   'COMMODITY'),
    (19,   'SILVER', 'COMMODITY'),
    (22,   'NATGAS', 'COMMODITY'),
    (1001, 'AAPL',   'US_TECH'),
    (1002, 'GOOG',   'US_TECH'),
    (1037, 'C',      'FINANCIAL'),
    (1137, 'NVDA',   'US_TECH'),
]
for iid, sym, ac in new_entries:
    sym_taken = db.execute("SELECT instrument_id FROM instruments WHERE symbol=?", (sym,)).fetchone()
    id_taken  = db.execute("SELECT symbol FROM instruments WHERE instrument_id=?", (iid,)).fetchone()
    if sym_taken:
        if sym_taken[0] == iid:
            pass  # already correct
        else:
            print(f"  ⚠ {sym} exists at ID {sym_taken[0]} (not {iid}) — skip")
    elif id_taken:
        print(f"  ⚠ ID {iid} occupied by {id_taken[0]} — cannot add {sym}")
    else:
        db.execute("INSERT INTO instruments (instrument_id, symbol, asset_class, last_updated) VALUES (?,?,?,?)", (iid, sym, ac, now))
        print(f"✓ Added {sym}@{iid}")

# ─────────────────────────────────────────────────────────────────────────────
# COMMIT
# ─────────────────────────────────────────────────────────────────────────────
db.commit()
db.execute("PRAGMA foreign_keys=ON")

print("\n=== AFTER ===")
show()
db.close()
print("\n✅ Migration complete — trading.db updated")
