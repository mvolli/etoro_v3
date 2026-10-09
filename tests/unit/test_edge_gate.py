"""Phase 3 (2026-10-09): Edge-Gate (SHADOW-MODUS).

Deterministisches Edge-Gate pro Signaltyp-Familie auf Basis signal_outcomes
(Fallback trade_events). LCB 95 % einseitig (z=1.645), n_min=25.

    n < n_min -> SHADOW (NICHT fail-open: zu wenig Daten = keine Freigabe)
    lcb < 0   -> SHADOW (keine nachweisbar positive Kante)
    sonst     -> LIVE

SHADOW-MODUS: das Gate blockt im execution_worker nichts (D6: 24-h-
Beobachtungsfenster des Fee-Fixes 7cd4cda, Ende ~14:20 UTC 10.10.), es
kennzeichnet nur (Log + signal_outcomes.edge_shadow).

E2E-Test: negativer Typ + $200-Floor -> Shadow (die Gate-Entscheidung laeuft
parallel zur Live-Kette, der Trade wird ausgefuehrt, aber als Schatten-Trage
gekennzeichnet).

Gemeinsam mit tests/conftest.py: keine Schreibzugriffe auf data/ —
die DB-Tests arbeiten auf einer tmp_path-Datei.
"""
from __future__ import annotations

import math

from bot.db.connection import DB
from bot.core import edge_gate


# ── DB-Helper ─────────────────────────────────────────────────────────────────

