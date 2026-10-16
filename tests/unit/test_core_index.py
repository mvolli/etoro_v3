#!/usr/bin/env python3
"""Unit tests — feat/core-index (2026-10-16).

Bot-verwalteter Index-Kern (VoLLi-Entscheid 16.10.): Kauf-only in Tranchen,
von allen Ausstiegspfaden ausgenommen, kein Handels-Exposure. Default off.
"""
from __future__ import annotations

import types

import pytest

from bot.api.client import EToroClient
from bot.core import core_index as ci
from bot.core import edge_gate
from bot.db.connection import DB


# ── Hilfen ───────────────────────────────────────────────────────────────────
def _cfg(**kw):
    base = dict(ci.DEFAULT_CONFIG, mode="live", symbol="ACWI", instrument_id=4002,
                target_pct=50.0, band_pct=2.0, min_order_usd=200.0, min_cash_pct=18.0)
    base.update(kw)
    return base


def _plan(**kw):
    args = dict(cfg=_cfg(), equity=10_000.0, cash=8_000.0, core_usd=0.0,
                regime="NORMAL", regime_max_trade_pct=10.0,
                pending_core_trade=False, bought_today=False)
    args.update(kw)
    return ci.plan_tranche(**args)


@pytest.fixture
def db(tmp_path):
    d = DB(db_path=tmp_path / "trading.db")
    d.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT)")
    d.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, signal_id INTEGER, "
              "api_position_id TEXT, status TEXT, created_at TEXT DEFAULT (datetime('now')))")
    return d


def _trade(db, sig_type, pid, status, shift="+0 seconds"):
    cur = db.execute("INSERT INTO signals (signal_type) VALUES (?)", (sig_type,))
    sid = cur.lastrowid
    db.execute("INSERT INTO trades (signal_id, api_position_id, status, created_at) "
               "VALUES (?, ?, ?, datetime('now', ?))", (sid, pid, status, shift))


# ── Config ───────────────────────────────────────────────────────────────────
def test_default_ist_aus():
    assert ci.load_config({})["mode"] == "off"
    assert ci.DEFAULT_CONFIG["target_pct"] == 0.0


def test_yaml_false_und_ungueltig_werden_off():
    assert ci.load_config({"core_index": {"mode": False}})["mode"] == "off"
    assert ci.load_config({"core_index": {"mode": "yolo"}})["mode"] == "off"
    assert ci.load_config({"core_index": {"mode": "shadow"}})["mode"] == "shadow"


def test_keys_nicht_llm_tunbar():
    from bot.workers.llm_review_worker import BIBLE_HARD_LIMITS
    assert not any(k.startswith("core_index.") for k in BIBLE_HARD_LIMITS)


# ── Planer ───────────────────────────────────────────────────────────────────
def test_off_plant_nichts():
    assert _plan(cfg=_cfg(mode="off")).amount_usd is None


def test_ohne_symbol_nichts():
    assert _plan(cfg=_cfg(symbol="", instrument_id=0)).amount_usd is None


def test_tranche_auf_regime_cap_begrenzt():
    # Luecke $5000, Cap 10 % von $10k = $1000
    assert _plan().amount_usd == 1000.0


def test_defensive_cap_kleiner():
    assert _plan(regime="DEFENSIVE", regime_max_trade_pct=5.0).amount_usd == 500.0


def test_critical_kauft_nie():
    assert _plan(regime="CRITICAL").amount_usd is None


def test_pending_und_heute_gekauft_blockieren():
    assert _plan(pending_core_trade=True).amount_usd is None
    assert _plan(bought_today=True).amount_usd is None


def test_im_band_kein_nachkauf():
    # Ziel $5000, Kern $4900, Band $200 -> Luecke $100 <= Band
    assert _plan(core_usd=4_900.0).amount_usd is None


def test_cash_reserve_begrenzt():
    # Reserve 18 % = $1800; Cash $2300 -> nur $500 verfuegbar
    assert _plan(cash=2_300.0).amount_usd == 500.0


def test_unter_mindestorder_nichts():
    # Cash $1900 -> $100 ueber Reserve < $200 min
    assert _plan(cash=1_900.0).amount_usd is None


def test_letzte_tranche_nur_bis_ziel():
    # Ziel $5000, Kern $4700 -> Luecke $300
    assert _plan(core_usd=4_700.0).amount_usd == 300.0


def test_equity_null_fail_closed():
    assert _plan(equity=0.0).amount_usd is None


# ── Positionen ───────────────────────────────────────────────────────────────
def test_kern_positionen_nur_offene_core_index(db):
    _trade(db, "CORE_INDEX", "111", "ACTIVE")
    _trade(db, "CORE_INDEX", "222", "CLOSED")
    _trade(db, "CORE_SWEEP", "333", "ACTIVE")
    assert ci.core_position_ids(db) == {"111"}
    assert ci.is_core_position(db, 111) is True
    assert ci.is_core_position(db, "333") is False


