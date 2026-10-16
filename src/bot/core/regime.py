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

# ── feat/market-regime (2026-10-16, VoLLi) ───────────────────────────────────
# Das Regime kommt aus dem MARKT (S&P 500 / SPY: SMA200 + Abstand vom
# 52-Wochen-Hoch). Der Konto-Drawdown (Konstanten oben, Config 20/30/40 %) ist
# nur noch die gestufte Bremse. Es gilt das STRENGERE von beiden.
# regime.source: "account" (Code-Default, altes Verhalten) | "market".
REGIME_SOURCE: str = "account"
MARKET_INSTRUMENT_ID: int = 3000          # SPY
MARKET_CAUTION_DD_PCT: float = 10.0       # >= 10 % unter 52W-Hoch -> CAUTION
MARKET_CAUTION_EXIT_DD_PCT: float = 8.0
MARKET_SMA_EXIT_BUFFER_PCT: float = 1.0   # CAUTION-Austritt erst > SMA200 * 1.01
MARKET_DEFENSIVE_DD_PCT: float = 15.0     # unter SMA200 UND >= 15 % -> DEFENSIVE
MARKET_DEFENSIVE_EXIT_DD_PCT: float = 12.0
MARKET_CRITICAL_DD_PCT: float = 25.0      # >= 25 % -> CRITICAL
MARKET_CRITICAL_EXIT_DD_PCT: float = 20.0
MARKET_MAX_STALENESS_DAYS: int = 4        # Kalendertage (Wochenende toleriert)
EXIT_GAP_PCT: float = 2.0                 # erzwungener Abstand Austritt < Eintritt

_SEVERITY = {"NORMAL": 0, "CAUTION": 1, "DEFENSIVE": 2, "CRITICAL": 3}


def stricter(a: str, b: str) -> str:
    """Das strengere von zwei Regimen."""
    return a if _SEVERITY.get(a, 0) >= _SEVERITY.get(b, 0) else b

REGIMES = frozenset({"NORMAL", "CAUTION", "DEFENSIVE", "CRITICAL"})


def apply_config(cfg: dict) -> None:
    """Override the drawdown regime thresholds from config.yaml's `regime:`
    block. Idempotent, fail-safe (bad values leave the defaults intact).

    fix/regime-config-wiring: detect_regime reads these module globals
    directly, but nothing ever overrode them from config — editing the
    config had zero effect on regime detection (the same 'config wiring lie'
    already fixed for risk.py's own constants). Keys map 1:1 to the 4-state
    model; the old drawdown_soft_cb_pct/normal_upper_pct keys were leftovers
    from the pre-V5 3-state model and mapped to nothing.

    fix/regime-min-conviction-config (2026-08-28): auch `min_conviction` pro
    Regime ist jetzt ueberschreibbar (`regime.min_conviction.<REGIME>`). Der
    Wert stand hart in _REGIME_PARAMS, obwohl er das schaerfste Gate im System
    ist: DEFENSIVE verlangte HIGH+, und seit dem Combo-Fix d0a07d7 (Conviction
    = schwaechste Komponente) sind fast alle Signale MEDIUM — der Handel kam
    dadurch drei Tage lang vollstaendig zum Erliegen.
    """
    global CAUTION_THRESHOLD, DEFENSIVE_THRESHOLD, CRITICAL_THRESHOLD
    global CAUTION_EXIT, DEFENSIVE_EXIT, CRITICAL_EXIT
    global REGIME_SOURCE, MARKET_CAUTION_DD_PCT, MARKET_CAUTION_EXIT_DD_PCT
    global MARKET_DEFENSIVE_DD_PCT, MARKET_DEFENSIVE_EXIT_DD_PCT
    global MARKET_CRITICAL_DD_PCT, MARKET_CRITICAL_EXIT_DD_PCT
    if not cfg:
        return
    rc = cfg.get("regime", {}) or {}
    _apply_min_conviction(rc.get("min_conviction") or {})
    try:
        CAUTION_THRESHOLD = float(rc.get("caution_pct", CAUTION_THRESHOLD))
        DEFENSIVE_THRESHOLD = float(rc.get("defensive_pct", DEFENSIVE_THRESHOLD))
        CRITICAL_THRESHOLD = float(rc.get("critical_pct", CRITICAL_THRESHOLD))
        CAUTION_EXIT = float(rc.get("caution_exit_pct", CAUTION_EXIT))
        DEFENSIVE_EXIT = float(rc.get("defensive_exit_pct", DEFENSIVE_EXIT))
        CRITICAL_EXIT = float(rc.get("critical_exit_pct", CRITICAL_EXIT))
        # feat/market-regime (2026-10-16)
        _src = str(rc.get("source", REGIME_SOURCE) or "account").lower()
        REGIME_SOURCE = _src if _src in ("account", "market") else "account"
        MARKET_CAUTION_DD_PCT = float(rc.get("market_caution_dd_pct", MARKET_CAUTION_DD_PCT))
        MARKET_CAUTION_EXIT_DD_PCT = float(rc.get("market_caution_exit_dd_pct", MARKET_CAUTION_EXIT_DD_PCT))
        MARKET_DEFENSIVE_DD_PCT = float(rc.get("market_defensive_dd_pct", MARKET_DEFENSIVE_DD_PCT))
        MARKET_DEFENSIVE_EXIT_DD_PCT = float(rc.get("market_defensive_exit_dd_pct", MARKET_DEFENSIVE_EXIT_DD_PCT))
        MARKET_CRITICAL_DD_PCT = float(rc.get("market_critical_dd_pct", MARKET_CRITICAL_DD_PCT))
        MARKET_CRITICAL_EXIT_DD_PCT = float(rc.get("market_critical_exit_dd_pct", MARKET_CRITICAL_EXIT_DD_PCT))
        _enforce_exit_below_entry()
    except (TypeError, ValueError):
        import logging
        logging.getLogger(__name__).error(
            "regime.apply_config: ungültiger Config-Wert — Defaults bleiben aktiv"
        )


