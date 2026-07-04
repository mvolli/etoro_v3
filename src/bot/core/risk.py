#!/usr/bin/env python3
"""Risk enforcement — Trading Bible V5 gates.

ALL buy/sell decisions pass through here before execution.
No DB, no API — pure logic, fully unit-testable.

V5 changes from V4:
  - Regime gate now uses 4-state regime (NORMAL/CAUTION/DEFENSIVE/CRITICAL)
  - risk_scalar applied to position sizing
  - Pyramiding check: blocked in DEFENSIVE/CRITICAL
  - Min conviction check: regime-dependent minimum signal strength
  - SL quality gate: SL >50% from entry = meaningless SL → blocked
  - Crypto SL: always calculated as relative % (never absolute price)
  - Already-over-limit check: blocks new buys when already at/over limit
"""
from __future__ import annotations

from dataclasses import dataclass

# ─── Constants (Defaults — Overrides via apply_config(cfg) weiter unten) ────

INSTRUMENT_LIMITS: dict[str, float] = {
    "NVDA": 25.0, "QQQ": 25.0, "SPY": 20.0,
    "META": 15.0, "MSFT": 15.0, "GLD": 15.0, "TLT": 15.0,
    "AMZN": 12.0, "CPER": 10.0,
    "BTC": 5.0, "ETH": 5.0, "TSLA": 5.0,
    "XRP": 3.0,
}
DEFAULT_INSTRUMENT_LIMIT = 10.0

ASSET_CLASS_LIMITS: dict[str, float] = {
    "US_TECH":      40.0,
    "BROAD_ETF":    25.0,
    "COMMODITY":    20.0,
    "CRYPTO":       10.0,
    "BOND":         20.0,
    "INTL":         20.0,
    # fix/asset-class-limits-gap: FINANCIAL/CONSUMER/HEALTHCARE/ENERGY are
    # populated in ASSET_CLASS_MAP below (JPM/WMT/JNJ/XOM etc.) but had no
    # entry here — check_asset_class_gate()'s .get(asset_class, 100.0)
    # fallback meant these four sectors had NO enforced concentration limit
    # at all (only the blunt 75% total-exposure gate could ever stop them).
    # 20.0 matches the already-configured (but, until this fix, unwired)
    # config.yaml sector_limits.max_per_sector_pct, and the existing
    # COMMODITY/BOND/INTL entries below.
    "FINANCIAL":    20.0,
    "CONSUMER":     20.0,
    "HEALTHCARE":   20.0,
    "ENERGY":       20.0,
}

# V5: Explicit crypto symbols for SL relative-calculation enforcement
CRYPTO_SYMBOLS: frozenset[str] = frozenset({
    "BTC", "BTC-USD", "ETH", "ETH-USD", "XRP", "XRP-USD",
    "DOGE", "DOGE-USD", "SOL", "SOL-USD", "BNB", "BNB-USD",
    "ADA", "ADA-USD", "DOT", "DOT-USD",
})

ASSET_CLASS_MAP: dict[str, str] = {
    # US Tech
    "NVDA": "US_TECH", "META": "US_TECH", "MSFT": "US_TECH",
    "AMZN": "US_TECH", "AAPL": "US_TECH", "GOOGL": "US_TECH",
    "NFLX": "US_TECH", "AMD": "US_TECH", "INTC": "US_TECH",
    "ADBE": "US_TECH", "CRM": "US_TECH", "TSLA": "US_TECH",
    "ORCL": "US_TECH", "PYPL": "US_TECH", "UBER": "US_TECH",
    "LYFT": "US_TECH", "SNAP": "US_TECH", "TWLO": "US_TECH",
    "ZM": "US_TECH", "DOCU": "US_TECH",
    # Broad ETF
    "QQQ": "BROAD_ETF", "SPY": "BROAD_ETF", "IWM": "BROAD_ETF",
    "VTI": "BROAD_ETF", "EEM": "BROAD_ETF",
    # Financial
    "JPM": "FINANCIAL", "BAC": "FINANCIAL", "GS": "FINANCIAL",
    "V": "FINANCIAL", "MA": "FINANCIAL", "BRK-B": "FINANCIAL",
    "AXP": "FINANCIAL", "C": "FINANCIAL", "WFC": "FINANCIAL",
    # Consumer
    "WMT": "CONSUMER", "HD": "CONSUMER", "PG": "CONSUMER",
    "KO": "CONSUMER", "PEP": "CONSUMER", "COST": "CONSUMER",
    "NKE": "CONSUMER", "MCD": "CONSUMER", "SBUX": "CONSUMER",
    # Healthcare
    "JNJ": "HEALTHCARE", "UNH": "HEALTHCARE", "PFE": "HEALTHCARE",
    "ABBV": "HEALTHCARE", "MRK": "HEALTHCARE", "LLY": "HEALTHCARE",
    "TMO": "HEALTHCARE",
    # Energy
    "XOM": "ENERGY", "CVX": "ENERGY", "COP": "ENERGY", "SLB": "ENERGY",
    # Commodity / Bond
    "GLD": "COMMODITY", "SLV": "COMMODITY", "CPER": "COMMODITY",
    "USO": "COMMODITY", "TLT": "BOND",
    # Crypto
    "BTC": "CRYPTO", "BTC-USD": "CRYPTO",
    "ETH": "CRYPTO", "ETH-USD": "CRYPTO",
    "XRP": "CRYPTO", "XRP-USD": "CRYPTO",
    "DOGE": "CRYPTO", "DOGE-USD": "CRYPTO",
    "SOL-USD": "CRYPTO", "BNB-USD": "CRYPTO",
    "ADA": "CRYPTO", "ADA-USD": "CRYPTO", "DOT": "CRYPTO", "DOT-USD": "CRYPTO",
    # International
    "ENI.MI": "INTL", "BP": "INTL", "SHEL": "INTL",
    "TSM": "INTL", "BABA": "INTL",
}

