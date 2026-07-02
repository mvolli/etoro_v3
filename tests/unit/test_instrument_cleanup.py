#!/usr/bin/env python3
"""Unit tests — fix/instrument-db-cleanup.

Covers the pure classification logic (bot.core.instrument_cleanup):
corpse detection, delete/deactivate/review decisions, the VALT.L rescue
case (eToro confirms a 'delisted'-flagged row), conservative mode
without verification, and the tolerant audit-results loader.
"""
from __future__ import annotations

from bot.core.instrument_cleanup import (
    ACTION_DEACTIVATE,
    ACTION_DELETE,
    ACTION_KEEP,
    ACTION_REVIEW,
    PLACEHOLDER_ID_MAX,
    PLACEHOLDER_ID_MIN,
    build_plan,
    classify_instrument,
    is_corpse_candidate,
    is_placeholder_id,
    load_audit_corrections,
)


def row(iid, symbol="SYM", yf="SYM", status="ok", active=1, **kw):
    d = {"instrument_id": iid, "symbol": symbol, "yfinance_symbol": yf,
         "yahoo_status": status, "is_active": active}
    d.update(kw)
    return d


# ─── corpse candidates ────────────────────────────────────────────────────────

class TestCorpseCandidate:
    def test_healthy_row(self):
        cand, _ = is_corpse_candidate(row(1003, "META", "META", "ok"))
        assert not cand

    def test_delisted(self):
        cand, why = is_corpse_candidate(row(2000, status="delisted"))
        assert cand and "delisted" in why

    def test_delisted_case_insensitive(self):
        assert is_corpse_candidate(row(2000, status="DELISTED"))[0]

    def test_empty_yfinance_symbol(self):
        assert is_corpse_candidate(row(2000, yf=""))[0]
        assert is_corpse_candidate(row(2000, yf=None))[0]

    def test_placeholder_range(self):
        assert is_placeholder_id(PLACEHOLDER_ID_MIN)
        assert is_placeholder_id(PLACEHOLDER_ID_MAX)
        assert not is_placeholder_id(PLACEHOLDER_ID_MIN - 1)
        assert not is_placeholder_id(PLACEHOLDER_ID_MAX + 1)
        assert is_corpse_candidate(row(123456, status="ok"))[0]

    def test_missing_optional_columns_tolerated(self):
        # Live DB may lack yahoo_status/yfinance_symbol columns entirely
        cand, _ = is_corpse_candidate({"instrument_id": 1003, "symbol": "META"})
        assert cand  # no yfinance_symbol → candidate (verification decides)


# ─── classification with eToro verification ──────────────────────────────────

class TestClassifyVerified:
    ETORO = {1456: "VALT.L", 5000: "OTHERSYM"}

    def test_valt_l_rescue(self):
        # The VALT.L case: yahoo_status='delisted' was a data-quality
        # artefact — eToro confirms the ID↔symbol pair → KEEP.
        d = classify_instrument(
            row(1456, "VALT.L", "VPL", "delisted"), set(), self.ETORO
        )
        assert d.action == ACTION_KEEP
        assert "Datenqualität" in d.reason

    def test_confirmed_corpse_unreferenced_deleted(self):
        d = classify_instrument(row(2000, "DEAD", "", "delisted"), set(), self.ETORO)
        assert d.action == ACTION_DELETE

    def test_confirmed_corpse_referenced_deactivated(self):
        d = classify_instrument(row(2000, "DEAD", "", "delisted"), {2000}, self.ETORO)
        assert d.action == ACTION_DEACTIVATE
        assert "referenziert" in d.reason

    def test_symbol_mismatch_goes_to_review(self):
        d = classify_instrument(row(5000, "LOCALSYM", "", "delisted"), set(), self.ETORO)
        assert d.action == ACTION_REVIEW
        assert d.etoro_symbol == "OTHERSYM"

    def test_symbol_mismatch_referenced_deactivated(self):
        d = classify_instrument(row(5000, "LOCALSYM", "", "delisted"), {5000}, self.ETORO)
        assert d.action == ACTION_DEACTIVATE

    def test_crypto_suffix_counts_as_match(self):
        d = classify_instrument(
            row(9000, "BTC-USD", "", "delisted"), set(), {9000: "BTC"}
        )
        assert d.action == ACTION_KEEP

    def test_placeholder_unknown_to_etoro_deleted(self):
        d = classify_instrument(row(123456, "GHOST", "GHOST", "ok"), set(), self.ETORO)
        assert d.action == ACTION_DELETE

    def test_healthy_row_kept_without_lookup(self):
        d = classify_instrument(row(1003, "META", "META", "ok"), set(), self.ETORO)
        assert d.action == ACTION_KEEP


# ─── conservative mode (no verification) ──────────────────────────────────────

class TestClassifyConservative:
    def test_never_deletes_without_verification(self):
        rows = [
            row(2000, "DEAD1", "", "delisted"),
            row(123456, "GHOST", "GHOST", "ok"),
            row(1003, "META", "META", "ok"),
        ]
        plan = build_plan(rows, set(), etoro_symbols=None)
        assert len(plan.delete) == 0
        assert len(plan.deactivate) == 2
        assert len(plan.keep) == 1

    def test_reason_mentions_missing_verification(self):
        d = classify_instrument(row(2000, "DEAD", "", "delisted"), set(), None)
        assert d.action == ACTION_DEACTIVATE
        assert "verify-etoro" in d.reason


# ─── plan aggregation ─────────────────────────────────────────────────────────

class TestPlan:
    def test_totals(self):
        etoro = {2: "B"}
        rows = [
            row(1, "A", "A", "ok"),           # keep (healthy)
            row(2, "B", "", "delisted"),      # keep (rescued)
            row(3, "C", "", "delisted"),      # delete (unknown, unref)
            row(4, "D", "", "delisted"),      # deactivate (unknown, ref)
        ]
        plan = build_plan(rows, {4}, etoro)
        assert plan.total == 4
        assert [d.instrument_id for d in plan.delete] == [3]
        assert [d.instrument_id for d in plan.deactivate] == [4]
        assert len(plan.keep) == 2


# ─── audit-results loader ─────────────────────────────────────────────────────

class TestAuditLoader:
    def test_wrapper_format(self):
        raw = {"corrections": {"1456": {"yfinance_symbol": "VALT.L", "yahoo_status": "ok"}}}
        out = load_audit_corrections(raw)
        assert out == {1456: {"yfinance_symbol": "VALT.L", "yahoo_status": "ok"}}

    def test_flat_format(self):
        out = load_audit_corrections({"7": {"name": "Foo Inc"}})
        assert out == {7: {"name": "Foo Inc"}}

    def test_list_format(self):
        out = load_audit_corrections([
            {"instrument_id": 9, "symbol": "NEW", "irrelevant": True},
            {"no_id": 1},
        ])
        assert out == {9: {"symbol": "NEW"}}

    def test_unknown_fields_filtered(self):
        out = load_audit_corrections({"5": {"symbol": "X", "hack": "DROP TABLE"}})
        assert out == {5: {"symbol": "X"}}

    def test_empty_and_garbage(self):
        assert load_audit_corrections(None) == {}
        assert load_audit_corrections("nonsense") == {}
        assert load_audit_corrections({"notanid": {"symbol": "X"}}) == {}
        assert load_audit_corrections({"5": {"symbol": ""}}) == {}
