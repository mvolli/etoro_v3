"""fix/crypto-entry-orders: allowEntryOrders blockt Market-Orders nicht bei Krypto."""
from bot.api.client import blocks_on_entry_orders


def test_crypto_never_blocks_on_entry_orders():
    # realer BTC-Fall: allowOpenPosition True, allowEntryOrders False, 24/7 offen
    elig = {"allowOpenPosition": True, "allowEntryOrders": False}
    assert blocks_on_entry_orders(elig, is_crypto=True) is False


def test_stock_blocks_when_open_position_false():
    # fix/entry-orders-market-proxy (2026-09-02): allowEntryOrders=false is
    # an ORDER-TYPE flag (no PENDING/LIMIT entries), NOT a market-close.
    # The only live gate left is allowOpenPosition=false.
    elig = {"allowOpenPosition": False, "allowEntryOrders": False}
    assert blocks_on_entry_orders(elig, is_crypto=False) is True


def test_stock_not_blocked_when_entry_orders_false_but_open():
    # ASX real shares at open exchange: allowOpenPosition=True,
    # allowEntryOrders=False -> must NOT block (MAD.ASX / IFT.ASX class).
    elig = {"allowOpenPosition": True, "allowEntryOrders": False}
    assert blocks_on_entry_orders(elig, is_crypto=False) is False


def test_stock_open_market_not_blocked():
    elig = {"allowOpenPosition": True, "allowEntryOrders": True}
    assert blocks_on_entry_orders(elig, is_crypto=False) is False


def test_missing_field_failopen():
    # Feld fehlt -> als offen annehmen (kein Block)
    assert blocks_on_entry_orders({}, is_crypto=False) is False
    assert blocks_on_entry_orders({}, is_crypto=True) is False
