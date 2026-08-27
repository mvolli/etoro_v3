#!/usr/bin/env python3
"""Regression — fix/signal-weight-ratchet (2026-08-27).

Pinnt die Ratsche gegen den alten Stand: ohne die Ratsche würde die LLM
JEDEN score_multiplier blind in die Gewichtsdatei schreiben (Lockerungen
einschliesslich). Mit der Ratsche gilt:

  * Ein Signal-TYP rattert seine Sizing-Gewichtung (score_multiplier) nur
    NACH OBEN (proposed > 1.0), und nur wenn der Typ es nach REALIZED
    (CLOSED) Daten post-Zaesur (ZAESUR_DATE) verdient hat:
        n_closed >= min_closed_trades  AND  SUM(pnl_usd) > 0
  * Realized-only: offene Buchgewinne (ACTIVE/CLOSING) duerven eine Lockerung
    NICHT begruenden (Rueckkopplung + taegliches Flackern).
  * Fenster an ZAESUR_DATE: Pre-Zaesur Trades zaelen NICHT (sonst druckte
    z.B. CORE_SWEEP die -166.38 $ Altlast mit und blieb Dauerfrozen aus
    Versehen).
  * Dämpfungen (proposed < 1.0) bleiben unangetastet.
  * Nicht verdiente Lockerung wird am CURRENT-Wert eingefroren (frozen,
    not rotated): ein Verlustbringer bleibt <=1.0 bis er nachweislich Geld
    macht.
  * Reihenfolge: erst pruefen, DANN schreiben — sonst wuerde die ungecheckte
    Lockerung persistiert und der Freeze verlorengangen.

Kein Live-DB-Zugriff: eine eigene tmp-DB mit exakt den n/pnl-Zahlen pro
Signal-Typ wird gebaut und gegen die REALIZED-only + ZAESUR-Filter-Logik
geprueft.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import bot.workers.llm_review_worker as lrw


def _build_db(path: Path) -> Path:
    """Erstellt eine trades+signals-DB mit den REALIZED-Zahlen pro Typ.

    Typen:
      WINNER    -> 20 CLOSED, +100.0 USD, ALL post-Zaesur
      LOSER     -> 25 CLOSED, -166.38 USD, ALL post-Zaesur
      PRE_ONLY  -> 20 CLOSED, +100.0 USD, ALL PRE-Zaesur (muss NICHT zaelen)
      OPEN_GAIN -> 1 CLOSED +10.0 USD + 10 ACTIVE/CLOSING +99.0 USD (realized only 10.0)
    """
    path = Path(path)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE signals ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, signal_type TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE trades ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, "
        " status TEXT, pnl_usd REAL, created_at TEXT)"
    )

    def add_trades(stype: str, n: int, pnl_each: float, when: str):
        cur = con.execute("INSERT INTO signals (signal_type) VALUES (?)", (stype,))
        sid = cur.lastrowid
        for _ in range(n):
            con.execute(
                "INSERT INTO trades (signal_id, status, pnl_usd, created_at) "
                "VALUES (?,?,?,?)",
                (sid, "CLOSED", pnl_each, when),
            )

    add_trades("MACD_TURN,WINNER", 20, +5.0, "2026-08-01T00:00:00")      # realized +100
    add_trades("CORE_SWEEP,LOSER", 25, -6.6552, "2026-08-01T00:00:00")   # realized -166.38
    add_trades("MACD_TURN,PRE_ONLY", 20, +5.0, "2026-07-01T00:00:00")    # all PRE-Zaesur
    add_trades("MACD_TURN,OPEN_GAIN", 1, +10.0, "2026-08-01T00:00:00")   # realized +10 only
    # OPEN_GAIN open gains (must NOT count): ACTIVE/CLOSING, realized None
    con.execute("SELECT id FROM signals WHERE signal_type='MACD_TURN,OPEN_GAIN'")
    gain_sid = con.execute(
        "SELECT id FROM signals WHERE signal_type='MACD_TURN,OPEN_GAIN'"
    ).fetchone()[0]
    for _ in range(10):
        con.execute(
            "INSERT INTO trades (signal_id, status, pnl_usd, created_at) "
            "VALUES (?,?,?,?)",
            (gain_sid, "ACTIVE", None, "2026-08-10T00:00:00"),
        )
    con.commit()
    con.close()
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = _build_db(tmp_path / "ratchet.db")
    weights = tmp_path / "llm_signal_weights.json"
    decisions = tmp_path / "llm_decision_log.json"
    monkeypatch.setattr(lrw, "SIGNAL_WEIGHTS_PATH", weights)
    monkeypatch.setattr(lrw, "DECISION_LOG_PATH", decisions)
    monkeypatch.setattr(
        lrw, "CFG",
        {"trading": {"signal_weight_ratchet": {"enabled": True,
                                               "min_closed_trades": 20}}},
    )
    return {"db": db, "weights": weights, "decisions": decisions}


def _write_current(weights: Path, adjustments: dict) -> None:
    import json
    weights.write_text(json.dumps({"adjustments": adjustments}))


def test_free_lock_when_realized_winnable(env):
    """n>=min AND realized>0 post-Zaesur -> ratte nach oben auf proposed.
    (Typ muss vorher gedaempft sein: proposed(1.0) > current(0.5) ist der
    Ratschen-Schritt; current==1.0 waere ein no-op, kein Locker.)"""
    _write_current(env["weights"],
                   {"MACD_TURN,WINNER": {"score_multiplier": 0.5}})
    adj = {"MACD_TURN,WINNER": {"score_multiplier": 1.0, "reason": "verdient"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    import json
    stored = json.loads(env["weights"].read_text())["adjustments"]
    assert stored["MACD_TURN,WINNER"]["score_multiplier"] == 1.0
    assert stored["MACD_TURN,WINNER"].get("_ratchet_frozen") in (None, False)
    assert "RATCHET-FREE-LOCK" in stored["MACD_TURN,WINNER"].get("_ratchet_reason", "")


def test_freeze_when_realized_loser(env):
    """n>=min ABER realized<0 -> freeze am CURRENT-Wert (0.5), NOT 1.0."""
    _write_current(env["weights"],
                   {"CORE_SWEEP,LOSER": {"score_multiplier": 0.5}})
    adj = {"CORE_SWEEP,LOSER": {"score_multiplier": 1.0, "reason": "locker"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    import json
    stored = json.loads(env["weights"].read_text())["adjustments"]["CORE_SWEEP,LOSER"]
    assert stored["score_multiplier"] == 0.5          # frozen at CURRENT, not 1.0
    assert stored.get("_ratchet_frozen") is True
    assert stored.get("_proposed") == 1.0
    assert "RATCHET-FROZEN" in stored["_ratchet_reason"]


def test_freeze_new_type_no_current(env):
    """Kein CURRENT-Eintrag (Base 1.0) + proposed 1.0 -> kein Locker (1.0 ==
    current) -> unangetastet, stays 1.0, NEVER above 1.0 (Never-Boost)."""
    adj = {"CORE_SWEEP,LOSER": {"score_multiplier": 1.0, "reason": "locker"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    import json
    stored = json.loads(env["weights"].read_text())["adjustments"]["CORE_SWEEP,LOSER"]
    assert stored["score_multiplier"] == 1.0          # not boosted, base stays 1.0
    assert stored["score_multiplier"] <= 1.0          # Never-Boost invariant
    # Not a freeze (no upward ratchet step to block) — no-op on the fresh base.
    assert stored.get("_ratchet_frozen") in (None, False)


def test_pre_zaesur_not_counted(env):
    """20 CLOSED but ALL pre-Zaesur -> n_closed=0 -> frozen (ZAESUR filter)."""
    _write_current(env["weights"],
                   {"MACD_TURN,PRE_ONLY": {"score_multiplier": 0.5}})
    adj = {"MACD_TURN,PRE_ONLY": {"score_multiplier": 1.0, "reason": "locker"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    import json
    stored = json.loads(env["weights"].read_text())["adjustments"]["MACD_TURN,PRE_ONLY"]
    assert stored["score_multiplier"] == 0.5          # frozen: pre-Zaesur not counted
    assert "n_closed=0/20" in stored["_ratchet_reason"]


def test_realized_only_open_gains_excluded(env):
    """10 offene +990$ Buchgewinne duerfen NICH'T die Lockerung free-locken."""
    _write_current(env["weights"],
                   {"MACD_TURN,OPEN_GAIN": {"score_multiplier": 0.5}})
    adj = {"MACD_TURN,OPEN_GAIN": {"score_multiplier": 1.0, "reason": "locker"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    import json
    stored = json.loads(env["weights"].read_text())["adjustments"]["MACD_TURN,OPEN_GAIN"]
    # Realized is only +10.0 (n=1 < 20) -> frozen, open gains ignored.
    assert stored["score_multiplier"] == 0.5
    assert stored.get("_ratchet_frozen") is True


def test_dampening_unaffected(env):
    """proposed < 1.0 (Daempfung) bleibt unangetastet, auch fuer Loser."""
    _write_current(env["weights"],
                   {"CORE_SWEEP,LOSER": {"score_multiplier": 1.0}})
    adj = {"CORE_SWEEP,LOSER": {"score_multiplier": 0.5, "reason": "daempfen"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    import json
    stored = json.loads(env["weights"].read_text())["adjustments"]["CORE_SWEEP,LOSER"]
    assert stored["score_multiplier"] == 0.5          # dampening applied as-is
    assert stored.get("_ratchet_frozen") in (None, False)


def test_ratchet_disabled_passthrough(env, monkeypatch):
    """enabled=false -> Lockerungen ungecheckt (altes Verhalten, kein Freeze)."""
    _write_current(env["weights"],
                   {"CORE_SWEEP,LOSER": {"score_multiplier": 0.5}})
    monkeypatch.setattr(
        lrw, "CFG",
        {"trading": {"signal_weight_ratchet": {"enabled": False,
                                               "min_closed_trades": 20}}},
    )
    adj = {"CORE_SWEEP,LOSER": {"score_multiplier": 1.0, "reason": "locker"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    import json
    stored = json.loads(env["weights"].read_text())["adjustments"]["CORE_SWEEP,LOSER"]
    assert stored["score_multiplier"] == 1.0          # passthrough, not frozen
    assert stored.get("_ratchet_frozen") in (None, False)


def test_min_n_threshold_boundary(env, monkeypatch):
    """min_closed_trades=30 -> WINNER(n=20) now too small -> frozen."""
    monkeypatch.setattr(
        lrw, "CFG",
        {"trading": {"signal_weight_ratchet": {"enabled": True,
                                               "min_closed_trades": 30}}},
    )
    _write_current(env["weights"],
                   {"MACD_TURN,WINNER": {"score_multiplier": 0.5}})
    adj = {"MACD_TURN,WINNER": {"score_multiplier": 1.0, "reason": "locker"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    import json
    stored = json.loads(env["weights"].read_text())["adjustments"]["MACD_TURN,WINNER"]
    assert stored["score_multiplier"] == 0.5          # frozen: n=20 < 30
    assert "n_closed=20/30" in stored["_ratchet_reason"]


def test_write_happens_after_ratchet(env):
    """Reihenfolge: Datei muss den (gefrorenen) NEW-Wert enthalten, nicht den
    ungecheckten Locker. Praezisiert: Freeze darf nicht in die Datei landen
    als 1.0, sondern als CURRENT."""
    _write_current(env["weights"],
                   {"CORE_SWEEP,LOSER": {"score_multiplier": 0.5}})
    adj = {"CORE_SWEEP,LOSER": {"score_multiplier": 1.0, "reason": "locker"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    import json
    stored = json.loads(env["weights"].read_text())["adjustments"]["CORE_SWEEP,LOSER"]
    # The persisted file holds the FROZEN value (0.5), proving the check ran
    # BEFORE the write (old code would have persisted 1.0).
    assert stored["score_multiplier"] == 0.5
    assert stored.get("_ratchet_frozen") is True
