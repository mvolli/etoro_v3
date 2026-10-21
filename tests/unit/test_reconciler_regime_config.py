"""fix/regime-config-reconciler (2026-10-16).

Der Reconciler ruft update_regime() auf und schreibt CURRENT_REGIME —
genau wie der risk_worker. Der risk_worker laedt ZUERST die
Regime-Schwellen aus config.yaml (apply_regime_config), der Reconciler
tue es nicht: er lief auf den Code-Defaults (8% DEFENSIVE), waehrend der
risk_worker mit den config-Werten (30%, feat/market-regime e89f02c)
NORMAL schrieb. Beide schrieben CURRENT_REGIME 1 Minute auseinander ->
5-min-Whipsaw am selben Equity-Punkt (DD 9,99%: risk=NORMAL, recon=DEFENSIVE).

Diese Tests sichern ab, dass der Reconciler die Config-Schwellen vor
update_regime laedt und damit zum risk_worker identisch detektiert.
Keine Live-DB, keine API — tmp_path + Quelltext-Reihenfolge (Reihenfolge-
Tests sind hier erlaubt: es geht um die AUFFOLGUNG load-before-use, nicht
um Datenstand).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from src.bot.core import regime as R
from src.bot.db.connection import DB
from src.bot.db.repo import StateRepo

_SRC = Path(__file__).parent.parent.parent / "src" / "bot" / "workers" / "reconciler.py"


# apply_config() mutiert MODUL-GLOBALS (Schwellen, REGIME_SOURCE, Exit-
# Stufen, MARKET_*-Stufen) UND _REGIME_PARAMS in-place (min_conviction
# ueber _apply_min_conviction). Um andere Test-Dateien (z.B.
# test_regime.py, das auf den Code-Defaults laeuft) nicht zu
# verunreinigen, snapshot+restore pro Test — inkl. Deep-Copy des
# _REGIME_PARAMS-Dicts.
_REGIME_GLOBALS = (
    "CAUTION_THRESHOLD", "DEFENSIVE_THRESHOLD", "CRITICAL_THRESHOLD",
    "CAUTION_EXIT", "DEFENSIVE_EXIT", "CRITICAL_EXIT", "REGIME_SOURCE",
    "MARKET_CAUTION_DD_PCT", "MARKET_CAUTION_EXIT_DD_PCT",
    "MARKET_DEFENSIVE_DD_PCT", "MARKET_DEFENSIVE_EXIT_DD_PCT",
    "MARKET_CRITICAL_DD_PCT", "MARKET_CRITICAL_EXIT_DD_PCT",
)


@pytest.fixture(autouse=True)
def _isolate_regime_globals():
    import copy
    saved_globals = {g: getattr(R, g) for g in _REGIME_GLOBALS}
    saved_params = copy.deepcopy(R._REGIME_PARAMS)
    yield
    for g, v in saved_globals.items():
        setattr(R, g, v)
    R._REGIME_PARAMS.clear()
    R._REGIME_PARAMS.update(saved_params)


def _reset_thresholds():
    # Code-Defaults (der Zustand, in dem der Reconciler VOR dem Fix lief)
    R.DEFENSIVE_THRESHOLD = 8.0
    R.CAUTION_THRESHOLD = 4.0
    R.CRITICAL_THRESHOLD = 15.0
    R.CAUTION_EXIT = 3.5
    R.DEFENSIVE_EXIT = 7.0
    R.CRITICAL_EXIT = 13.0
    R.REGIME_SOURCE = "account"


def test_reconciler_loads_regime_config_before_update_regime():
    """Quelltext: apply_config muss VOR dem update_regime-Aufruf stehen."""
    src = _SRC.read_text(encoding="utf-8")
    # import-Alias + Aufruf existiert
    assert "_apply_regime_config" in src, "Reconciler laedt keine Regime-Config mehr"
    apply_idx = src.index("_apply_regime_config(cfg)")
    update_idx = src.index("update_regime(state_repo, current_equity)")
    assert apply_idx < update_idx, (
        "Regime-Config wird NACH update_regime geladen — der Whipsaw "
        "kommt zurueck (load-must-precede-use)"
    )


def test_config_and_default_diverge_at_dd_10pct():
    """Beleg der Divergenz: bei DD=10% sagen Code-Default und Config
    VERSCHIEDENE Regimes — genau der Punkt, an dem der Whipsaw lag."""
    _reset_thresholds()
    equity, peak = 8796.68, 9772.67  # DD 9,99% (Live-Werte 2026-10-16)

    # Code-Defaults (Reconciler VOR dem Fix) -> DEFENSIVE
    assert R.detect_regime(equity, peak, "NORMAL")[0] == "DEFENSIVE"

    # config.yaml (risk_worker + Reconciler NACH dem Fix) -> NORMAL
    R.apply_config(_load_cfg())
    assert R.DEFENSIVE_THRESHOLD == 30.0
    assert R.detect_regime(equity, peak, "NORMAL")[0] == "NORMAL"
    _reset_thresholds()


def test_update_regime_agrees_after_config_load(tmp_path, monkeypatch):
    """Funktionell: nach apply_config(cfg) liefert update_regime() das
    config-konsistente Regime. Gesunder Markt (SPY ueber SMA200, tiefer DD)
    => Marktregime NORMAL, sodass die KONTOSTUFE das Ergebnis traelgt —
    genau der Punkt, an dem der Reconciler (Code-Default 8%) und der
    risk_worker (config 30%) auseinanderliefen.
    """
    _reset_thresholds()
    # Gesunder Markt: SPY 5% ueber SMA200, nur 2% unter 52W-Hoch -> NORMAL
    monkeypatch.setattr(
        R, "market_metrics",
        lambda db, instrument_id=None: {
            "date": date.today().isoformat(), "close": 650.0, "sma200": 620.0,
            "dd_pct": 2.0, "n": 250,
        },
    )
    d = DB(db_path=tmp_path / "state.db")
    d.execute("""
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)
    d.execute("""
        CREATE TABLE equity_history (
            date TEXT PRIMARY KEY, equity_high REAL NOT NULL
        )
    """)
    # 30-Tage-Hoch = 9772.67 (wie Live), heute = 8796.68 -> DD 9,99%
    d.execute(
        "INSERT INTO equity_history (date, equity_high) VALUES (date('now','-30 days'), ?)",
        (9772.67,),
    )
    repo = StateRepo(d)
    repo.set("CURRENT_REGIME", "NORMAL")

    # NACH dem Fix: config laeden -> Konto-Stufe 30% DEFENSIVE
    R.apply_config(_load_cfg())
    regime, _ = R.update_regime(repo, 8796.68)
    # Markt NORMAL + Konto NORMAL (DD 9,99% < 30%) -> NORMAL
    assert regime == "NORMAL"
    assert repo.get("ACCOUNT_REGIME") == "NORMAL"
    assert repo.get("MARKET_REGIME") == "NORMAL"
    d.close()
    _reset_thresholds()


