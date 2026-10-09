"""fix/entry-type-quote (2026-10-07): Signal-Type Entry-Quote.

Dosiert NEUEINSTIEGE pro Signaltyp-Familie, misst NICHT den Bestand
(das ist die Aufgabe der Kategorie-Kappe). Hintergrund: Das MIXED-Cap
0.45 vom 02-10 (42b4252) begrenzten den Bestand — und wenn die dominante
Kaufsignal-Familie MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING (MIXED)
85 % des Buchs stellt, sperrt die Kappe jeden Neueinstieg statt den
schlechten Typ zu dosieren (Deadlock-Befund 07-10). MIXED ging zurueck
auf 1.0; diese Quote uebernimmt die Dosierung eines einzelnen Typs.

fix/entry-quote-category (2026-10-09): das Matching laeuft NICHT mehr auf
dem exakten signal_type-String, sondern auf Komponenten-Ebene
(_quota_applies / _quota_state_for): ein Kombo "TREND_PULLBACK,
GOLDEN_CROSS" zaehlt ab jetzt auf die Quote "TREND_PULLBACK" (BAC/CDA.PA-
Schlupfloch) und "BB_UPPER_RSI_OVERBOUGHT" / "TREND_KIPP_1H" sind in
SIGNAL_CATEGORY aufgenommen (fail-open-Luecke geschlossen).

Gemeinsam mit tests/conftest.py: keine Schreibzugriffe auf data/ —
die DB-Tests arbeiten auf einer tmp_path-Datei.
"""
from __future__ import annotations

from bot.db.connection import DB
from bot.workers.signal_worker import (
    ENTRY_QUOTA_MAX,
    ENTRY_QUOTA_WINDOW_DAYS,
    _quota_applies,
    _quota_family_key,
    _quota_state_for,
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


# ── Kategorie-/Komponenten-Matching (fix/entry-quote-category 2026-10-09) ────

def test_quota_family_key_einzelner_typ_blaibt_identisch():
    assert _quota_family_key("TREND_PULLBACK") == "TREND_PULLBACK"
    assert _quota_family_key("") == ""


def test_quota_family_key_kombo_wird_sortiert():
    # sortierte Komponenten — Key ist stabil, egal in welcher Reihenfolge
    assert _quota_family_key("GOLDEN_CROSS,TREND_PULLBACK") == \
        _quota_family_key("TREND_PULLBACK,GOLDEN_CROSS")


def test_quota_applies_subset_matching():
    """Quote greift, wenn ALLE Komponenten des Quote-Keys im Signal sind."""
    # Einzel-Quote erfasst den reinen Typ UND jedes enthaltene Kombo
    assert _quota_applies("TREND_PULLBACK", "TREND_PULLBACK")
    assert _quota_applies("TREND_PULLBACK,GOLDEN_CROSS", "TREND_PULLBACK")
    assert _quota_applies("GOLDEN_CROSS,TREND_PULLBACK", "TREND_PULLBACK")
    # ... aber nicht den komplementaeren Typ
    assert not _quota_applies("GOLDEN_CROSS", "TREND_PULLBACK")
    # Kombo-Quote (MACD+BB) matcht exakt und jeden 3er-Kombo mit beiden
    _macd_bb = "MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING"
    assert _quota_applies(_macd_bb, _macd_bb)
    assert _quota_applies(
        "RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING",
        _macd_bb)
    assert not _quota_applies("MACD_TURN_BELOW_SMA20", _macd_bb)


def test_quota_state_for_combo_zaehlt_auf_einzelquote(monkeypatch):
    """BAC/CDA.PA-Fall: Quote 'TREND_PULLBACK' (max 2) + 2 offene
    'TREND_PULLBACK,GOLDEN_CROSS' -> das Kombo-Signal ist jetzt QUOTIERT
    (vorher: exakter String-Abgleich, Quote wirkte nicht)."""
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_MAX",
                        {"TREND_PULLBACK": 2})
    counts = {"TREND_PULLBACK,GOLDEN_CROSS": 2}
    state = _quota_state_for("TREND_PULLBACK,GOLDEN_CROSS", counts, {})
    assert state is not None
    key, used, mx = state
    assert key == "TREND_PULLBACK"
    assert used == 2
    assert mx == 2


def test_quota_state_for_unbetroffen_wohne_quote(monkeypatch):
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_MAX",
                        {"TREND_PULLBACK": 2})
    assert _quota_state_for("BB_LOWER_RSI_OVERSOLD", {}, {}) is None


def test_quota_state_for_in_cycle_wird_gezaehlt(monkeypatch):
    """In-Cycle-Approval eines Kombos belegt den Quote-Slot sofort."""
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_MAX",
                        {"TREND_PULLBACK": 1})
    counts = {"TREND_PULLBACK,GOLDEN_CROSS": 1}
    # DB-Bestand allein hat die Quote (1/1) schon aufgebraucht
    assert _quota_state_for("TREND_PULLBACK,GOLDEN_CROSS", counts, {}) == \
        ("TREND_PULLBACK", 1, 1)
    # in_cycle zusaetzlich: 2/1 (Striktste Quote bleibt dieselbe)
    assert _quota_state_for(
        "TREND_PULLBACK,GOLDEN_CROSS", counts,
        {"TREND_PULLBACK,GOLDEN_CROSS": 1}) == ("TREND_PULLBACK", 2, 1)


def test_quota_state_for_strikteste_quote_gewinnt(monkeypatch):
    """Zwei Quotes greifen -> die mit dem hoechsten Ausfuehrungsgrad
    bestimmt den Skip (2/2 > 1/2)."""
    monkeypatch.setattr("bot.workers.signal_worker.ENTRY_QUOTA_MAX",
                        {"TREND_PULLBACK": 2, "GOLDEN_CROSS": 2})
    counts = {"TREND_PULLBACK,GOLDEN_CROSS": 2}
    state = _quota_state_for("TREND_PULLBACK,GOLDEN_CROSS", counts, {})
    assert state == ("TREND_PULLBACK", 2, 2)


def test_signal_category_enthaelt_fail_open_luecken():
    """BB_UPPER_RSI_OVERBOUGHT und TREND_KIPP_1H sind jetzt kartografiert —
    die Diversity-Gate-Warnung 'nicht in SIGNAL_CATEGORY' darf fuer sie
    nicht mehr feuern (Fail-Open-Luecke aus dem Plan)."""
    from bot.workers.signal_worker import (
        SIGNAL_CATEGORY,
        _get_signal_category,
    )
    assert SIGNAL_CATEGORY["BB_UPPER_RSI_OVERBOUGHT"] == "MEAN_REVERSION"
    assert SIGNAL_CATEGORY["TREND_KIPP_1H"] == "TREND_FOLLOWING"
    # Kombos mit SELL-Teil: nur das bekannte Teil maechtigt
    assert _get_signal_category("TREND_KIPP_1H,SELL") == "TREND_FOLLOWING"
    assert _get_signal_category("BB_UPPER_RSI_OVERBOUGHT") == "MEAN_REVERSION"
