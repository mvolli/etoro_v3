#!/usr/bin/env python3
"""E8 — SEC Financial Statement Data Sets laden und kompakt extrahieren.

Quelle: https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets
(2009 Q1 - 2026 Q3, quartalsweise ZIPs). User-Agent mit Kontakt (VoLLi-Freigabe 17.10.).
Behaelt nur 10-K/10-Q (Originale) und wenige Kennzahlen-Tags ohne Segmente/Co-Registrant.
Ausgabe: data/e8_cache/sub.csv, data/e8_cache/num.csv, data/e8_cache/company_tickers.json.

  /usr/bin/python3 scripts/e8_fetch_fsds.py <fsds.html>
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "e8_cache")
UA = "VoLLi etoro-bot research volli1979@gmail.com"
FORMS = {"10-K", "10-Q"}
TAGS = {
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
    "SalesRevenueGoodsNet", "SalesRevenueServicesNet",
    "GrossProfit", "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
    "CostOfServices",
    "Assets", "NetIncomeLoss", "ProfitLoss",
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
}
SUB_COLS = ["adsh", "cik", "name", "sic", "form", "period", "fy", "fp", "filed", "accepted", "prevrpt"]
NUM_COLS = ["adsh", "tag", "ddate", "qtrs", "value"]


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            print(f"   Fehler {exc} — Versuch {attempt + 1}", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Download fehlgeschlagen: {url}")


def main() -> int:
    html = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
    paths = sorted(set(re.findall(r"(/files/dera/data/financial-statement-data-sets/\d{4}q[1-4]\.zip)", html)))
    print(f"{len(paths)} Quartale", flush=True)
    os.makedirs(os.path.join(OUT, "raw"), exist_ok=True)
    ct = os.path.join(OUT, "company_tickers.json")
    if not os.path.exists(ct):
        open(ct, "wb").write(get("https://www.sec.gov/files/company_tickers.json"))
    fs = open(os.path.join(OUT, "sub.csv"), "w", newline="")
    fn = open(os.path.join(OUT, "num.csv"), "w", newline="")
    ws = csv.writer(fs); wn = csv.writer(fn)
    ws.writerow(SUB_COLS); wn.writerow(NUM_COLS)
    for p in paths:
        q = re.search(r"(\d{4}q[1-4])", p).group(1)
        raw = os.path.join(OUT, "raw", f"{q}.zip")
        if not os.path.exists(raw) or os.path.getsize(raw) < 100_000:
            open(raw, "wb").write(get("https://www.sec.gov" + p))
            time.sleep(1)
        z = zipfile.ZipFile(raw)
        keep = set()
        with z.open("sub.txt") as fh:
            for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="ignore"), delimiter="\t",
                                    quoting=csv.QUOTE_NONE):
                if r.get("form") in FORMS:
                    keep.add(r["adsh"])
                    ws.writerow([r.get(c, "") for c in SUB_COLS])
        n = 0
        with z.open("num.txt") as fh:
            rd = csv.reader(io.TextIOWrapper(fh, encoding="utf-8", errors="ignore"), delimiter="\t",
                            quoting=csv.QUOTE_NONE)
            hdr = next(rd); ix = {c: i for i, c in enumerate(hdr)}
            for r in rd:
                if len(r) < len(hdr):
                    continue
                if r[ix["adsh"]] not in keep or r[ix["tag"]] not in TAGS:
                    continue
                if r[ix["uom"]] != "USD" or r[ix["coreg"]] or (r[ix["segments"]] if "segments" in ix else ""):
                    continue
                wn.writerow([r[ix["adsh"]], r[ix["tag"]], r[ix["ddate"]], r[ix["qtrs"]], r[ix["value"]]])
                n += 1
        print(f"  {q}: {len(keep)} Filings, {n} Werte", flush=True)
    fs.close(); fn.close()
    print("Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
