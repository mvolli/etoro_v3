#!/usr/bin/env python3
"""discord_embeds.py — Zentrales Discord Embed-Modul für alle Pipeline-Phasen.

Bietet strukturierte Embeds für:
  P1 — Heartbeat / Pipeline-Tick-Status
  P2 — Reconciliation (Portfolio-Sync)
  P3 — SL-Watchdog (Stop-Loss Events)
  P4 — Trading Decisions (BUY / SELL / HOLD)
  P5 — Consolidation (Fragment-Bereinigung)
  P6 — Candidate Ranking (bereits in monitoring_alerts.py als P3-O)

Alle Embeds gehen via _post_embed() → Discord Bot API v10.
Channel-Routing:
  #etoro-trading  (MAIN)   — Portfolio, Heartbeat, Monitoring, Candidates
  #etoro-trades   (TRADES) — BUY/SELL Executions, SL-Events, Consolidation
"""

from __future__ import annotations

import http.client
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import logging

# V3: infrastructure_module removed — stub out to avoid 120s SQLite timeout
logger = logging.getLogger("discord_embeds")


def insert_system_log(level: str, category: str, message: str, details: str = "") -> None:
    """No-op stub — V3 uses bot.db.repo.LogRepo instead of infrastructure_module."""
    logger.debug("[discord_embeds] log(%s, %s): %s", level, category, message)

# ─── Channels ────────────────────────────────────────────────────────────────
DISCORD_MAIN_CHANNEL  = "1513971015108263957"   # #etoro-trading
DISCORD_TRADE_CHANNEL = "1514786489110630600"   # #etoro-trades

# ─── Embed-Farben ─────────────────────────────────────────────────────────────
COLOR_GREEN   = 0x2ECC71
COLOR_RED     = 0xE74C3C
COLOR_ORANGE  = 0xE67E22
COLOR_BLUE    = 0x3498DB
COLOR_YELLOW  = 0xF1C40F
COLOR_GREY    = 0x95A5A6
COLOR_PURPLE  = 0x9B59B6
COLOR_TEAL    = 0x1ABC9C


# ─── Interne Helpers ─────────────────────────────────────────────────────────

def _read_token() -> Optional[str]:
    env = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env):
        with open(env) as f:
            for line in f:
                if "DISCORD_BOT_TOKEN" in line and not line.startswith("#"):
                    return line.split("=", 1)[1].strip()
    return None


def _clip_embed_limits(embed: dict) -> dict:
    """Discord-API-Limits zentral durchsetzen, statt in jedem Embed einzeln.

    Ohne Clipping lehnt Discord das GANZE Embed mit 400 ab (Notification
    verloren) — z.B. wenn resolve_instrument_display() lange Namen liefert
    und ein Feld über 1024 Zeichen wächst.
    """
    if embed.get("title"):
        embed["title"] = str(embed["title"])[:256]
    if embed.get("description"):
        embed["description"] = str(embed["description"])[:4096]
    fields = embed.get("fields") or []
    embed["fields"] = [
        {**f,
         "name":  str(f.get("name", ""))[:256],
         "value": str(f.get("value", ""))[:1024]}
        for f in fields[:25]
    ]
    return embed


