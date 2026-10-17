"""feat/analyst-strategy (VoLLi 2026-10-17): Q2-Screen + Earnings-Katalysator S1.

Belege E8/E6/E9. Geprueft werden die Regeln (identisch zum Backtest), die
Ausstiegsgruende (X0 + harte News, KEIN Earnings-Ausstieg), Handelstage,
Tranche und die Einbindung (kein Broker-TP, Ausnahme von generischen Exits).
"""
from __future__ import annotations

from datetime import date

import pytest

from bot.core import analyst_strategy as an
from bot.core import earnings_catalyst as ec
from bot.core import quality_screen as qs


# ── Q2: companyfacts -> Kennzahlen (wie FSDS) ────────────────────────────────

def _fact(start, end, val, accn="A1", fp="Q2", form="10-Q", filed="2026-08-26"):
    d = {"end": end, "val": val, "accn": accn, "fy": 2027, "fp": fp, "form": form, "filed": filed}
    if start:
        d["start"] = start
    return d


FACTS = {"facts": {"us-gaap": {
    "Revenues": {"units": {"USD": [
        _fact("2025-01-27", "2025-07-27", 90.0), _fact("2026-01-26", "2026-07-26", 180.0),
        _fact("2026-04-27", "2026-07-26", 96.0)]}},
    "GrossProfit": {"units": {"USD": [_fact("2026-01-26", "2026-07-26", 130.0)]}},
    "NetIncomeLoss": {"units": {"USD": [_fact("2026-01-26", "2026-07-26", 110.0)]}},
    "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
        _fact("2026-01-26", "2026-07-26", 70.0)]}},
    "Assets": {"units": {"USD": [_fact(None, "2026-07-26", 320.0),
                                 _fact(None, "2026-07-26", 320.0, accn="A0", form="10-Q/A")]}},
}}}


def test_companyfacts_ergibt_e8_kennzahlen():
    fl = qs.facts_to_filings(FACTS)
    assert len(fl) == 1 and fl[0][0] == "2026-08-26"
    m = fl[0][1]
    assert m["gpa"] == pytest.approx(130 * 2 / 320)          # YTD Q2 -> x 4/2
    assert m["acc"] == pytest.approx((110 - 70) * 2 / 320)
    assert m["growth"] == pytest.approx(180 / 90 - 1)


def test_vorjahr_muss_derselbe_tag_sein():
    f = {"facts": {"us-gaap": dict(FACTS["facts"]["us-gaap"])}}
    f["facts"]["us-gaap"]["Revenues"] = {"units": {"USD": [_fact("2026-01-26", "2026-07-26", 180.0)]}}
    f["facts"]["us-gaap"]["SalesRevenueNet"] = {"units": {"USD": [_fact("2025-01-27", "2025-07-27", 90.0)]}}
    assert qs.facts_to_filings(f) == []


def test_snapshot_nur_vor_stichtag_und_hoechstens_200_tage():
    fl = {"A": [("2026-03-02", {"x": 1}), ("2026-08-31", {"x": 2})], "B": [("2025-12-01", {"x": 3})]}
    s = qs.snapshot(fl, "2026-08-31")
    assert s["A"] == {"x": 1} and "B" not in s


def test_rank_picks_top_dezil_und_momentum_ueber_median():
    snap = {f"T{i}": {"gpa": i, "acc": -i, "growth": i} for i in range(100)}
    mom = {f"T{i}": (1.0 if i != 98 else -1.0) for i in range(100)}
    mom.update({f"T{i}": -0.5 for i in range(50)})
    picks = qs.rank_picks(snap, mom, buyable={f"T{i}" for i in range(100)} - {"T97"})
    names = [t for _, t in picks]
    assert names[0] == "T99" and "T98" not in names and "T97" not in names
    assert all(int(t[1:]) >= 90 for t in names)


def test_momentum_12_1():
    closes = [100.0] * 231 + [150.0] + [200.0] * 21
    assert qs.momentum_12_1(closes) == pytest.approx(0.5)
    assert qs.momentum_12_1(closes[1:]) is None


# ── S1 ───────────────────────────────────────────────────────────────────────

def test_day0_regel_wie_e6():
    dates = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
    assert ec.day0_index(dates, "2026-10-13", bmo=True) == 1
    assert ec.day0_index(dates, "2026-10-13", bmo=False) == 2     # nach Schluss/unbekannt
    assert ec.day0_index(dates, "2026-10-11", bmo=True) == 0      # Wochenende -> naechster Tag


