"""Signal-Floor vs. Dust-Floor (fix/dust-floor 2026-08-28).

Im Wert min_buy_usd steckten zwei gegenlaeufige Bedeutungen. Diese Tests
halten die Trennung fest — vor allem die Kopplung an den Parity-Korridor,
denn genau die geht bei einer "Verschaerfung" als erstes verloren.
"""
import re
from pathlib import Path

import pytest

from bot.core.regime import (
    SIZING_PARITY_FLOOR,
    _REGIME_PARAMS,
    dust_floor_usd,
    get_regime_params,
)

REGIMES = ("NORMAL", "CAUTION", "DEFENSIVE", "CRITICAL")


# ─── Signal-Floor: unveraendert regimeabhaengig ──────────────────────────────

def test_signal_floor_werte():
    """Die Leiter selbst bleibt, was sie war — nur der Name ist eindeutig."""
    assert [_REGIME_PARAMS[r]["signal_floor_usd"] for r in REGIMES] == [
        50.0, 75.0, 100.0, 150.0
    ]


def test_min_buy_usd_bleibt_als_alias():
    """Altleser (trade_veto_worker, externe Skripte) duerfen nicht brechen."""
    for r in REGIMES:
        p = get_regime_params(r)
        assert p["min_buy_usd"] == p["signal_floor_usd"]


# ─── Dust-Floor: Broker-Oekonomie am Kettenende ──────────────────────────────

def test_dust_floor_werte():
    assert dust_floor_usd("NORMAL", 50.0) == 50.0
    assert dust_floor_usd("CAUTION", 50.0) == 50.0
    assert dust_floor_usd("DEFENSIVE", 50.0) == 60.0
    assert dust_floor_usd("CRITICAL", 50.0) == 90.0


def test_dust_floor_nie_unter_broker_untergrenze():
    """Der Config-Wert ist die absolute Untergrenze, egal wie mild das Regime."""
    for r in REGIMES:
        assert dust_floor_usd(r, 80.0) >= 80.0


def test_dust_floor_unbekanntes_regime_faellt_auf_normal_zurueck():
    """Fail-safe: ein unbekannter Regime-String darf nicht KeyError werfen."""
    assert dust_floor_usd("QUATSCH", 50.0) == dust_floor_usd("NORMAL", 50.0)


# ─── Die Invariante, um die es geht ──────────────────────────────────────────

@pytest.mark.parametrize("regime", REGIMES)
def test_dust_floor_schnuert_den_parity_korridor_nicht_zu(regime):
    """KERN-INVARIANTE.

    Die ATR-Risk-Parity skaliert absichtlich bis SIZING_PARITY_FLOOR herunter.
    Liegt der Dust-Floor ueber signal_floor * SIZING_PARITY_FLOOR, wird genau
    diese Absicht wieder aufgehoben — ein Trade, der den Signal-Floor sauber
    passiert hat, scheiterte dann allein an der Volatilitaetsanpassung.

    Gemessen ueber die 30 Tage bis 2026-08-28: ein Dust-Floor auf Hoehe des
    Regime-Floors haette 54 von 222 Trades (24.3 %) verworfen — jene Menge,
    die als einzige positiv abschloss (n=38, WR 39.5 %, +3.55 USD, gegen
    -111.83 USD im Rest). Wer diesen Test rot macht, baut genau das wieder ein.
    """
    signal_floor = _REGIME_PARAMS[regime]["signal_floor_usd"]
    hoechstens = signal_floor * SIZING_PARITY_FLOOR
    # Ausnahme nach oben nur durch die Broker-Untergrenze selbst.
    assert dust_floor_usd(regime, 0.0) <= hoechstens + 1e-9


def test_dom_st_regression():
    """Trade #1750: DOM.ST, 2026-08-28 12:33, DEFENSIVE.

        Basis -> Kelly k=0.63: $209.21 -> $131.66
        ATR-SL 5.02 % statt 3.00 % -> Risk-Parity x0.60 -> $79.00

    $79.00 lag unter dem Signal-Floor (100) und wurde trotzdem genehmigt,
    weil am Kettenende nur das globale check_min_buy_gate ($50) stand.
    Erwartet: der Signal-Floor greift NICHT (er prueft die Groesse VOR der
    Risk-Parity, $131.66 >= 100 ist korrekt), der Dust-Floor laesst $79.00
    passieren, und ein wirklich unwirtschaftlicher Betrag wird gefangen.
    """
    vor_parity = 131.66
    nach_parity = 79.00
    signal_floor = _REGIME_PARAMS["DEFENSIVE"]["signal_floor_usd"]
    dust = dust_floor_usd("DEFENSIVE", 50.0)

    assert vor_parity >= signal_floor      # P1 zu Recht passiert
    assert nach_parity >= dust             # Endbetrag wirtschaftlich
    assert nach_parity < signal_floor      # und der alte Floor haette geblockt
    assert 45.00 < dust                    # Dust faengt echten Staub


# ─── Struktur: der Floor muss am ENDE der Kette stehen ───────────────────────

SW_SRC = (Path(__file__).resolve().parents[2]
          / "src/bot/workers/signal_worker.py").read_text(encoding="utf-8")


def test_dust_floor_pruefung_steht_vor_dem_order_create():
    """Der Sinn von "am Ende der Kette" ist, dass KEIN Multiplikator folgt.

    feat/deploy-idle-cash (2026-08-29): die Pruefung laeuft jetzt durch
    _deploy_bump_or_reject (Bump statt Reject). Die Invariante bleibt:
    nach der LETZTEN DUST-Floor-Entscheidung darf bis zur DB-Insertion
    kein Multiplikator/Clamp/Reduktion mehr greifen — der Bump-Writeback
    (buy_amount = _dep_amt) ist die letzte Groessenentscheidung.
    """
    pruefung = SW_SRC.rindex("if buy_amount < dust_floor:")
    create = SW_SRC.index("trade_id = trade_repo.create(")
    assert pruefung < create, "Dust-Floor-Pruefung liegt nicht mehr vor der Order"
    dazwischen = SW_SRC[pruefung:create]
    # Alle buy_amount-Zuweisungen (Statements) zwischen Pruefung und Order
    # muessen der Bump-Writeback sein — kein *=, round, min, max, Daempfer.
    # (Keyword-Args `buy_amount=...` in Calls haben kein Whitespace nach `=`.)
    zuweisungen = re.findall(r"^\s*buy_amount\s*=\s+(\S+)", dazwischen, re.M)
    for z in zuweisungen:
        assert z.strip() == "_dep_amt", (
            f"buy_amount wird NACH der DUST-Floor-Pruefung anders als "
            f"Bump-Writeback veraendert: 'buy_amount = {z.strip()}' — "
            "die Pruefung muss am Ende der Sizing-Kette bleiben")


def test_parity_faktor_nicht_hartkodiert():
    """Der 0.6 im signal_worker und der in regime.py muessen derselbe sein."""
    assert "max(_sl_default / _sl_pct_final, _PARITY_FLOOR)" in SW_SRC
    assert not re.search(r"max\(_sl_default / _sl_pct_final, 0\.6\)", SW_SRC)