def _post_embed(embed: dict, channel_id: str, dry_run: bool = False) -> bool:
    """Sende ein Discord Embed. Gibt True bei Erfolg zurück."""
    embed = _clip_embed_limits(embed)
    if dry_run:
        logger.info(f"[discord_embeds DRY-RUN] '{embed.get('title', '?')}' → channel {channel_id}")
        return True

    token = _read_token()
    if not token:
        logger.error("discord_embeds: kein DISCORD_BOT_TOKEN")
        return False

    try:
        payload = json.dumps({"embeds": [embed]}).encode("utf-8")
        conn = http.client.HTTPSConnection("discord.com", timeout=10)
        conn.request(
            "POST",
            f"/api/v10/channels/{channel_id}/messages",
            body=payload,
            headers={
                "Authorization":  f"Bot {token}",
                "Content-Type":   "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()

        if resp.status in (200, 201):
            logger.info(f"[discord_embeds] Embed gepostet: '{embed.get('title','?')}' → {channel_id}")
            return True
        else:
            logger.error(f"[discord_embeds] Discord API {resp.status}: {body[:200]}")
            return False
    except Exception as exc:
        logger.error(f"[discord_embeds] Post-Fehler: {exc}")
        return False


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pnl_emoji(pct: float) -> str:
    if pct >= 2:    return "🟢"
    if pct >= 0:    return "🔵"
    if pct >= -2:   return "🟡"
    if pct >= -5:   return "🟠"
    return "🔴"


def _severity_color(severity: str) -> int:
    return {
        "NORMAL":          COLOR_GREEN,
        "CAUTION":         COLOR_YELLOW,
        "WARNING":         COLOR_ORANGE,
        "CRITICAL":        COLOR_RED,
        "CIRCUIT_BREAKER": COLOR_PURPLE,
    }.get(severity, COLOR_GREY)


# ─── Instrument Display Resolution ───────────────────────────────────────────
# Discord-Embeds sollen nie rohe instrument_ids ohne Kontext zeigen.
# resolve_instrument_display() löst eine ID oder ein Symbol gegen die
# instruments-Tabelle auf und liefert "Symbol — Name (Market)".
# Fail-open: wenn DB/Zeile fehlt, kommt die Eingabe unverändert zurück
# (numerische IDs werden als "Instrument #<id>" gekennzeichnet).

_PROJECT_ROOT_DE = Path(__file__).resolve().parent.parent.parent   # src/bot → src → etoro_v3
_TRADING_DB_PATH = _PROJECT_ROOT_DE / "data" / "trading.db"
_INSTRUMENT_LOOKUP_CACHE: dict[str, Optional[dict]] = {}


def _lookup_instrument(instrument_ref) -> Optional[dict]:
    """Instrumenten-Zeile per instrument_id ODER Symbol aus data/trading.db.

    Ergebnis wird pro Prozess gecacht. Fail-open: None bei Fehlern/kein Match.
    """
    ref = str(instrument_ref).strip()
    if not ref:
        return None
    if ref in _INSTRUMENT_LOOKUP_CACHE:
        return _INSTRUMENT_LOOKUP_CACHE[ref]

    row_dict: Optional[dict] = None
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{_TRADING_DB_PATH}?mode=ro", uri=True, timeout=3)
        try:
            conn.row_factory = sqlite3.Row
            if ref.isdigit():
                row = conn.execute(
                    "SELECT instrument_id, symbol, name, market_region, asset_class "
                    "FROM instruments WHERE instrument_id = ?", (int(ref),)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT instrument_id, symbol, name, market_region, asset_class "
                    "FROM instruments WHERE symbol = ? COLLATE NOCASE", (ref,)
                ).fetchone()
            if row:
                row_dict = dict(row)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"[discord_embeds] Instrument-Lookup '{ref}' fehlgeschlagen: {exc}")
        return None  # nicht cachen — DB könnte gleich wieder da sein

    # Nur Treffer cachen: ein Miss kann durch spätere Discovery-Inserts zum
    # Treffer werden — Negativ-Cache würde dauerhaft "Instrument #id" zeigen.
    if row_dict is not None:
        _INSTRUMENT_LOOKUP_CACHE[ref] = row_dict
    return row_dict


def resolve_instrument_display(instrument_ref) -> str:
    """'Symbol — Name (Market)' für eine instrument_id oder ein Symbol.

    Beispiele:
        2358      → "00027.HK — China Telecom (HK)"
        "CVX.US"  → "CVX.US — Chevron (US)"
        "AAPL"    → "AAPL — Apple Inc (US)"

    Fail-open: unbekanntes Symbol kommt unverändert zurück, eine unbekannte
    numerische ID als "Instrument #<id>" (nie eine nackte Zahl ohne Kontext).
    """
    ref = str(instrument_ref).strip()
    if not ref or ref == "?":
        return "?"
    return _format_instrument_display(ref, _lookup_instrument(ref))


def _format_instrument_display(ref: str, row: Optional[dict]) -> str:
    """Display-String aus einer (evtl. fehlenden) instruments-Zeile bauen."""
    if row is None:
        return f"Instrument #{ref}" if ref.isdigit() else ref

    symbol = row.get("symbol") or ref
    name   = (row.get("name") or "").strip()
    region = (row.get("market_region") or "").strip()

    display = symbol
    if name and name.upper() != symbol.upper():
        display += f" — {name}"
    if region:
        display += f" ({region})"
    return display


# ═══════════════════════════════════════════════════════════════════════════════
# P1 — HEARTBEAT / TICK-STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def post_heartbeat_embed(
    tick: int,
    equity: float,
    cash: float,
    position_count: int,
    drawdown_pct: float,
    severity: str,
    cb_active: bool,
    elapsed_s: float,
    cb_status: dict = None,
    phase_durations: dict = None,
    dry_run: bool = False,
) -> bool:
    """Pipeline-Heartbeat — alle 30min (TAKT_MONITORING) in #etoro-trading.

    Args:
        tick: Pipeline tick number
        equity: Total portfolio equity
        cash: Available cash balance
        position_count: Number of open positions
        drawdown_pct: Current drawdown percentage
        severity: Drawdown severity level (NORMAL/CAUTION/WARNING/CRITICAL/CIRCUIT_BREAKER)
        cb_active: Whether circuit breaker is active
        elapsed_s: Pipeline runtime in seconds
        cb_status: CircuitBreaker.get_status() dict (state, failure_count, error_counts, etc.)
        phase_durations: Dict of {phase_name: elapsed_seconds} for this pipeline run
    """
    cash_pct  = (cash / equity * 100) if equity else 0
    dd_emoji  = _pnl_emoji(-drawdown_pct)

    # T10.1: Circuit Breaker Status Details
    if cb_status:
        cb_state = cb_status.get("state", "UNKNOWN")
        cb_failures = cb_status.get("failure_count", 0)
        cb_errors = cb_status.get("error_counts", {})
        cb_uptime = cb_status.get("uptime_seconds", 0)

        if cb_state == "OPEN":
            cb_str = f"🔴 OPEN (failures: {cb_failures})"
        elif cb_state == "HALF_OPEN":
            cb_str = f"🟡 HALF_OPEN (test mode)"
        elif cb_active:
            cb_str = f"🔴 AKTIV (failures: {cb_failures})"
        else:
            cb_str = "✅ CLOSED"

        # Add error breakdown if there are errors
        if cb_errors and any(v > 0 for v in cb_errors.values()):
            error_parts = [f"{k}×{v}" for k, v in sorted(cb_errors.items(), key=lambda x: -x[1]) if v > 0]
            cb_str += f"\nErrors: {', '.join(error_parts[:4])}"
    else:
        cb_str = "🔴 AKTIV" if cb_active else "✅ Inaktiv"

    pnl_total = equity - 10_000
    pnl_pct   = (pnl_total / 10_000 * 100)

    color = COLOR_PURPLE if cb_active else _severity_color(severity)

    fields = [
        {
            "name":   "💰 Portfolio",
            "value":  (
                f"Equity:  **${equity:,.2f}**\n"
                f"Cash:    **${cash:,.2f}** ({cash_pct:.1f}%)\n"
                f"Pos:     **{position_count}**"
            ),
            "inline": True,
        },
        {
            "name":   "📉 Drawdown",
            "value":  (
                f"{dd_emoji} **{drawdown_pct:.2f}%**\n"
                f"Severity: `{severity}`\n"
                f"CB: {cb_str}"
            ),
            "inline": True,
        },
        {
            "name":   "📊 Total PnL (seit $10k)",
            "value":  f"{_pnl_emoji(pnl_pct)} **${pnl_total:+,.2f}** ({pnl_pct:+.2f}%)",
            "inline": True,
        },
    ]

    # T10.2: Pipeline Duration per Phase (if available)
    if phase_durations:
        sorted_phases = sorted(phase_durations.items(), key=lambda x: -x[1])
        duration_lines = []
        for name, dur in sorted_phases[:8]:
            bar_len = min(int(dur / 5), 20)  # 1 block per 5 seconds, max 20
            bar = "🟦" * bar_len if dur < 60 else ("🟧" * bar_len if dur < 120 else "🟥" * bar_len)
            duration_lines.append(f"{bar} **{name}**: {dur:.0f}s")
        total_pipeline = sum(phase_durations.values())
        duration_lines.insert(0, f"**Total Pipeline**: {total_pipeline:.0f}s")
        fields.append({
            "name":   "⏱️ Phase Duration",
            "value":  "\n".join(duration_lines),
            "inline": False,
        })

    embed = {
        "title":       f"💓 Pipeline Heartbeat — Tick #{tick}",
        "description": f"Laufzeit letzter Tick: `{elapsed_s:.0f}s`",
        "color":       color,
        "fields":      fields,
        "footer":    {"text": f"eToro RoBoCop · Tick #{tick} · alle 30min"},
        "timestamp": _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P1 Heartbeat Tick#{tick} gepostet")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P2 — RECONCILIATION
# ═══════════════════════════════════════════════════════════════════════════════

def post_reconciliation_embed(
    result: dict,
    dry_run: bool = False,
) -> bool:
    """Portfolio Reconciliation Ergebnis — in #etoro-trading.

    `result` ist das Return-Dict von portfolio_module.reconcile().
    """
    if not result or result.get("success") is False:
        err = result.get("error", "Unbekannter Fehler") if result else "result=None"
        embed = {
            "title":       "🔄 Reconciliation — FEHLER",
            "description": f"```{err[:300]}```",
            "color":       COLOR_RED,
            "timestamp":   _ts(),
            "footer":      {"text": "eToro RoBoCop · Reconciliation"},
        }
        return _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)

    equity      = result.get("api_equity", 0)
    cash_raw    = result.get("api_credit", 0)   # credit = verfügbares Cash in eToro
    pos_count   = result.get("api_position_count", 0)
    equity_gap  = result.get("equity_gap", 0)
    pos_gap     = result.get("position_gap", 0)
    pnl_total   = result.get("total_pnl_from_start", 0)
    pnl_pct     = result.get("pnl_pct_from_start", 0)
    issues      = result.get("issues", [])
    corrected   = result.get("corrected", False)

    sync_str = "✅ In Sync" if not issues else f"⚠️ {len(issues)} Issue(s) korrigiert" if corrected else f"🔴 {len(issues)} Issue(s)"
    gap_str  = f"`{equity_gap:+.2f}$`" if abs(equity_gap) > 0.01 else "`±0.00$`"

    issue_lines = "\n".join(f"• `{code}`: {msg[:60]}" for code, msg in issues[:4]) or "_Keine_"

    embed = {
        "title":       "🔄 Portfolio Reconciliation",
        "color":       COLOR_GREEN if not issues else (COLOR_YELLOW if corrected else COLOR_RED),
        "fields": [
            {
                "name":   "📊 API-Stand",
                "value":  (
                    f"Equity:    **${equity:,.2f}**\n"
                    f"Pos:       **{pos_count}**\n"
                    f"Equity-Gap: {gap_str} | Pos-Gap: `{pos_gap:+d}`"
                ),
                "inline": True,
            },
            {
                "name":   "💹 Gesamt-PnL",
                "value":  f"{_pnl_emoji(pnl_pct)} **${pnl_total:+,.2f}** ({pnl_pct:+.2f}%)",
                "inline": True,
            },
            {
                "name":   f"🔧 Sync-Status: {sync_str}",
                "value":  issue_lines,
                "inline": False,
            },
        ],
        "footer":    {"text": "eToro RoBoCop · Reconciliation"},
        "timestamp": _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P2 Reconciliation gepostet equity={equity:.2f}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P3 — SL-WATCHDOG
# ═══════════════════════════════════════════════════════════════════════════════

def post_sl_watchdog_embed(
    result: dict,
    dry_run: bool = False,
) -> bool:
    """SL-Watchdog Ergebnis — NUR posten wenn closed > 0 oder errors > 0.

    `result` ist das Return-Dict von sl_watchdog.run_sl_watchdog():
    {checked, closed, errors, alerts[]}
    """
    checked = result.get("checked", 0)
    closed  = result.get("closed", 0)
    errors  = result.get("errors", 0)
    alerts  = result.get("alerts", [])

    # Nur posten wenn etwas passiert ist
    if closed == 0 and errors == 0:
        logger.debug(f"[discord_embeds] SL-Watchdog: {checked} geprüft, 0 Events — kein Embed")
        return True

    color = COLOR_RED if closed > 0 else COLOR_ORANGE

    def _fmt_alert(a) -> str:
        # Dict-Alerts ({symbol|instrument_id, pnl_pct, reason}) menschenlesbar
        # auflösen; String-Alerts unverändert durchreichen.
        if isinstance(a, dict):
            inst   = resolve_instrument_display(a.get("symbol") or a.get("instrument_id") or "?")
            parts  = [f"**{inst}**"]
            if a.get("pnl_pct") is not None:
                parts.append(f"PnL={a['pnl_pct']:+.1f}%")
            reason = a.get("reason") or a.get("message") or ""
            if reason:
                parts.append(str(reason)[:80])
            return " · ".join(parts)
        return str(a)

    alert_text = "\n".join(f"• {_fmt_alert(a)}" for a in alerts[:8]) or "_Keine Alerts_"

    embed = {
        "title":       f"🛑 SL-Watchdog — {closed} Position(en) geschlossen",
        "color":       color,
        "fields": [
            {
                "name":   "📊 Statistik",
                "value":  (
                    f"Geprüft:    **{checked}**\n"
                    f"Geschlossen: **{closed}**\n"
                    f"Fehler:     **{errors}**"
                ),
                "inline": True,
            },
            {
                "name":   "🚨 Stop-Loss Alerts",
                "value":  alert_text,
                "inline": False,
            },
        ],
        "footer":    {"text": "eToro RoBoCop · SL-Watchdog · alle 5min"},
        "timestamp": _ts(),
    }
    ok = _post_embed(embed, DISCORD_TRADE_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P3 SL-Watchdog gepostet closed={closed}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P4 — TRADING DECISIONS
# ═══════════════════════════════════════════════════════════════════════════════

def post_trading_decisions_embed(
    decisions: list,
    results: list,
    equity: float,
    cash: float,
    dry_run: bool = False,
) -> bool:
    """Trading-Entscheidungen — NUR posten wenn decisions nicht leer.

    `decisions` — Liste von Decision-Dicts {instrument, action, reason, conviction, amount_pct}
    `results`   — Liste von execute_all_trades() Rückgaben {instrument, action, success, error?}
    `equity`, `cash` — aktueller Portfolio-Stand
    """
    if not decisions:
        logger.debug("[discord_embeds] Trading Decisions: leer — kein Embed")
        return True

    buys    = [d for d in decisions if d.get("action") == "BUY"]
    sells   = [d for d in decisions if d.get("action") in ("SELL", "SELL_PARTIAL", "SELL_FULL", "CLOSE")]
    holds   = [d for d in decisions if d.get("action") == "HOLD"]

    # Ergebnis-Mapping: instrument → success
    res_map = {r.get("instrument"): r.get("success", False) for r in (results or [])}

    def _fmt_decisions(decs: list) -> str:
        if not decs:
            return "_Keine_"
        lines = []
        for d in decs[:6]:
            raw_sym = d.get("instrument", "?")
            sym    = resolve_instrument_display(raw_sym)
            conv   = d.get("conviction", "")
            pct    = d.get("amount_pct", 0)
            reason = d.get("reason", "")[:50]
            ok     = res_map.get(raw_sym)
            st     = "✅" if ok else ("❌" if ok is False else "⏳")
            conv_e = {"VERY_HIGH": "🔥", "HIGH": "⬆️", "MEDIUM-HIGH": "↗️", "MEDIUM": "〰️", "LOW": "⬇️"}.get(conv, "")
            lines.append(f"{st} {conv_e} **{sym}** ({pct:.1f}%) — {reason}")
        return "\n".join(lines)

    n_success = sum(1 for r in (results or []) if r.get("success"))
    n_fail    = sum(1 for r in (results or []) if not r.get("success"))
    color     = COLOR_GREEN if buys else (COLOR_RED if sells else COLOR_GREY)
    cash_pct  = (cash / equity * 100) if equity else 0

    fields = []
    if buys:
        fields.append({"name": f"📈 BUY ({len(buys)})", "value": _fmt_decisions(buys), "inline": False})
    if sells:
        fields.append({"name": f"📉 SELL/CLOSE ({len(sells)})", "value": _fmt_decisions(sells), "inline": False})
    if holds:
        hold_syms = ", ".join(resolve_instrument_display(d.get("instrument", "?")) for d in holds[:10])
        fields.append({"name": f"⏸️ HOLD ({len(holds)})", "value": f"`{hold_syms}`", "inline": False})

    fields.append({
        "name":   "💰 Portfolio nach Trades",
        "value":  f"Equity: **${equity:,.2f}** | Cash: **${cash:,.2f}** ({cash_pct:.1f}%) | ✅{n_success} ❌{n_fail}",
        "inline": False,
    })

    embed = {
        "title":       f"⚡ Trading — {len(buys)} BUY · {len(sells)} SELL · {len(holds)} HOLD",
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Active Trading · alle 15min"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_TRADE_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds",
                          f"P4 Trading Decisions gepostet buy={len(buys)} sell={len(sells)}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P5 — CONSOLIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def post_consolidation_embed(
    closed: list,
    fragment_count: int,
    fragment_threshold: int,
    cash_before: float,
    cash_after: float,
    dry_run: bool = False,
) -> bool:
    """Konsolidierungs-Ergebnis — NUR posten wenn closed nicht leer.

    `closed` — Liste von {symbol, pnl_pct, pnl_value, amount, reason}
    """
    if not closed:
        logger.debug("[discord_embeds] Consolidation: 0 geschlossen — kein Embed")
        return True

    cash_freed = cash_after - cash_before
    total_pnl  = sum(c.get("pnl_value", 0) for c in closed)
    color      = COLOR_TEAL if total_pnl >= 0 else COLOR_ORANGE

    lines = []
    for c in closed[:8]:
        sym    = resolve_instrument_display(c.get("symbol") or c.get("instrument_id") or "?")
        pct    = c.get("pnl_pct", 0)
        val    = c.get("pnl_value", 0)
        reason = c.get("reason", "")[:55]
        lines.append(f"{_pnl_emoji(pct)} **{sym}** PnL={pct:+.1f}% (${val:+.1f}) — {reason}")

    embed = {
        "title":       f"🗜️ Konsolidierung — {len(closed)} Position(en) geschlossen",
        "color":       color,
        "fields": [
            {
                "name":   "📊 Statistik",
                "value":  (
                    f"Fragmente: **{fragment_count}** (Schwelle: {fragment_threshold})\n"
                    f"Cash freigesetzt: **${cash_freed:+,.2f}**\n"
                    f"Gesamt-PnL: **${total_pnl:+,.2f}**"
                ),
                "inline": True,
            },
            {
                "name":   "🔴 Geschlossene Positionen",
                "value":  "\n".join(lines) or "_Keine_",
                "inline": False,
            },
        ],
        "footer":    {"text": "eToro RoBoCop · Consolidation · alle 2h"},
        "timestamp": _ts(),
    }
    ok = _post_embed(embed, DISCORD_TRADE_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds",
                          f"P5 Consolidation gepostet closed={len(closed)} pnl={total_pnl:.2f}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P7 — PORTFOLIO HEALTH DASHBOARD (Echtzeit-Metriken)
# ═══════════════════════════════════════════════════════════════════════════════

def post_portfolio_health_dashboard(dry_run: bool = False) -> bool:
    """Portfolio Health Dashboard — Echtzeit-Metriken aus DB.

    Zeigt: Equity, Drawdown, Open Positions, Cash Strategy, Performance Summary
    Geht an #etoro-trading (MAIN).
    """
    from db_manager import DBContext
    import sqlite3

    with DBContext() as conn:
        # ── Portfolio State ──
        row = conn.execute("SELECT * FROM portfolio_state LIMIT 1").fetchone()
        if row:
            cursor = conn.execute("SELECT * FROM portfolio_state LIMIT 1")
            cols = [desc[0] for desc in cursor.description]
            d = dict(zip(cols, row))
            equity = d.get("total_equity", 0) or 0
            cash = d.get("cash_balance", 0) or d.get("credit", 0) or 0
            unrealized = d.get("unrealized_pnl", 0) or 0
            total_pnl = unrealized
            pnl_pct = (total_pnl / 10_000 * 100) if total_pnl != 0 else 0
        else:
            equity = cash = total_pnl = pnl_pct = 0

        # ── Open Positions ──
        pos_rows = conn.execute(
            "SELECT instrument, amount_usd, pnl_usd, pnl_pct FROM trades_history WHERE status='OPEN'"
        ).fetchall()
        pos_count = len(pos_rows)
        pos_details = []
        for pr in pos_rows[:8]:
            sym, amt, pnl_val, pnl_p = pr[0], pr[1] or 0, pr[2] or 0, pr[3] or 0
            pos_details.append((sym, pnl_p, pnl_val))

        # ── Drawdown ──
        dd_row = conn.execute(
            "SELECT smoothed_peak, ratcheted_peak FROM peak_equity LIMIT 1"
        ).fetchone()
        if dd_row and dd_row[0]:
            peak = dd_row[0]
            drawdown_pct = ((peak - equity) / peak * 100) if peak > 0 else 0
        else:
            peak = equity
            drawdown_pct = 0

        # ── Performance Metrics (letzte 24h Trades) ──
        trade_rows = conn.execute(
            """SELECT COUNT(*), SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END),
                      SUM(pnl_usd), AVG(pnl_usd)
               FROM trades_history WHERE status IN ('CLOSED','CLOSED_SL')
               AND exit_timestamp >= datetime('now', '-24 hours')"""
        ).fetchone()
        total_trades = trade_rows[0] or 0
        winning = trade_rows[1] or 0
        losing = trade_rows[2] or 0
        total_trade_pnl = trade_rows[3] or 0
        avg_trade_pnl = trade_rows[4] or 0

        # ── Cache Stats ──
        import os, time, glob as _glob
        yf_cache_dir = os.path.join(os.path.dirname(__file__), "..", "cache", "yfinance")
        yf_hits = len(_glob.glob(os.path.join(yf_cache_dir, "*.parquet"))) if os.path.isdir(yf_cache_dir) else 0
        corr_cache_file = os.path.join(os.path.dirname(__file__), "..", "cache", "correlation.json")
        corr_cached = os.path.exists(corr_cache_file)
        corr_age = ""
        if corr_cached:
            age_s = int(time.time() - os.path.getmtime(corr_cache_file))
            corr_age = f" ({age_s}s alt)"

    # ── Build Embed ──
    cash_pct = (cash / equity * 100) if equity else 0
    win_rate = (winning / total_trades * 100) if total_trades > 0 else 0

    color = COLOR_GREEN if pnl_pct >= 0 else (COLOR_ORANGE if pnl_pct >= -5 else COLOR_RED)

    # Position Details
    pos_lines = []
    for sym, pp, pv in sorted(pos_details, key=lambda x: x[1], reverse=True):
        pos_lines.append(f"{_pnl_emoji(pp)} **{sym}**: {pp:+.1f}% (${pv:+.1f})")
    pos_text = "\n".join(pos_lines) if pos_lines else "_Keine offenen Positionen_"

    # Performance Summary
    perf_text = (
        f"Trades (24h): **{total_trades}** "
        f"(✅{winning} ❌{losing}, {win_rate:.0f}%)\\n"
        f"PnL: **${total_trade_pnl:+,.2f}** "
        f"(Ø ${avg_trade_pnl:+.2f}/Trade)"
    ) if total_trades > 0 else "_Keine Trades in 24h_"

    # Cache Status
    cache_text = (
        f"yf.download: **{yf_hits}** gecachte Files\\n"
        f"Correlation: {'✅ Gecached' + corr_age if corr_cached else '❌ Kein Cache'}"
    )

    fields = [
        {
            "name": "💰 Portfolio",
            "value": (
                f"Equity: **${equity:,.2f}**\\n"
                f"Cash: **${cash:,.2f}** ({cash_pct:.1f}%)\\n"
                f"PnL: {_pnl_emoji(pnl_pct)} **${total_pnl:+,.2f}** ({pnl_pct:+.2f}%)"
            ),
            "inline": True,
        },
        {
            "name": "📉 Drawdown",
            "value": (
                f"Peak: **${peak:,.2f}**\\n"
                f"DD: {_pnl_emoji(-drawdown_pct)} **{drawdown_pct:.2f}%**\\n"
                f"Pos: **{pos_count}** offen"
            ),
            "inline": True,
        },
        {
            "name": "📊 Performance (24h)",
            "value": perf_text,
            "inline": False,
        },
        {
            "name": f"📈 Offene Positionen ({pos_count})",
            "value": pos_text,
            "inline": False,
        },
        {
            "name": "⚡ Cache Status",
            "value": cache_text,
            "inline": True,
        },
    ]

    embed = {
        "title": "📊 Portfolio Health Dashboard",
        "color": color,
        "fields": fields,
        "footer": {"text": "eToro RoBoCop · Echtzeit-Metriken"},
        "timestamp": _ts(),
    }

    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P7 Portfolio Health Dashboard gepostet")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P8 — PIPELINE PERFORMANCE DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

def post_pipeline_performance_dashboard(dry_run: bool = False) -> bool:
    """Pipeline Performance Dashboard — Timing, Gate Stats, System Health.

    Zeigt: Pipeline-Latenz, Phase-Durations, Gate Hit-Rate, DB-Status
    Geht an #etoro-trading (MAIN).
    """
    from db_manager import DBContext

    with DBContext() as conn:
        # ── System Heartbeat ──
        hb_rows = conn.execute(
            "SELECT component, last_beat, notes FROM system_heartbeat ORDER BY component"
        ).fetchall()

        # ── Phase3 Metrics (Pipeline Performance) ──
        perf_row = conn.execute(
            """SELECT total_trades, winning_trades, losing_trades, win_rate,
                      total_pnl, max_drawdown
               FROM performance_metrics ORDER BY id DESC LIMIT 1"""
        ).fetchone()

        # ── System Heartbeat (letzte Pipeline Runs) ──
        last_pipeline = conn.execute(
            """SELECT component, last_beat FROM system_heartbeat
               WHERE component LIKE '%pipeline%' OR component LIKE '%cron%'
               ORDER BY last_beat DESC LIMIT 1"""
        ).fetchone()

        # ── Gate Stats (aus trades_history analysieren) ──
        buy_decisions = conn.execute(
            """SELECT COUNT(*) FROM trades_history
               WHERE direction='BUY' AND status IN ('OPEN','PENDING_API')"""
        ).fetchone()[0] or 0

        failed_trades = conn.execute(
            """SELECT COUNT(*) FROM trades_history
               WHERE status='FAILED' AND exit_timestamp >= datetime('now', '-24 hours')"""
        ).fetchone()[0] or 0

        # Queue stats
        queued = conn.execute("SELECT COUNT(*) FROM trade_queue WHERE status='QUEUED'").fetchone()[0] or 0
        pending = conn.execute("SELECT COUNT(*) FROM pending_orders WHERE status='PENDING'").fetchone()[0] or 0

        # DB Size
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
        import os as _os
        db_size_mb = round(_os.path.getsize(db_path) / 1024 / 1024, 1) if db_path and _os.path.exists(db_path) else 0

    # ── Build Embed ──
    total_trades_all = perf_row[0] or 0 if perf_row else 0
    winning_all = perf_row[1] or 0 if perf_row else 0
    losing_all = perf_row[2] or 0 if perf_row else 0
    win_rate_all = perf_row[3] or 0 if perf_row else 0
    total_pnl_all = perf_row[4] or 0 if perf_row else 0
    max_dd = perf_row[5] or 0 if perf_row else 0

    last_run = "N/A"
    if last_pipeline:
        from datetime import datetime as _dt, timezone as _tz
        try:
            beat_dt = _dt.fromisoformat(str(last_pipeline[1]).replace('Z', '+00:00'))
            age_s = int((_dt.now(_tz.utc) - beat_dt).total_seconds())
            last_run = f"{age_s}s ago"
        except Exception:
            last_run = str(last_pipeline[1])[:19]

    # Heartbeat Status
    hb_lines = []
    for comp, beat, notes in hb_rows[:8]:
        try:
            from datetime import datetime as _dt, timezone as _tz
            beat_dt = _dt.fromisoformat(str(beat).replace('Z', '+00:00'))
            age_s = int((_dt.now(_tz.utc) - beat_dt).total_seconds())
            status = "✅" if age_s < 600 else ("🟡" if age_s < 1800 else "🔴")
        except Exception:
            status = "⚠️"
            age_s = "?"
        hb_lines.append(f"{status} **{comp}**: {age_s}s ago")

    # Color based on system health
    color = COLOR_GREEN if failed_trades == 0 else (COLOR_ORANGE if failed_trades < 3 else COLOR_RED)

    fields = [
        {
            "name": "🔄 Pipeline",
            "value": (
                f"Total Trades: **{total_trades_all}**\\n"
                f"Win Rate: **{win_rate_all:.0f}%** (✅{winning_all} ❌{losing_all})\\n"
                f"All-Time PnL: **${total_pnl_all:+,.2f}**\\n"
                f"Max DD: **{max_dd:.1f}%**\\n"
                f"Last Run: `{last_run}`"
            ),
            "inline": True,
        },
        {
            "name": "📋 Queue Status",
            "value": (
                f"Trade Queue: **{queued}** pending\\n"
                f"Pending Orders: **{pending}**\\n"
                f"Open BUYs: **{buy_decisions}**\\n"
                f"Failed (24h): **{failed_trades}**"
            ),
            "inline": True,
        },
        {
            "name": "💾 System",
            "value": (
                f"DB Size: **{db_size_mb} MB**\\n"
                f"Components: **{len(hb_rows)}** monitored"
            ),
            "inline": True,
        },
        {
            "name": "❤️ Component Health",
            "value": "\\n".join(hb_lines) or "_Keine Daten_",
            "inline": False,
        },
    ]

    embed = {
        "title": "⚡ Pipeline Performance Dashboard",
        "color": color,
        "fields": fields,
        "footer": {"text": "eToro RoBoCop · System Health"},
        "timestamp": _ts(),
    }

    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P8 Pipeline Performance Dashboard gepostet")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P7 — DATA INGESTION (yfinance)
# ═══════════════════════════════════════════════════════════════════════════════

def post_data_worker_embed(
    tier1_count: int,
    tier2_open: int,
    tier2_closed: int,
    tier2_total: int,
    total_symbols: int,
    symbols_fetched: int,
    signals_generated: int,
    signals_expired: int,
    failed_cache_size: int,
    elapsed_s: float,
    new_signals: list = None,
    market_status: str = "",
    dry_run: bool = False,
) -> bool:
    """Data Worker Summary — data-rich Embed mit Fetch-Stats, Signalen und Märkten → #etoro-trading.

    Wird am Ende jedes Data-Worker-Runs aufgerufen (alle 5min).
    """
    if new_signals is None:
        new_signals = []

    # ── Color based on activity ─────────────────────────────────────────────
    if signals_generated > 0:
        color = COLOR_GREEN
    elif symbols_fetched < total_symbols * 0.5:
        color = COLOR_ORANGE
    else:
        color = COLOR_BLUE

    # ── Description: Fetch summary + timing ─────────────────────────────────
    fetch_pct = (symbols_fetched / total_symbols * 100) if total_symbols else 0
    desc_parts = [
        f"OHLCV geladen: **{symbols_fetched}/{total_symbols}** ({fetch_pct:.0f}%)",
        f"Dauer: **{elapsed_s:.1f}s**",
    ]
    if market_status:
        desc_parts.append(f"Märkte: {market_status}")
    desc = " · ".join(desc_parts)

    # ── Fields ──────────────────────────────────────────────────────────────
    fields = []

    # 1) Data Pipeline Stats
    pipeline_lines = [
        f"Tier 1 (Portfolio): **{tier1_count}** Symbole",
        f"Tier 2 (Watchlist): **{tier2_open}** offen / {tier2_closed} closed ({tier2_total} total)",
        f"Failed-Cache: **{failed_cache_size}** Symbole (cooldown 7d)",
    ]
    fields.append({
        "name": "📡 Data Pipeline",
        "value": "\n".join(pipeline_lines),
        "inline": True,
    })

    # 2) Signals
    signal_lines = [
        f"Neue Signale: **{signals_generated}**",
        f"Expired: **{signals_expired}**",
    ]
    fields.append({
        "name": "📊 Signale",
        "value": "\n".join(signal_lines),
        "inline": True,
    })

    # 3) New Signals Detail (if any)
    if new_signals:
        sig_lines = []
        for s in new_signals[:8]:
            sym = s.get("symbol", "?")
            direction = s.get("direction", "?").upper()
            score = s.get("score", 0)
            conviction = s.get("conviction", "?")
            rsi = s.get("rsi")
            dir_emoji = "🟢" if direction == "BUY" else "🔴"
            line = f"{dir_emoji} **{sym}** {direction} (Score: {score:.0f}, {conviction})"
            if rsi is not None:
                line += f" | RSI: {rsi:.1f}"
            sig_lines.append(line)
        fields.append({
            "name": "🎯 Neue Signale",
            "value": "\n".join(sig_lines),
            "inline": False,
        })

    embed = {
        "title":       f"📡 Data Worker — {symbols_fetched} Symbole geladen" + (f" ({signals_generated} Signale)" if signals_generated > 0 else ""),
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Data Worker · alle 5min"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P8 Data Worker gepostet fetched={symbols_fetched} signals={signals_generated}")
    return ok


def post_reconciler_embed(
    equity: float,
    peak_equity: float,
    position_count: int,
    synced_count: int,
    orphan_count: int,
    trades_closed: int,
    regime: str,
    drawdown_pct: float = 0.0,
    available_cash: float = 0.0,
    positions_summary: list = None,
    dry_run: bool = False,
) -> bool:
    """Reconciler Summary — data-rich Embed mit Equity, Positionen und Regime → #etoro-trading.

    Wird am Ende jedes Reconciler-Runs aufgerufen (alle 5min).
    """
    if positions_summary is None:
        positions_summary = []

    # ── Color based on regime + drawdown ──────────────────────────────────────
    if regime in ("CRITICAL",):
        color = COLOR_RED
    elif regime in ("DRAWDOWN", "CAUTION"):
        color = COLOR_ORANGE
    else:
        color = COLOR_GREEN

    # ── Description: Portfolio overview ───────────────────────────────────────
    desc_parts = [
        f"Equity: **${equity:,.2f}**",
        f"Peak: **${peak_equity:,.2f}**",
        f"Cash: **${available_cash:,.2f}**",
    ]
    if drawdown_pct > 0.5:
        desc_parts.append(f"Drawdown: **-{drawdown_pct:.1f}%**")
    desc = " · ".join(desc_parts)

    # ── Fields ────────────────────────────────────────────────────────────────
    fields = []

    # 1) Sync Stats
    sync_lines = [
        f"Positionen synchronisiert: **{synced_count}**",
        f"Orphans entfernt: **{orphan_count}**",
        f"Trades geschlossen: **{trades_closed}**",
    ]
    fields.append({
        "name": "🔄 Sync",
        "value": "\n".join(sync_lines),
        "inline": True,
    })

    # 2) Regime & Risk
    regime_emoji = {"NORMAL": "🟢", "CAUTION": "🟡", "DRAWDOWN": "🟠", "CRITICAL": "🔴"}.get(regime, "⚪")
    risk_lines = [
        f"Regime: {regime_emoji} **{regime}**",
        f"Aktive Positionen: **{position_count}**",
    ]
    if drawdown_pct > 0.5:
        risk_lines.append(f"Drawdown seit Peak: **-{drawdown_pct:.1f}%**")
    fields.append({
        "name": "⚠️ Regime & Risiko",
        "value": "\n".join(risk_lines),
        "inline": True,
    })

    # 3) Positions detail (top 8)
    if positions_summary:
        pos_lines = []
        for p in positions_summary[:8]:
            sym = p.get("symbol", "?")
            amount = p.get("amount_usd", 0) or 0
            pnl_pct = p.get("unrealized_pnl_pct")
            sl_rate = p.get("stop_loss_rate")
            no_sl = p.get("is_no_stop_loss", 0)

            emoji = "🟢" if (pnl_pct is not None and pnl_pct >= 0) else "🔴"
            line = f"{emoji} **{sym}** ${amount:,.2f}"
            if pnl_pct is not None:
                line += f" ({pnl_pct:+.1f}%)"
            if no_sl:
                line += " ⚠️ No SL"
            elif sl_rate:
                line += f" | SL: ${sl_rate:,.2f}"
            pos_lines.append(line)

        fields.append({
            "name": "💼 Positionen",
            "value": "\n".join(pos_lines),
            "inline": False,
        })

    embed = {
        "title":       f"🔄 Reconciler — ${equity:,.2f}",
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Reconciler · alle 5min"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P2 Reconciler gepostet equity={equity:.2f} regime={regime}")
    return ok


def post_data_ingestion_embed(
    results: list,
    dry_run: bool = False,
) -> bool:
    """Data Ingestion Status — yfinance Update mit konkreten Zahlen.

    `results` — Liste von {instrument, status, rows, rsi, error?}
                (fetch_price_data() Rückgaben)

    NUR posten wenn mindestens ein Instrument aktualisiert wurde
    (nicht bei 'skipped_recent').
    """
    if not results:
        return True

    # Filtere nur echte Updates (kein skipped_recent)
    updates = [r for r in results if r.get("status") != "skipped_recent"]
    if not updates:
        logger.debug("[discord_embeds] Data Ingestion: alle skipped — kein Embed")
        return True

    success = [r for r in updates if r.get("status") == "fetched"]
    errors  = [r for r in updates if r.get("status") == "error"]
    no_data = [r for r in updates if r.get("status") == "no_data"]

    total_rows = sum(r.get("rows", 0) for r in success)

    # Build instrument lines — compact format
    def _fmt_instruments(items, show_rsi=True):
        lines = []
        for r in items[:12]:
            sym = resolve_instrument_display(r.get("instrument") or r.get("instrument_id") or "?")
            if show_rsi and r.get("status") == "fetched":
                rsi = r.get("rsi")
                rsi_str = f"RSI={rsi:.1f}" if rsi is not None else "RSI=N/A"
                rows = r.get("rows", 0)
                lines.append(f"✅ **{sym}** — {rows} rows | {rsi_str}")
            elif r.get("status") == "error":
                err = (r.get("error") or "Unknown")[:40]
                lines.append(f"❌ **{sym}** — {err}")
            else:
                lines.append(f"⚠️  **{sym}** — keine Daten")
        return "\n".join(lines)

    # Determine color based on success rate
    if errors or no_data:
        color = COLOR_ORANGE
    else:
        color = COLOR_BLUE

    fields = []

    # Summary field
    summary_lines = [
        f"Instrumente aktualisiert: **{len(success)}** / {len(updates)}",
        f"Gesamt-Rows geladen: **{total_rows}**",
    ]
    if errors:
        summary_lines.append(f"Fehler: **{len(errors)}**")
    if no_data:
        summary_lines.append(f"Keine Daten: **{len(no_data)}**")

    fields.append({
        "name":   "📊 Zusammenfassung",
        "value":  "\n".join(summary_lines),
        "inline": False,
    })

    # Success details
    if success:
        fields.append({
            "name":   "✅ Geladene Instrumente",
            "value":  _fmt_instruments(success),
            "inline": False,
        })

    # Error details
    if errors or no_data:
        error_items = errors + no_data
        fields.append({
            "name":   "⚠️ Probleme",
            "value":  _fmt_instruments(error_items, show_rsi=False),
            "inline": False,
        })

    embed = {
        "title":       f"📥 Data Ingestion — {len(success)} Instrumente aktualisiert",
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · yfinance · alle 15min"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds",
                          f"P7 Data Ingestion gepostet success={len(success)} errors={len(errors)}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P8 — API METRICS DASHBOARD (T8)
# ═══════════════════════════════════════════════════════════════════════════════

def post_metrics_embed(
    api_stats: dict,
    endpoint_stats: dict,
    cb_status: str = "N/A",
    dry_run: bool = False,
) -> bool:
    """API Metrics Dashboard — Latency, Error Rates, CircuitBreaker Status.

    `api_stats` — from APIMetrics.get_stats(): {total, p50, p95, p99, error_rate_429, ...}
    `endpoint_stats` — from APIMetrics.get_per_endpoint(): {endpoint: {count, p50, p95, errors}}
    `cb_status` — CircuitBreaker state string
    """
    total = api_stats.get("total", 0)
    p50 = api_stats.get("p50", 0)
    p95 = api_stats.get("p95", 0)
    p99 = api_stats.get("p99", 0)
    e429 = api_stats.get("error_rate_429", 0)
    e5xx = api_stats.get("error_rate_5xx", 0)

    # Color based on p95 latency
    if p95 > 500:
        color = COLOR_RED
    elif p95 > 200:
        color = COLOR_YELLOW
    else:
        color = COLOR_GREEN

    # Latency field
    lat_lines = [
        f"p50: **{p50}ms**",
        f"p95: **{p95}ms** {'🔴' if p95 > 500 else ('🟡' if p95 > 200 else '✅')}",
        f"p99: **{p99}ms**",
    ]

    # Error rates
    err_lines = [
        f"429 (Rate Limit): **{e429}%** {'🔴' if e429 > 10 else '✅'}",
        f"5xx (Server):     **{e5xx}%** {'🔴' if e5xx > 5 else '✅'}",
    ]

    # Per-endpoint breakdown (top 6 by count)
    ep_lines = []
    sorted_eps = sorted(endpoint_stats.items(), key=lambda x: x[1]["count"], reverse=True)[:6]
    for ep, stats in sorted_eps:
        p95_ep = stats.get("p95", 0)
        e429_ep = stats.get("errors_429", 0)
        e5xx_ep = stats.get("errors_5xx", 0)
        errors_str = ""
        if e429_ep:
            errors_str += f" 429×{e429_ep}"
        if e5xx_ep:
            errors_str += f" 5xx×{e5xx_ep}"
        ep_lines.append(f"`{ep}` — {stats['count']} calls | p95={p95_ep}ms{errors_str}")

    embed = {
        "title":       f"📊 API Metrics Dashboard ({total} samples)",
        "color":       color,
        "fields": [
            {
                "name":   "⏱️ Latency",
                "value":  "\n".join(lat_lines),
                "inline": True,
            },
            {
                "name":   "❌ Error Rates",
                "value":  "\n".join(err_lines),
                "inline": True,
            },
            {
                "name":   f"🔌 CircuitBreaker: `{cb_status}`",
                "value":  "✅ Inaktiv" if cb_status in ("CLOSED", "N/A") else f"⚠️ {cb_status}",
                "inline": True,
            },
        ],
        "footer":      {"text": "eToro RoBoCop · API Metrics · alle 30min"},
        "timestamp":   _ts(),
    }

    # Add endpoint breakdown if there's data
    if ep_lines:
        embed["fields"].append({
            "name":   "📡 Top Endpoints",
            "value":  "\n".join(ep_lines),
            "inline": False,
        })

    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P8 Metrics gepostet samples={total}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P9 — CASH DISCREPANCY ALERT (T8.3)
# ═══════════════════════════════════════════════════════════════════════════════

def post_cash_discrepancy_embed(
    api_equity: float,
    db_equity: float,
    gap: float,
    dry_run: bool = False,
) -> bool:
    """Cash/Equity Discrepancy Alert — wenn Gap > $10.

    `api_equity` — Equity von eToro API
    `db_equity`  — Equity aus lokaler DB
    `gap`        — Differenz (api - db), positiv = API höher
    """
    gap_pct = (gap / db_equity * 100) if db_equity else 0

    embed = {
        "title":       f"💰 Cash Discrepancy Alert — ${gap:+,.2f} ({gap_pct:+.2f}%)",
        "description": "API- und DB-Equity weichen signifikant voneinander ab.",
        "color":       COLOR_RED if abs(gap) > 50 else COLOR_ORANGE,
        "fields": [
            {
                "name":   "📊 Vergleich",
                "value":  (
                    f"API Equity: **${api_equity:,.2f}**\n"
                    f"DB Equity:  **${db_equity:,.2f}**\n"
                    f"Gap:        **${gap:+,.2f}** ({gap_pct:+.2f}%)"
                ),
                "inline": True,
            },
            {
                "name":   "🔧 Aktion",
                "value":  (
                    "Reconciliation wird automatisch ausgelöst.\n"
                    "Bei wiederholtem Gap → manuelle Prüfung empfohlen."
                ),
                "inline": True,
            },
        ],
        "footer":      {"text": "eToro RoBoCop · Cash Discrepancy Alert"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("WARN", "discord_embeds",
                          f"P9 Cash Discrepancy Alert: gap=${gap:+.2f} ({gap_pct:+.2f}%)")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P10 — SYSTEM ALERT (Pipeline Watchdog, Regime-Wechsel, kritische Fehler)
# ═══════════════════════════════════════════════════════════════════════════════

def post_alert_embed(
    title: str,
    description: str,
    severity: str = "WARNING",   # "INFO" | "WARNING" | "CRITICAL"
    fields: list = None,
    channel: str = "main",       # "main" | "trades"
    dry_run: bool = False,
) -> bool:
    """Generisches System-Alert Embed — #etoro-trading (default) oder #etoro-trades.

    Wird genutzt von:
      - pipeline_watchdog.py  (Pipeline stalled / Mutex stale)
      - unified_pipeline.py   (Regime-Transition, CB-Aktivierung)
      - reconciler_service.py (Ghost / Orphan Orders)
      - Beliebige kritische Ereignisse
    """
    color_map = {
        "INFO":     COLOR_BLUE,
        "WARNING":  COLOR_ORANGE,
        "CRITICAL": COLOR_RED,
    }
    emoji_map = {
        "INFO":     "ℹ️",
        "WARNING":  "⚠️",
        "CRITICAL": "🚨",
    }
    sev = severity.upper()
    color  = color_map.get(sev, COLOR_ORANGE)
    emoji  = emoji_map.get(sev, "⚠️")
    ch_id  = DISCORD_TRADE_CHANNEL if channel == "trades" else DISCORD_MAIN_CHANNEL

    embed = {
        "title":       f"{emoji} {title}",
        "description": description,
        "color":       color,
        "fields":      fields or [],
        "footer":      {"text": f"eToro RoBoCop · System Alert · {sev}"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, ch_id, dry_run)
    if ok:
        log_level = "WARN" if sev == "WARNING" else ("CRITICAL" if sev == "CRITICAL" else "INFO")
        insert_system_log(log_level, "discord_embeds", f"P10 Alert: {title[:120]}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P11 — TRADE EXECUTION (BUY / SELL FILLED oder FAILED)
# ═══════════════════════════════════════════════════════════════════════════════

def post_trade_filled_embed(
    symbol: str,
    direction: str,          # "BUY" | "SELL" | "CLOSE"
    amount_usd: float,
    position_id: str = "",
    entry_price: float = 0.0,
    sl_price: float = 0.0,
    sl_pct: float = 0.0,
    reason: str = "",
    equity: float = 0.0,
    dry_run: bool = False,
) -> bool:
    """Trade FILLED Embed → #etoro-trades.

    Wird gepostet wenn ein BUY/SELL/CLOSE von eToro bestätigt wurde.
    """
    if direction.upper() == "BUY":
        color  = COLOR_GREEN
        emoji  = "✅"
        action = "BUY FILLED"
    elif direction.upper() in ("SELL", "CLOSE"):
        color  = COLOR_TEAL
        emoji  = "💰"
        action = f"{direction.upper()} FILLED"
    else:
        color  = COLOR_BLUE
        emoji  = "✅"
        action = f"{direction.upper()} FILLED"

    fields = [
        {"name": "💵 Betrag",    "value": f"`${amount_usd:,.2f}`",          "inline": True},
        {"name": "📈 Kurs",      "value": f"`${entry_price:,.4f}`" if entry_price else "`–`", "inline": True},
        {"name": "🛡️ Stop-Loss", "value": f"`${sl_price:,.4f}` ({sl_pct:.1f}%)" if sl_price else "`–`", "inline": True},
    ]
    if position_id:
        fields.append({"name": "🆔 Position-ID", "value": f"`{position_id}`", "inline": True})
    if equity:
        fields.append({"name": "💼 Equity", "value": f"`${equity:,.2f}`", "inline": True})
    if reason:
        fields.append({"name": "📋 Grund", "value": f"`{reason[:100]}`", "inline": False})

    embed = {
        "title":       f"{emoji} {action} — {resolve_instrument_display(symbol)}",
        "description": f"Order erfolgreich ausgeführt",
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Trade Execution"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_TRADE_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds",
                          f"P11 Trade FILLED: {direction} {symbol} ${amount_usd:.2f}")
    return ok


def post_trade_failed_embed(
    symbol: str,
    direction: str,
    amount_usd: float,
    error: str = "",
    reason: str = "",
    is_ghost: bool = False,
    dry_run: bool = False,
) -> bool:
    """Trade FAILED / GHOST Embed → #etoro-trades.

    Wird gepostet wenn ein BUY/SELL von eToro abgelehnt wurde oder als Ghost erkannt.
    """
    display = resolve_instrument_display(symbol)
    if is_ghost:
        color  = COLOR_PURPLE
        emoji  = "👻"
        title  = f"GHOST ORDER — {display}"
        desc   = "Order von eToro akzeptiert, aber keine Position erstellt"
    else:
        color  = COLOR_RED
        emoji  = "❌"
        title  = f"TRADE FAILED — {display}"
        desc   = "Order konnte nicht ausgeführt werden"

    fields = [
        {"name": "📋 Richtung", "value": f"`{direction.upper()}`",    "inline": True},
        {"name": "💵 Betrag",   "value": f"`${amount_usd:,.2f}`",     "inline": True},
    ]
    if error:
        fields.append({"name": "🔴 Fehler", "value": f"```{error[:200]}```", "inline": False})
    if reason:
        fields.append({"name": "📝 Grund",  "value": f"`{reason[:100]}`",    "inline": False})

    embed = {
        "title":       f"{emoji} {title}",
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Trade Failure"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_TRADE_CHANNEL, dry_run)
    if ok:
        level = "WARN" if is_ghost else "ERROR"
        insert_system_log(level, "discord_embeds",
                          f"P11 Trade {'GHOST' if is_ghost else 'FAILED'}: {direction} {symbol} ${amount_usd:.2f}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P12 — REGIME-WECHSEL (DRAWDOWN / RECOVERY / NORMAL)
# ═══════════════════════════════════════════════════════════════════════════════

def post_regime_change_embed(
    old_regime: str,
    new_regime: str,
    drawdown_pct: float,
    equity: float,
    peak_equity: float,
    reason: str = "",
    dry_run: bool = False,
) -> bool:
    """Regime-Transition Embed → #etoro-trading.

    Wird gepostet wenn das System zwischen NORMAL / DRAWDOWN / RECOVERY wechselt.
    """
    if new_regime == "DRAWDOWN":
        color  = COLOR_RED
        emoji  = "🔴"
        action = "DRAWDOWN-Regime aktiviert — BUYs blockiert"
    elif new_regime == "RECOVERY":
        color  = COLOR_YELLOW
        emoji  = "🟡"
        action = "RECOVERY-Regime — defensiver Betrieb"
    elif new_regime == "NORMAL":
        color  = COLOR_GREEN
        emoji  = "🟢"
        action = "NORMAL-Regime — Trading freigegeben"
    else:
        color  = COLOR_GREY
        emoji  = "⚪"
        action = f"Regime: {new_regime}"

    pnl_total = equity - peak_equity
    pnl_pct   = (pnl_total / peak_equity * 100) if peak_equity > 0 else 0.0

    fields = [
        {"name": "📊 Drawdown",    "value": f"`{drawdown_pct:.2f}%`",         "inline": True},
        {"name": "💼 Equity",      "value": f"`${equity:,.2f}`",               "inline": True},
        {"name": "🏔️ Peak",        "value": f"`${peak_equity:,.2f}`",          "inline": True},
        {"name": "📉 PnL vs Peak", "value": f"`${pnl_total:+,.2f}` ({pnl_pct:+.1f}%)", "inline": True},
        {"name": "↩️ Vorher",      "value": f"`{old_regime}`",                 "inline": True},
        {"name": "➡️ Jetzt",       "value": f"`{new_regime}`",                 "inline": True},
    ]
    if reason:
        fields.append({"name": "📋 Grund", "value": f"`{reason}`", "inline": False})

    embed = {
        "title":       f"{emoji} Regime-Wechsel: {old_regime} → {new_regime}",
        "description": action,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Adaptive Regime System"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("WARNING", "discord_embeds",
                          f"P12 Regime-Wechsel: {old_regime}→{new_regime} (DD={drawdown_pct:.2f}%)")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P13 — PIPELINE WATCHDOG STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def post_watchdog_alert_embed(
    status: str = "",             # "STALLED" | "MUTEX_STALE" | "HEALTHY" | "MISSING" | "GHOST_ORDER_ESCALATION"
    last_tick_age_s: float = 0.0,
    last_tick: int = 0,
    details: str = "",
    dry_run: bool = False,
    # c) Ghost-order escalation params (optional — used when status="GHOST_ORDER_ESCALATION")
    alert_type: str | None = None,
    symbol: str | None = None,
    message: str | None = None,
    severity: str | None = None,
) -> bool:
    """Pipeline Watchdog Alert → #etoro-trading.

    Nur bei PROBLEMen posten (STALLED, MUTEX_STALE, MISSING, GHOST_ORDER_ESCALATION).
    Bei HEALTHY → kein Post (oder nur auf explizite Anfrage).
    """
    # c) Ghost-order escalation path
    if status == "GHOST_ORDER_ESCALATION" or alert_type == "GHOST_ORDER_ESCALATION":
        st = severity or "high"
        color  = COLOR_RED if st == "critical" else COLOR_ORANGE
        emoji  = "🚨" if st == "critical" else "⚠️"
        sym_display = resolve_instrument_display(symbol) if symbol else "?"
        title  = f"{emoji} Ghost-Order Eskalation — {sym_display}"
        desc   = message or f"{sym_display}: Multiple consecutive ghost failures detected"

        fields = []
        if symbol:
            fields.append({"name": "📌 Instrument", "value": f"`{sym_display}`", "inline": True})
        if severity:
            sev_emoji = "💀" if severity == "critical" else "⚠️"
            fields.append({"name": "🔥 Severity", "value": f"{sev_emoji} `{severity.upper()}`", "inline": True})
        if details:
            fields.append({"name": "📋 Details", "value": f"`{details[:300]}`", "inline": False})

        action_text = (
            "⚠️ Manuelle Prüfung empfohlen: Check eToro-Instrument-Status, API-Limits, Account-Restriktionen."
            if severity != "critical"
            else "💀 PERMANENT BLACKLIST — Instrument dauerhaft gesperrt bis manueller Reset via DB."
        )
        fields.append({"name": "🔧 Aktion", "value": action_text, "inline": False})

        embed = {
            "title":       title,
            "description": desc,
            "color":       color,
            "fields":      fields,
            "footer":      {"text": "eToro RoBoCop · Ghost-Order Watchdog"},
            "timestamp":   _ts(),
        }

        ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
        if ok:
            insert_system_log("WARNING", "discord_embeds", f"P13 Watchdog Alert: GHOST_ORDER_ESCALATION {symbol}")
        return ok

    if status == "STALLED":
        color  = COLOR_RED
        emoji  = "🛑"
        title  = "Pipeline STALLED"
        desc   = f"Pipeline hat seit `{last_tick_age_s:.0f}s` keinen neuen Tick produziert"
    elif status == "MUTEX_STALE":
        color  = COLOR_ORANGE
        emoji  = "🔒"
        title  = "Pipeline Mutex STALE"
        desc   = "Abgestürzter Pipeline-Lauf — Mutex automatisch bereinigt"
    elif status == "MISSING":
        color  = COLOR_RED
        emoji  = "❓"
        title  = "Pipeline MISSING"
        desc   = "Kein Heartbeat gefunden — Pipeline möglicherweise nie gestartet"
    else:  # HEALTHY
        return True  # Kein Post bei gesunden Status

    fields = []
    if last_tick > 0:
        fields.append({"name": "🔢 Letzter Tick", "value": f"`#{last_tick}`", "inline": True})
    if last_tick_age_s > 0:
        fields.append({"name": "⏱️ Alter", "value": f"`{last_tick_age_s:.0f}s`", "inline": True})
    if details:
        fields.append({"name": "📋 Details", "value": f"```{details[:300]}```", "inline": False})

    fields.append({
        "name":  "🔧 Aktion",
        "value": "Watchdog prüft automatisch. Bei wiederholtem Stall → Pipeline-Neustart.",
        "inline": False,
    })

    embed = {
        "title":       f"{emoji} {title}",
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Pipeline Watchdog"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("WARNING", "discord_embeds", f"P13 Watchdog Alert: {status}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P14 — POSITION CLOSED (SL-Trigger, Konzentrations-Bereinigung, manuell)
# ═══════════════════════════════════════════════════════════════════════════════

def post_position_closed_embed(
    symbol: str,
    amount_usd: float,
    position_id: str = "",
    entry_price: float = 0.0,
    close_price: float = 0.0,
    pnl_usd: float = 0.0,
    pnl_pct: float = 0.0,
    reason: str = "",
    dry_run: bool = False,
) -> bool:
    """Position CLOSED Embed → #etoro-trades.

    Wird gepostet wenn eine Position geschlossen wird:
    - SL-Trigger (Rule 1: -3% Hard Close, -4% Emergency)
    - Konzentrationslimit-Verletzung (Rule 2)
    - Reconciler-Close (Position nicht mehr in API)
    - Manuelle Schließung
    """
    if pnl_usd >= 0:
        color = COLOR_TEAL
        emoji = "💰"
        result = f"Gewinn: **${pnl_usd:+.2f}**"
    else:
        color = COLOR_RED
        emoji = "🔴"
        result = f"Verlust: **${pnl_usd:+.2f}**"

    fields = [
        {"name": "💵 Betrag",    "value": f"`${amount_usd:,.2f}`",         "inline": True},
        {"name": "📊 PnL",      "value": f"`${pnl_usd:+.2f}` ({pnl_pct:+.1f}%)" if pnl_pct else f"`${pnl_usd:+.2f}`", "inline": True},
        {"name": "📋 Grund",    "value": f"`{reason[:80]}`" if reason else "`–`", "inline": False},
    ]
    if entry_price:
        fields.append({"name": "📈 Entry", "value": f"`${entry_price:,.4f}`", "inline": True})
    if close_price:
        fields.append({"name": "📉 Close", "value": f"`${close_price:,.4f}`", "inline": True})
    if position_id:
        fields.append({"name": "🆔 Position", "value": f"`{position_id}`", "inline": True})

    embed = {
        "title":       f"{emoji} POSITION CLOSED — {resolve_instrument_display(symbol)}",
        "description": result,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Position Close"},
        "timestamp":   _ts(),
    }

    ok = _post_embed(embed, DISCORD_TRADE_CHANNEL, dry_run)
    if ok:
        level = "INFO" if pnl_usd >= 0 else "WARN"
        insert_system_log(level, "discord_embeds",
                          f"P14 Position Closed: {symbol} ${amount_usd:.2f} PnL=${pnl_usd:+.2f}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P15 — KILL SWITCH ALERT
# ═══════════════════════════════════════════════════════════════════════════════

def post_kill_switch_embed(
    reason: str = 'Manual kill switch',
    dry_run: bool = False,
) -> bool:
    """Kill Switch Aktivierungs-Alert → #etoro-trading (MAIN).

    Wird gepostet wenn data/kill_switch.flag erstellt wird (Source of Truth:
    bot.core.kill_switch — NICHT /tmp, das WSL beim Reboot leert).
    Blockiert alle neuen BUYs durch Erzwingen von CRITICAL-Regime.
    """
    embed = {
        "title":       "🔴 KILL SWITCH AKTIVIERT — eToro Bot gestoppt",
        "description": (
            "Der Kill Switch ist aktiv. **Alle neuen BUYs sind blockiert.**\n"
            "Bestehende Positionen werden weiterhin per SL überwacht.\n\n"
            f"**Deaktivieren:** `rm data/kill_switch.flag` (im Projekt-Root)"
        ),
        "color":       COLOR_RED,
        "fields": [
            {
                "name":   "📋 Grund",
                "value":  f"`{reason[:200]}`",
                "inline": False,
            },
            {
                "name":   "⚙️ Forced Regime",
                "value":  "`CRITICAL` — Nur VERY_HIGH Conviction BUYs erlaubt",
                "inline": True,
            },
            {
                "name":   "📉 Risk Scalar",
                "value":  "`0.25` (25% der normalen Positionsgrösse)",
                "inline": True,
            },
        ],
        "footer":    {"text": "eToro RoBoCop · Kill Switch V5 · data/kill_switch.flag"},
        "timestamp": _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("WARNING", "discord_embeds", f"P15 Kill Switch alert gepostet: {reason[:80]}")
    return ok


# ═══════════════════════════════════════════════════════════════════════════════
# P16 — DISCOVERY CANDIDATES (Data-Rich Ranking)
# ═══════════════════════════════════════════════════════════════════════════════

_RANK_EMOJI = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def _rsi_emoji(rsi: Optional[float]) -> str:
    if rsi is None:
        return "⚪"
    if rsi <= 30:
        return "🟢"   # oversold — Kaufzone
    if rsi >= 70:
        return "🔴"   # overbought
    return "⚪"


def _trend_str(macd_hist: Optional[float], bb_pct: Optional[float]) -> str:
    """Menschenlesbare Trend-Einschätzung aus MACD-Histogramm + Bollinger %B."""
    if macd_hist is None:
        trend = "— unklar"
    elif macd_hist > 0:
        trend = "↗️ Aufwärts (MACD+)"
    else:
        trend = "↘️ Abwärts (MACD−)"
    if bb_pct is not None:
        if bb_pct <= 0.2:
            trend += " · nahe unterem BB-Band"
        elif bb_pct >= 0.8:
            trend += " · nahe oberem BB-Band"
    return trend


def _portfolio_fit(symbol: str) -> str:
    """Portfolio-Fit: ist das Symbol bereits im Portfolio? Fail-open bei DB-Fehler."""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{_TRADING_DB_PATH}?mode=ro", uri=True, timeout=3)
        try:
            row = conn.execute(
                "SELECT SUM(amount_usd) FROM portfolio_snapshot WHERE symbol = ? COLLATE NOCASE",
                (symbol,),
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            return f"⚠️ Bereits im Portfolio (${row[0]:,.0f} Exposure)"
        return "✅ Neu — keine Überschneidung"
    except Exception:
        return "—"


def post_risk_worker_embed(
    checked: int,
    closed: int,
    regime: str,
    equity: float,
    trailing_break_evens: int = 0,
    trailing_partials: int = 0,
    trailing_errors: list = None,
    sl_warnings: int = 0,
    sell_exits_closed: int = 0,
    concentration_closed: int = 0,
    concentration_warned: int = 0,
    kill_switch_active: bool = False,
    positions_summary: list = None,
    dry_run: bool = False,
) -> bool:
    """Risk Worker Summary — data-rich Embed mit allen Risikometriken → #etoro-trading.

    Wird am Ende jedes Risk-Worker-Runs aufgerufen (alle 5min).
    Nur posten bei Events (closed > 0, trailing actions, regime change, kill switch)
    ODER als periodischer Status alle ~6 Ticks (~30min).
    """
    if positions_summary is None:
        positions_summary = []
    if trailing_errors is None:
        trailing_errors = []

    # ── Color based on regime / events ──────────────────────────────────────
    if kill_switch_active:
        color = COLOR_RED
    elif closed > 0:
        color = COLOR_ORANGE
    elif regime in ("CRITICAL", "CIRCUIT_BREAKER"):
        color = COLOR_RED
    elif regime == "CAUTION":
        color = COLOR_YELLOW
    else:
        color = COLOR_TEAL

    # ── Description: Equity + Regime ────────────────────────────────────────
    ks_badge = "🛑 KILL SWITCH AKTIV" if kill_switch_active else ""
    desc = f"Equity: **${equity:,.2f}** · Regime: **{regime}** {ks_badge}"

    # ── Fields ──────────────────────────────────────────────────────────────
    fields = []

    # 1) Risk Summary
    risk_lines = [
        f"Positionen geprüft: **{checked}**",
        f"SL geschlossen:     **{closed}**",
        f"SL Warnungen:       **{sl_warnings}**",
    ]
    if sell_exits_closed > 0:
        risk_lines.append(f"SELL-Exits:         **{sell_exits_closed}**")
    if concentration_closed > 0:
        risk_lines.append(f"Konzentration Fix:  **{concentration_closed}**")
    if concentration_warned > 0:
        risk_lines.append(f"Konzentr. Warnung:  **{concentration_warned}**")
    fields.append({
        "name": "🛡️ Risiko-Status",
        "value": "\n".join(risk_lines),
        "inline": True,
    })

    # 2) Trailing Stop / Profit-Taking
    trailing_lines = [
        f"Break-Evens armed: **{trailing_break_evens}**",
        f"Partial Closes:    **{trailing_partials}**",
    ]
    if trailing_errors:
        trailing_lines.append(f"⚠️ Fehler:         **{len(trailing_errors)}**")
        for err in trailing_errors[:3]:
            trailing_lines.append(f"  • {str(err)[:100]}")
    fields.append({
        "name": "📈 Trailing Stop",
        "value": "\n".join(trailing_lines),
        "inline": True,
    })

    # 3) Positionen Overview (Top PnL)
    if positions_summary:
        pos_lines = []
        for p in positions_summary[:8]:
            sym = p.get("symbol", "?")
            pnl = p.get("pnl_pct", 0.0)
            amt = p.get("amount_usd", 0.0)
            emoji = _pnl_emoji(pnl)
            trailing = p.get("trailing_status", "")
            line = f"{emoji} **{sym}** {pnl:+.1f}% (${amt:,.0f})"
            if trailing:
                line += f" — {trailing}"
            pos_lines.append(line)
        fields.append({
            "name": "💼 Positionen",
            "value": "\n".join(pos_lines),
            "inline": False,
        })

    embed = {
        "title":       f"🛡️ Risk Worker — {regime}" + (f" ({closed} geschlossen)" if closed > 0 else ""),
        "description": desc,
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Risk Worker · alle 5min"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds", f"P7 Risk Worker gepostet regime={regime} closed={closed}")
    return ok


def post_discovery_embed(
    candidates: list,
    scanned: int = 0,
    stored: int = 0,
    unverified: int = 0,
    elapsed_s: float = 0.0,
    dry_run: bool = False,
) -> bool:
    """Discovery-Ranking — Top-Kandidaten mit vollem Kontext → #etoro-trading.

    `candidates` — sortierte Liste von Dicts (Score absteigend):
        {symbol, score, conviction, rsi, macd_hist, bb_pct, price,
         signal_types, instrument_id?}
    Pro Kandidat: Ranking, Score, Asset-Klasse, Preis, RSI, Trend,
    Begründung (Signal-Typen) und Portfolio-Fit.
    """
    if not candidates:
        logger.debug("[discord_embeds] Discovery: keine Kandidaten — kein Embed")
        return True

    top = candidates[:5]
    fields = []
    for i, c in enumerate(top):
        symbol  = c.get("symbol", "?")
        ref     = str(c.get("instrument_id") or symbol)
        row     = _lookup_instrument(ref)
        display = _format_instrument_display(ref, row)
        asset   = ((row or {}).get("asset_class") or "").capitalize() or "—"

        score = c.get("score", 0)
        conv  = c.get("conviction", "?")
        rsi   = c.get("rsi")
        price = c.get("price")

        rsi_str   = f"{rsi:.1f} {_rsi_emoji(rsi)}" if rsi is not None else "N/A"
        price_str = f"${price:,.2f}" if price else "N/A"
        types     = c.get("signal_types") or []
        reason    = ", ".join(types) if isinstance(types, (list, tuple)) else str(types)

        value = (
            f"📊 Score: **{score:.0f}** ({conv}) · 🏷️ {asset}\n"
            f"💵 Preis: {price_str} · RSI: {rsi_str}\n"
            f"📈 Trend: {_trend_str(c.get('macd_hist'), c.get('bb_pct'))}\n"
            f"📋 Begründung: {reason[:120] or '—'}\n"
            f"💼 Portfolio-Fit: {_portfolio_fit(symbol)}"
        )
        rank = _RANK_EMOJI[i] if i < len(_RANK_EMOJI) else f"#{i + 1}"
        fields.append({"name": f"{rank} {display}", "value": value, "inline": False})

    desc_parts = []
    if scanned:
        desc_parts.append(f"Gescannt: **{scanned}** Symbole")
    desc_parts.append(f"Kandidaten: **{len(candidates)}**")
    if stored:
        desc_parts.append(f"Gespeichert: **{stored}** Signale")
    if unverified:
        desc_parts.append(f"⚠️ Unverifiziert: **{unverified}**")
    if elapsed_s:
        desc_parts.append(f"Dauer: {elapsed_s:.0f}s")

    embed = {
        "title":       f"🔍 Discovery — Top {len(top)} Kandidaten",
        "description": " · ".join(desc_parts),
        "color":       COLOR_BLUE,
        "fields":      fields,
        "footer":      {"text": "eToro RoBoCop · Discovery · alle 2h"},
        "timestamp":   _ts(),
    }
    ok = _post_embed(embed, DISCORD_MAIN_CHANNEL, dry_run)
    if ok:
        insert_system_log("INFO", "discord_embeds",
                          f"P16 Discovery gepostet candidates={len(candidates)} stored={stored}")
    return ok

