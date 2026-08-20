"""Unit tests fuer fix/forex-sector-unknown (2026-08-20).

yfinance kennt fuer Forex/Rohstoffe/Indizes/Krypto keine Sektoren — das
Sync-Script markierte sie als 'unknown' und der Hauptkonto-Report zeigte
" Sektor noch nicht abgerufen" ($4.766 = 67.3% des Equity, gemessen
2026-08-20). Loesung: Sektoren aus asset_class ableiten
(bot.core.sector_taxonomy), Sync-Script ruft _resolve_sector auf.
"""
from __future__ import annotations

import pytest

from bot.core.sector_taxonomy import ASSET_CLASS_SECTOR, derive_asset_class_sector


# ── derive_asset_class_sector (pure Function) ────────────────────────────────

@pytest.mark.parametrize("asset_class,expected", [
    ("forex", "FOREX"),
    ("commodity", "COMMODITY"),
    ("index", "INDEX"),
    ("crypto", "CRYPTO"),
])
def test_asset_classes_leiten_sektor_ab(asset_class, expected):
    assert derive_asset_class_sector(asset_class) == expected


@pytest.mark.parametrize("asset_class", [
    "stock", "etf", None, "", "  ", "unknown", "bond",
])
def test_stock_etf_und_unbekannt_gehen_in_yfinance_pfad(asset_class):
    """stock/etf behalten die feine Yahoo-Taxonomie; Unbekanntes darf nicht
    erfinden — None = bisheriges Verhalten exakt erhalten."""
    assert derive_asset_class_sector(asset_class) is None


def test_case_insensitiv_und_whitespace_robust():
    assert derive_asset_class_sector("Forex") == "FOREX"
    assert derive_asset_class_sector("  CRYPTO ") == "CRYPTO"


def test_taxonomie_deckt_genau_die_yfinance_blind_klassen_ab():
    """Nur Klassen ohne Yahoo-Sektoren duerfen abgeleitet werden — alles
    andere wuerde echte yfinance-Werte ueberschreiben."""
    assert set(ASSET_CLASS_SECTOR) == {"forex", "commodity", "index", "crypto"}


# ── sync_instrument_sectors._resolve_sector (Script-Integration) ─────────────

def _load_sync():
    """scripts/ ist kein Paket — per Datei-Pfad laden."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent.parent / "scripts" / "sync_instrument_sectors.py"
    spec = importlib.util.spec_from_file_location("sync_instrument_sectors", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_sector_verwendet_asset_class_vor_yfinance():
    sync = _load_sync()
    row = {"symbol": "EURUSD", "yfinance_symbol": None, "asset_class": "forex"}
    sector, industry = sync._resolve_sector(row)
    assert sector == "FOREX"
    assert industry is None


def test_resolve_sector_faellt_fuer_stocks_auf_yfinance_zurueck(monkeypatch):
    """stock ohne asset-class-Derivation MUSS den yfinance-Pfad nehmen —
    sonst wuerden echte Yahoo-Sektoren nie mehr gefuellt."""
    sync = _load_sync()
    calls = []

    def fake_fetch(yf_symbol):
        calls.append(yf_symbol)
        return ("Financial Services", "Banks")

    monkeypatch.setattr(sync, "_fetch_sector", fake_fetch)
    row = {"symbol": "JPM", "yfinance_symbol": "JPM", "asset_class": "stock"}
    sector, industry = sync._resolve_sector(row)
    assert (sector, industry) == ("Financial Services", "Banks")
    assert calls == ["JPM"]


def test_resolve_sector_verwendet_yfinance_symbol_namespace(monkeypatch):
    """NAMESPACE-Invariante: yfinance IMMER mit yfinance_symbol fragen."""
    sync = _load_sync()
    seen = []
    monkeypatch.setattr(sync, "_fetch_sector", lambda s: (seen.append(s) or ("Energy", None)))
    row = {"symbol": "836.HK", "yfinance_symbol": "0836.HK", "asset_class": "stock"}
    sync._resolve_sector(row)
    assert seen == ["0836.HK"]


def test_resolve_sector_ohne_asset_class_verhaelt_sich_wie_bisher(monkeypatch):
    """Rueckwaerts-Kompatibilitaet: Zeilen ohne asset_class laufen exakt den
    alten Pfad (yfinance, Fallback auf symbol)."""
    sync = _load_sync()
    seen = []
    monkeypatch.setattr(sync, "_fetch_sector", lambda s: (seen.append(s) or (None, None)))
    row = {"symbol": "KO", "yfinance_symbol": None, "asset_class": None}
    sector, industry = sync._resolve_sector(row)
    assert (sector, industry) == (None, None)
    assert seen == ["KO"]
