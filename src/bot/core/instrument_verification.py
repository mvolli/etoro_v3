#!/usr/bin/env python3
"""Instrument verification — fix/discovery-identity-verification.

Pure logic (no API, no DB) to verify that a locally-resolved
instrument_id actually refers to the instrument a signal was generated
for, BEFORE the signal enters the pool.

Root cause this prevents (VALT.L incident, 2026-07-02): discovery
resolved a symbol to a wrong/placeholder instrument_id. The signal
carried the correct yfinance price (~$113) while the eToro ID pointed at
a completely different instrument (~$5,200). The slippage gate caught it
at execution time — this module catches the whole class at DISCOVERY
time, so mismatched identities never reach the signal pool at all.

Two independent checks:
  1. Identity  — does eToro metadata for the ID resolve to the expected
                 ticker? (Hard fail on mismatch; fail-open when metadata
                 is unavailable — same philosophy as
                 EToroClient.verify_instrument_identity.)
  2. Price     — does the eToro live price agree with the yfinance price
                 the signal is based on? Scale-aware to tolerate
                 GBp/GBP (LSE pence, ×100) and similar minor-unit quotes
                 (×0.01), so e.g. VALT.L at 113 GBP vs 11,300 GBp passes,
                 while 113 vs 5,200 (a genuinely different instrument)
                 fails at every scale.
"""
from __future__ import annotations

# Deviation above which two prices are considered "different instruments".
# Generous on purpose: yfinance candles can be up to a session old and the
# check must survive normal volatility + FX wobble. A true identity
# mismatch is typically off by 10×–1000×, far outside any tolerance.
MAX_PRICE_DEVIATION_PCT_DEFAULT = 25.0

# Scale factors tried when comparing prices. 1.0 = same unit,
# 100.0 / 0.01 = major/minor currency unit (GBP↔GBp, EUR↔cent).
PRICE_SCALE_FACTORS: tuple[float, ...] = (1.0, 100.0, 0.01)

# Metadata symbol fields in priority order — mirrors
# EToroClient.verify_instrument_identity (symbolFull verified 2026-07-01).
_SYMBOL_FIELDS: tuple[str, ...] = (
    "symbolFull",
    "internalSymbolFull",
    "symbol",
    "ticker",
    "displayName",
    "displayname",
)


def normalize_symbol(sym: str) -> str:
    """Loosely normalize a ticker for identity comparison.

    Upper-cases and strips common quote-currency suffixes so a local
    'DOT-USD' matches a live-API 'DOT'. Exchange suffixes like '.L' are
    KEPT — 'VALT.L' and 'VALT' are different listings on purpose.
    """
    if not sym:
        return ""
    s = sym.upper().strip()
    for suffix in ("-USD", "/USD", "USD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s


def extract_live_symbol(meta: dict | None) -> str:
    """Pull the ticker out of an eToro metadata dict ('' when absent)."""
    if not meta:
        return ""
    for field in _SYMBOL_FIELDS:
        val = meta.get(field)
        if val:
            return str(val)
    return ""


def check_identity(expected_symbol: str, meta: dict | None) -> tuple[bool, str]:
    """Identity check against eToro metadata.

    - meta missing / no symbol field → (True, ...) fail-open: a metadata
      outage must not silence discovery entirely; the price check below
      still applies.
    - symbol present and different   → (False, ...) HARD fail, never open.
    """
    live_symbol = extract_live_symbol(meta)
    if not live_symbol:
        return True, f"Identity: skipped für '{expected_symbol}' (keine Metadata — fail-open)"

    if normalize_symbol(expected_symbol) != normalize_symbol(live_symbol):
        return False, (
            f"Identity MISMATCH: lokal '{expected_symbol}' ≠ eToro '{live_symbol}' "
            f"— falsche instrument_id-Zuordnung"
        )
    return True, f"Identity OK: '{expected_symbol}' == '{live_symbol}'"


def check_price_consistency(
    reference_price: float | None,
    live_price: float | None,
    max_deviation_pct: float = MAX_PRICE_DEVIATION_PCT_DEFAULT,
    scale_factors: tuple[float, ...] = PRICE_SCALE_FACTORS,
) -> tuple[bool, float, float]:
    """Compare the signal's reference price (yfinance) with the eToro live
    price for the resolved instrument_id.

    Returns (consistent, best_deviation_pct, best_scale).

    - Either price missing/non-positive → (True, 0.0, 1.0): nothing to
      compare, treated as pass — the identity check is the primary gate.
    - Tries each scale factor and keeps the smallest absolute deviation,
      so GBp/GBP and cent quotes don't false-positive.
    """
    if not reference_price or reference_price <= 0 or not live_price or live_price <= 0:
        return (True, 0.0, 1.0)

    best_dev = float("inf")
    best_scale = 1.0
    for scale in scale_factors:
        scaled_live = live_price * scale
        dev_pct = abs(scaled_live - reference_price) / reference_price * 100.0
        if dev_pct < best_dev:
            best_dev = dev_pct
            best_scale = scale

    return (best_dev <= max_deviation_pct, best_dev, best_scale)


def verify_candidate(
    expected_symbol: str,
    meta: dict | None,
    reference_price: float | None,
    live_price: float | None,
    max_deviation_pct: float = MAX_PRICE_DEVIATION_PCT_DEFAULT,
) -> tuple[bool, str]:
    """Combined verification for one discovery candidate.

    Order matters: a hard identity mismatch always wins. The price check
    is the safety net for the fail-open metadata paths (missing meta,
    missing symbol field) — exactly the paths a placeholder/collided ID
    slips through.
    """
    id_ok, id_reason = check_identity(expected_symbol, meta)
    if not id_ok:
        return (False, id_reason)

    price_ok, dev_pct, scale = check_price_consistency(
        reference_price, live_price, max_deviation_pct
    )
    if not price_ok:
        return (False, (
            f"Preis-Mismatch: {expected_symbol} Signal ${reference_price:.4f} vs. "
            f"eToro ${live_price:.4f} — beste Abweichung {dev_pct:.1f}% "
            f"(Scale ×{scale:g}, Max {max_deviation_pct:.0f}%) — "
            f"instrument_id zeigt vermutlich auf ein anderes Instrument"
        ))

    detail = f"{id_reason} | Preis OK ({dev_pct:.1f}% @ ×{scale:g})" \
        if (reference_price and live_price) else f"{id_reason} | Preis: kein Vergleich möglich"
    return (True, detail)
