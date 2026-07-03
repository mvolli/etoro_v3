#!/usr/bin/env python3
"""Unit tests — fix/ghost-order-id-diff.

The fill verification in execution_worker must only accept a position whose
positionID did NOT exist before the order was submitted. An existing
pyramiding fragment of the same instrument must never confirm a ghost order.
"""
from __future__ import annotations

from bot.workers.execution_worker import (
    _find_new_position,
    _position_ids_for_instrument,
)


def _pos(pid, iid=42, amount=500.0):
    return {"positionID": pid, "instrumentID": iid, "amount": amount}


def test_snapshot_collects_only_target_instrument():
    positions = [_pos("a", iid=42), _pos("b", iid=42), _pos("c", iid=99)]
    assert _position_ids_for_instrument(positions, 42) == {"a", "b"}


def test_existing_fragment_does_not_confirm_ghost():
    # Vor dem Submit existierte Fragment "old"; danach ist NUR "old" da
    # (Ghost-Order) → darf NICHT als Fill zählen.
    positions = [_pos("old")]
    assert _find_new_position(positions, 42, {"old"}) is None


def test_new_position_is_found_next_to_old_fragment():
    positions = [_pos("old"), _pos("new")]
    found = _find_new_position(positions, 42, {"old"})
    assert found is not None
    assert found["positionID"] == "new"


def test_first_position_without_preexisting():
    positions = [_pos("only")]
    found = _find_new_position(positions, 42, set())
    assert found["positionID"] == "only"


def test_other_instrument_never_matches():
    positions = [_pos("x", iid=99)]
    assert _find_new_position(positions, 42, set()) is None


def test_position_without_id_only_accepted_when_none_preexisted():
    no_id = {"instrumentID": 42, "amount": 500.0}
    # Kein Alt-Fragment → konservativer Fallback akzeptiert
    assert _find_new_position([no_id], 42, set()) is no_id
    # Alt-Fragment existiert → ID-lose Position ist nicht unterscheidbar → ablehnen
    assert _find_new_position([no_id], 42, {"old"}) is None


def test_lowercase_id_field_variants():
    positions = [{"instrumentId": 42, "positionId": "new"}]
    found = _find_new_position(positions, 42, {"old"})
    assert found is not None