CONVICTIONS = ("LOW", "MEDIUM", "HIGH", "VERY_HIGH")


def _apply_min_conviction(mc: dict) -> None:
    """Setzt _REGIME_PARAMS[<REGIME>]["min_conviction"] aus der Config.

    Fail-safe wie apply_config: unbekannte Regime oder Conviction-Stufen
    werden protokolliert und ignoriert, der Default bleibt stehen.
    """
    if not isinstance(mc, dict):
        return
    import logging
    log = logging.getLogger(__name__)
    for regime, value in mc.items():
        key = str(regime).upper()
        val = str(value).upper()
        if key not in REGIMES:
            log.error("regime.min_conviction: unbekanntes Regime %r — ignoriert", regime)
            continue
        if val not in CONVICTIONS:
            log.error("regime.min_conviction[%s]: ungültiger Wert %r — Default bleibt",
                      key, value)
            continue
        _REGIME_PARAMS[key]["min_conviction"] = val

# ─── Risk Scalars per Regime ─────────────────────────────────────────────────

RISK_SCALARS: dict[str, float] = {
    "NORMAL":    1.00,  # Full sizing
    "CAUTION":   0.75,  # -25%: reduce new positions
    "DEFENSIVE": 0.50,  # -50%: Half-Kelly, high conviction only
    "CRITICAL":  0.25,  # -75%: Quarter-Kelly, only VERY_HIGH signals
}

# ─── Sizing-Floors ───────────────────────────────────────────────────────────
#
# Im Wert min_buy_usd steckten bis 2026-08-28 ZWEI Bedeutungen, die
# gegenlaeufig wirken — daher jetzt getrennt:
#
#   signal_floor_usd (hier, regimeabhaengig 200 — fix/fee-flat 2026-10-09
#       einheitlich $200, weil die Flat-Fee $1/$2 bei < $200 = 1-3 %
#       des Notional und jede Groesse darunter die Fee-Last nicht
#       wirtschaftlich traegt)
#       "Lohnt sich dieses Signal ueberhaupt?" — eine Groesse, die schon vor
#       den situativen Haircuts zu klein ist, traegt die These nicht. Wird
#       EINMAL geprueft, VOR Risk-Parity/Korrelation/Region.
#
#   dust_floor_usd() (unten, regimekonstante Broker-Oekonomie, im strengen
#       Regime angehoben)
#       "Ist die Order noch wirtschaftlich?" — Spread und Mindestvolumen
#       interessiert das Regime nicht. Wird am ENDE der Kette geprueft,
#       nach jedem Multiplikator.
#
# Warum der Faktor SIZING_PARITY_FLOOR: die ATR-Risk-Parity im signal_worker
# skaliert bewusst bis auf diesen Faktor herunter (hoher ATR heisst kleiner,
# nicht gar nicht). Ein Dust-Floor oberhalb von signal_floor * SIZING_PARITY_FLOOR
# wuerde genau diese Absicht wieder aufheben — gemessen ueber die 30 Tage bis
# 2026-08-28 haette ein Dust-Floor auf Hoehe des Regime-Floors 54 von 222
# Trades (24.3 %) verworfen, und zwar das einzige Segment mit positivem
# Ergebnis (n=38, WR 39.5 %, +3.55 USD gegen -111.83 USD im Rest).
# Die Ableitung koppelt beide Werte, statt sie unabhaengig driften zu lassen.

