#!/usr/bin/env python3
"""eToro Trading Bot V3 — taegliche Fortschreibung von signal_outcomes.

src/bot/core/outcome_refresh.py — feat/outcome-refresh (2026-10-16, VoLLi).

signal_outcomes war ein Schnappschuss (09.10., Signale bis 29.09., Kurse aus
den statischen data/exit_replay_bars). Damit war die Edge-Gate-Statistik
eingefroren: kein Typ konnte sich freiarbeiten, keiner fiel neu auf.

Diese Fortschreibung rechnet die Forward-Returns aller Signale der letzten
WINDOW_DAYS Tage neu, mit Kursen aus ohlcv_daily (der data_worker haelt sie
taeglich frisch). Regeln:

  - Nur abgeschlossene Tageskerzen (date < heute UTC). ohlcv_daily schreibt
    per INSERT OR REPLACE auch die laufende Kerze; ein Forward-Return darauf
    waere falsch.
  - Upsert statt INSERT OR IGNORE: Zeilen im Fenster werden neu berechnet,
    damit fehlende Horizonte nachwachsen und eine spaet korrigierte Kerze
    durchschlaegt. Zeilen aelter als das Fenster bleiben unberuehrt.
  - Gleiche Definitionen wie scripts/signal_forward_returns.py (Close(t+h)/
    Close(t) - 1 - COST, Dedup pro instrument_id/Tag/Typ) und
    scripts/signal_hygiene.py (direction, fwd_Nd_pnl; SELL = -fwd - 2*cost).
  - Features (ma200_pos, rsi_depth, ...) werden fuer neue Zeilen nicht
    berechnet; der Edge-Gate braucht nur fwd_5d_pnl.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("outcome_refresh")

COST = 0.011                  # identisch zu signal_forward_returns.COST
HORIZONS = (1, 3, 5, 10)
WINDOW_DAYS = 30              # Signale dieses Alters werden neu berechnet
SELL_MARKERS = ("SELL", "OVERBOUGHT")   # identisch zu signal_hygiene


def is_sell(signal_type: str) -> bool:
    up = (signal_type or "").upper()
    return any(m in up for m in SELL_MARKERS)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_signals(con, since: str) -> list[tuple]:
    """(instrument_id, signal_date, signal_type, symbol, conviction, score, rsi,
    status) — dedupliziert pro (instrument_id, Tag, Typ), erstes Signal zaehlt."""
    rows = con.execute(
        """
        SELECT s.instrument_id, substr(s.generated_at, 1, 10) AS d, s.signal_type,
               i.symbol, s.conviction, s.score, s.rsi, s.status
        FROM signals s LEFT JOIN instruments i ON i.instrument_id = s.instrument_id
        WHERE s.generated_at >= ? AND s.signal_type IS NOT NULL AND s.signal_type != ''
        ORDER BY s.generated_at
        """,
        (since,),
    ).fetchall()
    seen, out = set(), []
    for r in rows:
        key = (r[0], r[1], r[2])
        if key in seen:
            continue
        seen.add(key)
        out.append(tuple(r))
    return out


def load_closes(con, instrument_ids, since: str, before: str) -> dict[int, tuple[list, list]]:
    """{instrument_id: (dates, closes)} aus ohlcv_daily, nur date < before."""
    out: dict[int, tuple[list, list]] = {}
    ids = sorted(set(instrument_ids))
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        ph = ",".join("?" * len(chunk))
        acc: dict[int, list] = defaultdict(list)
        for iid, d, c in con.execute(
            f"SELECT instrument_id, substr(date, 1, 10), close FROM ohlcv_daily "
            f"WHERE instrument_id IN ({ph}) AND date >= ? AND date < ? "
            f"AND close IS NOT NULL AND close > 0 ORDER BY instrument_id, date",
            (*chunk, since, before),
        ):
            acc[iid].append((d, float(c)))
        for iid, pts in acc.items():
            out[iid] = ([p[0] for p in pts], [p[1] for p in pts])
    return out


def forward_returns(dates: list, closes: list, signal_date: str) -> dict | None:
    """Netto-Forward-Returns ab der Kerze des Signaltags; None ohne diese Kerze."""
    try:
        i = dates.index(signal_date)
    except ValueError:
        return None
    n = len(closes)
    return {
        f"fwd_{h}d": (closes[i + h] / closes[i] - 1 - COST) if i + h < n else None
        for h in HORIZONS
    }


def compute(con, *, window_days: int = WINDOW_DAYS, today: str | None = None) -> dict:
    """Berechnet die Zeilen fuer das Fenster. Schreibt nichts."""
    today = today or _today_utc()
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")
    sigs = load_signals(con, since)
    closes = load_closes(con, [s[0] for s in sigs], since, today)
    rows, no_bar = [], defaultdict(int)
    for iid, d, st, sym, conv, score, rsi, status in sigs:
        bar = closes.get(iid)
        fwd = forward_returns(*bar, d) if bar else None
        if fwd is None:
            no_bar[st] += 1
            continue
        sell = is_sell(st)
        row = {
            "signal_date": d, "instrument_id": iid, "symbol": sym,
            "signal_type": st, "conviction": conv, "score": score,
            "rsi_signal": rsi, "status": status, "cost": COST,
            "direction": "SELL" if sell else "BUY",
        }
        for h in HORIZONS:
            v = fwd[f"fwd_{h}d"]
            row[f"fwd_{h}d"] = v
            row[f"fwd_{h}d_pnl"] = None if v is None else ((-v - 2 * COST) if sell else v)
        rows.append(row)
    return {"since": since, "today": today, "signals": len(sigs),
            "rows": rows, "no_bar": dict(no_bar)}


_COLS = ("signal_date", "instrument_id", "symbol", "signal_type", "conviction",
         "score", "rsi_signal", "status", "cost", "direction",
         "fwd_1d", "fwd_3d", "fwd_5d", "fwd_10d",
         "fwd_1d_pnl", "fwd_3d_pnl", "fwd_5d_pnl", "fwd_10d_pnl")
_UPDATABLE = [c for c in _COLS if c not in ("signal_date", "instrument_id", "signal_type")]


def write(con, rows: list[dict]) -> int:
    """Upsert auf UNIQUE(instrument_id, signal_date, signal_type)."""
    sql = (
        f"INSERT INTO signal_outcomes ({', '.join(_COLS)}) "
        f"VALUES ({', '.join('?' * len(_COLS))}) "
        f"ON CONFLICT(instrument_id, signal_date, signal_type) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in _UPDATABLE)
    )
    con.executemany(sql, [tuple(r[c] for c in _COLS) for r in rows])
    con.commit()
    return len(rows)
