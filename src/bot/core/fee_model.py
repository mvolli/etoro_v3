"""Fee-Modell für eToro-Öffnungsgebühren (fix/fee-tracking, 2026-10-08).

eToro bucht die Eröffnungsgebühr NICHT in `netProfit` (belegt in
`e3d15b1` + Routespec: "fees is a separate, additional cost not deducted
from netProfit"). Sie liegt im `residual_usd` versteckt.

Modell (EMPIRISCH VERIFIZIERT 2026-10-09 via eToro API, Bot-Account
RoBoCoP-FTDJLSXW, 18 offene Positionen):
  - FLAT pro Order beim OPEN (NICHT prozentual):
  - Standard: $1.00 (US/DE/FR/SE/UK/…)
  - High-Fee: $2.00 auf .ASX / .HK / .T / .AE Börsen
  - Max Fee pro Position: $2.00 (auch bei isPartiallyAltered=True)
  - totalFees (intern): $0.00
  - totalExternalFees: 10×$2 + 8×$1 = $28 auf 18 offenen Positionen
  - Old-Modell 1 %/2 % prozentual → Fehlschätzung Faktor 1-8
    ($100: 1 % = $1 ✓, $50: 1 % = $0.50 ≠ $1, $200: 1 % = $2 ≠ $1)

Die Spalte `fee_usd` in `trade_events` wird bei OPEN-Events gesetzt.
`recorded_fees()` in `trade_pnl.py` summiert sie für die Reconciliation.

Fee-Tier-Konfiguration kommt aus `config.yaml` → `fee_tiering`:
  high_fee_suffixes: [.ASX, .HK, .T, .AX, .AE]
  fee_flat_high: 2.0  (High-Fee-Tier in USD)
  fee_flat_std: 1.0   (Standard-Tier in USD)
  fee_pct: 2.0        (LEGACY — nur noch für den Kandidaten-Ranking-Faktor
                      im signal_worker, keine USD-Buchung mehr)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Standard-Fee: $1.00 flat (US/DE/UK/CH/FR/SE/…)
_STD_FEE_FLAT = 1.0
# High-Fee: $2.00 flat auf asiatische/australische Börsen
_HIGH_FEE_FLAT = 2.0
# High-Fee-Suffixe (aus config.yaml, hier als Default-Fallback)
_HIGH_FEE_SUFFIXES = (".ASX", ".HK", ".T", ".AX", ".AE")


def is_high_fee_symbol(symbol: str,
                       high_fee_suffixes: tuple[str, ...] = _HIGH_FEE_SUFFIXES) -> bool:
    """True, wenn das Symbol auf einem High-Fee-Börsen-Suffix endet.

    Suffix-Match: `.T` matcht `.T` aber NICHT `.TW` (Taiwan) —
    die Suffixe sind exakt, keine Teilstring-Suche.
    """
    for suffix in high_fee_suffixes:
        if symbol.upper().endswith(suffix.upper()):
            return True
    return False


def fee_pct_for_symbol(symbol: str,
                       high_fee_suffixes: tuple[str, ...] = _HIGH_FEE_SUFFIXES,
                       high_fee_pct: float = 2.0) -> float:
    """LEGACY: Fee-Prozent für ein Symbol (1 % oder 2 %).

    NUR noch für den Kandidaten-Ranking-Faktor im signal_worker (relativer
    Bias High-Fee vs. Standard). Für USD-Buchung estimate_open_fee() nutzen.
    """
    return high_fee_pct if is_high_fee_symbol(symbol, high_fee_suffixes) else 1.0


def estimate_open_fee(amount_usd: float | None, symbol: str,
                      high_fee_suffixes: tuple[str, ...] = _HIGH_FEE_SUFFIXES,
                      high_fee_flat: float = _HIGH_FEE_FLAT,
                      std_fee_flat: float = _STD_FEE_FLAT) -> float | None:
    """Geschätzte Eröffnungsgebühr in USD — FLAT-Modell.

    $2.00 auf High-Fee-Börsen, $1.00 sonst. `amount_usd` wird nur als
    Gültigkeits-Check genutzt (positiv), nicht in die Fee eingerechnet.
    Gibt None zurück, wenn `amount_usd` nicht positiv ist.
    """
    try:
        if amount_usd is None:
            return None
        amount_usd = float(amount_usd)
        if amount_usd <= 0:
            return None
        if is_high_fee_symbol(symbol, high_fee_suffixes):
            return round(float(high_fee_flat), 4)
        return round(float(std_fee_flat), 4)
    except (TypeError, ValueError):
        return None


def load_fee_config(cfg: dict | None = None) -> dict:
    """Fee-Tier-Parameter aus Config laden.

    Returns {"high_fee_suffixes": tuple, "high_fee_flat": float,
             "std_fee_flat": float, "high_fee_pct": float (legacy)}.
    Fail-open: bei Fehlern Default-Werte.
    """
    defaults = {"high_fee_suffixes": _HIGH_FEE_SUFFIXES,
                "high_fee_flat": _HIGH_FEE_FLAT,
                "std_fee_flat": _STD_FEE_FLAT,
                "high_fee_pct": 2.0}
    if cfg is None:
        return defaults
    try:
        ft = cfg.get("fee_tiering", {})
        suffixes = tuple(ft.get("high_fee_suffixes", list(_HIGH_FEE_SUFFIXES)))
        # Legacy-Feld `fee_pct` (2.0) bleibt als Fallback, wenn das
        # neue flat-Feld fehlt — dann aber als FLAT-Wert interpretiert.
        high_flat = float(ft.get("fee_flat_high", ft.get("fee_pct", _HIGH_FEE_FLAT)))
        std_flat = float(ft.get("fee_flat_std", _STD_FEE_FLAT))
        return {"high_fee_suffixes": suffixes,
                "high_fee_flat": high_flat,
                "std_fee_flat": std_flat,
                "high_fee_pct": high_flat}
    except (TypeError, ValueError, AttributeError):
        return defaults