SIZING_PARITY_FLOOR: float = 0.6   # == signal_worker ATR-Risk-Parity-Untergrenze


def dust_floor_usd(regime: str, global_min_buy_usd: float = 50.0) -> float:
    """Untergrenze fuer den FINALEN Ordersbetrag, nach allen Multiplikatoren.

    max(signal_floor * SIZING_PARITY_FLOOR, global_min_buy_usd):
    der Config-Wert trading.min_buy_usd ist die absolute Broker-Untergrenze,
    strengere Regimes heben sie an.

    fix/fee-flat (2026-10-09): signal_floor ist jetzt in allen Regimes
    200.0, daher 200 * 0.6 = 120 als derived; die globale Untergrenze
    trading.min_buy_usd (50) unterliegt. Das finale $200-Runden
    passiert im execution_worker (vor dem API-Call) — hier bleibt
    der Dust-Check als Absicherung.

    NORMAL     200 -> 120.00     DEFENSIVE  200 -> 120.00
    CAUTION    200 -> 120.00     CRITICAL   200 -> 120.00
    """
    params = _REGIME_PARAMS.get(regime) or _REGIME_PARAMS["NORMAL"]
    derived = float(params["signal_floor_usd"]) * SIZING_PARITY_FLOOR
    return round(max(derived, float(global_min_buy_usd)), 2)


# ─── Regime Parameters ────────────────────────────────────────────────────────

_REGIME_PARAMS: dict[str, dict] = {
    "NORMAL": {
        "cash_min_pct":       15.0,
        # fix/sizing-base-10 (2026-10-09, VoLLi-Entscheid): Cap folgt der
        # neuen 10-%-Conviction-Base (6.0 -> 10.0). Vorher kappte der 6.0-Cap
        # die Base sofort zurueck — ein Parameter, der der Zielgroesse
        # widerspricht, ist eine Falle (siehe 2026-08-29-Notiz).
        # Regime-Leiter bleibt monoton fallend: 10.0 > 7.5 > 5.0 > 2.5.
        "max_trade_pct":       10.0,
        "buy_aggressiveness":  1.0,
        "signal_floor_usd":        200.0,   # fix/fee-flat: $200-Floor (Fee-Last)
        "allow_pyramiding":   True,
        "min_conviction":     "LOW",
        "description":        "Standard — alle Signale erlaubt",
    },
    "CAUTION": {
        "cash_min_pct":       20.0,   # Higher buffer
        # fix/sizing-base-10 (2026-10-09): Cap folgt der neuen Base
        # (10 % x 0.75 = 7.5 %). Vorher 4.5, mit conviction 6.0 kalibriert.
        "max_trade_pct":       7.5,   # Slightly smaller trades
        "buy_aggressiveness":  0.75,  # risk_scalar applied
        "signal_floor_usd":        200.0,   # fix/fee-flat: $200-Floor
        "allow_pyramiding":   True,   # Still allowed but at reduced size
        "min_conviction":     "MEDIUM",  # No LOW signals
        "description":        "Erhöhte Vorsicht — nur MEDIUM+ Signale",
    },
    "DEFENSIVE": {
        "cash_min_pct":       25.0,
        # fix/sizing-base-10 (2026-10-09): Cap folgt der neuen Base
        # (10 % x 0.50 = 5.0 %). Vorher 3.0, mit conviction 6.0 kalibriert
        # (3.0 % = 150.71 USD — genau das, was die Base kappte).
        "max_trade_pct":       5.0,
        "buy_aggressiveness":  0.50,
        "signal_floor_usd":       200.0,   # fix/fee-flat: $200-Floor
        "allow_pyramiding":   False,  # No adding to existing positions
        "min_conviction":     "HIGH",  # Only HIGH and VERY_HIGH
        "description":        "Defensiv — kein Pyramiding, min. Conviction laut Config (MEDIUM)",
    },
    "CRITICAL": {
        "cash_min_pct":       30.0,
        # fix/sizing-base-10 (2026-10-09): Cap folgt der neuen Base
        # (10 % x 0.25 = 2.5 %). Vorher 2.0, mit conviction 6.0 kalibriert.
        "max_trade_pct":       2.5,
        "buy_aggressiveness":  0.25,
        "signal_floor_usd":       200.0,   # fix/fee-flat: $200-Floor
        "allow_pyramiding":   False,
        "min_conviction":     "VERY_HIGH",  # Only best signals
        "description":        "Kritisch — nur VERY_HIGH Signale, Quarter-Kelly",
    },
}


