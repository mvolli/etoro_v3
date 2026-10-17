#!/usr/bin/env python3
"""E7 — SEC Insider Transactions Data Sets (Form 3/4/5) laden, Kaeufe extrahieren.

Quelle: https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets
(2006 Q1 - 2026 Q3, quartalsweise ZIPs). User-Agent mit Kontakt (VoLLi-Freigabe 17.10.).
Extrahiert nur Open-Market-Kaeufe (TRANS_CODE 'P', ACQUIRED 'A') aus Form 4 / 4/A.
Ausgabe: data/e7_cache/purchases.csv (eine Zeile je Transaktion) + raw ZIPs.

  /usr/bin/python3 scripts/e7_fetch_insider.py <form345.html>
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
OUT = os.path.join(ROOT, "data", "e7_cache")
UA = "VoLLi etoro-bot research volli1979@gmail.com"


def links(html_path: str) -> list[str]:
    html = open(html_path, encoding="utf-8", errors="ignore").read()
    found = sorted(set(re.findall(r"/files/[a-z0-9/_.-]*?(\d{4}q[1-4])_form345\.zip", html, re.I)))
    paths = {}
    for m in re.finditer(r"(/files/[a-z0-9/_.-]*?(\d{4}q[1-4])_form345\.zip)", html, re.I):
        paths[m.group(2).lower()] = m.group(1)
    return [paths[q] for q in sorted(paths)]


def get(url: str) -> bytes:
    req = urllib.request.Request("https://www.sec.gov" + url, headers={"User-Agent": UA,
                                                                     "Accept-Encoding": "identity"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            print(f"   Fehler {exc} — Versuch {attempt + 1}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Download fehlgeschlagen: {url}")


def tsv(z: zipfile.ZipFile, name: str):
    n = [x for x in z.namelist() if x.upper().endswith(name)]
    if not n:
        return []
    with z.open(n[0]) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="ignore"), delimiter="\t"))


def main() -> int:
    os.makedirs(os.path.join(OUT, "raw"), exist_ok=True)
    urls = links(sys.argv[1])
    print(f"{len(urls)} Quartale")
    out_path = os.path.join(OUT, "purchases.csv")
    cols = ["quarter", "accession", "filing_date", "doc_type", "symbol", "issuer_cik", "owner_cik",
            "owner_name", "is_director", "is_officer", "is_ten_pct", "officer_title",
            "trans_date", "shares", "price", "value"]
    with open(out_path, "w", newline="") as fo:
        w = csv.DictWriter(fo, fieldnames=cols)
        w.writeheader()
        total = 0
        for url in urls:
            q = re.search(r"(\d{4}q[1-4])", url).group(1)
            raw = os.path.join(OUT, "raw", f"{q}.zip")
            if not os.path.exists(raw) or os.path.getsize(raw) < 1000:
                data = get(url)
                open(raw, "wb").write(data)
                time.sleep(0.5)
            z = zipfile.ZipFile(raw)
            sub = {r["ACCESSION_NUMBER"]: r for r in tsv(z, "SUBMISSION.TSV")}
            owners: dict[str, list[dict]] = {}
            for r in tsv(z, "REPORTINGOWNER.TSV"):
                owners.setdefault(r["ACCESSION_NUMBER"], []).append(r)
            n = 0
            for r in tsv(z, "NONDERIV_TRANS.TSV"):
                if (r.get("TRANS_CODE") or "").upper() != "P":
                    continue
                if (r.get("TRANS_ACQUIRED_DISP_CD") or "").upper() != "A":
                    continue
                s = sub.get(r["ACCESSION_NUMBER"])
                if not s or (s.get("DOCUMENT_TYPE") or "") not in ("4", "4/A"):
                    continue
                try:
                    sh = float(r.get("TRANS_SHARES") or 0); px = float(r.get("TRANS_PRICEPERSHARE") or 0)
                except ValueError:
                    continue
                for o in owners.get(r["ACCESSION_NUMBER"], [{}])[:1]:
                    rel = (o.get("RPTOWNER_RELATIONSHIP") or "").upper()
                    w.writerow({
                        "quarter": q, "accession": r["ACCESSION_NUMBER"],
                        "filing_date": s.get("FILING_DATE"), "doc_type": s.get("DOCUMENT_TYPE"),
                        "symbol": (s.get("ISSUERTRADINGSYMBOL") or "").upper().strip(),
                        "issuer_cik": s.get("ISSUERCIK"), "owner_cik": o.get("RPTOWNERCIK"),
                        "owner_name": o.get("RPTOWNERNAME"),
                        "is_director": int("DIRECTOR" in rel), "is_officer": int("OFFICER" in rel),
                        "is_ten_pct": int("TENPERCENT" in rel.replace(" ", "")),
                        "officer_title": o.get("RPTOWNER_TITLE"),
                        "trans_date": r.get("TRANS_DATE"), "shares": sh, "price": px, "value": sh * px,
                    })
                    n += 1
            total += n
            print(f"  {q}: {n} Kaeufe (gesamt {total})", flush=True)
    print("Fertig:", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
