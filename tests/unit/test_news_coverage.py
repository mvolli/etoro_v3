"""Unit tests fuer fix/news-coverage (2026-08-12).

`symbols[:CAP]` schnitt hart ab: bei 54 Live-Symbolen gegen
EARNINGS_SYMBOL_CAP=12 blieben 42 offene Positionen ungeprueft. Earnings sind
der teuerste blinde Fleck dieses Bots — ein Termin ist ein Gap-Risiko, gegen
das der Software-Trailing-Stop (eToro hat keinen SL-Update-Endpoint) nicht
schuetzt.

Der Cap begrenzt jetzt nur noch den KANDIDATEN-Schwanz.
"""
from __future__ import annotations

from bot.workers.news_flags_worker import _capped


def _syms(n_held: int, n_cand: int) -> list[dict]:
    return (
        [{"symbol": f"H{i}", "yf": f"H{i}", "held": True} for i in range(n_held)]
        + [{"symbol": f"C{i}", "yf": f"C{i}", "held": False} for i in range(n_cand)]
    )


def test_alle_gehaltenen_positionen_werden_geprueft():
    """Der Kern: 54 Positionen gegen Cap 12 — keine darf durchfallen."""
    out = _capped(_syms(54, 30), cap=12)
    held_out = [s for s in out if s["held"]]
    assert len(held_out) == 54


def test_kandidaten_werden_gedeckelt():
    out = _capped(_syms(5, 100), cap=20)
    assert len(out) == 20
    assert sum(1 for s in out if not s["held"]) == 15


def test_positionen_stehen_vorn():
    out = _capped(_syms(3, 10), cap=6)
    assert [s["held"] for s in out[:3]] == [True, True, True]


def test_ohne_positionen_gilt_der_cap_normal():
    out = _capped(_syms(0, 50), cap=12)
    assert len(out) == 12


def test_positionen_ueber_cap_verdraengen_kandidaten_ganz():
    """Budget = max(cap, n_held) — Kandidaten bekommen dann nichts."""
    out = _capped(_syms(30, 20), cap=12)
    assert len(out) == 30
    assert all(s["held"] for s in out)


def test_leere_liste():
    assert _capped([], cap=12) == []


def test_fehlendes_held_flag_gilt_als_kandidat():
    """Rueckwaerts-sicher: alte Eintraege ohne Flag werden nicht bevorzugt."""
    syms = [{"symbol": "X", "yf": "X"}, {"symbol": "Y", "yf": "Y", "held": True}]
    out = _capped(syms, cap=1)
    assert out[0]["symbol"] == "Y"
