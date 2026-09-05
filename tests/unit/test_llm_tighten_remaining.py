#!/usr/bin/env python3
"""fix/llm-tighten-remaining (2026-09-05).

Der LLM-Execution-Pfad hat die 50-%-Untergrenze NICHT geprueft und die
Restmenge NICHT fortgeschrieben — anders als `sell_exits.py`, das beides
tut. `position_state.remaining_frac` blieb fuer LLM-getriebene Teilverkaeufe
dauerhaft auf 1.0, also konnte der Schutz nie greifen und dieselbe
TIGHTEN-Empfehlung wurde Zyklus fuer Zyklus als echte Order ausgefuehrt.

Gemessen auf trading.db (2026-09-05):
  - 610 der 910 Teilschliessungen aus `llm_tighten` (sell_exit: 184)
  - CAR.AX (trade 682): 52 Rungs, kumuliert $1.374 "geschlossen" auf einer
    $360-Position = 3,81x — mehrfach zum IDENTISCHEN Preis
  - 47 von 153 Trades mit Teilschliessungen haben mehr als 4 Rungs
  - system_log: "LLM TIGHTEN 25% ausgefuehrt" — es waren echte Orders

Jede redundante Tranche zahlt Spread.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import bot.core.llm_execution as LE
import bot.core.trailing_stop as TS


# ── Doubles ───────────────────────────────────────────────────────────────────

class _Client:
    """Merkt sich jeden close_position-Aufruf."""

    def __init__(self, units=100.0):
        self.units = units
        self.calls = []

    def get_position_units(self, position_id):
        return self.units

    def close_position(self, position_id, instrument_id, units_to_deduct=None):
        self.calls.append({"position_id": position_id,
                           "units_to_deduct": units_to_deduct,
                           "full": units_to_deduct is None})
        return {"ok": True}


class _DB:
    """Haelt remaining_frac im Speicher — wie position_state es taete."""

    def __init__(self, remaining=1.0):
        self.remaining = remaining
        self.writes = []

    def fetchone(self, sql, params=()):
        return None

    def fetchall(self, sql, params=()):
        return []

    def execute(self, sql, params=()):
        self.writes.append((sql, params))
        return None


class _Log:
    def write(self, *a, **k):
        return None


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Empfehlungsdatei ins tmp_path, Netzwerk/Embeds stumm, Floor auf 50 %."""
    monkeypatch.setattr(LE, "RECS_PATH", tmp_path / "recs.json")
    monkeypatch.setattr(TS, "MIN_REMAINING_PCT", 50.0)
    for name in ("_post_closed_embed", "_append_outcome_entry"):
        if hasattr(LE, name):
            monkeypatch.setattr(LE, name, lambda *a, **k: None)
    return tmp_path


def _write_rec(tmp_path, close_pct=25.0, recommendation="TIGHTEN"):
    from datetime import datetime, timezone
    rec = [{
        "symbol": "CAR.AX",
        "recommendation": recommendation,
        "close_pct": close_pct,
        "reason": "KI TIGHTEN: FADED",
        "ts": datetime.now(timezone.utc).isoformat()[:19],
        "instrument_id": 4711,
        "position_id": "P1",
        "executed": False,
    }]
    (tmp_path / "recs.json").write_text(json.dumps(rec), encoding="utf-8")


def _run(tmp_path, remaining, monkeypatch, close_pct=25.0):
    _write_rec(tmp_path, close_pct)
    client, db, log = _Client(), _DB(), _Log()
    monkeypatch.setattr(TS, "load_position_dynamic",
                        lambda _db, pids: {"P1": {"remaining": remaining}})
    applied = []
    monkeypatch.setattr(TS, "apply_partial_to_remaining",
                        lambda _db, pid, sym, pct: applied.append((pid, pct)))
    stats = LE.execute_llm_recommendations(client, db, {"P1"}, log)
    return client, stats, applied


# ── Der Guard ────────────────────────────────────────────────────────────────

