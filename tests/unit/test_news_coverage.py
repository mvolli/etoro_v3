"""Unit tests fuer fix/news-coverage (2026-08-12).

`symbols[:CAP]` schnitt hart ab: bei 54 Live-Symbolen gegen
EARNINGS_SYMBOL_CAP=12 blieben 42 offene Positionen ungeprueft. Earnings sind
der teuerste blinde Fleck dieses Bots — ein Termin ist ein Gap-Risiko, gegen
das der Software-Trailing-Stop (eToro hat keinen SL-Update-Endpoint) nicht
schuetzt.

Der Cap begrenzt nur noch den KANDIDATEN-Schwanz.

fix/news-candidate-floor (2026-09-12): dieselbe Formel liess die Kandidaten
verhungern — `budget = max(cap, n_held)` ergibt bei 51 Positionen und cap=20
ein Budget von 51, und `rest[:51-51]` ist leer. Gemessen am 12.09.:
Positionen 51/51, Kandidaten 0/8. Das war eine Umkehrung, denn die Flags
wirken fast nur auf Kandidaten (AVOID = Signal ueberspringen, CAUTION =
halbe Groesse — beides VOR dem Kauf). Kandidaten haben jetzt eine eigene,
vom Depotstand unabhaengige Quote in Hoehe des Caps.
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
    """Der Cap deckelt den Kandidaten-Schwanz — unabhaengig vom Depotstand.

    Frueher: 5 Positionen zogen 5 vom Budget 20 ab, es blieben 15 Kandidaten.
    Jetzt bekommen Kandidaten die vollen 20; Positionen kosten sie nichts.
    """
    out = _capped(_syms(5, 100), cap=20)
    assert sum(1 for s in out if not s["held"]) == 20
    assert len(out) == 25


def test_positionen_stehen_vorn():
    out = _capped(_syms(3, 10), cap=6)
    assert [s["held"] for s in out[:3]] == [True, True, True]


def test_ohne_positionen_gilt_der_cap_normal():
    out = _capped(_syms(0, 50), cap=12)
    assert len(out) == 12


def test_volles_depot_verdraengt_die_kandidaten_nicht_mehr():
    """Der eigentliche Fehler: ein volles Depot nahm den Kandidaten alles.

    Live-Fall vom 12.09.2026 — 51 gehaltene Positionen, 8 FRESH-Kandidaten,
    NEWS_SYMBOL_CAP=20: geprueft wurden 51 Positionen und NULL Kandidaten.
    Genau die Symbole, bei denen ein Flag noch einen Kauf verhindern kann,
    waren ungeprueft.
    """
    out = _capped(_syms(51, 8), cap=20)
    assert sum(1 for s in out if s["held"]) == 51
    assert sum(1 for s in out if not s["held"]) == 8

    # und der Deckel greift weiterhin, wenn wirklich viele Kandidaten warten
    viele = _capped(_syms(30, 100), cap=12)
    assert sum(1 for s in viele if s["held"]) == 30
    assert sum(1 for s in viele if not s["held"]) == 12


def test_leere_liste():
    assert _capped([], cap=12) == []


def test_fehlendes_held_flag_gilt_als_kandidat():
    """Rueckwaerts-sicher: alte Eintraege ohne Flag werden nicht bevorzugt."""
    syms = [{"symbol": "X", "yf": "X"}, {"symbol": "Y", "yf": "Y", "held": True}]
    out = _capped(syms, cap=1)
    assert out[0]["symbol"] == "Y"
