"""Unit tests fuer fix/exposure-auto-trim (2026-08-12, User-Entscheid).

Der Bot korrigiert ein Exposure ueber dem Cap SELBST (LIFO-Close bis zurueck
unter den Cap), statt eine Warnung an einen Menschen zu reichen. Begruendung
des Users: ein Trading-Bot, der eine erkannte Grenzverletzung nur meldet,
nimmt dem Betreiber die Entscheidung nicht ab — er verschiebt sie nur.

Getestet wird ausschliesslich die Planung (pure function). Die Ausfuehrung
(close_exposure_excess) haengt am eToro-Client und ist Integrationsflaeche.
"""
from __future__ import annotations

from bot.core.concentration_monitor import plan_exposure_trim


def _pos(pid: str, amount: float, opened: str, iid: int = 1) -> dict:
    return {
        "positionID": pid,
        "instrumentID": iid,
        "amount": amount,
        "openDateTime": opened,
    }


def test_innerhalb_des_caps_kein_plan():
    positions = [_pos("a", 1000.0, "2026-08-01"), _pos("b", 2000.0, "2026-08-02")]
    assert plan_exposure_trim(positions, equity=10_000.0, max_exposure_pct=75.0) == []


def test_lifo_neueste_zuerst():
    """Die juengste Position hat die kuerzeste Haltethese — sie geht zuerst."""
    positions = [
        _pos("alt", 3000.0, "2026-07-01"),
        _pos("mittel", 3000.0, "2026-07-15"),
        _pos("neu", 2200.0, "2026-08-10"),
    ]
    # 8200 / 10000 = 82% > 75% → Ueberhang 700
    plan = plan_exposure_trim(positions, equity=10_000.0, max_exposure_pct=75.0)
    assert [p["position_id"] for p in plan] == ["neu"]


def test_schliesst_bis_ueberhang_gedeckt_ist():
    positions = [
        _pos("a", 2000.0, "2026-07-01"),
        _pos("b", 2000.0, "2026-07-02"),
        _pos("c", 2000.0, "2026-07-03"),
        _pos("d", 2000.0, "2026-07-04"),
        _pos("e", 2000.0, "2026-07-05"),
    ]
    # 10000/10000 = 100% > 75% → Ueberhang 2500 → 2 Fragmente a 2000
    plan = plan_exposure_trim(positions, equity=10_000.0, max_exposure_pct=75.0)
    assert [p["position_id"] for p in plan] == ["e", "d"]
    assert sum(p["amount_usd"] for p in plan) >= 2500.0


def test_ueberschuss_wird_ausgewiesen():
    """Ganz-Fragment-Closes koennen leicht ueberschiessen — das muss sichtbar sein."""
    positions = [_pos("a", 5000.0, "2026-07-01"), _pos("b", 3000.0, "2026-08-01")]
    # 8000/10000 = 80% → Ueberhang 500, kleinstes Fragment ist aber 3000
    plan = plan_exposure_trim(positions, equity=10_000.0, max_exposure_pct=75.0)
    assert len(plan) == 1
    assert plan[-1]["overshoot_usd"] == 2500.0


def test_symbol_wird_aus_instrument_map_aufgeloest():
    positions = [_pos("a", 9000.0, "2026-08-01", iid=42)]
    plan = plan_exposure_trim(positions, 10_000.0, 75.0, instrument_map={42: "AAPL"})
    assert plan[0]["symbol"] == "AAPL"


def test_unbekannte_instrument_id_bricht_nicht():
    positions = [_pos("a", 9000.0, "2026-08-01", iid=999)]
    plan = plan_exposure_trim(positions, 10_000.0, 75.0, instrument_map={})
    assert plan[0]["symbol"] == "ID999"


def test_nullbetraege_werden_uebersprungen():
    positions = [
        _pos("leer", 0.0, "2026-08-11"),
        _pos("echt", 9000.0, "2026-08-10"),
    ]
    plan = plan_exposure_trim(positions, 10_000.0, 75.0)
    assert [p["position_id"] for p in plan] == ["echt"]


def test_fehlende_opendatetime_bricht_die_sortierung_nicht():
    positions = [
        {"positionID": "x", "instrumentID": 1, "amount": 5000.0},
        _pos("y", 4000.0, "2026-08-01"),
    ]
    plan = plan_exposure_trim(positions, 10_000.0, 75.0)
    assert len(plan) >= 1


def test_live_lage_2026_08_12():
    """Gegenprobe mit der realen Lage: 81.9%, Ueberhang $601."""
    # 59 Positionen, $7.100 investiert, Equity $8.667.85
    positions = [_pos(f"p{i}", 7100.0 / 59, f"2026-08-{(i % 28) + 1:02d}")
                 for i in range(59)]
    plan = plan_exposure_trim(positions, equity=8_667.85, max_exposure_pct=75.0)
    assert plan, "muss bei 81.9% einen Trim planen"
    # Ueberhang ~601 bei Fragmenten a ~120 → ~5 Positionen
    assert 4 <= len(plan) <= 6
    assert sum(p["amount_usd"] for p in plan) >= 601.0


# ── Market-Hours-Guard ────────────────────────────────────────────────────────

def test_geschlossene_maerkte_werden_uebersprungen():
    """Der Live-Fehler: LIFO traf 2883.HK bei geschlossener HK-Boerse.

    verify_full_close lief 165s in den Timeout, der Plan blieb gleich, und der
    naechste 5-min-Zyklus versuchte exakt denselben Close erneut — Exposure
    sank nie, jeder Lauf blockierte 165 Sekunden.
    """
    positions = [
        _pos("hk", 2200.0, "2026-08-12", iid=2332),    # neueste, Markt ZU
        _pos("eu", 2200.0, "2026-08-11", iid=100),     # naechste, Markt offen
        _pos("alt", 4000.0, "2026-07-01", iid=101),
    ]
    plan = plan_exposure_trim(
        positions, equity=10_000.0, max_exposure_pct=75.0,
        is_market_open_fn=lambda iid: iid != 2332,
    )
    assert [p["position_id"] for p in plan] == ["eu"]


def test_alle_maerkte_zu_ergibt_leeren_plan():
    """Kein Trim ist besser als ein Trim, der garantiert nicht verifiziert."""
    positions = [_pos("a", 9000.0, "2026-08-12", iid=1)]
    plan = plan_exposure_trim(positions, 10_000.0, 75.0,
                              is_market_open_fn=lambda _iid: False)
    assert plan == []


def test_ohne_guard_unveraendertes_verhalten():
    positions = [_pos("a", 9000.0, "2026-08-12", iid=1)]
    assert len(plan_exposure_trim(positions, 10_000.0, 75.0)) == 1


def test_guard_faellt_open_bei_exception():
    """Eine kaputte Marktzeit-Abfrage darf den Trim nicht lahmlegen."""
    def broken(_iid):
        raise RuntimeError("market_hours down")
    positions = [_pos("a", 9000.0, "2026-08-12", iid=1)]
    assert len(plan_exposure_trim(positions, 10_000.0, 75.0,
                                  is_market_open_fn=broken)) == 1
