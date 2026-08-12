"""Unit tests fuer feat/region-damper (2026-08-12).

Die reale Klumpenlage des Buchs ist GEOGRAFISCH, nicht sektoral: EU 34.8%,
ASIA_CN 18.0%, US 9.7% (Anteile am Equity, Stand 2026-08-12) — an den
Positionen allein sogar 44.7% EU. Kein Gate hat das je gemessen.

Bewusst ein Damper und kein Block: EU ist die Haupt-Signalquelle des Bots.
Ein harter Cap unterhalb der aktuellen Quote wuerde sie abschalten — das
waere weniger Autonomie, nicht mehr.
"""
from __future__ import annotations

import pytest

import bot.core.risk as risk
from bot.core.risk import region_size_factor


@pytest.fixture(autouse=True)
def _reset_globals():
    before = (risk.REGION_SOFT_CAP_PCT, risk.REGION_HARD_CAP_PCT, risk.REGION_MIN_FACTOR)
    yield
    (risk.REGION_SOFT_CAP_PCT, risk.REGION_HARD_CAP_PCT,
     risk.REGION_MIN_FACTOR) = before


def test_unter_soft_cap_unveraendert():
    f, reason = region_size_factor(20.0)
    assert f == 1.0
    assert "OK" in reason


def test_genau_am_soft_cap_unveraendert():
    assert region_size_factor(35.0)[0] == 1.0


def test_ueber_soft_cap_schrumpft_linear():
    """Mitte zwischen Soft (35) und Hard (50) -> Faktor mittig zwischen 1.0 und 0.35."""
    f, reason = region_size_factor(42.5)
    assert f == pytest.approx(0.675, abs=0.01)
    assert "Damper" in reason


def test_kurz_unter_hard_cap_nahe_min_factor():
    f, _ = region_size_factor(49.9)
    assert 0.35 <= f < 0.40


def test_ueber_hard_cap_blockt():
    f, reason = region_size_factor(55.0)
    assert f == 0.0
    assert "Hard-Cap" in reason


def test_monoton_fallend():
    prev = 1.1
    for pct in range(0, 51, 2):
        f, _ = region_size_factor(float(pct))
        assert f <= prev + 1e-9, f"nicht monoton bei {pct}%"
        prev = f


def test_faktor_bleibt_im_erlaubten_band():
    for pct in range(0, 100):
        f, _ = region_size_factor(float(pct))
        assert f == 0.0 or 0.35 <= f <= 1.0


def test_eigene_caps_werden_beachtet():
    f, _ = region_size_factor(30.0, soft_cap_pct=25.0, hard_cap_pct=40.0, min_factor=0.5)
    assert f == pytest.approx(0.8333, abs=0.01)


def test_ungueltige_caps_deaktivieren_den_damper():
    assert region_size_factor(99.0, soft_cap_pct=50.0, hard_cap_pct=40.0)[0] == 1.0
    assert region_size_factor(99.0, soft_cap_pct=0.0)[0] == 1.0


def test_apply_config_liest_region_limits():
    risk.apply_config({"region_limits": {
        "soft_cap_pct": 25.0, "hard_cap_pct": 45.0, "min_factor": 0.5,
    }})
    assert risk.REGION_SOFT_CAP_PCT == 25.0
    assert risk.REGION_HARD_CAP_PCT == 45.0
    assert risk.REGION_MIN_FACTOR == 0.5


def test_live_lage_eu_bleibt_handlungsfaehig():
    """EU 34.8% liegt knapp unter dem Soft-Cap — voll handlungsfaehig.

    Genau das ist der Zweck: der Damper greift, BEVOR der Klumpen kritisch
    wird, ohne die Haupt-Signalquelle abzuschneiden.
    """
    assert region_size_factor(34.8)[0] == 1.0
    # Erst weiterer Aufbau daempft
    assert region_size_factor(40.0)[0] < 1.0
