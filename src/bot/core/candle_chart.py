"""Kerzenchart-PNG aus eToro-Candles fuer Discord-Embeds.

feat/candle-charts (2026-07-16): matplotlib (Agg, headless), Discord-
Dark-Style. Pure Rendering — wirft nie, gibt None bei Problemen zurueck.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_UP = "#2ECC71"
_DOWN = "#E74C3C"
_BG = "#2B2D31"      # Discord-Embed-Hintergrund
_FG = "#DBDEE1"


def render_candles_png(
    candles: list[dict],
    title: str = "",
    entry: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    exit_level: float | None = None,
) -> bytes | None:
    """eToro-Candles (fromDate/open/high/low/close) -> PNG-Bytes."""
    try:
        if not candles or len(candles) < 5:
            return None
        import io

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        o = [float(c.get("open") or 0) for c in candles]
        h = [float(c.get("high") or 0) for c in candles]
        l = [float(c.get("low") or 0) for c in candles]
        cl = [float(c.get("close") or 0) for c in candles]
        n = len(candles)

        fig, ax = plt.subplots(figsize=(8, 4), dpi=110)
        fig.patch.set_facecolor(_BG)
        ax.set_facecolor(_BG)

        for i in range(n):
            color = _UP if cl[i] >= o[i] else _DOWN
            ax.vlines(i, l[i], h[i], color=color, linewidth=0.8)
            ax.bar(i, abs(cl[i] - o[i]) or (h[i] - l[i]) * 0.001,
                   bottom=min(o[i], cl[i]), width=0.65, color=color,
                   edgecolor=color, linewidth=0.5)

        for level, color, label in (
            (entry, "#3498DB", "Entry"),
            (sl, "#E67E22", "SL"),
            (tp, "#F1C40F", "TP"),
            (exit_level, "#9B59B6", "Exit"),
        ):
            if level:
                ax.axhline(float(level), color=color, linestyle="--",
                           linewidth=1.0, alpha=0.9)
                ax.annotate(f"{label} {float(level):g}", xy=(0, float(level)),
                            xytext=(2, 3), textcoords="offset points",
                            color=color, fontsize=8)

        # Sparse Zeit-Labels aus fromDate (UTC, MM-DD HH:MM)
        ticks = list(range(0, n, max(1, n // 6)))
        labels = []
        for i in ticks:
            fd = str(candles[i].get("fromDate") or "")
            labels.append(fd[5:16].replace("T", " ") if len(fd) >= 16 else "")
        ax.set_xticks(ticks)
        ax.set_xticklabels(labels, fontsize=7, color=_FG)
        ax.tick_params(colors=_FG, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color("#4E5058")
        ax.grid(True, color="#4E5058", alpha=0.25, linewidth=0.5)
        if title:
            ax.set_title(title, color=_FG, fontsize=10)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=_BG)
        plt.close(fig)
        return buf.getvalue()
    except Exception as exc:
        logger.debug("render_candles_png fehlgeschlagen: %s", exc)
        return None


def pick_story_interval(days_held: float | None) -> tuple[str, int, str]:
    """Pure: Chart-Intervall nach Haltedauer, damit die ganze Story passt."""
    if days_held is None or days_held <= 2.5:
        return "OneHour", 72, "1H"
    if days_held <= 12:
        return "FourHours", 80, "4H"
    return "OneDay", 90, "1D"


def trade_story_png(
    client,
    instrument_id,
    symbol: str,
    entry: float | None = None,
    exit_price: float | None = None,
    opened_at=None,
) -> bytes | None:
    """Trade-Story-Chart fuer Close-Embeds (feat/trade-story-charts).

    Intervall richtet sich nach der Haltedauer (openDateTime, Broker-
    Wahrheit); Entry/Exit als Level-Linien. Best effort, wirft nie.
    """
    try:
        if client is None or instrument_id is None:
            return None
        days = None
        if opened_at:
            from datetime import datetime, timezone

            s = str(opened_at).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
        interval, count, label = pick_story_interval(days)
        candles = client.get_candles(int(instrument_id), interval, count)
        return render_candles_png(
            candles,
            f"{symbol} — {label} Trade-Story",
            entry=float(entry) if entry else None,
            exit_level=float(exit_price) if exit_price else None,
        )
    except Exception as exc:
        logger.debug("trade_story_png fehlgeschlagen: %s", exc)
        return None


def pulse_grid_png(movers, bars: int = 30) -> bytes | None:
    """[(symbol, move_pct, ohlcv_df), ...] -> Grid-PNG (max 5 Mini-Panels).

    feat/pulse-charts (2026-07-20): Kerzenpanels der Sharp Movers aus den
    im data_worker OHNEHIN gefetchten yfinance-DataFrames — kein extra
    API-Call. Panel-Titel traegt den Tagesmove, Farbe nach Vorzeichen.
    """
    try:
        movers = [
            (s, mv, df) for s, mv, df in (movers or [])
            if df is not None and len(df) >= 5
        ][:5]
        if not movers:
            return None
        import io as _io

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        n = len(movers)
        fig, axes = plt.subplots(1, n, figsize=(3.1 * n, 3.0), dpi=110)
        if n == 1:
            axes = [axes]
        fig.patch.set_facecolor(_BG)
        for ax, (sym, mv, df) in zip(axes, movers):
            d = df.tail(bars)
            o = [float(x) for x in d["Open"]]
            h = [float(x) for x in d["High"]]
            l = [float(x) for x in d["Low"]]
            cl = [float(x) for x in d["Close"]]
            ax.set_facecolor(_BG)
            for i in range(len(d)):
                color = _UP if cl[i] >= o[i] else _DOWN
                ax.vlines(i, l[i], h[i], color=color, linewidth=0.7)
                ax.bar(i, abs(cl[i] - o[i]) or (h[i] - l[i]) * 0.001,
                       bottom=min(o[i], cl[i]), width=0.65, color=color,
                       edgecolor=color, linewidth=0.4)
            ax.set_title(f"{sym} {mv:+.1f}%",
                         color=(_UP if mv >= 0 else _DOWN),
                         fontsize=10, fontweight="bold")
            ax.tick_params(colors=_FG, labelsize=6)
            ax.yaxis.tick_right()
            ax.set_xticks([])
            for sp in ax.spines.values():
                sp.set_color("#4A4D53")
        fig.tight_layout()
        buf = _io.BytesIO()
        fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        logger.debug("pulse_grid_png failed", exc_info=True)
        return None
