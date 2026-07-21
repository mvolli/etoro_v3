"""fix/crypto-entry-orders: allowEntryOrders blockt Market-Orders nicht bei Krypto."""
from bot.api.client import blocks_on_entry_orders


def test_crypto_never_blocks_on_entry_orders():
    # realer BTC-Fall: allowOpenPosition True, allowEntryOrders False, 24/7 offen
    elig = {"allowOpenPosition": True, "allowEntryOrders": False}
    assert blocks_on_entry_orders(elig, is_crypto=True) is False


def test_stock_blocks_when_entry_orders_false():
    # Aktie bei geschlossener Boerse: allowEntryOrders=false -> blocken (Marktzeit-Proxy)
    elig = {"allowOpenPosition": True, "allowEntryOrders": False}
    assert blocks_on_entry_orders(elig, is_crypto=False) is True


def test_stock_open_market_not_blocked():
    elig = {"allowOpenPosition": True, "allowEntryOrders": True}
    assert blocks_on_entry_orders(elig, is_crypto=False) is False


def test_missing_field_failopen():
    # Feld fehlt -> als offen annehmen (kein Block)
    assert blocks_on_entry_orders({}, is_crypto=False) is False
    assert blocks_on_entry_orders({}, is_crypto=True) is False
