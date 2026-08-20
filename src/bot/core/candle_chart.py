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

# feat/trade-event-marker (2026-07-28): Ein-/Ausstiegs-PUNKTE auf der
# Zeitachse statt nur horizontaler Level-Linien. Marker-Map portiert aus
# research/etoro-repos/trading/src/utils/trade_chart.py (dort Telegram).
# (marker, farbe, groesse) — EXIT-Rot bewusst != Bearish-Candle-Rot.
_MARKERS = {
    "ENTRY":         ("v", "#29B6F6", 90),
    "PARTIAL_CLOSE": ("D", "#FFAB00", 55),
    "EXIT":          ("^", "#FF1744", 90),
}


def _parse_ts(value) -> "object | None":
    """Tolerant: ISO-String ('...T...Z' oder 'YYYY-MM-DD HH:MM:SS') -> aware dt."""
    try:
        from datetime import datetime, timezone
        s = str(value).strip().replace("Z", "+00:00")
        if " " in s and "T" not in s:
            s = s.replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _stagger_y(items: list[dict], price_range: float,
               min_gap_pct: float = 0.07) -> None:
    """Label-y-Positionen entzerren (in-place, Feld 'y_text')."""
    if not items:
        return
    min_gap = price_range * min_gap_pct
    for item in sorted(items, key=lambda x: x["price"]):
        y = item["price"]
        for other in items:
            y_prev = other.get("y_text")
            if y_prev is not None and other is not item and abs(y - y_prev) < min_gap:
                y = y_prev + min_gap
        item["y_text"] = y


def _event_index(ev_ts, candle_times: list, n: int) -> int:
    """Event-Zeitstempel -> naechster Candle-Index (geclampt auf [0, n-1])."""
    if ev_ts is None:
        return n - 1
    best_i, best_d = 0, None
    for i, ct in enumerate(candle_times):
        if ct is None:
            continue
        d = abs((ev_ts - ct).total_seconds())
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    return max(0, min(best_i, n - 1))


def _draw_events(ax, events: list[dict], candle_times: list, n: int,
                 price_lo: float, price_hi: float,
                 with_labels: bool = True) -> None:
    """Trade-Events als Marker (+ optionale Labels rechts) einzeichnen."""
    price_range = (price_hi - price_lo) or (price_hi * 0.05) or 0.01
    items = []
    for ev in events or []:
        ev_type = str(ev.get("type", ""))
        if ev_type not in _MARKERS:
            continue
        try:
            price = float(ev.get("price"))
        except (TypeError, ValueError):
            continue
        marker, color, msize = _MARKERS[ev_type]
        idx = _event_index(_parse_ts(ev.get("ts")), candle_times, n)
        ax.scatter(idx, price, marker=marker, color=color, s=msize, zorder=5)
        items.append({
            "idx": idx, "price": price, "color": color,
            "label": str(ev.get("label") or ev_type.title()),
        })
    if not with_labels or not items:
        return
    _stagger_y(items, price_range)
    x_text = n + max(2, n * 0.03)
    for item in items:
        ax.annotate(
            item["label"],
            xy=(item["idx"], item["price"]),
            xytext=(x_text, item["y_text"]),
            xycoords="data", textcoords="data",
            color=item["color"], fontsize=7, va="center", ha="left", zorder=6,
            bbox=dict(boxstyle="round,pad=0.25", facecolor=_BG,
                      alpha=0.85, edgecolor=item["color"], lw=0.5),
            arrowprops=dict(arrowstyle="-", color=item["color"], lw=0.7),
        )
    # Platz fuer die Label-Spalte rechts
    ax.set_xlim(left=-1, right=n + max(12, n * 0.30))


def render_candles_png(
    candles: list[dict],
    title: str = "",
    entry: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
    exit_level: float | None = None,
    events: list[dict] | None = None,
) -> bytes | None:
    """eToro-Candles (fromDate/open/high/low/close) -> PNG-Bytes.

    events: optionale Trade-Events als Zeitachsen-Marker, je
    {"ts": iso-str, "type": "ENTRY"|"PARTIAL_CLOSE"|"EXIT",
     "price": float, "label": str} — ts wird auf den naechsten Candle
    gemappt (geclampt, wenn ausserhalb des Fensters).
    """
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

        if events:
            candle_times = [_parse_ts(c.get("fromDate")) for c in candles]
            _draw_events(ax, events, candle_times, n,
                         min(x for x in l if x) if any(l) else 0.0, max(h))

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


