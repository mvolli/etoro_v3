#!/usr/bin/env python3
"""Phase 1c — Signalhygiene: Instrument-Tag-Dedup + SELL/BUY-Trennung.

Zwei Probleme der Signal-Statistik:

1. **Instrument-Tag-Dedup:** Die `signals`-Tabelle ist stark dupliziert —
   jeder 5-min-Zyklus des signal_worker schreibt neue FRESH-Rows für dasselbe
   (Instrument, Tag, Typ). 90 Tage: ~35.941 Rows = ~5.596 Instrument-Tage
   (6,4x). Die Forward-Returns (Phase 1a) deduplizieren bereits beim Laden
   pro (instrument_id, date, signal_type) — dieses Skript macht die Dedup-
   Semantik explizit, dokumentiert sie und liefert eine saubere
   Instrument-Tag-Basis.

2. **SELL-Typen aus BUY-Statistik trennen:** ~42 % der Outcomes sind
   SELL-Richtung (TREND_KIPP_1H,SELL + BB_UPPER_RSI_OVERBOUGHT). Ihre
   Forward-Returns sind PREIS-basiert (Preis rauf = positiv) — für ein SELL
   ist das aber ein VERLUST. In der kombinierten Statistik erscheinen die
   besten Signale (SELL: Profit-WR 74-76 %) damit als mittelmäßig.

   Lösung: `direction`-Spalte (BUY/SELL) + `fwd_Nd_pnl`-Spalten, bei SELL
   vorzeichen-gedreht (Profit = Preis runter). Die Typ-Logik in risk.py/
   sizing.py bleibt UNVERÄNDERT (nur die Statistik trennt).

Idempotent: ADD COLUMN via PRAGMA-Check, UPDATE ist stabil.

Kein Write auf `signals` — `signal_outcomes` (Phase 1a) bekommt die neuen
Spalten. `signals` wird nur lesend für die Dedup-Analyse genutzt.
"""
from __future__ import annotations

import os
import sqlite3
from collections import Counter

DB = os.path.join(os.path.dirname(__file__), "..", "data", "trading.db")
LOOKBACK_DAYS = 90
COST = 0.011  # 1.1 % Round-Trip (identisch zu Phase 1a / e_hypothesis_test)

# SELL-Marker (identisch zu sell_exits.SELL_SIGNAL_MARKERS)
SELL_MARKERS = ("SELL", "OVERBOUGHT")


def _is_sell(signal_type: str) -> bool:
    """SELL-Richtung, wenn der Typ einen SELL/OVERBOUGHT-Marker enthält."""
    up = (signal_type or "").upper()
    return any(m in up for m in SELL_MARKERS)


def _has_col(db: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in db.execute(f"PRAGMA table_info({table})"))


def _trimmed_mean(vals: list[float]) -> float | None:
    """5 %-symmetrisch getrimmtes Mittel (Krypto-Ausreißer)."""
    if not vals:
        return None
    v = sorted(vals)
    k = max(0, int(len(v) * 0.05))
    t = v[k:len(v) - k] if len(v) - 2 * k > 0 else v
    return sum(t) / len(t) if t else None


def _stat(rows: list[dict], col: str) -> tuple[int, float | None, float]:
    """(n, trimmed_mean, profit_winrate) für eine Spalte."""
    vals = [r[col] for r in rows if r[col] is not None]
    n = len(vals)
    m = _trimmed_mean(vals)
    wr = 100.0 * sum(1 for x in vals if x > 0) / n if n else 0.0
    return n, m, wr


