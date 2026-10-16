#!/usr/bin/env python3
"""Unit tests — feat/market-regime (2026-10-16, VoLLi).

Regime = strengeres aus Markt (SPY: SMA200 + Abstand vom 52W-Hoch) und Konto-
Bremse (Drawdown vom 30-Tage-Hoch, 20/30/40 %). Fehlende Marktdaten -> mind.
CAUTION. Austritt immer unter Eintritt. max_trade_pct in allen Regimen, nie
unter $200.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import bot.core.regime as rg

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for k, v in dict(REGIME_SOURCE="market", MARKET_CAUTION_DD_PCT=10.0,
                     MARKET_CAUTION_EXIT_DD_PCT=8.0, MARKET_SMA_EXIT_BUFFER_PCT=1.0,
                     MARKET_DEFENSIVE_DD_PCT=15.0, MARKET_DEFENSIVE_EXIT_DD_PCT=12.0,
                     MARKET_CRITICAL_DD_PCT=25.0, MARKET_CRITICAL_EXIT_DD_PCT=20.0,
                     CAUTION_THRESHOLD=20.0, DEFENSIVE_THRESHOLD=30.0, CRITICAL_THRESHOLD=40.0,
                     CAUTION_EXIT=18.0, DEFENSIVE_EXIT=28.0, CRITICAL_EXIT=38.0).items():
        monkeypatch.setattr(rg, k, v)


def _m(close, sma, dd, date=None):
    return {"close": close, "sma200": sma, "dd_pct": dd,
            "date": date or datetime.now(timezone.utc).date().isoformat()}


# ── Marktstufen ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("m,prev,exp", [
    (_m(110, 100, 2), "NORMAL", "NORMAL"),
    (_m(99, 100, 3), "NORMAL", "CAUTION"),            # unter SMA200
    (_m(105, 100, 11), "NORMAL", "CAUTION"),          # >= 10 % unter Hoch
    (_m(90, 100, 16), "CAUTION", "DEFENSIVE"),        # unter SMA UND >= 15 %
    (_m(80, 100, 26), "NORMAL", "CRITICAL"),          # >= 25 %
])
def test_marktstufen(m, prev, exp):
    assert rg.detect_market_regime(m, prev)[0] == exp


@pytest.mark.parametrize("m,prev,exp", [
    (_m(100.5, 100, 3), "CAUTION", "CAUTION"),        # ueber SMA, aber < SMA*1.01
    (_m(102, 100, 9), "CAUTION", "CAUTION"),          # dd > 8 -> bleibt
    (_m(102, 100, 7), "CAUTION", "NORMAL"),           # beide Austrittsbedingungen erfuellt
    (_m(102, 100, 13), "DEFENSIVE", "DEFENSIVE"),     # dd > 12 -> bleibt
    (_m(102, 100, 11), "DEFENSIVE", "CAUTION"),
    (_m(90, 100, 22), "CRITICAL", "CRITICAL"),        # dd > 20 -> bleibt
    (_m(90, 100, 19), "CRITICAL", "DEFENSIVE"),
])
def test_markt_hysterese(m, prev, exp):
    assert rg.detect_market_regime(m, prev)[0] == exp


def test_stricter():
    assert rg.stricter("NORMAL", "CAUTION") == "CAUTION"
    assert rg.stricter("CRITICAL", "DEFENSIVE") == "CRITICAL"


# ── Kennzahlen aus ohlcv_daily ───────────────────────────────────────────────
class _DB:
    def __init__(self, closes, start="2025-09-01"):
        d0 = datetime.fromisoformat(start)
        self.rows = [{"d": (d0 + timedelta(days=i)).date().isoformat(), "close": c}
                     for i, c in enumerate(closes)]
    def fetchall(self, sql, params=()):
        return self.rows


def test_metrics_brauchen_200_kurse():
    assert rg.market_metrics(_DB([100.0] * 199)) is None


def test_metrics_sma_und_abstand():
    closes = [100.0] * 251 + [80.0]
    m = rg.market_metrics(_DB(closes))
    assert m["dd_pct"] == pytest.approx(20.0)
    assert m["sma200"] == pytest.approx((199 * 100 + 80) / 200)


# ── update_regime: Kombination, getrennte Zustaende, Altmodus ────────────────
class _State:
    def __init__(self, **kv):
        self.kv = {k: str(v) for k, v in kv.items()}
        self.db = None
    def get(self, k, default=None):
        return self.kv.get(k, default)
    def set(self, k, v):
        self.kv[k] = str(v)
    def get_float(self, k, default=0.0):
        try:
            return float(self.kv[k])
        except (KeyError, ValueError):
            return default
    def get_regime(self):
        return self.kv.get("CURRENT_REGIME", "NORMAL")
    def set_regime(self, r):
        self.kv["CURRENT_REGIME"] = r


def _run(monkeypatch, *, market, equity=9_000.0, peak=10_000.0, prev="DEFENSIVE", **state):
    monkeypatch.setattr(rg, "market_metrics", lambda db, instrument_id=None: market)
    monkeypatch.setattr(rg, "record_equity_snapshot", lambda db, eq: None)
    monkeypatch.setattr(rg, "get_rolling_peak", lambda db, eq, days=30: peak)
    st = _State(PEAK_EQUITY=peak, CURRENT_REGIME=prev, **state)
    reg, _ = rg.update_regime(st, equity)
    return reg, st


def test_markt_normal_konto_normal_ergibt_normal(monkeypatch):
    reg, st = _run(monkeypatch, market=_m(110, 100, 1))   # Konto-DD 10 % < 20
    assert reg == "NORMAL"
    assert st.get("MARKET_REGIME") == "NORMAL" and st.get("ACCOUNT_REGIME") == "NORMAL"


def test_strengeres_gewinnt_markt(monkeypatch):
    reg, _ = _run(monkeypatch, market=_m(95, 100, 12))     # Markt CAUTION
    assert reg == "CAUTION"


def test_strengeres_gewinnt_konto(monkeypatch):
    reg, st = _run(monkeypatch, market=_m(110, 100, 1), equity=5_500.0)  # Konto-DD 45 %
    assert reg == "CRITICAL" and st.get("MARKET_REGIME") == "NORMAL"


def test_konto_hysterese_unabhaengig_vom_markt(monkeypatch):
    # Markt CAUTION, Konto-DD 10 %: Konto-Stufe darf nicht ueber die kombinierte
    # Vorgeschichte (CAUTION) festgehalten werden
    reg, st = _run(monkeypatch, market=_m(95, 100, 12), prev="CAUTION",
                   ACCOUNT_REGIME="NORMAL")
    assert st.get("ACCOUNT_REGIME") == "NORMAL" and reg == "CAUTION"


def test_veraltete_marktdaten_mindestens_caution(monkeypatch):
    sent = []
    import bot.discord_embeds as de
    monkeypatch.setattr(de, "post_alert_embed", lambda **kw: sent.append(kw) or True)
    old = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
    reg, st = _run(monkeypatch, market=_m(110, 100, 1, date=old), MARKET_REGIME="NORMAL")
    assert reg == "CAUTION" and len(sent) == 1
    reg, _ = _run(monkeypatch, market=_m(110, 100, 1, date=old), MARKET_REGIME="NORMAL",
                  MARKET_STALE_ALERT_DATE=datetime.now(timezone.utc).date().isoformat())
    assert len(sent) == 1          # nur einmal am Tag


def test_fehlende_marktdaten_halten_strengeres_letztes(monkeypatch):
    reg, _ = _run(monkeypatch, market=None, MARKET_REGIME="DEFENSIVE")
    assert reg == "DEFENSIVE"


def test_wochenende_ist_nicht_veraltet(monkeypatch):
    fri = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    reg, _ = _run(monkeypatch, market=_m(110, 100, 1, date=fri))
    assert reg == "NORMAL"


def test_altmodus_account_unveraendert(monkeypatch):
    monkeypatch.setattr(rg, "REGIME_SOURCE", "account")
    monkeypatch.setattr(rg, "CAUTION_THRESHOLD", 4.0)
    monkeypatch.setattr(rg, "DEFENSIVE_THRESHOLD", 8.0)
    monkeypatch.setattr(rg, "DEFENSIVE_EXIT", 7.0)
    monkeypatch.setattr(rg, "CAUTION_EXIT", 3.5)
    reg, st = _run(monkeypatch, market=_m(110, 100, 1))    # Konto-DD 10 %
    assert reg == "DEFENSIVE" and st.get("MARKET_REGIME") is None


# ── Hysterese erzwingen / Config ─────────────────────────────────────────────
def test_austritt_wird_unter_eintritt_gezwungen(monkeypatch):
    monkeypatch.setattr(rg, "CAUTION_THRESHOLD", 16.0)
    monkeypatch.setattr(rg, "CAUTION_EXIT", 18.0)
    monkeypatch.setattr(rg, "MARKET_DEFENSIVE_EXIT_DD_PCT", 15.0)
    rg._enforce_exit_below_entry()
    assert rg.CAUTION_EXIT == 14.0 and rg.MARKET_DEFENSIVE_EXIT_DD_PCT == 13.0


def test_apply_config_liest_quelle_und_marktwerte(monkeypatch):
    import copy
    monkeypatch.setattr(rg, "_REGIME_PARAMS", copy.deepcopy(rg._REGIME_PARAMS))
    rg.apply_config({"regime": {"source": "market", "market_caution_dd_pct": 9.0,
                                "caution_pct": 20.0, "caution_exit_pct": 18.0}})
    assert rg.REGIME_SOURCE == "market" and rg.MARKET_CAUTION_DD_PCT == 9.0
    rg.apply_config({"regime": {"source": "quatsch"}})
    assert rg.REGIME_SOURCE == "account"


def test_live_config_konsistent():
    import yaml
    c = yaml.safe_load((REPO / "config/config.yaml").read_text())["regime"]
    assert c["source"] == "market"
    assert (c["caution_pct"], c["defensive_pct"], c["critical_pct"]) == (20.0, 30.0, 40.0)
    for e, x in (("caution_pct", "caution_exit_pct"), ("defensive_pct", "defensive_exit_pct"),
                 ("critical_pct", "critical_exit_pct"),
                 ("market_caution_dd_pct", "market_caution_exit_dd_pct"),
                 ("market_defensive_dd_pct", "market_defensive_exit_dd_pct"),
                 ("market_critical_dd_pct", "market_critical_exit_dd_pct")):
        assert c[x] < c[e], (x, e)


def test_llm_grenzen_klammern_neue_werte():
    import yaml
    from bot.workers.llm_review_worker import BIBLE_HARD_LIMITS, MAX_STEP_PER_CHANGE, _validate_config_adjustment
    c = yaml.safe_load((REPO / "config/config.yaml").read_text())["regime"]
    for k in ("caution_pct", "defensive_pct", "critical_pct", "market_caution_dd_pct",
              "market_defensive_dd_pct", "market_critical_dd_pct"):
        assert _validate_config_adjustment(f"regime.{k}", c[k])[0], k
        assert f"regime.{k}" in MAX_STEP_PER_CHANGE
    assert not _validate_config_adjustment("regime.caution_pct", 3.0)[0]


# ── max_trade_pct in allen Regimen, nie unter $200 ───────────────────────────
def test_signal_worker_klammert_in_jedem_regime():
    src = (REPO / "src/bot/workers/signal_worker.py").read_text(encoding="utf-8")
    assert 'if regime == "DEFENSIVE" and _mt_pct > 0' not in src
    assert 'float(regime_params.get("signal_floor_usd", 200.0)))' in src
    assert "_dep_cap = _mt_cap\n" in src


def test_execution_guard_in_jedem_regime():
    src = (REPO / "src/bot/workers/execution_worker.py").read_text(encoding="utf-8")
    assert 'if regime in ("NORMAL", "CAUTION", "DEFENSIVE"):' in src
    assert 'float(_rp.get("signal_floor_usd", 200.0)))' in src


@pytest.mark.parametrize("regime,eq,exp", [("NORMAL", 8800, 880.0), ("CAUTION", 8800, 660.0),
                                            ("DEFENSIVE", 8800, 440.0), ("DEFENSIVE", 3000, 200.0)])
def test_cap_rechnung(regime, eq, exp):
    p = rg.get_regime_params(regime)
    assert max(eq * p["max_trade_pct"] / 100.0, p["signal_floor_usd"]) == pytest.approx(exp)


# ── Texte DEFENSIVE-Conviction ───────────────────────────────────────────────
def test_defensive_text_folgt_config(monkeypatch):
    from bot.core.risk import check_regime_gate
    # ueber monkeypatch, damit die globale Regime-Tabelle danach wiederhergestellt wird
    monkeypatch.setitem(rg._REGIME_PARAMS["DEFENSIVE"], "min_conviction", "MEDIUM")
    msg = check_regime_gate("DEFENSIVE").reasons[0]
    assert "MEDIUM" in msg and "HIGH+" not in msg
    assert "HIGH+" not in rg.get_regime_params("DEFENSIVE")["description"]


# ── Datenkontinuitaet: SPY immer laden ───────────────────────────────────────
def test_benchmark_wird_ergaenzt_wenn_nicht_gehalten():
    from bot.workers.data_worker import _with_benchmark
    out = _with_benchmark([{"symbol": "KO", "yf_symbol": "KO", "instrument_id": 1024}])
    assert any(it["instrument_id"] == 3000 and it["yf_symbol"] == "SPY" for it in out)


def test_benchmark_nicht_doppelt():
    from bot.workers.data_worker import _with_benchmark
    items = [{"symbol": "SPY", "yf_symbol": "SPY", "instrument_id": 3000}]
    assert _with_benchmark(items) == items


def test_data_worker_nutzt_benchmark_in_tier1():
    src = (REPO / "src/bot/workers/data_worker.py").read_text(encoding="utf-8")
    assert "tier1_items: list[dict] = _with_benchmark(_get_portfolio_symbols(db))" in src
