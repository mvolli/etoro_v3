#!/usr/bin/env python3
"""Unit tests — feat/pnl-nachreport: TradeEventRepo (trade_events ledger).

Ein Datensatz pro Open/Partial/Full-Close inkl. Discord-Message-Koordinaten.
Kernsemantik: pnl_usd/pnl_pct NULL = unbekannt (nie 0.0 als Platzhalter),
reported_final=0 markiert Events für den Retro-Fill (Reconciler Step 9e).
Alle Methoden fail-open — dürfen nie einen Live-Close-Pfad brechen.
"""
from __future__ import annotations

import pytest

from bot.db.connection import DB
from bot.db.repo import TradeEventRepo


@pytest.fixture
def repo(tmp_path):
    db = DB(db_path=tmp_path / "t.db")
    return TradeEventRepo(db)


def test_table_self_creates(repo):
    row = repo.db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_events'"
    )
    assert row is not None


def test_record_and_read_back(repo):
    eid = repo.record(
        symbol="AAPL",
        event_type="OPEN",
        source="execution_worker",
        trade_id=7,
        position_id="123456",
        instrument_id=1001,
        price=324.28,
        amount_usd=137.58,
    )
    assert isinstance(eid, int)
    events = repo.get_by_position("123456")
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "OPEN"
    assert ev["pnl_usd"] is None          # unbekannt bleibt NULL, nie 0.0
    assert ev["reported_final"] == 0
    assert ev["event_at"]                  # auto-gestempelt


def test_record_rejects_unknown_event_type(repo):
    assert repo.record(symbol="X", event_type="BOGUS", source="s") is None


def test_set_discord_message(repo):
    eid = repo.record(symbol="AAPL", event_type="CLOSE", source="risk_sl",
                      position_id="99")
    repo.set_discord_message(eid, "1514786489110630600", "1234567890")
    ev = repo.get_by_position("99")[0]
    assert ev["discord_channel_id"] == "1514786489110630600"
    assert ev["discord_message_id"] == "1234567890"


def test_fill_pnl_stamps_and_finalizes(repo):
    eid = repo.record(symbol="COA.L", event_type="PARTIAL_CLOSE",
                      source="trailing_partial", position_id="55",
                      close_pct=25.0, pnl_pct=5.1, pnl_source="derived")
    unresolved = repo.get_unresolved_closes()
    assert [e["id"] for e in unresolved] == [eid]

    repo.fill_pnl(eid, pnl_usd=4.20, pnl_pct=5.3, price=13.10)
    ev = repo.get_by_position("55")[0]
    assert ev["pnl_usd"] == pytest.approx(4.20)
    assert ev["pnl_pct"] == pytest.approx(5.3)
    assert ev["price"] == pytest.approx(13.10)
    assert ev["pnl_source"] == "api_history"
    assert ev["reported_final"] == 1
    assert ev["pnl_filled_at"] is not None
    assert repo.get_unresolved_closes() == []


def test_fill_pnl_keeps_existing_price_when_none(repo):
    eid = repo.record(symbol="X", event_type="CLOSE", source="reconciler_9d",
                      position_id="77", price=10.0)
    repo.fill_pnl(eid, pnl_usd=1.0, pnl_pct=2.0, price=None)
    assert repo.get_by_position("77")[0]["price"] == pytest.approx(10.0)


def test_unresolved_ignores_opens_and_old_events(repo):
    repo.record(symbol="A", event_type="OPEN", source="execution_worker",
                position_id="1")
    repo.record(symbol="B", event_type="CLOSE", source="risk_sl",
                position_id="2", event_at="2020-01-01 00:00:00")  # zu alt
    assert repo.get_unresolved_closes(max_age_days=14) == []


def test_get_events_between(repo):
    repo.record(symbol="A", event_type="OPEN", source="s",
                event_at="2026-07-28 09:00:00")
    repo.record(symbol="B", event_type="CLOSE", source="s",
                event_at="2026-07-28 15:00:00", pnl_usd=3.0)
    repo.record(symbol="C", event_type="CLOSE", source="s",
                event_at="2026-07-29 01:00:00")
    window = repo.get_events_between("2026-07-28 00:00:00", "2026-07-29 00:00:00")
    assert [e["symbol"] for e in window] == ["A", "B"]


def test_has_event_gate(repo):
    repo.record(symbol="A", event_type="CLOSE", source="reconciler_9d",
                position_id="42")
    assert repo.has_event("42", "CLOSE") is True
    assert repo.has_event("42", "CLOSE", source="reconciler_9d") is True
    assert repo.has_event("42", "CLOSE", source="risk_sl") is False
    assert repo.has_event("42", "PARTIAL_CLOSE") is False
    assert repo.has_event("777", "CLOSE") is False


def test_fail_open_on_broken_db(tmp_path):
    # Repo gegen eine DB ohne Schreibrechte-Simulation: kaputtes Objekt
    class BrokenDB:
        def execute(self, *a, **k):
            raise RuntimeError("boom")

        def fetchone(self, *a, **k):
            raise RuntimeError("boom")

        def fetchall(self, *a, **k):
            raise RuntimeError("boom")

    r = TradeEventRepo.__new__(TradeEventRepo)
    r.db = BrokenDB()
    assert r.record(symbol="X", event_type="CLOSE", source="s") is None
    r.set_discord_message(1, "c", "m")          # darf nicht raisen
    r.fill_pnl(1, 1.0, 2.0)                     # darf nicht raisen
    assert r.get_by_position("1") == []
    assert r.get_unresolved_closes() == []
    assert r.get_events_between("a", "b") == []
    assert r.has_event("1", "CLOSE") is False