def get_regime_params(regime: str) -> dict:
    """Get Trading Bible V5 parameters for a given regime."""
    if regime not in REGIMES:
        raise ValueError(f"Unknown regime {regime!r}. Must be one of {sorted(REGIMES)}")
    params = dict(_REGIME_PARAMS[regime])
    # Alias fuer Altleser (trade_veto_worker, externe Skripte). Neuer Code
    # nimmt signal_floor_usd bzw. dust_floor_usd() — der alte Name sagte
    # nicht, WELCHE der beiden Bedeutungen gemeint war.
    params["min_buy_usd"] = params["signal_floor_usd"]
    return params


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


# ─── Rolling Peak (fix/rolling-peak-drawdown) ────────────────────────────────
# PEAK_EQUITY war ein All-Time-High ohne Verfall: nach einem starken Run-up
# erzwang jeder normale 8%-Pullback DEFENSIVE, obwohl das Konto hochprofitabel
# war — uebertriebene Defensive als Renditebremse. Das Regime rechnet den
# Drawdown jetzt gegen das Hoch der letzten ROLLING_PEAK_DAYS Kalendertage;
# das All-Time-PEAK_EQUITY bleibt fuers Reporting erhalten.

ROLLING_PEAK_DAYS = 30


def record_equity_snapshot(db, equity: float) -> None:
    """Persist today's equity high into equity_history (lazy CREATE, idempotent)."""
    if db is None or equity <= 0:
        return
    db.execute("""
        CREATE TABLE IF NOT EXISTS equity_history (
            date        TEXT PRIMARY KEY,
            equity_high REAL NOT NULL
        )
    """)
    db.execute("""
        INSERT INTO equity_history (date, equity_high)
        VALUES (date('now'), ?)
        ON CONFLICT(date) DO UPDATE SET
            equity_high = MAX(equity_history.equity_high, excluded.equity_high)
    """, (equity,))


def get_rolling_peak(db, current_equity: float, days: int = ROLLING_PEAK_DAYS) -> float:
    """Highest equity over the last *days* calendar days (incl. current)."""
    if db is None:
        return current_equity
    row = db.fetchone(
        "SELECT MAX(equity_high) FROM equity_history WHERE date >= date('now', ?)",
        (f"-{days} days",),
    )
    hist_peak = float(row[0]) if row and row[0] is not None else 0.0
    return max(hist_peak, current_equity)


# ─── StateRepo Integration ────────────────────────────────────────────────────

def _enforce_exit_below_entry() -> None:
    """feat/market-regime: Austritt muss UNTER dem Eintritt liegen.

    Anlass: die LLM senkte am 09.07. regime.caution_pct 4 -> 3, der Austritt
    blieb 3.5 — die Hysterese war drei Monate verdreht. Jetzt wird jeder
    Austritt >= Eintritt auf Eintritt - EXIT_GAP_PCT gesetzt (laut geloggt).
    """
    global CAUTION_EXIT, DEFENSIVE_EXIT, CRITICAL_EXIT
    global MARKET_CAUTION_EXIT_DD_PCT, MARKET_DEFENSIVE_EXIT_DD_PCT, MARKET_CRITICAL_EXIT_DD_PCT
    import logging
    _log = logging.getLogger(__name__)
    pairs = [
        ("CAUTION_EXIT", "CAUTION_THRESHOLD"), ("DEFENSIVE_EXIT", "DEFENSIVE_THRESHOLD"),
        ("CRITICAL_EXIT", "CRITICAL_THRESHOLD"),
        ("MARKET_CAUTION_EXIT_DD_PCT", "MARKET_CAUTION_DD_PCT"),
        ("MARKET_DEFENSIVE_EXIT_DD_PCT", "MARKET_DEFENSIVE_DD_PCT"),
        ("MARKET_CRITICAL_EXIT_DD_PCT", "MARKET_CRITICAL_DD_PCT"),
    ]
    g = globals()
    for exit_name, entry_name in pairs:
        if g[exit_name] >= g[entry_name]:
            fixed = round(g[entry_name] - EXIT_GAP_PCT, 4)
            _log.error("regime: %s=%.2f >= %s=%.2f — Austritt auf %.2f gesetzt (Hysterese)",
                       exit_name, g[exit_name], entry_name, g[entry_name], fixed)
            g[exit_name] = fixed


