"""Unit tests fuer fix/trailing-symbol-resolution (2026-08-12).

INCIDENT: 9633.HK (Nongfu Spring) meldete seit 2026-08-10 alle 5 Minuten
"BE_CLOSE unverified — Full-close NOT confirmed after 165s". Die Position war
seit dem Break-Even-Trigger (Peak +3.3%) ungeschuetzt offen bei -0.79%.

URSACHE: Die eToro-Positions-Payload hat KEINEN 'symbol'-Key. evaluate_trailing
fiel deshalb ausnahmslos auf `str(instrumentID)` zurueck — 56 von 60
position_state-Zeilen trugen '3364' statt '9633.HK'.

is_market_open() leitet die Boerse aus dem ERSTEN Argument ab. '3364' hatte kein
erkennbares Suffix -> _get_market_key fiel auf den Default 'US' zurueck. NICHT
auf fail-open: 'US' ist ein definierter Market-Key, der fail_open-Zweig wurde
nie erreicht. Der Guard beantwortete damit stillschweigend die falsche Frage
("ist die NYSE offen?") und war fuer JEDE Nicht-US-Position wirkungslos
(48 von 60), obwohl er genau dafuer gebaut wurde.

Belegt am Live-Fall (2026-08-10, waehrend der US-Session):
    is_market_open('3364',   '9633.HK', '') -> True   (falsch — das ist die NYSE)
    is_market_open('9633.HK','9633.HK', '') -> False  (korrekt, HK zu)

NACHTRAG 2026-08-17 — die Restluecke ist geschlossen, es gibt jetzt drei
gestaffelte Verteidigungslinien statt einer:
    1. load_symbols() loest die ID zum echten Ticker auf   (2be4a7b, hier getestet)
    2. schlaegt das fehl, liefert yfinance_symbol die Boerse nach
    3. weiss auch das nichts, gilt UNKNOWN_MARKET_KEY -> der fail_open-Schalter
       des Aufrufers entscheidet (BUY fail-closed, Exit fail-open)
Der Guard kann seither NICHT mehr still zur NYSE-Frage werden — siehe
test_market_hours_fail_closed.py fuer die Ebenen 2 und 3.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import bot.core.market_hours as mh
import bot.core.trailing_stop as ts
from bot.core.market_hours import is_market_open


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

# Montag, 2026-08-17 — beide Zeitpunkte an einem Handelstag, Sommerzeit
# (ET = UTC-4, HKT = UTC+8), damit die zoneinfo-Umrechnung eindeutig ist.
HK_ZU_US_OFFEN = datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc)   # 23:00 HKT / 11:00 ET
HK_OFFEN_US_ZU = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)    # 11:00 HKT / 23:00 ET (So)


def _uhr_stellen(monkeypatch, when_utc):
    """Friert market_hours.datetime auf einen festen UTC-Zeitpunkt ein.

    market_hours macht `from datetime import datetime`, also genuegt das
    Modul-Attribut. Nur `is_market_open` liest ueberhaupt die Uhr — alle
    internen Checks bekommen `now_utc` als Argument durchgereicht.
    """
    class _EingefroreneUhr(datetime):
        @classmethod
        def now(cls, tz=None):
            return when_utc.astimezone(tz) if tz else when_utc.replace(tzinfo=None)

    monkeypatch.setattr(mh, "datetime", _EingefroreneUhr)


def test_market_guard_folgt_der_boerse_auch_bei_roher_id(monkeypatch):
    """Regressionsschutz fuer die Ursache selbst — Stand nach 2026-08-17.

    Frueher: '3364' hat kein Suffix -> Default 'US', der Guard beantwortet
    die falsche Frage ("ist die NYSE offen?") statt der richtigen ("ist die
    HKEX offen?") — genau das liess BE_CLOSE bei geschlossener HK-Boerse
    feuern. Der Test pinnte damals dieses Fehlverhalten.

    Heute rettet der yfinance-Suffix die rohe ID: beide Varianten liefern
    dieselbe Antwort. Der Test pinnt jetzt DIESE Gleichheit — bricht sie,
    ist der Guard wieder auf die NYSE-Frage zurueckgefallen.

    Die Uhr wird eingefroren, damit das Ergebnis nicht an der Tageszeit
    haengt (ohne Freeze war der Test nur waehrend 13:30-20:00 UTC gruen).
    """
    assert mh.get_instrument_market_key("9633.HK", "9633.HK", "") == "APAC_HK_GROUP"
    assert mh.get_instrument_market_key("3364", "9633.HK", "") == "APAC_HK_GROUP", (
        "die rohe instrumentID muss ueber yfinance_symbol zur HKEX finden"
    )

    # Die Incident-Konstellation: HK zu, US offen. Frueher meldete die
    # ID-Variante hier "offen" — heute folgt sie der HKEX.
    _uhr_stellen(monkeypatch, HK_ZU_US_OFFEN)
    assert is_market_open("9633.HK", "9633.HK", "") is False
    assert is_market_open("3364", "9633.HK", "") is False, (
        "ID statt Symbol liess den Guard die NYSE fragen — 9633.HK lief so "
        "2 Tage ohne Break-Even-Schutz"
    )

    # Gegenprobe: HK offen, US zu -> beide Varianten sagen "offen"
    _uhr_stellen(monkeypatch, HK_OFFEN_US_ZU)
    assert is_market_open("9633.HK", "9633.HK", "") is True
    assert is_market_open("3364", "9633.HK", "") is True


def test_voellig_unaufloesbare_id_blockiert_den_exit_nicht(monkeypatch):
    """Dritte Verteidigungslinie — und ihre bewusste Asymmetrie.

    Ohne Ticker UND ohne yfinance_symbol ist die Boerse nicht bestimmbar
    (der Fall aus test_unbekannte_id_faellt_auf_die_id_zurueck). Dann
    entscheidet fail_open:

      Exit-Pfade (BE_CLOSE/SL/Trim) rufen mit dem Default fail_open=True auf
      und muessen das auch. Fail-closed hiesse dort: der Break-Even-Boden
      wird fuer eine Position, die wir nicht identifizieren koennen,
      dauerhaft abgeschaltet — derselbe Schaden wie im Incident, nur ueber
      einen anderen Weg. AGENTS.md: "BE_CLOSE/SL sind Verlustschutz — Retry
      NIE unterdruecken." Ein vergeblicher Close faengt das PENDING-Muster ab.

      Die BUY-Boundary ruft mit fail_open=False auf: dort kostet ein falsches
      "geschlossen" nur einen DEFER.
    """
    _uhr_stellen(monkeypatch, HK_ZU_US_OFFEN)
    assert is_market_open("9999", "", "") is True
    assert is_market_open("9999", "", "", fail_open=False) is False

    _uhr_stellen(monkeypatch, HK_OFFEN_US_ZU)
    assert is_market_open("9999", "", "") is True, (
        "der Exit-Pfad darf zu KEINER Tageszeit blockiert werden"
    )


def test_stale_hold_vergleich_funktioniert_wieder(monkeypatch):
    """Zweites Opfer derselben Ursache: `symbol in fresh_holds` traf nie,
    weil _load_fresh_llm_holds echte Ticker liefert ('9633.HK'), das
    Symbol aber '3364' war — die LLM-HOLD-Schonfrist war tot."""
    monkeypatch.setattr(ts, "_action_market_open", lambda db, a: True)
    monkeypatch.setattr(ts, "_load_fresh_llm_holds", lambda h: {"9633.HK"})
    db = _DB({3364: "9633.HK"})
    actions = ts.evaluate_trailing([_pos(3364, 4.0)], db=db)
    assert all(a.symbol == "9633.HK" for a in actions)
