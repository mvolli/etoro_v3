"""Tests feat/liquidity-tiering (2026-07-26) — bot/core/liquidity.py.

Market-Cap/ADV-Tier-Faktor, FX-Naeherung, Migration + Ranking-Map.
"""

import sqlite3

import pandas as pd
import pytest

from bot.core import liquidity as liq


# ── liquidity_factor: Tier-Grenzen ───────────────────────────────────────────

def test_factor_unknown_is_neutral():
    assert liq.liquidity_factor(None, None) == 1.0
    assert liq.liquidity_factor(0, 0) == 1.0


@pytest.mark.parametrize("mc,expected", [
    (50e9, 1.1),    # Mega-Cap
    (10e9, 1.1),    # Grenze inklusiv
    (5e9, 1.0),     # Mid
    (1e9, 0.85),    # Small
    (100e6, 0.65),  # Micro
])
def test_factor_market_cap_tiers(mc, expected):
    assert liq.liquidity_factor(mc, None) == expected


@pytest.mark.parametrize("adv,expected", [
    (100e6, 1.05),  # tiefe Liquiditaet
    (10e6, 1.0),    # ok
    (2e6, 0.9),     # duenn
    (400e3, 0.7),   # illiquid
])
def test_factor_adv_tiers(adv, expected):
    assert liq.liquidity_factor(None, adv) == expected


def test_factor_worse_value_wins():
    # Mega-Cap mit ausgetrocknetem Volumen → ADV-Malus gewinnt
    assert liq.liquidity_factor(50e9, 400e3) == 0.7
    # Micro-Cap mit Volumen-Spike bleibt Micro-Cap
    assert liq.liquidity_factor(100e6, 100e6) == 0.65


def test_factor_clamped_to_bounds():
    assert liq.FACTOR_FLOOR <= liq.liquidity_factor(1.0, 1.0) <= liq.FACTOR_CAP
    # Mega-Cap (1.1) + tiefe Liquiditaet (1.05): min-Regel → 1.05, unter Cap
    assert liq.liquidity_factor(1e12, 1e12) == 1.05


# ── currency_factor ──────────────────────────────────────────────────────────

def test_currency_factor_gbx_and_defaults():
    assert liq.currency_factor("KRS.L") == pytest.approx(0.0127)
    assert liq.currency_factor("KTA.DE") == pytest.approx(1.08)
    assert liq.currency_factor("7203.T") == pytest.approx(0.0067)
    # US ohne Suffix und unbekannte Suffixe: 1.0
    assert liq.currency_factor("NVDA") == 1.0
    assert liq.currency_factor("FOO.XYZ") == 1.0
    assert liq.currency_factor("") == 1.0


def test_currency_factor_prevents_jpy_overstatement():
    """7203.T: 10M Stueck x 3000 JPY waere als 'USD' 30 Mrd — real ~200M."""
    df = pd.DataFrame({
        "Close": [3000.0] * 25,
        "Volume": [10_000_000] * 25,
    })
    adv = liq.compute_adv_usd(df, "7203.T")
    assert adv == pytest.approx(10_000_000 * 3000.0 * 0.0067)
    assert adv < 500e6  # nicht 30 Mrd


# ── compute_adv_usd ──────────────────────────────────────────────────────────

def test_compute_adv_basic_and_broken():
    df = pd.DataFrame({"Close": [10.0] * 30, "Volume": [200_000] * 30})
    assert liq.compute_adv_usd(df, "AAPL") == pytest.approx(2_000_000.0)
    # Preis-Override wird genutzt
    assert liq.compute_adv_usd(df, "AAPL", price=20.0) == pytest.approx(4_000_000.0)
    # Kaputte Daten → None (Aufrufer entscheidet)
    assert liq.compute_adv_usd(pd.DataFrame({"Close": [], "Volume": []}), "X") is None
    assert liq.compute_adv_usd(None, "X") is None
    df_zero = pd.DataFrame({"Close": [10.0] * 5, "Volume": [0] * 5})
    assert liq.compute_adv_usd(df_zero, "X") is None


# ── DB: Migration + load_liquidity_map ───────────────────────────────────────