def market_metrics(db, instrument_id: int | None = None) -> dict | None:
    """SPY-Kennzahlen aus ohlcv_daily: Schluss, SMA200, Abstand vom 52W-Hoch.

    None, wenn weniger als 200 Schlusskurse vorliegen.
    """
    iid = int(instrument_id or MARKET_INSTRUMENT_ID)
    rows = db.fetchall(
        "SELECT substr(date,1,10) AS d, close FROM ohlcv_daily "
        "WHERE instrument_id = ? AND close > 0 ORDER BY date", (iid,))
    if not rows or len(rows) < 200:
        return None
    closes = [float(r["close"]) for r in rows]
    sma = sum(closes[-200:]) / 200.0
    hi = max(closes[-252:])
    return {"date": rows[-1]["d"], "close": closes[-1], "sma200": sma,
            "dd_pct": (hi - closes[-1]) / hi * 100.0 if hi > 0 else 0.0,
            "n": len(closes)}


def detect_market_regime(m: dict, previous: str | None) -> tuple[str, str]:
    """Marktregime aus SPY-Kennzahlen, mit Hysterese gegen ``previous``."""
    dd, close, sma = float(m["dd_pct"]), float(m["close"]), float(m["sma200"])
    below = close < sma
    prev = previous if previous in _SEVERITY else "NORMAL"
    vs = (close / sma - 1.0) * 100.0 if sma > 0 else 0.0
    tag = f"SPY {vs:+.1f}% vs SMA200, {dd:.1f}% unter 52W-Hoch"
    if dd >= MARKET_CRITICAL_DD_PCT:
        return "CRITICAL", tag
    if prev == "CRITICAL" and dd > MARKET_CRITICAL_EXIT_DD_PCT:
        return "CRITICAL", tag + " (Hysterese)"
    if below and dd >= MARKET_DEFENSIVE_DD_PCT:
        return "DEFENSIVE", tag
    if prev in ("DEFENSIVE", "CRITICAL") and dd > MARKET_DEFENSIVE_EXIT_DD_PCT:
        return "DEFENSIVE", tag + " (Hysterese)"
    if below or dd >= MARKET_CAUTION_DD_PCT:
        return "CAUTION", tag
    if prev in ("CAUTION", "DEFENSIVE", "CRITICAL") and (
            close < sma * (1.0 + MARKET_SMA_EXIT_BUFFER_PCT / 100.0)
            or dd > MARKET_CAUTION_EXIT_DD_PCT):
        return "CAUTION", tag + " (Hysterese)"
    return "NORMAL", tag


