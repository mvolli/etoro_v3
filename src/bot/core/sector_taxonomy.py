"""Sektor-Taxonomie fuer asset-class-abgeleitete Sektoren (2026-08-20).

WARUM: yfinance kennt fuer Forex, Rohstoffe, Indizes und Krypto KEINE
Sektoren — das Sync-Script (scripts/sync_instrument_sectors.py) markierte
diese Instrumente daher als 'unknown'. Folge im taeglichen Hauptkonto-
Report: gehaltene Forex-Positionen fielen in die "Sektor noch nicht
abgerufen"-Kategorie (gemessen 2026-08-20: $4.766 = 67.3% des Equity).

Loesung: Fuer Instrumente OUTSIDE stock/etf leitet der Sync den Sektor
direkt aus `instruments.asset_class` ab (kein yfinance-Call, kein
Rate-Limiting, keine falschen Werte). Die Taxonomie ist bewusst grob —
sie dient dem Report-Gruppierung und dem Sektor-Gate als Fail-SAFE
(20%-Default-Cap statt fail-open), nicht der Feinanalyse.

Konvention:
  - Grossschreibung (FOREX/COMMODITY/INDEX/CRYPTO) = asset-class-derived,
    yfinance-unabhaengig.
  - 'unknown' bleibt ROTATIONS-BUCHHALTUNG des Sync-Scripts (Read-Paths
    behandeln es wie NULL — siehe test_sector_asset_class.py).
  - Kuratierte ASSET_CLASS_MAP-Eintraege behalten Vorrang vor dem DB-Sektor
    (resolve_asset_class), d.h. BTC-USD bleibt CRYPTO(10%), auch wenn der
    DB-Sektor 'CRYPTO' lautet.
"""
from __future__ import annotations

# asset_class -> abgeleiteter Sektor. stock/etf fehlen bewusst: die gehen
# durch den yfinance-Pfad (echte Yahoo-Sektoren, feiner als "STOCK"/"ETF").
ASSET_CLASS_SECTOR = {
    "forex": "FOREX",
    "commodity": "COMMODITY",
    "index": "INDEX",
    "crypto": "CRYPTO",
}


def derive_asset_class_sector(asset_class: str | None) -> str | None:
    """Sektor aus asset_class ableiten, oder None = yfinance-Pfad nutzen.

    Pure Function (ohne DB/API) — testbar ohne Mocks. Unbekannte/leere
    asset_class liefern None (damit der bisherige yfinance-Verhalten exakt
    erhalten bleibt).
    """
    if not asset_class:
        return None
    return ASSET_CLASS_SECTOR.get(asset_class.strip().lower())
