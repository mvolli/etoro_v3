#!/usr/bin/env python3
"""Trailing Stop Manager — Trading Bible V5.

Monitors open positions for profit-taking opportunities.
Runs inside Risk Worker after SL enforcement.

Note: eToro has no SL-update endpoint. Break-even enforcement
requires Close+Reopen (blocked in DEFENSIVE/CRITICAL).
Partial profit-taking uses units-based close (see EToroClient.close_position).
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── Profit-Taking Thresholds (Trading Bible V5) ──────────────────────────────
# fix/be-trigger-lowered: war 5.0. Bei SL=3% blieb eine Position bis +5%
# vollstaendig ungeschuetzt und konnte von +4.9% direkt auf -3% durchrutschen,
# bevor ueberhaupt ein Boden eingezogen wurde. 3.0 = Position muss sich weiter
# in unsere Richtung bewegt haben als das SL riskiert, bevor wir sie sichern.
BREAK_EVEN_TRIGGER_PCT = 3.0    # +3% PnL → move SL to entry (software tracking)
# fix/break-even-enforcement: Schwelle, unter die eine BE-armierte Position
# nicht zurückfallen darf. Leicht über 0, damit Spread/Fees den Close nicht
# in einen Mini-Verlust drehen. eToro hat kein SL-Update-Endpoint, daher
# Software-Enforcement: BE aktiv + PnL ≤ Floor → Full Close.
BREAK_EVEN_FLOOR_PCT = 0.3

# Fixed fallback ladder — used when an instrument has no ATR% on file yet
# (fresh instrument, data_worker hasn't run a cycle for it). Same values as
# the original Trading Bible V5 ladder.
PROFIT_TAKE_LEVELS = [
    {'threshold':  7.0, 'close_pct': 15},   # +7%  → 15% (1:1 R:R, füllt Gap zu BE)
    {'threshold': 15.0, 'close_pct': 15},   # +15% → weitere 15%
    {'threshold': 25.0, 'close_pct': 20},   # +25% → 20%
    {'threshold': 50.0, 'close_pct': 25},   # +50% → 25% (Runner-Schutz)
]

# fix/atr-adaptive-profit-levels: ein Blue-Chip (ATR ~1-1.5%) erreicht real
# selten ein flaches +15%; eine Crypto/High-Beta-Position (ATR ~4-6%) durch-
# schlaegt +50% oft als reines Intraday-Rauschen, ohne dass es einen echten
# Trend bedeutet. Die Level werden daher als ATR%-Vielfache skaliert statt
# fix — je Position EINMAL beim ersten Erreichen der Gewinnzone berechnet und
# in position_state eingefroren (siehe load_profit_levels/save_profit_levels),
# damit ein spaeter driftender ATR-Wert nie ein bereits genommenes Level neu
# triggert (Doppel-Verkaufs-Risiko).
ATR_PROFIT_LEVELS = [
    {'atr_mult': 6.0,  'close_pct': 20, 'min_pct': 6.0,  'max_pct': 30.0},
    {'atr_mult': 10.0, 'close_pct': 20, 'min_pct': 10.0, 'max_pct': 50.0},
    {'atr_mult': 18.0, 'close_pct': 30, 'min_pct': 18.0, 'max_pct': 90.0},
]

# fix/profit-ladder-reachability (2026-08-12): Skalierungsfaktor auf atr_mult
# und min_pct ALLER Stufen. 1.0 = unveraendert (konservativer Code-Default,
# die aktive Einstellung steht in config.yaml).
#
# WARUM: Die Leiter feuerte praktisch nie. Gemessen am realen Buch lag die
# erste Stufe (ATRx6, min 6%) im Median bei +17.1% — erreicht hatten sie 2 von
# 59 offenen Positionen (3%). Der durchschnittliche GEWINN-Trade der 220
# geschlossenen Trades liegt bei +3.49%, der Median-Peak der offenen bei 4.2%.
# Beendet wurden Gewinner damit ausschliesslich von Break-Even-Stop (+0.3%)
# und Momentum-Fade — zwischen +3.5% und +17% existierte kein Mechanismus,
# der einen Gewinner strukturiert am Leben haelt oder Teilgewinn sichert.
# Das ist die Ursache der Asymmetrie (Ø Gewinn +3.49% vs Ø Verlust -1.74%
# bei 23.6% Trefferquote gegen 33.3% Breakeven-Bedarf).
#
# max_pct wird BEWUSST nicht mitskaliert: das ist eine Sicherheits-Obergrenze,
# sie tiefer zu ziehen wuerde High-ATR-Titel zu frueh zwangsschliessen.
#
# Der LLM-Review-Worker darf den Faktor in BIBLE_HARD_LIMITS-Grenzen
# (0.2 .. 1.0) selbst nachjustieren — die Leiter kalibriert sich damit an der
# tatsaechlichen Bewegung des Buchs, statt eine geratene Konstante zu sein.
PROFIT_LADDER_ATR_SCALE = 1.0

# fix/min-partial-close (2026-08-12): Untergrenze fuer Teilverkaeufe in USD.
# eToro weist Orders unter minPositionExposure ab; ein 20%-Leiter-Anteil an
# einem $9-Restfragment sind ~$2. Siehe Begruendung in execute_trailing_actions.
MIN_PARTIAL_CLOSE_USD = 10.0


# ── Dynamic Quick-Profit (Stufe 1) ────────────────────────────────────────────
# Zwei Mechaniken, beide auf der LIVE-PnL jedes risk_worker-Zyklus (~5 min) —
# kein Intraday-Feed noetig, greift daher sofort auch fuer Bestandspositionen.
#
# ① MOMENTUM-FADE (universell): trackt das PnL-Hoch je Position (peak_pnl_pct).
#    Baut eine Position Gewinn auf und gibt ihn wieder ab, wird EINMALIG ein
#    Teil realisiert + Break-Even auf dem Rest armiert. Fuellt genau die Luecke
#    zwischen BE-Floor (+0.3%) und der ersten Profit-Stufe (+6%), die heute
#    voellig ungeschuetzt ist.
# ② SCALP-TIER (opt-in per strategy='scalp'): eine sehr fruehe erste Profit-
#    Stufe (ATR×2, clamp [2%,5%]), damit ein bewusst kurzfristiger Trade schnell
#    einen Teilgewinn sichert, statt auf die Swing-Leiter (+6/+10/+18%) zu warten.
MOMENTUM_FADE_ENABLED = True
# feat/full-exit (2026-08-24): Vollausstieg am Ziel statt Teilverkauf-Kaskade.
# Gemessen: offene Positionen erreichen im Median 9.9 % Peak-PnL, realisiert
# werden aber nur 0.27 % (Median der Gewinner) — vier unabhaengige
# Teilverkaufsmechanismen (Ladder, Fade, Scalp, Stale) nehmen je ~25 % und
# lassen im Median 11 % der Einstiegsgroesse uebrig. Die Gewinner werden
# zerlegt, waehrend sie noch steigen.
# Methodenpassung: unsere Gewinnersignale (MACD_TURN_BELOW_SMA20,
# BB_LOW_MACD_IMPROVING) sind Mean-Reversion, keine Trendfolge. Hinter dem
# Umkehrpunkt gibt es keine Kante mehr — dort schlaegt ein fester
# Vollausstieg das Aufteilen. Bei Trendfolge waere es andersherum.
# ATR-skaliert, weil adaptive Ausstiege in Vergleichstests deutlich besser
# abschneiden als feste Prozentmarken.
# feat/min-remaining (2026-08-24): Untergrenze der Restposition.
# close_pct wirkt auf die AKTUELLE Groesse, nicht auf den Einstieg — die Kette
# lautet 100 -> 75 -> 56 -> 42 %. Ohne Untergrenze zerfaellt eine Position so
# in Splitter (gemessen: Median 11 % Rest). Wuerde ein Teilverkauf die Marke
# unterschreiten, wird stattdessen GANZ geschlossen: die Leiter bleibt
# erhalten, aber sie kann eine Position nicht mehr zerfasern.
MIN_REMAINING_PCT = 50.0

FULL_EXIT_ENABLED = False       # 2026-08-24: aus — die Untergrenze oben
                                # uebernimmt den Vollausstieg, ohne die
                                # Profit-Leiter auszuhebeln. true setzt ein
                                # zusaetzliches ATR-Ziel davor.
FULL_EXIT_ATR_MULT = 2.0        # Ziel = atr_mult x ATR%
FULL_EXIT_MIN_PCT = 4.0         # Untergrenze des Ziels
FULL_EXIT_MAX_PCT = 10.0        # Obergrenze des Ziels

MOMENTUM_ARM_PCT = 2.0          # Peak muss dieses PnL erreichen, bevor Fade-Schutz armiert
MOMENTUM_RETRACE_FRAC = 0.40    # Rueckgabe dieses Anteils vom Peak → feuert
MOMENTUM_MIN_LOCK_PCT = 1.0     # unter diesem aktuellen PnL nie feuern (BE/SL-Revier)
MOMENTUM_FADE_CLOSE_PCT = 50.0  # % der Position, das ein Fade realisiert
MOMENTUM_MAX_RETRACE_ABS = 999.0 # Absoluter Cap: max. pp die vor Fade abgegeben werden dürfen
                                  # (999 = deaktiviert; bei Peaks > cap/retrace_frac schlägt an)

SCALP_ENABLED = True
SCALP_ATR_MULT = 2.0
SCALP_MIN_PCT = 2.0
SCALP_MAX_PCT = 5.0
SCALP_CLOSE_PCT = 25


# ── Stale-Exit (fix/stale-exit 2026-07-15) ───────────────────────────────────
# Totes Kapital: Position lief nie (Peak < Fade-Arm-Schwelle), haengt seit
# min_days seitwaerts im PnL-Band und blockiert Slot + Cash. Die Parameter
# garantieren Disjunktheit zu ALLEN bestehenden Exit-Mechaniken:
#   min_peak < MOMENTUM_ARM_PCT (2.0)  => nie im Momentum-Fade-Revier
#   |PnL| < Band (1.5) < BE-Trigger    => nie im BE/Ladder-Revier, kein SL-Fall
STALE_EXIT_ENABLED = False        # Rollout-Schalter: Dry-Log bis config true
STALE_MIN_DAYS = 10               # Kalendertage aus openDateTime (~7 Handelstage)
STALE_PNL_BAND_PCT = 1.5          # |PnL| unter diesem Band = seitwaerts
STALE_MIN_PEAK_PCT = 2.0          # Peak < Arm-Schwelle: Position lief nie
STALE_LLM_HOLD_GRACE_H = 24.0     # frische HOLD-Empfehlung schont ...
STALE_MAX_DAYS = 20               # ... aber harter Deckel: dann Exit trotz HOLD


def is_stale_candidate(pnl_pct: float, peak_pnl_pct: float,
                       days_held: int | None) -> bool:
    """Pure decision: ist die Position totes Kapital? (fix/stale-exit)

    days_held=None (fehlendes/kaputtes openDateTime) → False: ein
    Datenfehler darf NIE einen Exit ausloesen (fail-safe)."""
    if days_held is None or days_held < STALE_MIN_DAYS:
        return False
    if abs(pnl_pct) >= STALE_PNL_BAND_PCT:
        return False
    if peak_pnl_pct >= STALE_MIN_PEAK_PCT:
        return False
    return True


def _load_fresh_llm_holds(grace_h: float) -> set:
    """Symbole mit frischer HOLD-Empfehlung (< grace_h Stunden) aus
    llm_position_recommendations.json (position_review_worker, 4x/Tag).
    Fail-open: Fehler → leere Menge (die Regel entscheidet allein)."""
    try:
        from datetime import datetime, timezone
        from pathlib import Path
        path = (Path(__file__).resolve().parent.parent.parent.parent
                / 'data' / 'llm_position_recommendations.json')
        recs = json.loads(path.read_text())
        if not isinstance(recs, list):
            return set()
        now = datetime.now(timezone.utc)
        holds = set()
        for r in recs:
            if not isinstance(r, dict) or str(r.get('recommendation', '')).upper() != 'HOLD':
                continue
            try:
                ts = datetime.fromisoformat(str(r.get('ts', '')))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (now - ts).total_seconds() <= grace_h * 3600:
                    holds.add(str(r.get('symbol', '')))
            except Exception:
                continue
        return holds
    except Exception:
        return set()


def _append_stale_outcome(db: Any, action: 'TrailingAction') -> None:
    """Lernschleifen-Eintrag fuer den 72h-Rueckblick (llm_review bewertet
    GOOD/NEUTRAL/MISSED_UPSIDE). yf_symbol wird mitgespeichert, damit der
    spaetere Preisvergleich REIN yfinance-basiert ist (einheitsfest — die
    eToro-Rate-vs-GBX-Falle bei .L-Titeln wird so vermieden)."""
    try:
        from datetime import datetime, timezone
        from pathlib import Path
        yf_symbol = None
        try:
            if db is not None:
                row = db.fetchone(
                    "SELECT yfinance_symbol FROM instruments WHERE instrument_id=?",
                    (action.instrument_id,),
                )
                if row:
                    yf_symbol = row["yfinance_symbol"]
        except Exception:
            pass
        path = (Path(__file__).resolve().parent.parent.parent.parent
                / 'data' / 'stale_exit_outcomes.json')
        data = {'entries': []}
        if path.exists():
            data = json.loads(path.read_text()) or {'entries': []}
        data.setdefault('entries', []).append({
            'ts': datetime.now(timezone.utc).isoformat()[:19],
            'symbol': action.symbol,
            'yf_symbol': yf_symbol,
            'position_id': action.position_id,
            'instrument_id': action.instrument_id,
            'exit_pnl_pct': round(action.pnl_pct, 2),
            'outcome': None,
        })
        data['entries'] = data['entries'][-200:]
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        tmp.replace(path)
    except Exception as exc:
        logger.debug('[trailing] stale-outcome write failed: %s', exc)


def apply_config(cfg: dict) -> None:
    """Wire the `trailing:` config block into module thresholds (idempotent).

    Called once per risk_worker run before evaluate_trailing(). Missing keys
    keep the conservative code defaults above.
    """
    global MOMENTUM_FADE_ENABLED, MOMENTUM_ARM_PCT, MOMENTUM_RETRACE_FRAC
    global MOMENTUM_MIN_LOCK_PCT, MOMENTUM_FADE_CLOSE_PCT, MOMENTUM_MAX_RETRACE_ABS
    global SCALP_ENABLED, SCALP_ATR_MULT, SCALP_MIN_PCT, SCALP_MAX_PCT, SCALP_CLOSE_PCT
    global STALE_EXIT_ENABLED, STALE_MIN_DAYS, STALE_PNL_BAND_PCT
    global STALE_MIN_PEAK_PCT, STALE_LLM_HOLD_GRACE_H, STALE_MAX_DAYS
    global PROFIT_LADDER_ATR_SCALE, MIN_PARTIAL_CLOSE_USD
    global FULL_EXIT_ENABLED, FULL_EXIT_ATR_MULT, FULL_EXIT_MIN_PCT, FULL_EXIT_MAX_PCT
    global MIN_REMAINING_PCT
    t = ((cfg or {}).get('trailing') or {})
    try:
        MIN_PARTIAL_CLOSE_USD = float(t.get('min_partial_close_usd', MIN_PARTIAL_CLOSE_USD))
    except (TypeError, ValueError):
        pass
    pl = (t.get('profit_ladder') or {})
    try:
        _scale = float(pl.get('atr_scale', PROFIT_LADDER_ATR_SCALE))
        # Harte Klemme: 0 oder negativ wuerde die Leiter auf 0% setzen und
        # jede Position sofort teilschliessen.
        PROFIT_LADDER_ATR_SCALE = max(0.05, min(1.0, _scale))
    except (TypeError, ValueError):
        pass
    se = (t.get('stale_exit') or {})
    if 'enabled' in se:
        STALE_EXIT_ENABLED = bool(se['enabled'])
    STALE_MIN_DAYS = int(se.get('min_days', STALE_MIN_DAYS))
    STALE_PNL_BAND_PCT = float(se.get('pnl_band_pct', STALE_PNL_BAND_PCT))
    STALE_MIN_PEAK_PCT = float(se.get('min_peak_pct', STALE_MIN_PEAK_PCT))
    STALE_LLM_HOLD_GRACE_H = float(se.get('llm_hold_grace_h', STALE_LLM_HOLD_GRACE_H))
    STALE_MAX_DAYS = int(se.get('max_days', STALE_MAX_DAYS))
    mf = (t.get('momentum_fade') or {})
    if 'enabled' in mf:
        MOMENTUM_FADE_ENABLED = bool(mf['enabled'])
    MOMENTUM_ARM_PCT = float(mf.get('arm_pct', MOMENTUM_ARM_PCT))
    MOMENTUM_RETRACE_FRAC = float(mf.get('retrace_frac', MOMENTUM_RETRACE_FRAC))
    MOMENTUM_MIN_LOCK_PCT = float(mf.get('min_lock_pct', MOMENTUM_MIN_LOCK_PCT))
    MOMENTUM_FADE_CLOSE_PCT = float(mf.get('close_pct', MOMENTUM_FADE_CLOSE_PCT))
    MOMENTUM_MAX_RETRACE_ABS = float(mf.get('max_retrace_abs_pct', MOMENTUM_MAX_RETRACE_ABS))
    MIN_REMAINING_PCT = float(t.get('min_remaining_pct', MIN_REMAINING_PCT))
    fe = (t.get('full_exit') or {})
    if 'enabled' in fe:
        FULL_EXIT_ENABLED = bool(fe['enabled'])
    FULL_EXIT_ATR_MULT = float(fe.get('atr_mult', FULL_EXIT_ATR_MULT))
    FULL_EXIT_MIN_PCT = float(fe.get('min_pct', FULL_EXIT_MIN_PCT))
    FULL_EXIT_MAX_PCT = float(fe.get('max_pct', FULL_EXIT_MAX_PCT))
    sc = (t.get('scalp') or {})
    if 'enabled' in sc:
        SCALP_ENABLED = bool(sc['enabled'])
    SCALP_ATR_MULT = float(sc.get('atr_mult', SCALP_ATR_MULT))
    SCALP_MIN_PCT = float(sc.get('min_pct', SCALP_MIN_PCT))
    SCALP_MAX_PCT = float(sc.get('max_pct', SCALP_MAX_PCT))
    SCALP_CLOSE_PCT = float(sc.get('close_pct', SCALP_CLOSE_PCT))


def full_exit_threshold(atr_pct: float | None) -> float:
    """ATR-skaliertes Vollausstiegsziel, geklemmt auf [min_pct, max_pct]."""
    base = (atr_pct if atr_pct and atr_pct > 0 else FULL_EXIT_MIN_PCT) * FULL_EXIT_ATR_MULT
    return round(min(max(base, FULL_EXIT_MIN_PCT), FULL_EXIT_MAX_PCT), 2)


def should_full_exit(pnl_pct: float, atr_pct: float | None) -> bool:
    """Pure decision: Ziel erreicht -> Position GANZ schliessen.

    Hat Vorrang vor Ladder und Fade. Rein und seiteneffektfrei, damit die
    Schwelle ohne DB und ohne eToro-API testbar bleibt.
    """
    if not FULL_EXIT_ENABLED:
        return False
    return pnl_pct >= full_exit_threshold(atr_pct)


def would_breach_min_remaining(remaining_frac: float, close_pct: float) -> bool:
    """Wuerde dieser Teilverkauf die Restposition unter die Marke druecken?

    ``remaining_frac``: Anteil der Position, der noch offen ist (1.0 = ganz).
    ``close_pct``: Anteil DER AKTUELLEN Position, den der Teilverkauf nimmt.

    True heisst: statt des Teilverkaufs ganz schliessen. Auch dann True, wenn
    die Marke bereits unterschritten ist — eine schon zerfaserte Position soll
    nicht noch weiter zersplittert werden. Rein und seiteneffektfrei.
    """
    floor = MIN_REMAINING_PCT / 100.0
    if floor <= 0.0:
        return False
    if remaining_frac <= floor:
        return True
    return remaining_frac * (1.0 - close_pct / 100.0) < floor


def should_momentum_fade(pnl_pct: float, peak_pnl_pct: float, already_faded: bool) -> bool:
    """Pure decision: has a built-up gain faded enough to lock a partial?

    True iff momentum-fade is enabled, the position isn't already faded, the
    peak reached the arm threshold, current PnL is still a real gain (≥ min-lock)
    AND has given back at least RETRACE_FRAC of the peak. Kept pure/side-effect-
    free so the trigger can be unit-tested without a DB or the eToro API.
    """
    if not MOMENTUM_FADE_ENABLED or already_faded:
        return False
    if peak_pnl_pct < MOMENTUM_ARM_PCT:
        return False
    if pnl_pct < MOMENTUM_MIN_LOCK_PCT:
        return False
    floor_rel = peak_pnl_pct * (1.0 - MOMENTUM_RETRACE_FRAC)
    floor_abs = peak_pnl_pct - MOMENTUM_MAX_RETRACE_ABS  # absoluter Cap
    floor = max(floor_rel, floor_abs)  # strengerer (höherer) Floor gewinnt
    return pnl_pct <= floor


def _scalp_rung(atr_pct: float | None) -> dict:
    """First, early profit rung for a scalp-tagged position (ATR-scaled)."""
    base = (atr_pct if atr_pct and atr_pct > 0 else SCALP_MIN_PCT) * SCALP_ATR_MULT
    threshold = min(max(base, SCALP_MIN_PCT), SCALP_MAX_PCT)
    return {'threshold': round(threshold, 2), 'close_pct': SCALP_CLOSE_PCT}


def _resolve_profit_levels(atr_pct: float | None, strategy: str = 'swing') -> list[dict]:
    """Return the profit-take ladder to use for a position.

    ATR-scaled when *atr_pct* is known (> 0), else the fixed fallback ladder.
    A scalp-tagged position gets an extra early first rung (deduped/sorted so a
    scalp rung never sits at or above the first swing rung).
    """
    if not atr_pct or atr_pct <= 0:
        base = list(PROFIT_TAKE_LEVELS)
    else:
        base = []
        scale = PROFIT_LADDER_ATR_SCALE if PROFIT_LADDER_ATR_SCALE > 0 else 1.0
        for lv in ATR_PROFIT_LEVELS:
            # scale wirkt auf atr_mult UND min_pct — sonst klemmt min_pct die
            # Skalierung weg (bei ATR<2% war min_pct=6.0 die bindende Grenze).
            threshold = min(
                max(lv['atr_mult'] * scale * atr_pct, lv['min_pct'] * scale),
                lv['max_pct'],
            )
            base.append({'threshold': round(threshold, 2), 'close_pct': lv['close_pct']})
    if strategy == 'scalp' and SCALP_ENABLED:
        scalp = _scalp_rung(atr_pct)
        # Only prepend if it's genuinely earlier than the first swing rung.
        if not base or scalp['threshold'] < base[0]['threshold']:
            base = [scalp] + base
    return base

@dataclass
class TrailingAction:
    action: str          # 'BREAK_EVEN' | 'PARTIAL_CLOSE' | 'OK'
    symbol: str
    position_id: str
    pnl_pct: float
    reason: str
    close_pct: float = 0.0     # for PARTIAL_CLOSE — target % of position to close
    instrument_id: int = 0     # needed for close_position() body
    amount_usd: float = 0.0    # position size in USD — used to derive units
    open_rate: float = 0.0     # entry price — used to derive units
    level_threshold: float = 0.0  # which PROFIT_TAKE_LEVEL fired (for persistence)


# ── Position-State Persistenz ─────────────────────────────────────────────────
# fix/partial-close-level-tracking: evaluate_trailing() hatte KEIN Gedächtnis,
# welche Profit-Level bereits realisiert wurden. Eine Position bei +16% PnL
# feuerte bei JEDEM risk_worker-Lauf (5min) erneut "PARTIAL_CLOSE 20%" — der
# PnL-Prozentsatz des Rests bleibt ja ~gleich — und wurde so in 20%-Schritten
# zwangsliquidiert statt einmalig 20% zu realisieren (Bible: +15% → EINMAL 20%).
# position_state persistiert die genommenen Level pro position_id.

def _ensure_position_state_table(db: Any) -> None:
    """Create the position_state table if it doesn't exist (lazy, idempotent)."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS position_state (
            position_id     TEXT PRIMARY KEY,
            symbol          TEXT,
            levels_taken    TEXT NOT NULL DEFAULT '',
            be_active       INTEGER NOT NULL DEFAULT 0,
            be_triggered_at TEXT,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Migration for existing installs (idempotent). Each ALTER is isolated so
    # one already-existing column never blocks the others.
    for ddl in (
        "ALTER TABLE position_state ADD COLUMN profit_levels_json TEXT",
        "ALTER TABLE position_state ADD COLUMN peak_pnl_pct REAL NOT NULL DEFAULT 0",
        "ALTER TABLE position_state ADD COLUMN momentum_faded INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE position_state ADD COLUMN strategy TEXT NOT NULL DEFAULT 'swing'",
        # fix/sell-exit-cooldown: Zeitstempel des letzten SELL-Exits pro
        # Position — verhindert das Endlos-Zerlegen einer Position, wenn
        # data_worker die Überhitzungs-Bedingung jeden Zyklus neu signalisiert
        # (KTA.DE-Vorfall 2026-07-06: 39 Signale, Position in 50%-Schritten
        # von ~$500 auf $14.75 zerlegt).
        "ALTER TABLE position_state ADD COLUMN sell_exit_at TEXT",
        "ALTER TABLE position_state ADD COLUMN remaining_frac REAL NOT NULL DEFAULT 1.0",
    ):
        try:
            db.execute(ddl)
        except Exception:
            pass  # column already exists


