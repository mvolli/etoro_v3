"""1H-Exit-Signal-Monitor — P17-Muster aus dem OSS-Vergleich (2026-07-16).

Erkennt kippende Trends auf STUNDENkerzen der offenen Positionen und
schreibt SELL-markierte Signale in die signals-Tabelle. Ausfuehrung und
Sicherheit uebernimmt die bestehende sell_exits-Engine (nur profitable
Positionen, 50%-Partial mit Verifikation, 24h-Cooldown pro Instrument,
Market-Open-Guard) — dieses Modul trifft KEINE Ausfuehrungsentscheidung.

Laeuft huckepack im risk_worker mit Stunden-Gate (EXIT_MONITOR_1H_AT):
1H-Kerzen aendern sich nur stuendlich, und process_sell_exits laeuft
direkt danach im selben Zyklus. Config: exits.signal_exit_1h.enabled.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SIGNAL_TYPE = "TREND_KIPP_1H,SELL"  # SELL-Marker -> sell_exits konsumiert
TTL_MINUTES = 90
RSI_MAX = 50.0                      # Bear-Cross nur mit schwachem RSI = echter Kipp
GATE_KEY = "EXIT_MONITOR_1H_AT"
GATE_MINUTES = 55


def detect_trend_kipp_1h(closes, rsi_max: float = RSI_MAX) -> dict | None:
    """Pure: MACD(12/26/9)-Linie kreuzt in den letzten 2 abgeschlossenen
    1H-Bars unter die Signallinie UND RSI(14) < rsi_max -> Trend kippt.

    Rueckgabe {'rsi':..., 'macd_hist':...} bei Treffer, sonst None.
    """
    try:
        import pandas as pd
        import ta as _ta

        s = pd.Series(list(closes), dtype="float64").dropna().reset_index(drop=True)
        if len(s) < 35:  # MACD slow=26 + Signal=9 braucht Vorlauf
            return None
        macd_obj = _ta.trend.MACD(s, window_slow=26, window_fast=12, window_sign=9)
        diff = (macd_obj.macd() - macd_obj.macd_signal()).dropna()
        rsi_series = _ta.momentum.RSIIndicator(s, window=14).rsi().dropna()
        if len(diff) < 3 or rsi_series.empty:
            return None
        rsi = float(rsi_series.iloc[-1])
        crossed = float(diff.iloc[-1]) < 0 and (
            float(diff.iloc[-2]) > 0 or float(diff.iloc[-3]) > 0
        )
        if crossed and rsi < rsi_max:
            return {"rsi": rsi, "macd_hist": float(diff.iloc[-1])}
    except Exception as exc:
        logger.debug("exit_monitor: Indikator-Fehler: %s", exc)
    return None


def _gate_due(state_repo) -> bool:
    """Stunden-Gate wie HEARTBEAT_EMBED_AT (fail-open)."""
    try:
        last = state_repo.get(GATE_KEY) or ""
        if last:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < GATE_MINUTES * 60:
                return False
        state_repo.set(GATE_KEY, datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
    return True


def _batch_1h_closes(yf_syms: list[str]) -> dict:
    """Ein yf.download fuer alle Symbole (5d/60m); letzte (angebrochene)
    Stunde wird verworfen — nur fertige Bars zaehlen."""
    out: dict = {}
    try:
        import yfinance as yf

        data = yf.download(
            yf_syms, period="5d", interval="60m",
            progress=False, auto_adjust=True, group_by="ticker", threads=True,
        )
        if data is None or data.empty:
            return out
        for sym in yf_syms:
            try:
                if len(yf_syms) == 1:
                    closes = data["Close"].dropna()
                else:
                    closes = data[sym]["Close"].dropna()
                if len(closes) >= 2:
                    out[sym] = closes.iloc[:-1]  # angebrochene Stunde weg
            except Exception:
                continue
    except Exception as exc:
        logger.debug("exit_monitor: Batch-Fetch fehlgeschlagen: %s", exc)
    return out


def run_exit_monitor(db, state_repo, positions: list[dict], cfg: dict) -> dict:
    """Stuendlicher 1H-Scan der offenen Positionen -> SELL-Signale (FRESH).

    Gibt {'scanned': n, 'signals': n, 'symbols': [...]} zurueck.
    """
    stats: dict = {"scanned": 0, "signals": 0, "symbols": []}
    em_cfg = (cfg.get("exits") or {}).get("signal_exit_1h") or {}
    if not em_cfg.get("enabled", False):
        return stats
    if not positions or not _gate_due(state_repo):
        return stats

    iids = sorted({
        int(p["instrumentID"]) for p in positions
        if p.get("instrumentID") is not None
    })
    if not iids:
        return stats
    ph = ",".join("?" for _ in iids)
    rows = db.execute(
        f"SELECT instrument_id, symbol, COALESCE(yfinance_symbol, symbol) AS yf "
        f"FROM instruments WHERE instrument_id IN ({ph})",
        iids,
    ).fetchall()
    sym_map = {int(r["instrument_id"]): (str(r["symbol"]), str(r["yf"])) for r in rows}
    if not sym_map:
        return stats

    closes_by_yf = _batch_1h_closes([yf for _, yf in sym_map.values()])
    rsi_max = float(em_cfg.get("rsi_max", RSI_MAX))

    from bot.db.repo import SignalRepo
    signal_repo = SignalRepo(db)

    for iid, (symbol, yf_sym) in sym_map.items():
        stats["scanned"] += 1
        try:
            closes = closes_by_yf.get(yf_sym)
            if closes is None:
                continue
            hit = detect_trend_kipp_1h(closes, rsi_max=rsi_max)
            if not hit:
                continue
            dup = db.fetchone(
                "SELECT id FROM signals WHERE instrument_id = ? AND status = 'FRESH' "
                "AND signal_type LIKE 'TREND_KIPP_1H%'",
                (iid,),
            )
            if dup:
                continue
            signal_repo.create(
                instrument_id=iid,
                signal_type=SIGNAL_TYPE,
                conviction="HIGH",
                score=70.0,
                rsi=hit["rsi"],
                macd_hist=hit["macd_hist"],
                price=float(closes.iloc[-1]),
                ttl_minutes=TTL_MINUTES,
            )
            stats["signals"] += 1
            stats["symbols"].append(symbol)
            logger.info(
                "exit_monitor: %s TREND_KIPP_1H (RSI %.0f, MACD-Diff %.4f) -> SELL-Signal",
                symbol, hit["rsi"], hit["macd_hist"],
            )
        except Exception as exc:
            logger.debug("exit_monitor: %s uebersprungen (%s)", symbol, exc)
    return stats