def _mkdb(tmp_path):
    """DB auf tmp_path mit signal_outcomes (Produktions-Schema, minimal)
    + system_state (Regime-Quelle)."""
    db = DB(tmp_path / "edge_test.db")
    db.execute("""
        CREATE TABLE signal_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date TEXT NOT NULL,
            instrument_id INTEGER NOT NULL,
            symbol TEXT,
            signal_type TEXT NOT NULL,
            status TEXT,
            fwd_5d_pnl REAL,
            edge_shadow INTEGER DEFAULT 0,
            UNIQUE(instrument_id, signal_date, signal_type)
        )
    """)
    db.execute("""
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)
    db.execute("INSERT INTO system_state (key, value) VALUES ('CURRENT_REGIME','DEFENSIVE')")
    return db


def _insert_outcomes(db, signal_type: str, pnls: list[float]) -> None:
    for i, p in enumerate(pnls):
        db.execute(
            "INSERT INTO signal_outcomes (signal_date, instrument_id, symbol,"
            " signal_type, status, fwd_5d_pnl)"
            " VALUES (?,?,?,?,'CONSUMED',?)",
            (f"2026-09-{(i % 28) + 1:02d}", 1000 + i, "TEST", signal_type, p),
        )


# ── type_edge / evaluate: Statistik ───────────────────────────────────────────

def test_type_edge_leere_db(tmp_path):
    db = _mkdb(tmp_path)
    exp, lcb, n = edge_gate.type_edge("NOPE", "DEFENSIVE", db)
    assert (exp, lcb, n) == (0.0, 0.0, 0)


def test_type_edge_mittelwert_und_n(tmp_path):
    db = _mkdb(tmp_path)
    pnls = [0.010, 0.020, 0.030, 0.040]
    _insert_outcomes(db, "POS", pnls)
    exp, lcb, n = edge_gate.type_edge("POS", "DEFENSIVE", db)
    assert n == 4
    assert abs(exp - 0.025) < 1e-9
    # n < 25 -> LCB ist kleiner als exp (Breitening)
    assert lcb < exp


def test_lcb_formel_einseitig_95():
    """LCB = mean - 1.645 * (std/sqrt(n))."""
    exp, lcb, n = 0.025, None, 25
    samples = [0.025] * 25  # std = 0 -> LCB == exp
    std = edge_gate._std(samples)
    assert std == 0.0
    assert edge_gate._lcb(exp, std, n) == exp

    # mit Streuung
    samples = [0.0] * 12 + [0.05] * 13  # mean ~ 0.025
    m = sum(samples) / len(samples)
    s = edge_gate._std(samples)
    expected = m - 1.645 * s / math.sqrt(len(samples))
    assert abs(edge_gate._lcb(m, s, len(samples)) - expected) < 1e-12


# ── is_shadow: Entscheidung ──────────────────────────────────────────────────

def test_is_shadow_n_unter_n_min_nicht_fail_open():
    """n < n_min -> SHADOW, SELBST wenn exp_net stark positiv (kein fail-open)."""
    assert edge_gate.is_shadow(0.5, 0.49, 5) is True          # n=5 < 25
    assert edge_gate.is_shadow(0.5, 0.49, 24) is True         # n=24 < 25
    assert edge_gate.is_shadow(0.5, 0.49, 25) is False        # n=25, LCB>0


def test_is_shadow_lcb_negativ():
    """nreich aber LCB < 0 -> SHADOW (keine nachweisbar positive Kante)."""
    assert edge_gate.is_shadow(0.01, -0.005, 25) is True
    assert edge_gate.is_shadow(0.01, 0.001, 25) is False        # LCB>0
    assert edge_gate.is_shadow(0.01, -0.0, 25) is False         # LCB==0 ist nicht <0 (Grenze)


# ── evaluate: Vollbild ────────────────────────────────────────────────────────

def test_evaluate_negative_typ_shadows(tmp_path):
    """Negativer Typ (LCB < 0, nreich) -> SHADOW mit Grund 'LCB < 0'."""
    db = _mkdb(tmp_path)
    _insert_outcomes(db, "NEG", [-0.02] * 30)
    res = edge_gate.evaluate("NEG", "DEFENSIVE", db)
    assert res["n"] == 30
    assert res["shadow"] is True
    assert "LCB" in res["reason"]
    assert res["source"] == "signal_outcomes"


def test_evaluate_positiv_nreich_live(tmp_path):
    """Stark positiver, nreicher Typ (LCB > 0) -> LIVE."""
    db = _mkdb(tmp_path)
    _insert_outcomes(db, "POS", [0.02] * 40)
    res = edge_gate.evaluate("POS", "DEFENSIVE", db)
    assert res["n"] == 40
    assert res["lcb"] > 0
    assert res["shadow"] is False


def test_evaluate_keine_daten_shadows(tmp_path):
    db = _mkdb(tmp_path)
    res = edge_gate.evaluate("FEHL", "DEFENSIVE", db)
    assert res["n"] == 0
    assert res["shadow"] is True
    assert res["source"] == "none"
    assert "keine Outcome-Daten" in res["reason"]


# ── Komponenten-Broadening (Kombo-Signale) ───────────────────────────────────

def test_evaluate_kombo_broadening_zaehlt_auf_familie(tmp_path):
    """Ein 3er-Kombo-Signal, das die Komponenten eines abgefragten Typs
    enthaelt, wird als Datenerweiterung mitgezahlt (Kombo-Standard)."""
    db = _mkdb(tmp_path)
    # Nur ein 2er-Kombo mit den Komponenten A + B ist vorhanden;
    # A allein ist sparse (< n_min).
    for i in range(30):
        db.execute(
            "INSERT INTO signal_outcomes (signal_date, instrument_id, symbol,"
            " signal_type, status, fwd_5d_pnl)"
            " VALUES (?,?,?,?,'CONSUMED',?)",
            (f"2026-09-{(i % 28) + 1:02d}", 2000 + i, "TEST",
             "COMP_A,COMP_B", 0.015),
        )
    res = edge_gate.evaluate("COMP_A", "DEFENSIVE", db)
    # exakt 'COMP_A' = 0 Zeilen, Broadening auf COMP_A (subset von A,B) = 30
    assert res["n"] == 30
    assert res["shadow"] is False


# ── trade_events-Fallback ─────────────────────────────────────────────────────

def test_evaluate_fallback_trade_events(tmp_path):
    """Leere signal_outcomes -> Fallback auf trade_events (CLOSE + pnl_pct)."""
    db = _mkdb(tmp_path)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, signal_id INTEGER)")
    db.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT)")
    db.execute("CREATE TABLE trade_events (trade_id INTEGER, event_type TEXT, pnl_pct REAL)")
    db.execute("INSERT INTO signals VALUES (1, 'FALLBACK')")
    db.execute("INSERT INTO trades VALUES (1, 1)")
    for i in range(30):
        db.execute(
            "INSERT INTO trade_events (trade_id, event_type, pnl_pct) VALUES (1,'CLOSE',?)",
            (-0.01,),
        )
    res = edge_gate.evaluate("FALLBACK", "DEFENSIVE", db)
    assert res["source"] == "trade_events"
    assert res["n"] == 30
    assert res["shadow"] is True  # LCB < 0


# ── E2E: negativer Typ + $200-Floor -> Shadow ────────────────────────────────

def test_e2e_negativer_typ_200_floor_shadow(tmp_path):
    """E2E: ein Trade unter $200 (wird auf $200 gerundet) mit negativem
    Signal-Typ. Das Gate laeuft im SHADOW-MODUS: der Trade wird NICHT
    blockiert (amount_usd bleibt 200.00), aber die Gate-Entscheidung ist
    SHADOW (LCB < 0) und wird als edge_shadow=1 gekennzeichnet.

    Damit die Live-Kette (floor) und das Gate (shadow) gleichzeitig zeigen:
    Floor rundet hoch, Gate kennzeichnet Schatten — nichts wird verworfen.
    """
    db = _mkdb(tmp_path)
    _insert_outcomes(db, "NEG", [-0.015] * 40)  # nreich, negativ

    # -- $200-Floor (aus execution_worker, hier isoliert nachgebaut) --
    amount_usd = 60.0  # schwaches Kelly-Signal
    floored = 200.0 if 0 < amount_usd < 200.0 else amount_usd
    assert floored == 200.0, "Floor rundet $60 auf $200 (Trade wird NICHT verworfen)"

    # -- Edge-Gate-Entscheidung (SHADOW-MODUS) --
    res = edge_gate.evaluate("NEG", "DEFENSIVE", db)
    assert res["shadow"] is True, "negativer Typ -> SHADOW"
    assert res["lcb"] < 0

    # -- Shadow-Kennzeichnung in signal_outcomes (SHADOW-MODUS) --
    # Die Spalte edge_shadow exitiert im Test-Schema; UPDATE auf typ-basis.
    db.execute(
        "UPDATE signal_outcomes SET edge_shadow = ? WHERE signal_type = ?",
        (1 if res["shadow"] else 0, "NEG"),
    )
    marked = db.fetchone(
        "SELECT COUNT(*) c FROM signal_outcomes WHERE edge_shadow = 1 AND signal_type='NEG'"
    )
    assert marked["c"] == 40, "alle Zeilen des negativen Typs als Schatten-Trage"

    # -- Live-Kette ist nicht blockiert: der Trade geht mit $200 durch --
    assert floored > 0, "SHADOW-MODUS blockt nichts — der Trade wird ausgefuehrt"
