"""eToro Trading Bot V3 — Position Sizing Helpers

Risk-neutral Kelly factor for dynamic position sizing based on realized
per-signal-type performance.

The Kelly Criterion (f* = win_rate - (1-win_rate)/(avg_win/avg_loss))
determines the theoretically optimal fraction of capital to allocate. We
use a dampened, risk-neutral scale with SIGNED Kelly (negative edge pulls
the factor down to the floor):

    factor = clamp(base + scale * f*, min_factor, max_factor)

with f* clamped to [-1, 1].

RISK-NEUTRAL SCALE (post-commit-review 2026-08-21-b, branch
fix/kelly-risk-neutral):

The previous scale ``clamp(1.0 + 2.0 * half_kelly, 0.5, 1.5)`` was
aggressive by design: every combo got >= 0.5 (never shrunk below ~half
size) and proven edges got up to 1.5x. Against the live trade log it
produced a trade-weighted mean factor of ~0.74 vs the constant 0.30 the
account was tested/tuned at — i.e. ~2.5x the tested risk per trade, with
negative-edge combos (WR 10%, negative expectancy) floored at 0.5 and
positive-edge combos on only n=10 trades boosted to 1.35.

This revision makes sizing risk-neutral:
  * the scale is centered at 1.0 (f* = 0 -> no change vs the base),
  * the base multiplier is calibrated so the current trade mix reproduces
    the tested risk level (trade-weighted mean ~0.30; see DEFAULT_BASE),
  * the floor is lowered (DEFAULT_MIN_FACTOR=0.15) so combos with
    NEGATIVE expectancy shrink instead of sitting at half size,
  * min_trades raised to 25 (was 10) so 10-trade samples no longer earn
    boosts.

The learning loop itself is untouched: Kelly is still computed from
realized per-signal stats and still updates the factor as more trades
close. As positive-edge signals accumulate, their factor still climbs;
the calibrated base simply keeps the overall risk budget neutral while
the loop reallocates between signal types.

Config overrides (top-level ``sizing`` block in config.yaml, mirrored in
bot.config.SizingConfig): kelly_min_trades / kelly_base / kelly_scale /
kelly_min_factor / kelly_max_factor.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULT_MIN_TRADES = 25
# CALIBRATION IS POINT-IN-TIME: base was fitted (2026-08-22) so the then-
# current 90d trade mix reproduces the tested risk level (weighted mean
# ~0.30). If the live signal mix shifts materially (e.g. large share of
# high-Kelly combos), the weighted mean drifts and per-trade risk moves
# again. Re-fit DEFAULT_BASE when the realized trade-weighted mean
# deviates >~25% from 0.30 — or on a quarterly cadence.
DEFAULT_BASE = 0.49
DEFAULT_SCALE = 0.45
DEFAULT_MIN_FACTOR = 0.15
# Natuerliche Obergrenze: kelly ist auf [-1, 1] geklemmt, also ist der
# hoechste rohe Faktor base + scale*1.0 = 0.94. max_factor ist die harte
# Cap darueber; der Default entspricht exakt dieser natuerlichen Grenze,
# d.h. der Cap bindet nur, wenn base/scale per Config nach oben getunet
# werden. (0.49+0.45 = 0.94, NICHT 0.49*1.5 — das war ein alter Kommentar.)
DEFAULT_MAX_FACTOR = 0.94


# feat/kelly-shrinkage (2026-08-24): Schrumpfungskonstante. alpha = k0/(n+k0).
DEFAULT_SHRINK_K0 = 50.0


def _get_sizing_cfg() -> dict:
    """Read Kelly params from config.yaml (top-level ``sizing`` block).

    Falls back to the dataclass defaults in bot.config when the key is
    missing, so the function is safe in any environment.
    """
    try:
        from bot.config import load_config
        s = load_config().sizing
        return {
            "kelly_min_trades": s.kelly_min_trades,
            "kelly_base": s.kelly_base,
            "kelly_scale": s.kelly_scale,
            "kelly_min_factor": s.kelly_min_factor,
            "kelly_max_factor": s.kelly_max_factor,
            "kelly_shrink_k0": getattr(s, "kelly_shrink_k0", DEFAULT_SHRINK_K0),
        }
    except Exception:
        return {}


def _kelly_fraction(pnls: list[float]) -> float:
    """Kelly fraction from a list of realized PnL percentages.

    May be NEGATIVE for negative-expectancy samples — that is the whole
    point of the risk-neutral scale: the formula ``base + scale * kelly``
    must see the sign so loss-bringers get pulled down to the floor.
    All-win degeneracy caps at +1.0; all-lose degeneracy at -1.0.
    """
    wins = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p <= 0]

    if not wins and not losses:
        return 0.0
    if not losses:
        return 1.0   # all winners → cap the positive side
    if not wins:
        return -1.0  # all losers → full negative edge

    win_rate = len(wins) / len(pnls)
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)

    if avg_loss == 0:
        return 1.0

    f = win_rate - (1.0 - win_rate) / (avg_win / avg_loss)
    return max(-1.0, min(1.0, f))


def _recent_trade_rows(db, bucket: str | None = None) -> list[tuple[str, float]]:
    """(signal_type, pnl_pct) for all CLOSED trades in the lookback window.

    Same query as before the risk-neutral change (fix/kelly-components,
    2026-07-26): exact combo strings live in signals.signal_type and are
    joined via trades.signal_id.
    """
    sql = """
            SELECT s.signal_type AS st, t.pnl_pct
            FROM trades t
            JOIN signals s ON s.id = t.signal_id
            WHERE t.status = 'CLOSED'
              AND t.pnl_pct IS NOT NULL
              AND t.created_at > datetime('now', '-90 days')
    """
    if bucket in ("crypto", "other"):
        # feat/kelly-asset-class-split (2026-09-05): Krypto-Trades duerfen
        # nicht die Groesse fuer Aktien-Trades DESSELBEN Signal-Clusters
        # setzen. Messung 2026-09-05 am Dip-Cluster
        # MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING (81 Trades, 90d):
        #     Krypto n= 9  Ø +16.52%   6/9 Wins
        #     Aktien n=72  Ø  +0.08%  20/72 Wins
        # Der Cluster trug damit 0.573 — den hoechsten Faktor ueberhaupt —
        # obwohl 89% der Stichprobe bei null liegt. Die 9 Krypto-Trades
        # fielen in ein Fenster mit XRP +45.2% / BTC +24.3%: das ist Beta,
        # kein Edge, und es hat die Aktien-Seite mitgehoben.
        op = "=" if bucket == "crypto" else "<>"
        sql += (f"      AND LOWER(COALESCE((SELECT i.asset_class FROM instruments i"
                f" WHERE i.instrument_id = t.instrument_id), '')) {op} 'crypto'\n")
    try:
        rows = db.fetchall(sql)
    except Exception:
        return []
    return [(r["st"], float(r["pnl_pct"])) for r in rows]


def _asset_bucket(asset_class: str | None) -> str | None:
    """'crypto' | 'other' — bewusst grob.

    Feiner aufzuteilen (stock/etf/commodity/index/forex) wuerde die
    Stichproben zerlegen: gemessen 2026-09-05 auf 90 Tagen stehen 321
    stock-Trades 18 crypto-, 14 etf-, 6 commodity-, 4 index- und 1
    forex-Trade gegenueber. Die Evidenz betrifft ausschliesslich Krypto
    (Ø +10.84% gegen Ø +0.08% bei Aktien), also trennt der Schnitt genau
    dort und nirgends sonst.
    """
    if not asset_class:
        return None
    return "crypto" if asset_class.strip().lower() == "crypto" else "other"


def _instrument_asset_class(db, instrument_id) -> str | None:
    """asset_class eines Instruments; None bei jedem Fehler (fail-open)."""
    if db is None or instrument_id is None:
        return None
    try:
        row = db.fetchone(
            "SELECT asset_class FROM instruments WHERE instrument_id = ?",
            (int(instrument_id),),
        )
    except Exception:
        return None
    if not row:
        return None
    try:
        return row["asset_class"]
    except Exception:
        return None


def _kelly_for_signal(signal_type: str, rows: list[tuple[str, float]],
                      min_trades: int,
                      shrink_k0: float = DEFAULT_SHRINK_K0) -> float | None:
    """Kelly fraction for one signal type (combo-aware, mit Schrumpfung).

    Hierarchie, von spezifisch nach allgemein:
      1. Exakte Combo-Zeichenkette
      2. Komponenten-Pool — alle Trades, deren Combo mindestens eine
         Komponente teilt (fix/kelly-components)
      3. Gesamtmittel ueber alle Trades im Fenster

    feat/kelly-shrinkage (2026-08-24): Bis hierher entschied eine harte
    Schwelle — ab ``min_trades`` wurde die eigene Schaetzung zu 100 %
    geglaubt, darunter gar nicht. Das ist bei den real vorkommenden
    Stichproben (n = 26..73) eine Klippe: eine Trefferquote aus 26 Trades
    hat noch rund +-10 Prozentpunkte Streuung, wird aber wie eine
    gesicherte Groesse behandelt.

    Stattdessen jetzt Empirical-Bayes-Schrumpfung (James-Stein / Normal-
    Normal): jede Ebene wird stufenlos zur naechsthoeheren gezogen,

        alpha = shrink_k0 / (n + shrink_k0)
        k     = (1 - alpha) * eigene_Schaetzung + alpha * naechste_Ebene

    Bei n = shrink_k0 zaehlt die eigene Schaetzung zur Haelfte; mit
    wachsendem n konvergiert sie gegen sich selbst. Wenig Daten heissen
    damit "nahe am Durchschnitt" statt "voll vertrauen" oder "ignorieren".

    ``min_trades`` bleibt als Mindest-Datengrundlage erhalten: reicht weder
    die exakte Combo noch der Pool an ihn heran, wird gar nicht skaliert.

    Returns the Kelly fraction (negative for negative-edge samples),
    or None if insufficient data.
    """
    if not rows:
        return None

    parts = {p.strip() for p in signal_type.split(",") if p.strip()}
    exact = [p for st, p in rows if st == signal_type]
    pooled = [
        p for st, p in rows
        if parts & {q.strip() for q in (st or "").split(",") if q.strip()}
    ]

    # Unveraenderte Mindestanforderung an die Datengrundlage.
    if len(exact) < min_trades and len(pooled) < min_trades:
        return None

    k0 = max(0.0, float(shrink_k0))
    overall = _kelly_fraction([p for _, p in rows])

    # Ebene 2: Komponenten-Pool -> Gesamtmittel
    if pooled:
        a_pool = k0 / (len(pooled) + k0) if k0 else 0.0
        k_pool = (1.0 - a_pool) * _kelly_fraction(pooled) + a_pool * overall
    else:
        k_pool = overall

    # Ebene 1: exakte Combo -> Pool
    if len(exact) >= min_trades:
        raw = _kelly_fraction(exact)
        a_exact = k0 / (len(exact) + k0) if k0 else 0.0
        shrunk = (1.0 - a_exact) * raw + a_exact * k_pool
    else:
        raw = _kelly_fraction(pooled) if pooled else overall
        shrunk = k_pool

    # Einseitig: die Schrumpfung darf die Position nur VERKLEINERN.
    #
    # Sie ist ein Sicherheitsabschlag gegen ueberschaetzte Edges — eine
    # zufaellig gute Serie soll nicht zu viel Kapital bekommen. In die
    # Gegenrichtung wirkt sie schaedlich: BB_LOWER+BB_EXTREME hat 1 Gewinner
    # aus 37 Trades (WR 2.7 %), was durchaus aussagekraeftig ist — die
    # Regression zum Mittel haette den Faktor von 0.150 auf 0.317 gehoben
    # und die Position damit verdoppelt. Gemessene Verlustbringer bleiben
    # unten; nur die optimistische Seite bekommt den Abschlag.
    #
    # Dieselbe Asymmetrie wie beim LLM-Einfluss im Projekt: nur daempfen,
    # nie boosten. Fraktionales Kelly ist ohnehin genau das — ein
    # einseitiger Abschlag auf die eigene Schaetzung.
    return min(shrunk, raw)


def kelly_size_factor(signal_type: str, db=None, min_trades: int | None = None,
                      kelly_cfg: dict | None = None,
                      asset_class: str | None = None,
                      instrument_id=None) -> float:
    """Risk-neutral Kelly position-size factor from realized performance.

    Strategy (combo-aware):
      1. Exact combo string (e.g. "RSI_EXTREME_OVERSOLD,BB_UPPER_RSI_OVERBOUGHT")
      2. If < min_trades, component pool (any shared component)
      3. If still < min_trades, return base (neutral, no scaling)

    Args:
        signal_type: Single signal or combo string (comma-separated).
        db: Database handle (required — caller owns the connection).
            Defaults to a fresh handle on the configured db.path.
        min_trades: Minimum closed trades before Kelly scaling kicks in.
            Defaults to config kelly_min_trades or DEFAULT_MIN_TRADES.
        kelly_cfg: Override dict with kelly_min_trades / kelly_base /
            kelly_scale / kelly_min_factor / kelly_max_factor (for tests).
        asset_class: Anlageklasse des Kandidaten. Nur wirksam, wenn
            sizing.kelly_asset_class_split aktiv ist — dann zaehlen fuer den
            Faktor NUR Trades derselben Klasse (crypto | other).
        instrument_id: Alternative zu asset_class — wird bei Bedarf
            nachgeschlagen. asset_class hat Vorrang.

    Returns:
        Sizing factor in [min_factor, max_factor]; base when no data.
    """
    cfg = kelly_cfg if kelly_cfg is not None else _get_sizing_cfg()
    # feat/kelly-asset-class-split: Default AUS (Blue/Green) — erst nach
    # Auswertung scharfschalten. Ohne Flag exakt das bisherige Verhalten.
    bucket = None
    if bool(cfg.get("kelly_asset_class_split", False)):
        _ac = asset_class
        if _ac is None and instrument_id is not None:
            _ac = _instrument_asset_class(db, instrument_id)
        bucket = _asset_bucket(_ac)

    mt = int(min_trades if min_trades is not None else cfg.get("kelly_min_trades", DEFAULT_MIN_TRADES))
    base = float(cfg.get("kelly_base", DEFAULT_BASE))
    scale = float(cfg.get("kelly_scale", DEFAULT_SCALE))
    min_factor = float(cfg.get("kelly_min_factor", DEFAULT_MIN_FACTOR))
    max_factor = float(cfg.get("kelly_max_factor", DEFAULT_MAX_FACTOR))

    if db is None:
        # Lazy import — keep this module import-light for tests.
        from bot.config import load_config
        from bot.db.connection import DB
        db = DB(load_config().db.abs_path)

    kelly = _kelly_for_signal(
        signal_type, _recent_trade_rows(db, bucket), mt,
        float(cfg.get("kelly_shrink_k0", DEFAULT_SHRINK_K0)),
    )
    if kelly is None:
        logger.debug("Kelly: %s insufficient trades (need %d), base=%.3f",
                     signal_type, mt, base)
        return base

    factor = max(min_factor, min(max_factor, base + scale * kelly))
    logger.debug("Kelly sizing: %s factor=%.3f (kelly=%.3f)",
                 signal_type, factor, kelly)
    return factor


# ── feat/sizing-drift-guard (2026-09-09) ─────────────────────────────────────
# Uebernommen aus dem Karpathy-Autoresearch-Loop:
#
#   "Bigger bets raises return and sharpe on the exact same trades.
#    That is a leverage dial, not an idea. So a try only counts if the
#    vol barely moved."
#
# Genau dieser Fall ist uns passiert: `kelly_base = 0.49` wurde am 2026-08-22
# so kalibriert, dass das TRADE-GEWICHTETE MITTEL bei 0.3011 lag — dem
# Risikoniveau, auf dem das Konto getestet war. Der Signalmix hat sich
# seither verschoben, das Mittel ist auf 0.463 gewandert: **+54 %
# durchschnittliche Positionsgroesse, ohne dass jemand eine Idee geaendert
# hatte.** Zwei Wochen lang hat es niemand bemerkt, weil die Zahl nirgends
# ueberwacht wurde.
#
# `kelly_base` ist ein Punkt-in-der-Zeit-Wert, keine strukturelle
# Eigenschaft. Diese Funktionen machen die Drift sichtbar.

DEFAULT_TARGET_MEAN = 0.30    # Risikoniveau, auf dem das Konto getestet ist
DEFAULT_DRIFT_BAND_PCT = 15.0  # erlaubte Abweichung, bevor gemeldet wird


def kelly_weighted_mean(db, kelly_cfg: dict | None = None) -> float | None:
    """Trade-gewichtetes Mittel der Kelly-Faktoren ueber das Kelly-Fenster.

    Gewichtet nach HAEUFIGKEIT, nicht nach Signaltyp: ein Cluster, der
    hundertmal feuert, praegt das Risikoniveau staerker als einer mit drei
    Trades. Genau so wurde `kelly_base` am 2026-08-22 kalibriert, also misst
    der Guard dasselbe.

    None, wenn keine Trades im Fenster liegen (dann gibt es nichts zu
    ueberwachen).
    """
    rows = _recent_trade_rows(db)
    if not rows:
        return None
    faktoren = [kelly_size_factor(st, db, kelly_cfg=kelly_cfg) for st, _ in rows]
    faktoren = [f for f in faktoren if f is not None]
    if not faktoren:
        return None
    return sum(faktoren) / len(faktoren)


def check_sizing_drift(db, target_mean: float | None = None,
                       band_pct: float | None = None,
                       kelly_cfg: dict | None = None) -> dict:
    """Meldet, wenn das mittlere Sizing vom freigegebenen Niveau abweicht.

    Gibt immer ein Dict zurueck (fail-open): `ok=True` heisst "im Band ODER
    nicht messbar" — der Guard darf nie ein Blocker sein, er ist ein Melder.
    """
    cfg = kelly_cfg if kelly_cfg is not None else _get_sizing_cfg()
    ziel = float(target_mean if target_mean is not None
                 else cfg.get("kelly_target_mean", DEFAULT_TARGET_MEAN))
    band = float(band_pct if band_pct is not None
                 else cfg.get("kelly_drift_band_pct", DEFAULT_DRIFT_BAND_PCT))

    out = {"ok": True, "current": None, "target": ziel, "band_pct": band,
           "drift_pct": None, "reason": None}
    try:
        aktuell = kelly_weighted_mean(db, kelly_cfg=kelly_cfg)
    except Exception as exc:
        logger.debug("Sizing-Drift nicht messbar: %s", exc)
        return out
    if aktuell is None or ziel <= 0:
        return out

    out["current"] = round(aktuell, 4)
    drift = (aktuell - ziel) / ziel * 100.0
    out["drift_pct"] = round(drift, 1)
    if abs(drift) > band:
        out["ok"] = False
        richtung = "ueber" if drift > 0 else "unter"
        out["reason"] = (
            f"mittleres Sizing {aktuell:.4f} liegt {abs(drift):.0f}% {richtung} "
            f"dem freigegebenen Niveau {ziel:.4f} (Band {band:.0f}%) — "
            f"Hebel-Regler, keine Idee"
        )
    return out
