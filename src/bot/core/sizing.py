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
DEFAULT_BASE = 0.49
DEFAULT_SCALE = 0.45
DEFAULT_MIN_FACTOR = 0.15
# Natuerliche Obergrenze: kelly ist auf [-1, 1] geklemmt, also ist der
# hoechste rohe Faktor base + scale*1.0 = 0.94. max_factor ist die harte
# Cap darueber; der Default entspricht exakt dieser natuerlichen Grenze,
# d.h. der Cap bindet nur, wenn base/scale per Config nach oben getunet
# werden. (0.49+0.45 = 0.94, NICHT 0.49*1.5 — das war ein alter Kommentar.)
DEFAULT_MAX_FACTOR = 0.94


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


def _recent_trade_rows(db) -> list[tuple[str, float]]:
    """(signal_type, pnl_pct) for all CLOSED trades in the lookback window.

    Same query as before the risk-neutral change (fix/kelly-components,
    2026-07-26): exact combo strings live in signals.signal_type and are
    joined via trades.signal_id.
    """
    try:
        rows = db.fetchall(
            """
            SELECT s.signal_type AS st, t.pnl_pct
            FROM trades t
            JOIN signals s ON s.id = t.signal_id
            WHERE t.status = 'CLOSED'
              AND t.pnl_pct IS NOT NULL
              AND t.created_at > datetime('now', '-90 days')
            """,
        )
    except Exception:
        return []
    return [(r["st"], float(r["pnl_pct"])) for r in rows]


def _kelly_for_signal(signal_type: str, rows: list[tuple[str, float]],
                      min_trades: int) -> float | None:
    """Kelly fraction for one signal type (combo-aware).

    1. Exact match on the full combo string.
    2. Fallback: component pool — all trades whose combo shares at least
       one component (fix/kelly-components: multiplies the sample size
       while keeping the signal direction).

    Returns the Kelly fraction (negative for negative-edge samples),
    or None if insufficient data.
    """
    exact = [p for st, p in rows if st == signal_type]
    if len(exact) >= min_trades:
        return _kelly_fraction(exact)

    parts = {p.strip() for p in signal_type.split(",") if p.strip()}
    pooled = [
        p for st, p in rows
        if parts & {q.strip() for q in (st or "").split(",") if q.strip()}
    ]
    if len(pooled) >= min_trades:
        return _kelly_fraction(pooled)

    return None


def kelly_size_factor(signal_type: str, db=None, min_trades: int | None = None,
                      kelly_cfg: dict | None = None) -> float:
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

    Returns:
        Sizing factor in [min_factor, max_factor]; base when no data.
    """
    cfg = kelly_cfg if kelly_cfg is not None else _get_sizing_cfg()

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

    kelly = _kelly_for_signal(signal_type, _recent_trade_rows(db), mt)
    if kelly is None:
        logger.debug("Kelly: %s insufficient trades (need %d), base=%.3f",
                     signal_type, mt, base)
        return base

    factor = max(min_factor, min(max_factor, base + scale * kelly))
    logger.debug("Kelly sizing: %s factor=%.3f (kelly=%.3f)",
                 signal_type, factor, kelly)
    return factor
