"""Yahoo-Symbol je Instrument — die eine Aufloesung fuer alle Leser (VoLLi 2026-10-17).

`instruments.yfinance_symbol` war bei US-Aktien systematisch falsch befuellt
(17.10.: 5 777 von 6 266 nicht der Ticker), teils mit einer ANDEREN Firma:
CLPT -> 'CNI' (Canadian National), BRBI -> 'BAP' (Credicorp), CI -> 'CG'
(Carlyle), XPO -> 'XLI' (Industrie-ETF). Ein gueltiges Fremdsymbol faellt nie auf
den Ticker zurueck, der Bot rechnet dann still mit fremden Kursen, Earnings und
Headlines. 951 belegte Faelle sind in der DB korrigiert
(scripts/fix_yf_symbols.py), der Rest ist ungeprueft.

Regel:
  US-Aktie (asset_class stock/stocks, eToro-Symbol ohne Boersen-Suffix, nur
  Buchstaben/Ziffern/ein Bindestrich) -> der eToro-Ticker selbst, ausser bei
  belegten Ausnahmen (US_OVERRIDES: eToro handelt eine Auslandsnotiz unter
  US-artigem Symbol).
  Alles andere (EU/Asien mit Suffix, ETF, Krypto, Devisen, Indizes,
  Rohstoffe, Sonderzeichen wie 'D&C') -> instruments.yfinance_symbol, sonst
  das eToro-Symbol.

Neue Ausnahmen nur mit Beleg (eToro-Kurs gegen Yahoo, z. B.
scripts/check_yf_symbol_identity.py) eintragen.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

_US_TICKER = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,3})?$")
_STOCK = {"stock", "stocks"}

# eToro-Symbol -> Yahoo, belegt 17.10.2026 (eToro-Kurs / Yahoo):
US_OVERRIDES = {
    "BP": "BP.L",      # eToro BP = London-Notiz in Pence: 615,2 / BP.L 614,1 (ADR BP 50,2)
}


def resolve(symbol: str | None, yfinance_symbol: str | None = None,
            asset_class: str | None = None) -> str:
    """Yahoo-Symbol fuer ein Instrument (rein, ohne DB)."""
    sym = str(symbol or "").strip()
    stored = str(yfinance_symbol or "").strip()
    # Nur echte Ticker (Grossbuchstaben): Platzhalter wie 'DDOld', 'P_old',
    # 'SR&' behalten ihr gespeichertes Symbol.
    if str(asset_class or "").lower() in _STOCK and _US_TICKER.match(sym):
        return US_OVERRIDES.get(sym, sym)
    return stored or sym


def resolve_row(row: Mapping[str, Any] | Any) -> str:
    """resolve() fuer eine instruments-Zeile/ein dict mit symbol, yfinance_symbol, asset_class."""
    def g(k):
        try:
            return row[k]
        except (KeyError, IndexError, TypeError):
            return None
    return resolve(g("symbol"), g("yfinance_symbol"), g("asset_class"))


def for_instrument(db: Any, instrument_id: Any) -> str | None:
    """Yahoo-Symbol per instrument_id aus der DB, None wenn unbekannt."""
    try:
        row = db.fetchone("SELECT symbol, yfinance_symbol, asset_class FROM instruments "
                          "WHERE instrument_id = ?", (instrument_id,))
    except Exception:  # noqa: BLE001
        return None
    return resolve_row(row) if row else None


def for_symbol(db: Any, symbol: str) -> str | None:
    """Yahoo-Symbol per eToro-Symbol aus der DB, None wenn unbekannt."""
    try:
        row = db.fetchone("SELECT symbol, yfinance_symbol, asset_class FROM instruments "
                          "WHERE symbol = ? ORDER BY instrument_id LIMIT 1", (symbol,))
    except Exception:  # noqa: BLE001
        return None
    return resolve_row(row) if row else None
