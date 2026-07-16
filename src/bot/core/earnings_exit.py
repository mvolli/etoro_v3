"""Earnings-Exit — De-Risking BESTEHENDER Positionen vor Earnings.

OSS-Fund 2026-07-16 (investpilot/earnings_exit.py, ROKU-Fallstudie):
Ein After-Hours-Earnings-Gap umgeht den SL, weil nur zu Handelszeiten
geprueft wird. News-Flags daempfen nur NEUE Buys — bestehende Positionen
waren ungeschuetzt.

Regel (V1, bewusst simpel): Earnings-Termin in <= days_before Kalendertagen
UND Positions-Exposure >= min_exposure_pct der Equity -> Teilverkauf
(close_pct) ueber die bewaehrte sell_exits-Maschinerie (Verifikation,
Market-Guard, 24h-Cooldown via mark_sell_exit). Doppel-Halbierung pro
Earnings-Termin verhindert ein JSON-Marker.

Laeuft 1x taeglich (Gate EARNINGS_EXIT_AT) huckepack im risk_worker.
Nur Aktien; Termine via yfinance-Kalender (best effort, fail-open=skip).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

GATE_KEY = "EARNINGS_EXIT_AT"
GATE_HOURS = 20
MARKER_PATH = Path(__file__).resolve().parents[3] / "data" / "earnings_exit_done.json"


def should_trigger(days_until: int | None, exposure_pct: float, cfg: dict) -> bool:
    """Pure Trigger-Regel (testbar)."""
    if days_until is None or days_until < 0:
        return False
    ee = (cfg.get("exits") or {}).get("earnings_exit") or {}
    return (
        days_until <= int(ee.get("days_before", 2))
        and exposure_pct >= float(ee.get("min_exposure_pct", 5.0))
    )


def _gate_due(state_repo) -> bool:
    try:
        last = state_repo.get(GATE_KEY) or ""
        if last:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < GATE_HOURS * 3600:
                return False
        state_repo.set(GATE_KEY, datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
    return True


def _load_markers() -> dict:
    try:
        return json.loads(MARKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_markers(markers: dict) -> None:
    try:
        MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = MARKER_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(markers, indent=1), encoding="utf-8")
        tmp.replace(MARKER_PATH)
    except Exception as exc:
        logger.warning("earnings_exit: Marker-Save fehlgeschlagen: %s", exc)


def _next_earnings_days(yf_symbol: str) -> int | None:
    """Kalendertage bis zum naechsten Earnings-Termin (None = unbekannt)."""
    try:
        import yfinance as yf

        cal = yf.Ticker(yf_symbol).calendar or {}
        dates = cal.get("Earnings Date") or []
        if not dates:
            return None
        d0 = min(dates)
        today = datetime.now(timezone.utc).date()
        return (d0 - today).days
    except Exception:
        return None


def run_earnings_exit(db, state_repo, client, positions: list[dict], cfg: dict) -> dict:
    stats: dict = {"checked": 0, "actions": 0, "closed": 0, "symbols": []}
    ee_cfg = (cfg.get("exits") or {}).get("earnings_exit") or {}
    if not ee_cfg.get("enabled", False) or client is None or not positions:
        return stats
    if not _gate_due(state_repo):
        return stats

    try:
        equity = float(state_repo.get("CURRENT_EQUITY") or 0.0)
    except Exception:
        equity = 0.0
    if equity <= 0:
        return stats

    iids = sorted({
        int(p["instrumentID"]) for p in positions
        if p.get("instrumentID") is not None
    })
    ph = ",".join("?" for _ in iids)
    rows = db.execute(
        f"SELECT instrument_id, symbol, asset_class, "
        f"COALESCE(yfinance_symbol, symbol) AS yf "
        f"FROM instruments WHERE instrument_id IN ({ph})",
        iids,
    ).fetchall()
    meta = {int(r["instrument_id"]): dict(r) for r in rows}

    from bot.core.sell_exits import SellExitAction, execute_sell_exits
    from bot.db.repo import SignalRepo

    markers = _load_markers()
    close_pct = float(ee_cfg.get("close_pct", 50.0))
    actions: list = []

    for p in positions:
        iid = p.get("instrumentID")
        m = meta.get(int(iid)) if iid is not None else None
        if not m or (m.get("asset_class") or "stocks") != "stocks":
            continue
        stats["checked"] += 1
        amount = float(p.get("amount") or 0.0)
        exposure_pct = amount / equity * 100.0 if equity else 0.0
        days = _next_earnings_days(str(m["yf"]))
        if not should_trigger(days, exposure_pct, cfg):
            continue
        pos_id = str(p.get("positionID"))
        marker_key = f"{pos_id}:{days}d" if days is not None else pos_id
        if markers.get(pos_id):
            continue  # dieser Termin ist schon de-risked
        pnl = float((p.get("unrealizedPnL") or {}).get("pnL") or 0.0)
        actions.append(SellExitAction(
            signal_id=0,  # kein Signal — update_signal_status(0) ist ein No-op
            symbol=str(m["symbol"]),
            position_id=pos_id,
            instrument_id=int(iid),
            amount_usd=amount,
            open_rate=float(p.get("openRate") or 0.0),
            pnl_pct=(pnl / amount * 100.0) if amount else 0.0,
            close_pct=close_pct,
            reason=(
                f"Earnings-Exit: Termin in {days}d, Position {exposure_pct:.1f}% "
                f"der Equity — {close_pct:.0f}% De-Risking vor dem Event"
            ),
        ))
        markers[pos_id] = datetime.now(timezone.utc).isoformat()
        stats["symbols"].append(str(m["symbol"]))

    if not actions:
        return stats
    stats["actions"] = len(actions)
    _save_markers(markers)

    result = execute_sell_exits(client, SignalRepo(db), actions, db=db)
    stats["closed"] = int(result.get("closed") or 0)
    logger.info(
        "earnings_exit: %d Aktion(en), %d ausgefuehrt (%s)",
        stats["actions"], stats["closed"], ", ".join(stats["symbols"]),
    )
    return stats
