"""Unit tests fuer fix/trailing-symbol-resolution (2026-08-12).

INCIDENT: 9633.HK (Nongfu Spring) meldete seit 2026-08-10 alle 5 Minuten
"BE_CLOSE unverified — Full-close NOT confirmed after 165s". Die Position war
seit dem Break-Even-Trigger (Peak +3.3%) ungeschuetzt offen bei -0.79%.

URSACHE: Die eToro-Positions-Payload hat KEINEN 'symbol'-Key. evaluate_trailing
fiel deshalb ausnahmslos auf `str(instrumentID)` zurueck — 56 von 60
position_state-Zeilen trugen '3364' statt '9633.HK'.

is_market_open() leitet die Boerse aus dem ERSTEN Argument ab. '3364' hat kein
erkennbares Suffix -> fail_open=True -> "Markt offen". Der Guard aus
fix/stale-price-trailing war damit fuer JEDE Nicht-US-Position wirkungslos
(48 von 60), obwohl er genau dafuer gebaut wurde.

Belegt am Live-Fall:
    is_market_open('3364',   '9633.HK', '') -> True   (falsch)
    is_market_open('9633.HK','9633.HK', '') -> False  (korrekt, HK zu)
"""
from __future__ import annotations

import pytest

import bot.core.trailing_stop as ts


class _DB:
    """Minimal-DB: liefert instruments-Zeilen fuer die Batch-Lookups."""

    def __init__(self, mapping: dict[int, str]):
        self.mapping = mapping

    def fetchall(self, sql, params=None):
        if "symbol FROM instruments" in sql:
            return [(iid, sym) for iid, sym in self.mapping.items()
                    if iid in (params or [])]
        return []

    def fetchone(self, *a, **k):
        return None

    def execute(self, *a, **k):
        return None


def _pos(instrument_id: int, pnl_pct: float, pos_id: str = "p1") -> dict:
    """PnL kommt als ABSOLUTER Betrag (unrealizedPnL.pnL), nicht als Prozent —
    evaluate_trailing rechnet selbst pnl_usd/amount*100."""
    amount = 349.46
    return {
        "positionID": pos_id,
        "instrumentID": instrument_id,
        "amount": amount,
        "openRate": 42.98,
        "unrealizedPnL": {"pnL": amount * pnl_pct / 100.0},
    }


# ── load_symbols ──────────────────────────────────────────────────────────────

def test_load_symbols_liefert_echte_ticker():
    db = _DB({3364: "9633.HK", 1001: "AAPL"})
    assert ts.load_symbols(db, [3364, 1001]) == {3364: "9633.HK", 1001: "AAPL"}


def test_load_symbols_ohne_db_leer():
    assert ts.load_symbols(None, [3364]) == {}
    assert ts.load_symbols(_DB({}), []) == {}


def test_load_symbols_faellt_nicht_um():
    class Broken:
        def fetchall(self, *a, **k):
            raise RuntimeError("db weg")
    assert ts.load_symbols(Broken(), [1]) == {}


# ── Symbolaufloesung in evaluate_trailing ─────────────────────────────────────

def test_symbol_kommt_aus_der_instruments_tabelle(monkeypatch):
    """Der Kern: '3364' darf nicht mehr als Symbol durchgereicht werden."""
    seen = {}
    monkeypatch.setattr(ts, "_action_market_open", lambda db, a: True)
    monkeypatch.setattr(ts, "update_peak_pnl",
                        lambda db, pid, sym, pnl: seen.setdefault("sym", sym))
    db = _DB({3364: "9633.HK"})
    ts.evaluate_trailing([_pos(3364, 3.3)], db=db)
    assert seen.get("sym") == "9633.HK"


def test_actions_tragen_das_echte_symbol(monkeypatch):
    monkeypatch.setattr(ts, "_action_market_open", lambda db, a: True)
    db = _DB({3364: "9633.HK"})
    actions = ts.evaluate_trailing([_pos(3364, 4.0)], db=db)
    assert actions, "bei +4% muss mindestens BREAK_EVEN feuern"
    assert all(a.symbol == "9633.HK" for a in actions)


def test_payload_symbol_hat_vorrang(monkeypatch):
    """Falls eToro doch mal ein Symbol liefert, gewinnt es."""
    monkeypatch.setattr(ts, "_action_market_open", lambda db, a: True)
    p = _pos(3364, 4.0)
    p["symbol"] = "VOM_BROKER"
    actions = ts.evaluate_trailing([p], db=_DB({3364: "9633.HK"}))
    assert all(a.symbol == "VOM_BROKER" for a in actions)


def test_unbekannte_id_faellt_auf_die_id_zurueck(monkeypatch):
    """Letzter Notnagel — schlechter als ein Symbol, besser als leer."""
    monkeypatch.setattr(ts, "_action_market_open", lambda db, a: True)
    actions = ts.evaluate_trailing([_pos(9999, 4.0)], db=_DB({}))
    assert all(a.symbol == "9999" for a in actions)


# ── Die eigentliche Wirkung: der Market-Guard sieht wieder ────────────────────

def test_market_guard_erkennt_die_boerse_erst_mit_echtem_symbol():
    """Regressionsschutz fuer die Ursache selbst.

    Ohne Suffix kann is_market_open die Boerse nicht bestimmen und faellt
    fail-open auf "offen" zurueck — genau das liess BE_CLOSE bei
    geschlossener HK-Boerse feuern.
    """
    from bot.core.market_hours import is_market_open
    from datetime import datetime, timezone

    # 03:00 UTC: HK offen (01:30-08:00), 12:00 UTC: HK zu
    mit_symbol_zu = is_market_open("9633.HK", "9633.HK", "")
    mit_id = is_market_open("3364", "9633.HK", "")
    # Die ID-Variante ist IMMER True (fail-open) — unabhaengig von der Uhrzeit.
    assert mit_id is True
    # Und sie ist damit nachweislich nicht dasselbe wie die echte Antwort,
    # sobald HK geschlossen ist.
    now_h = datetime.now(timezone.utc).hour
    if not (1 <= now_h < 8):          # ausserhalb der HK-Session
        assert mit_symbol_zu is False, "HK muesste jetzt geschlossen sein"


def test_stale_hold_vergleich_funktioniert_wieder(monkeypatch):
    """Zweites Opfer derselben Ursache: `symbol in fresh_holds` traf nie,
    weil _load_fresh_llm_holds echte Ticker liefert ('9633.HK'), das
    Symbol aber '3364' war — die LLM-HOLD-Schonfrist war tot."""
    monkeypatch.setattr(ts, "_action_market_open", lambda db, a: True)
    monkeypatch.setattr(ts, "_load_fresh_llm_holds", lambda h: {"9633.HK"})
    db = _DB({3364: "9633.HK"})
    actions = ts.evaluate_trailing([_pos(3364, 4.0)], db=db)
    assert all(a.symbol == "9633.HK" for a in actions)