# ── V5: Asset-Class Score Boost — prioritize stocks/ETFs during signal ─────
# ranking. This does NOT change gate/exposure logic (ASSET_CLASS_LIMITS
# above still caps final exposure) — it only nudges *which* signals win a
# scarce top-3-per-cycle slot in signal_worker, so equities/ETFs are
# preferred over indices/commodities/crypto when scores are close.
# A multiplier of 1.0 is neutral; >1.0 boosts, <1.0 deprioritizes.
ASSET_CLASS_SCORE_BOOST: dict[str, float] = {
    "US_TECH":    1.15,
    "FINANCIAL":  1.15,
    "CONSUMER":   1.15,
    "HEALTHCARE": 1.15,
    "ENERGY":     1.15,
    "BROAD_ETF":  1.10,
    "INTL":       1.10,
    "BOND":       1.00,
    "COMMODITY":  0.95,
    "CRYPTO":     0.85,
}
# Fallback boost for equity symbols not present in ASSET_CLASS_MAP —
# classify_asset_class() below decides when this applies (default: any
# symbol that isn't recognisably crypto/ETF/commodity is treated as a
# plain stock and gets the same boost as the named equity sectors).
DEFAULT_STOCK_SCORE_BOOST = 1.15

_KNOWN_BROAD_ETFS = frozenset({"QQQ", "SPY", "IWM", "VTI", "EEM", "DIA", "RSP"})
_KNOWN_COMMODITIES = frozenset({"GLD", "SLV", "CPER", "USO"})
_KNOWN_BONDS = frozenset({"TLT"})


def classify_asset_class(symbol: str) -> str:
    """Best-effort asset-class classification for score-boost purposes.

    Falls back to symbol-shape heuristics for instruments not in the
    curated ASSET_CLASS_MAP, so the universe's ~65 symbols don't need to
    be exhaustively hand-mapped for prioritization to work.
    """
    s = symbol.upper()
    if s in ASSET_CLASS_MAP:
        return ASSET_CLASS_MAP[s]
    if s in CRYPTO_SYMBOLS or s.endswith("-USD"):
        return "CRYPTO"
    if s in _KNOWN_BROAD_ETFS:
        return "BROAD_ETF"
    if s in _KNOWN_COMMODITIES:
        return "COMMODITY"
    if s in _KNOWN_BONDS:
        return "BOND"
    return "STOCK"  # default: treat unrecognised symbols as plain equities


def get_score_boost(symbol: str) -> float:
    """Return the score multiplier for a symbol's asset class."""
    asset_class = classify_asset_class(symbol)
    if asset_class == "STOCK":
        return DEFAULT_STOCK_SCORE_BOOST
    return ASSET_CLASS_SCORE_BOOST.get(asset_class, 1.0)

