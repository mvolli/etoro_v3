"""Analysten-Strategie: Q2-Screen + Earnings-Katalysator S1 (VoLLi 2026-10-17).

Belege: E8 (Q2), E6 (S1), E9 (Ausstieg X0, Kapital k = 8) in docs/.
Entscheidungen VoLLi 17.10.:
  - Kandidaten: Q2 (monatlich, Qualitaet + Wachstum + Momentum) UND S1.
  - Ausstieg X0: 126 Handelstage halten, Stop -15 % (Broker-SL), dazu nur harte
    News-Ausstiege (8-K 1.03/3.01/4.02 seit Einstieg, News-Flag AVOID aus
    Headlines). KEIN Ausstieg vor Earnings, kein Trailing, kein LLM-EXIT.
  - Kapital: 8 Plaetze a ~6,5 % Equity (~$575); Signal-Trades A/C/D/E aus.

Die Positionen sind wie der Index-Kern von allen generischen Ausstiegspfaden
ausgenommen (risk_worker, llm_execution, client.close_position-Netz); nur
analyst_worker schliesst sie (allow_core=True). Broker-Take-Profit: keiner.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

SIGNAL_Q2 = "SCREEN_Q2"
SIGNAL_S1 = "EARN_S1"
TYPES = (SIGNAL_Q2, SIGNAL_S1)
CLOSED_STATUSES = ("CLOSED", "FAILED", "REJECTED", "CANCELLED")

DEFAULTS = {
    "mode": "off",              # off | shadow | live
    "max_open": 8,
    "tranche_pct": 6.5,         # % Equity je Position
    "cap_headroom": 0.98,       # hoechstens 98 % von max_trade_pct
    "min_usd": 200.0,
    "reserve_pct": 18.0,        # Cash nach Kauf >= 18 % Equity + Index-Kern-Luecke
    "stop_pct": 15.0,
    "hold_trading_days": 126,
    "q2": True,
    "s1": True,
    "q2_buy_window_days": 7,    # Kalendertage nach dem Stichtag
}


def load_config(cfg: dict | None) -> dict:
    sec = ((cfg or {}).get("analyst_strategy") or {}) if isinstance(cfg, dict) else {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in sec and sec[k] is not None:
            out[k] = sec[k]
    if out["mode"] is False:
        out["mode"] = "off"
    if str(out["mode"]) not in ("off", "shadow", "live"):
        logger.error("analyst_strategy.mode=%r ungueltig — 'off'", out["mode"])
        out["mode"] = "off"
    return out


# ── Positionen ───────────────────────────────────────────────────────────────

_ROWS_SQL = f"""
    SELECT t.id, t.symbol, t.instrument_id, t.status, t.api_position_id, t.amount_usd,
           t.created_at, t.confirmed_at, s.signal_type
    FROM trades t JOIN signals s ON s.id = t.signal_id
    WHERE s.signal_type IN ({",".join("?" * len(TYPES))})
      AND t.status NOT IN ({",".join("?" * len(CLOSED_STATUSES))})
"""


def open_rows(db: Any) -> list[dict]:
    """Offene und anstehende Strategie-Trades (APPROVED/SUBMITTING/ACTIVE …)."""
    try:
        return [dict(r) for r in db.fetchall(_ROWS_SQL, TYPES + CLOSED_STATUSES) or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyst_strategy: Trades nicht lesbar: %s", exc)
        return []


def position_ids(db: Any) -> set[str]:
    return {str(r["api_position_id"]) for r in open_rows(db) if r.get("api_position_id")}


def tranche_usd(cfg: dict, equity: float, regime_max_trade_pct: float) -> float:
    amt = equity * float(cfg["tranche_pct"]) / 100.0
    if regime_max_trade_pct > 0:
        amt = min(amt, equity * regime_max_trade_pct / 100.0 * float(cfg["cap_headroom"]))
    return round(amt, 2)


# ── Handelstage (NYSE) ───────────────────────────────────────────────────────

def _nyse_holidays(y0: int, y1: int) -> list[str]:
    from pandas.tseries.holiday import (
        AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay, USMartinLutherKingJr,
        USMemorialDay, USPresidentsDay, USThanksgivingDay, nearest_workday,
    )

    class _NYSE(AbstractHolidayCalendar):
        rules = [
            Holiday("NewYear", month=1, day=1, observance=nearest_workday),
            USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay,
            Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
            Holiday("July4", month=7, day=4, observance=nearest_workday),
            USLaborDay, USThanksgivingDay,
            Holiday("Christmas", month=12, day=25, observance=nearest_workday),
        ]
    return [d.strftime("%Y-%m-%d") for d in _NYSE().holidays(f"{y0}-01-01", f"{y1}-12-31")]


def trading_days_held(entry: date, today: date) -> int:
    """Handelstage von entry (einschliesslich, = Tag 1) bis today (einschliesslich)."""
    import numpy as np
    if today < entry:
        return 0
    hol = _nyse_holidays(entry.year, today.year)
    return int(np.busday_count(entry.isoformat(), (today + timedelta(days=1)).isoformat(), holidays=hol))


# ── Ausstiegsgruende ─────────────────────────────────────────────────────────

def _entry_date(row: dict) -> date | None:
    ts = row.get("confirmed_at") or row.get("created_at")
    try:
        return date.fromisoformat(str(ts)[:10])
    except (TypeError, ValueError):
        return None


def exit_reason(row: dict, cfg: dict, today: date, flags: dict, sec_filings=None) -> str | None:
    """Grund fuer den Ausstieg einer Strategie-Position, sonst None. Rein.

    sec_filings: [{form, date, items}] der letzten Tage (US-Titel) oder None.
    """
    entry = _entry_date(row)
    if entry is not None:
        n = trading_days_held(entry, today)
        if n >= int(cfg["hold_trading_days"]):
            return f"Haltedauer erreicht ({n} Handelstage seit {entry.isoformat()}, Regel X0: 126)"
    f = (flags or {}).get(row.get("symbol"))
    if f and str(f.get("flag", "")).upper() == "AVOID" and f.get("source") == "news_llm":
        return f"News-Flag AVOID: {str(f.get('reason', ''))[:120]}"
    if sec_filings and entry is not None:
        from bot.core.buy_veto import sec_block_reason
        recent = [x for x in sec_filings if str(x.get("date", "")) >= entry.isoformat()]
        r = sec_block_reason(recent)
        if r:
            return r
    return None
