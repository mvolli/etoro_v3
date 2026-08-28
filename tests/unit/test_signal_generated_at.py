"""generated_at-Zeitbasis (fix/signals-generated-at-tz).

Die Live-Tabelle trug DEFAULT (datetime('now','utc')) aus der Zeit vor c9eabc8.
In SQLite ist datetime('now') bereits UTC — der Zusatz 'utc' zieht den lokalen
Offset ein ZWEITES Mal ab. Jedes Signal war dadurch um 1-2 Stunden
zurueckdatiert, und jede zeitbasierte Diagnose las sich falsch (ein scheinbarer
Zwei-Stunden-Ausfall der Signalerzeugung war in Wahrheit nur dieser Versatz).

Diese Tests decken beides ab: dass SQLite sich so verhaelt wie beschrieben, und
dass create() den Wert selbst setzt statt sich auf die Spalten-Vorgabe zu
verlassen.
"""
import pathlib
import sqlite3

import pytest


def test_sqlite_utc_modifier_zieht_offset_doppelt_ab():
    """Haelt die Ursache fest — falls jemand den Zusatz fuer harmlos haelt."""
    db = sqlite3.connect(":memory:")
    ohne = db.execute("SELECT datetime('now')").fetchone()[0]
    mit = db.execute("SELECT datetime('now','utc')").fetchone()[0]
    # In einer UTC-Umgebung sind beide gleich; sonst liegt 'utc' zurueck.
    assert mit <= ohne, (
        "datetime('now','utc') darf nie SPAETER liegen als datetime('now')"
    )


def test_create_setzt_generated_at_selbst():
    """Der Wert darf nicht aus der Spalten-Vorgabe kommen."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src" / "bot" / "db" / "repo.py").read_text(encoding="utf-8")
    i = src.find("INSERT INTO signals")
    assert i != -1
    block = src[i:i + 500]
    assert "generated_at" in block, (
        "create() ueberlaesst generated_at der Spalten-Vorgabe — die traegt in "
        "bestehenden Datenbanken noch datetime('now','utc')"
    )
    assert "datetime('now')" in block


def test_init_db_hat_die_korrekte_vorgabe():
    sql = (pathlib.Path(__file__).resolve().parents[2]
           / "scripts" / "init_db.py").read_text(encoding="utf-8")
    zeile = next(z for z in sql.splitlines() if "generated_at" in z and "DEFAULT" in z)
    assert "datetime('now')" in zeile
    assert "'utc'" not in zeile, f"Fehlerhafte Vorgabe zurueck: {zeile.strip()}"


def test_generated_at_und_expires_at_auf_gleicher_zeitbasis(tmp_path):
    """Ein frisch angelegtes Signal muss in der Zukunft ablaufen, nicht in der
    Vergangenheit — genau das war der beobachtbare Schaden."""
    p = tmp_path / "t.db"
    db = sqlite3.connect(p)
    db.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY,
            generated_at TEXT NOT NULL DEFAULT (datetime('now','utc')),
            expires_at TEXT NOT NULL
        )""")
    # So wie create() es jetzt macht: beide Spalten explizit, gleiche Basis.
    db.execute("INSERT INTO signals (generated_at, expires_at) "
               "VALUES (datetime('now'), datetime('now','+60 minutes'))")
    gen, exp = db.execute("SELECT generated_at, expires_at FROM signals").fetchone()
    assert gen < exp
    jetzt = db.execute("SELECT datetime('now')").fetchone()[0]
    assert gen <= jetzt, "generated_at darf nicht in der Zukunft liegen"
    assert exp > jetzt, "Ein frisches Signal muss noch gueltig sein"

    # Gegenprobe: die alte Vorgabe datiert zurueck.
    db.execute("INSERT INTO signals (expires_at) VALUES (datetime('now','+60 minutes'))")
    alt = db.execute("SELECT generated_at FROM signals ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert alt <= gen
