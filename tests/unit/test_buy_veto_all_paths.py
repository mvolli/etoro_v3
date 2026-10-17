"""feat/veto-alle-kaufwege (VoLLi 2026-10-17): Pre-Trade-Veto auf ALLEN Kaufwegen.

Entscheidungen VoLLi:
- Index-Kern und Core-Sweep laufen durchs Veto, aber nur hartes VETO (kein REDUCE).
- „Regeln hart, LLM weich“: ohne LLM-Urteil wartet der Kauf einen Zyklus, dann
  greifen nur die Regel-Sperren (AVOID, Earnings, 8-K 1.03/3.01/4.02).
- Quellen: SEC EDGAR live, Truth Social (trumpstruth.org-RSS), frische Headlines.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.core import buy_veto as bv
from bot.db.connection import DB

NOW = datetime(2026, 10, 19, 14, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def db(tmp_path):
    d = DB(db_path=tmp_path / "t.db")
    d.execute("""CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, instrument_id INTEGER,
                 amount_usd REAL, signal_price REAL, signal_id INTEGER, status TEXT,
                 order_id TEXT, rejection_reason TEXT, direction TEXT DEFAULT 'BUY',
                 created_at TEXT)""")
    d.execute("""CREATE TABLE signals (id INTEGER PRIMARY KEY, instrument_id INTEGER, signal_type TEXT,
                 conviction TEXT, score REAL, rsi REAL, bb_pct REAL, status TEXT)""")
    d.execute("""CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT, name TEXT,
                 sector TEXT, industry TEXT, asset_class TEXT, market_region TEXT, yfinance_symbol TEXT)""")
    bv.ensure_columns(d)
    d.execute("INSERT INTO instruments VALUES (1, 'NVDA', 'NVIDIA Corporation', 'Technology', "
              "'Semiconductors', 'stock', 'US', 'NVDA')")
    d.execute("INSERT INTO instruments VALUES (2, 'ISAC.L', 'iShares MSCI ACWI UCITS ETF', NULL, NULL, "
              "'etf', 'GLOBAL', 'ISAC.L')")
    d.execute("INSERT INTO signals VALUES (10, 1, 'TREND_PULLBACK', 'MEDIUM', 40, 45, 0.3, 'CONSUMED')")
    d.execute("INSERT INTO signals VALUES (11, 2, 'CORE_INDEX', NULL, 0, NULL, NULL, 'CONSUMED')")
    d.execute("INSERT INTO trades (id, symbol, instrument_id, amount_usd, signal_price, signal_id, status) "
              "VALUES (1, 'NVDA', 1, 880, 180, 10, 'SUBMITTING')")
    d.execute("INSERT INTO trades (id, symbol, instrument_id, amount_usd, signal_price, signal_id, status) "
              "VALUES (2, 'ISAC.L', 2, 880, 100, 11, 'APPROVED')")
    return d


def _row(db, tid):
    return dict(db.fetchone("SELECT * FROM trades WHERE id=?", (tid,)))


def _no_rules(_trade):
    return None


# ── execution_gate ───────────────────────────────────────────────────────────

def test_ohne_urteil_wartet_der_kauf_und_merkt_sich_den_beginn(db):
    action, info = bv.execution_gate(db, 1, now=NOW, rule_check=_no_rules)
    assert action == "WAIT"
    assert _row(db, 1)["veto_wait_since"] == _iso(NOW)


def test_innerhalb_eines_zyklus_wird_weiter_gewartet(db):
    db.execute("UPDATE trades SET veto_wait_since=? WHERE id=1", (_iso(NOW - timedelta(minutes=10)),))
    assert bv.execution_gate(db, 1, now=NOW, rule_check=_no_rules)[0] == "WAIT"


def test_nach_einem_zyklus_ohne_urteil_laeuft_der_kauf_mit_no_llm(db):
    db.execute("UPDATE trades SET veto_wait_since=? WHERE id=1", (_iso(NOW - timedelta(minutes=15)),))
    action, info = bv.execution_gate(db, 1, now=NOW, rule_check=_no_rules)
    assert action == "PASS" and info["decision"] == "NO_LLM"
    assert _row(db, 1)["veto_decision"] == "NO_LLM"


def test_regel_sperre_greift_auch_ohne_llm(db):
    db.execute("UPDATE trades SET veto_wait_since=? WHERE id=1", (_iso(NOW - timedelta(minutes=15)),))
    action, info = bv.execution_gate(db, 1, now=NOW, rule_check=lambda t: "Insolvenz (8-K 1.03)")
    assert action == "BLOCK" and "1.03" in info["reason"]
    assert _row(db, 1)["veto_decision"] == "RULE_VETO"


def test_gueltiges_urteil_laesst_durch_und_liefert_reduzierten_betrag(db):
    db.execute("UPDATE trades SET veto_decision='REDUCE', veto_at=?, amount_usd=440 WHERE id=1",
               (_iso(NOW - timedelta(minutes=5)),))
    action, info = bv.execution_gate(db, 1, now=NOW, rule_check=_no_rules)
    assert action == "PASS" and info["amount_usd"] == 440.0


def test_altes_urteil_zaehlt_nicht(db):
    db.execute("UPDATE trades SET veto_decision='APPROVE', veto_at=? WHERE id=1",
               (_iso(NOW - timedelta(minutes=61)),))
    assert bv.execution_gate(db, 1, now=NOW, rule_check=_no_rules)[0] == "WAIT"


def test_veto_urteil_laesst_nie_durch(db):
    db.execute("UPDATE trades SET veto_decision='VETO', veto_at=? WHERE id=1", (_iso(NOW),))
    assert bv.execution_gate(db, 1, now=NOW, rule_check=_no_rules)[0] == "WAIT"


def test_set_verdict_nur_auf_approved_ohne_order(db):
    assert bv.set_verdict(db, 2, "APPROVE", "ok") is True
    assert bv.set_verdict(db, 1, "APPROVE", "ok") is False          # SUBMITTING
    db.execute("UPDATE trades SET status='APPROVED', order_id='123' WHERE id=1")
    assert bv.set_verdict(db, 1, "APPROVE", "ok") is False          # Order schon beim Broker


def test_set_verdict_setzt_warte_marke_zurueck(db):
    db.execute("UPDATE trades SET veto_wait_since='2026-10-19 10:00:00' WHERE id=2")
    bv.set_verdict(db, 2, "APPROVE", "ok")
    assert _row(db, 2)["veto_wait_since"] is None


# ── Regel-Sperren ────────────────────────────────────────────────────────────

NVDA = {"symbol": "NVDA", "asset_class": "stock", "yfinance_symbol": "NVDA"}


def test_avoid_flag_sperrt(monkeypatch):
    monkeypatch.setattr(bv, "earnings_block_reason", lambda s: pytest.fail("nicht noetig"))
    r = bv.rule_block_reason(NVDA, flags={"NVDA": {"flag": "AVOID", "reason": "Ermittlung"}})
    assert r.startswith("News-Flag AVOID")


def test_earnings_sperren(monkeypatch):
    monkeypatch.setattr(bv, "earnings_block_reason", lambda s: "Earnings binnen 2 Tagen")
    assert "Earnings" in bv.rule_block_reason(NVDA, flags={})


def test_8k_items_sperren_nur_die_drei_kritischen(monkeypatch):
    monkeypatch.setattr(bv, "earnings_block_reason", lambda s: None)
    monkeypatch.setattr(bv, "sec_cik", lambda s: 1045810)
    monkeypatch.setattr(bv, "sec_recent_filings", lambda c: [
        {"form": "8-K", "date": "2026-10-15", "items": "2.02,9.01"}])
    assert bv.rule_block_reason(NVDA, flags={}) is None
    monkeypatch.setattr(bv, "sec_recent_filings", lambda c: [
        {"form": "8-K", "date": "2026-10-15", "items": "4.02,9.01"}])
    assert "4.02" in bv.rule_block_reason(NVDA, flags={})


def test_nicht_us_titel_fragen_die_sec_nicht(monkeypatch):
    monkeypatch.setattr(bv, "earnings_block_reason", lambda s: None)
    monkeypatch.setattr(bv, "sec_cik", lambda s: pytest.fail("kein SEC-Call fuer SAP.DE"))
    assert bv.rule_block_reason({"symbol": "SAP.DE", "asset_class": "stock"}, flags={}) is None


def test_sec_ausfall_sperrt_nicht(monkeypatch):
    monkeypatch.setattr(bv, "earnings_block_reason", lambda s: None)
    monkeypatch.setattr(bv, "sec_cik", lambda s: 1)
    monkeypatch.setattr(bv, "sec_recent_filings", lambda c: None)
    assert bv.rule_block_reason(NVDA, flags={}) is None


def test_etf_ohne_earnings_abfrage(monkeypatch):
    monkeypatch.setattr(bv, "earnings_block_reason", lambda s: pytest.fail("ETF hat keine Earnings"))
    assert bv.rule_block_reason({"symbol": "ISAC.L", "asset_class": "etf"}, flags={}) is None


# ── Truth Social ─────────────────────────────────────────────────────────────

RSS = """<rss><channel>
<item><title><![CDATA[x]]></title><link>https://www.trumpstruth.org/statuses/1</link>
<description><![CDATA[<p>We will put a 100% TARIFF on all Semiconductors from China!</p>]]></description>
<pubDate>Sat, 17 Oct 2026 04:11:12 +0000</pubDate></item>
<item><title><![CDATA[y]]></title><link>https://www.trumpstruth.org/statuses/2</link>
<description><![CDATA[<p>Best in History!</p><p><a href="https://x/y.jpg">View image</a></p>]]></description>
<pubDate>Sat, 17 Oct 2026 04:10:57 +0000</pubDate></item>
</channel></rss>"""


def test_rss_wird_zu_posts_mit_zeit_und_text():
    posts = bv.parse_truth_rss(RSS)
    assert [p["ts"] for p in posts] == ["2026-10-17 04:11:12", "2026-10-17 04:10:57"]
    assert posts[1]["text"] == "Best in History!"


def test_nur_relevante_posts_gehen_ins_prompt():
    posts = bv.parse_truth_rss(RSS)
    trade = {"symbol": "NVDA", "name": "NVIDIA Corporation", "sector": "Technology",
             "industry": "Semiconductors"}
    hits = bv.relevant_posts(posts, bv.trade_keywords(trade))
    assert len(hits) == 1 and "TARIFF" in hits[0]["text"]


def test_ticker_nur_als_ganzes_wort():
    posts = [{"ts": "2026-10-17 04:00:00", "text": "Great MEETING today"}]
    assert bv.relevant_posts(posts, ["MEET"]) == []
