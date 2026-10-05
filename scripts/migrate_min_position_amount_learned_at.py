#!/usr/bin/env python3
"""Migration 2026-10-05: instruments.min_position_amount_learned_at.

fix/units-only-tradability: der Rejection-Lerner schreibt seitdem bei
UnitsOnlyMinShare-Blocks den ANTALTSWERT als min_position_amount (ABB.ST:
$100 kauft 0.104 shares at $963). Ein gelernter Anteilswert veraltet mit
dem Kurs — das signal_worker-BROKER_MIN-Gate vertraut daher nur frischen
Werten (learned_at <= 7 Tage) und fail-open-t alles andere. Statische
eToro-720-Minima (NATGAS $1000) haben kein learned_at und gelten unveraendert.

Idempotent: existiert die Spalte bereits, ist das Script ein No-op.

    python3 scripts/migrate_min_position_amount_learned_at.py
"""
import sqlite3

DB = "data/trading.db"

conn = sqlite3.connect(DB, timeout=15)
cols = [r[1] for r in conn.execute("PRAGMA table_info(instruments)")]
if "min_position_amount_learned_at" in cols:
    print("min_position_amount_learned_at existiert bereits — no-op")
else:
    conn.execute(
        "ALTER TABLE instruments ADD COLUMN min_position_amount_learned_at TEXT"
    )
    conn.commit()
    print("Spalte min_position_amount_learned_at angelegt")
conn.close()