def _market_regime_step(state_repo) -> tuple[str, str]:
    """Marktregime berechnen und persistieren. Bei fehlenden/veralteten Daten:
    mindestens CAUTION (stricter(letztes Marktregime, CAUTION)) + einmal
    taeglich ein Alert — NIE auf die (weiten) Konto-Stufen allein zurueckfallen."""
    import logging
    from datetime import date, datetime, timezone
    prev = state_repo.get("MARKET_REGIME") or "NORMAL"
    m = None
    try:
        m = market_metrics(state_repo.db)
    except Exception as exc:
        logging.getLogger(__name__).error("regime: Marktdaten nicht lesbar: %s", exc)
    today = datetime.now(timezone.utc).date()
    stale = m is None or (today - date.fromisoformat(m["date"])).days > MARKET_MAX_STALENESS_DAYS
    if stale:
        fallback = stricter(prev, "CAUTION")
        why = ("keine 200 SPY-Kurse" if m is None
               else f"letzter SPY-Kurs {m['date']} aelter als {MARKET_MAX_STALENESS_DAYS} Tage")
        logging.getLogger(__name__).error("regime: Marktdaten fehlen/veraltet (%s) -> %s", why, fallback)
        if state_repo.get("MARKET_STALE_ALERT_DATE") != today.isoformat():
            state_repo.set("MARKET_STALE_ALERT_DATE", today.isoformat())
            try:
                from bot.discord_embeds import post_alert_embed
                post_alert_embed(title="⚠️ Marktregime: SPY-Daten fehlen/veraltet",
                                 description=f"{why}. Regime mindestens CAUTION ({fallback}), "
                                             f"bis der Data-Worker SPY nachfuehrt.",
                                 severity="WARNING")
            except Exception:
                pass
        return fallback, f"Marktdaten veraltet ({why}) -> mind. CAUTION"
    regime, reason = detect_market_regime(m, prev)
    state_repo.set("MARKET_REGIME", regime)
    state_repo.set("MARKET_DD_PCT", f"{m['dd_pct']:.2f}")
    state_repo.set("MARKET_VS_SMA200_PCT", f"{(m['close'] / m['sma200'] - 1) * 100:.2f}")
    state_repo.set("MARKET_DATA_DATE", m["date"])
    return regime, reason


def update_regime(state_repo, equity: float) -> tuple[str, bool]:
    """Detect and persist current regime. Returns (new_regime, changed).

    V5: Also updates HIGH_WATERMARK and RISK_SCALAR in system_state.
    fix/rolling-peak-drawdown: Drawdown wird gegen den 30-Tage-Peak
    gerechnet (equity_history); All-Time-PEAK_EQUITY bleibt fuers
    Reporting. Bei DB-Fehlern Fallback auf den All-Time-Peak.
    """
    all_time_peak = state_repo.get_float("PEAK_EQUITY", equity)
    previous_regime = state_repo.get_regime()

    try:
        record_equity_snapshot(state_repo.db, equity)
        peak_equity = get_rolling_peak(state_repo.db, equity)
        state_repo.set("ROLLING_PEAK", str(peak_equity))
    except Exception:
        # Fail-safe: ohne History wie bisher gegen den All-Time-Peak rechnen
        peak_equity = all_time_peak

    # feat/market-regime: Konto-Stufe mit EIGENER Hysterese (ACCOUNT_REGIME),
    # sonst haelt ein Markt-CAUTION die Konto-Stufe ueber deren Hysterese fest.
    account_prev = state_repo.get("ACCOUNT_REGIME") or previous_regime
    account_regime, account_reason = detect_regime(equity, peak_equity, account_prev)
    state_repo.set("ACCOUNT_REGIME", account_regime)
    if REGIME_SOURCE == "market":
        market_regime, market_reason = _market_regime_step(state_repo)
        new_regime = stricter(market_regime, account_regime)
        reason = (f"Markt {market_regime} ({market_reason}) | "
                  f"Konto {account_regime} ({account_reason}) -> {new_regime}")
    else:
        new_regime, reason = account_regime, account_reason
    changed = new_regime != previous_regime

    # Update regime and risk scalar
    state_repo.set_regime(new_regime)
    state_repo.set("RISK_SCALAR", str(get_risk_scalar(new_regime)))
    state_repo.set("DRAWDOWN_REASON", reason)

    # Update drawdown pct (gegen Rolling Peak — konsistent zum Regime)
    if peak_equity > 0:
        dd_pct = (peak_equity - equity) / peak_equity * 100
        state_repo.set("DRAWDOWN_PCT", f"{dd_pct:.4f}")

    # All-Time-Peak fuers Reporting weiter pflegen; HIGH_WATERMARK im
    # Rolling-Modell: volle Risikofreigabe am 30-Tage-Hoch.
    if equity > all_time_peak:
        state_repo.set("PEAK_EQUITY", str(equity))
    state_repo.set(
        "HIGH_WATERMARK_REACHED",
        "true" if equity >= peak_equity else "false",
    )

    return new_regime, changed


def is_at_full_risk(state_repo) -> bool:
    """True only when equity is at or above high-watermark (no drawdown).

    V5 recovery protocol: full risk (risk_scalar=1.0) only at new equity high.
    """
    return state_repo.get("HIGH_WATERMARK_REACHED", "false") == "true"
