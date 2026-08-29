"""Signalbericht nur bei Aenderung posten (2026-08-29).

Bei 15-Minuten-Takt waeren "immer posten" 96 Meldungen taeglich; nachts und
am Wochenende aendert sich zwischen den Laeufen meist nichts.
"""
from datetime import datetime, timedelta, timezone

from bot.workers.signal_worker import _report_fingerprint, _post_report_if_changed


BERICHT = [
    {"symbol": "MUX.DE", "signal_type": "MACD_TURN_BELOW_SMA20",
     "conviction": "MEDIUM", "score": 35, "direction": "BUY",
     "outcome": "markt_geschlossen"},
    {"symbol": "VET", "signal_type": "BB_UPPER_RSI_OVERBOUGHT",
     "conviction": "HIGH", "score": 30, "direction": "SELL",
     "outcome": "Verkaufssignal — kein Kaufkandidat"},
]


class _State:
    """Minimaler state_repo-Ersatz."""

    def __init__(self, **vals):
        self.v = dict(vals)
        self.sets = []

    def get(self, k):
        return self.v.get(k)

    def set(self, k, val):
        self.v[k] = val
        self.sets.append(k)


def _kw(**over):
    kw = dict(
        approved_trades=[], regime="DEFENSIVE", risk_scalar=0.5,
        evaluated_count=0, equity=8356.85, cash=7371.38,
        total_exposure=983.37, position_count=14, signal_report=BERICHT,
    )
    kw.update(over)
    return kw


# ─── Der Kern: was zaehlt als Aenderung? ────────────────────────────────────

def test_marktbewegung_aendert_den_abdruck_nicht():
    """DIE Design-Entscheidung.

    equity/cash/exposure bewegen sich in JEDEM Zyklus. Fliessen sie in den
    Abdruck ein, ist jeder Lauf "veraendert" und die Drossel wirkungslos.
    """
    a = _report_fingerprint("DEFENSIVE", [], BERICHT)
    b = _report_fingerprint("DEFENSIVE", [], BERICHT)
    assert a == b


def test_neues_signal_aendert_den_abdruck():
    mehr = BERICHT + [{"symbol": "BTC-USD", "signal_type": "GOLDEN_CROSS",
                       "conviction": "HIGH", "score": 40, "direction": "BUY",
                       "outcome": "genehmigt"}]
    assert _report_fingerprint("DEFENSIVE", [], BERICHT) != \
        _report_fingerprint("DEFENSIVE", [], mehr)


def test_geaendertes_ergebnis_aendert_den_abdruck():
    """Gleiches Signal, anderer Ausgang — das MUSS sichtbar werden."""
    anders = [dict(BERICHT[0], outcome="genehmigt"), BERICHT[1]]
    assert _report_fingerprint("DEFENSIVE", [], BERICHT) != \
        _report_fingerprint("DEFENSIVE", [], anders)


def test_regimewechsel_aendert_den_abdruck():
    assert _report_fingerprint("DEFENSIVE", [], BERICHT) != \
        _report_fingerprint("CAUTION", [], BERICHT)


def test_reihenfolge_ist_egal():
    """Sortierung im Bericht darf keinen Fehlalarm ausloesen."""
    assert _report_fingerprint("DEFENSIVE", [], BERICHT) == \
        _report_fingerprint("DEFENSIVE", [], list(reversed(BERICHT)))


def test_neuer_trade_aendert_den_abdruck():
    t = [{"symbol": "BTC-USD", "amount_usd": 250.71}]
    assert _report_fingerprint("DEFENSIVE", [], BERICHT) != \
        _report_fingerprint("DEFENSIVE", t, BERICHT)


# ─── Verhalten der Drossel ──────────────────────────────────────────────────

def test_erster_lauf_postet(monkeypatch):
    gepostet = []
    monkeypatch.setattr("bot.workers.signal_worker._post",
                        lambda *a, **k: gepostet.append(k))
    st = _State()
    assert _post_report_if_changed(st, **_kw()) is True
    assert len(gepostet) == 1
    assert "SIGNAL_REPORT_FP" in st.sets


def test_unveraenderter_folgelauf_schweigt(monkeypatch):
    gepostet = []
    monkeypatch.setattr("bot.workers.signal_worker._post",
                        lambda *a, **k: gepostet.append(k))
    fp = _report_fingerprint("DEFENSIVE", [], BERICHT)
    st = _State(SIGNAL_REPORT_FP=fp,
                SIGNAL_REPORT_POSTED_AT=datetime.now(timezone.utc).isoformat())
    assert _post_report_if_changed(st, **_kw()) is False
    assert gepostet == []


def test_lebenszeichen_nach_der_stillefrist(monkeypatch):
    """Stille darf nicht mit "Worker tot" verwechselbar sein."""
    gepostet = []
    monkeypatch.setattr("bot.workers.signal_worker._post",
                        lambda *a, **k: gepostet.append(k))
    fp = _report_fingerprint("DEFENSIVE", [], BERICHT)
    alt = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    st = _State(SIGNAL_REPORT_FP=fp, SIGNAL_REPORT_POSTED_AT=alt)
    assert _post_report_if_changed(st, max_silence_h=6.0, **_kw()) is True
    assert len(gepostet) == 1


def test_kaputter_zustandsspeicher_postet_statt_zu_schweigen(monkeypatch):
    """Fail-open: lieber eine Meldung zu viel als eine verschluckte."""
    gepostet = []
    monkeypatch.setattr("bot.workers.signal_worker._post",
                        lambda *a, **k: gepostet.append(k))

    class _Kaputt:
        def get(self, k):
            raise RuntimeError("DB weg")

        def set(self, k, v):
            raise RuntimeError("DB weg")

    assert _post_report_if_changed(_Kaputt(), **_kw()) is True
    assert len(gepostet) == 1
