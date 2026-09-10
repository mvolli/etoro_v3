#!/usr/bin/env python3
"""diag/close-embed-caller (2026-09-10).

511 Close-Embeds fuer CAR.AX zwischen dem 2026-08-28 02:31:45 und dem
2026-09-10 21:33, immer exakt 15 am Stueck, alle "$0.00 PnL=folgt" —
waehrend CAR.AX genau EIN CLOSE-Event vom 28.08. hat und heute Abend kein
einziger CAR.AX-Trade geschlossen wurde.

Statisch nicht aufloesbar: alle 12 Aufrufstellen von
post_position_closed_embed() rufen record_posted_event(), das aber
dedupliziert, sobald die Position bereits ein CLOSE-Event traegt. In
trade_events steht deshalb keine Spur, die den Verursacher nennt.

`amount_usd == 0` ist die Signatur — ein echter Close traegt immer einen
Betrag. Nur dann wird der Aufrufer ins system_log geschrieben.
"""
from __future__ import annotations

import pytest

import bot.discord_embeds as DE


@pytest.fixture
def erfasst(monkeypatch):
    zeilen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(DE, "_post_embed", lambda *a, **k: "msg-id-1")
    monkeypatch.setattr(DE, "insert_system_log",
                        lambda lvl, src, msg: zeilen.append((lvl, src, msg)))
    return zeilen


def test_null_betrag_nennt_den_aufrufer(erfasst):
    """Die Anomalie-Signatur: der Aufrufer landet im Log."""
    def _verdaechtiger_aufrufer():
        return DE.post_position_closed_embed(
            symbol="CAR.AX", amount_usd=0.0, position_id="3523612825",
            pnl_usd=None, pnl_pct=None, reason="Test")
    _verdaechtiger_aufrufer()

    assert len(erfasst) == 1
    msg = erfasst[0][2]
    assert "CAR.AX" in msg and "$0.00" in msg and "PnL=folgt" in msg
    assert "[Aufrufer:" in msg
    assert "_verdaechtiger_aufrufer" in msg
    assert "test_close_embed_caller_diag.py" in msg


def test_echter_close_bleibt_unveraendert(erfasst):
    """Normalbetrieb: kein Aufrufer-Anhang, kein zusaetzliches Rauschen."""
    DE.post_position_closed_embed(
        symbol="SPY", amount_usd=175.19, position_id="1",
        pnl_usd=4.21, pnl_pct=2.4, reason="Test")

    assert len(erfasst) == 1
    msg = erfasst[0][2]
    assert "[Aufrufer:" not in msg
    assert "SPY $175.19 PnL=$+4.21" in msg


def test_diagnose_bricht_den_post_nicht(erfasst, monkeypatch):
    """Fail-open: eine kaputte Stack-Auswertung darf den Embed nicht kosten."""
    import traceback
    monkeypatch.setattr(traceback, "extract_stack",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaputt")))
    ok = DE.post_position_closed_embed(
        symbol="CAR.AX", amount_usd=0.0, position_id="x",
        pnl_usd=None, pnl_pct=None, reason="Test")
    assert ok == "msg-id-1"
    assert len(erfasst) == 1
    assert "[Aufrufer:" not in erfasst[0][2]
