"""fix/fee-tier-asx-namespace (2026-09-26): der Fee-Tier-Daempfer verfehlte .ASX.

`fix/fee-churn-minhold` (2026-09-20) sollte 2-%-Boersen im Kandidaten-Ranking
nachrangig machen (Faktor 0.70). Die Suffix-Liste stand aber in der
YFINANCE-Schreibweise (".AX"), waehrend der signal_worker das BOT-Symbol
uebergibt: `fee_tier_factor(sym, ...)` mit sym aus `eligible`.

In `instruments` stehen **437 Titel auf ".ASX" und genau EINER auf ".AX"**.
Der Daempfer verfehlte damit alle australischen Werte.

Gemessen am 2026-09-26 gegen `portfolio_snapshot.broker_fee_pct` (die
Broker-Wahrheit seit feat/broker-fee-capture) — genau drei Endungen tragen
2,0 %:

    .HK   11 Positionen  $736,45   (getroffen)
    .ASX  10 Positionen  $615,06   (VERFEHLT)
    .T     6 Positionen  $473,99   (getroffen)

Also 10 von 27 Hochgebuehren-Positionen mit voller Gewichtung — 37 %.
"""
from __future__ import annotations

import pytest

from bot.core.liquidity import FEE_TIER_DEFAULT_SUFFIXES, fee_tier_factor

CFG = {"enabled": True, "factor": 0.70, "fee_pct": 2.0}


@pytest.mark.parametrize("symbol", ["CAR.ASX", "DOW.ASX", "AIA.ASX", "REA.ASX"])
def test_asx_wird_gedaempft(symbol):
    """Der eigentliche Fehler: diese Titel liefen mit 1.00 durch."""
    assert fee_tier_factor(symbol, CFG) == 0.70


@pytest.mark.parametrize("symbol", ["0700.HK", "9531.T", "5101.T", "BHP.AX"])
def test_die_uebrigen_2pct_boersen_bleiben_gedaempft(symbol):
    assert fee_tier_factor(symbol, CFG) == 0.70


@pytest.mark.parametrize("symbol", ["MSFT", "AAPL", "SIE.DE", "VU.PA", "GFRD.L",
                                    "ASSA-B.ST", "CEM.MI"])
def test_ein_prozent_boersen_bleiben_unveraendert(symbol):
    """US und Europa zahlen 1 % — gemessen: 45 Positionen, keine Ausnahme."""
    assert fee_tier_factor(symbol, CFG) == 1.0


def test_alle_gemessenen_2pct_endungen_sind_abgedeckt():
    """Regression gegen die Broker-Wahrheit statt gegen eine Annahme."""
    gemessen = {".HK", ".ASX", ".T"}
    assert gemessen <= set(FEE_TIER_DEFAULT_SUFFIXES)


def test_asx_endet_nicht_auf_ax():
    """Die Falle, die den Fehler erzeugt hat: '.ASX'.endswith('.AX') ist False.

    Wer die Liste kuerzen will, stolpert sonst erneut darueber.
    """
    assert not ".ASX".endswith(".AX")
    assert fee_tier_factor("CAR.ASX", {**CFG, "high_fee_suffixes": [".AX"]}) == 1.0


# ── unveraendertes Verhalten ────────────────────────────────────────────────

def test_abgeschaltet_bleibt_neutral():
    assert fee_tier_factor("CAR.ASX", {**CFG, "enabled": False}) == 1.0
    assert fee_tier_factor("CAR.ASX", None) == 1.0
    assert fee_tier_factor(None, CFG) == 1.0


def test_faktor_ab_1_deaktiviert():
    """Nur < 1.0 daempft — 1.0/fehlend heisst aus."""
    assert fee_tier_factor("CAR.ASX", {**CFG, "factor": 1.0}) == 1.0


def test_eigene_liste_aus_der_config_gewinnt():
    assert fee_tier_factor("XYZ.TO", {**CFG, "high_fee_suffixes": [".TO"]}) == 0.70
    assert fee_tier_factor("CAR.ASX", {**CFG, "high_fee_suffixes": [".TO"]}) == 1.0
