"""Unit tests fuer fix/profit-ladder-reachability (2026-08-12).

Die ATR-Profit-Leiter feuerte praktisch nie: erste Stufe (ATRx6, min 6%) lag
im Median bei +17.1%, erreicht hatten sie 2 von 59 offenen Positionen. Der
durchschnittliche GEWINN-Trade der 220 geschlossenen liegt bei +3.49%.
Gewinner endeten damit ausschliesslich an Break-Even-Stop (+0.3%) und
Momentum-Fade — zwischen +3.5% und +17% gab es keinen Mechanismus.

PROFIT_LADDER_ATR_SCALE skaliert atr_mult UND min_pct aller Stufen.
"""
from __future__ import annotations

import pytest

import bot.core.trailing_stop as ts
from bot.core.trailing_stop import _resolve_profit_levels


@pytest.fixture(autouse=True)
def _reset_scale():
    """Modul-Global sichern — apply_config anderer Tests leckt sonst."""
    before = ts.PROFIT_LADDER_ATR_SCALE
    yield
    ts.PROFIT_LADDER_ATR_SCALE = before


def _thresholds(atr_pct: float) -> list[float]:
    return [lv["threshold"] for lv in _resolve_profit_levels(atr_pct)]


def test_scale_1_ist_der_historische_stand():
    """Konservativer Code-Default: ohne Config aendert sich nichts."""
    ts.PROFIT_LADDER_ATR_SCALE = 1.0
    # ATR 2.89% (Ø des realen Buchs) → x6 = 17.34
    assert _thresholds(2.89)[0] == pytest.approx(17.34, abs=0.01)


def test_scale_senkt_alle_stufen():
    ts.PROFIT_LADDER_ATR_SCALE = 0.35
    t = _thresholds(2.89)
    assert t[0] == pytest.approx(6.07, abs=0.05)
    assert t[1] == pytest.approx(10.12, abs=0.05)
    assert t[2] == pytest.approx(18.21, abs=0.05)


def test_min_pct_wird_mitskaliert():
    """Ohne Skalierung von min_pct klemmt es die Aenderung weg.

    Bei ATR 1.1% war min_pct=6.0 die bindende Grenze — eine kleinere
    atr_mult haette gar nichts bewirkt.
    """
    ts.PROFIT_LADDER_ATR_SCALE = 0.35
    first = _thresholds(1.1)[0]
    assert first < 6.0, "min_pct muss mitskalieren, sonst bleibt die Klemme bei 6%"
    assert first == pytest.approx(2.31, abs=0.05)   # max(6*0.35*1.1, 6*0.35)


def test_max_pct_wird_nicht_mitskaliert():
    """Die Obergrenze ist eine Sicherheitsklemme — sie bleibt, wo sie ist.

    Sie tiefer zu ziehen wuerde High-ATR-Titel zu frueh zwangsschliessen.
    """
    ts.PROFIT_LADDER_ATR_SCALE = 1.0
    assert _thresholds(20.0)[0] == 30.0     # bleibt bei max_pct
    ts.PROFIT_LADDER_ATR_SCALE = 0.35
    assert _thresholds(60.0)[0] == 30.0     # immer noch 30, nicht 10.5


def test_leiter_bleibt_aufsteigend():
    for scale in (1.0, 0.5, 0.35, 0.2):
        ts.PROFIT_LADDER_ATR_SCALE = scale
        t = _thresholds(2.89)
        assert t == sorted(t), f"Leiter nicht aufsteigend bei scale={scale}"


def test_close_pct_bleibt_unveraendert():
    """Nur die Schwellen wandern — WIE VIEL realisiert wird, bleibt gleich."""
    ts.PROFIT_LADDER_ATR_SCALE = 0.35
    assert [lv["close_pct"] for lv in _resolve_profit_levels(2.89)] == [20, 20, 30]


def test_fallback_leiter_ohne_atr_ist_unberuehrt():
    """Ohne ATR gilt die flache Leiter (+7/15/25/50) — die skaliert nicht."""
    ts.PROFIT_LADDER_ATR_SCALE = 0.35
    assert _thresholds(None)[0] == 7.0
    assert _thresholds(0.0)[0] == 7.0


# ── apply_config ──────────────────────────────────────────────────────────────

def test_apply_config_liest_atr_scale():
    ts.apply_config({"trailing": {"profit_ladder": {"atr_scale": 0.35}}})
    assert ts.PROFIT_LADDER_ATR_SCALE == pytest.approx(0.35)


def test_apply_config_klemmt_null_und_negativ():
    """0 wuerde die Leiter auf 0% setzen und jede Position sofort schliessen."""
    ts.apply_config({"trailing": {"profit_ladder": {"atr_scale": 0.0}}})
    assert ts.PROFIT_LADDER_ATR_SCALE >= 0.05
    ts.apply_config({"trailing": {"profit_ladder": {"atr_scale": -5.0}}})
    assert ts.PROFIT_LADDER_ATR_SCALE >= 0.05


