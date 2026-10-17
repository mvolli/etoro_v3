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
    # fix/tests-schreiben-in-produktion (2026-09-10): hier stand
    #     for name in ("_post_closed_embed", "_append_outcome_entry"):
    #         if hasattr(LE, name):
    # `llm_execution` hat keine Funktion `_post_closed_embed` — sie heisst
    # `_discord`. hasattr war False, der Patch griff nie, und das `if`
    # verschluckte den Fehler: die Fixture SAH aus wie eine Absicherung,
    # waehrend jeder Lauf 15 echte Embeds nach #etoro-trades schickte und
    # 15 Zeilen in die Produktions-system_log schrieb. Zwischen dem
    # 2026-09-05 und dem 2026-09-10 kamen so 526 zusammen.
    # Kein hasattr mehr: raising=True soll knallen, wenn sich ein Name
    # aendert, statt still nichts zu tun.
    monkeypatch.setattr(LE, "_discord", lambda *a, **k: None)
    monkeypatch.setattr(LE, "_append_outcome_entry", lambda *a, **k: None)
    # fix/llm-tighten-market-hours (2026-10-17): TIGHTEN mit close_pct braucht
    # jetzt offenen Markt. Ohne Patch hingen diese Tests an der echten Uhrzeit.
    import bot.core.market_hours as _mh
    monkeypatch.setattr(_mh, "is_market_open", lambda *a, **k: True)
    # feat/let-run-exits: LIVE-Config hat tighten_orders false; diese Tests pruefen
    # den Order-Pfad selbst.
    monkeypatch.setattr(LE, "_TIGHTEN_ORDERS_CACHE", True)
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


# ── fix/llm-tighten-market-hours (2026-10-17) ────────────────────────────────

def test_tighten_mit_order_wartet_bei_geschlossenem_markt(tmp_path, monkeypatch):
    """BOQ.ASX/ORG.ASX 16.10.: TIGHTEN 100 % bei geschlossener ASX ging als
    Order raus und wurde sofort als CLOSED gebucht. Jetzt: keine Order, retry."""
    import bot.core.market_hours as _mh
    monkeypatch.setattr(_mh, "is_market_open", lambda *a, **k: False)
    client, stats, _ = _run(tmp_path, 1.0, monkeypatch, close_pct=25.0)
    assert client.calls == []
    assert stats["skip_count"] == 1
    recs = json.loads((tmp_path / "recs.json").read_text())
    assert not any(r.get("executed") for r in recs)


# ── feat/tighten-min-gain (VoLLi 2026-10-17) ─────────────────────────────────

class _PnlDB(_DB):
    def __init__(self, pnl):
        super().__init__()
        self.pnl = pnl

    def fetchone(self, sql, params=()):
        if "unrealized_pnl_pct FROM portfolio_snapshot" in sql and "amount_usd" not in sql:
            return {"unrealized_pnl_pct": self.pnl}
        return None


def _run_pnl(tmp_path, monkeypatch, pnl, recommendation="TIGHTEN", close_pct=25.0):
    monkeypatch.setattr(LE, "_TIGHTEN_MIN_CACHE", 3.0)
    _write_rec(tmp_path, close_pct, recommendation)
    monkeypatch.setattr(TS, "load_position_dynamic", lambda _db, pids: {"P1": {"remaining": 1.0}})
    monkeypatch.setattr(TS, "apply_partial_to_remaining", lambda *a, **k: None)
    client = _Client()
    stats = LE.execute_llm_recommendations(client, _PnlDB(pnl), {"P1"}, _Log())
    return client, stats


def test_tighten_unter_mindestgewinn_wird_gehalten(tmp_path, monkeypatch):
    client, stats = _run_pnl(tmp_path, monkeypatch, pnl=1.2)
    assert client.calls == [] and stats["skip_count"] == 1
    recs = json.loads((tmp_path / "recs.json").read_text())
    assert not recs[0].get("executed")             # bleibt offen fuer den Retry


def test_tighten_ab_mindestgewinn_laeuft(tmp_path, monkeypatch):
    client, _ = _run_pnl(tmp_path, monkeypatch, pnl=4.0)
    assert len(client.calls) == 1


def test_exit_ignoriert_mindestgewinn(tmp_path, monkeypatch):
    client, _ = _run_pnl(tmp_path, monkeypatch, pnl=-2.0, recommendation="EXIT", close_pct=100.0)
    assert len(client.calls) == 1 and client.calls[0]["full"]


def test_tighten_orders_aus_verbraucht_empfehlung(tmp_path, monkeypatch):
    """feat/let-run-exits: llm_execution.tighten_orders false -> keine Order."""
    monkeypatch.setattr(LE, "_TIGHTEN_ORDERS_CACHE", False)
    client, stats, _ = _run(tmp_path, 1.0, monkeypatch, close_pct=25.0)
    assert client.calls == []
    recs = json.loads((tmp_path / "recs.json").read_text())
    assert recs[0]["executed"] and recs[0]["executed_reason"] == "tighten_orders_disabled"
