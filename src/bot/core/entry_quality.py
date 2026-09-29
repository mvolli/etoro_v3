"""feat/entry-quality (2026-08-22) — Shadow-mode Entry-Quality Gates.

Basis:
  - Live-DB-Evidenz (trading.db, 2026-08-22): MACD_TURN-Split 42.6% WR
    (n=68, +$106) vs. 23.4% (n=175, -$444); Pure-Oversold-Cluster ohne
    Turn 2.7% WR (n=37, -$90); CORE_SWEEP -$171 (n=73).
  - Web-Research (25 Quellen): Confluence/Confirmation, Trend-Filter,
    Volatility-Gating, Regime-Conditional —详见
    references/entry-quality-plan-2026-08-22.md.

PHASE 1 = SHADOW MODE (Default ``mode: shadow``):
  Gates werden bewertet und in ``entry_quality_events`` geloggt — die
  Execution aendert sich NICHT (kein Size-Change, kein Reject). Nach ~50
  Trades bestimmt der Shadow-Abgleich (gate-WR vs. live-WR,
  false-positive-Rate) welche Gates in ``mode: live`` geschaltet werden
  (Phase 2: Sizing-Koeffizienten 0.25-1.0, keine Hard-Blocks).

Gates (config: ``trading.entry_quality.gates.<name>.enabled``):
  1. macd_turn_required — Pure-Oversold-Dip-Buys OHNE MACD_TURN-Component
     → size_mult 0.5. (Beweis: 2.7%-WR-Cluster.)
  2. core_sweep_regime  — CORE_SWEEP nur in allowed Regimes (Default
     NORMAL/CAUTION), optional Trend-Override wenn SMA20>SMA50 oder
     ROC5d > min_roc_5d_pct. (Beweis: -$171 Drag.)
  3. dipbuy_trend       — Dip-Buys brauchen ROC5d > min_roc_5d_pct
     (Default -15). Soft: 0.5.
  4. volume_confirm     — vol_ratio >= min_vol_ratio (Default 1.2).
     Soft: 0.5.
  5. atr_window         — min_atr_pct < ATR% < max_atr_pct
     (Default 0.8/7.0). Soft: 0.5.

Semantics:
  - ``evaluate()`` returns an :class:`EntryQualityEval` with per-gate hits
    and a combined ``size_mult`` = min over all hits (1.0 = no hit).
  - Every gate FAILS OPEN on missing data (wie das Knife-Gate): fehlende
    Metriken = kein Hit, das Gate bestraft keine Datenluecken.
  - A gate hit with ``size_mult = 0.0`` is a (potential) BLOCK — only the
    core_sweep_regime gate uses it, and only in live mode does it skip the
    order.

Idempotenz (AGENTS.md): ``ensure_table`` uses ``CREATE TABLE IF NOT
EXISTS`` and runs once per worker start (best-effort, fail-open).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

TABLE_NAME = "entry_quality_events"

# Default-Gates — werden von config.yaml ueberschrieben (fail-open:
# Config-Abschnitt fehlt = Defaults, shadow mode).
DEFAULT_CONFIG: dict = {
    "enabled": True,
    "mode": "shadow",          # shadow | live
    "min_size_mult": 0.25,     # hart-clamped floor fuer die kombinierten Mults
    "gates": {
        "macd_turn_required": {
            "enabled": True,
            "oversold_types": [
                "BB_LOWER_RSI_OVERSOLD",
                "BB_EXTREME_RSI_OVERSOLD",
                "RSI_EXTREME_OVERSOLD",
            ],
            "macd_turn_types": ["MACD_TURN_BELOW_SMA20"],
            "size_mult": 0.5,
        },
        "core_sweep_regime": {
            "enabled": True,
            "allowed_regimes": ["NORMAL", "CAUTION"],
            "trend_override": True,
            "min_roc_5d_pct": -12.0,
            "size_mult": 0.0,   # in live mode: block (skip) the sweep order
        },
        "dipbuy_regime": {
            # feat/dipbuy-regime-gate (2026-09-11): Dip-Buys brauchen im
            # defensiven Regime Trend-Bestaetigung. Evidenz (New-Regime,
            # created_at >= 2026-08-24 13:16, beides: Conviction-Abflachung
            # 56b6c04 + Entry-Quality P2 5892deb live): MACD_TURN+BB_LOW-
            # Cluster n=75, WR 16.0 %, avg -1.94 %, SUM -126.08 USD — im
            # Alt-Regime (07-26..08-24) n=43, WR 46.5 %, +49.63 USD. Der
            # Dip-Buy-Cluster ist regime-abhaengig (Vault-Concept
            # dip-buy-cluster-regime-dependent-2026-09-05): in CAUTION/
            # DEFENSIVE ohne Trend-Bestaetigung (SMA20<SMA50 UND
            # ROC5d<=min_roc) wird er auf 0.25x gedaeempft. SOFT-Gate
            # (Anti-Brake: kein Block), TREND_PULLBACK/Klassik-Trend bleibt
            # aus, CORE_SWEEP geht ueber core_sweep_regime.
            "enabled": True,
            "applies_to": [
                "BB_LOWER_RSI_OVERSOLD",
                "BB_EXTREME_RSI_OVERSOLD",
                "RSI_EXTREME_OVERSOLD",
                "MACD_TURN_BELOW_SMA20",
                "BB_LOW_MACD_IMPROVING",
            ],
            "allowed_regimes": ["NORMAL", "CAUTION"],
            "trend_override": True,
            "min_roc_5d_pct": -12.0,
            "size_mult": 0.25,
        },
        "dipbuy_trend": {
            "enabled": True,
            "applies_to": [
                "BB_LOWER_RSI_OVERSOLD",
                "BB_EXTREME_RSI_OVERSOLD",
                "RSI_EXTREME_OVERSOLD",
                "MACD_TURN_BELOW_SMA20",
                "BB_LOW_MACD_IMPROVING",
            ],
            "min_roc_5d_pct": -15.0,
            "size_mult": 0.5,
        },
        "volume_confirm": {
            "enabled": True,
            "min_vol_ratio": 1.2,
            "size_mult": 0.5,
        },
        "atr_window": {
            "enabled": True,
            "min_atr_pct": 0.8,
            "max_atr_pct": 7.0,
            "size_mult": 0.5,
        },
        # feat/ma200-filter (2026-09-29, advisor-frozen): block/soft-gate
        # when close[D-1] < SMA200. SHADOW by default — the ledger records
        # every would-be block with entry price; execution is untouched.
        # Live mode applies size_mult 0.25 (soft, min-size-floor protected;
        # a HARD block would need size_mult 0.0 — advisor protocol v2,
        # decision: VoLLi). Evaluation before any live flip: >=50 closed
        # live + >=50 closed shadow-blocked trades.
        "ma200_trend": {
            "enabled": True,
            "mode": "shadow",     # shadow | live
            "window": 200,
            "lag": 0,             # decision bar = last provided close (D-1)
            "size_mult": 0.25,
        },
    },
}

# Dip-Buy-Cluster fuer das macd_turn_required / dipbuy_trend Gate — die
# Signaltypen, die "guenstig" kaufen, bevor der Fall bestaetigt ist.
_DIPBUY_TYPES_DEFAULT = [
    "BB_LOWER_RSI_OVERSOLD",
    "BB_EXTREME_RSI_OVERSOLD",
    "RSI_EXTREME_OVERSOLD",
]


# ─── MA200 Entry-Filter (feat/ma200-filter, 2026-09-29, advisor-frozen) ─────
#
# Regel (FROZEN — kein Parameter-Tuning, advisor-Protokoll 2026-09-29):
#   blockieren, wenn  close[D-1] < SMA200(close[D-200..D-1])
# Live-treu (config ``lag: 0``): das Signal wird intraday am Signal-Tag D
# erzeugt; ``get_daily_closes`` liefert NUR abgeschlossene Bars, der letzte
# ist D-1 — und mit lag=0 entscheidet ``ma200_decision`` genau auf D-1
# (close[D-1] < SMA200(close[D-200..D-1])). Das entspricht dem Replay-
# Referenz-Szenario S12b_L2 (``ma200_lag=2``); die Signal-Tag-Close-Variante
# (S12) war Advisor-Runde-3 als leicht look-ahead-haftig eingeordnet.
# Fail-open (wie das Knife-Gate): <200 geschlossene Bars = KEIN Block.
#
# Evidenz (2026-09-29, trading.db since 2026-07-26, replay S12b_L2):
#   REALIZED blocked-vs-passed (kein Replay-Mechanismus, nur DB):
#     all      n=506: passed 232 (-0.83 $/tr)  blocked 274 (-1.16 $/tr)
#     holdout  n=322: passed 146 (-0.80 $/tr)  blocked 176 (-1.39 $/tr)
#     day-bootstrap CI holdout: [+0.51, +1.76] $/tr (passed-blocked > 0)
#     symbol-bootstrap CI holdout: [-0.12, -0.00] (nicht significant)
#   Fixed-date window (entry+10 bars <= last bar, no mark-to-market date):
#     S12b passed: all -32.4 (WR 37.6%), train +36.7, holdout -69.1 (all CIs
#     include 0) — the filter alone is NOT an OOS edge; its value is
#     avoiding the −$245.3 (holdout, n=176) of realized blocked trades.
#   Concentration: no single symbol/asset class dominates (top symbol
#   CATE.ST = 4.2% of holdout blocked PnL; 93% of blocked = stocks).
#   => Purely REDUCING change (removes trades, adds none). Shadow-first per
#   advisor: evaluate after >=50 closed live + >=50 closed blocked-shadow
#   trades; keep if passed−blocked $/trade point estimate > 0.

def ma200_decision(closes: list[float] | None, *, window: int = 200, lag: int = 1):
    """Frozen MA200 filter rule.

    ``closes``: chronological daily closes, LAST element = latest CLOSED bar
    (D-1 when evaluated intraday on signal day D).

    Returns ``(blocked: bool, detail: dict)``.
    Fail-open: fewer than ``window + lag`` closes → ``(False, {status:
    "insufficient", ...})`` — the gate never blocks on data gaps.
    """
    need = window + lag
    if closes is None or len(closes) < need:
        return False, {"status": "insufficient", "bars": len(closes) if closes else 0, "need": need}
    r = len(closes) - 1 - lag          # index of close[D-1]
    sma = sum(closes[r - window + 1: r + 1]) / window
    px = closes[r]
    blocked = px < sma
    return blocked, {
        "status": "ok",
        "close": px,
        "sma200": sma,
        "below_pct": (px / sma - 1.0) * 100.0 if sma else 0.0,
    }


@dataclass
class GateHit:
    gate: str
    reason: str
    size_mult: float


@dataclass
class EntryQualityEval:
    symbol: str
    signal_type: str
    regime: str
    hits: list[GateHit] = field(default_factory=list)
    # Kombiniert = MIN ueber alle Hits; wird am Ende von evaluate() gesetzt
    # und dort hart-geclampt (min_size_mult). 1.0 = kein Gate getroffen.
    size_mult: float = 1.0
    # ma200_trend specifics (feat/ma200-filter 2026-09-29):
    ma200_detail: dict | None = None   # {status, close, sma200, below_pct, ...}
    ma200_shadow: bool = False         # would-block, shadow mode (ledger only)
    ma200_live: bool = False           # gate hit in live mode

    @property
    def blocked(self) -> bool:
        return any(h.size_mult <= 0.0 for h in self.hits)

    @property
    def live_effective_mult(self) -> float:
        """Execution-side multiplier: shadow ma200-hits are NOT applied.

        A ma200_trend hit in SHADOW mode carries size_mult 0.0 (ledger
        marker only); execution must treat it as 1.0. In live mode the
        configured size_mult applies. All other gates behave unchanged.
        """
        if not self.ma200_shadow:
            return self.size_mult
        others = [h.size_mult for h in self.hits if h.gate != "ma200_trend"]
        return max(min(others, default=1.0), 0.0)

    @property
    def reasons(self) -> str:
        return "; ".join(f"{h.gate}:{h.reason}" for h in self.hits) or "-"


def _merged_config(cfg: dict | None) -> dict:
    """Merged defaults + config (trading.entry_quality), shallow per-gate."""
    cfg = cfg or {}
    eq = (cfg.get("trading", {}) or {}).get("entry_quality", {}) or {}
    out: dict = {
        "enabled": bool(eq.get("enabled", DEFAULT_CONFIG["enabled"])),
        "mode": str(eq.get("mode", DEFAULT_CONFIG["mode"])).lower(),
        "min_size_mult": float(eq.get("min_size_mult", DEFAULT_CONFIG["min_size_mult"])),
        "gates": {},
    }
    for name, dflt in DEFAULT_CONFIG["gates"].items():
        user_gate = (eq.get("gates", {}) or {}).get(name, {}) or {}
        merged = dict(dflt)
        merged.update(user_gate)
        out["gates"][name] = merged
    return out


def _signal_types(signal_type: str) -> list[str]:
    """Split comma-separated signal_type string into components."""
    return [s.strip().upper() for s in (signal_type or "").split(",") if s.strip()]


def evaluate(
    cfg: dict | None,
    *,
    symbol: str,
    signal_type: str,
    indicators: dict,
    regime: str,
    is_core_sweep: bool = False,
    daily_closes: list[float] | None = None,
) -> EntryQualityEval:
    """Evaluate all enabled gates. Pure function, fail-open, no side effects.

    ``indicators``: the dict from ``signals.compute_indicators`` (rsi,
    macd_hist, bb_pct, atr, price, sma20, sma50, vol_ratio, roc_5d_pct,
    ...). Missing keys are tolerated.

    ``daily_closes``: chronological daily closes, last = latest CLOSED bar
    (needed by the ma200_trend gate). ``None`` → gate fails open.
    """
    conf = _merged_config(cfg)
    if not conf["enabled"]:
        return EntryQualityEval(symbol=symbol, signal_type=signal_type or "", regime=regime or "")

    ev = EntryQualityEval(symbol=symbol, signal_type=signal_type or "", regime=regime or "")
    types = _signal_types(signal_type)
    gates = conf["gates"]

    # ── 1. macd_turn_required: Pure-Oversold OHNE MACD_TURN → size down ──
    g = gates.get("macd_turn_required", {})
    if g.get("enabled") and not is_core_sweep:
        oversold = [t for t in types if t in g.get("oversold_types", _DIPBUY_TYPES_DEFAULT)]
        turn = any(t in g.get("macd_turn_types", ["MACD_TURN_BELOW_SMA20"]) for t in types)
        if oversold and not turn:
            ev.hits.append(GateHit(
                "macd_turn_required",
                f"Pure-Oversold {','.join(oversold)} ohne MACD_TURN",
                float(g.get("size_mult", 0.5)),
            ))

    # ── 2. core_sweep_regime: CORE_SWEEP nur in allowed Regimes ──────────
    g = gates.get("core_sweep_regime", {})
    if g.get("enabled") and is_core_sweep:
        allowed = [str(r).upper() for r in g.get("allowed_regimes", ["NORMAL", "CAUTION"])]
        if regime.upper() not in allowed:
            override_ok = False
            if g.get("trend_override"):
                sma20, sma50 = indicators.get("sma20"), indicators.get("sma50")
                roc = indicators.get("roc_5d_pct")
                min_roc = float(g.get("min_roc_5d_pct", -12.0))
                override_ok = (
                    (sma20 is not None and sma50 is not None and sma20 > sma50)
                    or (roc is not None and roc > min_roc)
                )
            if not override_ok:
                ev.hits.append(GateHit(
                    "core_sweep_regime",
                    f"Regime {regime.upper()} not in {allowed}"
                    + ("" if override_ok else " (kein Trend-Override)"),
                    float(g.get("size_mult", 0.0)),
                ))

    # ── 3. dipbuy_trend: ROC5d-Mindestgrenze fuer Dip-Buys ───────────────
    g = gates.get("dipbuy_trend", {})
    if g.get("enabled") and not is_core_sweep:
        applies_to = g.get("applies_to", _DIPBUY_TYPES_DEFAULT)
        if any(t in applies_to for t in types):
            roc = indicators.get("roc_5d_pct")
            min_roc = float(g.get("min_roc_5d_pct", -15.0))
            if roc is not None and roc <= min_roc:
                ev.hits.append(GateHit(
                    "dipbuy_trend",
                    f"ROC5d {roc:.1f}% <= {min_roc}%",
                    float(g.get("size_mult", 0.5)),
                ))

    # ── 4. volume_confirm: Vol-Bestaetigung am Entry ─────────────────────
    g = gates.get("volume_confirm", {})
    if g.get("enabled") and not is_core_sweep:
        vr = indicators.get("vol_ratio")
        min_vr = float(g.get("min_vol_ratio", 1.2))
        if vr is not None and vr < min_vr:
            ev.hits.append(GateHit(
                "volume_confirm",
                f"vol_ratio {vr:.2f} < {min_vr}",
                float(g.get("size_mult", 0.5)),
            ))

    # ── 5. atr_window: Volatilaets-Fenster am Entry ──────────────────────
    g = gates.get("atr_window", {})
    if g.get("enabled"):
        atr, price = indicators.get("atr"), indicators.get("price")
        if atr is not None and price:
            atr_pct = atr / price * 100.0
            lo, hi = float(g.get("min_atr_pct", 0.8)), float(g.get("max_atr_pct", 7.0))
            if not (lo < atr_pct < hi):
                ev.hits.append(GateHit(
                    "atr_window",
                    f"ATR% {atr_pct:.2f} outside ({lo}..{hi})",
                    float(g.get("size_mult", 0.5)),
                ))

    # ── 6. dipbuy_regime: Dip-Buys im defensiven Regime nur mit
    #        Trend-Bestaetigung (feat/dipbuy-regime-gate, 2026-09-11) ─────
    g = gates.get("dipbuy_regime", {})
    if g.get("enabled") and not is_core_sweep:
        applies_to = g.get("applies_to", _DIPBUY_TYPES_DEFAULT)
        if any(t in applies_to for t in types):
            allowed = [str(r).upper() for r in g.get("allowed_regimes", ["NORMAL", "CAUTION"])]
            if regime.upper() not in allowed:
                override_ok = False
                if g.get("trend_override"):
                    sma20, sma50 = indicators.get("sma20"), indicators.get("sma50")
                    roc = indicators.get("roc_5d_pct")
                    min_roc = float(g.get("min_roc_5d_pct", -12.0))
                    # Trend-Bestaetigung: SMA20 > SMA50 ODER ROC5d > min_roc.
                    # Fail-open wie die uebrigen Gates: OHNE Trend-Daten
                    # (beide SMA UND ROC fehlen) greift das Gate NICHT —
                    # ein Indikatorenausfall darf nicht zum stillen
                    # Strategy-Wechsel werden.
                    if (sma20 is not None and sma50 is not None and sma20 > sma50) \
                            or (roc is not None and roc > min_roc):
                        override_ok = True
                    elif (sma20 is None or sma50 is None) and roc is None:
                        override_ok = True   # keine Trend-Daten -> fail-open
                if not override_ok:
                    ev.hits.append(GateHit(
                        "dipbuy_regime",
                        f"Dip-Buy in Regime {regime.upper()} ohne Trend-Bestaetigung",
                        float(g.get("size_mult", 0.25)),
                    ))

    # ── 7. ma200_trend: close[D-1] < SMA200 → shadow-Block / live 0.25x ──
    # Frozen rule (advisor 2026-09-29). SHADOW mode: GateHit with size_mult
    # 0.0 is recorded in the ledger but MUST NOT touch execution — the
    # caller applies sizing via live_effective_mult(), which is 1.0 in
    # shadow. Fail-open on missing/short history (< window+lag closes).
    g = gates.get("ma200_trend", {})
    if g.get("enabled"):
        gmode = str(g.get("mode", "shadow")).lower()
        blocked_ma, detail = ma200_decision(
            daily_closes,
            window=int(g.get("window", 200)),
            lag=int(g.get("lag", 1)),
        )
        ev.ma200_detail = detail
        if blocked_ma:
            if gmode == "live":
                ev.ma200_live = True
                ev.hits.append(GateHit(
                    "ma200_trend",
                    f"close[{detail['close']:.4g}] < SMA200[{detail['sma200']:.4g}]"
                    f" ({detail['below_pct']:.1f}%)",
                    float(g.get("size_mult", 0.25)),
                ))
            else:
                # Shadow: ledger-only marker. size_mult 0.0 flags it as
                # "would-block" for the evaluation query, but shadow mode
                # never applies it to execution.
                ev.ma200_shadow = True
                ev.hits.append(GateHit(
                    "ma200_trend",
                    f"[SHADOW] close[{detail['close']:.4g}] < SMA200[{detail['sma200']:.4g}]"
                    f" ({detail['below_pct']:.1f}%)",
                    0.0,
                ))

    # Kombiniert = MIN ueber alle Hits, hart-geclampt (min_size_mult) —
    # aber ein Block (0.0) bleibt ein Block.
    if ev.hits:
        ev.size_mult = 0.0 if ev.blocked else max(
            min((h.size_mult for h in ev.hits), default=1.0), conf["min_size_mult"]
        )

    return ev


# ─── DB layer ────────────────────────────────────────────────────────────────

def ensure_table(db) -> None:
    """Idempotente Migration (AGENTS.md): CREATE TABLE IF NOT EXISTS.

    ``db``: bot.db.db.DB (has .execute) oder ein rohes sqlite3.Connection.
    Fail-open: wirft nicht.
    """
    try:
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                signal_type TEXT,
                regime TEXT,
                is_core_sweep INTEGER NOT NULL DEFAULT 0,
                hits TEXT NOT NULL DEFAULT '[]',
                size_mult REAL NOT NULL DEFAULT 1.0,
                blocked INTEGER NOT NULL DEFAULT 0,
                applied INTEGER NOT NULL DEFAULT 0,
                signal_id INTEGER,
                instrument_id INTEGER,
                entry_price REAL
            )
        """)
        # Idempotente Migration (AGENTS.md): ALTER TABLE je Spalte in
        # try/except — pre-existing tables (without entry_price) get it here.
        # feat/ma200-filter: entry_price = what a would-be-blocked trade
        # would have paid (shadow-ledger PnL-Bezug).
        try:
            db.execute(
                f"ALTER TABLE {TABLE_NAME} ADD COLUMN entry_price REAL"
            )
        except Exception:
            pass  # column already exists
    except Exception:
        logger.debug("entry_quality: ensure_table fehlgeschlagen (fail-open)", exc_info=True)