_INTERVAL_HOURS = {"OneHour": 1.0, "FourHours": 4.0, "OneDay": 24.0}


def trade_story_png_v2(
    client,
    instrument_id,
    symbol: str,
    events: list[dict],
    opened_at=None,
    closed_at=None,
    sl: float | None = None,
    tp: float | None = None,
) -> bytes | None:
    """Trade-Story-Chart mit Event-MARKERN (feat/trade-event-marker).

    events: [{"ts", "type": ENTRY|PARTIAL_CLOSE|EXIT, "price", "label"}].
    Intervall nach Haltedauer (pick_story_interval); reicht das Fenster
    nicht bis zum Entry zurueck, wird der Bar-Count aufgestockt (max 1000,
    eToro-API-Limit). Best effort, wirft nie.
    """
    try:
        if client is None or instrument_id is None:
            return None
        from datetime import datetime, timezone

        def _dt(v):
            return _parse_ts(v)

        opened_dt = _dt(opened_at) if opened_at else None
        closed_dt = _dt(closed_at) if closed_at else None
        now = datetime.now(timezone.utc)
        end = closed_dt or now
        days = ((end - opened_dt).total_seconds() / 86400.0) if opened_dt else None

        interval, count, label = pick_story_interval(days)
        # Fenster muss den Entry abdecken: Abstand von JETZT (Candles enden
        # heute), nicht nur die Haltedauer — plus etwas Vorlauf.
        if opened_dt:
            hours_back = (now - opened_dt).total_seconds() / 3600.0
            need = int(hours_back / _INTERVAL_HOURS[interval] * 1.15) + 8
            count = max(count, min(need, 1000))
        candles = client.get_candles(int(instrument_id), interval, count)
        return render_candles_png(
            candles,
            f"{symbol} — {label} Trade-Story",
            sl=sl, tp=tp,
            events=events,
        )
    except Exception as exc:
        logger.debug("trade_story_png_v2 fehlgeschlagen: %s", exc)
        return None


def trade_story_png(
    client,
    instrument_id,
    symbol: str,
    entry: float | None = None,
    exit_price: float | None = None,
    opened_at=None,
) -> bytes | None:
    """Trade-Story-Chart fuer Close-Embeds (feat/trade-story-charts).

    Duenner Wrapper um trade_story_png_v2: baut aus Entry/Exit eine
    2-Event-Liste (Entry am opened_at, Exit jetzt). Best effort, wirft nie.
    """
    try:
        events: list[dict] = []
        if entry:
            events.append({"ts": str(opened_at) if opened_at else None,
                           "type": "ENTRY", "price": float(entry),
                           "label": f"Entry {float(entry):g}"})
        if exit_price:
            events.append({"ts": None, "type": "EXIT",
                           "price": float(exit_price),
                           "label": f"Exit {float(exit_price):g}"})
        return trade_story_png_v2(client, instrument_id, symbol,
                                  events, opened_at=opened_at)
    except Exception as exc:
        logger.debug("trade_story_png fehlgeschlagen: %s", exc)
        return None