def test_erster_teilverkauf_laeuft_normal(tmp_path, monkeypatch):
    """Volle Position, 25 % zu — die Leiter darf arbeiten."""
    client, stats, applied = _run(tmp_path, 1.0, monkeypatch)
    assert len(client.calls) == 1
    assert client.calls[0]["full"] is False, "kein Vollverkauf bei 100 % Rest"
    assert applied == [("P1", 25.0)], "Restmenge muss fortgeschrieben werden"


def test_unterschreitung_wird_zum_vollverkauf(tmp_path, monkeypatch):
    """Rest 56 % minus 25 % = 42 % < 50 % -> Vollverkauf statt Zerfaserung."""
    client, stats, applied = _run(tmp_path, 0.5625, monkeypatch)
    assert len(client.calls) == 1
    assert client.calls[0]["full"] is True
    assert client.calls[0]["units_to_deduct"] is None
    assert applied == [], "bei Vollverkauf wird keine Restmenge fortgeschrieben"


def test_rest_am_floor_schliesst_ganz(tmp_path, monkeypatch):
    client, _, _ = _run(tmp_path, 0.50, monkeypatch)
    assert client.calls[0]["full"] is True


# ── Die eigentliche Ursache: Fortschreibung ──────────────────────────────────

def test_ohne_fortschreibung_gaebe_es_die_52_rungs(tmp_path, monkeypatch):
    """Regressionsbeleg: der Guard wirkt NUR mit fortgeschriebener Restmenge.

    Bleibt remaining_frac bei 1.0 (der Zustand vor dem Fix), waere jeder
    Zyklus erneut ein regulaerer 25-%-Teilverkauf — beliebig oft.
    """
    for _ in range(6):
        client, _, _ = _run(tmp_path, 1.0, monkeypatch)   # stale 1.0
        assert client.calls[0]["full"] is False
    # Mit korrekt fortgeschriebener Kette bricht es nach zwei Rungs ab:
    kette = [1.0, 0.75, 0.5625]
    voll = [_run(tmp_path, r, monkeypatch)[0].calls[0]["full"] for r in kette]
    assert voll == [False, False, True], (
        "100->75->56 darf teilverkaufen, der dritte Schritt muss ganz schliessen"
    )


def test_exit_100_prozent_unberuehrt(tmp_path, monkeypatch):
    """EXIT mit close_pct=100 ist ohnehin Vollverkauf — kein Guard noetig."""
    client, _, applied = _run(tmp_path, 1.0, monkeypatch,
                              close_pct=100.0)
    assert client.calls[0]["full"] is True
    assert applied == []


# ── Fail-open ────────────────────────────────────────────────────────────────

def test_defekter_lookup_blockiert_nicht(tmp_path, monkeypatch):
    """Kein DB-Stand -> weiter wie bisher, keine Ausnahme."""
    _write_rec(tmp_path)
    def boom(*a, **k):
        raise RuntimeError("db weg")
    monkeypatch.setattr(TS, "load_position_dynamic", boom)
    monkeypatch.setattr(TS, "apply_partial_to_remaining", lambda *a, **k: None)
    client, db, log = _Client(), _DB(), _Log()
    stats = LE.execute_llm_recommendations(client, db, {"P1"}, log)
    assert len(client.calls) == 1
    assert client.calls[0]["full"] is False


def test_fehlgeschlagene_fortschreibung_bricht_nicht_ab(tmp_path, monkeypatch):
    _write_rec(tmp_path)
    monkeypatch.setattr(TS, "load_position_dynamic",
                        lambda _db, pids: {"P1": {"remaining": 1.0}})
    def boom(*a, **k):
        raise RuntimeError("write weg")
    monkeypatch.setattr(TS, "apply_partial_to_remaining", boom)
    client, db, log = _Client(), _DB(), _Log()
    stats = LE.execute_llm_recommendations(client, db, {"P1"}, log)
    assert stats["tighten_count"] == 1, "Order gilt trotzdem als ausgefuehrt"
