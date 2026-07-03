#!/usr/bin/env python3
"""Unit tests — fix/fragment-limit.

Trading Bible: max_fragments_per_instrument (default 3) blocks additional
buys regardless of regime; the regime-based pyramiding rules stay intact.
"""
from __future__ import annotations

from bot.core.risk import check_buy_gate, check_pyramiding_gate


def test_at_limit_blocks_even_in_normal():
    result = check_pyramiding_gate("NVDA", "NORMAL", existing_fragments=3)
    assert not result.allowed
    assert "Fragment-Limit" in result.summary()


def test_below_limit_allowed_in_normal():
    result = check_pyramiding_gate("NVDA", "NORMAL", existing_fragments=2)
    assert result.allowed


def test_regime_pyramiding_still_blocks_in_defensive():
    # 1 Fragment < Limit, aber DEFENSIVE verbietet Pyramiding grundsätzlich
    result = check_pyramiding_gate("NVDA", "DEFENSIVE", existing_fragments=1)
    assert not result.allowed
    assert "Pyramiding-Gate" in result.summary()


def test_fresh_position_allowed_in_defensive():
    result = check_pyramiding_gate("NVDA", "DEFENSIVE", existing_fragments=0)
    assert result.allowed


def test_custom_limit_from_config():
    assert check_pyramiding_gate("NVDA", "NORMAL", 4, max_fragments=5).allowed
    assert not check_pyramiding_gate("NVDA", "NORMAL", 5, max_fragments=5).allowed


def test_zero_disables_fragment_limit():
    result = check_pyramiding_gate("NVDA", "NORMAL", 99, max_fragments=0)
    assert result.allowed


def test_master_gate_blocks_at_fragment_limit():
    result = check_buy_gate(
        symbol="AAPL",
        buy_amount=100.0,
        equity=10_000.0,
        cash=3_000.0,
        regime="NORMAL",
        open_count=5,
        current_symbol_amount=500.0,
        total_exposed=5_000.0,
        conviction="HIGH",
        existing_fragments=3,
    )
    assert not result.allowed
    assert "Fragment-Limit" in result.summary()