class _FakeDB:
    """Minimaler DB-Wrapper um sqlite3 im Speicher (execute/fetchall-API)."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE instruments (instrument_id INTEGER PRIMARY KEY, symbol TEXT)"
        )

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def fetchall(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()


@pytest.fixture()
def fake_db(monkeypatch):
    monkeypatch.setattr(liq, "_COLUMNS_READY", False)
    return _FakeDB()


def test_migration_idempotent_and_updates(fake_db, monkeypatch):
    liq.ensure_liquidity_columns(fake_db)
    monkeypatch.setattr(liq, "_COLUMNS_READY", False)
    liq.ensure_liquidity_columns(fake_db)  # zweiter Lauf darf nicht werfen

    fake_db.execute("INSERT INTO instruments (instrument_id, symbol) VALUES (1, 'NVDA')")
    liq.update_adv(fake_db, 1, 75e6)
    liq.update_market_cap(fake_db, 1, 3e12)
    row = fake_db.fetchall("SELECT adv_usd, market_cap FROM instruments WHERE instrument_id=1")[0]
    assert row[0] == 75e6 and row[1] == 3e12


def test_load_liquidity_map(fake_db):
    liq.ensure_liquidity_columns(fake_db)
    fake_db.execute(
        "INSERT INTO instruments (instrument_id, symbol, market_cap, adv_usd) "
        "VALUES (1, 'NVDA', 3e12, 75e6), (2, 'MICRO.L', 50e6, 200e3), (3, 'UNKNOWN', NULL, NULL)"
    )
    m = liq.load_liquidity_map(fake_db, [1, 2, 3, 999])
    assert m[1] == 1.05  # min(1.1 Mega-Cap, 1.05 tiefe Liquiditaet)
    assert m[2] == 0.65  # min(0.65 Micro-Cap, 0.7 illiquid) = 0.65
    assert m[3] == 1.0
    assert 999 not in m


def test_load_liquidity_map_fails_open(fake_db):
    assert liq.load_liquidity_map(fake_db, []) == {}

    class _BrokenDB:
        def execute(self, *a, **k):
            raise RuntimeError("boom")

        def fetchall(self, *a, **k):
            raise RuntimeError("boom")

    assert liq.load_liquidity_map(_BrokenDB(), [1, 2]) == {}


# ── Krypto-ADV (fix/crypto-adv-double-count, 2026-08-28) ────────────────────
# yfinance meldet bei …-USD-Paaren den Dollar-Umsatz, nicht die Stueckzahl.
# Der Preis darf dann nicht noch einmal hineinmultipliziert werden.
# Belegt an den Rohdaten: BTC-USD avg_Volume 31,5 Mrd bei Preis 79.821 $ —
# als Stueckzahl waeren das mehr Bitcoin pro Tag als je existieren werden.


def _df(volume, close):
    return pd.DataFrame({"Close": [close] * 20, "Volume": [volume] * 20})


def test_krypto_volumen_wird_nicht_mit_dem_preis_multipliziert():
    df = _df(31_524_508_672.0, 79_821.93)
    adv = liq.compute_adv_usd(df, "BTC", price=79_821.93, yf_symbol="BTC-USD")
    assert adv == pytest.approx(31_524_508_672.0)


def test_aktien_verhalten_bleibt_unveraendert():
    df = _df(100_000.0, 20.0)
    assert liq.compute_adv_usd(df, "AAPL", price=20.0,
                               yf_symbol="AAPL") == pytest.approx(2_000_000.0)


def test_guenstiger_altcoin_faellt_nicht_mehr_unter_die_schwelle():
    """Der eigentliche Schaden: Preis < 1 DRUECKTE den Wert unter MIN_ADV_USD.

    ADA bei 0,21 $ mit 436 Mio $ Umsatz landete bei 91 Mio (noch ueber der
    Schwelle), aber ein Coin zu 0,01 $ mit 2 Mio $ Umsatz kam auf 20.000 und
    seine BUY-Signale wurden als illiquide verworfen.
    """
    df = _df(2_000_000.0, 0.01)
    alt = 2_000_000.0 * 0.01                      # so rechnete es vorher
    neu = liq.compute_adv_usd(df, "TINY", price=0.01, yf_symbol="TINY-USD")
    assert alt < liq.MIN_ADV_USD                  # waere verworfen worden
    assert neu == pytest.approx(2_000_000.0)
    assert neu > liq.MIN_ADV_USD                  # bleibt jetzt drin


def test_ohne_yf_symbol_faellt_es_auf_symbol_zurueck():
    df = _df(1_000.0, 5.0)
    assert liq.compute_adv_usd(df, "ETH-USD", price=5.0) == pytest.approx(1_000.0)
    assert liq.compute_adv_usd(df, "MSFT", price=5.0) == pytest.approx(5_000.0)


def test_erkennung_der_quote_waehrung():
    assert liq.is_quote_currency_volume("BTC-USD")
    assert liq.is_quote_currency_volume("ada-usd")
    assert not liq.is_quote_currency_volume("AAPL")
    assert not liq.is_quote_currency_volume("7203.T")
    assert not liq.is_quote_currency_volume(None)