MAX_POSITIONS = 21
MIN_BUY_USD = 50.0
CASH_TARGET_MIN_PCT = 15.0
# fix/cash-emergency-floor: Bible-Hard-Floor unterhalb des Soft-Floors.
# Buys blockt bereits der 15%-Soft-Floor; unter 10% ist der Cash-Stand
# ein Incident (Positionen ueber Plan / Reconcile-Drift) → Gate meldet
# EMERGENCY, monitor_worker alarmiert.
# (There is no CASH_TARGET_MAX gate: idle cash above the target band is
# not a reason to BLOCK buys — the config's cash_target_max_pct is an
# informational band bound, not enforced here.)
CASH_EMERGENCY_PCT = 10.0
MAX_TOTAL_EXPOSURE_PCT = 75.0
# NB: the enforced correlation thresholds live in bot.core.correlation
# (CORRELATION_BLOCK_THRESHOLD=0.80 / CORRELATION_REDUCE_THRESHOLD=0.60),
# which is what check_correlation_gate_risk actually calls. A duplicate
# MAX_CORRELATION constant used to sit here, read by nothing — removed so
# nobody "tunes" a dead value expecting it to change gate behaviour.

# fix/autonomy-hardening: execution-time slippage guard.
# Max allowed deviation between signal price and live price at execution.
MAX_SLIPPAGE_PCT_DEFAULT = 1.5   # stocks / ETFs
MAX_SLIPPAGE_PCT_CRYPTO = 3.0    # crypto is more volatile between cycles

# fix/autonomy-hardening: automatic daily-loss kill switch.
# 0.0 disables the check. Overridable via config risk.daily_loss_limit_pct.
DAILY_LOSS_LIMIT_PCT_DEFAULT = 5.0

# fix/multi-horizon-loss-limits: the daily kill switch is intraday only
# (resets each UTC day against DAY_START_EQUITY), so a slow bleed of e.g.
# -4.9%/day never trips it. The rolling-peak regime reduces sizing over 30
# days but never HARD-stops. These add hard weekly/monthly circuit breakers,
# measured as max drawdown from the trailing-window equity high (MDD
# semantics, matching the old Trading Bible's MDD_WEEKLY/MONTHLY intent) —
# strictly more conservative than a point-to-point 7/30-days-ago compare and
# reuses regime.get_rolling_peak(). 0.0 disables. Overridable via config.
WEEKLY_LOSS_LIMIT_PCT_DEFAULT = 8.0
MONTHLY_LOSS_LIMIT_PCT_DEFAULT = 12.0

SL_HARD_CLOSE_PCT = -3.0
SL_EMERGENCY_PCT = -4.0
SL_WARNING_PCT = -2.0

# V5: SL quality threshold — SL further than this % from entry = meaningless
SL_MAX_DISTANCE_PCT = 50.0
# V5: Default SL percentage for crypto (always relative)
CRYPTO_DEFAULT_SL_PCT = 3.0

# ─── Config-Wiring ────────────────────────────────────────────────────────────
# fix/risk-config-wiring: der Kommentar "overridden by config" oben war eine
# Luege — KEINE der Konstanten las jemals config.yaml. Eine Config-Aenderung
# (z.B. neues Instrument-Limit) war wirkungslos, ohne dass es jemand merkte.
# apply_config() wird von den Workern nach dem Config-Load aufgerufen.
# Dicts werden IN-PLACE mutiert (andere Module halten importierte Referenzen,
# z.B. concentration_monitor auf INSTRUMENT_LIMITS).

def apply_config(cfg: dict) -> None:
    """Override risk constants from config.yaml. Idempotent, fail-safe."""
    global MAX_POSITIONS, MIN_BUY_USD, CASH_TARGET_MIN_PCT
    global CASH_EMERGENCY_PCT
    global SL_HARD_CLOSE_PCT, SL_EMERGENCY_PCT, SL_WARNING_PCT
    global MAX_FRAGMENTS_PER_INSTRUMENT

    if not cfg:
        return
    trading = cfg.get("trading", {}) or {}
    sl = cfg.get("sl", {}) or {}

    try:
        MAX_POSITIONS = int(trading.get("max_positions", MAX_POSITIONS))
        MIN_BUY_USD = float(trading.get("min_buy_usd", MIN_BUY_USD))
        CASH_TARGET_MIN_PCT = float(trading.get("cash_target_min_pct", CASH_TARGET_MIN_PCT))
        CASH_EMERGENCY_PCT = float(trading.get("cash_emergency_pct", CASH_EMERGENCY_PCT))
        MAX_FRAGMENTS_PER_INSTRUMENT = int(
            trading.get("max_fragments_per_instrument", MAX_FRAGMENTS_PER_INSTRUMENT)
        )
        # SL-Schwellen: config fuehrt positive Distanzen, evaluate_sl arbeitet
        # mit negativen PnL-Schwellen.
        if "default_pct" in sl:
            SL_HARD_CLOSE_PCT = -abs(float(sl["default_pct"]))
        if "emergency_pct" in sl:
            SL_EMERGENCY_PCT = -abs(float(sl["emergency_pct"]))
        if "warning_pct" in sl:
            SL_WARNING_PCT = -abs(float(sl["warning_pct"]))

        # Instrument-Limits: in-place mergen (importierte Referenzen behalten)
        cfg_limits = cfg.get("instrument_limits", {}) or {}
        for sym, limit in cfg_limits.items():
            INSTRUMENT_LIMITS[str(sym).upper()] = float(limit)
    except (TypeError, ValueError) as exc:
        import logging
        logging.getLogger(__name__).error(
            "apply_config: ungueltiger Config-Wert — Teilanwendung gestoppt: %s", exc
        )


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class GateResult:
    allowed: bool
    reasons: list[str]

    def __bool__(self) -> bool:
        return self.allowed

    def summary(self) -> str:
        return " | ".join(self.reasons)


