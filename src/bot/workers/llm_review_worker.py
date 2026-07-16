#!/usr/bin/env python3
"""LLM Review Worker — Tägliche Trading-Analyse & Autonome Ghost-Blacklist.

Läuft täglich um 20:00 UTC. Ruft den lokalen llama-server (Port 8080) auf um:
  1. Ghost-Order-Muster nach Exchange-Suffix zu erkennen
  2. Signal-Typ-Performance zu bewerten
  3. Anomalien zu identifizieren
  4. data/llm_ghost_blacklist.json autonom zu aktualisieren
  5. Erkenntnisse an docs/llm_insights.md anzuhängen
  6. Discord-Embed zu posten

Die LLM-Entscheidungen wirken direkt auf signal_worker.py zurück —
kein Mensch nötig in diesem Loop.

Schedule: 0 20 * * *
"""
from __future__ import annotations

import json
import json as _json_mod
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

GHOST_BLACKLIST_PATH = PROJECT_ROOT / "data" / "llm_ghost_blacklist.json"
INSIGHTS_PATH = PROJECT_ROOT / "docs" / "llm_insights.md"
LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
LLM_TIMEOUT_S = 85  # < 120s cron budget
ANALYSIS_WINDOW_DAYS = 14
MIN_SAMPLES_FOR_BLACKLIST = 2   # mindestens 2 Trades für Exchange-Bewertung
GHOST_RATE_THRESHOLD = 0.6
SIGNAL_WEIGHTS_PATH = PROJECT_ROOT / "data" / "llm_signal_weights.json"
TRADING_MEMORY_PATH = PROJECT_ROOT / "data" / "llm_trading_memory.json"
SIGNAL_WEIGHTS_EXPIRY_DAYS = 14

# Trading Bible Hard Limits — unveraenderliche Konstanten im Quellcode.
# Die LLM kann config.yaml autonom aendern, aber NIEMALS diese Grenzen ueberschreiten.
# Kein Mensch noetig, aber auch keine LLM-Halluzination kann hier Schaden anrichten.
BIBLE_HARD_LIMITS: dict[str, tuple] = {
    # key                              min    max    type
    "sl.default_pct":                (0.5,  8.0,   float),
    "sl.emergency_pct":              (1.0,  12.0,  float),
    "sizing.very_high_pct":          (2.0,  20.0,  float),
    "sizing.high_pct":               (1.0,  18.0,  float),
    "sizing.medium_pct":             (1.0,  15.0,  float),
    "sizing.low_pct":                (0.5,  8.0,   float),
    "trading.max_positions":         (5,    40,    int),
    "trading.max_trades_per_day":    (2,    30,    int),
    "trading.max_fragments_per_instrument": (1, 5, int),
    "trading.cash_target_min_pct":   (5.0,  40.0,  float),
    "risk.daily_loss_limit_pct":     (2.0,  15.0,  float),
    "risk.weekly_loss_limit_pct":    (3.0,  20.0,  float),
    "risk.monthly_loss_limit_pct":   (5.0,  25.0,  float),
    "regime.caution_pct":            (1.0,  8.0,   float),
    "regime.defensive_pct":          (3.0,  15.0,  float),
    "regime.critical_pct":           (8.0,  25.0,  float),
    # fix/stale-exit (2026-07-15): LLM darf die Stale-Parameter nachjustieren
    # (z.B. nach MISSED_UPSIDE-Haeufung min_days erhoehen) — in harten Grenzen.
    "trailing.stale_exit.min_days":      (5,   30,   int),
    "trailing.stale_exit.pnl_band_pct":  (0.5, 3.0,  float),
    "trailing.stale_exit.min_peak_pct":  (1.0, 5.0,  float),
    "trading.deployment_boost":          (1.0, 1.5,  float),
}

STALE_OUTCOMES_PATH = PROJECT_ROOT / "data" / "stale_exit_outcomes.json"