# ---------------------------------------------------------------------------
# 1. Instrument-Tag-Dedup (lesend auf signals)
# ---------------------------------------------------------------------------
def dedup_report(db: sqlite3.Connection) -> dict:
    rows = db.execute(
        """
        SELECT instrument_id, date(generated_at) d, COUNT(*) n,
               COUNT(DISTINCT signal_type) types
        FROM signals WHERE generated_at >= datetime('now', ?)
        GROUP BY instrument_id, date(generated_at)
        """,
        (f"-{LOOKBACK_DAYS} days",),
    ).fetchall()
    total = sum(r[2] for r in rows)
    instr_days = len(rows)
    # Dedup-Basis: pro (instrument, day, type) = eine kanonische Zeile
    type_rows = db.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT instrument_id, date(generated_at), signal_type
            FROM signals WHERE generated_at >= datetime('now', ?))
        """,
        (f"-{LOOKBACK_DAYS} days",),
    ).fetchone()[0]
    # Distribution: wie viele Signale pro Instrument-Tag?
    dist = Counter(r[2] for r in rows)
    return {
        "total": total,
        "instrument_days": instr_days,
        "dedup_type_rows": type_rows,
        "ratio": total / instr_days if instr_days else 0,
        "dist": dist,
    }


# ---------------------------------------------------------------------------
# 2. SELL/BUY-Trennung in signal_outcomes
# ---------------------------------------------------------------------------
def add_direction(db: sqlite3.Connection) -> None:
    if not _has_col(db, "signal_outcomes", "direction"):
        db.execute("ALTER TABLE signal_outcomes ADD COLUMN direction TEXT")
    # direction aus signal_type (stabil, idempotent)
    db.execute(
        "UPDATE signal_outcomes SET direction = "
        "CASE WHEN UPPER(signal_type) LIKE '%SELL%' "
        "OR UPPER(signal_type) LIKE '%OVERBOUGHT%' THEN 'SELL' ELSE 'BUY' END"
    )
    db.commit()


def add_pnl_columns(db: sqlite3.Connection) -> None:
    """fwd_Nd_pnl: BUY = fwd_Nd, SELL = -fwd_Nd (Profit = Preis runter)."""
    for col in ("fwd_1d", "fwd_3d", "fwd_5d", "fwd_10d"):
        pnl = f"{col}_pnl"
        if not _has_col(db, "signal_outcomes", pnl):
            db.execute(f"ALTER TABLE signal_outcomes ADD COLUMN {pnl} REAL")
        db.execute(
            f"UPDATE signal_outcomes SET {pnl} = "
            f"CASE WHEN direction = 'SELL' THEN -{col} ELSE {col} END"
            f" WHERE {col} IS NOT NULL"
        )
    db.commit()


def stats_report(db: sqlite3.Connection) -> dict:
    rows = [dict(r) for r in db.execute(
        "SELECT direction, signal_type, fwd_10d, fwd_10d_pnl FROM signal_outcomes"
    ).fetchall()]
    out: dict = {}
    for direction in ("BUY", "SELL"):
        sub = [r for r in rows if r["direction"] == direction]
        n, m, wr = _stat(sub, "fwd_10d_pnl")
        # raw price-based (zum Vergleich)
        nr, mr, wr_raw = _stat(sub, "fwd_10d")
        out[direction] = {"n": n, "pnl_trim5": m, "pnl_wr": wr,
                          "price_trim5": mr, "price_wr": wr_raw}
    # Gesamt (vorher kombiniert — der Fehlschluss)
    n_all, m_all, wr_all = _stat(rows, "fwd_10d_pnl")
    out["ALL"] = {"n": n_all, "pnl_trim5": m_all, "pnl_wr": wr_all,
                  "price_trim5": None, "price_wr": 0.0}
    return out


def type_breakdown(db: sqlite3.Connection) -> list[tuple]:
    return db.execute(
        """
        SELECT direction, signal_type, COUNT(*) n,
               AVG(fwd_10d_pnl) pnl,
               SUM(CASE WHEN fwd_10d_pnl>0 THEN 1 ELSE 0 END)*100.0/COUNT(*) wr
        FROM signal_outcomes
        WHERE fwd_10d_pnl IS NOT NULL
        GROUP BY direction, signal_type
        ORDER BY direction, n DESC
        """
    ).fetchall()


def main() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    print("=" * 64)
    print("Phase 1c — Signalhygiene")
    print("=" * 64)

    print("\n[1/3] Instrument-Tag-Dedup (lesend, signals) …")
    d = dedup_report(db)
    print(f"  90-Tage-Signale:      {d['total']}")
    print(f"  Instrument-Tage:      {d['instrument_days']}")
    print(f"  Dedup (Instr,Tag,Typ): {d['dedup_type_rows']}")
    print(f"  Duplikations-Ratio:   {d['ratio']:.1f}x pro Instrument-Tag")
    print(f"  Distribution Signale/Instrument-Tag (Top):")
    for k in sorted(d["dist"])[:8]:
        print(f"    {k:3d} Signale/Tag: {d['dist'][k]:5d} Instrument-Tage")

    print("\n[2/3] direction + fwd_Nd_pnl Spalten in signal_outcomes …")
    add_direction(db)
    add_pnl_columns(db)
    nb = db.execute("SELECT COUNT(*) FROM signal_outcomes WHERE direction='BUY'").fetchone()[0]
    ns = db.execute("SELECT COUNT(*) FROM signal_outcomes WHERE direction='SELL'").fetchone()[0]
    print(f"  BUY: {nb}   SELL: {ns}   (SELL-Anteil {100*ns/(nb+ns):.0f} %)")

    print("\n[3/3] Statistik getrennt nach Richtung (fwd_10d) …")
    s = stats_report(db)
    hdr = f"  {'Richtung':8s} {'n':>5s} {'pnl trim5':>11s} {'pnl WR':>8s}   {'preis trim5':>12s} {'preis WR':>9s}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for k in ("BUY", "SELL", "ALL"):
        r = s[k]
        ps = f"{r['pnl_trim5']:+.2f}%" if r["pnl_trim5"] is not None else "  -"
        ms = f"{r['price_trim5']:+.2f}%" if r["price_trim5"] is not None else "  -"
        print(f"  {k:8s} {r['n']:5d} {ps:>11s} {r['pnl_wr']:7.0f}%   {ms:>12s} {r['price_wr']:8.0f}%")

    print("\n  Typ-Aufschlüsselung (pnl-Basis):")
    for direction, st, n, pnl, wr in type_breakdown(db):
        print(f"    {direction:4s} {st[:48]:48s} n={n:4d} pnl={100*pnl:+.2f}% WR={wr:.0f}%")

    print("\nFERTIG. direction + fwd_Nd_pnl sind in signal_outcomes.")
    db.close()


if __name__ == "__main__":
    main()
