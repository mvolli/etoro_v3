#!/usr/bin/env python3
"""
Phase 1b — Confluence-Grading A/B/C (walk-forward).

Trainingfenster: W27–W36 (2026)   →  Schwellen werden HIER festgelegt
Testfenster:     W37–W40 (2026)   →  Validierung (Monotonie A>B>C)

Grading-Regel (aus Trainingsfenster abgeleitet, VOR dem Test fixiert):
  +1  wenn co_signals >= 1        (Konvergenz mit Ko-Signalen)
  +1  wenn ma200_pos > 0          (Preis über SMA200)
  +1  wenn rsi_depth < 60         (nicht überkauft)
  Score = Summe (0..3)
  Grade A: score >= 2
  Grade B: score == 1
  Grade C: score == 0

Das Grade wird als Spalte `confluence_grade` in `signals` geschrieben
(Join über instrument_id + date(signal_date) + signal_type).

Aufruf:  python scripts/confluence_grading.py
"""
import datetime
import os
import sqlite3
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "trading.db")
TRAIN_LO, TRAIN_HI = 27, 36
TEST_LO, TEST_HI = 37, 40


def iso_week(date_str):
    return datetime.date.fromisoformat(date_str).isocalendar()[1]


def confluence_score(r):
    """Score 0..3 aus drei Confluence-Features."""
    s = 0
    if (r["co_signals"] or 0) >= 1:
        s += 1
    if r["ma200_pos"] is not None and r["ma200_pos"] > 0:
        s += 1
    if r["rsi_depth"] is not None and r["rsi_depth"] < 60:
        s += 1
    return s


def grade(score):
    if score >= 2:
        return "A"
    if score == 1:
        return "B"
    return "C"


def trimmed_mean(rows, key="fwd_10d", pct=0.05):
    vals = sorted(r[key] for r in rows if r[key] is not None)
    if not vals:
        return None, 0
    k = max(1, int(pct * len(vals)))
    tv = vals[k:len(vals) - k] if len(vals) > 4 else vals
    return 100 * st.mean(tv), len(vals)


def evaluate(rows, label):
    print(f"\n  {label}  (n={len(rows)})")
    res = {}
    for g in ("A", "B", "C"):
        sub = [r for r in rows if r["_grade"] == g]
        m, n = trimmed_mean(sub)
        wr = 100 * sum(1 for r in sub if r["fwd_10d"] is not None and r["fwd_10d"] > 0) / n if n else 0
        res[g] = (n, m, wr)
        ms = f"{m:+.2f}%" if m is not None else "  -  "
        print(f"    Grade {g}: n={n:4d}  trim5={ms}  WR={wr:.0f}%")
    # Monotonie-Check
    means = [res[g][1] for g in ("A", "B", "C") if res[g][0] > 0]
    monotonic = all(means[i] > means[i + 1] for i in range(len(means) - 1))
    print(f"    Monotonie A>B>C: {'JA' if monotonic else 'NEIN'}")
    return res, monotonic


def main():
    print(f"Phase 1b — Confluence-Grading  ({datetime.datetime.now().isoformat()})")
    print(f"  Training: W{TRAIN_LO}–W{TRAIN_HI},  Test: W{TEST_LO}–W{TEST_HI}")

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    rows = [dict(r) for r in db.execute(
        "SELECT * FROM signal_outcomes WHERE fwd_10d IS NOT NULL"
    ).fetchall()]

    # Grade + Score berechnen
    for r in rows:
        r["_score"] = confluence_score(r)
        r["_grade"] = grade(r["_score"])

    train = [r for r in rows if TRAIN_LO <= iso_week(r["signal_date"]) <= TRAIN_HI]
    test = [r for r in rows if TEST_LO <= iso_week(r["signal_date"]) <= TEST_HI]

    print(f"\n  Outcomes mit fwd_10d: {len(rows)}  (train {len(train)}, test {len(test)})")

    tr_res, tr_mono = evaluate(train, "TRAINING W27-36")
    te_res, te_mono = evaluate(test, "TEST W37-40")

    # Score-Verteilung (Training)
    print("\n  Score-Verteilung (Training):")
    for s in (3, 2, 1, 0):
        sub = [r for r in train if r["_score"] == s]
        m, n = trimmed_mean(sub)
        print(f"    score={s}: n={n:4d} trim5={m:+.2f}%")

    # Grade in signals-Tabelle schreiben
    print("\n[1/2] confluence_grade Spalte in signals …")
    if not _has_col(db, "signals", "confluence_grade"):
        db.execute("ALTER TABLE signals ADD COLUMN confluence_grade TEXT")
    db.commit()

    # Join: signal_outcomes (hat grade) → signals (instrument_id, date, type)
    # signal_outcomes.signal_date == signals.generated_at[:10]
    # signal_outcomes.signal_type == signals.signal_type
    updated = 0
    for r in rows:
        cur = db.execute(
            """
            UPDATE signals SET confluence_grade = ?
            WHERE instrument_id = ?
              AND date(generated_at) = ?
              AND signal_type = ?
            """,
            (r["_grade"], r["instrument_id"], r["signal_date"], r["signal_type"]),
        )
        updated += cur.rowcount
    db.commit()
    print(f"  {updated} signals-Zeilen geupdated")

    # Verteilung in signals
    print("\n  Grade-Verteilung in signals (mit Outcome):")
    for row in db.execute(
        "SELECT confluence_grade, COUNT(*) c FROM signals "
        "WHERE confluence_grade IS NOT NULL GROUP BY confluence_grade ORDER BY confluence_grade"
    ):
        print(f"    {row[0]}: {row[1]}")

    # Zusammenfassung
    print("\n" + "=" * 60)
    print("ZUSAMMENFASSUNG (Vorregistrierung-Basis)")
    print("=" * 60)
    print(f"  Training Monotonie: {'JA' if tr_mono else 'NEIN'}")
    print(f"  Test Monotonie:     {'JA' if te_mono else 'NEIN'}")
    a_tr = tr_res["A"]
    a_te = te_res["A"]
    c_tr = tr_res["C"]
    a_tr_s = f"{a_tr[1]:+.2f}%" if a_tr[1] is not None else "  -"
    a_te_s = f"{a_te[1]:+.2f}%" if a_te[1] is not None else "  -"
    c_tr_s = f"{c_tr[1]:+.2f}%" if c_tr[1] is not None else "  -"
    print(f"  Grade A: train n={a_tr[0]} trim5={a_tr_s}  |  test n={a_te[0]} trim5={a_te_s}")
    print(f"  Grade C: train n={c_tr[0]} trim5={c_tr_s}")
    if a_tr[1] is not None and c_tr[1] is not None and c_tr[0] > 0:
        sep = a_tr[1] - c_tr[1]
        print(f"  A-C Separation (train): {sep:+.2f}%-Punkte")

    db.close()
    print("\nFertig.")


def _has_col(db, table, col):
    return any(c[1] == col for c in db.execute(f"PRAGMA table_info({table})").fetchall())


if __name__ == "__main__":
    main()
