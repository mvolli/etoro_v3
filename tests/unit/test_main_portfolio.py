"""Unit tests fuer feat/main-portfolio-report (2026-08-20).

Report fuer das HAUPTKONTO — getrennt vom Bot-Konto, das der Rest des Repos
handelt. Die Fallen, die hier abgesichert werden:

- Erster Lauf hat keine Vergleichsbasis und darf nicht "+71 Positionen"
  melden, als waere ueber Nacht ein Portfolio entstanden.
- Portfolio-`unrealizedPnL` weicht bewusst von der Positions-Summe ab
  (Mirrors), beide Werte muessen getrennt erhalten bleiben.
- "Veraenderung" gilt nur fuer Positionen, die BEIDE Tage existierten.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.core.main_portfolio import (
    DUST_USD, aggregate_by, build_snapshot, diff_snapshots, pct_change, top_positions,
)

NOW = datetime(2026, 8, 20, 21, 30, tzinfo=timezone.utc)


def _pos(pid, iid, amount, open_rate, close_rate, pnl):
    return {
        "positionID": pid, "instrumentID": iid, "amount": amount,
        "units": 1.0, "openRate": open_rate, "isBuy": True, "leverage": 1,
        "openDateTime": "2025-01-08T14:31:07.917Z",
        "unrealizedPnL": {"pnL": pnl, "closeRate": close_rate},
    }


def _cp(positions, credit=46.8, unreal=411.59, mirrors=None):
    return {
        "positions": positions, "credit": credit, "unrealizedPnL": unreal,
        "mirrors": mirrors if mirrors is not None else [],
    }


# ── build_snapshot ────────────────────────────────────────────────────────────

def test_snapshot_grundwerte():
    acc, pos = build_snapshot(_cp([_pos("1", 1914, 20.0, 618.0, 360.19, -8.34)]),
                              {1914: "TSLA"}, now=NOW)
    assert len(pos) == 1
    assert pos[0]["symbol"] == "TSLA"
    assert pos[0]["close_rate"] == 360.19
    assert acc["invested"] == 20.0
    assert acc["snapshot_date"] == "2026-08-20"


def test_unbekanntes_instrument_bekommt_id_platzhalter():
    _, pos = build_snapshot(_cp([_pos("1", 99999, 10.0, 1.0, 1.0, 0.0)]), {}, now=NOW)
    assert pos[0]["symbol"] == "ID99999"


def test_portfolio_pnl_und_positions_pnl_bleiben_getrennt():
    """Der Kern der Abstimmung: die Differenz ist der Mirror-Anteil und
    darf nicht weggerechnet werden."""
    acc, _ = build_snapshot(
        _cp([_pos("1", 1, 100.0, 10.0, 11.0, 357.40)], unreal=411.59), {}, now=NOW)
    assert acc["positions_pnl"] == 357.40
    assert acc["unrealized_pnl"] == 411.59
    assert acc["mirror_pnl"] == pytest.approx(54.19, abs=0.01)


def test_equity_rechnet_sich_aus_investiert_cash_und_pnl():
    acc, _ = build_snapshot(
        _cp([_pos("1", 1, 7080.63, 1.0, 1.0, 357.40)], credit=46.8, unreal=411.59),
        {}, now=NOW)
    assert acc["equity"] == pytest.approx(7080.63 + 46.8 + 411.59, abs=0.01)


def test_mirrors_werden_aggregiert_nicht_als_positionen_gezaehlt():
    mirrors = [{"initialInvestment": 417.0, "closedPositionsNetProfit": -49.69,
                "availableAmount": 47.88},
               {"initialInvestment": 200.0, "closedPositionsNetProfit": -13.8,
                "availableAmount": 306.74}]
    acc, pos = build_snapshot(_cp([_pos("1", 1, 10.0, 1.0, 1.0, 0.0)], mirrors=mirrors),
                              {}, now=NOW)
    assert acc["position_count"] == 1          # Mirrors zaehlen NICHT mit
    assert acc["mirror_count"] == 2
    assert acc["mirror_invested"] == 617.0
    assert acc["mirror_net_profit"] == pytest.approx(-63.49, abs=0.01)


def test_leeres_portfolio_bricht_nicht():
    acc, pos = build_snapshot(_cp([]), {}, now=NOW)
    assert pos == [] and acc["position_count"] == 0


def test_kaputte_zahlen_werden_zu_null_statt_zu_werfen():
    bad = _pos("1", 1, "keine Zahl", None, "x", None)
    _, pos = build_snapshot(_cp([bad]), {}, now=NOW)
    assert pos[0]["amount"] == 0.0 and pos[0]["pnl_usd"] == 0.0


# ── pct_change ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("new,old,exp", [(110, 100, 10.0), (90, 100, -10.0), (100, 100, 0.0)])
def test_pct_change(new, old, exp):
    assert pct_change(new, old) == pytest.approx(exp)


def test_pct_change_ohne_basis_ist_none():
    assert pct_change(10, 0) is None
    assert pct_change(10, None) is None


# ── diff_snapshots ────────────────────────────────────────────────────────────

def _snap(specs, credit=0.0, unreal=0.0, date="2026-08-19"):
    acc, pos = build_snapshot(_cp([_pos(*s) for s in specs], credit=credit, unreal=unreal),
                              {}, now=datetime.fromisoformat(date + "T21:30+00:00"))
    return acc, pos


def test_erster_lauf_ist_baseline_kein_nullvergleich():
    """Ohne diesen Zweig meldete der erste Report '+71 Positionen'."""
    acc, pos = _snap([("1", 1, 100.0, 10.0, 11.0, 5.0)])
    d = diff_snapshots(None, None, acc, pos)
    assert d["is_baseline"] is True
    assert d["opened"] == [] and d["closed"] == [] and d["movers"] == []
    assert d["equity_delta"] is None


def test_neue_und_geschlossene_positionen():
    pa, pp = _snap([("1", 1, 100.0, 10.0, 10.0, 0.0), ("2", 2, 50.0, 5.0, 5.0, 0.0)])
    ca, cp_ = _snap([("1", 1, 100.0, 10.0, 10.0, 0.0), ("3", 3, 70.0, 7.0, 7.0, 0.0)],
                    date="2026-08-20")
    d = diff_snapshots(pa, pp, ca, cp_)
    assert [p["position_id"] for p in d["opened"]] == ["3"]
    assert [p["position_id"] for p in d["closed"]] == ["2"]


def test_mover_nur_fuer_positionen_die_beide_tage_existierten():
    """Ein Einstieg ist keine Kursbewegung — sonst stuende jede neue
    Position mit einer erfundenen Prozentzahl in der Bewegungsliste."""
    pa, pp = _snap([("1", 1, 100.0, 10.0, 10.0, 0.0)])
    ca, cp_ = _snap([("1", 1, 100.0, 10.0, 12.0, 20.0), ("neu", 9, 80.0, 8.0, 9.0, 10.0)],
                    date="2026-08-20")
    d = diff_snapshots(pa, pp, ca, cp_)
    assert [m["position_id"] for m in d["movers"]] == ["1"]
    assert d["movers"][0]["change_pct"] == pytest.approx(20.0)


def test_mover_nach_betrag_der_bewegung_sortiert():
    pa, pp = _snap([("a", 1, 100.0, 10.0, 10.0, 0.0), ("b", 2, 100.0, 10.0, 10.0, 0.0)])
    ca, cp_ = _snap([("a", 1, 100.0, 10.0, 10.5, 5.0), ("b", 2, 100.0, 10.0, 8.0, -20.0)],
                    date="2026-08-20")
    d = diff_snapshots(pa, pp, ca, cp_)
    # -20% steht vor +5%: Betrag entscheidet, nicht Vorzeichen
    assert [m["position_id"] for m in d["movers"]] == ["b", "a"]


def test_staubpositionen_tauchen_nicht_als_mover_auf():
    pa, pp = _snap([("dust", 1, DUST_USD - 1, 10.0, 10.0, 0.0)])
    ca, cp_ = _snap([("dust", 1, DUST_USD - 1, 10.0, 20.0, 1.0)], date="2026-08-20")
    assert diff_snapshots(pa, pp, ca, cp_)["movers"] == []


def test_equity_delta_wird_berechnet():
    pa, pp = _snap([("1", 1, 100.0, 10.0, 10.0, 0.0)], credit=10.0, unreal=0.0)
    ca, cp_ = _snap([("1", 1, 100.0, 10.0, 11.0, 10.0)], credit=10.0, unreal=10.0,
                    date="2026-08-20")
    d = diff_snapshots(pa, pp, ca, cp_)
    assert d["equity_delta"] == pytest.approx(10.0)
    assert d["prev_date"] == "2026-08-19"


# ── Aggregation ───────────────────────────────────────────────────────────────

def test_aggregate_by_gruppiert_und_sortiert():
    pos = [{"symbol": "A", "amount": 100.0, "pnl_usd": 5.0},
           {"symbol": "B", "amount": 300.0, "pnl_usd": -2.0},
           {"symbol": "C", "amount": 50.0, "pnl_usd": 1.0}]
    out = aggregate_by(pos, {"A": "Tech", "B": "Energie"}, label="Ohne")
    assert out[0] == ("Energie", 300.0, -2.0)
    assert ("Ohne", 50.0, 1.0) in out          # C faellt nicht raus


def test_top_positions_sortiert_absteigend_und_kappt():
    pos = [{"symbol": "A", "amount": 5.0, "pnl_usd": 0.0},
           {"symbol": "B", "amount": 90.0, "pnl_usd": 0.0},
           {"symbol": "C", "amount": 40.0, "pnl_usd": 0.0}]
    assert [p["amount"] for p in top_positions(pos, 2)] == [90.0, 40.0]


def test_top_positions_aggregiert_je_symbol():
    """eToro fuehrt mehrere Kaeufe desselben Titels getrennt — ungruppiert
    stuende derselbe Titel mehrfach und saehe kleiner aus als er ist."""
    pos = [{"symbol": "ICM", "amount": 488.0, "pnl_usd": 15.87},
           {"symbol": "ICM", "amount": 250.0, "pnl_usd": 10.56},
           {"symbol": "SPCX", "amount": 377.0, "pnl_usd": -100.62}]
    top = top_positions(pos, 5)
    assert [t["symbol"] for t in top] == ["ICM", "SPCX"]
    assert top[0]["amount"] == 738.0
    assert top[0]["pnl_usd"] == pytest.approx(26.43)
    assert top[0]["parts"] == 2


def test_movers_werden_je_symbol_zusammengefasst():
    """Mehrere Positionen im selben Titel bewegen sich identisch (gleicher
    Kurs) — ungruppiert stuende derselbe Titel mehrfach mit DERSELBEN
    Prozentzahl in Liste und Grafik und verdraengte andere Titel."""
    pa, pp = _snap([("a1", 1, 100.0, 10.0, 10.0, 0.0),
                    ("a2", 1, 200.0, 10.0, 10.0, 0.0),
                    ("b1", 2, 50.0, 5.0, 5.0, 0.0)])
    ca, cp_ = _snap([("a1", 1, 100.0, 10.0, 11.0, 10.0),
                     ("a2", 1, 200.0, 10.0, 11.0, 20.0),
                     ("b1", 2, 50.0, 5.0, 4.0, -10.0)], date="2026-08-20")
    movers = diff_snapshots(pa, pp, ca, cp_)["movers"]
    assert len(movers) == 2, "je Symbol genau ein Eintrag"
    a = next(m for m in movers if m["symbol"] == "ID1")
    assert a["change_pct"] == pytest.approx(10.0)   # Prozent bleibt
    assert a["pnl_delta"] == pytest.approx(30.0)    # Dollar summiert
    assert a["amount"] == pytest.approx(300.0)      # Einsatz summiert
    assert a["parts"] == 2


# ── Klarnamen ─────────────────────────────────────────────────────────────────

def test_display_name_nutzt_klarnamen():
    from bot.core.main_portfolio import display_name
    assert display_name("Allianz SE", "ALV.DE") == "Allianz SE"


def test_display_name_faellt_auf_symbol_zurueck():
    from bot.core.main_portfolio import display_name
    assert display_name(None, "ALV.DE") == "ALV.DE"
    assert display_name("   ", "ALV.DE") == "ALV.DE"


def test_display_name_kuerzt_lange_namen():
    from bot.core.main_portfolio import display_name
    lang = "Vanguard FTSE All World High Dividend Yield UCITS ETF"
    out = display_name(lang, "VFA_10560", width=26)
    assert len(out) == 26 and out.endswith("…")


def test_snapshot_traegt_den_namen():
    """'ICM_3040' ist der iShares Core MSCI World — ein Report, den man
    ohne Nachschlagen nicht lesen kann, ist keiner."""
    acc, pos = build_snapshot(_cp([_pos("1", 3040, 20.0, 1.0, 1.0, 0.0)]),
                              {3040: "ICM_3040"},
                              name_by_id={3040: "iShares Core MSCI World UCITS ETF"},
                              now=NOW)
    assert pos[0]["name"] == "iShares Core MSCI World UCITS ETF"
    assert pos[0]["symbol"] == "ICM_3040"


def test_ohne_namensmap_bleibt_der_name_leer_statt_zu_werfen():
    _, pos = build_snapshot(_cp([_pos("1", 3040, 20.0, 1.0, 1.0, 0.0)]), {}, now=NOW)
    assert pos[0]["name"] == ""


def test_aggregation_behaelt_den_namen():
    pos = [{"symbol": "ICM_3040", "name": "iShares Core MSCI World",
            "amount": 488.0, "pnl_usd": 15.0},
           {"symbol": "ICM_3040", "name": "iShares Core MSCI World",
            "amount": 250.0, "pnl_usd": 10.0}]
    top = top_positions(pos, 3)
    assert top[0]["name"] == "iShares Core MSCI World"
    assert top[0]["amount"] == 738.0