def daily_grid_png(stories: list[dict], bars: int = 60) -> bytes | None:
    """Grid der Tages-Trade-Stories fuer den Daily Report (max 4 Panels).

    stories: [{"title": str, "up": bool, "candles": [eToro-Candles],
               "events": [Event-Dicts]}]. Marker ohne Label-Spalte
    (Panels sind klein); Titel-Farbe nach PnL-Vorzeichen.
    """
    try:
        stories = [
            s for s in (stories or [])
            if s.get("candles") and len(s["candles"]) >= 5
        ][:4]
        if not stories:
            return None
        import io as _io

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        n_panels = len(stories)
        cols = 2 if n_panels > 1 else 1
        rows = (n_panels + cols - 1) // cols
        fig, axes = plt.subplots(rows, cols,
                                 figsize=(5.4 * cols, 3.2 * rows), dpi=110)
        try:
            axes_flat = list(axes.flat)
        except AttributeError:
            axes_flat = [axes]
        fig.patch.set_facecolor(_BG)

        for ax, story in zip(axes_flat, stories):
            candles = story["candles"][-bars:]
            o = [float(c.get("open") or 0) for c in candles]
            h = [float(c.get("high") or 0) for c in candles]
            l = [float(c.get("low") or 0) for c in candles]
            cl = [float(c.get("close") or 0) for c in candles]
            n = len(candles)
            ax.set_facecolor(_BG)
            for i in range(n):
                color = _UP if cl[i] >= o[i] else _DOWN
                ax.vlines(i, l[i], h[i], color=color, linewidth=0.7)
                ax.bar(i, abs(cl[i] - o[i]) or (h[i] - l[i]) * 0.001,
                       bottom=min(o[i], cl[i]), width=0.65, color=color,
                       edgecolor=color, linewidth=0.4)
            candle_times = [_parse_ts(c.get("fromDate")) for c in candles]
            _draw_events(ax, story.get("events"), candle_times, n,
                         min(x for x in l if x) if any(l) else 0.0,
                         max(h) if h else 0.0, with_labels=False)
            ax.set_title(str(story.get("title", "")),
                         color=(_UP if story.get("up", True) else _DOWN),
                         fontsize=9, fontweight="bold")
            ax.tick_params(colors=_FG, labelsize=6)
            ax.yaxis.tick_right()
            ax.set_xticks([])
            for sp in ax.spines.values():
                sp.set_color("#4A4D53")
        for ax in axes_flat[n_panels:]:
            ax.set_visible(False)
        fig.tight_layout()
        buf = _io.BytesIO()
        fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception:
        logger.debug("daily_grid_png failed", exc_info=True)
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


# ── Haupt-Konto-Report: Tagesveraenderung je Position ─────────────────────────
# feat/main-portfolio-report (2026-08-20). Form bewusst ein horizontaler
# Balken statt eines Kerzengitters: gefragt ist "wer hat sich wie stark
# bewegt" (Groesse + Vorzeichen ueber Entitaeten), nicht der Kursverlauf
# einzelner Titel — und ein Balken braucht keinen OHLC-Abruf.
#
# Farben sind die Status-Rollen (good/critical), nicht die generische
# Blau/Rot-Divergenz: Gewinn/Verlust IST ein Status, deckt sich mit der
# Finanzkonvention und mit COLOR_GREEN/COLOR_RED der Embeds. Beide Stufen
# klaren 3:1 auf der dunklen Flaeche. Die Farbe traegt die Aussage nie
# allein — jeder Balken hat Symbol und Prozentwert als Direktlabel.
_MP_SURFACE   = "#1a1a19"   # dunkle Chart-Flaeche
_MP_GOOD      = "#0ca30c"
_MP_CRITICAL  = "#d03b3b"
_MP_TEXT      = "#ffffff"
_MP_TEXT_DIM  = "#c3c2b7"
_MP_GRID      = "#383835"   # neutraler Mittelpunkt = recessives Raster
_MP_LABEL_WIDTH = 26        # Zeichen je Zeile (2 Zeilen = 52) — deckt auch
                            # "Vanguard FTSE All World High Dividend Yield" (43)


