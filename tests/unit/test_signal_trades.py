#!/usr/bin/env python3
"""feat/signal-trades (VoLLi 2026-10-17): feste Groesse, $1-Boersen, Gate-Ausnahme."""
from __future__ import annotations

import sqlite3

import pytest

from bot.core import edge_gate
from bot.core import signal_trades as st
from bot.db.connection import DB

CFG = {"trading": {"signal_trades": {
    "enabled": True,
    "families": ["MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING", "TREND_PULLBACK"],
    "max_open": 8, "cap_headroom": 0.98, "min_usd": 200.0, "reserve_pct": 18.0,
    "exclude_suffixes": [".ASX", ".HK", ".T"], "exclude_asset_classes": ["crypto"],
}}}


def test_familie_ist_komponenten_menge():
    c = st.load_config(CFG)
    assert st.is_family("BB_LOW_MACD_IMPROVING,MACD_TURN_BELOW_SMA20", c)   # Reihenfolge egal
    assert st.is_family("TREND_PULLBACK", c)
    assert not st.is_family("TREND_PULLBACK,GOLDEN_CROSS", c)              # exakt, keine Obermenge
    assert not st.is_family("RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20", c)


def test_aus_heisst_keine_familie():
    c = st.load_config({"trading": {"signal_trades": dict(CFG["trading"]["signal_trades"], enabled=False)}})
    assert not st.is_family("TREND_PULLBACK", c)


def test_feste_tranche():
    c = st.load_config(CFG)
    assert st.fixed_amount(10_000, 10.0, c) == 980.0
    assert st.fixed_amount(10_000, 5.0, c) == 490.0
    assert st.fixed_amount(1_000, 10.0, c) == 200.0          # nie unter $200
    assert st.fixed_amount(10_000, 100.0, c) == 980.0        # nie ueber 10 %


def test_marktfilter():
    c = st.load_config(CFG)
    assert st.excluded_market("BHP.ASX", "stock", c)
    assert st.excluded_market("0700.HK", "stock", c)
    assert st.excluded_market("ETH", "crypto", c)
    assert st.excluded_market("BTC-USD", "", c)
    assert not st.excluded_market("AAPL", "stock", c)
    assert not st.excluded_market("SAP.DE", "stock", c)


def test_reserve_mit_kern_luecke():
    c = st.load_config(CFG)
    assert st.reserve_usd(10_000, 3_000, c) == pytest.approx(4_800)


def test_open_count(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY, signal_id INTEGER, status TEXT);
        INSERT INTO signals VALUES (1,'TREND_PULLBACK'), (2,'CORE_SWEEP'),
            (3,'MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING');
        INSERT INTO trades VALUES (1,1,'ACTIVE'), (2,2,'ACTIVE'), (3,3,'APPROVED'), (4,1,'CLOSED');
    """)
    con.commit(); con.close()
    assert st.open_count(DB(p), st.load_config(CFG)) == 2


def test_edge_gate_nimmt_familien_aus(monkeypatch):
    monkeypatch.setattr(edge_gate, "_EXTRA_EXEMPT", st.load_config(CFG)["family_sets"])
    assert edge_gate.is_exempt("MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING")
    assert edge_gate.is_exempt("CORE_SWEEP")
    assert not edge_gate.is_exempt("RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20")
    assert not edge_gate.is_exempt("TREND_KIPP_1H,SELL")


def test_edge_gate_blockt_ausgenommene_familie_nicht(monkeypatch, tmp_path):
    monkeypatch.setattr(edge_gate, "_EXTRA_EXEMPT", st.load_config(CFG)["family_sets"])
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE signal_outcomes (signal_date TEXT, signal_type TEXT, fwd_5d_pnl REAL)")
    con.executemany("INSERT INTO signal_outcomes VALUES (?,?,?)",
                    [(f"2026-09-{i % 20 + 1:02d}", "TREND_PULLBACK", -0.02) for i in range(40)])
    con.commit(); con.close()
    res = edge_gate.evaluate("TREND_PULLBACK", "NORMAL", DB(p))
    assert res["shadow"] is True and res["exempt"] is True
    assert edge_gate.blocks(res) is False
