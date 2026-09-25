"""fix/veto-signal-status (2026-09-25): abgelehnte Trades markieren auch ihr Signal.

Der Veto-Pfad setzte nur den TRADE auf REJECTED, das Signal blieb CONSUMED.
`has_fresh_signal` blockt aber auf status IN ('FRESH','REJECTED') — CONSUMED
faellt durch. Der data_worker erzeugte dasselbe Signal auf unveraendertem
Zustand neu, der Veto verbrannte erneut einen LLM-Call und einen der knappen
Kandidaten-Slots.

Gemessen ueber 14 Tage (2026-09-25): 198 "LLM-Reduce unter Min-Buy"-
Ablehnungen auf 80 Instrumenten, 2,5 je Instrument — und in ALLEN 198 Faellen
stand das Signal danach auf CONSUMED. Der Reduce-Fall ist weitgehend
vorherbestimmt: REDUCE_MIN_PCT/MAX = 25/75, min_buy_usd = 50 — ein Trade am
$50-Floor faellt bei JEDER erlaubten Reduktion darunter. Die 198 Faelle lagen
bei $50,00-$99,30 (Ø $70,11), also genau in dieser Zone.

Der signal_worker kennt die Konvention laengst (8 Aufrufstellen, u. a.
_reject_below_floor_impl fuer denselben Fall "Betrag unter Floor"). Hier wird
sie nachgezogen, kein neuer Mechanismus gebaut.
"""
from __future__ import annotations

import pytest

from bot.db.connection import DB
from bot.db.repo import SignalRepo
from bot.workers.trade_veto_worker import _apply_decision


@pytest.fixture()
def db(tmp_path):
    d = DB(db_path=tmp_path / "t.db")
    d.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, symbol TEXT, amount_usd REAL, status TEXT,
            rejection_reason TEXT, order_id TEXT, signal_id INTEGER
        )
    """)
    d.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY, instrument_id INTEGER, signal_type TEXT,
            status TEXT, generated_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT
        )
    """)
    # Signal CONSUMED = Trade wurde platziert; TTL laeuft noch 60 min
    d.execute("INSERT INTO signals VALUES (7, 42, 'MACD_TURN_BELOW_SMA20', "
              "'CONSUMED', datetime('now'), datetime('now','+60 minutes'))")
    # $70,11 = Ø der 198 real abgelehnten Trades
    d.execute("INSERT INTO trades VALUES (1, 'AAPL', 70.11, 'APPROVED', NULL, NULL, 7)")
    return d


def _trade(db, tid=1):
    return dict(db.fetchone("SELECT * FROM trades WHERE id=?", (tid,)))


def _status(db, sid=7):
    return db.fetchone("SELECT status FROM signals WHERE id=?", (sid,))["status"]


# ── Der gemessene Fall: REDUCE faellt unter min_buy ──────────────────────────

def test_reduce_unter_min_buy_markiert_das_signal(db):
    """25 % von $70,11 = $17,53 < min_buy $50 -> Ablehnung steht vorher fest."""
    out = _apply_decision(db, _trade(db),
                          {"decision": "REDUCE", "reduce_to_pct": 25}, 50.0)
    assert out == "VETO"
    assert db.fetchone("SELECT status FROM trades WHERE id=1")["status"] == "REJECTED"
    assert _status(db) == "REJECTED"


def test_veto_markiert_das_signal(db):
    out = _apply_decision(db, _trade(db), {"decision": "VETO", "reason": "News"}, 50.0)
    assert out == "VETO"
    assert _status(db) == "REJECTED"


# ── Die eigentliche Wirkung: die Neuerzeugung ist gesperrt ───────────────────

def test_has_fresh_signal_blockt_nach_der_ablehnung(db):
    """Das ist der Zweck der Aenderung — ohne sie feuert der data_worker neu."""
    repo = SignalRepo(db)
    assert repo.has_fresh_signal(42, 'MACD_TURN_BELOW_SMA20') is False, \
        "CONSUMED darf nicht blocken (Vorbedingung)"
    _apply_decision(db, _trade(db), {"decision": "REDUCE", "reduce_to_pct": 25}, 50.0)
    assert repo.has_fresh_signal(42, 'MACD_TURN_BELOW_SMA20') is True


def test_sperre_endet_mit_dem_signal_ttl(db):
    """Transiente Gruende heilen — dieselbe Semantik wie fix/rejected-signal-dedup."""
    db.execute("UPDATE signals SET expires_at=datetime('now','-1 minutes') WHERE id=7")
    _apply_decision(db, _trade(db), {"decision": "VETO", "reason": "x"}, 50.0)
    assert _status(db) == "REJECTED"
    assert SignalRepo(db).has_fresh_signal(42, 'MACD_TURN_BELOW_SMA20') is False


# ── Was NICHT angefasst werden darf ──────────────────────────────────────────

def test_reduce_innerhalb_der_grenzen_laesst_das_signal_in_ruhe(db):
    """75 % von $70,11 = $52,58 > min_buy: der Trade laeuft weiter."""
    out = _apply_decision(db, _trade(db),
                          {"decision": "REDUCE", "reduce_to_pct": 75}, 50.0)
    assert out == "REDUCE"
    assert _status(db) == "CONSUMED"


def test_approve_laesst_das_signal_in_ruhe(db):
    assert _apply_decision(db, _trade(db), {"decision": "APPROVE"}, 50.0) == "APPROVE"
    assert _status(db) == "CONSUMED"


def test_verlorener_race_laesst_das_signal_in_ruhe(db):
    """Der execution_worker war schneller — dann greift hier gar nichts mehr."""
    db.execute("UPDATE trades SET status='ACTIVE' WHERE id=1")
    out = _apply_decision(db, _trade(db), {"decision": "VETO", "reason": "zu spaet"}, 50.0)
    assert out == "NOOP"
    assert _status(db) == "CONSUMED"


def test_laufende_order_laesst_das_signal_in_ruhe(db):
    """fix/veto-inflight: Geld liegt beim Broker — weder Trade noch Signal."""
    db.execute("UPDATE trades SET order_id='1582187392' WHERE id=1")
    out = _apply_decision(db, _trade(db), {"decision": "VETO", "reason": "x"}, 50.0)
    assert out == "NOOP"
    assert _status(db) == "CONSUMED"


# ── Robustheit: Datensammlung darf den Veto-Lauf nie kippen ──────────────────

def test_trade_ohne_signal_id_ist_kein_fehler(db):
    db.execute("UPDATE trades SET signal_id=NULL WHERE id=1")
    out = _apply_decision(db, _trade(db), {"decision": "VETO", "reason": "x"}, 50.0)
    assert out == "VETO"
    assert db.fetchone("SELECT status FROM trades WHERE id=1")["status"] == "REJECTED"


def test_kaputte_signals_tabelle_kippt_die_ablehnung_nicht(db):
    """Die Trade-Ablehnung ist bereits geschrieben und bleibt gueltig."""
    db.execute("DROP TABLE signals")
    out = _apply_decision(db, _trade(db), {"decision": "VETO", "reason": "x"}, 50.0)
    assert out == "VETO"
    assert db.fetchone("SELECT status FROM trades WHERE id=1")["status"] == "REJECTED"