def load_atr_pct(db: Any, instrument_ids: list[int]) -> dict[int, float]:
    """Return {instrument_id: atr_pct} for the given ids (data_worker-populated)."""
    ids = [i for i in instrument_ids if i]
    if db is None or not ids:
        return {}
    try:
        placeholders = ",".join("?" * len(ids))
        rows = db.fetchall(
            f"SELECT instrument_id, atr_pct FROM instruments "
            f"WHERE instrument_id IN ({placeholders}) AND atr_pct IS NOT NULL",
            ids,
        )
        return {int(row[0]): float(row[1]) for row in rows}
    except Exception as exc:
        logger.warning("[trailing] load_atr_pct failed: %s", exc)
        return {}


def load_symbols(db: Any, instrument_ids: list[int]) -> dict[int, str]:
    """Return {instrument_id: symbol} — eToro-Namespace aus der instruments-Tabelle.

    fix/trailing-symbol-resolution (2026-08-12): Die eToro-Positions-Payload
    enthaelt KEINEN 'symbol'-Key, nur 'instrumentID'. evaluate_trailing fiel
    deshalb ausnahmslos auf `str(instrumentID)` zurueck — 56 von 60
    position_state-Zeilen trugen '3364' statt '9633.HK'.

    Folgen, beide still:
      1. _action_market_open() reicht dieses Pseudo-Symbol an is_market_open()
         weiter, das die Boerse aus dem ERSTEN Argument ableitet. Ohne
         erkennbares Suffix greift fail_open=True -> "Markt offen". Der Guard
         aus fix/stale-price-trailing war damit fuer JEDE Nicht-US-Position
         wirkungslos (48 von 60), obwohl er genau dafuer gebaut wurde.
      2. Der Stale-Exit-Vergleich `symbol in fresh_holds` traf nie, weil
         _load_fresh_llm_holds() echte Ticker liefert — die LLM-HOLD-
         Schonfrist war ebenfalls tot.

    Entspricht der AGENTS.md-Invariante: alles Richtung eToro-API loest
    `instruments.symbol` per instrument_id auf (vgl.
    execution_worker._canonical_symbol).
    """
    ids = [i for i in instrument_ids if i]
    if db is None or not ids:
        return {}
    try:
        placeholders = ",".join("?" * len(ids))
        rows = db.fetchall(
            f"SELECT instrument_id, symbol FROM instruments "
            f"WHERE instrument_id IN ({placeholders}) AND symbol IS NOT NULL",
            ids,
        )
        return {int(row[0]): str(row[1]) for row in rows}
    except Exception as exc:
        logger.warning("[trailing] load_symbols failed: %s", exc)
        return {}


