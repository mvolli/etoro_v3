"""llm_profit_advisor.py — KI-gestuetzte Feinsteuerung der Gewinnmitnahme.

Wird vom risk_worker aufgerufen wenn ein mechanischer Trailing-Stop-Trigger
(MOMENTUM_FADE oder PROFIT_LEVEL) feuert. Die KI entscheidet die optimale
Schliessungsquote (0% = Trigger ignorieren/laufen lassen, 100% = Vollverkauf).

Timeout: 8 Sekunden. Bei Fehler/Timeout: mechanischer Default.
Kein Einfluss auf BE_CLOSE (Verlustschutz, immer mechanisch).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
LLM_TIMEOUT_S = 180.0  # 3 min — local consumer hardware kann 2-3 min brauchen
LLM_ADVISOR_COOLDOWN_S = 3600.0  # 1h: kein zweiter LLM-Call fuer dieselbe Position

# In-Prozess-Cache: position_id → (close_pct, reason, timestamp)
_ADVISOR_CACHE: dict = {}


def _get_context(db_path: Path, position_id: str | None, instrument_id: int | None) -> dict:
    """Laedt Position-State + Signal-Indikatoren + historische Performance aus DB."""
    ctx: dict = {
        "peak_pnl_pct": 0.0, "strategy": "swing", "signal_type": "UNBEKANNT",
        "rsi": None, "bb_pct": None, "macd_hist": None,
        "win_rate": None, "avg_pnl": None, "n": 0,
    }
    if not db_path:
        return ctx
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Position-State: peak_pnl_pct + strategy
        if position_id:
            cur.execute(
                "SELECT peak_pnl_pct, strategy FROM position_state WHERE position_id = ?",
                (position_id,),
            )
            row = cur.fetchone()
            if row:
                ctx["peak_pnl_pct"] = float(row["peak_pnl_pct"] or 0)
                ctx["strategy"] = row["strategy"] or "swing"

        # Letztes Signal fuer dieses Instrument: Typ + Indikatoren
        if instrument_id:
            cur.execute("""
                SELECT s.signal_type, ROUND(s.rsi, 1) as rsi,
                       ROUND(s.bb_pct, 3) as bb_pct,
                       ROUND(s.macd_hist, 6) as macd_hist
                FROM trades t JOIN signals s ON s.id = t.signal_id
                WHERE t.instrument_id = ? AND t.status = 'ACTIVE'
                ORDER BY t.created_at DESC LIMIT 1
            """, (instrument_id,))
            sig = cur.fetchone()
            if sig:
                ctx["signal_type"] = sig["signal_type"] or "UNBEKANNT"
                ctx["rsi"]       = sig["rsi"]
                ctx["bb_pct"]    = sig["bb_pct"]
                ctx["macd_hist"] = sig["macd_hist"]

            # Historische Performance des Signal-Typs (letzte 60 Tage)
            if ctx["signal_type"] != "UNBEKANNT":
                cur.execute("""
                    SELECT ROUND(AVG(t.pnl_pct), 2) as avg_pnl,
                           ROUND(1.0 * SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END)
                                 / COUNT(*), 2) as win_rate,
                           COUNT(*) as n
                    FROM trades t JOIN signals s ON s.id = t.signal_id
                    WHERE s.signal_type = ? AND t.status = 'CLOSED'
                      AND t.pnl_pct IS NOT NULL
                      AND t.created_at > datetime('now', '-60 days', 'utc')
                """, (ctx["signal_type"],))
                perf = cur.fetchone()
                if perf and perf["n"]:
                    ctx["win_rate"] = perf["win_rate"]
                    ctx["avg_pnl"]  = perf["avg_pnl"]
                    ctx["n"]        = int(perf["n"])

        conn.close()
    except Exception as e:
        logger.debug("[llm_profit_advisor] DB-Fehler: %s", e)
    return ctx


def advise_close_pct(
    symbol: str,
    trigger: str,
    pnl_pct: float,
    default_close_pct: float,
    regime: str,
    position_id: str | None = None,
    instrument_id: int | None = None,
    db_path: Path | None = None,
) -> tuple[float, str]:
    """KI-Beratung fuer optimale Schliessungsquote bei mechanischem Trailing-Trigger.

    Parameters
    ----------
    symbol : str
    trigger : str
        "MOMENTUM_FADE" | "PARTIAL_CLOSE"
    pnl_pct : float
        Aktueller unrealisierter Gewinn in %.
    default_close_pct : float
        Mechanischer Default (Fallback bei LLM-Ausfall).
    regime : str
        NORMAL / CAUTION / DEFENSIVE / CRITICAL
    position_id, instrument_id, db_path : optional
        Fuer DB-Kontext (Peak, Signal-Typ, RSI, History).

    Returns
    -------
    (close_pct, reason)
        close_pct=0.0 bedeutet: Trigger ueberspringen (Trend noch intakt).
    """
    # Cooldown: gleiche Position nicht oefter als 1x/h anfragen (risk_worker laeuft alle 5 min)
    import time as _time
    _now = _time.monotonic()
    _cache_key = str(position_id or symbol)
    if _cache_key in _ADVISOR_CACHE:
        _cached_pct, _cached_reason, _cached_ts = _ADVISOR_CACHE[_cache_key]
        if _now - _cached_ts < LLM_ADVISOR_COOLDOWN_S:
            logger.debug(
                "[llm_profit_advisor] %s: Cache-Hit (%.0f min alt) -> %.0f%%",
                symbol, (_now - _cached_ts) / 60, _cached_pct,
            )
            return _cached_pct, _cached_reason + " [cache]"

    ctx = _get_context(db_path, position_id, instrument_id)
    peak = ctx["peak_pnl_pct"] or pnl_pct
    strategy = ctx["strategy"]
    signal_type = ctx["signal_type"]

    # Indikatoren-String
    ind_parts = []
    rsi = ctx.get("rsi")
    bb  = ctx.get("bb_pct")
    mcd = ctx.get("macd_hist")
    if rsi is not None: ind_parts.append("RSI=%.0f" % rsi)
    if bb  is not None: ind_parts.append("BB%%=%.2f" % bb)
    if mcd is not None: ind_parts.append("MACD=%s" % ("auf" if mcd > 0 else "ab"))
    ind_str = " ".join(ind_parts) if ind_parts else "keine Indikatoren"

    # Signal-Performance-String
    n = ctx.get("n") or 0
    if n >= 3:
        wr = ctx.get("win_rate") or 0
        ap = ctx.get("avg_pnl") or 0
        perf_str = "%s: %.0f%% Trefferquote, avg %+.1f%% (n=%d)" % (signal_type, wr*100, ap, n)
    else:
        perf_str = "%s: zu wenig Daten" % signal_type

    pp_back = peak - pnl_pct

    prompt = (
        "/no_think\n"
        "Trailing-Trigger fuer %s. Entscheide die optimale Schliessungsquote.\n\n"
        "TRIGGER: %s | Mechanisch-Default: %.0f%%\n"
        "POSITION: PnL=%+.1f%% | Peak=%+.1f%% | %.1fpp abgegeben | strategy=%s\n"
        "SIGNAL-HISTORY: %s\n"
        "INDIKATOREN: %s\n"
        "REGIME: %s\n\n"
        "Entscheidungsmatrix:\n"
        "- RSI<65 + MACD aufwaerts + Trefferquote>50%%: Trend intakt -> 0%% (laufen lassen)\n"
        "- RSI 65-72, gemischte Signale: Teilsicherung -> 10-20%%\n"
        "- RSI>72 ODER MACD abwaerts: erhoehte Absicherung -> 25-35%%\n"
        "- RSI>78 ODER Trefferquote<30%%: defensiv -> 40-60%%\n"
        "- REGIME=DEFENSIVE oder CRITICAL: mindestens 35%%\n\n"
        '{"close_pct": <0-100>, "reason": "<max 10 Woerter>"}'
    ) % (
        symbol,
        trigger, default_close_pct,
        pnl_pct, peak, pp_back, strategy,
        perf_str,
        ind_str,
        regime,
    )

    payload = json.dumps({
        "model": "local",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 64,
        "temperature": 0.05,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()

    try:
        req = urllib.request.Request(
            LLM_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"].get("content", "") or ""
            start = content.find("{")
            end   = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(content[start:end])
                close_pct = float(result.get("close_pct", default_close_pct))
                close_pct = max(0.0, min(100.0, close_pct))
                reason = str(result.get("reason", ""))[:100]
                logger.info(
                    "[llm_profit_advisor] %s %s: KI -> %.0f%% (default %.0f%%) — %s",
                    trigger, symbol, close_pct, default_close_pct, reason,
                )
                _ADVISOR_CACHE[_cache_key] = (close_pct, reason, _now)
                return close_pct, reason
    except Exception as exc:
        logger.debug(
            "[llm_profit_advisor] %s %s: nicht verfuegbar (%s) -> default %.0f%%",
            trigger, symbol, type(exc).__name__, default_close_pct,
        )

    return default_close_pct, "mechanischer Default"
