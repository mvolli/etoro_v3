#!/usr/bin/env python3
"""Migration 2026-07-16: instruments.min_position_amount (eToro-Fehler 720).

Idempotent: existiert die Spalte bereits, ist das Script ein No-op.
Kontext: fix/order-error-learning — execution_worker lernt Broker-Minima
aus Order-Ablehnungen (errorCode 720, z.B. NATGAS $1000 bei Hebel x1);
signal_worker approved unterhalb des Minimums gar nicht erst.
"""
import sqlite3

DB = "data/trading.db"

conn = sqlite3.connect(DB, timeout=15)
cols = [r[1] for r in conn.execute("PRAGMA table_info(instruments)")]
if "min_position_amount" in cols:
    print("min_position_amount existiert bereits — no-op")
else:
    conn.execute("ALTER TABLE instruments ADD COLUMN min_position_amount REAL")
    conn.commit()
    print("Spalte min_position_amount angelegt")
conn.close()