def _evaluate_stale_exits(min_age_h: float = 72.0) -> int:
    """Lernschleife fix/stale-exit: bewertet Stale-Exits nach >=72h REIN
    yfinance-basiert (einheitsfest — eToro-Rate vs GBX bei .L vermeiden):
    Referenz = erster Close ab Exit-Tag, Vergleich = letzter Close.
    GOOD (<= +1.5% seither) / MISSED_UPSIDE (> +3%) / NEUTRAL dazwischen.
    Neue Grades landen als strategy_note im Trading-Memory und fliessen so
    in kuenftige LLM-Prompts (Parameter-Nachjustierung via BIBLE-Limits)."""
    import json as _js
    from datetime import datetime, timezone
    try:
        if not STALE_OUTCOMES_PATH.exists():
            return 0
        data = _js.loads(STALE_OUTCOMES_PATH.read_text()) or {}
        entries = data.get("entries", [])
        now = datetime.now(timezone.utc)
        pending = []
        for e in entries:
            if e.get("outcome") is not None or not e.get("yf_symbol"):
                continue
            try:
                ts = datetime.fromisoformat(e["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (now - ts).total_seconds() >= min_age_h * 3600:
                    pending.append((e, ts))
            except Exception:
                continue
        if not pending:
            return 0
        import yfinance as yf
        graded = 0
        notes: list[str] = []
        for e, ts in pending:
            try:
                hist = yf.Ticker(e["yf_symbol"]).history(period="1mo")["Close"].dropna()
                if hist.empty:
                    e["outcome"] = "NO_DATA"
                    graded += 1
                    continue
                exit_date = ts.date()
                ref = None
                for idx, val in hist.items():
                    if idx.date() >= exit_date:
                        ref = float(val)
                        break
                if ref is None or ref <= 0:
                    ref = float(hist.iloc[0])
                delta_pct = (float(hist.iloc[-1]) / ref - 1) * 100
                e["outcome"] = ("MISSED_UPSIDE" if delta_pct > 3.0
                                else "GOOD" if delta_pct <= 1.5 else "NEUTRAL")
                e["outcome_delta_pct"] = round(delta_pct, 2)
                graded += 1
                notes.append(f"Stale-Exit {e['symbol']}: {e['outcome']} "
                             f"({delta_pct:+.1f}% seit Exit)")
            except Exception:
                continue
        if graded:
            tmp = STALE_OUTCOMES_PATH.with_suffix(".json.tmp")
            tmp.write_text(_js.dumps(data, indent=1, ensure_ascii=False))
            tmp.replace(STALE_OUTCOMES_PATH)
            if notes:
                try:
                    mem = (_js.loads(TRADING_MEMORY_PATH.read_text())
                           if TRADING_MEMORY_PATH.exists() else {})
                    mem.setdefault("strategy_notes", []).extend(notes)
                    mem["strategy_notes"] = mem["strategy_notes"][-50:]
                    TRADING_MEMORY_PATH.write_text(
                        _js.dumps(mem, indent=2, ensure_ascii=False))
                except Exception:
                    pass
        return graded
    except Exception:
        return 0
CONFIG_YAML_PATH = PROJECT_ROOT / "config" / "config.yaml"
DECISION_LOG_PATH = PROJECT_ROOT / "data" / "llm_decision_log.json"   # Signal-Weights laenger gueltig als Ghost-Liste      # ab 60% Ghost-Rate → Exchange blockieren

# ── Discord Embeds ─────────────────────────────────────────────────────────────
try:
    sys.path.insert(0, str(PROJECT_ROOT / "src" / "bot"))
    import discord_embeds as _DE
except Exception:
    _DE = None


def _discord(fn: str, **kwargs) -> None:
    try:
        if _DE and hasattr(_DE, fn):
            getattr(_DE, fn)(**kwargs)
    except Exception as e:
        print(f"[llm_review] Discord post failed: {e}")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _collect_trade_performance(db_path: Path) -> dict:
    """Sammelt abgeschlossene Trades mit PnL fuer Lern-Analyse."""
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Abgeschlossene Trades mit PnL-Daten
    cur.execute("""
        SELECT t.symbol, t.status, t.pnl_usd, t.pnl_pct,
               t.entry_price, t.exit_price, t.created_at, t.closed_at,
               s.signal_type, s.conviction
        FROM trades t
        LEFT JOIN signals s ON t.signal_id = s.id
        WHERE t.status = 'CLOSED' AND t.closed_at IS NOT NULL
          AND t.entry_price IS NOT NULL
        ORDER BY t.closed_at DESC
        LIMIT 100
    """)
    trades_closed = [dict(r) for r in cur.fetchall()]

    # Signal-Performance-Aggregat aus allen gemessenen Trades
    cur.execute("""
        SELECT s.signal_type, s.conviction, t.status,
               COUNT(*) as n,
               AVG(t.pnl_pct) as avg_pnl,
               SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN t.pnl_pct <= 0 THEN 1 ELSE 0 END) as losses
        FROM trades t JOIN signals s ON t.signal_id = s.id
        WHERE t.pnl_pct IS NOT NULL
        GROUP BY s.signal_type, s.conviction, t.status
        ORDER BY n DESC
    """)
    signal_perf_with_pnl = [dict(r) for r in cur.fetchall()]

    con.close()
    return {"closed": trades_closed, "signal_perf_pnl": signal_perf_with_pnl}


def _load_trading_memory() -> dict:
    """Laedt persistentes Trading-Gedaechtnis (akkumuliert ueber Runs)."""
    try:
        if TRADING_MEMORY_PATH.exists():
            data = _json_mod.loads(TRADING_MEMORY_PATH.read_text())
            # Sicherstellen dass alle benoetigten Keys vorhanden (Compat mit Self-Improvement Cycle)
            data.setdefault("runs", [])
            data.setdefault("signal_insights", {})
            data.setdefault("strategy_notes", [])
            return data
    except Exception:
        pass
    return {"runs": [], "signal_insights": {}, "strategy_notes": []}


def _collect_data(db_path: Path) -> dict:
    """Liest alle relevanten Daten aus der DB (read-only)."""
    import sqlite3
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    since = (datetime.now(timezone.utc) - timedelta(days=ANALYSIS_WINDOW_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # Trades im Fenster — inkl. is_tradable fuer bereingte Ghost-Raten
    cur.execute(
        "SELECT t.symbol, t.status, t.rejection_reason, t.created_at, "
        "COALESCE(i.is_tradable, 1) AS is_tradable "
        "FROM trades t LEFT JOIN instruments i ON t.instrument_id = i.instrument_id "
        "WHERE t.created_at > ? ORDER BY t.created_at DESC",
        (since,),
    )
    trades = [dict(r) for r in cur.fetchall()]

    # Signal-Typen (historisch, alle Zeit)
    cur.execute(
        "SELECT s.signal_type, s.conviction, t.status, COUNT(*) as n "
        "FROM trades t JOIN signals s ON t.signal_id = s.id "
        "WHERE t.created_at > ? "
        "GROUP BY s.signal_type, s.conviction, t.status",
        (since,),
    )
    signal_stats = [dict(r) for r in cur.fetchall()]

    # System-State
    cur.execute("SELECT key, value FROM system_state WHERE key IN "
                "('CURRENT_EQUITY','PEAK_EQUITY','CURRENT_DRAWDOWN_PCT','CURRENT_REGIME')")
    state = {r["key"]: r["value"] for r in cur.fetchall()}

    # Aktive Positionen
    cur.execute("SELECT symbol, amount_usd FROM portfolio_snapshot ORDER BY amount_usd DESC")
    positions = [dict(r) for r in cur.fetchall()]

    # Ghost-Failures pro Signal-Typ (separat — Exchange-Infra != Strategy-Qualitaet)
    cur.execute(
        "SELECT s.signal_type, COUNT(*) as n "
        "FROM trades t JOIN signals s ON t.signal_id = s.id "
        "WHERE t.created_at > ? AND t.rejection_reason LIKE '%Ghost order%' "
        "GROUP BY s.signal_type",
        (since,),
    )
    ghost_signal_stats = {r["signal_type"]: r["n"] for r in cur.fetchall()}

    # Slippage-Rejects (Prio 7)
    try:
        since_slip = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            SELECT symbol, COUNT(*) as n FROM slippage_rejects
            WHERE rejected_at > ? GROUP BY symbol ORDER BY n DESC LIMIT 10
        """, (since_slip,))
        slippage_top = {r["symbol"]: r["n"] for r in cur.fetchall()}
    except Exception:
        slippage_top = {}

    con.close()
    return {"trades": trades, "signal_stats": signal_stats, "state": state,
            "positions": positions, "ghost_signal_stats": ghost_signal_stats,
            "slippage_top": slippage_top}


# ── Analytik ──────────────────────────────────────────────────────────────────

def _exchange_suffix(symbol: str) -> str:
    """Extrahiert den Exchange-Suffix (z.B. '.L', '.HE') oder '_FOREX'/'_FUT'/'_CRYPTO'."""
    # Crypto: Symbole mit Bindestrich (BTC-USD, ETH-USD, DOT-USD)
    if "-" in symbol:
        return "_CRYPTO"
    # yfinance Forex-Format: AUDNZD=X, EURUSD=X
    if symbol.endswith("=X"):
        return "_FOREX"
    # Asiatische Boersen: Symbole mit fuehrender Ziffer (7203.T=Toyota, 005930.KS=Samsung)
    if symbol and symbol[0].isdigit():
        return "_ASIA"
    # HK-Lot-Symbole: BABA_2402, HSBC_0005 — Unterstrich gefolgt von rein numerischem Suffix
    if "_" in symbol:
        _parts = symbol.rsplit("_", 1)
        if len(_parts) == 2 and _parts[1].isdigit():
            return "_ASIA"
    # Futures: explizit .FUT Endung
    if symbol.endswith(".FUT"):
        return "_FUT"
    # Forex: kein Punkt, kurz, uppercase, endet auf Waehrung
    if "." not in symbol and len(symbol) <= 7 and symbol.isupper():
        if "/" in symbol or symbol.endswith("JPY") or symbol.endswith("GBP") or symbol.endswith("USD") or symbol.endswith("CHF") or symbol.endswith("EUR") or symbol.endswith("AUD") or symbol.endswith("CAD"):
            return "_FOREX"
    # Dot-Suffix (.L, .DE, .HE, .PA etc.)
    if "." in symbol:
        return "." + symbol.rsplit(".", 1)[-1]
    return "_OTHER"


def _compute_ghost_rates(trades: list[dict]) -> dict:
    """Berechnet Ghost-Rate nach Exchange-Suffix.
    Nur tradable Instrumente (is_tradable != 0) werden gezaehlt —
    non-tradable Instrumente sind bereits durch den is_tradable-Filter
    aus dem Trade-Flow entfernt und wuerden die Raten kuenstlich erhoehen.
    """
    stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "ghost": 0, "symbols": set()})
    for t in trades:
        # is_tradable=0: Instrument per API als nicht-handelbar markiert — bereits
        # aus signal/data/discovery Worker gefiltert. Ghost-History nicht einrechnen.
        if t.get("is_tradable") == 0:
            continue
        suffix = _exchange_suffix(t["symbol"])
        stats[suffix]["total"] += 1
        stats[suffix]["symbols"].add(t["symbol"])
        if "Ghost order" in (t.get("rejection_reason") or ""):
            stats[suffix]["ghost"] += 1
    return {
        k: {
            "total": v["total"],
            "ghost": v["ghost"],
            "rate": round(v["ghost"] / v["total"], 2) if v["total"] else 0,
            "examples": list(v["symbols"])[:5],
        }
        for k, v in stats.items()
        if v["total"] >= MIN_SAMPLES_FOR_BLACKLIST
    }


def _compute_signal_perf(signal_stats: list[dict], ghost_signal_stats: dict | None = None) -> dict:
    """Aggregiert Signal-Performance (Erfolgsrate nach Typ).
    Ghost-Order-Failures werden SEPARAT gezaehlt: sie spiegeln Exchange-Infrastruktur,
    nicht Strategie-Qualitaet. success_rate basiert nur auf echten Executions."""
    perf: dict[str, dict] = defaultdict(
        lambda: {"ACTIVE": 0, "CLOSED": 0, "FAILED": 0, "REJECTED": 0, "GHOST_FAILED": 0}
    )
    ghost_counts = ghost_signal_stats or {}
    for row in signal_stats:
        stype = (row.get("signal_type") or "UNKNOWN")
        status = row.get("status", "UNKNOWN")
        n = int(row.get("n", 0))
        if status in perf[stype]:
            perf[stype][status] += n
    # Ghost-Failures umbuchen: FAILED -> GHOST_FAILED
    for stype, ghost_n in ghost_counts.items():
        if stype in perf:
            actual = min(ghost_n, perf[stype]["FAILED"])
            perf[stype]["FAILED"] -= actual
            perf[stype]["GHOST_FAILED"] += actual
    result = {}
    for stype, counts in perf.items():
        real_total = counts["ACTIVE"] + counts["CLOSED"] + counts["FAILED"] + counts["REJECTED"]
        ghost_n = counts["GHOST_FAILED"]
        if real_total + ghost_n >= 2:
            success = counts["ACTIVE"] + counts["CLOSED"]
            result[stype] = {
                "total": real_total,
                "ghost_failed": ghost_n,
                "success_rate": round(success / real_total, 2) if real_total > 0 else 0.0,
                "ACTIVE": counts["ACTIVE"],
                "CLOSED": counts["CLOSED"],
                "FAILED": counts["FAILED"],
                "REJECTED": counts["REJECTED"],
            }
    return result


# ── LLM Call ─────────────────────────────────────────────────────────────────

def _call_llm(*args, **kwargs) -> dict | None:
    """Retry-Wrapper um _call_llm_once (fix/llm-fast-retry).

    llama-server ist bei Modell-Reload/Neustart kurz weg (connection refused,
    Fehler in <10s). Solche Schnell-Fails werden bis zu 2x mit 15s/30s Pause
    wiederholt — passt ins 120s-Cron-Budget. Ein voller Timeout
    (LLM_TIMEOUT_S=85s) wird NICHT wiederholt: das wuerde das Budget sprengen;
    dafuer gibt es den Watchdog-Re-Run (etoro_kill_switch_watchdog.sh, Stufe 2).
    """
    for _attempt in range(3):
        _t0 = time.monotonic()
        result = _call_llm_once(*args, **kwargs)
        if result is not None:
            return result
        _elapsed = time.monotonic() - _t0
        if _elapsed > 10.0 or _attempt >= 2:
            return None
        _wait = (15, 30)[min(_attempt, 1)]
        print(f"[llm_review] LLM-Schnell-Fail nach {_elapsed:.1f}s — Retry in {_wait}s")
        time.sleep(_wait)
    return None


def _call_llm_once(ghost_rates: dict, signal_perf: dict, state: dict, positions: list,
              trade_perf: dict | None = None, trading_memory: dict | None = None,
              slippage_top: dict | None = None, ghost_trends: dict | None = None,
              non_tradable_ghost_count: int = 0) -> dict | None:
    """Ruft llama-server auf und parst JSON-Antwort."""
    equity = state.get("CURRENT_EQUITY", "?")
    drawdown = state.get("CURRENT_DRAWDOWN_PCT", "?")
    regime = state.get("CURRENT_REGIME", "?")

    # Nur auffällige Exchanges in den Prompt
    notable_ghosts = {
        k: v for k, v in ghost_rates.items() if v["rate"] >= 0.3
    }
    # Pre-compute JSON strings to avoid {{}} anti-pattern inside f-string expressions
    # ({{}} inside {expr} creates a set containing {}, which is unhashable)
    _ghost_trends_str = json.dumps(ghost_trends if ghost_trends else {}, ensure_ascii=False)
    _slippage_str = json.dumps(slippage_top if slippage_top else {}, ensure_ascii=False)

    prompt = f"""/no_think
Du bist Trading-Analyst für einen autonomen eToro-Bot. Analysiere die Daten und gib NUR valides JSON zurück.

## Architektur-Kontext (wichtig für korrekte Interpretation)
- is_tradable-Filter: {non_tradable_ghost_count} historische Ghost-Orders stammen aus Instrumenten,
  die per eToro-API als nicht-handelbar markiert wurden (is_tradable=0). Diese werden
  NICHT mehr getradet — aus den Ghost-Raten unten herausgefiltert.
- Markt-Timing → DEFER: Orders bei geschlossenem Markt werden NICHT mehr als FAILED
  gebucht. Sie bleiben APPROVED und werden alle 15min wiederholt (allowEntryOrders
  von eToros Eligibility-API ist jetzt das Live-Gate). Solche Fehler tauchen NICHT
  mehr in Ghost-Statistiken auf.
- Ghost-Blacklist-Fokus: Exchanges nur blocken, wenn tradable Instrumente strukturell
  von eToro abgelehnt werden — NICHT fuer reine Marktzeiten-Probleme.
- Strukturelle Exchange-Bloecke (eToro unterstützt keinen Handel dort):
  _FOREX=100% hist., _FUT=100% hist., .HE=100% hist., .ST=75% hist.
  Diese Bloecke bleiben PERMANENT aktiv — senke sie NICHT bei 0%-Rate nach Daten-Reset.
  .DE ist KEINE strukturelle Sperre — viele .DE Aktien sind auf eToro handelbar.

## Ghost-Order-Raten nach Exchange (letzte {ANALYSIS_WINDOW_DAYS} Tage, nur tradable Instrumente)
{json.dumps(notable_ghosts, ensure_ascii=False)}

## Signal-Performance nach Typ
{json.dumps(signal_perf, ensure_ascii=False)}

## Aktueller Portfoliostatus
Regime: {regime} | Equity: {equity} | Drawdown: {drawdown}%
Positionen: {json.dumps([p['symbol'] for p in positions[:10]])}
Regime-Hinweis: Im {regime}-Regime gelten verschaerfte Conviction-Filter. Weights anpassen.

## Ghost-Rate Trends (STEIGEND/FALLEND vs. 7-Tage-Avg)
{_ghost_trends_str}

## Symbole mit Slippage-Rejects (letzte 30 Tage)
{_slippage_str}

## Aufgabe
Gib JSON mit genau diesen Feldern zurück:
{{
  "ghost_exchanges": ["Liste von Exchange-Suffixen mit >{int(GHOST_RATE_THRESHOLD*100)}% Ghost-Rate, z.B. [\".L\", \".HE\"]"],
  "ghost_symbols": ["Spezifische Symbol-Muster die ghost-anfällig sind, z.B. EURJPY"],
  "underperforming_signals": ["Signal-Typen mit success_rate < 0.3 und mind. 3 Trades"],
  "regime_ok": true,
  "anomalies": ["Auffällige Muster als kurze Strings, leer wenn keine"],
  "discord_summary": "2-3 Sätze Zusammenfassung auf Deutsch für Discord"
}}"""

    # Learning-Kontext: Signal-Performance mit PnL
    perf_summary = ""
    if trade_perf and trade_perf.get("signal_perf_pnl"):
        perf_summary = json.dumps(trade_perf["signal_perf_pnl"][:20], ensure_ascii=False)

    prev_insights = ""
    if trading_memory and trading_memory.get("strategy_notes"):
        prev_insights = " | ".join(trading_memory["strategy_notes"][-3:])

    # Kuerzlich abgeschlossene Trades (mit PnL)
    recent_closed = ""
    if trade_perf and trade_perf.get("closed"):
        measured = [t for t in trade_perf["closed"] if t.get("pnl_pct") is not None][:15]
        recent_closed = json.dumps([
            {"sym": t["symbol"], "sig": (t.get("signal_type") or "")[:40],
             "conv": t.get("conviction"), "pnl": round(t["pnl_pct"], 2)}
            for t in measured
        ], ensure_ascii=False)

    # Aktuelle Config-Werte fuer LLM-Kontext laden
    import yaml
    try:
        raw_cfg = yaml.safe_load(CONFIG_YAML_PATH.read_text())
        current_cfg = {
            "sl.default_pct": raw_cfg.get("sl", {}).get("default_pct"),
            "sl.emergency_pct": raw_cfg.get("sl", {}).get("emergency_pct"),
            "sizing.very_high_pct": raw_cfg.get("sizing", {}).get("very_high_pct"),
            "sizing.high_pct": raw_cfg.get("sizing", {}).get("high_pct"),
            "sizing.medium_pct": raw_cfg.get("sizing", {}).get("medium_pct"),
            "trading.max_positions": raw_cfg.get("trading", {}).get("max_positions"),
            "trading.max_trades_per_day": raw_cfg.get("trading", {}).get("max_trades_per_day"),
            "risk.daily_loss_limit_pct": raw_cfg.get("risk", {}).get("daily_loss_limit_pct"),
            "regime.caution_pct": raw_cfg.get("regime", {}).get("caution_pct"),
            "regime.defensive_pct": raw_cfg.get("regime", {}).get("defensive_pct"),
        }
        limits_summary = {k: {"min": v[0], "max": v[1]} for k, v in BIBLE_HARD_LIMITS.items()}
    except Exception:
        current_cfg, limits_summary = {}, {}

    prompt += f"""

## Aktuelle Trading-Konfiguration
{json.dumps(current_cfg, ensure_ascii=False)}

## Bible Hard Limits (darf die LLM NIEMALS ueberschreiten)
{json.dumps(limits_summary, ensure_ascii=False)}

WICHTIG: Die Conviction-Leiter muss MONOTON bleiben (very_high_pct >=
high_pct >= medium_pct >= low_pct) — invertierende Aenderungen werden
verworfen. Wenn ein Signal-TYP schlecht performt, daempfe ihn ueber
signal_weight_updates (score_multiplier/skip), NICHT ueber die Sizing-Leiter.

Ergaenze das JSON AUCH um:
{{
  "config_adjustments": {{
    "sl.default_pct": {{"value": 2.5, "reason": "Begruendung"}},
    ... (nur Werte die sich wirklich verbessern wuerden, leer lassen wenn keine Aenderung noetig)
  }}
}}"""

    if perf_summary:
        prompt += f"""

## Signal-Performance mit PnL-Daten
{perf_summary}

## Kuerzlich abgeschlossene Trades (sym, signal, conviction, pnl%)
{recent_closed or 'Keine PnL-Daten verfuegbar'}

## Bisherige LLM-Erkenntnisse (Langzeitgedaechtnis)
{prev_insights or 'Erster Run — kein Vorwissen'}

Ergaenze das JSON um folgende zusaetzliche Felder:
{{
  ...ghost/signal/regime Felder wie oben...,
  "signal_weight_adjustments": {{
    "SIGNAL_TYP": {{"score_multiplier": 0.5, "skip": false, "reason": "kurze Begruendung"}}
  }},
  "new_strategy_notes": ["Konkrete Strategie-Erkenntnis 1", "Erkenntnis 2"],
  "conviction_issues": ["z.B. VERY_HIGH Conviction hat 80% Verlustrate — ueberpruefen"]
}}"""

    payload = json.dumps({
        "model": "local",
        "messages": [
            {"role": "system", "content": "Du bist ein JSON-API fuer Trading-Analyse. Antworte AUSSCHLIESSLICH mit validem JSON ohne jede weitere Erklaerung."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2048,  # 1024 war zu klein — JSON-Response ~2300 chars
        "temperature": 0.1,
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
            content = msg.get("content", "") or ""
            # Fallback: reasoning_content wenn content leer (Qwen3 --reasoning on)
            if not content.strip():
                content = msg.get("reasoning_content", "") or ""
            # Extrahiere JSON
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                raw = content[start:end]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    # Truncated response: close any open object at last complete comma
                    last_comma = raw.rfind(",")
                    if last_comma > 0:
                        try:
                            recovered = raw[:last_comma] + "}"
                            result = json.loads(recovered)
                            print("[llm_review] Truncated JSON recovered (last-comma strategy)")
                            return result
                        except json.JSONDecodeError:
                            pass
                    print(f"[llm_review] JSON-Parse-Fehler (unrecoverable), raw[:200]={raw[:200]!r}")
            else:
                print(f"[llm_review] Kein JSON in Antwort (len={len(content)}): {content[:120]!r}")
    except urllib.error.URLError as e:
        print(f"[llm_review] LLM nicht erreichbar: {e}")
    except Exception as e:
        print(f"[llm_review] LLM-Fehler: {e}")
    return None


# ── Blacklist schreiben ───────────────────────────────────────────────────────

def _load_decision_log() -> list:
    """Laedt das persistente Decision-Log aller autonomen LLM-Entscheidungen."""
    try:
        if DECISION_LOG_PATH.exists():
            return _json_mod.loads(DECISION_LOG_PATH.read_text())
    except Exception:
        pass
    return []


def _record_decision(decision_type: str, key: str, old_value, new_value, reason: str,
                     baseline: dict | None = None) -> None:
    """Protokolliert eine autonome LLM-Entscheidung fuer spaeteren Outcome-Vergleich."""
    log = _load_decision_log()
    log.append({
        "date": datetime.now(timezone.utc).isoformat()[:10],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": decision_type,           # "config", "signal_weight", "ghost_blacklist"
        "key": key,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
        "baseline": baseline,            # Metriken VOR der Entscheidung
        "outcome": None,                 # Wird beim naechsten Run befuellt
        "outcome_grade": None,           # "IMPROVED" | "WORSE" | "NEUTRAL"
        "outcome_date": None,
    })
    log = log[-200:]  # max 200 Entscheidungen
    DECISION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DECISION_LOG_PATH.write_text(_json_mod.dumps(log, indent=2, ensure_ascii=False))


def _evaluate_past_decisions(trades: list, log: list) -> tuple[list, list]:
    """Bewertet vergangene Entscheidungen anhand aktueller Trade-Daten.
    Gibt (log_mit_outcomes, erkenntnisse) zurueck."""
    if not log:
        return log, []

    insights = []
    today = datetime.now(timezone.utc).date()

    for entry in log:
        if entry.get("outcome") is not None:
            continue  # Bereits bewertet

        try:
            decision_date = datetime.fromisoformat(entry["timestamp"]).date()
        except Exception:
            continue

        # Nur Entscheidungen die mind. 1 Tag alt sind bewerten
        if (today - decision_date).days < 1:
            continue

        dtype = entry.get("type", "")
        key = entry.get("key", "")
        new_val = entry.get("new_value")

        # Trades die NACH der Entscheidung entstanden sind
        post_trades = [
            t for t in trades
            if t.get("created_at", "") > entry["timestamp"][:10]
               and t.get("pnl_pct") is not None
        ]

        # Mind. 3 echte Trades fuer aussagekraeftige Auswertung
        if len(post_trades) < 3:
            continue

        avg_pnl = sum(t["pnl_pct"] for t in post_trades) / len(post_trades)
        win_rate = sum(1 for t in post_trades if t.get("pnl_pct", 0) > 0) / len(post_trades)

        if dtype == "config":
            outcome = (
                f"Nach {key}={new_val}: avg_pnl={avg_pnl:.2f}%, "
                f"win_rate={win_rate:.0%} ({len(post_trades)} Trades)"
            )
        elif dtype == "signal_weight":
            # Signal-spezifische PnL (nicht portfolio-weit)
            sig_trades = [t for t in post_trades if key in (t.get("signal_type") or "")]
            if sig_trades:
                sig_avg = sum(t["pnl_pct"] for t in sig_trades) / len(sig_trades)
                outcome = f"Signal {key[:40]}: {len(sig_trades)} Trades, avg_pnl={sig_avg:.2f}% (Gewicht={new_val})"
            else:
                outcome = f"Signal {key[:40]}: 0 Trades nach Gewichts-Aenderung — Signal nicht mehr gefeuert"
        elif dtype == "ghost_blacklist":
            ghost_after = sum(1 for t in post_trades if "Ghost order" in (t.get("rejection_reason") or ""))
            outcome = f"Ghost-Blacklist '{key}': {ghost_after} Ghost-Failures nach Einfuehrung (erwartet: 0)"
        else:
            outcome = f"avg_pnl={avg_pnl:.2f}%, win_rate={win_rate:.0%} ({len(post_trades)} Trades)"

        entry["outcome"] = outcome
        entry["outcome_date"] = today.isoformat()
        # Outcome-Grade aus Baseline-Vergleich (Prio 2b)
        if entry.get("baseline") and isinstance(entry["baseline"], dict):
            base_wr = entry["baseline"].get("win_rate", 0)
            base_pnl = entry["baseline"].get("avg_pnl_pct", 0)
            if win_rate > base_wr + 0.03 or avg_pnl > base_pnl + 0.1:
                entry["outcome_grade"] = "IMPROVED"
            elif win_rate < base_wr - 0.03 or avg_pnl < base_pnl - 0.1:
                entry["outcome_grade"] = "WORSE"
            else:
                entry["outcome_grade"] = "NEUTRAL"
        insights.append(f"{dtype}/{key}: {outcome}")

    return log, insights


_SIZING_LADDER = (
    "sizing.very_high_pct", "sizing.high_pct", "sizing.medium_pct", "sizing.low_pct",
)


def _read_sizing_ladder(content: str) -> dict:
    """Liest die aktuellen Werte der vier Conviction-Stufen aus config.yaml-Text."""
    import re as _re
    vals: dict = {}
    for key in _SIZING_LADDER:
        field = key.split(".", 1)[1]
        m = _re.search(rf"[ \t]+{_re.escape(field)}:[ \t]*([0-9]+(?:\.?[0-9]*)?)", content)
        vals[key] = float(m.group(1)) if m else None
    return vals


def _ladder_violation(key: str, new_value: float, current: dict) -> str | None:
    """Conviction-Leiter muss monoton bleiben: very_high >= high >= medium >= low.

    User-Entscheid 2026-07-14 — eine invertierte Leiter bedeutet, dass die
    staerkste Ueberzeugung die KLEINSTE Position bekommt. Die LLM hat das
    zweimal versucht (5->4 am 13.07., 8->6 am 15.07.); schwache Signal-TYPEN
    gehoeren in signal_weights gedaempft, nicht in die Leiter.
    Gibt Begruendung zurueck wenn verletzt, sonst None (fail-open bei
    unvollstaendig lesbaren Werten — dann greifen nur die Bible-Limits)."""
    if key not in _SIZING_LADDER:
        return None
    vals = dict(current)
    vals[key] = new_value
    order = [vals.get(k) for k in _SIZING_LADDER]
    if any(v is None for v in order):
        return None
    for a, b, ka, kb in zip(order, order[1:], _SIZING_LADDER, _SIZING_LADDER[1:]):
        if a < b:
            return f"Leiter invertiert: {ka}={a} < {kb}={b}"
    return None


def _validate_config_adjustment(key: str, value) -> tuple[bool, str, object]:
    """Prueft ob Config-Aenderung innerhalb der Bible Hard Limits liegt.
    Gibt (ok, reason, clamped_value) zurueck."""
    if key not in BIBLE_HARD_LIMITS:
        return False, f"{key} nicht in BIBLE_HARD_LIMITS — nicht aenderbar", value
    lo, hi, dtype = BIBLE_HARD_LIMITS[key]
    try:
        v = dtype(value)
    except (TypeError, ValueError):
        return False, f"Typfehler: {value} ist kein {dtype.__name__}", value
    if v < lo:
        return False, f"{key}={v} < Bible-Min {lo}", lo
    if v > hi:
        return False, f"{key}={v} > Bible-Max {hi}", hi
    return True, "OK", v


def _update_config_yaml(adjustments: dict, baseline: dict | None = None) -> list[str]:
    """Schreibt validierte Config-Aenderungen in config.yaml (Kommentare bleiben erhalten).
    Gibt Liste der angewendeten Aenderungen zurueck."""
    import re
    if not CONFIG_YAML_PATH.exists() or not adjustments:
        return []

    _config_baseline = baseline
    content = CONFIG_YAML_PATH.read_text(encoding="utf-8")
    applied = []
    skipped = []

    for key, adj in adjustments.items():
        value = adj.get("value")
        reason = adj.get("reason", "")
        ok, msg, validated = _validate_config_adjustment(key, value)
        if not ok:
            skipped.append(f"{key}: {msg}")
            continue

        # fix/sizing-ladder-guard: Cross-Parameter-Constraint (Monotonie)
        _lv = _ladder_violation(key, float(validated), _read_sizing_ladder(content))
        if _lv:
            skipped.append(
                f"{key}: {_lv} — Conviction-Leiter muss monoton bleiben "
                f"(User-Entscheid 2026-07-14); schwache Signal-Typen via "
                f"signal_weights daempfen, nicht via Leiter-Inversion"
            )
            continue

        # Dot-notation aufloesen: "sl.default_pct" -> section="sl", field="default_pct"
        parts = key.split(".", 1)
        if len(parts) != 2:
            skipped.append(f"{key}: Ungueltige Notation")
            continue
        field = parts[1]

        # Regex: findet "  field: NUMBER" (mit optionalem Kommentar) und ersetzt NUMBER
        pattern = rf"([ 	]+{re.escape(field)}:[ 	]*)([0-9]+(?:\.?[0-9]*)?)"
        val_str = str(int(validated)) if isinstance(validated, int) else str(float(validated))

        # Ist-Wert lesen — kein Update wenn identisch
        match = re.search(pattern, content)
        current_str = match.group(2) if match else None
        try:
            current_val = int(current_str) if isinstance(validated, int) else float(current_str)
        except (TypeError, ValueError):
            current_val = None
        if current_val is not None and current_val == validated:
            skipped.append(f"{key}: unveraendert ({validated}) — kein Update")
            continue

        # 24h-Cooldown: Config-Key nicht mehrfach am selben Tag aendern
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _cutoff = (_dt.now(_tz.utc) - _td(hours=24)).isoformat()
        _recent = [
            e for e in _load_decision_log()
            if e.get("type") == "config"
            and e.get("key") == key
            and (e.get("timestamp") or "") >= _cutoff
        ]
        if _recent:
            skipped.append(f"{key}: 24h-Cooldown aktiv (zuletzt geaendert {_recent[-1]['timestamp'][:16]})")
            continue

        new_content, n = re.subn(pattern, rf"\g<1>{val_str}", content, count=1)
        if n == 0:
            skipped.append(f"{key}: Pattern nicht gefunden in YAML")
            continue

        content = new_content
        applied.append(f"{key}: {current_val} -> {validated} ({reason[:60]})")
        _record_decision("config", key, current_val, validated, reason, baseline=_config_baseline)

    if applied:
        CONFIG_YAML_PATH.write_text(content, encoding="utf-8")
        print(f"[llm_review] Config-Updates: {len(applied)} angewendet, {len(skipped)} abgelehnt")
        for a in applied:
            print(f"  + {a}")
    for s in skipped:
        print(f"  SKIP {s}")
    return applied


def _update_ghost_blacklist(
    ghost_rates: dict, llm_analysis: dict | None
) -> dict:
    """
    Aktualisiert die Ghost-Blacklist autonom.
    Algorithmische Erkennung (>= GHOST_RATE_THRESHOLD) ist primär,
    LLM-Analyse ergänzt und erklärt.
    """
    # Algorithmisch erkannte Ghost-Exchanges (kein LLM nötig)
    auto_exchanges = [
        suffix for suffix, stats in ghost_rates.items()
        if stats["rate"] >= GHOST_RATE_THRESHOLD
    ]

    llm_exchanges = llm_analysis.get("ghost_exchanges", []) if llm_analysis else []
    llm_symbols = llm_analysis.get("ghost_symbols", []) if llm_analysis else []
    llm_reason = llm_analysis.get("discord_summary", "") if llm_analysis else ""

    # Vereinigung aus algorithmischer + LLM-Erkennung
    combined_exchanges = sorted(set(auto_exchanges) | set(llm_exchanges))

    # Preserve exchanges already hard-blocked in current file
    # (prevents unblocking after history resets where rate temporarily = 0%)
    try:
        _existing = json.loads(GHOST_BLACKLIST_PATH.read_text()) if GHOST_BLACKLIST_PATH.exists() else {}
        _prev_blocked = _existing.get("exchanges", [])
    except Exception:
        _prev_blocked = []
    combined_exchanges = sorted(set(combined_exchanges) | set(_prev_blocked))

    # Caution-Tier: 30-60% Ghost-Rate — sichtbar im Prompt/Discord, nicht geblockt
    CAUTION_THRESHOLD = 0.30
    caution_exchanges = sorted(
        suffix for suffix, stats in ghost_rates.items()
        if CAUTION_THRESHOLD <= stats["rate"] < GHOST_RATE_THRESHOLD
        and suffix not in combined_exchanges
    )

    blacklist = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "auto_expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "exchanges": combined_exchanges,
        "caution_exchanges": caution_exchanges,
        "symbols": sorted(set(llm_symbols)),
        "stats": {
            k: {"total": v["total"], "ghost": v["ghost"], "rate": v["rate"]}
            for k, v in ghost_rates.items()
        },
        "reason": llm_reason or f"Auto: {len(combined_exchanges)} geblockt, {len(caution_exchanges)} in Caution",
    }

    GHOST_BLACKLIST_PATH.write_text(json.dumps(blacklist, indent=2, ensure_ascii=False))
    print(f"[llm_review] Ghost-Blacklist aktualisiert: {combined_exchanges}")
    # Nur NEUE Exchanges loggen (nicht bei jedem Run die gleichen 5 wiederholen)
    try:
        _prev = json.loads(GHOST_BLACKLIST_PATH.read_text()) if GHOST_BLACKLIST_PATH.exists() else {}
        _prev_exchanges = set(_prev.get("exchanges", []))
    except Exception:
        _prev_exchanges = set()
    for ex in combined_exchanges:
        if ex not in _prev_exchanges:
            _record_decision("ghost_blacklist", ex, False, True, blacklist.get("reason", "")[:80])
    return blacklist


# ── Insights Log ─────────────────────────────────────────────────────────────

def _compute_ghost_trends(memory: dict, current_rates: dict) -> dict:
    """Vergleicht aktuelle Ghost-Rates mit 7-Tage-Durchschnitt aus History."""
    history = memory.get("ghost_rate_history", [])
    if len(history) < 3:
        return {}
    recent = history[-7:]
    trends = {}
    for suffix, stats in current_rates.items():
        rate = stats["rate"]
        past_vals = [h["rates"].get(suffix, 0) for h in recent[:-1]]
        if not past_vals:
            continue
        past_avg = sum(past_vals) / len(past_vals)
        delta = rate - past_avg
        trends[suffix] = {
            "trend": "STEIGEND" if delta > 0.05 else "FALLEND" if delta < -0.05 else "STABIL",
            "delta": round(delta, 2),
            "past_avg": round(past_avg, 2),
        }
    return trends


def _update_trading_memory(llm_analysis: dict, trade_perf: dict,
                           ghost_rates: dict | None = None) -> None:
    """Akkumuliert Lernerkenntnisse im persistenten Trading-Gedaechtnis."""
    memory = _load_trading_memory()
    now = datetime.now(timezone.utc).isoformat()

    # Neue Erkenntnisse anhaengen — semantisches Dedup: Note ueberspringen
    # wenn ein signifikantes Keyword (>5 Zeichen) schon in einer bestehenden Note vorkommt.
    new_notes = (llm_analysis or {}).get("new_strategy_notes", [])
    existing_notes_text = " ".join(memory["strategy_notes"]).lower()
    for note in new_notes:
        keywords = [w for w in note.lower().split() if len(w) > 5]
        is_dupe = any(kw in existing_notes_text for kw in keywords[:3])
        if not is_dupe:
            memory["strategy_notes"].append(note)
            existing_notes_text += " " + note.lower()
    memory["strategy_notes"] = memory["strategy_notes"][-15:]  # max 15 kompakte Erkenntnisse

    # Signal-Insights akkumulieren
    adjustments = (llm_analysis or {}).get("signal_weight_adjustments", {})
    for sig_type, adj in adjustments.items():
        if sig_type not in memory["signal_insights"]:
            memory["signal_insights"][sig_type] = []
        memory["signal_insights"][sig_type].append({
            "date": now[:10],
            "score_multiplier": adj.get("score_multiplier", 1.0),
            "skip": adj.get("skip", False),
            "reason": adj.get("reason", ""),
        })
        # max 10 Eintraege pro Signal-Typ
        memory["signal_insights"][sig_type] = memory["signal_insights"][sig_type][-10:]

    # Ghost-Rate-History fuer Trend-Tracking (Prio 3)
    if ghost_rates:
        memory.setdefault("ghost_rate_history", [])
        memory["ghost_rate_history"].append({
            "ts": now,  # voller ISO-Timestamp fuer intra-day Trend-Granularitaet
            "rates": {s: v["rate"] for s, v in ghost_rates.items()},
        })
        memory["ghost_rate_history"] = memory["ghost_rate_history"][-30:]

    # Run-Protokoll: update-in-place wenn gleicher Tag (kein Dedup-Bloat)
    _run_entry = {
        "date": now[:10],
        "closed_trades": len(trade_perf.get("closed", [])),
        "insights": len(new_notes),
        "adjustments": list(adjustments.keys()),
    }
    _today_idx = next(
        (i for i, r in enumerate(memory["runs"]) if r.get("date") == now[:10]), None
    )
    if _today_idx is not None:
        memory["runs"][_today_idx] = _run_entry
    else:
        memory["runs"].append(_run_entry)
    memory["runs"] = memory["runs"][-30:]  # max 30 Runs

    TRADING_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRADING_MEMORY_PATH.write_text(_json_mod.dumps(memory, indent=2, ensure_ascii=False))
    print(f"[llm_review] Trading-Memory aktualisiert: {len(new_notes)} neue Erkenntnisse")


def _update_signal_weights(llm_analysis: dict) -> None:
    """Schreibt LLM-Signal-Gewichtungen (autonome Anpassung des Scorings)."""
    adjustments = (llm_analysis or {}).get("signal_weight_adjustments", {})
    if not adjustments:
        print("[llm_review] Keine Signal-Gewichtungsaenderungen")
        return

    weights = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "auto_expires_at": (datetime.now(timezone.utc) + timedelta(days=SIGNAL_WEIGHTS_EXPIRY_DAYS)).isoformat(),
        "adjustments": adjustments,
        "strategy_notes": (llm_analysis or {}).get("new_strategy_notes", []),
        "conviction_issues": (llm_analysis or {}).get("conviction_issues", []),
    }
    SIGNAL_WEIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_WEIGHTS_PATH.write_text(_json_mod.dumps(weights, indent=2, ensure_ascii=False))
    for sig, adj in adjustments.items():
        _record_decision("signal_weight", sig, 1.0, adj, adj.get("reason", "")[:80])

    skipped = [k for k, v in adjustments.items() if v.get("skip")]
    demoted = [k for k, v in adjustments.items() if v.get("score_multiplier", 1.0) < 1.0 and not v.get("skip")]
    print(f"[llm_review] Signal-Weights: {len(skipped)} gesperrt, {len(demoted)} abgewertet")


def _append_insights(blacklist: dict, llm_analysis: dict | None, ghost_rates: dict) -> None:
    """Hängt Tages-Erkenntnisse an docs/llm_insights.md an."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"\n## {now}\n"]

    lines.append("**Ghost-Raten (≥2 Trades):**")
    for suffix, stats in sorted(ghost_rates.items(), key=lambda x: -x[1]["rate"]):
        if suffix in blacklist["exchanges"]:
            flag = " ← GEBLOCKT"
        elif suffix in blacklist.get("caution_exchanges", []):
            flag = " ⚠ CAUTION"
        else:
            flag = ""
        lines.append(f"- `{suffix}`: {int(stats['rate']*100)}% ({stats['ghost']}/{stats['total']}){flag}")

    if blacklist["symbols"]:
        lines.append(f"\n**Symbol-Blacklist:** {', '.join(blacklist['symbols'])}")

    if llm_analysis:
        if llm_analysis.get("underperforming_signals"):
            lines.append(f"\n**Schwache Signaltypen:** {', '.join(llm_analysis['underperforming_signals'])}")
        if llm_analysis.get("anomalies"):
            lines.append("\n**Anomalien:**")
            for a in llm_analysis["anomalies"]:
                lines.append(f"- {a}")
        if llm_analysis.get("discord_summary"):
            lines.append(f"\n**LLM-Fazit:** {llm_analysis['discord_summary']}")
    else:
        lines.append("\n*LLM nicht verfügbar — nur algorithmische Analyse.*")

    INSIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(INSIGHTS_PATH, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[llm_review] Insights geschrieben → {INSIGHTS_PATH}")


# ── Discord ───────────────────────────────────────────────────────────────────

def _post_discord(blacklist: dict, llm_analysis: dict | None, ghost_rates: dict,
                  trade_count: int, ghost_count: int) -> None:
    ghost_pct = int(ghost_count / trade_count * 100) if trade_count else 0
    blocked = blacklist["exchanges"] + blacklist["symbols"]
    blocked_str = ", ".join(f"`{b}`" for b in blocked[:8]) or "keine"

    summary = (llm_analysis or {}).get("discord_summary", "Keine LLM-Analyse verfügbar.")

    description = (
        f"**Analysefenster:** letzte {ANALYSIS_WINDOW_DAYS} Tage\n"
        f"**Trades:** {trade_count} | Ghost-Failures: {ghost_count} ({ghost_pct}%)\n"
        f"**Geblockte Exchanges/Symbole:** {blocked_str}\n\n"
        f"{summary}"
    )

    anomalies = (llm_analysis or {}).get("anomalies", [])
    if anomalies:
        description += "\n\n**Anomalien:**\n" + "\n".join(f"• {a}" for a in anomalies[:3])

    severity = "CRITICAL" if ghost_pct >= 50 else "WARNING" if ghost_pct >= 30 else "INFO"
    _discord(
        "post_alert_embed",
        title=f"{'🔴' if severity == 'CRITICAL' else '🟡' if severity == 'WARNING' else '🟢'} "
              f"LLM Daily Review — {ghost_pct}% Ghost-Rate",
        description=description,
        severity=severity,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    from bot.config import load_config
    from bot.core.worker_lock import worker_lock

    with worker_lock("llm_review_worker") as acquired:
        if not acquired:
            print("[llm_review] SKIPPED (bereits aktiv)")
            return 0

        t_start = time.time()
        cfg = load_config()
        db_path = PROJECT_ROOT / cfg.db.path if hasattr(cfg, "db") else PROJECT_ROOT / "data" / "trading.db"

        print(f"[llm_review] Starte Datensammlung ({ANALYSIS_WINDOW_DAYS}-Tage-Fenster)...")
        trading_memory = _load_trading_memory()
        trade_perf = _collect_trade_performance(db_path)

        # Vergangene Entscheidungen gegen aktuelle Daten bewerten
        decision_log = _load_decision_log()
        decision_log, decision_insights = _evaluate_past_decisions(
            trade_perf["closed"], decision_log
        )
        if decision_insights:
            print(f"[llm_review] {len(decision_insights)} vergangene Entscheidungen bewertet:")
            for ins in decision_insights:
                print(f"  > {ins}")
            # Aktualisiertes Log zurueckschreiben
            DECISION_LOG_PATH.write_text(_json_mod.dumps(decision_log, indent=2, ensure_ascii=False))
            # Erkenntnisse in Trading-Memory uebernehmen
            mem = _load_trading_memory()
            mem.setdefault("decision_outcomes", []).extend(decision_insights)
            mem["decision_outcomes"] = mem["decision_outcomes"][-100:]
            TRADING_MEMORY_PATH.write_text(_json_mod.dumps(mem, indent=2, ensure_ascii=False))
        trades_with_pnl = [t for t in trade_perf["closed"] if t.get("pnl_pct") is not None]
        print(f"[llm_review] {len(trade_perf['closed'])} abgeschlossene Trades, {len(trades_with_pnl)} mit PnL")
        data = _collect_data(db_path)
        trades = data["trades"]
        trade_count = len(trades)
        ghost_count = sum(1 for t in trades if "Ghost order" in (t.get("rejection_reason") or ""))

        print(f"[llm_review] {trade_count} Trades, {ghost_count} Ghost-Failures")

        ghost_rates = _compute_ghost_rates(trades)
        signal_perf = _compute_signal_perf(data["signal_stats"], data.get("ghost_signal_stats"))

        print(f"[llm_review] Ghost-Raten berechnet: {len(ghost_rates)} Exchanges analysiert")
        for suffix, stats in sorted(ghost_rates.items(), key=lambda x: -x[1]["rate"]):
            print(f"  {suffix}: {int(stats['rate']*100)}% ({stats['ghost']}/{stats['total']})")

        # LLM-Analyse (best-effort, Timeout 85s)
        print(f"[llm_review] Rufe LLM auf (Timeout {LLM_TIMEOUT_S}s)...")
        ghost_trends = _compute_ghost_trends(trading_memory, ghost_rates)
        # Ghost-Orders aus non-tradable Instrumenten (werden nicht mehr platziert)
        non_tradable_ghost_count = sum(
            1 for t in trades
            if t.get("is_tradable") == 0
            and "Ghost order" in (t.get("rejection_reason") or "")
        )
        if non_tradable_ghost_count:
            print(f"[llm_review] {non_tradable_ghost_count} Ghost-Orders aus non-tradable Instrumenten (herausgefiltert)")
        # fix/position-meta-dedup (2026-07-15): Positions-Empfehlungen macht
        # ausschliesslich der position_review_worker (jetzt 4x/Tag inkl.
        # 22:35 nach NYSE-Close) — die Doppel-Schreiber-Kollision auf
        # llm_position_recommendations.json ist damit beseitigt.
        llm_analysis = _call_llm(
            ghost_rates, signal_perf, data["state"], data["positions"],
            trade_perf, trading_memory,
            slippage_top=data.get("slippage_top"),
            ghost_trends=ghost_trends,
            non_tradable_ghost_count=non_tradable_ghost_count,
        )
        if llm_analysis:
            print(f"[llm_review] LLM-Antwort erhalten: {list(llm_analysis.keys())}")
        else:
            print("[llm_review] LLM nicht verfügbar — fahre mit algorithmischer Analyse fort")

        # Blacklist autonom aktualisieren
        blacklist = _update_ghost_blacklist(ghost_rates, llm_analysis)

        # Trading-Memory und Signal-Weights autonom aktualisieren
        if llm_analysis:
            _update_trading_memory(llm_analysis, trade_perf, ghost_rates=ghost_rates)
            _update_signal_weights(llm_analysis)

        # Config autonom optimieren (innerhalb Bible Hard Limits)
        if llm_analysis and llm_analysis.get("config_adjustments"):
            # Baseline-Metriken VOR Config-Aenderung (Prio 2a)
            _run_baseline: dict | None = None
            _recent_pnl = [t for t in trade_perf.get("closed", []) if t.get("pnl_pct") is not None][:20]
            if _recent_pnl:
                _run_baseline = {
                    "avg_pnl_pct": round(sum(t["pnl_pct"] for t in _recent_pnl) / len(_recent_pnl), 2),
                    "win_rate": round(sum(1 for t in _recent_pnl if t["pnl_pct"] > 0) / len(_recent_pnl), 2),
                    "n_trades": len(_recent_pnl),
                    "ghost_rate_total": round(ghost_count / trade_count, 2) if trade_count else 0,
                }
            config_changes = _update_config_yaml(llm_analysis["config_adjustments"], baseline=_run_baseline)
            if config_changes:
                # Aenderungen in Memory persistieren
                memory = _load_trading_memory()
                memory.setdefault("config_history", []).append({
                    "date": datetime.now(timezone.utc).isoformat()[:10],
                    "changes": config_changes,
                })
                memory["config_history"] = memory.get("config_history", [])[-20:]
                TRADING_MEMORY_PATH.write_text(_json_mod.dumps(memory, indent=2, ensure_ascii=False))

        # Insights-Log schreiben
        _append_insights(blacklist, llm_analysis, ghost_rates)

        # Discord-Post
        _post_discord(blacklist, llm_analysis, ghost_rates, trade_count, ghost_count)

        # Stale-Exit-Lernschleife (fix/stale-exit): 72h-Rueckblick
        try:
            _graded = _evaluate_stale_exits()
            if _graded:
                print(f"[llm_review] {_graded} Stale-Exit(s) bewertet (72h-Rueckblick)")
        except Exception as _se_exc:
            print(f"[llm_review] Stale-Exit-Bewertung fehlgeschlagen: {_se_exc}")

        try:
            from bot.core.heartbeat import record_duration as _rd
            from bot.db.connection import DB as _DB_dur
            from bot.db.repo import StateRepo as _SR_dur
            _rd(_SR_dur(_DB_dur(db_path)), "llm_review_worker", time.time() - t_start)
        except Exception:
            pass
        print(f"[llm_review] Fertig in {time.time() - t_start:.1f}s")
        return 0


if __name__ == "__main__":
    sys.exit(main())
