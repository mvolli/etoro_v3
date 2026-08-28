"""Regime-Gate im Execution-Worker (fix/execution-defensive-sizing-check).

Vorher warf der Worker DEFENSIVE und CRITICAL pauschal ab. Damit war jeder
Parameter, den _REGIME_PARAMS fuer DEFENSIVE definiert, dort toter Code — am
2026-08-28 wurden 26 genehmigte Trades in Folge verworfen, jeder Zyklus neu.

Der Sicherheitszweck bleibt: ein unter lockererem Regime dimensionierter Trade
darf nicht in einem strengeren ausgefuehrt werden. Das prueft jetzt
max_trade_pct statt eines Pauschalstopps.
"""
import pathlib

import pytest

from bot.core.regime import get_regime_params


SRC = (pathlib.Path(__file__).resolve().parents[2]
       / "src" / "bot" / "workers" / "execution_worker.py").read_text(encoding="utf-8")


def test_critical_bleibt_harter_stopp():
    assert 'if regime == "CRITICAL":' in SRC, (
        "CRITICAL (DD >= 15 %) muss ein bedingungsloser Stopp bleiben"
    )


def test_defensive_wird_nicht_mehr_pauschal_abgeworfen():
    assert 'if regime in ("DEFENSIVE", "CRITICAL"):' not in SRC, (
        "Der Pauschalstopp fuer DEFENSIVE ist wieder da — damit ist jeder "
        "DEFENSIVE-Parameter in _REGIME_PARAMS erneut wirkungslos"
    )
    assert 'if regime == "DEFENSIVE":' in SRC


def test_defensive_prueft_gegen_max_trade_pct():
    assert "max_trade_pct" in SRC, (
        "Die DEFENSIVE-Pruefung muss die Trade-Groesse gegen das Regime-Limit "
        "halten, sonst faellt der Schutz gegen Eskalation zwischen Freigabe "
        "und Ausfuehrung weg"
    )


@pytest.mark.parametrize(
    "equity, amount, erwartet_abgelehnt",
    [
        (8387.66,  81.00, False),   # in DEFENSIVE dimensioniert -> passt
        (8387.66, 251.62, False),   # exakt 3.0 % -> Grenze erlaubt
        (8387.66, 300.00, True),    # unter NORMAL (5 %) dimensioniert -> zu gross
        (0.0,     999.00, False),   # Equity unbekannt -> Pruefung entfaellt
    ],
)
def test_groessenlogik(equity, amount, erwartet_abgelehnt):
    """Bildet die Bedingung des Workers nach: _eq > 0 and amount_usd > _cap."""
    cap = equity * float(get_regime_params("DEFENSIVE")["max_trade_pct"]) / 100.0
    abgelehnt = equity > 0 and amount > cap
    assert abgelehnt is erwartet_abgelehnt


def test_defensive_max_trade_pct_ist_strenger_als_normal():
    """Sonst waere die Pruefung wirkungslos."""
    assert (get_regime_params("DEFENSIVE")["max_trade_pct"]
            < get_regime_params("NORMAL")["max_trade_pct"])
