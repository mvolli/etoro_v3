#!/usr/bin/env python3
"""Regression — fix/ghost-blacklist-decision-log (2026-07-28).

_update_ghost_blacklist las den Vergleichsstand NACH dem write_text()
erneut von der Platte → verglich den frischen Stand mit sich selbst →
kein Exchange war je "neu" → das Decision-Log bekam NIE
ghost_blacklist-Eintraege. Jetzt zaehlt der Vor-Write-Stand.
"""
from __future__ import annotations

import json

import pytest

import bot.workers.llm_review_worker as lrw


@pytest.fixture
def paths(tmp_path, monkeypatch):
    bl = tmp_path / "ghost_blacklist.json"
    dl = tmp_path / "decision_log.json"
    monkeypatch.setattr(lrw, "GHOST_BLACKLIST_PATH", bl)
    monkeypatch.setattr(lrw, "DECISION_LOG_PATH", dl)
    return bl, dl


def _decisions(dl) -> list:
    return json.loads(dl.read_text()) if dl.exists() else []


def test_new_exchange_is_logged(paths):
    bl, dl = paths
    ghost_rates = {".L": {"total": 10, "ghost": 8, "rate": 0.8}}
    result = lrw._update_ghost_blacklist(ghost_rates, None)
    assert ".L" in result["exchanges"]
    logged = [(e["type"], e["key"]) for e in _decisions(dl)]
    assert ("ghost_blacklist", ".L") in logged   # vorher: nie geloggt


def test_existing_exchange_not_relogged(paths):
    bl, dl = paths
    bl.write_text(json.dumps({"exchanges": [".L"]}))
    ghost_rates = {".L": {"total": 10, "ghost": 8, "rate": 0.8}}
    lrw._update_ghost_blacklist(ghost_rates, None)
    assert _decisions(dl) == []                  # .L war schon geblockt


def test_only_delta_logged(paths):
    bl, dl = paths
    bl.write_text(json.dumps({"exchanges": [".L"]}))
    ghost_rates = {".L": {"total": 10, "ghost": 8, "rate": 0.8}}
    llm = {"ghost_exchanges": [".OL"], "ghost_symbols": [],
           "discord_summary": "Norwegen-Ghosts"}
    result = lrw._update_ghost_blacklist(ghost_rates, llm)
    assert set(result["exchanges"]) == {".L", ".OL"}
    logged = [e["key"] for e in _decisions(dl)]
    assert logged == [".OL"]
