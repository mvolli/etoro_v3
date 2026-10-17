"""Earnings-Katalysator S1 live (VoLLi 2026-10-17). Regeln exakt wie E6/E9.

E6 (docs/vorregistrierung-e6-earnings-katalysator-2026-10-17.md):
  Ueberraschung (Reported - Estimate) / |Estimate| >= 5 %, |Estimate| >= 0,05
  Reaktion day0: Close/Close(-1) - 1 - SPY dasselbe >= +2 %
  Close(day0+1) > SMA200(day0+1)
  Einstieg zum Open von day0+2
  day0 = Termin, wenn vor 16:00 ET gemeldet (Uhrzeit bekannt), sonst naechster Handelstag.

Live: Der Lauf am Handelstag T kauft Ereignisse mit day0 = T-2 (Entscheidung zum
Close T-1, Einstieg heute). Wer berichtet hat, liefert der Nasdaq-Kalender; die
Zahlen (Zeitstempel, Schaetzung, Ist) kommen wie im Backtest aus yfinance
`get_earnings_dates`.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.request
from datetime import date, timedelta

logger = logging.getLogger(__name__)

SURPRISE_MIN, EAR_MIN, MIN_EST = 5.0, 0.02, 0.05
NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"


def nasdaq_reporters(day: date) -> list[str] | None:
    """Ticker, die laut Nasdaq-Kalender an `day` berichtet haben. None bei Fehler."""
    try:
        req = urllib.request.Request(NASDAQ_URL.format(d=day.isoformat()), headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            rows = ((json.loads(r.read()).get("data") or {}).get("rows")) or []
        return [str(x.get("symbol", "")).upper().replace(".", "-") for x in rows if x.get("symbol")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nasdaq-Earnings-Kalender %s nicht verfuegbar: %s", day, exc)
        return None


def day0_index(dates: list[str], ed: str, bmo: bool) -> int | None:
    """Index des Reaktionstags day0 in `dates` (E6-Regel)."""
    import bisect
    k0 = bisect.bisect_left(dates, ed)
    if k0 >= len(dates):
        return None
    if not bmo or dates[k0] != ed:
        k0 = k0 + 1 if dates[k0] == ed else k0
    return k0 if k0 < len(dates) else None


def earnings_rows(yf_symbol: str) -> list[tuple[str, bool, float]]:
    """[(Termin YYYY-MM-DD in New York, bmo, Ueberraschung %)] aus yfinance."""
    import yfinance as yf
    out = []
    try:
        df = yf.Ticker(yf_symbol).get_earnings_dates(limit=8)
    except Exception as exc:  # noqa: BLE001
        logger.debug("earnings_dates %s: %s", yf_symbol, exc)
        return out
    if df is None or len(df) == 0:
        return out
    for ts, r in df.iterrows():
        try:
            est, rep = float(r["EPS Estimate"]), float(r["Reported EPS"])
        except Exception:  # noqa: BLE001
            continue
        if not (math.isfinite(est) and math.isfinite(rep)) or abs(est) < MIN_EST:
            continue
        if ts.tzinfo is not None:
            ts = ts.tz_convert("America/New_York")
        bmo = (ts.hour, ts.minute) != (0, 0) and ts.hour < 16
        out.append((ts.strftime("%Y-%m-%d"), bmo, (rep - est) / abs(est) * 100.0))
    out.sort(key=lambda x: x[0])
    return out


def evaluate(dates: list[str], closes: list[float], spy: dict[str, float],
             events: list[tuple[str, bool, float]], d1_date: str) -> dict | None:
    """S1-Pruefung fuer ein Ereignis mit Entscheidungstag d1 (= day0+1). Rein.

    dates/closes: abgeschlossene Tageskurse des Titels (ohne heute).
    spy: {date: close}. Rueckgabe {ed, surprise, ear, close, sma200} oder None.
    """
    if d1_date not in dates:
        return None
    d1 = dates.index(d1_date)
    k0 = d1 - 1
    if k0 < 1 or d1 < 199:
        return None
    for ed, bmo, surprise in events:
        if day0_index(dates, ed, bmo) != k0:
            continue
        if surprise < SURPRISE_MIN:
            return None
        s0, s1 = spy.get(dates[k0 - 1]), spy.get(dates[k0])
        if not s0 or not s1:
            return None
        ear = closes[k0] / closes[k0 - 1] - 1 - (s1 / s0 - 1)
        if ear < EAR_MIN:
            return None
        sma200 = sum(closes[d1 - 199:d1 + 1]) / 200.0
        if not closes[d1] > sma200:
            return None
        return {"ed": ed, "surprise": round(surprise, 2), "ear": round(ear * 100, 2),
                "close": closes[d1], "sma200": round(sma200, 4)}
    return None


def scan(universe: dict, today: date, bars_fn) -> list[dict]:
    """S1-Kandidaten fuer den Einstieg heute (T).

    bars_fn(list[yf_symbol]) -> {yf_symbol: (dates, closes)} abgeschlossener Tage.
    Rueckgabe [{ticker, ed, surprise, ear}], nach Ueberraschung absteigend.
    """
    spy_b = bars_fn(["SPY"]).get("SPY")
    if not spy_b:
        logger.warning("S1: SPY-Kurse fehlen — kein Scan")
        return []
    sdates = [d for d in spy_b[0] if d < today.isoformat()]
    if len(sdates) < 3:
        return []
    d1_date, k0_date, km1_date = sdates[-1], sdates[-2], sdates[-3]
    spy = dict(zip(spy_b[0], spy_b[1]))
    # Termine, deren day0 = k0 sein kann: Kalendertage nach km1 bis k0
    days, d = [], date.fromisoformat(km1_date)
    while d <= date.fromisoformat(k0_date):
        days.append(d); d += timedelta(days=1)
    tickers: set[str] = set()
    for day in days:
        rows = nasdaq_reporters(day)
        if rows is None:
            continue
        tickers |= {t for t in rows if (universe.get(t) or {}).get("seg") == "large_mid"}
    if not tickers:
        return []
    yfs = {t: (universe[t].get("yf") or t) for t in tickers}
    bars = bars_fn(sorted(yfs.values()))
    out = []
    for t, y in sorted(yfs.items()):
        b = bars.get(y)
        if not b:
            continue
        dates = [x for x in b[0] if x < today.isoformat()]
        closes = list(b[1][:len(dates)])
        res = evaluate(dates, closes, spy, earnings_rows(y), d1_date)
        if res:
            out.append(dict(res, ticker=t))
    out.sort(key=lambda r: -r["surprise"])
    logger.info("S1: %d Berichte im Universum (day0 %s), %d Kandidaten", len(tickers), k0_date, len(out))
    return out
