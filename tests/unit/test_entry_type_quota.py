"""fix/entry-type-quote (2026-10-07): Signal-Type Entry-Quote.

Dosiert NEUEINSTIEGE pro Signaltyp-Familie, misst NICHT den Bestand
(das ist die Aufgabe der Kategorie-Kappe). Hintergrund: Das MIXED-Cap
0.45 vom 02-10 (42b4252) begrenzten den Bestand — und wenn die dominante
Kaufsignal-Familie MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING (MIXED)
85 % des Buchs stellt, sperrt die Kappe jeden Neueinstieg statt den
schlechten Typ zu dosieren (Deadlock-Befund 07-10). MIXED ging zurueck
auf 1.0; diese Quote uebernimmt die Dosierung eines einzelnen Typs.

Gemeinsam mit tests/conftest.py: keine Schreibzugriffe auf data/ —
die DB-Tests arbeiten auf einer tmp_path-Datei.
"""
from __future__ import annotations

from bot.db.connection import DB
from bot.workers.signal_worker import (
    ENTRY_QUOTA_MAX,
    ENTRY_QUOTA_WINDOW_DAYS,
    _type_entry_counts,
    apply_entry_quota_config,
)


# ── apply_entry_quota_config ────────────────────────────────────────────────

def test_apply_laedet_window_und_quota(monkeypatch):
    """Valide Config: window_days + Quote landen in den Modulkonstanten."""
    monkeypatch.setitem(ENTRY_QUOTA_MAX, "TEST_TYPE", 0)  # vorher aufgeraeumt
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_MAX", {"TEST_TYPE": 0})
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_WINDOW_DAYS", 7)
    apply_entry_quota_config(
        {"diversity": {"type_entry_quota": {
            "window_days": 5, "TEST_TYPE": 3}}}
    )
    from bot.workers import signal_worker as sw
    assert sw.ENTRY_QUOTA_WINDOW_DAYS == 5
    assert sw.ENTRY_QUOTA_MAX["TEST_TYPE"] == 3


def test_apply_zero_deaktiviert_quote(monkeypatch):
    """0/fehlend = Quote inaktiv (aus Config dokumentiert)."""
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_MAX", {"TEST_TYPE": 3})
    apply_entry_quota_config(
        {"diversity": {"type_entry_quota": {"TEST_TYPE": 0}}}
    )
    from bot.workers import signal_worker as sw
    assert "TEST_TYPE" not in sw.ENTRY_QUOTA_MAX


def test_apply_unlesbar_blaeuft_fail_safe(monkeypatch):
    """Ungueltige Werte werden protokolliert, Default bleibt stehen."""
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_MAX", {"OLD": 2})
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_WINDOW_DAYS", 7)
    apply_entry_quota_config(
        {"diversity": {"type_entry_quota": {
            "window_days": "kaputt", "BAD": "x", "TEST_TYPE": 4}}}
    )
    from bot.workers import signal_worker as sw
    assert sw.ENTRY_QUOTA_WINDOW_DAYS == 7, "ungueltiges window_days darf Default nicht fuehren"
    assert "BAD" not in sw.ENTRY_QUOTA_MAX
    assert sw.ENTRY_QUOTA_MAX["TEST_TYPE"] == 4
    assert sw.ENTRY_QUOTA_MAX["OLD"] == 2  # bestehender Eintrag bleibt


def test_apply_ohne_block_tut_nichts(monkeypatch):
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_MAX", {"OLD": 2})
    apply_entry_quota_config({})
    from bot.workers import signal_worker as sw
    assert sw.ENTRY_QUOTA_MAX == {"OLD": 2}


# ── _type_entry_counts ──────────────────────────────────────────────────────

def _mkdb(tmp_path):
    """DB-Wrapper (wie der Worker) auf einer tmp_path-Datei, mit
    trades + signals in der Produktions-Schema."""
    db = DB(tmp_path / "quota_test.db")
    db.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT)")
    db.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, signal_id INTEGER,"
        " status TEXT, created_at TEXT)"
    )
    return db


def test_type_entry_counts_zaeht_active_im_fenster(tmp_path):
    """Ausschliesslich ACTIVE-Trades im 7d-Fenster werden je Typ gezaehlt."""
    db = _mkdb(tmp_path)
    db.execute("INSERT INTO signals VALUES (1, 'TYPE_A')")
    db.execute("INSERT INTO signals VALUES (2, 'TYPE_B')")
    # TYPE_A: 3 ACTIVE im Fenster, 1 CLOSED (zaehlt NICHT), 1 ALTES ACTIVE (außerhalb)
    for i in range(3):
        db.execute(
            "INSERT INTO trades (signal_id, status, created_at)"
            " VALUES (1, 'ACTIVE', datetime('now', '-1 day'))"
        )
    db.execute(
        "INSERT INTO trades (signal_id, status, created_at)"
        " VALUES (1, 'CLOSED', datetime('now', '-1 day'))"
    )
    db.execute(
        "INSERT INTO trades (signal_id, status, created_at)"
        " VALUES (1, 'ACTIVE', datetime('now', '-30 days'))"
    )
    # TYPE_B: 1 ACTIVE
    db.execute(
        "INSERT INTO trades (signal_id, status, created_at)"
        " VALUES (2, 'ACTIVE', datetime('now', '-2 days'))"
    )
    counts = _type_entry_counts(db)
    assert counts["TYPE_A"] == 3, f"wartete 3, bekam {counts.get('TYPE_A')}"
    assert counts["TYPE_B"] == 1


def test_type_entry_counts_leere_db(tmp_path):
    db = _mkdb(tmp_path)
    assert _type_entry_counts(db) == {}


def test_type_entry_counts_fail_open_beim_db_fehler(tmp_path):
    """Ein DB-Problem liefert den leeren Dict (fail-open, wie
    _open_signal_categories) — der Worker laeuft weiter ohne Quote."""
    class Boom:
        def fetchall(self, *a, **k):
            raise RuntimeError("db down")
    assert _type_entry_counts(Boom()) == {}
