#!/usr/bin/env python3
"""eToro v3 — LLM Position Review Worker.

Runs 3× daily (09:00, 14:00, 19:00 UTC).
Evaluates all open positions via LLM: HOLD / TIGHTEN / EXIT.
Results saved to data/llm_position_recommendations.json.
Discord alert sent for TIGHTEN and EXIT recommendations.

This worker is intentionally lightweight — no ghost analysis, no config changes.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("position_review_worker")

LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
LLM_TIMEOUT_S = 60
RECS_PATH = PROJECT_ROOT / "data" / "llm_position_recommendations.json"
OUTCOMES_PATH = PROJECT_ROOT / "data" / "llm_position_review_outcomes.json"


MARKET_CONTEXT_TICKERS = ["SPY", "QQQ", "^VIX"]


def _fetch_market_context() -> dict:
    """Holt SPY/QQQ Tagesperformance + VIX via yfinance (Prio 3)."""
    ctx: dict = {
        "spy_1d_pct": None, "qqq_1d_pct": None, "vix": None,
        "vix_label": "UNBEKANNT", "market_direction": "UNBEKANNT",
    }
    try:
        import yfinance as yf
        data = yf.download(
            MARKET_CONTEXT_TICKERS, period="5d", interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )
        if data is None or data.empty:
            return ctx

        def _pct(t):
            try:
                c = data[t]["Close"].dropna()
                return round(float(c.iloc[-1] / c.iloc[-2] - 1) * 100, 2) if len(c) >= 2 else None
            except Exception:
                return None

        def _last(t):
            try:
                return round(float(data[t]["Close"].dropna().iloc[-1]), 2)
            except Exception:
                return None

        ctx["spy_1d_pct"] = _pct("SPY")
        ctx["qqq_1d_pct"] = _pct("QQQ")
        vix = _last("^VIX"); ctx["vix"] = vix
        if vix is not None:
            ctx["vix_label"] = "NORMAL" if vix < 20 else ("ERHOEHT" if vix < 30 else "HOCH")
        spy, qqq = ctx["spy_1d_pct"], ctx["qqq_1d_pct"]
        if spy is not None and qqq is not None:
            avg = (spy + qqq) / 2
            ctx["market_direction"] = "BULLISH" if avg > 0.3 else ("BEARISH" if avg < -0.3 else "GEMISCHT")
    except Exception as e:
        print(f"[position_review] Marktkontext fehlgeschlagen: {e}")
    return ctx


def _backfill_outcomes(current_positions: list, db_path) -> list:
    """Bewertet Outcomes fuer ausgefuehrte LLM-Empfehlungen (>=24h alt) (Prio 2b)."""
    if not OUTCOMES_PATH.exists():
        return []
    try:
        outcomes = json.loads(OUTCOMES_PATH.read_text(encoding="utf-8"))
        if not isinstance(outcomes, list):
            return []
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    open_symbols = {p.get("symbol") for p in current_positions}
    open_pnl = {p["symbol"]: float(p.get("pnl_pct", 0) or 0)
                for p in current_positions if p.get("symbol")}

    changed = False
    for entry in outcomes:
        if entry.get("outcome_checked"):
            continue
        ts_exec = entry.get("ts_executed")
        if not ts_exec:
            continue
        try:
            exec_dt = datetime.fromisoformat(ts_exec)
            if exec_dt.tzinfo is None:
                exec_dt = exec_dt.replace(tzinfo=timezone.utc)
            if (now - exec_dt).total_seconds() < 86400:
                continue
        except Exception:
            continue

        symbol = entry.get("symbol", "")
        rec    = entry.get("recommendation", "")
        pnl_x  = entry.get("pnl_at_execution")
        px     = entry.get("price_at_execution")
        grade  = None
        delta  = None

        if rec == "EXIT" and symbol not in open_symbols:
            if px:
                try:
                    import yfinance as yf, sqlite3
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                    conn.row_factory = sqlite3.Row
                    r = conn.execute(
                        "SELECT yfinance_symbol FROM instruments WHERE symbol=? LIMIT 1",
                        (symbol,)
                    ).fetchone()
                    conn.close()
                    yf_sym = r["yfinance_symbol"] if r else None
                    if yf_sym:
                        td = yf.download(yf_sym, period="2d", interval="1d",
                                         auto_adjust=True, progress=False)
                        if not td.empty:
                            cp = float(td["Close"].dropna().iloc[-1])
                            pct = (cp - px) / px * 100
                            delta = round(pct, 2)
                            grade = ("GOOD" if pct < -0.5
                                     else "MISSED_UPSIDE" if pct > 1.0 else "NEUTRAL")
                except Exception as _be:
                    print(f"[position_review] Backfill {symbol}: {_be}")
            else:
                grade = "GOOD"
        elif rec == "TIGHTEN" and symbol in open_pnl and pnl_x is not None:
            delta = round(open_pnl[symbol] - pnl_x, 2)
            grade = ("AVOIDED_LOSS" if delta < -0.5
                     else "MISSED_UPSIDE" if delta > 1.0 else "NEUTRAL")

        if grade:
            entry["outcome_checked"] = True
            entry["outcome_grade"]   = grade
            entry["outcome_pnl_delta"] = delta
            changed = True

    if changed:
        try:
            OUTCOMES_PATH.write_text(
                json.dumps(outcomes, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as _we:
            print(f"[position_review] Outcomes schreiben: {_we}")
    return outcomes


def _format_recent_outcomes(outcomes: list) -> str:
    """Formatiert letzte 5 abgeschlossene Outcomes fuer LLM-Prompt."""
    checked = [o for o in outcomes if o.get("outcome_checked") and o.get("outcome_grade")]
    if not checked:
        return "  Noch keine ausgewerteten Entscheidungen."
    lines = []
    for o in checked[-5:]:
        pnl_s = f"{o['pnl_at_execution']:+.1f}%" if o.get("pnl_at_execution") is not None else "?%"
        d_s   = f"{o['outcome_pnl_delta']:+.1f}pp" if o.get("outcome_pnl_delta") is not None else ""
        lines.append(f"  {o['recommendation']} {o['symbol']} @ {pnl_s} -> {d_s} -> {o['outcome_grade']}")
    return "\n".join(lines)


def _load_env() -> None:
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _discord(fn_name: str, **kwargs) -> None:
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..'  ))
        import discord_embeds as _DE
        if _DE and hasattr(_DE, fn_name):
            getattr(_DE, fn_name)(**kwargs)
    except Exception:
        pass


def _collect_data(db_path: Path) -> dict:
    """Sammelt Positions- und Signal-Performance-Daten aus der DB."""
    import sqlite3
    data: dict = {"positions": [], "signal_perf": [], "regime": "NORMAL", "equity": 0.0}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Offene Positionen mit Signal-Typ und PnL
        # Hauptquery: Positionen + letztes Signal pro Instrument (RSI/BB/MACD)
        cur.execute("""
            SELECT ps.instrument_id, ps.api_position_id, ps.symbol,
                   COALESCE(ps.unrealized_pnl_pct, 0.0) as pnl_pct,
                   ps.amount_usd, ps.open_price, ps.current_price, ps.last_synced,
                   COALESCE(pos_state.peak_pnl_pct, 0.0) as peak_pnl_pct,
                   COALESCE(pos_state.strategy, 'swing') as strategy,
                   COALESCE(pos_state.be_active, 0) as be_active,
                   COALESCE(pos_state.momentum_faded, 0) as momentum_faded,
                   ls.rsi, ls.bb_pct, ls.macd_hist
            FROM portfolio_snapshot ps
            LEFT JOIN position_state pos_state ON pos_state.position_id = ps.api_position_id
            LEFT JOIN (
                SELECT instrument_id, rsi, bb_pct, macd_hist,
                       ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY generated_at DESC) as rn
                FROM signals
                WHERE generated_at > datetime('now', '-7 days', 'utc')
            ) ls ON ls.instrument_id = ps.instrument_id AND ls.rn = 1
            ORDER BY ps.amount_usd DESC
            LIMIT 25
        """)
        open_pos = [dict(r) for r in cur.fetchall()]

        # Signal-Typ + Eroeffnungsdatum + days_held — gemeinsames Modul
        # (fix/position-meta-dedup 2026-07-15: Logik war zwischen
        # position_review und llm_review dupliziert und divergierte bereits)
        from bot.core.position_meta import enrich_signal_and_age
        enrich_signal_and_age(cur, open_pos, statuses=("ACTIVE", "CONFIRMED"),
                              default_signal_type="UNBEKANNT")

        data["positions"] = open_pos

        # Frische Signale der letzten 36h für alle offenen Instrumente
        open_ids = [p["instrument_id"] for p in open_pos if p.get("instrument_id")]
        if open_ids:
            placeholders = ",".join("?" * len(open_ids))
            cur.execute(f"""
                SELECT s.instrument_id,
                       (SELECT symbol FROM portfolio_snapshot WHERE instrument_id = s.instrument_id LIMIT 1) as symbol,
                       s.signal_type, ROUND(s.rsi, 1) as rsi,
                       ROUND(s.bb_pct, 3) as bb_pct, ROUND(s.macd_hist, 6) as macd_hist,
                       s.generated_at
                FROM signals s
                WHERE s.instrument_id IN ({placeholders})
                  AND s.generated_at > datetime('now', '-36 hours', 'utc')
                ORDER BY s.generated_at DESC
            """, open_ids)
            data["recent_signals"] = [dict(r) for r in cur.fetchall()]
        else:
            data["recent_signals"] = []

        # Signal-Performance aus CLOSED Trades (letzte 60 Tage)
        cur.execute("""
            SELECT s.signal_type,
                   COUNT(*) as n,
                   ROUND(AVG(t.pnl_pct), 2) as avg_pnl,
                   ROUND(1.0 * SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) / COUNT(*), 2) as win_rate
            FROM trades t
            JOIN signals s ON s.id = t.signal_id
            WHERE t.status = 'CLOSED'
              AND t.pnl_pct IS NOT NULL
              AND t.created_at > datetime('now', '-60 days', 'utc')
            GROUP BY s.signal_type
            HAVING n >= 2
            ORDER BY n DESC
        """)
        data["signal_perf"] = [dict(r) for r in cur.fetchall()]

        # Regime + Equity
        cur.execute("SELECT key, value FROM system_state WHERE key IN ('CURRENT_REGIME', 'CURRENT_EQUITY', 'CURRENT_DRAWDOWN_PCT')")
        for row in cur.fetchall():
            if row["key"] == "CURRENT_REGIME":
                data["regime"] = row["value"] or "NORMAL"
            elif row["key"] == "CURRENT_EQUITY":
                try:
                    data["equity"] = float(row["value"] or 0)
                except Exception:
                    pass

        conn.close()
    except Exception as e:
        print(f"[position_review] DB-Fehler: {e}")
    return data


def _build_prompt(data: dict, market_ctx: dict | None = None, recent_outcomes: list | None = None) -> str:
    """Baut den LLM-Prompt für die Positions-Evaluation."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Positionen für Prompt aufbereiten
    pos_rows = []
    for p in data["positions"]:
        stype = p.get("signal_type", "?")
        pnl = p.get("pnl_pct", 0)
        peak = float(p.get("peak_pnl_pct") or 0)
        delta = pnl - peak  # negativ = bereits vom Peak abgegeben
        days = p.get("days_held")
        days_str = f"{days}d" if days is not None else "?"
        strategy = p.get("strategy") or "swing"
        be_str = " BE✓" if p.get("be_active") else ""
        faded_str = " FADED" if p.get("momentum_faded") else ""
        peak_part = f" peak={peak:+.1f}% Δ={delta:+.1f}%" if peak else ""
        # TA-Indikatoren (live wenn verfügbar, sonst aus Signal-DB)
        rsi  = p.get("rsi")
        bb   = p.get("bb_pct")
        mcd  = p.get("macd_hist")
        sma20 = p.get("sma20")
        sma50 = p.get("sma50")
        vol_r = p.get("vol_ratio")
        cur_price = p.get("current_price") or p.get("_live_price")
        live_tag  = " [LIVE]" if p.get("_live") else ""

        ta_parts = []
        if rsi is not None:
            ta_parts.append(f"RSI={rsi:.0f}")
        if bb is not None:
            ta_parts.append(f"BB%={bb:.2f}")
        if mcd is not None:
            ta_parts.append(f"MACD={'↑' if mcd > 0 else '↓'}")
        if sma50 is not None and cur_price is not None:
            ta_parts.append(f"SMA50={'↑' if cur_price > sma50 else '↓'}")
        if vol_r is not None:
            ta_parts.append(f"VolR={vol_r:.1f}x")
        ta_str = (" | " + " ".join(ta_parts) + live_tag) if ta_parts else live_tag

        pos_rows.append(
            f"  {p['symbol']}: PnL={pnl:+.1f}%{peak_part}{be_str}{faded_str}"
            f" held={days_str} signal={stype} strategy={strategy}{ta_str}"
        )

    # Signal-Performance
    sig_rows = []
    for sp in data["signal_perf"]:
        avg = sp.get("avg_pnl")
        wr = sp.get("win_rate")
        avg_str = f"{avg:+.2f}%" if avg is not None else "N/A"
        wr_str = f"{wr:.0%}" if wr is not None else "N/A"
        sig_rows.append(f"  {sp['signal_type']}: n={sp['n']} avg_pnl={avg_str} win_rate={wr_str}")

    regime = data.get("regime", "NORMAL")
    equity = data.get("equity", 0)

    # Frische Signale der letzten 36h (Kontext für LLM)
    recent_sigs = data.get("recent_signals", [])
    recent_rows = []
    for rs in recent_sigs[:20]:
        rsi_s = f" RSI={rs['rsi']}" if rs.get("rsi") else ""
        bb_s  = f" BB%={rs['bb_pct']}" if rs.get("bb_pct") else ""
        recent_rows.append(f"  {rs.get('symbol','?')}: {rs['signal_type']}{rsi_s}{bb_s} @ {rs['generated_at'][:16]}")

    # Marktkontext-Block (Prio 3)
    mkt_block = ""
    if market_ctx and market_ctx.get("spy_1d_pct") is not None:
        spy = market_ctx["spy_1d_pct"]
        qqq = market_ctx.get("qqq_1d_pct")
        vix = market_ctx.get("vix")
        vlbl = market_ctx.get("vix_label", "?")
        mdir = market_ctx.get("market_direction", "?")
        qqq_s = f" QQQ={qqq:+.1f}%" if qqq is not None else ""
        vix_s = f" VIX={vix:.1f} [{vlbl}]" if vix is not None else ""
        mkt_block = (
            f"\n## Marktkontext heute\n"
            f"SPY={spy:+.1f}%{qqq_s}{vix_s} -> Markt: {mdir}\n"
            f"-> Einzeltitel deutlich unter SPY: instrument-spezifisches Problem\n"
            f"-> Gesamtes Portfolio faellt mit Markt: Regime-Bewertung, keine Panik"
        )

    # Outcomes-Block (Prio 2c)
    outcomes_block = ""
    if recent_outcomes:
        _os = _format_recent_outcomes(recent_outcomes)
        outcomes_block = f"\n## Meine letzten Entscheidungen (Lernkontext)\n{_os}"

    prompt = f"""/no_think
Du bist Trading-Risikoanalyst. Bewerte jede offene Position und gib NUR valides JSON zurück.

## Zeitpunkt
{now_str} | Regime={regime} | Equity=${equity:.0f}{mkt_block}{outcomes_block}

## Offene Positionen
Format: Symbol: PnL% peak=X% Δ=Y% BE✓/FADED held=Nd signal=TYP strategy=S | RSI BB% MACD-Richtung
{chr(10).join(pos_rows) if pos_rows else "  Keine offenen Positionen"}

Legende: Δ = Rückgang vom Peak (negativ = Gewinn bereits abgegeben), FADED = Momentum gebrochen,
RSI > 70 = überkauft (Longs riskant), BB% > 0.95 = nahe oberem BB, MACD↓ = Schwung dreht,
SMA50=↓ = Preis unter 50-Tage-Schnitt (Abwärtstrend), VolR > 1.5 bei Verlust = Verkaufsdruck,
[LIVE] = Echtzeit-Daten (yfinance)

## Frische Signale der letzten 36h (technische Lage)
{chr(10).join(recent_rows) if recent_rows else "  Keine frischen Signale"}

## Historische Signal-Performance (CLOSED Trades, 60 Tage)
{chr(10).join(sig_rows) if sig_rows else "  Keine Daten verfügbar"}

## Entscheidungsregeln

Jede Evaluation bekommt zusaetzlich eine `close_pct` (0-100):
- HOLD:    close_pct=0 (keine Aktion)
- TIGHTEN: close_pct=15-40 (direkter Teilverkauf + Gewinnschutz)
- EXIT:    close_pct=50-100 (starke Reduktion oder Vollverkauf)

TIGHTEN (Direkter Teilverkauf) wenn MINDESTENS EIN Punkt zutrifft:
- momentum_faded=FADED im Label → Schwung bereits gebrochen → sofort TIGHTEN
- PnL > 0% UND Δ < -1.5% → Position gibt Peak-Gewinn ab → TIGHTEN
- PnL > +2% UND RSI > 72 → stark überkauft, Rücksetzer wahrscheinlich → TIGHTEN
- PnL > +3% UND BB% > 0.90 → nahe oberem BB, Ausdehnung erschöpft → TIGHTEN
- PnL > +2% UND MACD↓ → Momentum dreht bei profitabler Position → TIGHTEN
- PnL > 0% UND SMA50=↓ UND VolR > 1.5 → Preis bricht unter Trend mit hohem Volumen → TIGHTEN

EXIT (Position schliessen) wenn MINDESTENS EIN Punkt zutrifft:
- PnL < -2% UND RSI > 62 (kein Erholungszeichen) → keine Umkehr absehbar → EXIT
- PnL < -1% UND SMA50=↓ UND MACD↓ → Doppeltes Trendsignal gegen Position → EXIT
- PnL < -1% UND Signal-Typ hat avg_pnl < -1% bei mind. 3 historischen Trades → schlechtes Setup
- PnL < -3% → Verlust begrenzen unabhängig von RSI

HOLD in allen anderen Fällen.
Wenn RSI/BB/MACD fehlt (NULL) — nur auf PnL, Δ und momentum_faded stützen.
Bei < 3 historischen Trades pro Signal-Typ: Performance ignorieren, nur TA verwenden.

Antworte mit:
{{
  "evaluations": [
    {{"symbol": "XYZ", "recommendation": "HOLD", "close_pct": 0, "reason": "Trend intakt"}},
    {{"symbol": "ABC", "recommendation": "TIGHTEN", "close_pct": 25, "reason": "RSI 74 ueberkauft"}},
    {{"symbol": "DEF", "recommendation": "EXIT", "close_pct": 100, "reason": "Verlust ohne Erholung"}}
  ],
  "summary": "2-3 Sätze Gesamteinschätzung auf Deutsch"
}}"""
    return prompt


