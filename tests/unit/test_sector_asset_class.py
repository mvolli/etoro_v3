"""Unit tests fuer feat/sector-backfill (2026-08-12).

check_asset_class_gate loeste Symbole ausschliesslich ueber ASSET_CLASS_MAP
auf (~65 US-Ticker) und fiel bei jedem Miss fail-open durch. Gemessen am
realen Buch: 43 von 54 gehaltenen Symbolen = $6.433 = 74.2% des Equity ohne
jede Sektor-Grenze — das Gate hat faktisch nie etwas begrenzt.

instruments.sector (yfinance, befuellt von scripts/sync_instrument_sectors.py)
ist die fehlende Datenquelle. Ohne die Map bleibt das Verhalten unveraendert.
"""
from __future__ import annotations

import pytest

from bot.core.risk import check_asset_class_gate, resolve_asset_class


@pytest.fixture(autouse=True)
def _reset_globals():
    """Modul-Globals sichern — apply_config anderer Tests leckt sonst."""
    import bot.core.risk as risk
    before = (risk.ASSET_CLASS_DEFAULT_LIMIT_PCT, dict(risk.ASSET_CLASS_LIMITS))
    yield
    risk.ASSET_CLASS_DEFAULT_LIMIT_PCT = before[0]
    risk.ASSET_CLASS_LIMITS.clear()
    risk.ASSET_CLASS_LIMITS.update(before[1])


# ── resolve_asset_class ───────────────────────────────────────────────────────

def test_kuratiertes_mapping_hat_vorrang_vor_db_sektor():
    """Haendische Feinheiten (NVDA=US_TECH) duerfen nicht von yfinance
    ueberschrieben werden ('Technology' waere gruober)."""
    assert resolve_asset_class("NVDA", {"NVDA": "Technology"}) == "US_TECH"


def test_db_sektor_greift_wenn_mapping_fehlt():
    assert resolve_asset_class("OR.PA", {"OR.PA": "Consumer Defensive"}) == "SECTOR:Consumer Defensive"


def test_ohne_map_weiterhin_none():
    assert resolve_asset_class("OR.PA") is None
    assert resolve_asset_class("OR.PA", {}) is None


def test_unknown_gilt_wie_nicht_gesetzt():
    """'unknown' ist nur Rotations-Buchhaltung des Sync-Scripts, kein Sektor."""
    assert resolve_asset_class("XYZ.MI", {"XYZ.MI": "unknown"}) is None
    assert resolve_asset_class("XYZ.MI", {"XYZ.MI": "  "}) is None


def test_symbol_lookup_ist_case_insensitiv():
    assert resolve_asset_class("or.pa", {"OR.PA": "Utilities"}) == "SECTOR:Utilities"


# ── check_asset_class_gate ────────────────────────────────────────────────────

def test_ohne_sektormap_unveraendertes_fail_open():
    """Rueckwaerts-Kompatibilitaet: der bisherige Zustand bleibt exakt erhalten."""
    res = check_asset_class_gate("OR.PA", 500.0, 10_000.0,
                                 [{"symbol": "SRG.MI", "amount_usd": 9000.0}])
    assert res.allowed
    assert "kein Mapping" in " ".join(res.reasons)


def test_mit_sektormap_greift_der_cap():
    """Der Kern: gleiche Eingabe, aber mit Sektordaten wird begrenzt."""
    sectors = {"OR.PA": "Consumer Defensive", "KO2": "Consumer Defensive"}
    res = check_asset_class_gate(
        "OR.PA", 500.0, 10_000.0,
        [{"symbol": "KO2", "amount_usd": 1900.0}],   # 19% + 5% = 24% > 20%
        sector_by_symbol=sectors,
    )
    assert not res.allowed
    assert "Consumer Defensive" in " ".join(res.reasons)


def test_unter_dem_cap_erlaubt():
    sectors = {"OR.PA": "Consumer Defensive", "KO2": "Consumer Defensive"}
    res = check_asset_class_gate(
        "OR.PA", 500.0, 10_000.0,
        [{"symbol": "KO2", "amount_usd": 1000.0}],   # 10% + 5% = 15% < 20%
        sector_by_symbol=sectors,
    )
    assert res.allowed


def test_fremder_sektor_zaehlt_nicht_mit():
    sectors = {"OR.PA": "Consumer Defensive", "XOM2": "Energy"}
    res = check_asset_class_gate(
        "OR.PA", 500.0, 10_000.0,
        [{"symbol": "XOM2", "amount_usd": 9000.0}],  # anderer Sektor
        sector_by_symbol=sectors,
    )
    assert res.allowed


def test_bestand_ohne_sektor_wird_nicht_mitgezaehlt():
    """Teilbefuellung darf keine falschen Summen erzeugen (Backfill laeuft
    tagelang — waehrenddessen hat nur ein Teil des Buchs einen Sektor)."""
    sectors = {"OR.PA": "Consumer Defensive"}   # Bestand fehlt in der Map
    res = check_asset_class_gate(
        "OR.PA", 500.0, 10_000.0,
        [{"symbol": "UNBEKANNT", "amount_usd": 9000.0}],
        sector_by_symbol=sectors,
    )
    assert res.allowed


def test_equity_null_bleibt_skip():
    res = check_asset_class_gate("OR.PA", 500.0, 0.0, [],
                                 sector_by_symbol={"OR.PA": "Utilities"})
    assert res.allowed


# ── Topf-Zusammenfuehrung ─────────────────────────────────────────────────────

def test_sektor_aequivalente_klasse_weicht_dem_db_sektor():
    """JPM darf nicht in einem anderen Topf landen als SAVE.ST.

    Sonst hat derselbe Wirtschaftssektor zwei 20%-Caps = effektiv 40%.
    """
    assert resolve_asset_class("JPM", {"JPM": "Financial Services"}) == "SECTOR:Financial Services"
    assert resolve_asset_class("JNJ", {"JNJ": "Healthcare"}) == "SECTOR:Healthcare"
    assert resolve_asset_class("XOM", {"XOM": "Energy"}) == "SECTOR:Energy"
    assert resolve_asset_class("KO", {"KO": "Consumer Defensive"}) == "SECTOR:Consumer Defensive"


def test_abweichende_limits_behalten_vorrang():
    """US_TECH (40%) / BROAD_ETF (25%) / CRYPTO (10%) sind bewusste
    Abweichungen vom Default — ein yfinance-Sektor bildet sie nicht ab."""
    assert resolve_asset_class("NVDA", {"NVDA": "Technology"}) == "US_TECH"
    assert resolve_asset_class("SPY", {"SPY": "Financial Services"}) == "BROAD_ETF"
    assert resolve_asset_class("BTC", {"BTC": "Technology"}) == "CRYPTO"


def test_ohne_db_sektor_bleibt_kuratierte_klasse():
    assert resolve_asset_class("JPM") == "FINANCIAL"
    assert resolve_asset_class("JPM", {}) == "FINANCIAL"
    assert resolve_asset_class("JPM", {"JPM": "unknown"}) == "FINANCIAL"


def test_zusammengefuehrter_topf_summiert_korrekt():
    """Der eigentliche Zweck: JPM und SAVE.ST zaehlen auf dasselbe Limit."""
    sectors = {"JPM": "Financial Services", "SAVE.ST": "Financial Services"}
    res = check_asset_class_gate(
        "JPM", 500.0, 10_000.0,
        [{"symbol": "SAVE.ST", "amount_usd": 1900.0}],   # 19% + 5% = 24% > 20%
        sector_by_symbol=sectors,
    )
    assert not res.allowed
