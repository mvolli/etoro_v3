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
                    fresh_after: float | None = None,
                    max_age_days: float = FACTS_MAX_AGE_DAYS) -> list[tuple[str, dict]] | None:
    """Kennzahl-Historie einer Firma (Cache data/sec_cache/facts/<cik>.json, 7 Tage).

    fresh_after (epoch): Cache nur gueltig, wenn danach geschrieben (Screen zum
    Stichtag M braucht alle Meldungen bis M).
    """
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    p = FACTS_DIR / f"{cik}.json"
    ok = p.exists() and not refresh and time.time() - p.stat().st_mtime < max_age_days * 86400
    if ok and fresh_after is not None and p.stat().st_mtime < fresh_after:
        ok = False
    if ok:
        try:
            return [tuple(x) for x in json.loads(p.read_text())]
        except Exception:  # noqa: BLE001
            pass
    fl = None
    for attempt in range(3):                 # vereinzelt "No route to host" (17.10.)
        try:
            fl = facts_to_filings(_http_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                logger.warning("companyfacts CIK %s nicht verfuegbar: %s", cik, exc)
            time.sleep(2 * (attempt + 1))
        finally:
            time.sleep(0.12)                 # SEC: hoechstens 10 Anfragen/s
    if fl is None:
        if p.exists():                       # lieber alter Stand als Luecke in der Rangmenge
            try:
                return [tuple(x) for x in json.loads(p.read_text())]
            except Exception:  # noqa: BLE001
                return None
        return None
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


# ── Variante A: Kauf bei Einreichung (E11, VoLLi 2026-10-19) ─────────────────
# Regel (docs/vorregistrierung-e11-ereignisgetrieben-2026-10-19.md, Ergebnis
# docs/e11-ergebnis-2026-10-19.md): Reicht ein Titel 10-Q/10-K ein (Tag F) und
# liegt er am ersten Handelstag d1 nach F mit den neuen Kennzahlen im Top-Dezil
# der Rangmenge, Momentum > Median, und lag seine VORHERIGE Meldung an ihrem
# eigenen d1 nicht im Top-Dezil (Eintritt), wird zum Open d1+1 gekauft.

EDGAR_DAILY = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
_IDX_LINE = __import__("re").compile(r"^(10-K|10-Q)\s+.*?\s(\d{1,10})\s+(\d{8})\s+edgar/")


def parse_daily_index(text: str) -> set[int]:
    """CIKs mit 10-K/10-Q (ohne /A) aus einem EDGAR-Tagesindex (form.YYYYMMDD.idx)."""
    out = set()
    for line in text.splitlines():
        m = _IDX_LINE.match(line)
        if m:
            out.add(int(m.group(2)))
    return out


def daily_filers(day: date) -> set[int] | None:
    """CIKs mit 10-K/10-Q an `day`. Leere Menge an Tagen ohne Index (Wochenende,
    Feiertag, 404); None bei anderem Fehler (dann ist der Lauf unvollstaendig)."""
    import urllib.error
    q = (day.month - 1) // 3 + 1
    url = EDGAR_DAILY.format(y=day.year, q=q, ymd=day.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return parse_daily_index(r.read().decode("latin-1", "ignore"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return set()
        logger.warning("EDGAR-Tagesindex %s: %s", day, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("EDGAR-Tagesindex %s: %s", day, exc)
        return None
    finally:
        time.sleep(0.12)


class Ranker:
    """Rangmenge zu beliebigen Handelstagen D (E8/E11-Regeln), mit Cache.

    filings: {ticker: [(filed YYYY-MM-DD, metrics), ...]} chronologisch
    closes:  {ticker: (dates[], closes[])}  abgeschlossene Tageskurse
    nonfin:  Ticker der Rangmenge (Nicht-Finanzwerte)
    """

    def __init__(self, filings: dict, closes: dict, nonfin) -> None:
        self.filings, self.closes, self.nonfin = filings, closes, list(nonfin)
        self._idx = {t: {d: i for i, d in enumerate(c[0])} for t, c in closes.items()}
        self._cache: dict[str, tuple] = {}

    def at(self, D: str) -> tuple[dict, float, dict, float | None]:
        if D in self._cache:
            return self._cache[D]
        lo = (date.fromisoformat(D) - timedelta(days=MAX_AGE_DAYS)).isoformat()
        snap = {}
        for t in self.nonfin:
            ix = self._idx.get(t)
            if not ix or D not in ix:
                continue
            best = None
            for filed, m in self.filings.get(t) or []:
                if filed < D:
                    best = (filed, m)
                else:
                    break
            if best and best[0] >= lo:
                snap[t] = best[1]
        if len(snap) < 50:
            self._cache[D] = ({}, 1.0, {}, None)
            return self._cache[D]

        def pr(key, inv=False):
            items = sorted(snap.items(), key=lambda kv: kv[1][key]); n = len(items)
            r = {t: (k + 1) / n for k, (t, _) in enumerate(items)}
            return {t: 1 - v + 1 / n for t, v in r.items()} if inv else r
        r1, r2, r3 = pr("gpa"), pr("acc", True), pr("growth")
        score = {t: (r1[t] + r2[t] + r3[t]) / 3 for t in snap}
        vals = sorted(score.values()); pos = 0.9 * (len(vals) - 1); lo_i = int(pos)
        thr = vals[lo_i] + (vals[min(lo_i + 1, len(vals) - 1)] - vals[lo_i]) * (pos - lo_i)
        mom = {}
        for t in snap:
            k = self._idx[t][D]
            if k >= 252:
                c = self.closes[t][1]
                mom[t] = float(c[k - 21]) / float(c[k - 252]) - 1
        mv = sorted(mom.values()); n = len(mv)
        med = (mv[n // 2] if n % 2 else (mv[n // 2 - 1] + mv[n // 2]) / 2) if n else None
        self._cache[D] = (score, thr, mom, med)
        return self._cache[D]


def next_trading_day(dates: list[str], d: str) -> str | None:
    import bisect
    k = bisect.bisect_right(dates, d)
    return dates[k] if k < len(dates) else None


def event_candidates(ranker: Ranker, cal: list[str], d1: str, buyable: set[str]) -> list[tuple[float, str]]:
    """Variante A: Titel, deren neue Meldung an d1 den Eintritt ins Top-Dezil ausloest.

    cal: Handelstage (z. B. SPY), d1 = Entscheidungstag. Eine Meldung gehoert zu d1,
    wenn der erste Handelstag nach ihrem Einreichungsdatum d1 ist.
    """
    score, thr, mom, med = ranker.at(d1)
    if med is None:
        return []
    out = []
    for t in buyable:
        fl = ranker.filings.get(t) or []
        new = [i for i, (filed, _) in enumerate(fl) if next_trading_day(cal, filed) == d1]
        if not new or t not in score or score[t] < thr or t not in mom or mom[t] <= med:
            continue
        i = new[-1]
        prev_top = False
        if i > 0:
            pd1 = next_trading_day(cal, fl[i - 1][0])
            if pd1:
                ps, pthr, _, _ = ranker.at(pd1)
                prev_top = t in ps and ps[t] >= pthr
        if not prev_top:
            out.append((score[t], t))
    return sorted(out, reverse=True)
