"""feat/sector-gate-wiring: DB-Sektoren erreichen das Kauf-Gate.

Vom 2026-08-12 bis 2026-09-26 stand enforce_db_sectors=true, aber
check_buy_gate reichte die Map nie an check_asset_class_gate weiter.
"""
from pathlib import Path

import bot.core.risk as risk
from bot.core.risk import GateResult, check_asset_class_gate, check_buy_gate

SW_SRC = (Path(__file__).resolve().parents[2]
          / "src/bot/workers/signal_worker.py").read_text(encoding="utf-8")

SECTORS = {"SIE.DE": "Industrials", "AIR.PA": "Industrials", "JPM": "Financial Services"}


def _capture(monkeypatch):
    seen = {}

    def fake(symbol, buy_amount, equity, open_positions, sector_by_symbol=None):
        seen["map"] = sector_by_symbol
        return GateResult(True, ["captured"])

    monkeypatch.setattr(risk, "check_asset_class_gate", fake)
    monkeypatch.setattr(risk, "check_correlation_gate_risk",
                        lambda *a, **k: GateResult(True, ["corr skipped"]))
    return seen


def _gate(**kw):
    return check_buy_gate(
        symbol="SIE.DE", buy_amount=100.0, equity=10_000.0, cash=6_000.0,
        regime="NORMAL", total_exposed=1_000.0, open_positions=[], **kw)


def test_check_buy_gate_reicht_sector_map_durch(monkeypatch):
    seen = _capture(monkeypatch)
    _gate(sector_by_symbol=SECTORS)
    assert seen["map"] is SECTORS


def test_ohne_map_verhalten_wie_vorher(monkeypatch):
    seen = _capture(monkeypatch)
    _gate()
    assert seen["map"] is None


def test_db_sektor_blockt_ueber_limit():
    # 1.950 USD Industrials offen + 100 = 20.5 % > 20 % Default-Cap
    opens = [{"symbol": "AIR.PA", "amount_usd": 1_950.0}]
    r = check_asset_class_gate("SIE.DE", 100.0, 10_000.0, opens, SECTORS)
    assert not r.allowed and "SECTOR:Industrials" in r.reasons[0]


def test_ohne_map_faellt_dasselbe_symbol_fail_open_durch():
    opens = [{"symbol": "AIR.PA", "amount_usd": 1_950.0}]
    r = check_asset_class_gate("SIE.DE", 100.0, 10_000.0, opens, None)
    assert r.allowed and "kein Mapping" in r.reasons[0]


def test_kuratiertes_mapping_behaelt_vorrang():
    # AAPL ist US_TECH (40 %) — ein DB-Sektor darf das nicht auf 20 % druecken
    opens = [{"symbol": "MSFT", "amount_usd": 2_500.0}]
    r = check_asset_class_gate("AAPL", 100.0, 10_000.0, opens,
                               {"AAPL": "Technology", "MSFT": "Technology"})
    assert r.allowed and "US_TECH" in r.reasons[0]


def test_signal_worker_uebergibt_die_map():
    assert "sector_by_symbol=_sector_map or None," in SW_SRC
