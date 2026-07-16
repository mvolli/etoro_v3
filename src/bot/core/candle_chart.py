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