def test_pending_und_bought_today(db):
    assert ci.pending_core_trade(db) is False
    assert ci.bought_today(db) is False
    _trade(db, "CORE_INDEX", None, "APPROVED")
    assert ci.pending_core_trade(db) is True
    assert ci.bought_today(db) is True


def test_gestern_gekauft_zaehlt_nicht_als_heute(db):
    _trade(db, "CORE_INDEX", "111", "ACTIVE", shift="-1 day")
    assert db.fetchone("SELECT created_at FROM trades")["created_at"] is not None
    assert ci.bought_today(db) is False


def test_db_fehler_blockiert_kaeufe():
    class Kaputt:
        def fetchone(self, *a, **k):
            raise RuntimeError("weg")
        def fetchall(self, *a, **k):
            raise RuntimeError("weg")
    assert ci.pending_core_trade(Kaputt()) is True
    assert ci.bought_today(Kaputt()) is True
    assert ci.core_position_ids(Kaputt()) == set()


def test_exclude_und_exposure_beide_formate():
    broker = [{"positionID": 111, "amount": 500.0}, {"positionID": 222, "amount": 60.0}]
    snap = [{"api_position_id": "111", "amount_usd": 500.0}, {"api_position_id": "222", "amount_usd": 60.0}]
    assert [p["positionID"] for p in ci.exclude_core(broker, {"111"})] == [222]
    assert [p["api_position_id"] for p in ci.exclude_core(snap, {"111"})] == ["222"]
    assert ci.core_exposure(snap, {"111"}) == (500.0, 1)
    assert ci.core_exposure(broker, {"111"}) == (500.0, 1)
    assert ci.exclude_core(broker, set()) == broker


# ── Sicherheitsnetz close_position ───────────────────────────────────────────
def _fake_client(is_core):
    fake = types.SimpleNamespace(posts=[])
    fake._is_core_index_position = lambda pid: is_core
    def post(endpoint, body):
        fake.posts.append((endpoint, body))
        return {"orderID": 1}
    fake.post = post
    return fake


def test_close_verweigert_kern_position():
    fake = _fake_client(True)
    assert EToroClient.close_position(fake, "111", 4002) == {}
    assert fake.posts == []


def test_close_mit_allow_core_geht_durch():
    fake = _fake_client(True)
    assert EToroClient.close_position(fake, "111", 4002, allow_core=True) == {"orderID": 1}
    assert len(fake.posts) == 1


def test_close_normale_position_unveraendert():
    fake = _fake_client(False)
    assert EToroClient.close_position(fake, "222", 1, units_to_deduct=2.0) == {"orderID": 1}
    assert fake.posts[0][1] == {"instrumentId": 1, "UnitsToDeduct": 2.0}


def test_core_pruefung_fail_open(monkeypatch):
    import sqlite3
    def boom(*a, **k):
        raise sqlite3.OperationalError("db weg")
    monkeypatch.setattr(sqlite3, "connect", boom)
    assert EToroClient._is_core_index_position("111") is False


# ── Edge-Gate ────────────────────────────────────────────────────────────────
def test_edge_gate_nimmt_core_index_aus():
    assert "CORE_INDEX" in edge_gate.GATE_EXEMPT_TYPES


# ── Signal-Worker-Schritt ────────────────────────────────────────────────────
class _Repo:
    def __init__(self):
        self.created, self.status = [], []
    def create(self, **kw):
        self.created.append(kw); return len(self.created)
    def update_status(self, tid, status, **kw):
        self.status.append((tid, status))
    def update_signal_status(self, sid, status):
        self.status.append((sid, status))
    def write(self, *a, **k):
        self.created.append(("log",) + a)
    def get_all(self):
        return []


def _run(monkeypatch, mode):
    from bot.workers import signal_worker as sw
    monkeypatch.setattr(ci, "pending_core_trade", lambda db: False)
    monkeypatch.setattr(ci, "bought_today", lambda db: False)
    trade_repo, signal_repo, log_repo, port = _Repo(), _Repo(), _Repo(), _Repo()
    out = sw._run_core_index(
        cfg={"core_index": {"mode": mode, "symbol": "ACWI", "instrument_id": 4002,
                            "target_pct": 50.0}},
        db=None, trade_repo=trade_repo, signal_repo=signal_repo, log_repo=log_repo,
        portfolio_repo=port, equity=10_000.0, regime="DEFENSIVE",
        regime_params={"max_trade_pct": 5.0}, cash_estimate=8_000.0,
        core_ids=set(), approved_count=0)
    return out, trade_repo, signal_repo, log_repo


def test_signal_worker_off_legt_nichts_an(monkeypatch):
    out, trades, sigs, _ = _run(monkeypatch, "off")
    assert out == (0, 8_000.0) and trades.created == [] and sigs.created == []


