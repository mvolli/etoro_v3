#!/usr/bin/env python3
"""Regime detection — Trading Bible V5.

Four regimes with risk_scalar (replaces V4's binary DRAWDOWN/NORMAL/RECOVERY).

V4 → V5 changes:
  - RECOVERY regime abolished (organic recovery via risk_scalar)
  - 3 states → 4 states: NORMAL / CAUTION / DEFENSIVE / CRITICAL
  - risk_scalar replaces binary BUY-block: continuous sizing from 25%–100%
  - High-watermark condition: full risk only at new equity high
  - AQR continuous formula available as alternative to stepped regime

Thresholds (inspired by Man AHL / CTAs):
  NORMAL:    DD < 4.0%  → risk_scalar = 1.00 (full sizing)
  CAUTION:   DD 4–8%    → risk_scalar = 0.75 (reduce 25%)
  DEFENSIVE: DD 8–15%   → risk_scalar = 0.50 (Half-Kelly)
  CRITICAL:  DD > 15%   → risk_scalar = 0.25 (Quarter-Kelly, only VERY_HIGH)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ─── Thresholds (Trading Bible V5) ───────────────────────────────────────────

# Entry thresholds (immediately on breach)
CAUTION_THRESHOLD:   float = 4.0   # ≥ 4%  → CAUTION
DEFENSIVE_THRESHOLD: float = 8.0   # ≥ 8%  → DEFENSIVE
CRITICAL_THRESHOLD:  float = 15.0  # ≥ 15% → CRITICAL

# Exit thresholds (sticky — require sustained improvement to prevent whipsawing)
# Exit is only at LOWER threshold to create hysteresis band
CAUTION_EXIT:    float = 3.5   # < 3.5% from CAUTION → NORMAL
DEFENSIVE_EXIT:  float = 7.0   # < 7.0% from DEFENSIVE → CAUTION
CRITICAL_EXIT:   float = 13.0  # < 13.0% from CRITICAL → DEFENSIVE

REGIMES = frozenset({"NORMAL", "CAUTION", "DEFENSIVE", "CRITICAL"})

# ─── Risk Scalars per Regime ─────────────────────────────────────────────────

RISK_SCALARS: dict[str, float] = {
    "NORMAL":    1.00,  # Full sizing
    "CAUTION":   0.75,  # -25%: reduce new positions
    "DEFENSIVE": 0.50,  # -50%: Half-Kelly, high conviction only
    "CRITICAL":  0.25,  # -75%: Quarter-Kelly, only VERY_HIGH signals
}

# ─── Regime Parameters ────────────────────────────────────────────────────────

_REGIME_PARAMS: dict[str, dict] = {
    "NORMAL": {
        "cash_min_pct":       15.0,
        "max_trade_pct":       5.0,
        "buy_aggressiveness":  1.0,
        "min_buy_usd":        50.0,
        "allow_pyramiding":   True,
        "min_conviction":     "LOW",
        "description":        "Standard — alle Signale erlaubt",
    },
    "CAUTION": {
        "cash_min_pct":       20.0,   # Higher buffer
        "max_trade_pct":       4.0,   # Slightly smaller trades
        "buy_aggressiveness":  0.75,  # risk_scalar applied
        "min_buy_usd":        75.0,
        "allow_pyramiding":   True,   # Still allowed but at reduced size
        "min_conviction":     "MEDIUM",  # No LOW signals
        "description":        "Erhöhte Vorsicht — nur MEDIUM+ Signale",
    },
    "DEFENSIVE": {
        "cash_min_pct":       25.0,
        "max_trade_pct":       3.0,
        "buy_aggressiveness":  0.50,
        "min_buy_usd":       100.0,
        "allow_pyramiding":   False,  # No adding to existing positions
        "min_conviction":     "HIGH",  # Only HIGH and VERY_HIGH
        "description":        "Defensiv — kein Pyramiding, nur HIGH+ Signale",
    },
    "CRITICAL": {
        "cash_min_pct":       30.0,
        "max_trade_pct":       2.0,
        "buy_aggressiveness":  0.25,
        "min_buy_usd":       150.0,
        "allow_pyramiding":   False,
        "min_conviction":     "VERY_HIGH",  # Only best signals
        "description":        "Kritisch — nur VERY_HIGH Signale, Quarter-Kelly",
    },
}


def get_regime_params(regime: str) -> dict:
    """Get Trading Bible V5 parameters for a given regime."""
    if regime not in REGIMES:
        raise ValueError(f"Unknown regime {regime!r}. Must be one of {sorted(REGIMES)}")
    return dict(_REGIME_PARAMS[regime])


def get_risk_scalar(regime: str) -> float:
    """Get position sizing scalar for regime (1.0 = full, 0.25 = quarter-Kelly)."""
    return RISK_SCALARS.get(regime, 1.0)


# ─── AQR Continuous Formula (alternative to stepped regime) ──────────────────

def aqr_risk_scalar(drawdown_pct: float) -> float:
    """AQR continuous risk scalar: max(0.25, 1 - 2 * drawdown).

    More granular than stepped regime. Can be used alongside
    or instead of the stepped system.

    Examples:
      0% DD  → 1.00 (full)
      10% DD → 0.80
      25% DD → 0.50
      37.5% DD → 0.25 (minimum)
    """
    return max(0.25, 1.0 - 2.0 * (drawdown_pct / 100.0))


# ─── Regime Detection ─────────────────────────────────────────────────────────

def detect_regime(
    equity: float,
    peak_equity: float,
    previous_regime: str = "NORMAL",
) -> tuple[str, str]:
    """Detect current trading regime with sticky hysteresis.

    V5 changes from V4:
    - RECOVERY regime removed (replaced by organic risk_scalar recovery)
    - 4 regimes instead of 3
    - Asymmetric transitions: fast entry into higher-risk regime,
      slow exit requiring lower threshold (hysteresis)

    Args:
        equity: Current portfolio equity
        peak_equity: Highest equity ever reached (high-watermark)
        previous_regime: Last known regime (for hysteresis)

    Returns:
        (regime, reason)
    """
    if peak_equity <= 0:
        return "NORMAL", "No peak data — defaulting to NORMAL"

    dd = ((peak_equity - equity) / peak_equity) * 100.0

    # ── Entry into higher-risk regime: immediate on breach ────────────────
    if dd >= CRITICAL_THRESHOLD:
        return "CRITICAL", (
            f"DD {dd:.1f}% ≥ {CRITICAL_THRESHOLD:.0f}% → CRITICAL "
            f"(Quarter-Kelly, only VERY_HIGH signals)"
        )

    # ── Hysteresis for CRITICAL: stay until dd drops below CRITICAL_EXIT ──
    # fix/critical-hysteresis: CRITICAL_EXIT (13%) war toter Code — bei DD
    # um 15% flatterte das System im 5-min-Takt zwischen CRITICAL (0.25)
    # und DEFENSIVE (0.50), genau das Whipsawing, das die Hysterese für
    # CAUTION/DEFENSIVE bereits verhindert.
    if previous_regime == "CRITICAL" and dd > CRITICAL_EXIT:
        return "CRITICAL", (
            f"Hysteresis: DD {dd:.1f}% > {CRITICAL_EXIT:.0f}% exit threshold "
            f"— staying in CRITICAL"
        )

    if dd >= DEFENSIVE_THRESHOLD:
        return "DEFENSIVE", (
            f"DD {dd:.1f}% ≥ {DEFENSIVE_THRESHOLD:.0f}% → DEFENSIVE "
            f"(Half-Kelly, no pyramiding)"
        )

    # ── Hysteresis for DEFENSIVE: stay until dd drops below DEFENSIVE_EXIT ─
    if previous_regime in ("DEFENSIVE", "CRITICAL") and dd > DEFENSIVE_EXIT:
        return "DEFENSIVE", (
            f"Hysteresis: DD {dd:.1f}% > {DEFENSIVE_EXIT:.0f}% exit threshold "
            f"— staying in DEFENSIVE"
        )

    if dd >= CAUTION_THRESHOLD:
        return "CAUTION", (
            f"DD {dd:.1f}% ≥ {CAUTION_THRESHOLD:.0f}% → CAUTION "
            f"(75% sizing, MEDIUM+ only)"
        )

    # ── Hysteresis for CAUTION: stay until dd drops below CAUTION_EXIT ────
    if previous_regime in ("CAUTION", "DEFENSIVE", "CRITICAL") and dd > CAUTION_EXIT:
        return "CAUTION", (
            f"Hysteresis: DD {dd:.1f}% > {CAUTION_EXIT:.0f}% exit threshold "
            f"— staying in CAUTION"
        )

    # ── NORMAL ────────────────────────────────────────────────────────────
    return "NORMAL", f"DD {dd:.1f}% < {CAUTION_EXIT:.0f}% → NORMAL (full sizing)"


def is_pyramiding_allowed(regime: str) -> bool:
    """V5: pyramiding forbidden in DEFENSIVE and CRITICAL regimes."""
    return _REGIME_PARAMS.get(regime, {}).get("allow_pyramiding", True)


def get_min_conviction(regime: str) -> str:
    """Minimum signal conviction required to trade in this regime."""
    return _REGIME_PARAMS.get(regime, {}).get("min_conviction", "MEDIUM")


# ─── StateRepo Integration ────────────────────────────────────────────────────

def update_regime(state_repo, equity: float) -> tuple[str, bool]:
    """Detect and persist current regime. Returns (new_regime, changed).

    V5: Also updates HIGH_WATERMARK and RISK_SCALAR in system_state.
    """
    peak_equity = state_repo.get_float("PEAK_EQUITY", equity)
    previous_regime = state_repo.get_regime()

    new_regime, reason = detect_regime(equity, peak_equity, previous_regime)
    changed = new_regime != previous_regime

    # Update regime and risk scalar
    state_repo.set_regime(new_regime)
    state_repo.set("RISK_SCALAR", str(get_risk_scalar(new_regime)))
    state_repo.set("DRAWDOWN_REASON", reason)

    # Update drawdown pct
    if peak_equity > 0:
        dd_pct = (peak_equity - equity) / peak_equity * 100
        state_repo.set("DRAWDOWN_PCT", f"{dd_pct:.4f}")

    # Update peak equity (high-watermark) if new high
    if equity > peak_equity:
        state_repo.set("PEAK_EQUITY", str(equity))
        state_repo.set("HIGH_WATERMARK_REACHED", "true")
    else:
        state_repo.set("HIGH_WATERMARK_REACHED", "false")

    return new_regime, changed


def is_at_full_risk(state_repo) -> bool:
    """True only when equity is at or above high-watermark (no drawdown).

    V5 recovery protocol: full risk (risk_scalar=1.0) only at new equity high.
    """
    return state_repo.get("HIGH_WATERMARK_REACHED", "false") == "true"
