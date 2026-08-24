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
import sqlite3
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
    },
}

# Dip-Buy-Cluster fuer das macd_turn_required / dipbuy_trend Gate — die
# Signaltypen, die "guenstig" kaufen, bevor der Fall bestaetigt ist.
_DIPBUY_TYPES_DEFAULT = [
    "BB_LOWER_RSI_OVERSOLD",
    "BB_EXTREME_RSI_OVERSOLD",
    "RSI_EXTREME_OVERSOLD",
]


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

    @property
    def blocked(self) -> bool:
        return any(h.size_mult <= 0.0 for h in self.hits)

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
) -> EntryQualityEval:
    """Evaluate all enabled gates. Pure function, fail-open, no side effects.

    ``indicators``: the dict from ``signals.compute_indicators`` (rsi,
    macd_hist, bb_pct, atr, price, sma20, sma50, vol_ratio, roc_5d_pct,
    ...). Missing keys are tolerated.
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
                instrument_id INTEGER
            )
        """)
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
) -> int | None:
    """Insert one evaluation row. Returns row id (None on failure, fail-open).

    ``is_core_sweep``: explicit flag from the caller. When ``None`` it is
    derived from the signal_type (keeps the signal path's legacy behaviour).
    """
    import json
    if is_core_sweep is None:
        is_core_sweep = ev.signal_type == "CORE_SWEEP"
    try:
        cur = db.execute(
            f"""
            INSERT INTO {TABLE_NAME}
                (mode, symbol, signal_type, regime, is_core_sweep,
                 hits, size_mult, blocked, applied, signal_id, instrument_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ev.size_mult,
                1 if ev.blocked else 0,
                1 if applied else 0,
                signal_id,
                instrument_id,
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
