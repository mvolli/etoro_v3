"""DB-verifizierte Signal-Scorecard — die eine Faktenbasis der LLM-Advisor.

feat/signal-scorecard (2026-07-20): Reviews rechneten bisher auf selbst
extrahierten (teils falschen) Statistiken — RoBoCoP meldete 10% WR, wo
25% real waren. Diese Scorecard wird deterministisch aus trades+signals
aggregiert, vom llm_review_worker taeglich nach
data/signal_scorecard.json refresht und von Review- UND Veto-Prompt
konsumiert. Regel: Die LLM bekommt Fakten, nie Rohdaten zum Selberzaehlen.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SL_KILL_PCT = -2.1  # pnl_pct darunter = am/nahe Stop gestorben


# fix/scorecard-macd-split-scope (2026-09-14): Die Dip-Buy-Familie — die
# einzige, in der "MACD-Wende" vs "Oversold ohne MACD" ein sinnhafter
# Vergleich ist (Vault: dip-buy-cluster-regime-dependent, Entry-Quality
# Gate uses the same cluster).
DIPBUY_TYPES_DEFAULT = (
    "BB_LOWER_RSI_OVERSOLD",
    "BB_EXTREME_RSI_OVERSOLD",
    "RSI_EXTREME_OVERSOLD",
    "MACD_TURN_BELOW_SMA20",
    "BB_LOW_MACD_IMPROVING",
)


def aggregate_scorecard(rows: list) -> dict:
    """rows: Iterable[(signal_type, pnl_usd, pnl_pct)] -> Scorecard-Dict.

    fix/scorecard-macd-split-scope (2026-09-14): macd_split zaeHLT nur die
    Dip-Buy-Familie (eine der DIPBUY_TYPES_DEFAULT-Komponenten im Signal).
    Zuvor landeten ALLE Signale mit MACD im Namen darin — inkl.
    TREND_PULLBACK-Kombos, die KEINE Dip-Buys sind (30d: "with" n=103
    30.1% / -69.50 USD vs "without" n=101 49.5% / -70.84 USD — die
    "MACD-Komponente senkt WR"-Merksatz floss woertllich in fast jeden
    Veto-Entscheid: 194 REJECTED in 7d, meist "Halve size ... falling
    knife"). Dip-Buy-true Split: with n=105 29.5% / -73.10 USD,
    without n=0 — der Merksatz ist dip-buy-spezifisch und darf trend-
    folgende Signale (TREND_PULLBACK, GOLDEN_CROSS) nicht verurteilen.
    """
    combos: dict = {}
    comps: dict = {}
    macd = {"with": [0, 0, 0.0], "without": [0, 0, 0.0]}
    for st, pnl, pct in rows:
        st = (st or "").strip()
        if not st:
            continue
        pnl = float(pnl or 0)
        c = combos.setdefault(st, [0, 0, 0.0, 0])
        c[0] += 1
        c[1] += 1 if pnl > 0 else 0
        c[2] += pnl
        c[3] += 1 if (pct or 0) <= SL_KILL_PCT else 0
        parts = [part.strip() for part in st.split(",") if part.strip()]
        for part in parts:
            p = comps.setdefault(part, [0, 0, 0.0, 0])
            p[0] += 1
            p[1] += 1 if pnl > 0 else 0
            p[2] += pnl
            p[3] += 1 if (pct or 0) <= SL_KILL_PCT else 0
        if any(part in DIPBUY_TYPES_DEFAULT for part in parts):
            m = macd["with" if any("MACD" in part.upper() for part in parts)
                      else "without"]
            m[0] += 1
            m[1] += 1 if pnl > 0 else 0
            m[2] += pnl

    def _fmt(d: dict) -> list:
        return [
            {
                "signal": k,
                "n": v[0],
                "win_rate_pct": round(100 * v[1] / v[0], 1),
                "pnl_usd": round(v[2], 2),
                "sl_kills": v[3],
            }
            for k, v in sorted(d.items(), key=lambda x: x[1][2])
        ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "combos": _fmt(combos),
        "components": _fmt(comps),
        "macd_split": {
            k: {
                "n": v[0],
                "win_rate_pct": round(100 * v[1] / max(v[0], 1), 1),
                "pnl_usd": round(v[2], 2),
            }
            for k, v in macd.items()
        },
        "macd_split_scope": "dip-buy",  # fix/scorecard-macd-split-scope 2026-09-14
    }


def refresh_scorecard_from_path(db_path, out_path, days: int = 30) -> dict | None:
    """Scorecard aus der Live-DB (read-only) aggregieren und persistieren."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = [
            (r["signal_type"], r["pnl_usd"], r["pnl_pct"])
            for r in conn.execute(
                """
                SELECT s.signal_type, t.pnl_usd, t.pnl_pct
                FROM trades t JOIN signals s ON s.id = t.signal_id
                WHERE t.status = 'CLOSED' AND t.pnl_usd IS NOT NULL
                  AND t.closed_at >= datetime('now', ?)
                """,
                (f"-{days} days",),
            )
        ]
        conn.close()
        sc = aggregate_scorecard(rows)
        sc["window_days"] = days
        Path(out_path).write_text(
            json.dumps(sc, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return sc
    except Exception:
        return None


STRATEGY_RULES = """STRATEGIE-BASIS (30d-DB-Fakten, nicht verhandelbar):
1. Oversold OHNE MACD-Wende = Falling Knife (30d: Dip-Buys MIT MACD-Wende
   WR 29.5 %, n=105, -73 USD; oversold OHNE MACD-Wende historisch WR 8-10 %,
   -159 USD). Der Code blockt solche Signale bereits — empfiehl sie NIE und
   daempfe verwandte Muster. WICHTIG (fix/scorecard-macd-split-scope,
   2026-09-14): der macd_split-Vergleich gilt NUR fuer Dip-Buy-Signale
   (Oversold/BB-Familie). Trend-folgende Signale (TREND_PULLBACK,
   GOLDEN_CROSS, MACD-Classic) sind KEINE Dip-Buys und werden von diesem
   Merksatz NIE verurteilt.
2. Tiefer RSI ist KEIN Kaufargument, sondern ein Krisenzeichen: je
   extremer, desto eher Messer (RSI<25: WR 9%).
3. Gewinner brauchen Zeit (Median 143h vs 45h bei Verlierern): junge
   Positionen im normalen ATR-Rauschen sind KEIN Panik-/Schliessgrund.
4. Der SL ist ATR-adaptiv; SL-Naehe allein ist kein Handlungsgrund.
5. Kombos mit WR < 25% bei n >= 5 laut Scorecard brauchen einen SEHR
   konkreten Grund fuer eine positive Empfehlung.
6. Asymmetrische Rechte: daempfen/verhindern JA, verstaerken NIE."""
