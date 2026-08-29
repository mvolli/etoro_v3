"""Signalbericht fuer den Discord-Embed (feat/signal-report 2026-08-29).

Der Embed soll auf einen Blick zeigen, WELCHE Signale ein Lauf gesehen hat.
Die Richtung muss aus dem signal_type kommen — die signals-Tabelle mischt Kauf
und Verkauf, und diese Vermischung hat am 2026-08-29 eine Diagnose
fehlgeleitet: 463 von 465 vermeintlich "verfallenen" Kryptosignalen waren
BB_UPPER_RSI_OVERBOUGHT, also SELL.
"""
import pytest

from bot.workers.signal_worker import _build_signal_report


class _FakeDB:
    """Liefert Symbole wie instruments; sonst nichts."""

    def __init__(self, mapping):
        self._m = mapping

    def fetchall(self, sql, params=None):
        return [{"instrument_id": k, "symbol": v} for k, v in self._m.items()]


def _sig(iid, stype, score=30.0, conv="MEDIUM", sid=None):
    return {"id": sid or iid, "instrument_id": iid, "signal_type": stype,
            "score": score, "conviction": conv}


DB = _FakeDB({1: "BTC-USD", 2: "AAPL", 3: "VET", 4: "MUX.DE"})


@pytest.mark.parametrize("stype,erwartet", [
    ("BB_UPPER_RSI_OVERBOUGHT", "SELL"),
    ("TREND_KIPP_1H,SELL", "SELL"),
    ("rsi_extreme_overbought", "SELL"),          # Kleinschreibung
    ("MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING", "BUY"),
    ("TREND_PULLBACK,GOLDEN_CROSS", "BUY"),
    ("RSI_EXTREME_OVERSOLD,MACD_TURN_BELOW_SMA20", "BUY"),   # OVERSOLD != OVERBOUGHT
])
def test_richtung_kommt_aus_dem_signaltyp(stype, erwartet):
    rows = _build_signal_report(DB, [_sig(1, stype)])
    assert rows[0]["direction"] == erwartet


def test_oversold_wird_nicht_als_sell_gelesen():
    """Der Teilstring-Fall, an dem eine naive Pruefung scheitert."""
    rows = _build_signal_report(DB, [_sig(1, "RSI_EXTREME_OVERSOLD")])
    assert rows[0]["direction"] == "BUY"


def test_mehrfachsignale_je_symbol_werden_verdichtet():
    """Ungefiltert stand ein Instrument sechsmal untereinander."""
    sigs = [_sig(1, "TREND_PULLBACK,GOLDEN_CROSS", score=s, sid=100 + i)
            for i, s in enumerate((10.0, 33.0, 20.0))]
    rows = _build_signal_report(DB, sigs)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC-USD x3"       # Anzahl bleibt sichtbar
    assert float(rows[0]["score"]) == 33.0          # staerkstes gewinnt


def test_kauf_vor_verkauf_und_nach_score():
    rows = _build_signal_report(DB, [
        _sig(3, "BB_UPPER_RSI_OVERBOUGHT", score=99.0),
        _sig(1, "TREND_PULLBACK,GOLDEN_CROSS", score=10.0),
        _sig(2, "MACD_TURN_BELOW_SMA20", score=40.0),
    ])
    assert [r["direction"] for r in rows] == ["BUY", "BUY", "SELL"]
    assert [r["symbol"] for r in rows] == ["AAPL", "BTC-USD", "VET"]


def test_ergebnis_wird_zugeordnet():
    rows = _build_signal_report(
        DB,
        [_sig(1, "MACD_TURN_BELOW_SMA20"), _sig(2, "TREND_PULLBACK"),
         _sig(4, "GOLDEN_CROSS")],
        approved_syms={"BTC-USD"},
        blocked_reasons=["AAPL: $23.14 < SIGNAL_FLOOR $100"],
        skip_map={"markt_geschlossen": ["MUX.DE[]"]},
    )
    nach_sym = {r["symbol"]: r["outcome"] for r in rows}
    assert nach_sym["BTC-USD"] == "genehmigt"
    assert "SIGNAL_FLOOR" in nach_sym["AAPL"]
    assert nach_sym["MUX.DE"] == "markt_geschlossen"   # Klammer-Suffix ignoriert


def test_verkaufssignal_bekommt_immer_seinen_eigenen_hinweis():
    """Auch wenn das Symbol in blocked_reasons steht: SELL ist kein Kandidat."""
    rows = _build_signal_report(
        DB, [_sig(3, "BB_UPPER_RSI_OVERBOUGHT")],
        blocked_reasons=["VET: irgendwas"],
    )
    assert "Verkaufssignal" in rows[0]["outcome"]


def test_leere_eingabe_ist_kein_fehler():
    assert _build_signal_report(DB, []) == []