def movers_bar_png(movers: list[dict], top_n: int = 12,
                   title: str = "Tagesveränderung je Position") -> bytes | None:
    """Horizontaler Balken der Tagesveraenderung, staerkste Bewegung oben.

    *movers*: [{symbol, change_pct, pnl_delta, amount}] — bereits nach
    |change_pct| sortiert (diff_snapshots liefert das so).

    Gibt None zurueck, wenn nichts darzustellen ist oder matplotlib fehlt —
    der Report laeuft dann ohne Grafik weiter statt auszufallen.
    """
    if not movers:
        return None
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - Umgebungsfrage
        logger.warning("[chart] movers_bar_png: matplotlib fehlt (%s)", exc)
        return None

    # Auswahl und Anordnung sind ZWEI Schritte — vorher waren sie eins, und
    # das Ergebnis las sich als Zickzack (+6.8 / -5.4 / +4.1 / -3.7 ...),
    # weil die Liste nach BETRAG sortiert ankommt.
    #
    # 1. AUSWAHL von beiden Enden: die staerksten Gewinner UND die staerksten
    #    Verlierer. Wuerde man einfach die ersten N nach Vorzeichen sortieren,
    #    zeigte ein guter Tag nur Gewinner und die Verluste verschwaenden.
    gains = sorted((m for m in movers if float(m.get("change_pct") or 0) >= 0),
                   key=lambda m: -float(m["change_pct"]))
    losses = sorted((m for m in movers if float(m.get("change_pct") or 0) < 0),
                    key=lambda m: float(m["change_pct"]))
    half = max(1, top_n // 2)
    # Ist eine Seite duenn, darf die andere den Platz nutzen.
    n_gain = min(len(gains), max(half, top_n - len(losses)))
    n_loss = min(len(losses), top_n - n_gain)
    chosen = gains[:n_gain] + losses[:n_loss]

    # 2. ANORDNUNG als Rangliste: bester Titel oben, schlechtester unten.
    #    barh zeichnet Index 0 unten, deshalb aufsteigend sortieren.
    items = sorted(chosen, key=lambda m: float(m["change_pct"]))
    # Klarname statt Symbol: 'ICM_3040' ist der iShares Core MSCI World —
    # eine Achsenbeschriftung, die man nachschlagen muss, erklaert nichts.
    #
    # ZWEIZEILIG statt gekuerzt: "Vanguard FTSE All World High Dividend
    # Yield" auf 22 Zeichen zu schneiden ergibt "Vanguard FTSE All Wor…" —
    # das unterscheidet sich von anderen Vanguard-Titeln nicht mehr. Zwei
    # Zeilen a 22 Zeichen fassen 44 und damit praktisch jeden Namen
    # vollstaendig. Erst wenn auch das nicht reicht, wird die ZWEITE Zeile
    # gekuerzt — nie die erste, die traegt die Unterscheidung.
    import textwrap as _tw

    def _label(m):
        n = (m.get("name") or "").strip()
        if not n:
            return str(m.get("symbol") or "?")
        zeilen = _tw.wrap(n, width=_MP_LABEL_WIDTH, max_lines=2, placeholder="…")
        return "\n".join(zeilen) if zeilen else n

    labels = [_label(m) for m in items]
    values = [float(m.get("change_pct") or 0.0) for m in items]
    colors = [_MP_GOOD if v >= 0 else _MP_CRITICAL for v in values]

    # Zwei Textzeilen je Balken brauchen mehr Hoehe, sonst laufen die
    # Beschriftungen benachbarter Balken ineinander.
    height = max(2.8, 0.58 * len(items) + 1.2)
    fig, ax = plt.subplots(figsize=(9.0, height), dpi=150)
    fig.patch.set_facecolor(_MP_SURFACE)
    ax.set_facecolor(_MP_SURFACE)

    # Duenne Marke, 2px Flaechenabstand zwischen benachbarten Balken
    bars = ax.barh(range(len(items)), values, height=0.62, color=colors,
                   edgecolor=_MP_SURFACE, linewidth=2.0, zorder=3)

    span = max((abs(v) for v in values), default=1.0) or 1.0
    pad = span * 0.30
    ax.set_xlim(-span - pad, span + pad)
    ax.set_yticks(range(len(items)))
    ax.set_yticklabels(labels, color=_MP_TEXT, fontsize=8.5,
                       va="center", linespacing=1.25)

    # Direktlabel je Balken: Prozent + Dollar-Wirkung. Damit steht die
    # Aussage auch ohne Farbe da (CVD/Graustufendruck).
    for i, (b, v, m) in enumerate(zip(bars, values, items)):
        d = m.get("pnl_delta")
        txt = f"{v:+.1f}%" + (f"  ({float(d):+,.0f}$)" if d is not None else "")
        off = span * 0.03
        ax.text(v + (off if v >= 0 else -off), i, txt,
                va="center", ha="left" if v >= 0 else "right",
                color=_MP_TEXT, fontsize=8.5, zorder=4)

    # Nulllinie als Bezug, Raster recessiv
    ax.axvline(0, color=_MP_TEXT_DIM, linewidth=1.0, zorder=2)
    ax.xaxis.grid(True, color=_MP_GRID, linewidth=0.8, zorder=1)
    ax.set_axisbelow(True)
    ax.tick_params(axis="x", colors=_MP_TEXT_DIM, labelsize=8)
    ax.tick_params(axis="y", length=0)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)

    ax.set_title(title, color=_MP_TEXT, fontsize=11, pad=10, loc="left")
    ax.set_xlabel("Veränderung in %", color=_MP_TEXT_DIM, fontsize=8.5)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()
