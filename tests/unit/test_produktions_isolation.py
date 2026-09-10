#!/usr/bin/env python3
"""fix/tests-schreiben-in-produktion (2026-09-10).

Zwischen dem 2026-09-05 und dem 2026-09-10 gingen 526 Close-Embeds fuer
CAR.AX nach #etoro-trades und ebenso viele Zeilen in die Produktions-
`system_log` — nicht vom Bot, sondern aus der Testsuite. Ein Lauf von
`test_llm_tighten_remaining.py` erzeugte reproduzierbar exakt 15 davon
(gemessen: 526 -> 541), und die Datei brauchte 10,6 s statt 0,12 s, weil
jeder Post ein echter HTTP-Request war.

Ursache: eine Fixture, die `LE._post_closed_embed` stumm schalten wollte —
einen Namen, den `llm_execution` nicht hat. Das `if hasattr(...)` drumherum
verschluckte den Fehlgriff, die Fixture SAH aus wie eine Absicherung.

Diese Tests halten die conftest-Absicherung fest, die nicht davon abhaengt,
dass jeder kuenftige Test an den richtigen Namen denkt.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import bot.discord_embeds as DE

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_DB = PROJECT_ROOT / "data" / "trading.db"


def _live_zeilen() -> int | None:
    if not LIVE_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True, timeout=3)
        n = con.execute("SELECT COUNT(*) FROM system_log").fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return None


def test_ohne_token_keine_verbindung():
    """Der Netzpfad endet vor dem Socket — die Logik darueber bleibt echt."""
    assert DE._read_token() is None
    status, body = DE._request_discord("POST", "/x", b"{}", "application/json")
    assert status == 0 and "DISCORD_BOT_TOKEN" in body
    assert DE._post_embed({"title": "t"}, "1") is False


def test_eigener_token_gewinnt(monkeypatch):
    """Tests, die einen Post brauchen, setzen den Token selbst."""
    monkeypatch.setattr(DE, "_read_token", lambda: "test-token")
    assert DE._read_token() == "test-token"


def test_insert_system_log_schreibt_nicht_in_produktion():
    """Der Pfad, ueber den die 526 Zeilen kamen."""
    vorher = _live_zeilen()
    DE.post_position_closed_embed(
        symbol="CAR.AX", amount_usd=0.0, position_id="3523612825",
        pnl_usd=None, pnl_pct=None, reason="Isolationstest")
    nachher = _live_zeilen()
    if vorher is not None and nachher is not None:
        assert nachher == vorher, (
            f"Testlauf hat {nachher - vorher} Zeile(n) in die Produktions-DB "
            f"geschrieben — die conftest-Absicherung greift nicht.")


def test_eigener_patch_gewinnt_ueber_die_absicherung(monkeypatch):
    """Wer Discord-Verhalten pruefen will, patcht selbst — das muss gehen."""
    gesehen: list[str] = []
    monkeypatch.setattr(DE, "_read_token", lambda: "test-token")
    monkeypatch.setattr(DE, "_request_discord",
                        lambda *a, **k: (200, '{"id": "1"}'))
    monkeypatch.setattr(DE, "insert_system_log",
                        lambda lvl, src, msg: gesehen.append(msg))
    DE.post_position_closed_embed(
        symbol="SPY", amount_usd=100.0, position_id="1",
        pnl_usd=1.0, pnl_pct=1.0, reason="Test")
    assert len(gesehen) == 1 and "SPY" in gesehen[0]