def load_profit_levels(db: Any, position_ids: list[str]) -> dict[str, list[dict]]:
    """Return the frozen profit-take ladder already snapshot for each position."""
    if db is None or not position_ids:
        return {}
    try:
        _ensure_position_state_table(db)
        placeholders = ",".join("?" * len(position_ids))
        rows = db.fetchall(
            f"SELECT position_id, profit_levels_json FROM position_state "
            f"WHERE position_id IN ({placeholders})",
            list(position_ids),
        )
        result: dict[str, list[dict]] = {}
        for pid, levels_json in rows:
            if levels_json:
                try:
                    result[pid] = json.loads(levels_json)
                except Exception:
                    continue
        return result
    except Exception as exc:
        logger.warning("[trailing] load_profit_levels failed: %s", exc)
        return {}


def save_profit_levels(db: Any, position_id: str, symbol: str, levels: list[dict]) -> None:
    """Freeze *levels* for *position_id* — first write wins (never overwrite)."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        levels_json = json.dumps(levels)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, profit_levels_json, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                profit_levels_json = COALESCE(position_state.profit_levels_json, excluded.profit_levels_json),
                updated_at = excluded.updated_at
        """, (position_id, symbol, levels_json))
    except Exception as exc:
        logger.warning("[trailing] save_profit_levels(%s) failed: %s", position_id, exc)


