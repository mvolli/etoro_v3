"""feat/veto-alle-kaufwege (VoLLi 2026-10-17): trade_veto_worker.main End-to-End.

Ohne Netz, ohne echte DB, ohne echtes Veto-Log: alle Quellen und das LLM sind
gestubbt. Geprueft wird die Verdrahtung: Kern-Kaeufe laufen mit, Regeln vor
dem LLM, REDUCE beim Kern wird APPROVE, Schweigen des LLM = APPROVE, LLM-Ausfall
= kein Urteil (der execution_worker wartet dann).
"""
from __future__ import annotations

import contextlib
import sys
import types

import pytest

from bot.core import buy_veto as bv
from bot.db.connection import DB
import bot.workers.trade_veto_worker as tvw


@pytest.fixture()
def env(tmp_path, monkeypatch):
    d = DB(db_path=tmp_path / "t.db")
    d.execute("""CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, instrument_id INTEGER,
                 amount_usd REAL, signal_price REAL, signal_id INTEGER, status TEXT,
                 order_id TEXT, rejection_reason TEXT, direction TEXT DEFAULT 'BUY')""")
    d.execute("""CREATE TABLE signals (id INTEGER PRIMARY KEY, instrument_id INTEGER, signal_type TEXT,
                 conviction TEXT, score REAL, rsi REAL, bb_pct REAL, status TEXT)""")
    d.execute("""CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT, name TEXT,
                 sector TEXT, industry TEXT, asset_class TEXT, market_region TEXT, yfinance_symbol TEXT)""")
    d.execute("CREATE TABLE portfolio_snapshot (symbol TEXT, unrealized_pnl_pct REAL)")
    d.execute("CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    bv.ensure_columns(d)
    rows = [(1, "NVDA", "stock", "TREND_PULLBACK"), (2, "ISAC.L", "etf", "CORE_INDEX"),
            (3, "MU", "stock", "CORE_SWEEP"), (4, "XYZ", "stock", "TREND_PULLBACK"),
            (5, "AAPL", "stock", None)]
    for i, sym, ac, st in rows:
        d.execute("INSERT INTO instruments VALUES (?,?,?,?,?,?,?,?)", (i, sym, sym, None, None, ac, "US", sym))
        if st:
            d.execute("INSERT INTO signals VALUES (?,?,?,?,?,?,?,?)", (i, i, st, "MEDIUM", 30, 40, 0.2, "CONSUMED"))
        d.execute("INSERT INTO trades (id, symbol, instrument_id, amount_usd, signal_price, signal_id, status) "
                  "VALUES (?,?,?,?,?,?, 'APPROVED')", (i, sym, i, 880.0, 100.0, i if st else None))

    import bot.db.connection as conn
    monkeypatch.setattr(conn, "DB", lambda *a, **k: d)
    import bot.core.worker_lock as wl
    monkeypatch.setattr(wl, "worker_lock", lambda name: contextlib.nullcontext(True))
    import bot.core.heartbeat as hb
    monkeypatch.setattr(hb, "record_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(tvw, "_veto_log_read", lambda: [])
    monkeypatch.setattr(tvw, "_veto_log_write", lambda e: None)
    monkeypatch.setattr(tvw, "_backfill_outcomes", lambda e: None)
    monkeypatch.setattr(tvw, "_fetch_market_context",
                        lambda: {"spy_1d_pct": 0.1, "qqq_1d_pct": 0.2, "vix": 15, "vix_label": "NORMAL"})
    monkeypatch.setattr(tvw, "_load_news_flags", lambda: {})
    monkeypatch.setattr(tvw, "_market_open", lambda db, t: True)
    monkeypatch.setattr(tvw, "_trade_context", lambda t, posts: {"headlines": [], "sec": [], "truth": []})
    monkeypatch.setattr(bv, "truth_posts", lambda: [])
    monkeypatch.setattr(bv, "rule_block_reason",
                        lambda t, flags=None: "Bilanzkorrektur (8-K 4.02)" if t["symbol"] == "XYZ" else None)
    fake_embeds = types.SimpleNamespace(posts=[])
    fake_embeds.post_alert_embed = lambda **k: fake_embeds.posts.append(k)
    monkeypatch.setitem(sys.modules, "discord_embeds", fake_embeds)
    prompts = []
    llm = {"result": None}

    def fake_llm(prompt, **k):
        prompts.append(prompt)
        return llm["result"]
    import bot.core.llm_client as lc
    monkeypatch.setattr(lc, "call_llm_json", fake_llm)
    return types.SimpleNamespace(db=d, llm=llm, prompts=prompts, embeds=fake_embeds)


def _t(env, tid):
    return dict(env.db.fetchone("SELECT * FROM trades WHERE id=?", (tid,)))


def test_alle_kaufwege_und_regeln_vor_dem_llm(env):
    env.llm["result"] = {"decisions": [
        {"trade_id": 1, "decision": "VETO", "reason": "Prognose gesenkt (Headline)"},
        {"trade_id": 2, "decision": "REDUCE", "reduce_to_pct": 50, "reason": "Unsicherheit"},
        {"trade_id": 3, "decision": "APPROVE", "reason": "ok"},
    ]}
    assert tvw.main() == 0
    # Regel-Sperre: XYZ abgelehnt, ohne im LLM-Prompt zu landen
    assert _t(env, 4)["status"] == "REJECTED" and _t(env, 4)["veto_decision"] == "RULE_VETO"
    assert "XYZ" not in env.prompts[0]
    # Kern-Kaeufe und Trade ohne Signal stehen im Prompt
    for sym in ("ISAC.L", "MU", "AAPL"):
        assert sym in env.prompts[0]
    assert "KERN-KAUF" in env.prompts[0]
    # LLM-Veto
    assert _t(env, 1)["status"] == "REJECTED" and _t(env, 1)["veto_decision"] == "VETO"
    # REDUCE beim Index-Kern -> APPROVE, Betrag unveraendert
    assert _t(env, 2)["veto_decision"] == "APPROVE" and _t(env, 2)["amount_usd"] == 880.0
    assert _t(env, 3)["veto_decision"] == "APPROVE"
    # Nicht erwaehnt (AAPL) = kein Einwand
    assert _t(env, 5)["veto_decision"] == "APPROVE"
    assert env.embeds.posts and "Veto" in env.embeds.posts[0]["title"]


def test_reduce_bei_einzelaktie_verkleinert(env):
    env.llm["result"] = {"decisions": [{"trade_id": 1, "decision": "REDUCE", "reduce_to_pct": 50,
                                        "reason": "Klage anhaengig"}]}
    tvw.main()
    t = _t(env, 1)
    assert t["status"] == "APPROVED" and t["amount_usd"] == 440.0 and t["veto_decision"] == "REDUCE"


def test_llm_ausfall_schreibt_kein_urteil(env):
    env.llm["result"] = None
    tvw.main()
    for tid in (1, 2, 3, 5):
        assert _t(env, tid)["veto_decision"] is None and _t(env, tid)["status"] == "APPROVED"
    assert _t(env, 4)["veto_decision"] == "RULE_VETO"     # Regeln wirken trotzdem


def test_gueltiges_urteil_wird_nicht_neu_geprueft(env):
    for tid in (1, 2, 3, 4, 5):
        bv.set_verdict(env.db, tid, "APPROVE", "schon geprueft")
    tvw.main()
    assert env.prompts == []
