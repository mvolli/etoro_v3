"""eToro Trading Bot V3 — Position Sizing Helpers
src/bot/core/sizing.py

Half-Kelly factor for dynamic position sizing based on historical signal performance.
The Kelly Criterion (f* = win_rate - (1-win_rate)/(avg_win/avg_loss)) determines
the theoretically optimal fraction of capital to allocate. We use Half-Kelly (0.5*f)
as standard practice for robustness against estimation error.
"""
from __future__ import annotations


def kelly_size_factor(signal_type: str, db, min_trades: int = 10) -> float:
    """Half-Kelly position size factor based on historical signal performance.

    Formula:
        f  = win_rate - (1 - win_rate) / (avg_win / avg_loss)
        f* = 0.5 * f   (Half-Kelly: standard for real systems with imperfect estimates)
        clamped to [0.3, 1.5]

    Parameters
    ----------
    signal_type : str
        e.g. "BB_LOWER_RSI_OVERSOLD"
    db : DB
        Open DB connection.
    min_trades : int
        Minimum trades required; returns 1.0 (neutral) if insufficient data.

    Returns
    -------
    float
        Multiplier in [0.3, 1.5]. 1.0 = neutral (no change to sizing).

    fix/kelly-components (2026-07-26): vorher Exact-Match auf den vollen
    Combo-String — bei ~30 moeglichen Combos erreichten nur 2 je min_trades,
    Kelly war fuer fast alle Signale dauerhaft neutral. Jetzt: reicht der
    Exact-Match nicht, wird auf Komponenten-Ebene gepoolt (alle Closed
    Trades, deren Combo mindestens eine Komponente teilt) — verzehnfacht
    die Stichprobe bei gleicher Aussage-Richtung.
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
        return 1.0

    exact = [r for r in rows if r["st"] == signal_type]
    if len(exact) >= min_trades:
        rows = exact
    else:
        parts = {p.strip() for p in signal_type.split(",") if p.strip()}
        rows = [
            r for r in rows
            if parts & {p.strip() for p in (r["st"] or "").split(",") if p.strip()}
        ]

    if len(rows) < min_trades:
        return 1.0

    pnls = [float(r["pnl_pct"]) for r in rows]
    wins   = [p        for p in pnls if p > 0]
    losses = [abs(p)   for p in pnls if p <= 0]

    if not losses:
        return 1.5   # all winners → max factor
    if not wins:
        return 0.3   # all losers → min factor

    win_rate = len(wins) / len(pnls)
    avg_win  = sum(wins)   / len(wins)
    avg_loss = sum(losses) / len(losses)

    if avg_loss == 0:
        return 1.5

    f = win_rate - (1.0 - win_rate) / (avg_win / avg_loss)
    half_kelly = 0.5 * f

    return max(0.3, min(1.5, half_kelly))