def test_reconciler_without_config_would_diverge(tmp_path, monkeypatch):
    """Gegenprobe: OHNE apply_config (Code-Default 8%) wird derselbe
    Equity-Punkt zur DEFENSIVE — belegt, dass genau das der Whipsaw war.
    """
    _reset_thresholds()
    monkeypatch.setattr(
        R, "market_metrics",
        lambda db, instrument_id=None: {
            "date": date.today().isoformat(), "close": 650.0, "sma200": 620.0,
            "dd_pct": 2.0, "n": 250,
        },
    )
    d = DB(db_path=tmp_path / "state.db")
    d.execute("""
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)
    d.execute("""
        CREATE TABLE equity_history (
            date TEXT PRIMARY KEY, equity_high REAL NOT NULL
        )
    """)
    d.execute(
        "INSERT INTO equity_history (date, equity_high) VALUES (date('now','-30 days'), ?)",
        (9772.67,),
    )
    repo = StateRepo(d)
    repo.set("CURRENT_REGIME", "NORMAL")

    # VOR dem Fix: KEIN apply_config -> Code-Default 8% DEFENSIVE
    regime, _ = R.update_regime(repo, 8796.68)
    # Markt NORMAL + Konto DEFENSIVE (DD 9,99% >= 8% Code-Default) -> DEFENSIVE
    assert regime == "DEFENSIVE"
    assert repo.get("ACCOUNT_REGIME") == "DEFENSIVE"
    d.close()
    _reset_thresholds()



def _load_cfg():
    import yaml
    cfg_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"
    with cfg_path.open() as fh:
        return yaml.safe_load(fh)