def test_apply_config_klemmt_ueber_eins():
    ts.apply_config({"trailing": {"profit_ladder": {"atr_scale": 99.0}}})
    assert ts.PROFIT_LADDER_ATR_SCALE == 1.0


def test_apply_config_ohne_block_laesst_wert_stehen():
    ts.PROFIT_LADDER_ATR_SCALE = 0.35
    ts.apply_config({"trailing": {}})
    assert ts.PROFIT_LADDER_ATR_SCALE == pytest.approx(0.35)


def test_apply_config_ignoriert_muell():
    ts.PROFIT_LADDER_ATR_SCALE = 0.35
    ts.apply_config({"trailing": {"profit_ladder": {"atr_scale": "abc"}}})
    assert ts.PROFIT_LADDER_ATR_SCALE == pytest.approx(0.35)


def test_scale_ist_in_bible_hard_limits():
    """Der LLM-Review-Worker darf den Faktor nachjustieren — in Grenzen."""
    from bot.workers.llm_review_worker import BIBLE_HARD_LIMITS
    lo, hi, typ = BIBLE_HARD_LIMITS["trailing.profit_ladder.atr_scale"]
    assert (lo, hi, typ) == (0.2, 1.0, float)


# ── Mini-Teilverkauf-Schutz ───────────────────────────────────────────────────

class _FakeClient:
    """Zaehlt close_position-Aufrufe — ein Mini-Verkauf darf keinen ausloesen."""
    def __init__(self):
        self.calls = []

    def get_position_units(self, _pid):
        return 100.0

    def close_position(self, **kw):
        self.calls.append(kw)
        return {"ok": True}


class _FakeDB:
    def __init__(self):
        self.marked = []

    def execute(self, *a, **k):
        self.marked.append(a)
        return None

    def fetchone(self, *a, **k):
        return None

    def fetchall(self, *a, **k):
        return []


def _action(amount_usd: float, close_pct: float = 20.0):
    return ts.TrailingAction(
        action="PARTIAL_CLOSE", symbol="TINY", position_id="p1",
        pnl_pct=9.0, reason="test", close_pct=close_pct,
        instrument_id=1, amount_usd=amount_usd, open_rate=10.0,
        level_threshold=6.07,
    )


@pytest.fixture
def _market_open(monkeypatch):
    """Markt offen + Discord STUMM.

    KRITISCH: execute_trailing_actions postet nach einem erfolgreichen
    (Teil-)Close ein echtes Discord-Embed in den LIVE-Channel #trades.
    Ohne diesen Patch schickt jeder Testlauf eine erfundene Position
    ("TINY $35.00") an den echten Server — beim Bau dieses Tests sind so
    7 Fake-Meldungen rausgegangen. Jeder Test, der execute_trailing_actions
    mit einem erfolgreichen Close durchlaeuft, MUSS Discord stummschalten.
    """
    monkeypatch.setattr(ts, "_action_market_open", lambda *a, **k: True)
    monkeypatch.setattr(ts, "_post_closed_embed", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_get_discord_embeds", lambda: None)
    monkeypatch.setattr(ts, "_verify_partial_close",
                        lambda *a, **k: (True, "test: verified"))


def test_mini_teilverkauf_sendet_keine_order(_market_open):
    """$9 Position x 20% = $1.80 — darunter wird keine Order gesendet."""
    ts.MIN_PARTIAL_CLOSE_USD = 10.0
    client = _FakeClient()
    stats = ts.execute_trailing_actions(client, [_action(9.0)], db=_FakeDB())
    assert client.calls == []
    assert stats["partial_closes"] == 0
    assert stats["errors"] == []          # kein Fehler — bewusst uebersprungen


def test_mini_teilverkauf_bucht_level_als_genommen(_market_open, monkeypatch):
    """Ohne diese Buchung feuert die Stufe alle 5 Minuten endlos neu."""
    ts.MIN_PARTIAL_CLOSE_USD = 10.0
    taken = []
    monkeypatch.setattr(ts, "mark_level_taken",
                        lambda db, pid, sym, thr: taken.append((pid, thr)))
    ts.execute_trailing_actions(_FakeClient(), [_action(9.0)], db=_FakeDB())
    assert taken == [("p1", 6.07)]


def test_ausreichend_grosser_teilverkauf_geht_durch(_market_open):
    """$175 x 20% = $35 — deutlich ueber dem Minimum."""
    ts.MIN_PARTIAL_CLOSE_USD = 10.0
    client = _FakeClient()
    ts.execute_trailing_actions(client, [_action(175.0)], db=_FakeDB())
    assert len(client.calls) == 1
    assert client.calls[0]["units_to_deduct"] == pytest.approx(20.0)


def test_apply_config_liest_min_partial_close():
    ts.apply_config({"trailing": {"min_partial_close_usd": 25.0}})
    assert ts.MIN_PARTIAL_CLOSE_USD == pytest.approx(25.0)
