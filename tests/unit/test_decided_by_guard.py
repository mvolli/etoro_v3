#!/usr/bin/env python3
"""Regression — fix/decided-by-guard (2026-09-12).

Schuetzt VoLLi-Entscheide in llm_signal_weights.json (Eintraege mit dem
`_decided_by`-Marker, z.B. die MEDIUM-Freigabe TREND_PULLBACK,GOLDEN_CROSS
vom 2026-09-11, Commit 65996de) davor, dass der automatische
Review-Pfad sie ueberschreibt.

Die Ratsche und der Merge-Schutz decken NUR den Codepfad — aber ein
LLM-Vorschlag mit komplettem `by_conviction`-Dict wuerde auch HIER das
VoLLi-Dict schluesselweise ersetzen (teilweise Teilmenge = LOW/VERY_HIGH
leise geloescht). Deshalb gilt: ein Vorschlag an einen `_decided_by`-
Eintrag wird verworfen, ESSE mit ausdruecklichem `_override_decided_by=True`
(VoLLi-Freigabe). Der Merge behaelt dann den CURRENT-Eintrag intakt.

Kein Live-DB-Zugriff: tmp-DB + tmp-Gewichtsdatei, wie test_signal_weight_ratchet.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import bot.workers.llm_review_worker as lrw


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "decided_by.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE signals ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, signal_type TEXT NOT NULL)")
    con.execute(
        "CREATE TABLE trades ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER, "
        " status TEXT, pnl_usd REAL, created_at TEXT)")
    con.commit(); con.close()
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
    weights.write_text(json.dumps({"adjustments": adjustments}))


MEDIUM_ENTRY = {
    "score_multiplier": 1.0,
    "skip": False,
    "reason": "MEDIUM freigegeben — Entscheid VoLLi 2026-09-11.",
    "_ratchet_frozen": True,
    "by_conviction": {"HIGH": 0.25, "LOW": 0.25, "VERY_HIGH": 0.25},
    "_decided_by": "VoLLi 2026-09-11",
}


def test_decided_by_proposal_discarded(env):
    """LLM-Vorschlag an einen _decided_by-Eintrag (OHNE Override) wird
    verworfen; der Merge behaelt den CURRENT-Eintrag intakt (by_conviction
    bleibt LOW/VERY_HIGH=0.25, mult 1.0)."""
    sig = "TREND_PULLBACK,GOLDEN_CROSS"
    _write_current(env["weights"], {sig: dict(MEDIUM_ENTRY)})
    # LLM will dampen the whole thing (completes the by_conviction dict with
    # a different value -> would clobber the VoLLi-Dict):
    adj = {sig: {"score_multiplier": 0.5,
                 "by_conviction": {"HIGH": 0.5, "MEDIUM": 0.5},
                 "reason": "autonom: MEDIUM zu groessig"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    stored = json.loads(env["weights"].read_text())["adjustments"][sig]
    # File still holds the VoLLi entry, NOT the LLM proposal:
    assert stored["score_multiplier"] == 1.0
    assert stored["by_conviction"] == {"HIGH": 0.25, "LOW": 0.25, "VERY_HIGH": 0.25}
    assert stored["_decided_by"] == "VoLLi 2026-09-11"


def test_decided_by_unmentioned_preserved(env):
    """LLM nannt den _decided_by-Eintrag NICHT: Merge-Keeper behaelt ihn
    (guard ist ein No-Op) — the Freigabe survives an unrelated Run."""
    sig = "TREND_PULLBACK,GOLDEN_CROSS"
    other = "CORE_SWEEP"
    _write_current(env["weights"], {sig: dict(MEDIUM_ENTRY),
                                    other: {"score_multiplier": 0.5}})
    adj = {other: {"score_multiplier": 0.3, "reason": "daempfen"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    stored = json.loads(env["weights"].read_text())["adjustments"]
    assert stored[sig]["_decided_by"] == "VoLLi 2026-09-11"
    assert stored[sig]["by_conviction"] == {"HIGH": 0.25, "LOW": 0.25, "VERY_HIGH": 0.25}
    assert stored[other]["score_multiplier"] == 0.3


def test_llm_kann_die_sperre_nicht_selbst_aufheben(env):
    """fix/override-nicht-llm-schreibbar (2026-09-12) — ersetzt den alten
    test_decided_by_override_passes.

    Der urspruengliche Test schrieb fest, dass ein Vorschlag MIT
    `_override_decided_by=True` durchlaeuft. Da `adjustments` woertlich aus
    der LLM-Antwort kommt, konnte das Sprachmodell dieses Feld selbst
    setzen — die Sperre war von der gesperrten Seite aus aufhebbar.
    Nachgestellt am 2026-09-12: der geschuetzte Eintrag ging von 1.0 auf
    0.1 und `_decided_by` verschwand dabei, der Schutz war danach
    dauerhaft weg.

    Schutz-Metadaten werden jetzt aus dem LLM-Kanal entfernt, bevor sie
    jemand liest. Eine menschliche Freigabe laeuft ueber die Datei selbst.
    """
    sig = "TREND_PULLBACK,GOLDEN_CROSS"
    _write_current(env["weights"], {sig: dict(MEDIUM_ENTRY)})
    adj = {sig: {"score_multiplier": 0.1, "_override_decided_by": True,
                 "_decided_by": "geklaut",
                 "reason": "als VoLLi-Freigabe ausgegeben"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    stored = json.loads(env["weights"].read_text())["adjustments"][sig]

    assert stored["score_multiplier"] == MEDIUM_ENTRY["score_multiplier"], \
        "LLM hat die Sperre mit selbstgesetztem _override_decided_by umgangen"
    assert stored["_decided_by"] == MEDIUM_ENTRY["_decided_by"], \
        "_decided_by wurde ueberschrieben — der Schutz waere danach weg"
    log = json.loads(env["decisions"].read_text())
    assert any("LOCKED_OUT_BY_GUARD" in str(e.get("new_value")) for e in log)


def test_override_aus_dem_code_behaelt_decided_by(env):
    """Ein Aufrufer, der `adjustments` selbst baut (nicht die LLM), darf
    aendern — `_decided_by` muss dabei erhalten bleiben, sonst waere der
    Schutz nach einer einzigen berechtigten Aenderung dauerhaft weg."""
    sig = "TREND_PULLBACK,GOLDEN_CROSS"
    _write_current(env["weights"], {sig: dict(MEDIUM_ENTRY)})
    adj = {sig: {"score_multiplier": 0.75, "reason": "Handentscheid"}}
    # So wie ein Codepfad es tun wuerde: NACH der LLM-Bereinigung gesetzt.
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    stored = json.loads(env["weights"].read_text())["adjustments"][sig]
    # Ohne Override greift der Guard -> unveraendert, _decided_by intakt.
    assert stored["score_multiplier"] == MEDIUM_ENTRY["score_multiplier"]
    assert stored["_decided_by"] == MEDIUM_ENTRY["_decided_by"]


def test_non_decided_by_unaffected(env):
    """Eintraege OHNE _decided_by are NOT locked — normal Ratchet/Merge
    still applies (guard must not over-lock)."""
    sig = "CORE_SWEEP,LOSER"
    _write_current(env["weights"], {sig: {"score_multiplier": 0.5}})
    adj = {sig: {"score_multiplier": 0.25, "reason": "daempfen"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    stored = json.loads(env["weights"].read_text())["adjustments"][sig]
    assert stored["score_multiplier"] == 0.25  # dampening applied as-is


def test_decided_by_lockout_logged(env):
    """Ein verworfener Vorschlag landet im Decision-Log (LOCKED_OUT_BY_GUARD),
    damit der Lockout sichtbar ist statt still."""
    sig = "TREND_PULLBACK,GOLDEN_CROSS"
    _write_current(env["weights"], {sig: dict(MEDIUM_ENTRY)})
    adj = {sig: {"score_multiplier": 0.4, "reason": "autonom"}}
    lrw._update_signal_weights({"signal_weight_adjustments": adj}, db_path=env["db"])
    log = json.loads(env["decisions"].read_text())
    assert any(e.get("new_value", {}).get("action") == "LOCKED_OUT_BY_GUARD"
               for e in log if e.get("key") == sig)
