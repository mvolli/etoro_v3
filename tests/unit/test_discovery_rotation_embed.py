"""Rotations-Zeile im Discovery-Embed (feat/rotation-embed).

Die Raeumung veralteter Watchlist-Plaetze lief unsichtbar: Der Embed meldete
nur gefundene Kandidaten. Ohne diese Zeile laesst sich nicht beurteilen, ob die
Rotation arbeitet — und sie tat es monatelang nicht (fix/rotation-usefulness).
"""
import pathlib

import pytest

from bot import discord_embeds as de


KANDIDAT = {"symbol": "AAPL", "score": 42.0, "conviction": "MEDIUM",
            "rsi": 28.0, "price": 190.0, "signal_types": ["MACD_TURN_BELOW_SMA20"]}


@pytest.fixture
def gefangen(monkeypatch):
    """Faengt den Embed ab, statt ihn zu posten."""
    box = {}

    def _fake(embed, channel, dry_run=False):
        box["embed"] = embed
        box["channel"] = channel
        return "1234"

    monkeypatch.setattr(de, "_post_embed", _fake)
    monkeypatch.setattr(de, "insert_system_log", lambda *a, **k: None)
    return box


def _feld(embed, name_teil):
    return next((f for f in embed["fields"] if name_teil in f["name"]), None)


def test_rotationszeile_erscheint(gefangen):
    de.post_discovery_embed([KANDIDAT], scanned=277, evicted=15, slots_used=205)
    f = _feld(gefangen["embed"], "Rotation")
    assert f is not None, "Rotations-Feld fehlt"
    assert "15" in f["value"] and "205" in f["value"]


def test_ohne_rotation_kein_feld(gefangen):
    """Kein Rauschen, wenn nichts geraeumt wurde und keine Zahl vorliegt."""
    de.post_discovery_embed([KANDIDAT], scanned=277)
    assert _feld(gefangen["embed"], "Rotation") is None


def test_belegung_allein_reicht(gefangen):
    """Auch ohne Raeumung ist die Belegung eine nuetzliche Angabe."""
    de.post_discovery_embed([KANDIDAT], scanned=277, evicted=0, slots_used=205)
    f = _feld(gefangen["embed"], "Rotation")
    assert f is not None
    assert "205" in f["value"]
    assert "geräumt" not in f["value"]


def test_worker_reicht_die_zaehler_durch():
    """Die Zahlen muessen den Weg vom Worker in den Embed finden."""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src" / "bot" / "workers" / "discovery_worker.py").read_text(encoding="utf-8")
    assert "slots_used = int(_row[" in src, "Belegung wird nicht ermittelt"
    assert "evicted=evicted" in src, "evicted wird nicht durchgereicht"
    assert "slots_used=slots_used" in src
