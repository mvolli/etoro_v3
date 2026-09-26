"""feat/core-sweep-buy-gate: Core-Sweep sieht Instrument-, Sektor- und
Regionen-Grenze wie der Signal-Pfad."""
from pathlib import Path

import bot.core.risk as risk
from bot.workers.signal_worker import _core_sweep_portfolio_gate as gate

SW_SRC = (Path(__file__).resolve().parents[2]
          / "src/bot/workers/signal_worker.py").read_text(encoding="utf-8")
EQ = 10_000.0


def test_ohne_klumpen_durch():
    ok, amt, why = gate("SIE.DE", 400.0, EQ, [], {"SIE.DE": "Industrials"}, {"SIE.DE": "EU"}, 50.0)
    assert ok and amt == 400.0 and why == "ok"


def test_instrument_limit_blockt():
    # Default-Instrument-Limit 10 %: 1.100 USD > 1.000
    ok, _, why = gate("SIE.DE", 1_100.0, EQ, [], None, None, 50.0)
    assert not ok and "Instrument-Gate" in why


def test_db_sektor_blockt():
    opens = [{"symbol": "AIR.PA", "amount_usd": 1_900.0}]
    sectors = {"SIE.DE": "Industrials", "AIR.PA": "Industrials"}
    ok, _, why = gate("SIE.DE", 400.0, EQ, opens, sectors, None, 50.0)
    assert not ok and "SECTOR:Industrials" in why


def test_ohne_sektor_map_nur_kuratiert():
    opens = [{"symbol": "AIR.PA", "amount_usd": 1_900.0}]
    ok, _, _ = gate("SIE.DE", 400.0, EQ, opens, None, None, 50.0)
    assert ok


def test_region_hard_cap_blockt():
    opens = [{"symbol": "AIR.PA", "amount_usd": 5_100.0}]   # 51 % > 50 %
    ok, _, why = gate("SIE.DE", 400.0, EQ, opens, None,
                      {"SIE.DE": "EU", "AIR.PA": "EU"}, 50.0)
    assert not ok and "Hard-Cap" in why


def test_region_damper_verkleinert():
    opens = [{"symbol": "AIR.PA", "amount_usd": 4_250.0}]   # 42.5 % -> Damper
    ok, amt, why = gate("SIE.DE", 400.0, EQ, opens, None,
                        {"SIE.DE": "EU", "AIR.PA": "EU"}, 50.0)
    assert ok and 50.0 <= amt < 400.0 and "Damper" in why


def test_region_damper_unter_dust_floor_blockt():
    opens = [{"symbol": "AIR.PA", "amount_usd": 4_900.0}]   # ~49 % -> Faktor ~0.37
    ok, amt, why = gate("SIE.DE", 120.0, EQ, opens, None,
                        {"SIE.DE": "EU", "AIR.PA": "EU"}, 100.0)
    assert not ok and amt < 100.0 and "Dust-Floor" in why


def test_fehler_ist_fail_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaputt")
    monkeypatch.setattr(risk, "check_instrument_limit_gate", boom)
    ok, _, why = gate("SIE.DE", 400.0, EQ, [], None, None, 50.0)
    assert not ok and "fail-closed" in why


def test_verdrahtung_im_worker():
    assert "sector_by_symbol=_sector_map, region_by_symbol=_region_by_symbol," in SW_SRC
    blk = SW_SRC[SW_SRC.index("for _o in _sweep_orders:"):SW_SRC.index("Core-Sweep-Pass uebersprungen")]
    # Gate vor der Order, Projektion danach
    assert blk.index("_core_sweep_portfolio_gate(") < blk.index("_cs_tid = trade_repo.create(")
    assert blk.index("_cs_tid = trade_repo.create(") < blk.index("open_positions.append(")
