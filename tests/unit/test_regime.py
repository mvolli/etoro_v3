"""Unit tests for regime detection V5 — no DB, no API needed."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.bot.core.regime import detect_regime, get_regime_params, get_risk_scalar, aqr_risk_scalar
import pytest

# ─── Regime Detection ─────────────────────────────────────────────────────────

def test_normal(): assert detect_regime(9700, 10000)[0] == "NORMAL"
def test_caution(): assert detect_regime(9500, 10000)[0] == "CAUTION"   # 5% DD
def test_defensive(): assert detect_regime(9100, 10000)[0] == "DEFENSIVE"  # 9% DD
def test_critical(): assert detect_regime(8400, 10000)[0] == "CRITICAL"   # 16% DD

def test_hysteresis_caution_stays():
    # 5.5% from CAUTION: between 3.5% (exit) and 8% (defensive) → stays CAUTION
    regime, _ = detect_regime(9450, 10000, previous_regime="CAUTION")
    assert regime == "CAUTION"

def test_hysteresis_exits_to_normal():
    # 3.2% from CAUTION → exits to NORMAL (below 3.5% exit threshold)
    regime, _ = detect_regime(9680, 10000, previous_regime="CAUTION")
    assert regime == "NORMAL"

def test_hysteresis_defensive_stays():
    # 7.5% from DEFENSIVE: above 7.0% exit → stays DEFENSIVE
    regime, _ = detect_regime(9250, 10000, previous_regime="DEFENSIVE")
    assert regime == "DEFENSIVE"

def test_hysteresis_critical_stays():
    # fix/critical-hysteresis: 14% DD from CRITICAL — above 13% exit
    # threshold → stays CRITICAL (kein Flattern CRITICAL↔DEFENSIVE)
    regime, reason = detect_regime(8600, 10000, previous_regime="CRITICAL")
    assert regime == "CRITICAL"
    assert "Hysteresis" in reason

def test_hysteresis_critical_exits_to_defensive():
    # 12.5% DD from CRITICAL — below 13% exit → drops to DEFENSIVE
    regime, _ = detect_regime(8750, 10000, previous_regime="CRITICAL")
    assert regime == "DEFENSIVE"

def test_critical_exit_boundary():
    # Exakt 13.0% → nicht mehr > CRITICAL_EXIT → DEFENSIVE
    regime, _ = detect_regime(8700, 10000, previous_regime="CRITICAL")
    assert regime == "DEFENSIVE"

def test_zero_peak(): assert detect_regime(9000, 0)[0] == "NORMAL"

# ─── Regime Parameters ────────────────────────────────────────────────────────

def test_params_normal():
    p = get_regime_params("NORMAL")
    assert p["cash_min_pct"] == 15.0
    assert p["buy_aggressiveness"] == 1.0
    assert p["allow_pyramiding"] == True
    assert p["min_conviction"] == "LOW"

def test_params_caution():
    p = get_regime_params("CAUTION")
    assert p["buy_aggressiveness"] == 0.75
    assert p["min_conviction"] == "MEDIUM"
    assert p["allow_pyramiding"] == True

def test_params_defensive():
    p = get_regime_params("DEFENSIVE")
    assert p["cash_min_pct"] == 25.0
    assert p["buy_aggressiveness"] == 0.50
    assert p["allow_pyramiding"] == False
    assert p["min_conviction"] == "HIGH"

def test_params_critical():
    p = get_regime_params("CRITICAL")
    assert p["buy_aggressiveness"] == 0.25
    assert p["allow_pyramiding"] == False
    assert p["min_conviction"] == "VERY_HIGH"

def test_params_invalid():
    with pytest.raises(ValueError): get_regime_params("DRAWDOWN")
    with pytest.raises(ValueError): get_regime_params("RECOVERY")

# ─── Risk Scalar ─────────────────────────────────────────────────────────────

def test_risk_scalars():
    assert get_risk_scalar("NORMAL") == 1.00
    assert get_risk_scalar("CAUTION") == 0.75
    assert get_risk_scalar("DEFENSIVE") == 0.50
    assert get_risk_scalar("CRITICAL") == 0.25

# ─── AQR Formula ─────────────────────────────────────────────────────────────

def test_aqr_no_drawdown(): assert aqr_risk_scalar(0) == 1.0
def test_aqr_10pct_dd(): assert abs(aqr_risk_scalar(10) - 0.80) < 0.01
def test_aqr_25pct_dd(): assert abs(aqr_risk_scalar(25) - 0.50) < 0.01
def test_aqr_minimum(): assert aqr_risk_scalar(50) == 0.25  # Capped at 0.25


# ─── fix/regime-config-wiring: apply_config actually overrides thresholds ────
from src.bot.core import regime as _regime_mod


@pytest.fixture
def _restore_regime_thresholds():
    saved = {k: getattr(_regime_mod, k) for k in (
        "CAUTION_THRESHOLD", "DEFENSIVE_THRESHOLD", "CRITICAL_THRESHOLD",
        "CAUTION_EXIT", "DEFENSIVE_EXIT", "CRITICAL_EXIT",
    )}
    yield
    for k, v in saved.items():
        setattr(_regime_mod, k, v)


def test_apply_config_overrides_thresholds(_restore_regime_thresholds):
    _regime_mod.apply_config({"regime": {
        "caution_pct": 3.0, "defensive_pct": 6.0, "critical_pct": 12.0,
        "caution_exit_pct": 2.5, "defensive_exit_pct": 5.0, "critical_exit_pct": 10.0,
    }})
    assert _regime_mod.CAUTION_THRESHOLD == 3.0
    assert _regime_mod.CRITICAL_THRESHOLD == 12.0
    # detect_regime must reflect the new (tighter) thresholds at runtime:
    # 6.5% DD is now DEFENSIVE (was only CAUTION under the 8% default).
    assert detect_regime(9350, 10000)[0] == "DEFENSIVE"


def test_apply_config_empty_is_noop(_restore_regime_thresholds):
    before = _regime_mod.CAUTION_THRESHOLD
    _regime_mod.apply_config({})
    _regime_mod.apply_config(None)
    assert _regime_mod.CAUTION_THRESHOLD == before


def test_apply_config_bad_value_keeps_defaults(_restore_regime_thresholds):
    before = _regime_mod.CRITICAL_THRESHOLD
    _regime_mod.apply_config({"regime": {"critical_pct": "not-a-number"}})
    assert _regime_mod.CRITICAL_THRESHOLD == before


# ── min_conviction aus der Config (fix/regime-min-conviction-config) ─────────
# Der Wert stand hart in _REGIME_PARAMS. Diese Tests sichern beides ab: dass
# die Config wirklich durchschlaegt (die "config wiring lie", vor der
# apply_config im Docstring warnt) und dass Unsinn die Defaults stehen laesst.

import pytest as _pytest

from bot.core import regime as _regime


@_pytest.fixture(autouse=True)
def _restore_min_conviction():
    """Modul-Globals sind Prozess-Zustand — nach jedem Test zuruecksetzen."""
    vorher = {r: p.get("min_conviction") for r, p in _regime._REGIME_PARAMS.items()}
    yield
    for r, v in vorher.items():
        _regime._REGIME_PARAMS[r]["min_conviction"] = v


def test_min_conviction_aus_config_schlaegt_durch():
    assert _regime.get_min_conviction("DEFENSIVE") == "HIGH"   # Default
    _regime.apply_config({"regime": {"min_conviction": {"DEFENSIVE": "MEDIUM"}}})
    assert _regime.get_min_conviction("DEFENSIVE") == "MEDIUM"
    # get_regime_params liest dieselbe Quelle
    assert _regime.get_regime_params("DEFENSIVE")["min_conviction"] == "MEDIUM"


def test_min_conviction_akzeptiert_kleinschreibung():
    _regime.apply_config({"regime": {"min_conviction": {"defensive": "medium"}}})
    assert _regime.get_min_conviction("DEFENSIVE") == "MEDIUM"


def test_min_conviction_ignoriert_unsinn_und_laesst_default():
    _regime.apply_config({"regime": {"min_conviction": {"DEFENSIVE": "SEHR_HOCH"}}})
    assert _regime.get_min_conviction("DEFENSIVE") == "HIGH"
    _regime.apply_config({"regime": {"min_conviction": {"GIBTS_NICHT": "LOW"}}})
    assert _regime.get_min_conviction("DEFENSIVE") == "HIGH"


def test_min_conviction_ohne_block_aendert_nichts():
    _regime.apply_config({"regime": {"defensive_pct": 8.0}})
    assert _regime.get_min_conviction("DEFENSIVE") == "HIGH"
    assert _regime.get_min_conviction("CRITICAL") == "VERY_HIGH"


def test_produktivconfig_setzt_defensive_auf_medium():
    """Haelt fest, was live gilt — CRITICAL bleibt bewusst gesperrt."""
    import pathlib, yaml
    root = pathlib.Path(__file__).resolve().parents[2]
    cfg = yaml.safe_load((root / "config" / "config.yaml").read_text(encoding="utf-8"))
    mc = (cfg.get("regime") or {}).get("min_conviction") or {}
    assert mc.get("DEFENSIVE") == "MEDIUM"
    assert mc.get("CRITICAL") == "VERY_HIGH"


def test_signal_worker_verdrahtet_regime_config():
    """Wer get_min_conviction nutzt, muss regime.apply_config aufgerufen haben.

    Jeder Worker ist ein eigener Prozess; _REGIME_PARAMS wird pro Prozess
    ueberschrieben. Ein Config-Wert ohne passenden apply_config-Aufruf ist eine
    Attrappe — genau das war fuer risk.py und die Regime-Schwellen schon
    zweimal der Fall ("config wiring lie", siehe Docstring von apply_config).
    Der Test liest die Quelle, weil sich der Import-Zeitpunkt sonst nicht
    pruefen laesst, ohne den ganzen Worker zu starten.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src" / "bot" / "workers" / "signal_worker.py").read_text(encoding="utf-8")
    assert "get_min_conviction" in src
    assert "apply_regime_config(cfg)" in src, (
        "signal_worker nutzt get_min_conviction, ruft aber regime.apply_config "
        "nicht auf — regime.min_conviction aus der config.yaml bliebe wirkungslos"
    )