def record(
    db,
    ev: EntryQualityEval,
    *,
    mode: str,
    applied: bool = False,
    signal_id: int | None = None,
    instrument_id: int | None = None,
    is_core_sweep: bool | None = None,
    entry_price: float | None = None,
) -> int | None:
    """Insert one evaluation row. Returns row id (None on failure, fail-open).

    ``is_core_sweep``: explicit flag from the caller. When ``None`` it is
    derived from the signal_type (keeps the signal path's legacy behaviour).

    ``entry_price``: signal/entry price at evaluation time (ledger context
    for the shadow-Ma200 evaluation: what the would-be-blocked trade WOULD
    have paid). Optional; None when the caller has no price.

    ``size_mult`` column = the EXECUTION-effective multiplier
    (``ev.live_effective_mult``): a shadow ma200_trend hit (size_mult 0.0,
    ledger marker only) is excluded, so ``latest_size_mult()`` at execution
    never sees a would-block. The full per-gate truth (incl. the shadow hit)
    stays in the ``hits`` JSON; ``blocked=1`` doubles as the would-block
    marker for the shadow evaluation.
    """
    import json
    if is_core_sweep is None:
        is_core_sweep = ev.signal_type == "CORE_SWEEP"
    try:
        cur = db.execute(
            f"""
            INSERT INTO {TABLE_NAME}
                (mode, symbol, signal_type, regime, is_core_sweep,
                 hits, size_mult, blocked, applied, signal_id, instrument_id,
                 entry_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mode,
                ev.symbol,
                ev.signal_type,
                ev.regime,
                1 if is_core_sweep else 0,
                json.dumps([
                    {"gate": h.gate, "reason": h.reason, "size_mult": h.size_mult}
                    for h in ev.hits
                ], ensure_ascii=False),
                ev.live_effective_mult,
                1 if ev.blocked else 0,
                1 if applied else 0,
                signal_id,
                instrument_id,
                entry_price,
            ),
        )
        return int(cur.lastrowid) if cur.lastrowid is not None else None
    except Exception:
        logger.debug("entry_quality: record fehlgeschlagen (fail-open)", exc_info=True)
        return None


def apply_sizing(amount: float, size_mult: float, min_buy: float) -> tuple[float, bool]:
    """Gate-Multiplikator anwenden, ohne unter ``min_buy`` zu fallen.

    Ohne diesen Boden wird aus einem Soft-Gate ein stiller Hard-Block: der
    Betrag wird heruntergesetzt, faellt unter die Mindestordergroesse und
    der Trade wird komplett verworfen — die markierten Signale liefern dann
    NIE ein Ergebnis, womit der Live-WR-Abgleich fuer genau diese Gruppe
    blind bleibt. Gemessen am 2026-08-24: 42% der letzten 120 Trades fielen
    nach einem 0.5x-Gate unter die 50-USD-Grenze (min_buy_usd ist zudem
    regimeabhaengig: NORMAL 50, CAUTION 75, DEFENSIVE 100, CRITICAL 150).

    Der angehobene Betrag ist nie groesser als der ungegatete — das Gate
    kann eine Position also niemals vergroessern. War der Betrag schon vor
    dem Gate unter ``min_buy``, bleibt er unveraendert und wird wie bisher
    stromabwaerts aussortiert.

    Returns ``(betrag, wurde_angehoben)``.
    """
    if size_mult >= 1.0 or amount <= 0:
        return round(amount, 2), False
    scaled = round(amount * size_mult, 2)
    if scaled < min_buy <= amount:
        return round(min_buy, 2), True
    return scaled, False


def mark_applied(db, signal_id: int | None) -> None:
    """Markiert die juengste Evaluation eines Signals als tatsaechlich angewandt.

    Phase 2 (live): trennt in ``entry_quality_events`` die Zeilen, die die
    Execution wirklich veraendert haben (``applied=1``), von reinen
    Beobachtungen. Ohne diese Trennung waere die spaetere WR-Auswertung
    mehrdeutig, weil ``mode=live`` allein nichts darueber aussagt, ob ein
    Gate bei diesem Signal ueberhaupt gegriffen hat. Fail-open: wirft nicht.
    """
    if signal_id is None:
        return
    try:
        db.execute(
            f"UPDATE {TABLE_NAME} SET applied = 1 WHERE id = ("
            f"SELECT id FROM {TABLE_NAME} WHERE signal_id = ? ORDER BY id DESC LIMIT 1)",
            (signal_id,),
        )
    except Exception:
        logger.debug("entry_quality: mark_applied fehlgeschlagen (fail-open)", exc_info=True)


def latest_size_mult(db, signal_id: int | None) -> float:
    """Kombinierter Sizing-Multiplikator fuer ein Signal (1.0 = kein Gate).

    Nur fuer live-Mode-Application — liest die letzte Evaluation pro
    Signal-Id. Fail-open: 1.0 bei Fehlern oder fehlender Zeile.
    """
    if signal_id is None:
        return 1.0
    try:
        row = db.fetchone(
            f"SELECT size_mult FROM {TABLE_NAME} WHERE signal_id = ? ORDER BY id DESC LIMIT 1",
            (signal_id,),
        )
        if row is None:
            return 1.0
        return float(row["size_mult"] if isinstance(row, dict) else row[0])
    except Exception:
        return 1.0