def load_levels_taken(db: Any, position_ids: list[str]) -> dict[str, set[float]]:
    """Return {position_id: {threshold, ...}} for the given positions."""
    if db is None or not position_ids:
        return {}
    try:
        _ensure_position_state_table(db)
        placeholders = ",".join("?" * len(position_ids))
        rows = db.fetchall(
            f"SELECT position_id, levels_taken FROM position_state "
            f"WHERE position_id IN ({placeholders})",
            list(position_ids),
        )
        result: dict[str, set[float]] = {}
        for row in rows:
            pid, levels_csv = row[0], row[1] or ""
            result[pid] = {float(x) for x in levels_csv.split(",") if x.strip()}
        return result
    except Exception as exc:
        logger.warning("[trailing] load_levels_taken failed: %s", exc)
        return {}


def mark_level_taken(db: Any, position_id: str, symbol: str, threshold: float) -> None:
    """Persist that *threshold* has been realized for *position_id*."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        existing = load_levels_taken(db, [position_id]).get(position_id, set())
        existing.add(threshold)
        levels_csv = ",".join(f"{t:g}" for t in sorted(existing))
        db.execute("""
            INSERT INTO position_state (position_id, symbol, levels_taken, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                levels_taken = excluded.levels_taken,
                updated_at = excluded.updated_at
        """, (position_id, symbol, levels_csv))
    except Exception as exc:
        logger.warning("[trailing] mark_level_taken(%s, %.0f) failed: %s",
                       position_id, threshold, exc)


def load_be_active(db: Any, position_ids: list[str]) -> set[str]:
    """Return the subset of *position_ids* whose break-even is armed."""
    if db is None or not position_ids:
        return set()
    try:
        _ensure_position_state_table(db)
        placeholders = ",".join("?" * len(position_ids))
        rows = db.fetchall(
            f"SELECT position_id FROM position_state "
            f"WHERE be_active = 1 AND position_id IN ({placeholders})",
            list(position_ids),
        )
        return {row[0] for row in rows}
    except Exception as exc:
        logger.warning("[trailing] load_be_active failed: %s", exc)
        return set()


def mark_break_even_active(db: Any, position_id: str, symbol: str) -> None:
    """Arm break-even for *position_id* (idempotent)."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, be_active, be_triggered_at, updated_at)
            VALUES (?, ?, 1, datetime('now'), datetime('now'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                be_active = 1,
                be_triggered_at = COALESCE(position_state.be_triggered_at, excluded.be_triggered_at),
                updated_at = excluded.updated_at
        """, (position_id, symbol))
    except Exception as exc:
        logger.warning("[trailing] mark_break_even_active(%s) failed: %s", position_id, exc)


def load_position_dynamic(db: Any, position_ids: list[str]) -> dict[str, dict]:
    """Return {position_id: {'peak': float, 'faded': bool, 'strategy': str}}.

    Powers momentum-fade (peak/faded) and the scalp ladder (strategy). Positions
    with no state row default to peak 0 / not-faded / 'swing' at the call site.
    """
    if db is None or not position_ids:
        return {}
    try:
        _ensure_position_state_table(db)
        placeholders = ",".join("?" * len(position_ids))
        rows = db.fetchall(
            f"SELECT position_id, peak_pnl_pct, momentum_faded, strategy, "
            f"COALESCE(remaining_frac, 1.0) "
            f"FROM position_state WHERE position_id IN ({placeholders})",
            list(position_ids),
        )
        result: dict[str, dict] = {}
        for pid, peak, faded, strat, remaining in rows:
            result[pid] = {
                'peak': float(peak or 0.0),
                'faded': bool(faded),
                'strategy': (strat or 'swing'),
                'remaining': float(remaining if remaining is not None else 1.0),
            }
        return result
    except Exception as exc:
        logger.warning("[trailing] load_position_dynamic failed: %s", exc)
        return {}


def update_peak_pnl(db: Any, position_id: str, symbol: str, pnl_pct: float) -> None:
    """Raise the stored PnL high-water-mark for *position_id* (never lowers it)."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, peak_pnl_pct, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                peak_pnl_pct = MAX(position_state.peak_pnl_pct, excluded.peak_pnl_pct),
                updated_at = excluded.updated_at
        """, (position_id, symbol, float(pnl_pct)))
    except Exception as exc:
        logger.warning("[trailing] update_peak_pnl(%s) failed: %s", position_id, exc)


def _as_full_exit(a: 'TrailingAction', remaining_frac: float) -> 'TrailingAction':
    """Wandelt einen Teilverkauf in einen Vollausstieg um (Untergrenze erreicht)."""
    return TrailingAction(
        action='FULL_EXIT',
        symbol=a.symbol,
        position_id=a.position_id,
        pnl_pct=a.pnl_pct,
        reason=(
            f"{a.reason} | Rest {remaining_frac * 100:.0f}% — ein weiterer "
            f"Teilverkauf ({a.close_pct:.0f}%) wuerde unter {MIN_REMAINING_PCT:.0f}% "
            f"fallen, daher Full Close"
        ),
        instrument_id=a.instrument_id,
        amount_usd=a.amount_usd,
        open_rate=a.open_rate,
    )


def apply_partial_to_remaining(db: Any, position_id: str, symbol: str,
                               close_pct: float) -> None:
    """Schreibt den verbleibenden Anteil nach einem Teilverkauf fort.

    ``close_pct`` wirkt auf die AKTUELLE Groesse, die Fortschreibung ist also
    multiplikativ. Bewusst in SQL gerechnet statt im Aufrufer: der
    Ausfuehrungs-Loop kennt den vorherigen Stand nicht, und so kann er auch
    nicht veralten. Fail-open.
    """
    keep = max(0.0, min(1.0, 1.0 - (close_pct or 0.0) / 100.0))
    try:
        db.execute(
            """
            INSERT INTO position_state (position_id, symbol, remaining_frac, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(position_id) DO UPDATE SET
                remaining_frac = COALESCE(position_state.remaining_frac, 1.0) * ?,
                updated_at = datetime('now')
            """,
            (position_id, symbol, keep, keep),
        )
    except Exception as exc:
        logger.warning("[trailing] apply_partial_to_remaining(%s) failed: %s",
                       position_id, exc)


def mark_momentum_faded(db: Any, position_id: str, symbol: str) -> None:
    """Persist that momentum-fade has fired once for *position_id* (one-shot)."""
    if db is None or not position_id:
        return
    try:
        _ensure_position_state_table(db)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, momentum_faded, updated_at)
            VALUES (?, ?, 1, datetime('now'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                momentum_faded = 1,
                updated_at = excluded.updated_at
        """, (position_id, symbol))
    except Exception as exc:
        logger.warning("[trailing] mark_momentum_faded(%s) failed: %s", position_id, exc)


def set_strategy(db: Any, position_id: str, symbol: str, strategy: str) -> None:
    """Tag *position_id* as 'scalp' or 'swing' (drives the early scalp rung).

    Resets the frozen profit_levels_json so the ladder re-resolves under the new
    strategy on the next cycle — otherwise a position frozen as 'swing' would
    never gain the early scalp rung when retro-tagged. Levels already TAKEN stay
    recorded, so re-resolving cannot re-fire a rung that was already realized.
    """
    if db is None or not position_id or strategy not in ('scalp', 'swing'):
        return
    try:
        _ensure_position_state_table(db)
        db.execute("""
            INSERT INTO position_state (position_id, symbol, strategy, profit_levels_json, updated_at)
            VALUES (?, ?, ?, NULL, datetime('now'))
            ON CONFLICT(position_id) DO UPDATE SET
                symbol = excluded.symbol,
                strategy = excluded.strategy,
                profit_levels_json = NULL,
                updated_at = excluded.updated_at
        """, (position_id, symbol, strategy))
    except Exception as exc:
        logger.warning("[trailing] set_strategy(%s,%s) failed: %s", position_id, strategy, exc)


def cleanup_position_state(db: Any, live_position_ids: set[str]) -> int:
    """Remove state rows for positions that no longer exist. Returns count."""
    if db is None:
        return 0
    try:
        _ensure_position_state_table(db)
        rows = db.fetchall("SELECT position_id FROM position_state")
        stale = [r[0] for r in rows if r[0] not in live_position_ids]
        for pid in stale:
            db.execute("DELETE FROM position_state WHERE position_id = ?", (pid,))
        return len(stale)
    except Exception as exc:
        logger.warning("[trailing] cleanup_position_state failed: %s", exc)
        return 0


def evaluate_trailing(
    positions: list[dict],
    regime: str = 'NORMAL',
    db: Any = None,
) -> list[TrailingAction]:
    """Evaluate all positions for trailing stop opportunities.

    Args:
        positions: Raw positions from eToro API (clientPortfolio.positions)
        regime: Current trading regime
        db: DB handle (bot.db.connection.DB) for level-taken persistence.
            None → stateless fallback (every level fires; only for tests).
    Returns:
        List of TrailingActions to execute
    """
    pos_ids = [str(p.get('positionID', '')) for p in positions if p.get('positionID')]
    levels_taken = load_levels_taken(db, pos_ids)
    be_armed = load_be_active(db, pos_ids)
    frozen_levels = load_profit_levels(db, pos_ids)
    dynamic = load_position_dynamic(db, pos_ids)
    instrument_ids = [
        int(p.get('instrumentID') or p.get('instrumentId') or 0) for p in positions
    ]
    atr_by_instrument = load_atr_pct(db, instrument_ids)
    # fix/trailing-symbol-resolution (2026-08-12): echte Symbole aufloesen —
    # die eToro-Payload hat keinen 'symbol'-Key, siehe load_symbols().
    symbol_by_instrument = load_symbols(db, instrument_ids)

    # Stale-Exit: frische LLM-HOLD-Empfehlungen einmal pro Lauf laden (Grace)
    fresh_holds = _load_fresh_llm_holds(STALE_LLM_HOLD_GRACE_H)

    actions = []
    for pos in positions:
        pos_id = str(pos.get('positionID', ''))
        instrument_id = int(pos.get('instrumentID') or pos.get('instrumentId') or 0)
        # Reihenfolge: Payload (falls eToro es je liefert) -> instruments-
        # Tabelle -> ID als letzter Notnagel. Der Notnagel macht den
        # Market-Guard blind, ist aber besser als ein leeres Symbol.
        symbol = (pos.get('symbol')
                  or symbol_by_instrument.get(instrument_id)
                  or str(pos.get('instrumentID', '')))
        amount = float(pos.get('amount', 0))
        open_rate = float(pos.get('openRate', 0) or 0)
        upnl = pos.get('unrealizedPnL') or {}
        pnl_usd = float(upnl.get('pnL', 0)) if isinstance(upnl, dict) else 0.0

        if amount <= 0:
            continue
        pnl_pct = (pnl_usd / amount) * 100

        # ── Momentum-Fade state: peak high-water-mark je Position ─────────────
        meta = dynamic.get(pos_id, {})
        prev_peak = float(meta.get('peak', 0.0))
        faded = bool(meta.get('faded', False))
        strategy = meta.get('strategy', 'swing')
        remaining_frac = float(meta.get('remaining', 1.0))
        peak = max(prev_peak, pnl_pct)
        if peak > prev_peak:
            update_peak_pnl(db, pos_id, symbol, pnl_pct)  # SQL raises the mark

        atr_pct = atr_by_instrument.get(instrument_id)
        # Scalp-Positionen duerfen ihren fruehen ersten Rung unterhalb des
        # normalen BE-Trigger-Gates (3%) nehmen — sonst waere ein Scalp-Rung
        # < 3% praktisch tot. Swing bleibt exakt beim bisherigen 3%-Gate.
        is_scalp = strategy == 'scalp' and SCALP_ENABLED
        effective_gate = BREAK_EVEN_TRIGGER_PCT
        if is_scalp:
            effective_gate = min(effective_gate, _scalp_rung(atr_pct)['threshold'])

        def _fade_action() -> TrailingAction:
            return TrailingAction(
                action='MOMENTUM_FADE',
                symbol=symbol,
                position_id=pos_id,
                pnl_pct=pnl_pct,
                reason=(
                    f"Momentum-Fade: Peak +{peak:.1f}% → jetzt {pnl_pct:+.1f}% "
                    f"({peak - pnl_pct:.1f}pp={((peak - pnl_pct) / peak * 100) if peak > 0 else 0:.0f}% abgegeben)"
                    f" — {MOMENTUM_FADE_CLOSE_PCT:.0f}% sichern + BE"
                ),
                close_pct=MOMENTUM_FADE_CLOSE_PCT,
                instrument_id=instrument_id,
                amount_usd=amount,
                open_rate=open_rate,
            )

        if pnl_pct < effective_gate:
            # fix/break-even-enforcement: eine BE-armierte Position (war
            # schon ≥ BREAK_EVEN_TRIGGER_PCT) darf nicht zurück unter Entry
            # fallen — Full Close am Floor, statt bis zum Hard-SL durchzurutschen.
            if pos_id in be_armed and pnl_pct <= BREAK_EVEN_FLOOR_PCT:
                actions.append(TrailingAction(
                    action='BE_CLOSE',
                    symbol=symbol,
                    position_id=pos_id,
                    pnl_pct=pnl_pct,
                    reason=(
                        f"Break-Even-Enforcement: war ≥ +{BREAK_EVEN_TRIGGER_PCT:.0f}%, "
                        f"jetzt {pnl_pct:+.1f}% ≤ +{BREAK_EVEN_FLOOR_PCT:.1f}% Floor — Full Close"
                    ),
                    instrument_id=instrument_id,
                    amount_usd=amount,
                    open_rate=open_rate,
                ))
            # Quick-profit lock in the +min_lock..+BE_trigger gap that the
            # ladder never reaches — the whole point of momentum-fade.
            elif should_momentum_fade(pnl_pct, peak, faded):
                _fa = _fade_action()
                actions.append(
                    _as_full_exit(_fa, remaining_frac)
                    if would_breach_min_remaining(remaining_frac, _fa.close_pct or 0.0)
                    else _fa
                )
            else:
                # ── Stale-Exit (fix/stale-exit 2026-07-15): totes Kapital.
                # days_held aus openDateTime (Broker-Wahrheit); kaputter
                # Timestamp → kein Exit (fail-safe in is_stale_candidate).
                _stale_days = None
                try:
                    from bot.core.position_meta import days_held_from
                    _stale_days = days_held_from(pos.get('openDateTime'))
                except Exception:
                    _stale_days = None
                if is_stale_candidate(pnl_pct, peak, _stale_days):
                    if symbol in fresh_holds and (_stale_days or 0) < STALE_MAX_DAYS:
                        logger.info(
                            '[trailing] STALE-Kandidat %s (%sd, %+.1f%%) — '
                            'LLM-HOLD-Schonfrist (%.0fh), Deckel %dd',
                            symbol, _stale_days, pnl_pct,
                            STALE_LLM_HOLD_GRACE_H, STALE_MAX_DAYS,
                        )
                    elif not STALE_EXIT_ENABLED:
                        logger.info(
                            '[trailing] STALE-Kandidat %s: %sd seitwaerts '
                            '(Peak +%.1f%%, PnL %+.1f%%) — Dry-Log '
                            '(stale_exit.enabled=false)',
                            symbol, _stale_days, peak, pnl_pct,
                        )
                    else:
                        actions.append(TrailingAction(
                            action='STALE_EXIT',
                            symbol=symbol,
                            position_id=pos_id,
                            pnl_pct=pnl_pct,
                            reason=(
                                f'Stale-Exit: {_stale_days}d seitwaerts '
                                f'(Peak +{peak:.1f}%, PnL {pnl_pct:+.1f}%) '
                                f'— Kapital freigesetzt'
                            ),
                            instrument_id=instrument_id,
                            amount_usd=amount,
                            open_rate=open_rate,
                        ))
            continue  # No structured profit-taking below the BE trigger

        taken = levels_taken.get(pos_id, set())

        # ATR-adaptive Ladder: einmal beim ersten Erreichen der Gewinnzone
        # bestimmt und in position_state eingefroren (siehe save_profit_levels),
        # damit ein spaeter aktualisierter ATR-Wert das Level fuer diese
        # Position NICHT mehr verschiebt — sonst koennte ein bereits als
        # "genommen" markiertes Level bei naechster Berechnung einen leicht
        # anderen threshold ergeben und erneut feuern (Doppel-Verkauf).
        profit_levels = frozen_levels.get(pos_id)
        if profit_levels is None:
            profit_levels = _resolve_profit_levels(atr_pct, strategy)
            save_profit_levels(db, pos_id, symbol, profit_levels)

        # Fälliges, noch NICHT genommenes Level suchen — das NIEDRIGSTE zuerst
        # (Bible-Reihenfolge: springt der PnL direkt auf das oberste Level,
        # nimmt dieser Zyklus das unterste, der naechste 5-min-Zyklus das naechste).
        pending = [
            lv for lv in sorted(profit_levels, key=lambda x: x['threshold'])
            if pnl_pct >= lv['threshold'] and lv['threshold'] not in taken
        ]
        if should_full_exit(pnl_pct, atr_pct):
            # feat/full-exit: Ziel erreicht — GANZ raus, vor Ladder und Fade.
            actions.append(TrailingAction(
                action='FULL_EXIT',
                symbol=symbol,
                position_id=pos_id,
                pnl_pct=pnl_pct,
                reason=(
                    f"+{pnl_pct:.1f}% >= +{full_exit_threshold(atr_pct):.1f}% "
                    f"Vollausstiegsziel (ATR-skaliert) — Full Close"
                ),
                instrument_id=instrument_id,
                amount_usd=amount,
                open_rate=open_rate,
            ))
        elif pending:
            # Structured ladder profit-taking takes priority over the fade.
            level = pending[0]
            _pa = TrailingAction(
                action='PARTIAL_CLOSE',
                symbol=symbol,
                position_id=pos_id,
                pnl_pct=pnl_pct,
                reason=f"+{pnl_pct:.1f}% ≥ +{level['threshold']:.0f}% profit target",
                close_pct=level['close_pct'],
                instrument_id=instrument_id,
                amount_usd=amount,
                open_rate=open_rate,
                level_threshold=level['threshold'],
            )
            # feat/min-remaining: Die Leiter bleibt erhalten — sie darf die
            # Position nur nicht unter die Untergrenze zerfasern.
            actions.append(
                _as_full_exit(_pa, remaining_frac)
                if would_breach_min_remaining(remaining_frac, _pa.close_pct or 0.0)
                else _pa
            )
        elif should_momentum_fade(pnl_pct, peak, faded):
            # No ladder level due, but a built-up gain is fading back → lock it.
            _fa = _fade_action()
            actions.append(
                _as_full_exit(_fa, remaining_frac)
                if would_breach_min_remaining(remaining_frac, _fa.close_pct or 0.0)
                else _fa
            )
        elif pnl_pct >= BREAK_EVEN_TRIGGER_PCT:
            # Only break-even (BE-trigger..first-rung range, or all due levels taken)
            actions.append(TrailingAction(
                action='BREAK_EVEN',
                symbol=symbol,
                position_id=pos_id,
                pnl_pct=pnl_pct,
                reason=f"+{pnl_pct:.1f}% ≥ +{BREAK_EVEN_TRIGGER_PCT:.0f}% — break-even tracked",
                instrument_id=instrument_id,
            ))
        # else: scalp position in [scalp_gate, BE_trigger) with its rung already
        # taken and no fade due → nothing to do this cycle.
    return actions


# Modul-Cache für discord_embeds — vorher wurde das ~1700-Zeilen-Modul bei
# JEDEM Close per importlib neu von der Platte geladen und ausgeführt.
# False = Laden bereits fehlgeschlagen (nicht erneut versuchen).
_DISCORD_EMBEDS_CACHE: Any = None


def _get_discord_embeds() -> Any:
    """Load discord_embeds once per process. Returns module or None."""
    global _DISCORD_EMBEDS_CACHE
    if _DISCORD_EMBEDS_CACHE is not None:
        return _DISCORD_EMBEDS_CACHE or None
    try:
        from pathlib import Path as _Path
        import importlib.util
        _embed_file = str(_Path(__file__).resolve().parent.parent / 'discord_embeds.py')
        spec = importlib.util.spec_from_file_location('discord_embeds', _embed_file)
        de = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(de)
        _DISCORD_EMBEDS_CACHE = de
        return de
    except Exception:
        _DISCORD_EMBEDS_CACHE = False
        return None


def _post_closed_embed(symbol: str, position_id: str, reason: str,
                       pnl_pct: float | None = None, amount_usd: float = 0.0,
                       close_pct: float = 100.0, *,
                       client: Any = None, db: Any = None,
                       instrument_id: int | None = None,
                       units: float | None = None,
                       source: str = 'trailing_partial') -> None:
    """Best-effort Discord embed for a (partial) close. Never raises.

    fix/embed-real-amounts (KTA.DE 2026-07-06): amount_usd war hartkodiert 0 —
    jedes Close-Embed zeigte '$0.00 Betrag' und erweckte den Eindruck, die
    Position existiere nicht. Jetzt: tatsächlich geschlossener Anteil.

    feat/pnl-nachreport (2026-07-28): der abgeleitete Dollar-PnL
    (amount × close_pct × pnl_pct) war nach frueheren Partials bis 4x falsch
    (trades.amount_usd wird nie dekrementiert, gemessen COA.L 485.83 vs.
    116.60 live). Das Embed postet daher pnl_usd=None ("P/L folgt") + die
    verlaessliche Live-Prozentzahl; Reconciler Step 9e traegt das echte
    netProfit aus der API-History nach und EDITIERT dieses Embed (deshalb
    wird die Message-ID in trade_events gespeichert).

    feat/trade-event-marker: mit client+instrument_id bekommt das Embed
    einen Trade-Story-Chart mit Entry/Exit-Markern.
    """
    try:
        closed_amount = float(amount_usd) * float(close_pct) / 100.0
        de = _get_discord_embeds()
        if de is None or not hasattr(de, 'post_position_closed_embed'):
            return

        # Entry-Kontext aus der trades-Tabelle (fuer Chart + Event-Record)
        trade_id = None
        entry_price = None
        opened_at = None
        if db is not None:
            try:
                row = db.fetchone(
                    "SELECT id, entry_price, confirmed_at, created_at "
                    "FROM trades WHERE api_position_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (str(position_id),),
                )
                if row:
                    trade_id = row["id"]
                    entry_price = row["entry_price"]
                    opened_at = row["confirmed_at"] or row["created_at"]
            except Exception:
                pass

        # Chart mit Ein-/Ausstiegs-Markern (best effort)
        chart_ok = False
        if client is not None and instrument_id and hasattr(de, 'attach_chart'):
            try:
                from bot.core.candle_chart import trade_story_png_v2
                events = []
                if entry_price:
                    events.append({"ts": opened_at, "type": "ENTRY",
                                   "price": float(entry_price),
                                   "label": f"Entry {float(entry_price):g}"})
                # Exit-Preis-Schaetzung fuer den Marker: Entry × (1 + pnl%) —
                # der exakte closeRate kommt spaeter per History-Nachtrag.
                exit_est = None
                if entry_price and pnl_pct is not None:
                    exit_est = float(entry_price) * (1.0 + float(pnl_pct) / 100.0)
                if exit_est:
                    ev_type = ("PARTIAL_CLOSE"
                               if (close_pct and close_pct < 99.5) else "EXIT")
                    label = (f"-{close_pct:.0f}% @{exit_est:g}"
                             if ev_type == "PARTIAL_CLOSE"
                             else f"Exit {exit_est:g} ({pnl_pct:+.1f}%)")
                    events.append({"ts": None, "type": ev_type,
                                   "price": exit_est, "label": label})
                png = trade_story_png_v2(client, instrument_id, symbol,
                                         events, opened_at=opened_at)
                if png:
                    de.attach_chart(png)
                    chart_ok = True
            except Exception:
                pass

        post_ok = de.post_position_closed_embed(
            symbol=symbol,
            amount_usd=closed_amount,
            position_id=position_id,
            pnl_usd=None,          # Dollar-Ableitung unzuverlaessig → Nachreport
            pnl_pct=pnl_pct,
            reason=reason,
            close_pct=close_pct,
        )

        if db is not None:
            try:
                from bot.core.event_log import record_posted_event
                record_posted_event(
                    db, de, symbol=symbol,
                    event_type=("PARTIAL_CLOSE"
                                if (close_pct and close_pct < 99.5) else "CLOSE"),
                    source=source, post_result=post_ok,
                    trade_id=trade_id, position_id=str(position_id),
                    instrument_id=instrument_id,
                    close_pct=float(close_pct) if close_pct else None,
                    units=units, amount_usd=closed_amount,
                    pnl_usd=None, pnl_pct=pnl_pct, pnl_source='derived',
                    reason=reason, chart_posted=chart_ok,
                    reported_final=False,
                )
            except Exception:
                pass
    except Exception:
        pass


def _find_position(client: Any, instrument_id: int, position_id: str) -> dict | None:
    """Look up a position by instrument_id (+ position_id if present) in the
    live eToro portfolio. Used to verify a partial-close actually took
    effect, since eToro's close-order response only confirms the order was
    ACCEPTED (statusID=1), not that it has been applied yet — verified via
    a live test on 2026-07-01: a partial-close response arrived instantly
    with statusID=1, but the portfolio amount only reflected the reduction
    after ~9s of polling.
    """
    try:
        portfolio = client.get_portfolio()
    except Exception:
        return None
    positions = (
        portfolio.get("clientPortfolio", {}).get("positions")
        or portfolio.get("positions")
        or []
    )
    for pos in positions:
        pid = str(pos.get("positionID") or pos.get("positionId") or "")
        iid = pos.get("instrumentID") or pos.get("instrumentId")
        if position_id and pid == str(position_id):
            return pos
        if not position_id and iid is not None and int(iid) == int(instrument_id):
            return pos
    return None


def _verify_partial_close(
    client: Any,
    action: "TrailingAction",
    max_attempts: int = 6,
    initial_wait_s: float = 3.0,
) -> tuple[bool, str]:
    """Poll the live portfolio with exponential backoff until the position's
    amount actually reflects the expected reduction, instead of trusting
    close_position()'s immediate 200/statusID=1 response.

    Mirrors the ghost-order verification pattern already used in
    execution_worker.py (open-side) — this is the same check for the
    close/partial-close side, which previously had none.

    Returns (verified, detail).
    """
    import time as _time

    expected_amount = action.amount_usd * (1 - action.close_pct / 100.0)
    tolerance_pct = 5.0  # allow rounding/spread drift, matches manual test tolerance
    waited = 0.0

    for attempt in range(max_attempts):
        wait_s = min(initial_wait_s * (2 ** attempt), 30)
        _time.sleep(wait_s)
        waited += wait_s

        pos = _find_position(client, action.instrument_id, action.position_id)

        if pos is None:
            # Position fully gone — could mean the WHOLE position closed
            # instead of just close_pct% of it. That is a worse outcome
            # than "nothing happened", not a success — never count it.
            return False, (
                f"{action.symbol}: position vanished entirely after partial-close "
                f"(expected ~${expected_amount:.2f} remaining, position not found "
                f"after {waited:.0f}s) — possible FULL close instead of partial"
            )

        actual_amount = float(pos.get("amount", 0))
        if abs(actual_amount - action.amount_usd) < 0.01:
            continue  # amount hasn't moved yet — keep polling

        diff_pct = abs(actual_amount - expected_amount) / max(expected_amount, 0.01) * 100
        if diff_pct < tolerance_pct:
            return True, (
                f"{action.symbol}: partial-close CONFIRMED after {waited:.0f}s — "
                f"${action.amount_usd:.2f} → ${actual_amount:.2f} "
                f"(expected ${expected_amount:.2f}, diff {diff_pct:.1f}%)"
            )
        # Amount changed but not to the expected value — record and keep
        # polling in case it's still settling, but don't return success yet.
        logger.debug(
            "[trailing] %s: amount changed to $%.2f (expected $%.2f) after %.0fs, "
            "still polling", action.symbol, actual_amount, expected_amount, waited,
        )

    # Exhausted all attempts without a confirmed match
    final_pos = _find_position(client, action.instrument_id, action.position_id)
    final_amount = float(final_pos.get("amount", 0)) if final_pos else 0.0
    return False, (
        f"{action.symbol}: partial-close NOT CONFIRMED after {waited:.0f}s "
        f"— amount is ${final_amount:.2f}, expected ~${expected_amount:.2f} "
        f"(started at ${action.amount_usd:.2f})"
    )


def verify_full_close(
    client: Any,
    instrument_id: int,
    position_id: str,
    max_attempts: int = 8,
    initial_wait_s: float = 3.0,
) -> tuple[bool, str, dict | None]:
    """Poll until a position after a full-close has actually disappeared,
    instead of trusting the immediate 200 response (which only means the
    order was accepted). For SL-close (risk_worker) and concentration-close.

    Returns (confirmed, detail, pnl_data) where pnl_data is a dict with
    exit_price, pnl_usd, pnl_pct if available, else None.
    
    fix/sl-close-embed: increased max_attempts from 6 to 8 for ~165s total
    (was 105s) to handle HK/ASIA markets with slower API response times.
    """
    import time as _time

    waited = 0.0
    final_pnl_data = None
    
    for attempt in range(max_attempts):
        wait_s = min(initial_wait_s * (2 ** attempt), 30)
        _time.sleep(wait_s)
        waited += wait_s
        pos = _find_position(client, instrument_id, position_id)
        if pos is None:
            # Position gone — close confirmed. Try to get final PnL from the
            # last known state (caller should pass it). Return None here;
            # risk_worker fills it from the pre-close snapshot.
            return True, f"Full-close CONFIRMED after {waited:.0f}s", final_pnl_data
    return False, f"Full-close NOT confirmed after {waited:.0f}s — position may still be open", None


def _action_market_open(db: Any, action: "TrailingAction") -> bool:
    """Market-Hours-Check fuer Trailing-/Exit-Aktionen (fix/stale-price-trailing).

    yfinance_symbol/asset_class werden aus der instruments-Tabelle aufgeloest,
    damit z.B. Forex-Symbole (EURJPY) nicht faelschlich an US-Boersenzeiten
    gebunden werden. Fail-open: bei jedem Fehler gilt der Markt als offen
    (bisheriges Verhalten bleibt dann erhalten)."""
    try:
        from bot.core.market_hours import is_market_open, resolve_market_fields
        _mf = resolve_market_fields(db, action.instrument_id)
        yf_sym, cat = (_mf[1], _mf[2]) if _mf else ("", "")
        # action.symbol gewinnt bewusst ueber _mf[0]: load_symbols() hat es
        # bereits aufgeloest, und eine Payload, die doch ein Symbol mitbringt,
        # soll Vorrang behalten (test_payload_symbol_hat_vorrang).
        return is_market_open(action.symbol, yf_sym, cat)
    except Exception:
        return True


def execute_trailing_actions(
    client: Any,
    actions: list[TrailingAction],
    regime: str = 'NORMAL',
    dry_run: bool = False,
    db: Any = None,
) -> dict:
    """Execute trailing stop actions.

    PARTIAL_CLOSE: Closes a percentage of the position via API. The fired
    level is persisted via mark_level_taken() as soon as eToro ACCEPTS the
    order (not only after verification) — if the close settles slowly, the
    next 5-min cycle must NOT fire the same level again (double-sell risk
    outweighs the risk of losing one level to a never-executed order).
    BREAK_EVEN: arms persistent break-even state (position was ≥ BREAK_EVEN_TRIGGER_PCT).
    BE_CLOSE: full close because an armed position fell back to entry —
    executes in ALL regimes (loss protection, not profit-taking).
    """
    import time
    stats = {'partial_closes': 0, 'break_evens': 0, 'be_closes': 0,
             'momentum_fades': 0, 'stale_exits': 0, 'full_exits': 0, 'errors': []}

    for action in actions:
        # fix/stale-price-trailing (2026-07-14, HLAG.DE 21:49): Trigger
        # rechnen mit Live-PnL — nach Boersenschluss ist der stale (Xetra
        # schliesst 17:30, die Aktion kam 21:49). Die Order queued bei eToro
        # und fuellt blind ins Open-Gap (oder verpufft — Discord meldete
        # trotzdem CLOSED). Bei geschlossenem Markt: ueberspringen, der
        # naechste Zyklus nach Open entscheidet mit frischen Preisen.
        # BREAK_EVEN (nur State-Aenderung, keine Order) laeuft immer.
        if action.action != 'BREAK_EVEN' and not _action_market_open(db, action):
            logger.info('[trailing] %s: Markt geschlossen — %s uebersprungen (stale Preise)',
                        action.symbol, action.action)
            continue

        if action.action == 'BREAK_EVEN':
            # fix/break-even-enforcement: persist armed state — the next
            # cycles enforce the entry floor via BE_CLOSE (eToro has no
            # SL-update endpoint, so this is software enforcement).
            mark_break_even_active(db, action.position_id, action.symbol)
            logger.debug('[trailing] BREAK-EVEN armed: %s %+.1f%% — floor at entry (+%.1f%%)', action.symbol, action.pnl_pct, BREAK_EVEN_FLOOR_PCT)
            stats['break_evens'] += 1
            continue

        if action.action in ('BE_CLOSE', 'STALE_EXIT', 'FULL_EXIT'):
            # BE_CLOSE: Loss protection — runs in ALL regimes.
            # STALE_EXIT (fix/stale-exit): Kapital-Freisetzung, ebenfalls in
            # allen Regimes (De-Risking, kein Profit-Taking).
            logger.info('[trailing] %s: %s %+.1f%% — %s', action.action,
                        action.symbol, action.pnl_pct, action.reason)
            if dry_run:
                stats[{'BE_CLOSE': 'be_closes',
                       'FULL_EXIT': 'full_exits'}.get(action.action, 'stale_exits')] += 1
                continue
            try:
                result = client.close_position(
                    position_id=action.position_id,
                    instrument_id=action.instrument_id,
                )
                if result:
                    verified, detail, _pnl_data = verify_full_close(
                        client, action.instrument_id, action.position_id
                    )
                    if verified:
                        logger.info('[trailing] %s verified: %s', action.action, detail)
                        if action.action == 'STALE_EXIT':
                            stats['stale_exits'] += 1
                            # Lernschleife: Eintrag fuer 72h-Rueckblick
                            _append_stale_outcome(db, action)
                            _embed_reason = f'💤 {action.reason}'
                        elif action.action == 'FULL_EXIT':
                            stats['full_exits'] += 1
                            _embed_reason = f'🎯 Vollausstieg: {action.reason}'
                        else:
                            stats['be_closes'] += 1
                            _embed_reason = f'Break-Even-Schutz: {action.reason}'
                        _post_closed_embed(
                            action.symbol, action.position_id,
                            _embed_reason,
                            pnl_pct=action.pnl_pct,
                            amount_usd=action.amount_usd,   # Full Close
                            client=client, db=db,
                            instrument_id=action.instrument_id,
                            source=('trailing_stale'
                                    if action.action == 'STALE_EXIT'
                                    else 'trailing_be'),
                        )
                    else:
                        logger.warning('[trailing] %s unverified: %s', action.action, detail)
                        stats['errors'].append(f'{action.symbol}: {action.action} unverified — {detail}')
                else:
                    stats['errors'].append(
                        f'{action.symbol}: {action.action} close_position() returned empty/falsy result'
                    )
                time.sleep(0.5)
            except Exception as e:
                msg = f'{action.symbol}: {action.action} API call failed — {e}'
                logger.error('[trailing] %s', msg)
                stats['errors'].append(msg)
            continue

        if action.action in ('PARTIAL_CLOSE', 'MOMENTUM_FADE'):
            is_fade = action.action == 'MOMENTUM_FADE'
            # Structured ladder-taking (PARTIAL_CLOSE) is suppressed in stressed
            # regimes ("let winners run"). MOMENTUM_FADE is protective de-risking
            # — locking a gain that is actively fading — so it runs in ALL
            # regimes, like BE_CLOSE and SELL-exits.
            if not is_fade and regime in ('DEFENSIVE', 'CRITICAL'):
                logger.debug('[trailing] PARTIAL_CLOSE skipped in %s: %s %+.1f%%', regime, action.symbol, action.pnl_pct)
                continue

            # ── Convert target % into absolute units (eToro API expects
            #    UnitsToDeduct as a unit count, not a percentage) ──────────
            if action.open_rate <= 0:
                msg = (
                    f'{action.symbol}: cannot compute partial-close units '
                    f'(missing open_rate={action.open_rate}) — skipped, no order sent'
                )
                logger.warning('[trailing] %s', msg)
                stats['errors'].append(msg)
                continue

            # fix/partial-close-units: echte units aus dem Live-Portfolio —
            # amount_usd/open_rate ignoriert openConversionRate (~14% Fehler
            # bei EUR-Titeln). Fallback: alte Formel (schliesst tendenziell
            # etwas MEHR — de-risking-Richtung, nie weniger Schutz).
            _live_units = None
            try:
                _live_units = client.get_position_units(action.position_id)
            except Exception:
                pass
            total_units = _live_units or (action.amount_usd / action.open_rate)
            units_to_deduct = round(total_units * (action.close_pct / 100.0), 8)

            if units_to_deduct <= 0:
                msg = f'{action.symbol}: computed units_to_deduct <= 0 — skipped'
                logger.warning('[trailing] %s', msg)
                stats['errors'].append(msg)
                continue

            # fix/min-partial-close (2026-08-12): Mini-Teilverkaeufe abfangen.
            # Rest-Fragmente von wenigen Dollar erzeugen bei 20% Leiter-Anteil
            # Orders von ~$2 — die weist eToro unter minPositionExposure ab.
            # Kritisch dabei: mark_level_taken() laeuft erst, wenn eToro die
            # Order ANNIMMT. Eine abgelehnte Mini-Order wuerde das Level also
            # nie als genommen verbuchen und im 5-min-Takt ENDLOS neu feuern
            # (verletzt die Invariante "Einmal-Aktionen pro Position immer
            # persistieren"). Deshalb: ueberspringen UND als genommen buchen —
            # fuer ein $9-Restfragment ist die Stufe schlicht keine Order wert.
            _close_value = action.amount_usd * (action.close_pct / 100.0)
            if 0 < _close_value < MIN_PARTIAL_CLOSE_USD:
                logger.info(
                    '[trailing] %s: Teilverkauf $%.2f < Minimum $%.2f — '
                    'Stufe uebersprungen und als genommen gebucht (kein Endlos-Retry)',
                    action.symbol, _close_value, MIN_PARTIAL_CLOSE_USD,
                )
                if not dry_run:
                    if is_fade:
                        mark_momentum_faded(db, action.position_id, action.symbol)
                    elif action.level_threshold > 0:
                        mark_level_taken(db, action.position_id, action.symbol,
                                         action.level_threshold)
                continue

            logger.info('[trailing] %s %s%%: %s %+.1f%% — %s (units=%.6f)', action.action, action.close_pct, action.symbol, action.pnl_pct, action.reason, units_to_deduct)

            if dry_run:
                stats['partial_closes'] += 1
                continue

            try:
                result = client.close_position(
                    position_id=action.position_id,
                    instrument_id=action.instrument_id,
                    units_to_deduct=units_to_deduct,
                )
                if result:
                    # State SOFORT persistieren (Order wurde von eToro
                    # akzeptiert) — verhindert Endlos-Feuer im nächsten
                    # 5-min-Zyklus, selbst wenn die Verifikation unten wegen
                    # Settlement-Latenz fehlschlägt.
                    if is_fade:
                        # One-shot: nie erneut faden; Rest per BE absichern.
                        mark_momentum_faded(db, action.position_id, action.symbol)
                        mark_break_even_active(db, action.position_id, action.symbol)
                    elif action.level_threshold > 0:
                        mark_level_taken(db, action.position_id, action.symbol,
                                         action.level_threshold)
                    # ── Verify the partial-close actually took effect ──────
                    # close_position() returning 200 only means the order was
                    # ACCEPTED (statusID=1), not applied — confirmed via live
                    # test 2026-07-01 (amount only updated after ~9s poll).
                    # Don't count it as a success until we've seen it reflected
                    # in the actual portfolio.
                    verified, detail = _verify_partial_close(client, action)
                    if verified:
                        logger.info('[trailing] %s', detail)
                        # feat/min-remaining: Restanteil fortschreiben.
                        apply_partial_to_remaining(
                            db, action.position_id, action.symbol,
                            action.close_pct or 0.0,
                        )
                        stats['partial_closes'] += 1
                        if is_fade:
                            stats['momentum_fades'] += 1
                    else:
                        logger.warning('[trailing] %s', detail)
                        stats['errors'].append(detail)
                    # Post Discord embed
                    _post_closed_embed(
                        action.symbol, action.position_id,
                        f'Profit-Taking: {action.reason}'
                        + ('' if verified else ' [UNVERIFIED — siehe Log]'),
                        pnl_pct=action.pnl_pct,
                        amount_usd=action.amount_usd,
                        close_pct=action.close_pct,
                        client=client, db=db,
                        instrument_id=action.instrument_id,
                        units=units_to_deduct,
                        source=('momentum_fade' if is_fade
                                else 'trailing_partial'),
                    )
                else:
                    stats['errors'].append(
                        f'{action.symbol}: close_position() returned empty/falsy result'
                    )
                time.sleep(0.5)
            except Exception as e:
                msg = f'{action.symbol}: partial-close API call failed — {e}'
                logger.error('[trailing] %s', msg)
                stats['errors'].append(msg)

    return stats
