#!/usr/bin/env python3
"""Belegte Korrektur falscher instruments.yfinance_symbol (VoLLi 2026-10-17: „nur belegte Fälle“).

Grundlage: Abgleich-CSV (eToro-Kurs vs. Yahoo gespeichertes Symbol vs. Yahoo
Ticker, je US-Aktie; erzeugt mit dem Abgleich in docs/ANALYSE-yfinance-
symbole-2026-10-17.md). Korrigiert wird nur, wo der TICKER nachweislich zum
eToro-Kurs passt (±15 %) und das gespeicherte Symbol nicht:
  A  gespeichert = andere Firma, Ticker passt  -> Symbol korrigieren
  C  gespeichert ohne Daten,      Ticker passt  -> Symbol korrigieren
In beiden Faellen wird ohlcv_daily des Instruments geloescht (Cache): die Reihe
kann vom alten Symbol stammen (z. B. VOD.L in Pence statt ADR in USD) und waere
nach der Korrektur gemischt. Der data_worker laedt Watchlist-Titel neu.
Dazu die einzeln geprueften aktiven Faelle (EXTRA). yahoo_status wird auf
'unknown' gesetzt, damit der data_worker neu laedt.

  /usr/bin/python3 scripts/fix_yf_symbols.py <abgleich.csv>            # Trockenlauf
  /usr/bin/python3 scripts/fix_yf_symbols.py <abgleich.csv> --apply    # schreiben
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "trading.db"

# Einzeln gegen eToro geprueft am 17.10. (eToro / Yahoo-Ticker):
# CLPT 9,14 / 9,07 (gespeichert CNI = Canadian National), BRBI 14,84 / 14,21
# (BAP = Credicorp), ALX.ASX 3,79 / ALX.AX 3,79 (AAL.AX = andere Firma).
EXTRA = {10358: ("CLPT", "A"), 1059762: ("BRBI", "A"), 3313: ("ALX.AX", "A")}


def plan(csv_path: str) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for r in csv.DictReader(open(csv_path)):
        ticker = r["symbol"].upper().replace(".", "-")
        if r["yf"] == ticker or r["ticker_ok"] != "True":
            continue
        if r["yf_ok"] == "False":
            out[int(r["instrument_id"])] = (ticker, "A")
        elif r["yf_ok"] in ("", "None"):
            out[int(r["instrument_id"])] = (ticker, "C")
    out.update(EXTRA)
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    p = plan(sys.argv[1])
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA busy_timeout = 30000")
    n_a = sum(1 for _, k in p.values() if k == "A")
    rows_del = 0
    for iid, (new, kind) in p.items():
        rows_del += con.execute("SELECT COUNT(*) FROM ohlcv_daily WHERE instrument_id=?", (iid,)).fetchone()[0]
    print(f"Korrekturen: {len(p)} (A andere Firma: {n_a}, C ohne Daten: {len(p) - n_a}); "
          f"zu loeschende ohlcv_daily-Zeilen: {rows_del}")
    for iid, (new, kind) in list(p.items())[:10]:
        old = con.execute("SELECT symbol, yfinance_symbol FROM instruments WHERE instrument_id=?", (iid,)).fetchone()
        print(f"  {iid} {old} -> {new} [{kind}]")
    if not apply:
        print("Trockenlauf — nichts geschrieben.")
        return 0
    with con:
        for iid, (new, kind) in p.items():
            con.execute("UPDATE instruments SET yfinance_symbol=?, yahoo_status='unknown', yahoo_fail_count=0, "
                        "last_updated=CURRENT_TIMESTAMP WHERE instrument_id=?", (new, iid))
            con.execute("DELETE FROM ohlcv_daily WHERE instrument_id=?", (iid,))
        con.execute("INSERT INTO system_log (ts, level, worker, message, details) VALUES "
                    "(datetime('now'), 'INFO', 'fix_yf_symbols', ?, NULL)",
                    (f"yfinance_symbol korrigiert: {len(p)} US-Aktien/aktive Faelle (A {n_a}, "
                     f"C {len(p) - n_a}), {rows_del} ohlcv_daily-Zeilen geloescht (VoLLi 17.10.)",))
    print("geschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
