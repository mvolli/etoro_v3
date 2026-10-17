"""Analysten-Screen Q2 live (VoLLi 2026-10-17): Qualitaet + Wachstum + Momentum.

Regeln exakt wie E8 (docs/vorregistrierung-e8-qualitaet-wachstum-2026-10-17.md,
scripts/e8_quality_growth_test.py), Daten live statt aus den quartalsweisen
Bulk-Dateien: SEC XBRL companyfacts je Firma. companyfacts fuehrt jede Meldung
(accn) mit ihren Werten INKL. Vorjahresvergleich -- dieselbe Struktur wie
FSDS num.txt einer Meldung (ohne Segmente). Unterschied: Verfuegbarkeit ab
`filed` (Datum) statt `accepted` (Zeitstempel).

Monatlich zum letzten Handelstag M:
  Kennzahlen je Titel = juengste 10-K/10-Q mit filed < M, hoechstens 200 Tage alt
  GP/A, Accruals (invertiert), Umsatzwachstum -> Perzentil-Raenge ueber alle
  Nicht-Finanzwerte des Universums (S&P 1500) mit vollstaendigen Kennzahlen
  Score = Mittel der drei Raenge; Top-Dezil UND 12-1-Momentum > Median
  Kaufbar: nur Titel aus S&P 500/400 (E9).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
UNIVERSE_PATH = PROJECT_ROOT / "config" / "analyst_universe.json"
FACTS_DIR = PROJECT_ROOT / "data" / "sec_cache" / "facts"
SEC_UA = "VoLLi etoro-bot volli1979@gmail.com"
MAX_AGE_DAYS = 200
FACTS_MAX_AGE_DAYS = 7

REV_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
            "SalesRevenueGoodsNet", "SalesRevenueServicesNet"]
COGS_TAGS = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices"]
NI_TAGS = ["NetIncomeLoss", "ProfitLoss"]
CFO_TAGS = ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
ALL_TAGS = set(REV_TAGS + COGS_TAGS + NI_TAGS + CFO_TAGS + ["GrossProfit", "Assets"])
FP_QTRS = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}


# ── Kennzahlen je Meldung (identisch zu E8) ──────────────────────────────────

def _ddiff(a: str, b: str) -> int:
    return (datetime.strptime(a, "%Y%m%d") - datetime.strptime(b, "%Y%m%d")).days


def filing_metrics(sub_row: dict, vals: dict) -> dict | None:
    """sub_row: {fp, period(YYYYMMDD)}; vals: {(tag, ddate, qtrs): value | None}."""
    q = FP_QTRS.get(sub_row["fp"])
    if q is None:
        return None
    per = sub_row["period"]

    def cur(tag, qt=q):
        return vals.get((tag, per, qt))

    def prior(tag, qt=q):
        for (tg, dd, qq), v in vals.items():
            if tg == tag and qq == qt and v is not None and 350 <= _ddiff(per, dd) <= 380:
                return v
        return None

    def first(tags, qt=q):
        for tg in tags:
            v = cur(tg, qt)
            if v is not None:
                return tg, v
        return None, None

    assets = cur("Assets", 0)
    if assets is None or assets <= 0:
        return None
    rev = rev_prior = None
    for tg in REV_TAGS:
        c, p = cur(tg), prior(tg)
        if c is not None and p is not None:
            rev, rev_prior = c, p
            break
    if rev is None or rev_prior is None or rev_prior <= 0:
        return None
    gp = cur("GrossProfit")
    if gp is None:
        _, cogs = first(COGS_TAGS)
        if cogs is None:
            return None
        gp = rev - cogs
    _, ni = first(NI_TAGS)
    _, cfo = first(CFO_TAGS)
    if ni is None or cfo is None:
        return None
    scale = 4.0 / q
    return {"gpa": gp * scale / assets, "acc": (ni - cfo) * scale / assets, "growth": rev / rev_prior - 1}


# ── companyfacts -> Meldungen ────────────────────────────────────────────────

def _qtrs(start: str | None, end: str) -> int:
    if not start:
        return 0
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return int(round(days / 91.31))


def facts_to_filings(facts: dict) -> list[tuple[str, dict]]:
    """companyfacts-JSON -> [(filed YYYY-MM-DD, metrics)], chronologisch.

    Je Meldung (accn, nur 10-K/10-Q ohne /A) die Werte wie in FSDS num.txt:
    Schluessel (tag, ddate=end, qtrs). Periode = juengstes Bilanzdatum (Assets)
    der Meldung. Widerspruechliche Dubletten -> fehlend (E8-Regel).
    """
    gaap = ((facts or {}).get("facts") or {}).get("us-gaap") or {}
    by_accn: dict[str, dict] = defaultdict(lambda: {"vals": {}, "fp": None, "filed": None, "form": None})
    for tag in ALL_TAGS:
        for r in ((gaap.get(tag) or {}).get("units") or {}).get("USD") or []:
            if r.get("form") not in ("10-K", "10-Q"):
                continue
            f = by_accn[r["accn"]]
            f["fp"], f["filed"], f["form"] = r.get("fp"), r.get("filed"), r.get("form")
            k = (tag, r["end"].replace("-", ""), _qtrs(r.get("start"), r["end"]))
            v = float(r["val"])
            if k in f["vals"] and f["vals"][k] is not None and f["vals"][k] != v:
                f["vals"][k] = None
            elif k not in f["vals"]:
                f["vals"][k] = v
    out = []
    for accn, f in by_accn.items():
        bs = [dd for (tg, dd, q) in f["vals"] if tg == "Assets" and q == 0]
        if not bs or not f["filed"]:
            continue
        m = filing_metrics({"fp": f["fp"], "period": max(bs)}, f["vals"])
        if m is not None:
            out.append((f["filed"], m))
    out.sort(key=lambda x: x[0])
    return out


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def company_filings(cik: int, refresh: bool = False,
                    fresh_after: float | None = None) -> list[tuple[str, dict]] | None:
    """Kennzahl-Historie einer Firma (Cache data/sec_cache/facts/<cik>.json, 7 Tage).

    fresh_after (epoch): Cache nur gueltig, wenn danach geschrieben (Screen zum
    Stichtag M braucht alle Meldungen bis M).
    """
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    p = FACTS_DIR / f"{cik}.json"
    ok = p.exists() and not refresh and time.time() - p.stat().st_mtime < FACTS_MAX_AGE_DAYS * 86400
    if ok and fresh_after is not None and p.stat().st_mtime < fresh_after:
        ok = False
    if ok:
        try:
            return [tuple(x) for x in json.loads(p.read_text())]
        except Exception:  # noqa: BLE001
            pass
    try:
        fl = facts_to_filings(_http_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("companyfacts CIK %s nicht verfuegbar: %s", cik, exc)
        return None
    finally:
        time.sleep(0.12)                     # SEC: hoechstens 10 Anfragen/s
    p.write_text(json.dumps(fl))
    return fl


# ── Screen ───────────────────────────────────────────────────────────────────

def load_universe(path: Path = UNIVERSE_PATH) -> dict:
    """{ticker: {seg: large_mid|small, cik: int|None, sic: int|None, yf: str}}"""
    return json.loads(path.read_text())


def is_fin(sic) -> bool:
    return sic is not None and 6000 <= int(sic) <= 6999


def snapshot(filings: dict[str, list], M: str) -> dict[str, dict]:
    """Je Titel die juengste Meldung mit filed < M, hoechstens 200 Tage alt."""
    lo = (date.fromisoformat(M) - timedelta(days=MAX_AGE_DAYS)).isoformat()
    out = {}
    for t, fl in filings.items():
        best = None
        for filed, m in fl or []:
            if filed < M:
                best = (filed, m)
            else:
                break
        if best and best[0] >= lo:
            out[t] = best[1]
    return out


def rank_picks(snap: dict[str, dict], momentum: dict[str, float], buyable: set[str]) -> list[tuple[float, str]]:
    """E8-Q2: Score-Top-Dezil UND Momentum > Median der Rangmenge; nur `buyable`, nach Score."""
    if len(snap) < 50:
        return []

    def pr(key, inv=False):
        items = sorted(snap.items(), key=lambda kv: kv[1][key])
        n = len(items)
        r = {t: (k + 1) / n for k, (t, _) in enumerate(items)}
        return {t: 1 - v + 1 / n for t, v in r.items()} if inv else r
    r1, r2, r3 = pr("gpa"), pr("acc", True), pr("growth")
    score = {t: (r1[t] + r2[t] + r3[t]) / 3 for t in snap}
    vals = sorted(score.values())
    # np.percentile(…, 90) mit linearer Interpolation
    pos = 0.9 * (len(vals) - 1)
    lo_i = int(pos)
    thr = vals[lo_i] + (vals[min(lo_i + 1, len(vals) - 1)] - vals[lo_i]) * (pos - lo_i)
    mom = {t: v for t, v in momentum.items() if t in snap}
    if not mom:
        return []
    mv = sorted(mom.values())
    n = len(mv)
    med = mv[n // 2] if n % 2 else (mv[n // 2 - 1] + mv[n // 2]) / 2
    picks = [(score[t], t) for t in snap
             if score[t] >= thr and t in mom and mom[t] > med and t in buyable]
    return sorted(picks, reverse=True)


def momentum_12_1(closes) -> float | None:
    """Close M-21 / Close M-252 - 1 (closes: Liste bis einschliesslich M)."""
    if closes is None or len(closes) < 253:
        return None
    return float(closes[-22]) / float(closes[-253]) - 1
