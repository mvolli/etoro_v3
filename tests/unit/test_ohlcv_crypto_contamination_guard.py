"""fix/crypto-symbol-contamination (2026-08-21): Unit tests for the
Asset-Class-Sanity-Guard in ohlcv_cache.py.

Root cause: a backfill script (25.06.) mapped 180 stock/etf instruments
per Name-Fuzzy-Match to real crypto tickers (STMicroelectronics→TRX-USD,
First Bancorp→BNT-USD, …). The price path wrote crypto prices into their
ohlcv_daily tables. The guard detects this at fetch time, NULLs the
yfinance_symbol (self-heal), and skips the fetch.

Krypto-ETPs (CoinShares/Grayscale/21Shares Bitcoin/Ethereum/Solana) are
the legitimate exception — there the crypto ticker IS the reference.
Commodity-Futures/FX with '-USD' (MICRO WT-USD, RUBBER-USD) are NOT
flagged.
"""
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

from bot.core.ohlcv_cache import (
    _is_crypto_price_ticker,
    _is_crypto_etp,
    _asset_class_price_mismatch,
    ensure_ohlcv,
    bulk_ensure_ohlcv,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE instruments (
            instrument_id INTEGER PRIMARY KEY,
            symbol TEXT,
            yfinance_symbol TEXT,
            asset_class TEXT,
            name TEXT,
            yahoo_status TEXT DEFAULT 'ok',
            yahoo_fail_count INTEGER DEFAULT 0,
            last_updated TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE ohlcv_daily (
            instrument_id INTEGER,
            date TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume INTEGER, adjusted_close REAL
        )
    """)
    yield c
    c.close()


def _add(conn, iid=1, symbol="AAPL", yf="AAPL", ac="stock", name="Apple Inc.",
         yahoo_status="ok"):
    c = conn.cursor()
    c.execute(
        "INSERT INTO instruments "
        "(instrument_id, symbol, yfinance_symbol, asset_class, name, yahoo_status) "
        "VALUES (?,?,?,?,?,?)",
        (iid, symbol, yf, ac, name, yahoo_status),
    )
    conn.commit()


# ── _is_crypto_price_ticker ────────────────────────────────────────────────────

def test_crypto_ticker_true_all_bases():
    for t in ("BTC-USD", "ETH-USD", "SOL-USD", "BNT-USD", "TRX-USD",
              "GALA-USD", "CELO-USD", "XLM-USD", "AVAX-USD", "ATOM-USD",
              "DOGE-USD", "DOT-USD", "LTC-USD", "LINK-USD", "SHIB-USD",
              "MIOTA-USD"):
        assert _is_crypto_price_ticker(t), f"expected {t} → True"

def test_crypto_ticker_case_insensitive():
    assert _is_crypto_price_ticker("btc-usd")
    assert _is_crypto_price_ticker("Btc-Usd")
    assert _is_crypto_price_ticker("Trx-Usd")

def test_crypto_ticker_false_commodity_usd():
    assert not _is_crypto_price_ticker("MICRO WT-USD")
    assert not _is_crypto_price_ticker("RUBBER-USD")
    assert not _is_crypto_price_ticker("NATURAL GAS-USD")

def test_crypto_ticker_false_fx_pair():
    assert not _is_crypto_price_ticker("GOLD/EUR")
    assert not _is_crypto_price_ticker("EUR/USD")

def test_crypto_ticker_false_plain():
    assert not _is_crypto_price_ticker("AAPL")
    assert not _is_crypto_price_ticker("MSFT")
    assert not _is_crypto_price_ticker("MICRO WT-USD")
    # NOTE: a bare crypto base ticker (no -USD) is ALSO recognized — the
    # base-ticker set is matched after stripping '-USD', so 'BTC' alone fires.
    assert _is_crypto_price_ticker("BTC")

def test_crypto_ticker_false_empty_none():
    assert not _is_crypto_price_ticker("")
    assert not _is_crypto_price_ticker(None)

# ── _is_crypto_etp ─────────────────────────────────────────────────────────────

def test_crypto_etp_true():
    for n in ("CoinShares Bitcoin ETP", "Grayscale Bitcoin Trust",
              "21Shares Ethereum ETF", "iShares Bitcoin ETF",
              "Teucrium Solana Fund", "Fidelity Bitcoin Fund",
              "Van Eck Ethereum ETP", "Hashdex Bitcoin ETF",
              "Winklev Coin ETP", "ABRDN Solana Fund"):
        assert _is_crypto_etp(n), f"expected {n!r} → True"

def test_crypto_etp_false():
    assert not _is_crypto_etp("STMicroelectronics")
    assert not _is_crypto_etp("First Bancorp")
    assert not _is_crypto_etp("Apple Inc.")
    assert not _is_crypto_etp(None)
    assert not _is_crypto_etp("")
    # 'Gold ETP' has a name-keyword AND an asset word ("etp") → still True.
    # The exemption is name-based, so this is correct: it looks like an ETP.
    assert _is_crypto_etp("CoinShares Gold ETP")

def test_crypto_etp_case_insensitive():
    assert _is_crypto_etp("COINSHARES BITCOIN ETP")
    assert _is_crypto_etp("coinshares ethereum etp")

# ── _asset_class_price_mismatch ────────────────────────────────────────────────

def test_mismatch_stock_crypto_true(conn):
    _add(conn, 1, "STM", "TRX-USD", "stock", "STMicroelectronics")
    assert _asset_class_price_mismatch(conn, 1, "TRX-USD") is True

def test_mismatch_etf_crypto_true(conn):
    _add(conn, 2, "XX", "BTC-USD", "etf", "Some ETF")
    assert _asset_class_price_mismatch(conn, 2, "BTC-USD") is True

def test_mismatch_commodity_false(conn):
    _add(conn, 3, "MWT", "TRX-USD", "commodity", "Micro Wheat Futures")
    assert _asset_class_price_mismatch(conn, 3, "TRX-USD") is False

def test_mismatch_forex_false(conn):
    _add(conn, 4, "EURUSD", "EUR/USD", "forex", "EUR/USD")
    assert _asset_class_price_mismatch(conn, 4, "EUR/USD") is False

def test_mismatch_crypto_asset_class_false(conn):
    _add(conn, 5, "BTC", "BTC-USD", "crypto", "Bitcoin")
    assert _asset_class_price_mismatch(conn, 5, "BTC-USD") is False

def test_mismatch_non_crypto_ticker_false(conn):
    _add(conn, 6, "AAPL", "AAPL", "stock", "Apple Inc.")
    assert _asset_class_price_mismatch(conn, 6, "AAPL") is False

def test_mismatch_commodity_usd_ticker_false(conn):
    _add(conn, 7, "MWT", "MICRO WT-USD", "stock", "Micro Wheat")
    assert _asset_class_price_mismatch(conn, 7, "MICRO WT-USD") is False

def test_mismatch_crypto_etp_name_exemption(conn):
    _add(conn, 8, "BSCL", "BTC-USD", "etf", "CoinShares Bitcoin ETP")
    assert _asset_class_price_mismatch(conn, 8, "BTC-USD") is False

def test_mismatch_unknown_instrument_false(conn):
    assert _asset_class_price_mismatch(conn, 999, "BTC-USD") is False

# ── ensure_ohlcv guard integration ─────────────────────────────────────────────

def test_ensure_ohlcv_nulls_contaminated_symbol(conn):
    _add(conn, 1, "STM", "TRX-USD", "stock", "STMicroelectronics")
    has_data, days = ensure_ohlcv(conn, 1, "TRX-USD")
    assert has_data is False
    assert days == 0
    # yfinance_symbol must be NULLed (self-heal)
    row = conn.execute(
        "SELECT yfinance_symbol FROM instruments WHERE instrument_id = 1"
    ).fetchone()
    assert row[0] is None, "yfinance_symbol must be NULL after guard"

def test_ensure_ohlcv_guard_skips_fetch(conn):
    _add(conn, 2, "STM", "TRX-USD", "stock", "STMicroelectronics")
    with patch("bot.core.ohlcv_cache.fetch_ohlcv") as mock_fetch:
        ensure_ohlcv(conn, 2, "TRX-USD")
        mock_fetch.assert_not_called(), \
            "guard must return before calling fetch_ohlcv"

def test_ensure_ohlcv_non_crypto_symbol_no_null(conn):
    _add(conn, 3, "AAPL", "AAPL", "stock", "Apple Inc.")
    with patch("bot.core.ohlcv_cache.fetch_ohlcv") as mock_fetch:
        mock_fetch.return_value = (None, False, None)
        ensure_ohlcv(conn, 3, "AAPL")
    # yfinance_symbol must NOT be NULLed
    row = conn.execute(
        "SELECT yfinance_symbol FROM instruments WHERE instrument_id = 3"
    ).fetchone()
    assert row[0] == "AAPL", "non-contaminated symbol must not be NULLed"

# ── bulk_ensure_ohlcv or-fallback ─────────────────────────────────────────────

def test_bulk_ohlcv_null_symbol_falls_back_to_raw(conn):
    """A NULLed yfinance_symbol (key present, value None) must fall back
    to the raw eToro symbol via the `or`-fallback, not get skipped."""
    _add(conn, 1, "AAPL", None, "stock", "Apple Inc.")
    with patch("bot.core.ohlcv_cache.ensure_ohlcv") as mock_eo:
        mock_eo.return_value = (True, 50)
        results = bulk_ensure_ohlcv(
            conn, [{"instrument_id": 1, "yfinance_symbol": None, "symbol": "AAPL"}]
        )
    # The `or`-fallback must pass "AAPL" (not None) to ensure_ohlcv
    mock_eo.assert_called_once()
    call_args = mock_eo.call_args
    # 3rd positional arg is yf_symbol
    yf_arg = call_args[0][2]
    assert yf_arg == "AAPL", f"expected 'AAPL', got {yf_arg!r}"

def test_bulk_ohlcv_both_null_yields_no_yf_symbol(conn):
    """If both yfinance_symbol AND symbol are None/empty → no_yf_symbol error."""
    _add(conn, 2, None, None, "stock", None)
    with patch("bot.core.ohlcv_cache.ensure_ohlcv") as mock_eo:
        mock_eo.return_value = (True, 50)
        results = bulk_ensure_ohlcv(
            conn, [{"instrument_id": 2, "yfinance_symbol": None, "symbol": None}]
        )
    mock_eo.assert_not_called()
    assert results[2]["error"] == "no_yf_symbol"

def test_bulk_ohlcv_delisted_skipped(conn):
    """Delisted instruments are skipped before the symbol resolution."""
    _add(conn, 3, "DEAD", "DEAD.X", "stock", "Dead Corp", yahoo_status="delisted")
    with patch("bot.core.ohlcv_cache.ensure_ohlcv") as mock_eo:
        mock_eo.return_value = (True, 50)
        results = bulk_ensure_ohlcv(
            conn, [{"instrument_id": 3, "yfinance_symbol": "DEAD.X", "symbol": "DEAD"}]
        )
    mock_eo.assert_not_called()
    assert results[3]["error"] == "yahoo_delisted"


# ── fix/crypto-guard-selfheal-loop (2026-08-21) ───────────────────────────────
# Der Guard NULLt `yfinance_symbol`, damit der naechste Lauf ueber den rohen
# eToro-Symbol-Kandidaten neu aufloest. Ist das Symbol SELBST der auffaellige
# Ticker, liefert der Fallback denselben Wert zurueck — der Guard feuert
# erneut, das Instrument bekaeme nie Daten und `yahoo_fail_count` wuerde jeden
# Zyklus auf 0 zurueckgesetzt. Ein Fuzzy-Match-Fehlgriff erzeugt IMMER einen
# abweichenden String, also ist Gleichheit ein sicheres Ausschlusskriterium.

def test_symbol_gleich_yfinance_symbol_ist_ausgenommen(conn):
    """ATOM = echter NASDAQ-Ticker (Atomera) und zugleich Cosmos-Krypto."""
    _add(conn, iid=1, symbol="ATOM", yf="ATOM", ac="stock",
         name="Atomera Incorporated")
    assert _asset_class_price_mismatch(conn, 1, "ATOM") is False


def test_fuzzy_leiche_feuert_weiterhin(conn):
    """Gegenprobe: abweichendes Symbol = echter Fehlgriff, muss feuern."""
    _add(conn, iid=2, symbol="LLTC", yf="LTC", ac="stock",
         name="Linear Technology Corporation")
    assert _asset_class_price_mismatch(conn, 2, "LTC") is True


def test_ausnahme_ist_gross_klein_unabhaengig(conn):
    _add(conn, iid=3, symbol="atom", yf="ATOM", ac="stock", name="Atomera Inc")
    assert _asset_class_price_mismatch(conn, 3, "ATOM") is False


def test_keine_dauerschleife_nach_null(conn, monkeypatch):
    """Der eigentliche Schaden: NULLen darf sich nicht endlos wiederholen.

    Simuliert den Fallback aus bulk_ensure_ohlcv (`yfinance_symbol or symbol`)
    ueber mehrere Laeufe. Ohne die Ausnahme feuerte der Guard in JEDEM Lauf.
    """
    _add(conn, iid=4, symbol="ATOM", yf="ATOM", ac="stock", name="Atomera Inc")
    feuer = 0
    for _ in range(5):
        row = conn.execute(
            "SELECT yfinance_symbol, symbol FROM instruments WHERE instrument_id = 4"
        ).fetchone()
        yf_sym = row["yfinance_symbol"] or row["symbol"]     # Fallback wie in bulk
        if _asset_class_price_mismatch(conn, 4, yf_sym):
            feuer += 1
            conn.execute("UPDATE instruments SET yfinance_symbol = NULL "
                         "WHERE instrument_id = 4")
            conn.commit()
    assert feuer == 0, f"Guard feuerte {feuer}x auf einen legitimen Ticker"