def _series(n=230):
    dates = [f"D{i:04d}" for i in range(n)]
    closes = [100.0] * (n - 2) + [104.0, 105.0]                    # day0 +4 %, d1 105
    spy = {d: 500.0 for d in dates}
    spy[dates[-2]] = 505.0                                          # SPY +1 % an day0
    return dates, closes, spy


def test_s1_bedingungen():
    dates, closes, spy = _series()
    ev = [(dates[-2], True, 8.0)]
    r = ec.evaluate(dates, closes, spy, ev, dates[-1])
    assert r and r["surprise"] == 8.0 and r["ear"] == pytest.approx(3.0)
    assert ec.evaluate(dates, closes, spy, [(dates[-2], True, 4.0)], dates[-1]) is None   # Ueberraschung
    spy2 = dict(spy); spy2[dates[-2]] = 512.0                                            # Reaktion < 2 %
    assert ec.evaluate(dates, closes, spy2, ev, dates[-1]) is None
    closes3 = [200.0] * 228 + [104.0, 105.0]                                             # unter SMA200
    assert ec.evaluate(dates, closes3, spy, ev, dates[-1]) is None


# ── Ausstiege, Handelstage, Tranche ──────────────────────────────────────────

def test_handelstage_mit_nyse_feiertagen():
    # 2026-11-23 (Mo) .. 2026-11-27 (Fr), Thanksgiving 26.11. -> 4 Handelstage
    assert an.trading_days_held(date(2026, 11, 23), date(2026, 11, 27)) == 4


ROW = {"symbol": "NVDA", "confirmed_at": "2026-05-01 14:00:00"}
CFG = dict(an.DEFAULTS)


def test_ausstieg_nach_126_handelstagen():
    assert an.exit_reason(ROW, CFG, date(2026, 10, 15), {}) is None
    r = an.exit_reason(ROW, CFG, date(2026, 11, 3), {})
    assert r and "Haltedauer" in r


def test_kein_ausstieg_vor_earnings_nur_bei_news_avoid():
    earn = {"NVDA": {"flag": "AVOID", "reason": "Earnings am 2026-10-20", "source": "earnings_calendar"}}
    assert an.exit_reason(ROW, CFG, date(2026, 10, 15), earn) is None
    news = {"NVDA": {"flag": "AVOID", "reason": "SEC-Ermittlung", "source": "news_llm"}}
    assert "AVOID" in an.exit_reason(ROW, CFG, date(2026, 10, 15), news)


def test_8k_nur_seit_einstieg():
    old = [{"form": "8-K", "date": "2026-04-20", "items": "4.02"}]
    assert an.exit_reason(ROW, CFG, date(2026, 10, 15), {}, old) is None
    new = [{"form": "8-K", "date": "2026-10-14", "items": "1.03"}]
    assert "1.03" in an.exit_reason(ROW, CFG, date(2026, 10, 15), {}, new)


def test_tranche_6_5_prozent_und_regime_kappe():
    assert an.tranche_usd(CFG, 8797.33, 10.0) == pytest.approx(571.83, abs=0.01)
    assert an.tranche_usd(CFG, 8797.33, 5.0) == pytest.approx(8797.33 * 0.05 * 0.98, abs=0.01)


def test_config_default_aus_und_ungueltig_aus():
    assert an.load_config({})["mode"] == "off"
    assert an.load_config({"analyst_strategy": {"mode": "yolo"}})["mode"] == "off"


# ── Einbindung ───────────────────────────────────────────────────────────────

class _DB:
    def __init__(self, sig):
        self.sig = sig

    def fetchone(self, q, p=()):
        return {"signal_type": self.sig}


@pytest.mark.parametrize("sig,expected", [("SCREEN_Q2", None), ("EARN_S1", None),
                                          ("CORE_INDEX", None), ("TREND_PULLBACK", 25.0)])
def test_kein_broker_take_profit_fuer_strategie_und_kern(sig, expected):
    from bot.workers.execution_worker import _safety_tp_pct
    assert _safety_tp_pct(_DB(sig), {"signal_id": 1}, {"sl": {"safety_tp_pct": 25.0}}) == expected


def test_edge_gate_ausnahme():
    from bot.core import edge_gate
    assert edge_gate.is_exempt("SCREEN_Q2") and edge_gate.is_exempt("EARN_S1")


def test_risk_worker_nimmt_strategie_positionen_heraus(monkeypatch):
    from bot.core import core_index
    from bot.workers import risk_worker
    monkeypatch.setattr(core_index, "core_position_ids", lambda db: {"1"})
    monkeypatch.setattr(an, "position_ids", lambda db: {"2"})
    kept = risk_worker._exclude_core_positions(None, [{"positionID": 1}, {"positionID": 2}, {"positionID": 3}])
    assert [p["positionID"] for p in kept] == [3]
