#!/usr/bin/env python3
"""Migration: yfinance_symbol in portfolio_snapshot denormalisieren.

fix/yf-symbol-denormalization (2026-07-14):
  Alle yfinance-Calls (correlation, signal_worker, data_worker) müssen
  instrument_id -> yfinance_symbol via instruments-Tabelle auflösen.
  Dieser Join/Query-Overhead wird durch eine denormalisierte Spalte
  eliminiert.

  SSOT: instruments.yfinance_symbol bleibt die Quelle der Wahrheit.
  portfolio_snapshot.yfinance_symbol ist ein Cache (denormalisiert).
  Fail-open: NULL -> fallback zu instruments-Tabelle.

Usage:
  python3 scripts/migrate_yf_symbol.py [--apply] [--dry-run]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "trading.db"


def check_column_exists(conn: sqlite3.Connection) -> bool:
    """Prüft ob yfinance_symbol Spalte bereits existiert."""
    cursor = conn.execute(
        "PRAGMA table_info(portfolio_snapshot)"
    )
    columns = [row[1] for row in cursor.fetchall()]
    return "yfinance_symbol" in columns


def migrate_dry_run(conn: sqlite3.Connection) -> dict:
    """Dry-Run: Zeigt was geändert würde."""
    has_column = check_column_exists(conn)

    # 1. Bestehende NULL-Werte zählen (nur wenn Spalte existiert)
    if has_column:
        null_count = conn.execute(
            "SELECT COUNT(*) FROM portfolio_snapshot WHERE yfinance_symbol IS NULL"
        ).fetchone()[0]
    else:
        null_count = conn.execute("SELECT COUNT(*) FROM portfolio_snapshot").fetchone()[0]

    # 2. Match-Rate mit instruments-Tabelle prüfen
    unmatched = conn.execute("""
        SELECT ps.api_position_id, ps.instrument_id
        FROM portfolio_snapshot ps
        LEFT JOIN instruments i ON ps.instrument_id = i.instrument_id
        WHERE i.yfinance_symbol IS NULL OR i.yfinance_symbol = ''
    """).fetchall()

    return {
        "total_positions": conn.execute("SELECT COUNT(*) FROM portfolio_snapshot").fetchone()[0],
        "null_count": null_count,
        "unmatched_count": len(unmatched),
        "unmatched": unmatched[:10],  # Max 10 Beispiele
        "column_exists": has_column,
    }


def migrate_apply(conn: sqlite3.Connection) -> dict:
    """Migration durchführen."""
    results = {
        "added_column": False,
        "updated": 0,
        "skipped_null": 0,
        "errors": [],
    }

    # 1. Spalte hinzufügen wenn nötig
    if not check_column_exists(conn):
        try:
            conn.execute("ALTER TABLE portfolio_snapshot ADD COLUMN yfinance_symbol TEXT")
            results["added_column"] = True
            print("✓ Spalte yfinance_symbol hinzugefügt")
        except Exception as e:
            results["errors"].append(f"ALTER TABLE fehlgeschlagen: {e}")
            return results
    else:
        print("✓ Spalte yfinance_symbol existiert bereits")

    # 2. Bestehende NULL-Werte auffüllen
    updated = conn.execute("""
        UPDATE portfolio_snapshot
        SET yfinance_symbol = (
            SELECT i.yfinance_symbol
            FROM instruments i
            WHERE i.instrument_id = portfolio_snapshot.instrument_id
        )
        WHERE yfinance_symbol IS NULL
          AND instrument_id IN (SELECT instrument_id FROM instruments)
    """).rowcount
    results["updated"] = updated
    print(f"✓ {updated} Einträge mit yfinance_symbol aus instruments-Tabelle aktualisiert")

    # 3. Verbleibende NULL zählen
    remaining_null = conn.execute(
        "SELECT COUNT(*) FROM portfolio_snapshot WHERE yfinance_symbol IS NULL"
    ).fetchone()[0]
    results["skipped_null"] = remaining_null
    if remaining_null > 0:
        print(f"⚠ {remaining_null} Einträge ohne matching instrument_id (NULL verbleibt)")
        examples = conn.execute("""
            SELECT api_position_id, instrument_id
            FROM portfolio_snapshot
            WHERE yfinance_symbol IS NULL
            LIMIT 5
        """).fetchall()
        for pos_id, instr_id in examples:
            print(f"   - position {pos_id} (instrument_id={instr_id})")

    conn.commit()
    return results


def main():
    parser = argparse.ArgumentParser(description="Migrate yfinance_symbol to portfolio_snapshot")
    parser.add_argument("--apply", action="store_true", help="Apply migration")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        args.dry_run = True

    if not DB_PATH.exists():
        print(f"ERROR: DB nicht gefunden: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    if args.dry_run:
        print("=== DRY RUN ===")
        results = migrate_dry_run(conn)
        print(f"\nGesamt Positionen: {results['total_positions']}")
        print(f"NULL yfinance_symbol: {results['null_count']}")
        print(f"Unmatched (keine instruments): {results['unmatched_count']}")
        if results["unmatched"]:
            print("Beispiele:")
            for pos_id, instr_id in results["unmatched"]:
                print(f"  - position {pos_id} (instrument_id={instr_id})")

    conn.close()

    if args.apply:
        conn = sqlite3.connect(str(DB_PATH))
        results = migrate_apply(conn)
        conn.close()
        print(f"\n=== ERGEBNIS ===")
        print(f"Spalte hinzugefügt: {results['added_column']}")
        print(f"Einträge aktualisiert: {results['updated']}")
        print(f"Verbleibende NULL: {results['skipped_null']}")
        if results["errors"]:
            print(f"Fehler: {results['errors']}")
            sys.exit(1)
        print("✓ Migration erfolgreich")


if __name__ == "__main__":
    main()
