#!/usr/bin/env python3
"""Unit tests — fix/eu-watchlist-expansion + fix/watchlist-promotion-all-regions.

Covers the rotating EU discovery chunk partitioning (added after ~2900 EU
instruments were reactivated by scripts/fix_eu_yfinance_symbols.py but
remained invisible to discovery_worker's hardcoded FULL_UNIVERSE) and the
region-generic watchlist promotion/cap/eviction logic (a discovery signal
is otherwise a one-shot 6h-TTL entry, for any region, not just EU).
"""
from __future__ import annotations

import pytest

from bot.db.connection import DB
from bot.workers.discovery_worker import (
    EU_DISCOVERY_CHUNK_COUNT,
    _get_eu_discovery_chunk,
    _promote_to_watchlist,
)


@pytest.fixture
def db(tmp_path):
    d = DB(db_path=tmp_path / "trading.db")
    d.execute("""
        CREATE TABLE instruments (
            instrument_id INTEGER PRIMARY KEY,
            symbol TEXT,
            yfinance_symbol TEXT,
            market_region TEXT,
            asset_class TEXT,
            is_active INTEGER DEFAULT 1,
            is_tradable INTEGER DEFAULT 1
        )
    """)
    d.execute("""
        CREATE TABLE watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            instrument_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, category)
        )
    """)
    return d


def _seed_instrument(db, iid, symbol, yf_symbol=None, active=1, asset_class="stock", region="EU"):
    db.execute(
        "INSERT INTO instruments (instrument_id, symbol, yfinance_symbol, market_region, asset_class, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (iid, symbol, yf_symbol or symbol, region, asset_class, active),
    )


# ── _get_eu_discovery_chunk ───────────────────────────────────────────────────

def test_chunk_partitions_are_disjoint_and_complete(db):
    for i in range(1, 51):
        _seed_instrument(db, i, f"SYM{i}.DE")

    seen: set[int] = set()
    for chunk_idx in range(EU_DISCOVERY_CHUNK_COUNT):
        rows = _get_eu_discovery_chunk(db, chunk_idx)
        ids = {iid for _, iid, _ in rows}
        assert not (ids & seen), "chunks must not overlap"
        seen |= ids
    assert seen == set(range(1, 51))


def test_chunk_assignment_is_stable_across_calls(db):
    _seed_instrument(db, 42, "FOO.DE")
    first = _get_eu_discovery_chunk(db, 42 % EU_DISCOVERY_CHUNK_COUNT)
    second = _get_eu_discovery_chunk(db, 42 % EU_DISCOVERY_CHUNK_COUNT)
    assert first == second
    assert any(iid == 42 for _, iid, _ in first)


def test_chunk_excludes_inactive_and_non_eu(db):
    _seed_instrument(db, 1, "ACTIVE.DE", active=1)
    _seed_instrument(db, 2, "INACTIVE.DE", active=0)
    _seed_instrument(db, 3, "US_STOCK", region="US")
    _seed_instrument(db, 4, "AN_ETF.PA", asset_class="etf")
    _seed_instrument(db, 5, "A_BOND.PA", asset_class="bond")

    all_ids: set[int] = set()
    for chunk_idx in range(EU_DISCOVERY_CHUNK_COUNT):
        all_ids |= {iid for _, iid, _ in _get_eu_discovery_chunk(db, chunk_idx)}

    assert 1 in all_ids
    assert 4 in all_ids       # etf included
    assert 2 not in all_ids   # inactive excluded
    assert 3 not in all_ids   # non-EU excluded
    assert 5 not in all_ids   # bond excluded (only stock/etf)


# ── _promote_to_watchlist ─────────────────────────────────────────────────────

def test_unknown_instrument_is_not_promoted(db):
    _promote_to_watchlist(db, 999, "GHOST", score=99.0)
    assert db.fetchone("SELECT count(*) AS n FROM watchlist")["n"] == 0


def test_eu_candidate_added_under_cap(db):
    _seed_instrument(db, 1, "FOO.DE", region="EU")
    _promote_to_watchlist(db, 1, "FOO.DE", score=50.0, cap=5)
    row = db.fetchone("SELECT * FROM watchlist WHERE instrument_id = 1")
    assert row is not None
    assert row["category"] == "eu.discovered"
    assert row["last_score"] == 50.0


@pytest.mark.parametrize("region,expected_category", [
    ("US", "us.discovered"),
    ("EU", "eu.discovered"),
    ("ASIA_JP", "asia_jp.discovered"),
    ("GLOBAL", "global.discovered"),
    (None, "global.discovered"),   # unknown/blank region falls back to a global bucket
])
def test_promotion_uses_region_specific_category(db, region, expected_category):
    _seed_instrument(db, 1, "SYM", region=region)
    _promote_to_watchlist(db, 1, "SYM", score=50.0, cap=5)
    row = db.fetchone("SELECT category FROM watchlist WHERE instrument_id = 1")
    assert row["category"] == expected_category


def test_regions_have_independent_caps(db):
    # An EU candidate at its region's cap must not block a US candidate —
    # each region's pool is capped independently.
    _seed_instrument(db, 1, "EU1.DE", region="EU")
    _promote_to_watchlist(db, 1, "EU1.DE", score=50.0, cap=1)
    _seed_instrument(db, 2, "US1", region="US")
    _promote_to_watchlist(db, 2, "US1", score=1.0, cap=1)

    categories = {r["category"] for r in db.fetchall("SELECT category FROM watchlist")}
    assert categories == {"eu.discovered", "us.discovered"}


def test_existing_entry_is_refreshed_not_duplicated(db):
    _seed_instrument(db, 1, "FOO.DE")
    _promote_to_watchlist(db, 1, "FOO.DE", score=50.0, cap=5)
    _promote_to_watchlist(db, 1, "FOO.DE", score=75.0, cap=5)
    rows = db.fetchall("SELECT * FROM watchlist WHERE instrument_id = 1")
    assert len(rows) == 1
    assert rows[0]["last_score"] == 75.0


def test_at_cap_higher_score_displaces_weakest(db):
    for i in range(1, 4):
        _seed_instrument(db, i, f"SYM{i}.DE")
        _promote_to_watchlist(db, i, f"SYM{i}.DE", score=float(i * 10), cap=3)
    # cap=3 reached, weakest is SYM1.DE (score 10.0)
    _seed_instrument(db, 4, "SYM4.DE")
    _promote_to_watchlist(db, 4, "SYM4.DE", score=99.0, cap=3)

    symbols = {r["symbol"] for r in db.fetchall(
        "SELECT symbol FROM watchlist WHERE category = ?", ("eu.discovered",)
    )}
    assert symbols == {"SYM2.DE", "SYM3.DE", "SYM4.DE"}


def test_at_cap_lower_score_does_not_displace(db):
    for i in range(1, 4):
        _seed_instrument(db, i, f"SYM{i}.DE")
        _promote_to_watchlist(db, i, f"SYM{i}.DE", score=float(i * 10), cap=3)
    # weakest is SYM1.DE (score 10.0) — a weaker candidate must not displace it
    _seed_instrument(db, 5, "SYM5.DE")
    _promote_to_watchlist(db, 5, "SYM5.DE", score=5.0, cap=3)

    symbols = {r["symbol"] for r in db.fetchall(
        "SELECT symbol FROM watchlist WHERE category = ?", ("eu.discovered",)
    )}
    assert symbols == {"SYM1.DE", "SYM2.DE", "SYM3.DE"}