# ─── Individual Gates ─────────────────────────────────────────────────────────

def check_regime_gate(regime: str) -> GateResult:
    """Rule 3 V5: Regime-based BUY gate with 4 levels.

    V4: Binary DRAWDOWN=block / other=allow
    V5: 4 levels — CRITICAL allows only VERY_HIGH, DEFENSIVE allows HIGH+,
        CAUTION allows MEDIUM+, NORMAL allows all.
        This gate only checks if the regime itself allows ANY buys.
        Conviction filtering happens in check_conviction_gate().
    """
    if regime == "CRITICAL":
        # Still allows VERY_HIGH signals — signal_worker filters conviction
        return GateResult(True, [f"REGIME {regime}: nur VERY_HIGH Signale (Quarter-Kelly)"])
    if regime == "DEFENSIVE":
        return GateResult(True, [f"REGIME {regime}: nur HIGH+ Signale (Half-Kelly)"])
    if regime == "CAUTION":
        return GateResult(True, [f"REGIME {regime}: nur MEDIUM+ Signale (75% Sizing)"])
    if regime == "NORMAL":
        return GateResult(True, ["REGIME NORMAL: alle Signale erlaubt (100% Sizing)"])
    # Legacy V4 compatibility
    if regime == "DRAWDOWN":
        return GateResult(False, ["🛑 DRAWDOWN-Regime: BUYs blockiert (Legacy V4)"])
    return GateResult(True, [f"REGIME OK: {regime}"])


