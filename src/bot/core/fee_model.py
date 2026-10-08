"""Fee-Modell für eToro-Öffnungsgebühren (fix/fee-tracking, 2026-10-08).

eToro bucht die Eröffnungsgebühr NICHT in `netProfit` (belegt in
`e3d15b1` + Routespec: "fees is a separate, additional cost not deducted
from netProfit"). Sie liegt im `residual_usd` versteckt.

Modell (gemessen, nicht geraten):
  - Standard: 1 % des Open-Betrags (US/EU/UK/CH/DE)
  - High-Fee: 2 % auf .ASX / .HK / .T / .AX Börsen

Die Spalte `fee_usd` in `trade_events` wird bei OPEN-Events gesetzt.
`recorded_fees()` in `trade_pnl.py` summiert sie für die Reconciliation.

Fee-Tier-Konfiguration kommt aus `config.yaml` → `fee_tiering`:
  high_fee_suffixes: [.ASX, .HK, .T, .AX]
  fee_pct: 2.0  (High-Fee-Tier)
Standard-Tier: 1.0 %
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Standard-Fee: 1 % (US/EU/UK/CH/DE)
_STD_FEE_PCT = 1.0
# High-Fee-Suffixe (aus config.yaml, hier als Default-Fallback)
_HIGH_FEE_SUFFIXES = (".ASX", ".HK", ".T", ".AX")


def fee_pct_for_symbol(symbol: str,
                       high_fee_suffixes: tuple[str, ...] = _HIGH_FEE_SUFFIXES,
                       high_fee_pct: float = 2.0) -> float:
    """Fee-Prozent für ein Symbol (1 % oder 2 %).

    Suffix-Match: `.T` matcht `.T` aber NICHT `.TW` (Taiwan) —
    die Suffixe sind exakt, keine Teilstring-Suche.
    """
    for suffix in high_fee_suffixes:
        if symbol.upper().endswith(suffix.upper()):
            return high_fee_pct
    return _STD_FEE_PCT


def estimate_open_fee(amount_usd: float | None, symbol: str,
                      high_fee_suffixes: tuple[str, ...] = _HIGH_FEE_SUFFIXES,
                      high_fee_pct: float = 2.0) -> float | None:
    """Geschätzte Eröffnungsgebühr in USD.

    `amount_usd` ist der Open-Betrag (Kostenbasis). Fee = Betrag × Satz.
    Gibt None zurück, wenn `amount_usd` nicht positiv ist.
    """
    try:
        if amount_usd is None:
            return None
        amount_usd = float(amount_usd)
        if amount_usd <= 0:
            return None
        pct = fee_pct_for_symbol(symbol, high_fee_suffixes, high_fee_pct)
        return round(amount_usd * pct / 100.0, 4)
    except (TypeError, ValueError):
        return None


def load_fee_config(cfg: dict | None = None) -> dict:
    """Fee-Tier-Parameter aus Config laden.

    Returns {"high_fee_suffixes": tuple, "high_fee_pct": float}.
    Fail-open: bei Fehlern Default-Werte.
    """
    if cfg is None:
        return {"high_fee_suffixes": _HIGH_FEE_SUFFIXES,
                "high_fee_pct": 2.0}
    try:
        ft = cfg.get("fee_tiering", {})
        suffixes = tuple(ft.get("high_fee_suffixes", list(_HIGH_FEE_SUFFIXES)))
        pct = float(ft.get("fee_pct", 2.0))
        return {"high_fee_suffixes": suffixes, "high_fee_pct": pct}
    except (TypeError, ValueError, AttributeError):
        return {"high_fee_suffixes": _HIGH_FEE_SUFFIXES,
                "high_fee_pct": 2.0}