def test_signal_worker_shadow_loggt_ohne_trade(monkeypatch):
    out, trades, sigs, log = _run(monkeypatch, "shadow")
    assert out == (0, 8_000.0) and trades.created == [] and sigs.created == []
    assert any("SHADOW" in str(x) for x in log.created)


def test_signal_worker_live_legt_approved_trade_an(monkeypatch):
    out, trades, sigs, _ = _run(monkeypatch, "live")
    assert out == (1, 7_500.0)                          # DEFENSIVE-Cap 5 % = $500
    assert sigs.created[0]["signal_type"] == "CORE_INDEX"
    t = trades.created[0]
    assert t["amount_usd"] == 500.0 and t["stop_loss_pct"] == 25.0 and t["symbol"] == "ACWI"
    assert (1, "APPROVED") in trades.status


def test_signal_worker_fehler_fail_open(monkeypatch):
    from bot.workers import signal_worker as sw
    def boom(cfg):
        raise RuntimeError("kaputt")
    monkeypatch.setattr(ci, "load_config", boom)
    out = sw._run_core_index(cfg={}, db=None, trade_repo=None, signal_repo=None,
                             log_repo=None, portfolio_repo=None, equity=1.0, regime="NORMAL",
                             regime_params={}, cash_estimate=5.0, core_ids=set(),
                             approved_count=3)
    assert out == (3, 5.0)


# ── Risk-Worker: Filter vor allen Ausstiegsschritten ─────────────────────────
def test_risk_worker_filter_entfernt_kern(db):
    from bot.workers import risk_worker as rw
    _trade(db, "CORE_INDEX", "111", "ACTIVE")
    raw = [{"positionID": 111, "amount": 500.0}, {"positionID": 222, "amount": 60.0}]
    assert [p["positionID"] for p in rw._exclude_core_positions(db, raw)] == [222]


def test_risk_worker_filter_fehler_laesst_liste(monkeypatch):
    from bot.workers import risk_worker as rw
    monkeypatch.setattr(ci, "core_position_ids", lambda db: (_ for _ in ()).throw(RuntimeError("x")))
    raw = [{"positionID": 1}]
    assert rw._exclude_core_positions(None, raw) == raw


def test_risk_worker_main_filtert_vor_jedem_ausstiegsschritt():
    """Struktur-Waechter: der Filter muss in main() VOR dem ersten Ausstiegs-
    schritt stehen, sonst sehen SL/Trailing/Konzentration den Kern."""
    import inspect
    from bot.workers import risk_worker as rw
    src = inspect.getsource(rw.main)
    i_filter = src.index("_exclude_core_positions(db, raw_positions)")
    for step in ("_run_sl_checks(", "_run_concentration(", "_run_trailing(",
                 "_run_exit_monitor(", "_run_sell_exits(", "_run_earnings_exit("):
        assert i_filter < src.index(step), step


# ── Signal-Worker: Ausschluesse ──────────────────────────────────────────────
def test_diversitaet_zaehlt_kern_nicht(tmp_path):
    from bot.workers import signal_worker as sw
    d = DB(db_path=tmp_path / "div.db")
    d.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, signal_type TEXT)")
    d.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, instrument_id INTEGER, "
              "status TEXT, signal_id INTEGER)")
    d.execute("CREATE TABLE portfolio_snapshot (api_position_id TEXT, instrument_id INTEGER)")
    for iid, typ in ((1, "CORE_INDEX"), (2, "TREND_PULLBACK")):
        sid = d.execute("INSERT INTO signals (signal_type) VALUES (?)", (typ,)).lastrowid
        d.execute("INSERT INTO trades (instrument_id, status, signal_id) VALUES (?, 'ACTIVE', ?)", (iid, sid))
        d.execute("INSERT INTO portfolio_snapshot VALUES (?, ?)", (str(100 + iid), iid))
    cats = sw._open_signal_categories(d)
    assert "CORE_INDEX" not in cats and sum(cats.values()) == 1


def test_kern_kategorie_eingetragen():
    from bot.workers import signal_worker as sw
    assert sw.SIGNAL_CATEGORY.get("CORE_INDEX") == "CORE_INDEX"


def test_exposure_abzug_nach_cash_schaetzung():
    """Das Cash fuer den Kern ist real weg — die equity−exposure-Schaetzung
    muss also VOR dem Abzug laufen, sonst wuerde Cash ueberschaetzt."""
    import inspect
    from bot.workers import signal_worker as sw
    src = inspect.getsource(sw.main)
    i_cash = src.index("cash_estimate = max(0.0, equity - total_exposure)")
    i_core = src.index("_ci.core_exposure(portfolio_repo.get_all(), _core_ids)")
    i_step = src.index("_run_core_index(")
    assert i_cash < i_core < i_step