def check_conviction_gate(conviction: str, regime: str) -> GateResult:
    """V5 NEW: Minimum conviction required for current regime.

    NORMAL:    all (LOW, MEDIUM, HIGH, VERY_HIGH)
    CAUTION:   MEDIUM, HIGH, VERY_HIGH
    DEFENSIVE: HIGH, VERY_HIGH
    CRITICAL:  VERY_HIGH only
    """
    from bot.core.regime import get_min_conviction
    min_conv = get_min_conviction(regime)
    order = {"VERY_HIGH": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    min_idx = order.get(min_conv, 3)
    our_idx = order.get(conviction.upper(), 3)
    if our_idx > min_idx:
        return GateResult(False, [
            f"Conviction-Gate: {conviction} nicht ausreichend im {regime}-Regime "
            f"(Minimum: {min_conv})"
        ])
    return GateResult(True, [f"Conviction OK: {conviction} ≥ {min_conv} ({regime})"])


# Trading Bible: max. Fragmente (Teilkäufe/DCA-Entries) pro Instrument.
# Config-Key: trading.max_fragments_per_instrument. 0 = deaktiviert.
MAX_FRAGMENTS_PER_INSTRUMENT = 3


def check_pyramiding_gate(
    symbol: str,
    regime: str,
    existing_fragments: int,
    max_fragments: int | None = None,
) -> GateResult:
    """V5: Pyramiding forbidden in DEFENSIVE and CRITICAL regimes.

    Prevents 'good money after bad' — no adding to positions when system
    is already under stress.

    fix/fragment-limit: zusätzlich hartes Fragment-Limit pro Instrument
    (Bible: max_fragments_per_instrument, default 3) — gilt UNABHÄNGIG
    vom Regime. Vorher wurde das Limit nirgends geprüft: in NORMAL/CAUTION
    waren unbegrenzte Fragmente möglich, bis das %-Limit griff.

    max_fragments=None → Laufzeit-Auflösung über die (per apply_config
    überschreibbare) Modul-Konstante; ein Definitionszeit-Default würde
    Config-Overrides ignorieren.
    """
    if max_fragments is None:
        max_fragments = MAX_FRAGMENTS_PER_INSTRUMENT
    if max_fragments > 0 and existing_fragments >= max_fragments:
        return GateResult(False, [
            f"Fragment-Limit: {symbol} hat bereits {existing_fragments} Fragment(e) "
            f"(Max: {max_fragments}/Instrument, Trading Bible)"
        ])

    from bot.core.regime import is_pyramiding_allowed
    if existing_fragments > 0 and not is_pyramiding_allowed(regime):
        return GateResult(False, [
            f"Pyramiding-Gate: {symbol} hat bereits {existing_fragments} Fragment(e) — "
            f"kein Pyramiding im {regime}-Regime erlaubt"
        ])
    return GateResult(True, [
        f"Pyramiding OK: {existing_fragments}/{max_fragments} Fragment(e) (Regime: {regime})"
    ])


def check_sl_quality_gate(
    entry_price: float,
    sl_price: float,
    symbol: str = "",
) -> GateResult:
    """V5 NEW: SL quality check — SL >50% from entry = meaningless.

    Catches the XRP $0.01 SL bug and similar issues.
    A stop-loss that is more than SL_MAX_DISTANCE_PCT away from entry
    provides no meaningful risk management.

    For crypto symbols: always validate SL is relative (not near-zero absolute).
    """
    if entry_price <= 0:
        return GateResult(True, ["SL-Quality: skipped (no entry price)"])
    if sl_price <= 0:
        # No SL set at all — check_sl_gate handles this separately
        return GateResult(True, ["SL-Quality: no SL price to validate"])

    distance_pct = abs(entry_price - sl_price) / entry_price * 100

    # Extra check for crypto: SL should never be below 0.1% of entry
    if symbol.upper() in CRYPTO_SYMBOLS and sl_price < entry_price * 0.001:
        return GateResult(False, [
            f"SL-Quality CRYPTO: SL ${sl_price:.4f} ist nahe 0 "
            f"(Entry ${entry_price:.4f}) — faktisch kein SL. "
            f"Verwende relativen SL: entry × (1 - {CRYPTO_DEFAULT_SL_PCT}%)"
        ])

    if distance_pct > SL_MAX_DISTANCE_PCT:
        return GateResult(False, [
            f"SL-Quality: SL {distance_pct:.1f}% vom Entry entfernt "
            f"(Max: {SL_MAX_DISTANCE_PCT:.0f}%) — bedeutungsloser Stop-Loss"
        ])

    return GateResult(True, [
        f"SL-Quality OK: {distance_pct:.1f}% vom Entry (Max: {SL_MAX_DISTANCE_PCT:.0f}%)"
    ])


def calculate_sl_price(
    entry_price: float,
    symbol: str,
    sl_pct: float = 3.0,
) -> float:
    """V5 NEW: Calculate SL price — always relative for crypto.

    For crypto symbols, ALWAYS use relative percentage (never absolute).
    This prevents the $0.01 SL issue on high-unit-price crypto.

    Returns: stop_loss_rate as a price level
    """
    is_crypto = symbol.upper() in CRYPTO_SYMBOLS
    if is_crypto and sl_pct > SL_MAX_DISTANCE_PCT:
        sl_pct = CRYPTO_DEFAULT_SL_PCT  # Force default for crypto

    return round(entry_price * (1.0 - sl_pct / 100.0), 6)


def check_cash_gate(cash: float, equity: float) -> GateResult:
    """Rule: Cash must be ≥ CASH_TARGET_MIN_PCT of equity (soft floor).

    Unter CASH_EMERGENCY_PCT (hard floor) meldet das Gate EMERGENCY —
    funktional blockt beides, aber der Emergency-Fall ist ein Incident
    (Cash sollte durch den Soft-Floor nie so tief fallen) und wird vom
    monitor_worker separat alarmiert.
    """
    if equity <= 0:
        return GateResult(False, ["Cash-Gate: Equity = 0"])
    cash_pct = (cash / equity) * 100
    if cash_pct < CASH_EMERGENCY_PCT:
        return GateResult(False, [
            f"🚨 Cash-EMERGENCY: {cash_pct:.1f}% < {CASH_EMERGENCY_PCT:.0f}% Hard-Floor "
            f"(${cash:.2f} / ${equity:.2f}) — Cash-Stand ist ein Incident, "
            f"Reconcile/Positionsgrößen prüfen"
        ])
    if cash_pct < CASH_TARGET_MIN_PCT:
        return GateResult(False, [
            f"Cash-Gate: {cash_pct:.1f}% < {CASH_TARGET_MIN_PCT:.0f}% Minimum "
            f"(${cash:.2f} / ${equity:.2f})"
        ])
    return GateResult(True, [f"Cash OK: {cash_pct:.1f}% (${cash:.2f})"])


def check_max_positions_gate(open_count: int) -> GateResult:
    """Rule: Max open positions."""
    if open_count >= MAX_POSITIONS:
        return GateResult(False, [
            f"Positions-Gate: {open_count}/{MAX_POSITIONS} — Limit erreicht"
        ])
    return GateResult(True, [f"Positions OK: {open_count}/{MAX_POSITIONS}"])


def check_instrument_limit_gate(
    symbol: str,
    buy_amount: float,
    current_amount: float,
    equity: float,
) -> GateResult:
    """Rule 2: Per-instrument concentration limit.

    Two checks:
    1. If current_amount alone already exceeds the limit → block immediately.
    2. If adding buy_amount would exceed the limit → block.
    """
    if equity <= 0:
        return GateResult(False, ["Instrument-Gate: Equity = 0"])
    limit_pct = INSTRUMENT_LIMITS.get(symbol.upper(), DEFAULT_INSTRUMENT_LIMIT)

    # ── NEW: guard against already-over-limit positions ────────────────────
    current_pct = (current_amount / equity) * 100
    if current_pct > limit_pct:
        return GateResult(False, [
            f"Already over limit: {symbol} at {current_pct:.1f}% "
            f"(Limit: {limit_pct:.0f}%)"
        ])

    new_total = current_amount + buy_amount
    new_pct = (new_total / equity) * 100
    if new_pct > limit_pct:
        return GateResult(False, [
            f"Instrument-Gate: {symbol} würde {new_pct:.1f}% erreichen "
            f"(Limit: {limit_pct:.0f}%)"
        ])
    return GateResult(True, [
        f"Instrument OK: {symbol} {new_pct:.1f}% / {limit_pct:.0f}%"
    ])


def check_min_buy_gate(buy_amount: float) -> GateResult:
    """Rule: Minimum buy amount to avoid micro-fragments."""
    if buy_amount < MIN_BUY_USD:
        return GateResult(False, [
            f"Min-Buy-Gate: ${buy_amount:.2f} < ${MIN_BUY_USD:.0f} Minimum"
        ])
    return GateResult(True, [f"Min-Buy OK: ${buy_amount:.2f}"])


def check_exposure_gate(total_exposed: float, equity: float, buy_amount: float) -> GateResult:
    """Rule: Total portfolio exposure ≤ MAX_TOTAL_EXPOSURE_PCT."""
    if equity <= 0:
        return GateResult(True, ["Exposure-Gate: skipped (equity=0)"])
    new_exposure_pct = ((total_exposed + buy_amount) / equity) * 100
    if new_exposure_pct > MAX_TOTAL_EXPOSURE_PCT:
        return GateResult(False, [
            f"Exposure-Gate: {new_exposure_pct:.1f}% > {MAX_TOTAL_EXPOSURE_PCT:.0f}% Max"
        ])
    return GateResult(True, [
        f"Exposure OK: {new_exposure_pct:.1f}% / {MAX_TOTAL_EXPOSURE_PCT:.0f}%"
    ])


def check_sl_gate(has_stop_loss: bool) -> GateResult:
    """Rule 1: Every BUY must have a stop-loss."""
    if not has_stop_loss:
        return GateResult(False, ["SL-Gate: Stop-Loss ist Pflicht (Trading Bible Rule 1)"])
    return GateResult(True, ["SL OK: Stop-Loss gesetzt"])


def check_asset_class_gate(
    symbol: str,
    buy_amount: float,
    equity: float,
    open_positions: list[dict],  # [{symbol, amount_usd}]
) -> GateResult:
    """Rule 2: Asset-class concentration limits."""
    if equity <= 0:
        return GateResult(True, ["Asset-Class-Gate: skipped (equity=0)"])
    asset_class = ASSET_CLASS_MAP.get(symbol.upper())
    if not asset_class:
        return GateResult(True, [f"Asset-Class OK: {symbol} (kein Mapping)"])

    limit_pct = ASSET_CLASS_LIMITS.get(asset_class, 100.0)
    current_class_total = sum(
        p["amount_usd"] for p in open_positions
        if ASSET_CLASS_MAP.get(p.get("symbol", "").upper()) == asset_class
    )
    new_total = current_class_total + buy_amount
    new_pct = (new_total / equity) * 100

    if new_pct > limit_pct:
        return GateResult(False, [
            f"Asset-Class-Gate: {asset_class} würde {new_pct:.1f}% erreichen "
            f"(Limit: {limit_pct:.0f}%)"
        ])
    return GateResult(True, [
        f"Asset-Class OK: {asset_class} {new_pct:.1f}% / {limit_pct:.0f}%"
    ])


def check_correlation_gate_risk(
    symbol: str,
    open_positions: list[dict],
) -> GateResult:
    """Rule 9 V5: Correlation blocking.

    Blocks BUY if new symbol has r >= 0.80 with any existing position (30-day returns).
    Fails open (allows trade) if yfinance data unavailable.
    """
    try:
        from bot.core.correlation import check_correlation_gate
        allowed, reason = check_correlation_gate(symbol, open_positions)
        return GateResult(allowed, [reason])
    except Exception as e:
        # Fail-open: correlation check failed, don't block trading
        return GateResult(True, [f'Correlation check skipped: {e}'])


def get_max_slippage_pct(symbol: str, cfg: dict | None = None) -> float:
    """Max allowed signal→execution price deviation for *symbol* (%).

    Config keys (optional): trading.max_slippage_pct,
    trading.max_slippage_pct_crypto.
    """
    trading_cfg = (cfg or {}).get("trading", {}) if cfg else {}
    if symbol.upper() in CRYPTO_SYMBOLS:
        return float(trading_cfg.get("max_slippage_pct_crypto", MAX_SLIPPAGE_PCT_CRYPTO))
    return float(trading_cfg.get("max_slippage_pct", MAX_SLIPPAGE_PCT_DEFAULT))


def check_slippage_gate(
    symbol: str,
    signal_price: float | None,
    current_price: float | None,
    max_slippage_pct: float | None = None,
) -> GateResult:
    """fix/autonomy-hardening: block execution when the live price has moved
    too far from the price the signal was generated on.

    Between signal generation (yfinance, up to 15-min-old candles) and
    execution (:04 cron), price can run away — especially after gaps or
    news. Without this gate the bot buys blind at market.

    Semantics:
      - signal_price missing/0  → PASS (legacy trades without signal_price;
        open_position() still applies its own price sanity checks)
      - current_price missing/0 → PASS with warning reason (fail-open:
        the downstream ghost-guard in open_position() blocks price=0 cases)
      - |deviation| > max      → BLOCK (both directions: a price that
        collapsed below the signal price usually means the setup broke,
        not that we get a bargain)
    """
    if max_slippage_pct is None:
        max_slippage_pct = get_max_slippage_pct(symbol)

    if not signal_price or signal_price <= 0:
        return GateResult(True, ["Slippage-Gate: skipped (kein signal_price)"])
    if not current_price or current_price <= 0:
        return GateResult(True, [
            "Slippage-Gate: skipped (kein Live-Preis verfügbar — "
            "open_position() Ghost-Guard greift)"
        ])

    deviation_pct = (current_price - signal_price) / signal_price * 100.0
    if abs(deviation_pct) > max_slippage_pct:
        return GateResult(False, [
            f"Slippage-Gate: {symbol} Preis {deviation_pct:+.2f}% vs. Signal "
            f"(Signal ${signal_price:.6f} → Live ${current_price:.6f}, "
            f"Max ±{max_slippage_pct:.1f}%)"
        ])
    return GateResult(True, [
        f"Slippage OK: {deviation_pct:+.2f}% (Max ±{max_slippage_pct:.1f}%)"
    ])


def check_daily_loss_breach(
    day_start_equity: float,
    current_equity: float,
    limit_pct: float = DAILY_LOSS_LIMIT_PCT_DEFAULT,
) -> tuple[bool, float]:
    """fix/autonomy-hardening: automatic kill-switch trigger.

    Returns (breached, day_pnl_pct). breached=True when equity dropped
    more than limit_pct below the day-start equity. limit_pct <= 0
    disables the check entirely.
    """
    if limit_pct <= 0 or day_start_equity <= 0 or current_equity <= 0:
        return (False, 0.0)
    day_pnl_pct = (current_equity - day_start_equity) / day_start_equity * 100.0
    return (day_pnl_pct <= -abs(limit_pct), day_pnl_pct)


def check_trailing_loss_breach(
    window_peak_equity: float,
    current_equity: float,
    limit_pct: float,
) -> tuple[bool, float]:
    """fix/multi-horizon-loss-limits: hard weekly/monthly circuit breaker.

    Returns (breached, drawdown_pct). breached=True when current_equity is
    more than limit_pct below the trailing-window equity high (7- or 30-day
    peak, supplied by regime.get_rolling_peak). This is max-drawdown-from-peak
    semantics, not a point-to-point compare — a single low reading N days ago
    can never mask an ongoing bleed. limit_pct <= 0 disables the check.

    drawdown_pct is signed (negative = loss) for symmetry with
    check_daily_loss_breach's day_pnl_pct.
    """
    if limit_pct <= 0 or window_peak_equity <= 0 or current_equity <= 0:
        return (False, 0.0)
    drawdown_pct = (current_equity - window_peak_equity) / window_peak_equity * 100.0
    return (drawdown_pct <= -abs(limit_pct), drawdown_pct)


# ─── Master BUY Gate ──────────────────────────────────────────────────────────

def check_buy_gate(
    symbol: str,
    buy_amount: float,
    equity: float,
    cash: float,
    regime: str,
    open_count: int = 0,
    current_symbol_amount: float = 0.0,
    total_exposed: float = 0.0,
    has_stop_loss: bool = True,
    open_positions: list[dict] | None = None,
    # V5 new parameters
    conviction: str = "MEDIUM",
    existing_fragments: int = 0,
    entry_price: float = 0.0,
    sl_price: float = 0.0,
    max_fragments: int | None = None,
) -> GateResult:
    """Master gate V5 — all rules in sequence. Returns on first block.

    V5 additions:
    - check_conviction_gate: regime-dependent minimum signal strength
    - check_pyramiding_gate: no adding to positions in DEFENSIVE/CRITICAL
    - check_sl_quality_gate: SL >50% from entry = meaningless (blocks order)

    Order: cheapest/most-likely-to-block first.
    """
    open_positions = open_positions or []

    checks = [
        check_regime_gate(regime),
        check_conviction_gate(conviction, regime),           # V5: conviction filter
        check_pyramiding_gate(symbol, regime, existing_fragments, max_fragments),  # V5 + Fragment-Limit
        check_cash_gate(cash, equity),
        check_max_positions_gate(open_count),
        check_instrument_limit_gate(symbol, buy_amount, current_symbol_amount, equity),
        check_min_buy_gate(buy_amount),
        check_exposure_gate(total_exposed, equity, buy_amount),
        check_sl_gate(has_stop_loss),
        check_sl_quality_gate(entry_price, sl_price, symbol),  # V5: SL quality
        check_asset_class_gate(symbol, buy_amount, equity, open_positions),
        check_correlation_gate_risk(symbol, open_positions),  # V5: correlation — last (slowest)
    ]

    all_reasons: list[str] = []
    for result in checks:
        if not result.allowed:
            return GateResult(False, result.reasons)
        all_reasons.extend(result.reasons)

    return GateResult(True, all_reasons)


# ─── SL Enforcement ──────────────────────────────────────────────────────────

@dataclass
class SLAction:
    action: str   # 'CLOSE' | 'WARNING' | 'OK'
    reason: str
    pnl_pct: float


def evaluate_sl(pnl_pct: float) -> SLAction:
    """Trading Bible Rule 1: Evaluate stop-loss action for a position.

    Args:
        pnl_pct: Current unrealized PnL in percent (negative = loss)

    Returns:
        SLAction with action and reason
    """
    if pnl_pct <= SL_EMERGENCY_PCT:
        return SLAction("CLOSE", f"EMERGENCY-SL: {pnl_pct:.2f}% ≤ {SL_EMERGENCY_PCT:.0f}%", pnl_pct)
    if pnl_pct <= SL_HARD_CLOSE_PCT:
        return SLAction("CLOSE", f"HARD-SL: {pnl_pct:.2f}% ≤ {SL_HARD_CLOSE_PCT:.0f}%", pnl_pct)
    if pnl_pct <= SL_WARNING_PCT:
        return SLAction("WARNING", f"SL-WARNING: {pnl_pct:.2f}% ≤ {SL_WARNING_PCT:.0f}%", pnl_pct)
    return SLAction("OK", f"SL OK: {pnl_pct:.2f}%", pnl_pct)
