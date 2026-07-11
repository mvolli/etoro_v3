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
        cur.execute("""
            SELECT ps.instrument_id, ps.symbol,
                   COALESCE(ps.unrealized_pnl_pct, 0.0) as pnl_pct,
                   ps.amount_usd, ps.open_price, ps.current_price, ps.last_synced,
                   COALESCE(pos_state.peak_pnl_pct, 0.0) as peak_pnl_pct,
                   COALESCE(pos_state.strategy, 'swing') as strategy,
                   COALESCE(pos_state.be_active, 0) as be_active,
                   COALESCE(pos_state.momentum_faded, 0) as momentum_faded
            FROM portfolio_snapshot ps
            LEFT JOIN position_state pos_state ON pos_state.position_id = ps.api_position_id
            ORDER BY ps.amount_usd DESC
            LIMIT 25
        """)
        open_pos = [dict(r) for r in cur.fetchall()]

        # Signal-Typ pro Position (neuester Trade per Instrument)
        for pos in open_pos:
            iid = pos.get("instrument_id")
            cur.execute("""
                SELECT s.signal_type, t.created_at
                FROM trades t
                JOIN signals s ON s.id = t.signal_id
                WHERE t.instrument_id = ?
                  AND t.status IN ('ACTIVE', 'CONFIRMED')
                ORDER BY t.created_at DESC LIMIT 1
            """, (iid,))
            row = cur.fetchone()
            pos["signal_type"] = row["signal_type"] if row else "UNBEKANNT"
            pos["opened_at"] = row["created_at"] if row else None

            # Tage gehalten
            if pos.get("opened_at"):
                try:
                    opened = datetime.fromisoformat(pos["opened_at"].replace(" ", "T"))
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=timezone.utc)
                    pos["days_held"] = (datetime.now(timezone.utc) - opened).days
                except Exception:
                    pos["days_held"] = None
            else:
                pos["days_held"] = None

        data["positions"] = open_pos

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


def _build_prompt(data: dict) -> str:
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
        pos_rows.append(
            f"  {p['symbol']}: PnL={pnl:+.1f}%{peak_part}{be_str}{faded_str}"
            f" held={days_str} signal={stype} strategy={strategy}"
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

    prompt = f"""/no_think
Du bist Trading-Risikoanalyst. Bewerte jede offene Position und gib NUR valides JSON zurück.

## Zeitpunkt
{now_str} | Regime={regime} | Equity=${equity:.0f}

## Offene Positionen
{chr(10).join(pos_rows) if pos_rows else "  Keine offenen Positionen"}

## Historische Signal-Performance (CLOSED Trades)
{chr(10).join(sig_rows) if sig_rows else "  Keine Daten verfügbar"}

## Aufgabe
Bewerte jede Position als HOLD, TIGHTEN oder EXIT:
- HOLD: Position läuft wie erwartet, weiterhalten
- TIGHTEN: Position hat Gewinn aufgebaut aber Signal-Typ hat schlechte historische Performance
  → Empfehlung: Momentum-Schutz enger einstellen (warnt vor Gewinnrückgabe)
- EXIT: Position im Minus + Signal-Typ hat nachweislich schlechte Performance (avg_pnl < -1%, win_rate < 20%)
  → Empfehlung: Position schliessen bevor weiterer Verlust entsteht

WICHTIG:
- Profitable Positionen (PnL > 0) NIEMALS als EXIT markieren — nur HOLD oder TIGHTEN
- EXIT nur für Verlustpositionen mit schlechtem Signal-Typ
- Bei < 3 Trades pro Signal-Typ: zu wenig Daten → HOLD

Antworte mit:
{{
  "evaluations": [
    {{"symbol": "XYZ", "recommendation": "HOLD", "reason": "kurze Begründung"}}
  ],
  "summary": "2-3 Sätze Gesamteinschätzung"
}}"""
    return prompt


def _call_llm(prompt: str) -> dict | None:
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


def _save_and_notify(evaluations: list[dict], summary: str) -> None:
    """Speichert Recommendations und sendet Discord-Alert für TIGHTEN/EXIT."""
    now_iso = datetime.now(timezone.utc).isoformat()[:19]
    stamped = [
        {**ev, "ts": now_iso}
        for ev in evaluations
        if isinstance(ev, dict) and ev.get("symbol")
    ]

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

        prompt = _build_prompt(data)
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
            _save_and_notify(evaluations, summary)
        else:
            print("[position_review] LLM gab keine evaluations zurück")

        return 0


if __name__ == "__main__":
    sys.exit(main())