def _call_llm(prompt: str) -> dict | None:
    """Retry-Wrapper um _call_llm_once (fix/llm-fast-retry).

    Schnell-Fails (<10s, z.B. connection refused bei Modell-Reload) werden
    bis zu 2x mit 15s/30s Pause wiederholt — passt ins 120s-Cron-Budget.
    Voller Timeout wird NICHT wiederholt; dafuer gibt es den Watchdog-Re-Run.
    """
    for _attempt in range(3):
        _t0 = time.monotonic()
        result = _call_llm_once(prompt)
        if result is not None:
            return result
        _elapsed = time.monotonic() - _t0
        if _elapsed > 10.0 or _attempt >= 2:
            return None
        _wait = (15, 30)[min(_attempt, 1)]
        print(f"[position_review] LLM-Schnell-Fail nach {_elapsed:.1f}s — Retry in {_wait}s")
        time.sleep(_wait)
    return None


def _call_llm_once(prompt: str) -> dict | None:
    """Ruft llama-server auf und parst die JSON-Antwort."""
    payload = json.dumps({
        "model": "local",
        "messages": [
            {
                "role": "system",
                "content": "Du bist ein JSON-API für Trading-Risikoanalyse. Antworte AUSSCHLIESSLICH mit validem JSON."
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2048,
        "temperature": 0.05,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()

    try:
        req = urllib.request.Request(
            LLM_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
            msg = data["choices"][0]["message"]
            content = msg.get("content", "") or msg.get("reasoning_content", "") or ""
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
    except urllib.error.URLError as e:
        print(f"[position_review] LLM nicht erreichbar: {e}")
    except json.JSONDecodeError as e:
        print(f"[position_review] JSON-Parse-Fehler: {e}")
    except Exception as e:
        print(f"[position_review] LLM-Fehler: {e}")
    return None


def _fetch_live_indicators(positions: list[dict], db_path: Path) -> dict[int, dict]:
    """Holt aktuelle TA-Indikatoren via yfinance (1 Batch-Call für alle offenen Positionen).

    Gibt {instrument_id: {rsi, bb_pct, macd_hist, sma20, sma50, vol_ratio, price}} zurück.
    Fehler werden still ignoriert — Fallback sind die Signal-DB-Werte.
    """
    import sqlite3
    try:
        import yfinance as yf
    except ImportError:
        print("[position_review] yfinance nicht installiert — Live-Fetch übersprungen")
        return {}

    from bot.core.signals import compute_indicators

    # yfinance_symbol je instrument_id aus DB
    instrument_ids = [p["instrument_id"] for p in positions if p.get("instrument_id")]
    if not instrument_ids:
        return {}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        placeholders = ",".join("?" * len(instrument_ids))
        cur.execute(
            f"SELECT instrument_id, yfinance_symbol FROM instruments "
            f"WHERE instrument_id IN ({placeholders}) "
            f"  AND yfinance_symbol IS NOT NULL AND yfinance_symbol != ''",
            instrument_ids,
        )
        id_to_yf: dict[int, str] = {row["instrument_id"]: row["yfinance_symbol"] for row in cur.fetchall()}
        conn.close()
    except Exception as e:
        print(f"[position_review] DB-Fehler bei yfinance_symbol-Lookup: {e}")
        return {}

    if not id_to_yf:
        print("[position_review] Keine yfinance_symbols gefunden — Live-Fetch übersprungen")
        return {}

    tickers = list(set(id_to_yf.values()))
    print(f"[position_review] yfinance Batch-Fetch: {len(tickers)} Ticker ({len(id_to_yf)} Instrumente)...")

    try:
        raw = yf.download(
            tickers,
            period="3mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"[position_review] yfinance Batch-Fehler: {e}")
        return {}

    if raw is None or raw.empty:
        print("[position_review] yfinance gab leeres DataFrame zurück")
        return {}

    # Indikatoren pro Ticker berechnen
    yf_to_ind: dict[str, dict] = {}
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                # Einzelner Ticker: raw hat flache Columns (Close, High, …)
                df = raw.dropna(how="all")
            else:
                # Mehrere Ticker: raw hat MultiIndex (metric, ticker)
                if ticker not in raw.columns.get_level_values(0):
                    continue
                df = raw[ticker].dropna(how="all")
            if df.empty or len(df) < 20:
                continue
            ind = compute_indicators(df)
            if ind:
                yf_to_ind[ticker] = ind
        except Exception as e:
            print(f"[position_review] Indikator-Fehler {ticker}: {e}")

    # instrument_id → Indikatoren
    result: dict[int, dict] = {}
    for iid, yf_sym in id_to_yf.items():
        if yf_sym in yf_to_ind:
            result[iid] = yf_to_ind[yf_sym]

    live_count = len(result)
    print(f"[position_review] Live-Indikatoren: {live_count}/{len(positions)} Positionen aktualisiert")
    return result


def _save_and_notify(evaluations: list[dict], summary: str, positions: list[dict] | None = None) -> None:
    """Speichert Recommendations und sendet Discord-Alert für TIGHTEN/EXIT."""
    now_iso = datetime.now(timezone.utc).isoformat()[:19]
    # Position-Lookup für instrument_id + api_position_id
    pos_by_symbol: dict[str, dict] = {p["symbol"]: p for p in (positions or [])}
    stamped = []
    for ev in evaluations:
        if not isinstance(ev, dict) or not ev.get("symbol"):
            continue
        pos = pos_by_symbol.get(ev["symbol"], {})
        stamped.append({
            **ev,
            "ts": now_iso,
            "instrument_id": pos.get("instrument_id"),
            "position_id": pos.get("api_position_id"),
            "executed": False,
            "executed_at": None,
        })

    # Speichern (überschreibt vorherige — immer aktuelle Sicht)
    RECS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECS_PATH.write_text(json.dumps(stamped, indent=2, ensure_ascii=False))
    print(f"[position_review] {len(stamped)} Recommendations gespeichert:")

    tighten = [e for e in stamped if e.get("recommendation") == "TIGHTEN"]
    exits = [e for e in stamped if e.get("recommendation") == "EXIT"]
    holds = [e for e in stamped if e.get("recommendation") == "HOLD"]

    for e in stamped:
        rec = e.get("recommendation", "?")
        print(f"  {e['symbol']}: {rec} — {(e.get('reason') or '')[:80]}")

    # Discord-Alert für actionable Empfehlungen
    if exits or tighten:
        lines = []
        if exits:
            lines.append("**EXIT empfohlen:**")
            for e in exits:
                lines.append(f"• {e['symbol']}: {e.get('reason','')[:100]}")
        if tighten:
            lines.append("**TIGHTEN (Gewinnschutz enger):**")
            for e in tighten:
                lines.append(f"• {e['symbol']}: {e.get('reason','')[:100]}")
        if summary:
            _sum = summary[:1500] + ("…" if len(summary) > 1500 else "")
            lines.append(f"\n*{_sum}*")

        _discord(
            "post_alert_embed",
            title=f"🤖 LLM Position Review — {len(exits)} EXIT / {len(tighten)} TIGHTEN / {len(holds)} HOLD",
            description="\n".join(lines),
            severity="WARNING" if exits else "INFO",
        )
    else:
        print(f"[position_review] Alle {len(holds)} Positionen: HOLD — kein Alert")
        _discord(
            "post_alert_embed",
            title=f"✅ LLM Position Review — {len(holds)} x HOLD",
            description=(
                f"{len(holds)} offene Positionen bewertet — keine dringende Aktion.\n"
                + (f"*{summary[:2000]}{'…' if len(summary) > 2000 else ''}*" if summary else "")
            ),
            severity="INFO",
        )


def main() -> int:
    from bot.core.worker_lock import worker_lock
    from bot.config import load_config

    with worker_lock("position_review_worker") as acquired:
        if not acquired:
            print("[position_review] SKIPPED (bereits aktiv)")
            return 0

        t0 = time.time()
        _load_env()
        cfg = load_config()
        db_path = PROJECT_ROOT / cfg.db.path if hasattr(cfg, "db") else PROJECT_ROOT / "data" / "trading.db"

        print(f"[position_review] Sammle Positions-Daten...")
        data = _collect_data(db_path)

        if not data["positions"]:
            print("[position_review] Keine offenen Positionen — nichts zu evaluieren")
            return 0

        print(f"[position_review] {len(data['positions'])} Positionen, "
              f"{len(data['signal_perf'])} Signal-Typen mit History, "
              f"Regime={data['regime']}")

        # Prio 2b: Outcomes fuer ausgefuehrte Empfehlungen backfuellen
        outcomes = _backfill_outcomes(data["positions"], db_path)

        # Prio 3: Marktkontext holen (SPY/QQQ/VIX)
        print("[position_review] Hole Marktkontext (SPY/QQQ/VIX)...")
        market_ctx = _fetch_market_context()
        if market_ctx.get("spy_1d_pct") is not None:
            print(
                f"[position_review] Markt: SPY={market_ctx['spy_1d_pct']:+.1f}% "
                f"QQQ={market_ctx.get('qqq_1d_pct') or 'N/A'} "
                f"VIX={market_ctx.get('vix') or 'N/A'} [{market_ctx.get('vix_label', '?')}]"
            )

        # Live-Indikatoren via yfinance holen (überschreiben stale Signal-DB-Werte)
        live_ind = _fetch_live_indicators(data["positions"], db_path)
        for pos in data["positions"]:
            iid = pos.get("instrument_id")
            if iid and iid in live_ind:
                ind = live_ind[iid]
                pos["rsi"]        = ind.get("rsi",      pos.get("rsi"))
                pos["bb_pct"]     = ind.get("bb_pct",   pos.get("bb_pct"))
                pos["macd_hist"]  = ind.get("macd_hist",pos.get("macd_hist"))
                pos["sma20"]      = ind.get("sma20")
                pos["sma50"]      = ind.get("sma50")
                pos["vol_ratio"]  = ind.get("vol_ratio")
                pos["_live_price"]= ind.get("price")
                pos["_live"]      = True

        prompt = _build_prompt(data, market_ctx=market_ctx, recent_outcomes=outcomes)
        print(f"[position_review] Rufe LLM auf (Timeout {LLM_TIMEOUT_S}s)...")
        result = _call_llm(prompt)

        if not result:
            print("[position_review] LLM nicht verfügbar — keine Recommendations")
            return 1

        evaluations = result.get("evaluations", [])
        summary = result.get("summary", "")
        print(f"[position_review] LLM-Antwort: {len(evaluations)} Evaluierungen in {time.time()-t0:.1f}s")
        print(f"[position_review] Summary: {summary[:150]}")

        if evaluations:
            _save_and_notify(evaluations, summary, data['positions'])
        else:
            print("[position_review] LLM gab keine evaluations zurück")

        try:
            from bot.core.heartbeat import record_duration as _rd
            from bot.db.connection import DB as _DB_dur
            from bot.db.repo import StateRepo as _SR_dur
            _rd(_SR_dur(_DB_dur(db_path)), "position_review_worker", time.time() - t0)
        except Exception:
            pass

        return 0


if __name__ == "__main__":
    sys.exit(main())
