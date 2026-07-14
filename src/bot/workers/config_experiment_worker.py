#!/usr/bin/env python3
"""eToro Trading Bot V3 — Config-Experiment-Loop (fix/llm-config-experiments)

Woechentlich So 05:00. Macht aus Bauchgefuehl-Tuning einen geschlossenen
Experimentier-Loop: das LLM schlaegt EINE Parameteraenderung mit Hypothese
vor, der Bot handelt 14 Tage damit, dann wird gegen die Baseline bewertet
und behalten oder zurueckgerollt.

SICHERHEITS-DESIGN (das LLM waehlt, der CODE erzwingt):
  - Nur Parameter aus TUNABLE_PARAMS (Whitelist) mit harten Bounds —
    engere Grenzen als BIBLE_HARD_LIMITS. Unbekannte Parameter/Werte
    ausserhalb der Bounds werden verworfen bzw. geclampt.
  - Max. 1 aktives Experiment. kill_switch.py, DB-Schema, .env, BIBLE:
    unantastbar (gar nicht adressierbar — nur config.yaml-Zeilen).
  - Abbruch-Guard: Equity faellt >5% unter den Startwert → sofortiger
    Rollback (ABORTED), unabhaengig vom LLM.
  - Zu wenig Daten nach 28 Tagen → Rollback (keine Evidenz = alter Wert).
  - config.yaml-Backup vor jeder Aenderung (config.yaml.bak-experiment).

State: data/llm_config_experiments.json = {active: {...}|null, history: [...]}
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("config_experiment_worker")

WORKER_NAME = "config_experiment_worker"
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
STATE_PATH = PROJECT_ROOT / "data" / "llm_config_experiments.json"
LLM_TIMEOUT_S = 75.0

EVAL_AFTER_DAYS = 14      # fruehestens nach 14 Tagen bewerten
MAX_RUNTIME_DAYS = 28     # ohne ausreichende Daten: Rollback
MIN_TRADES_EVAL = 8       # Mindest-Trades im Experimentfenster
ABORT_EQUITY_DROP_PCT = 5.0

# Whitelist: YAML-Key (einzigartig in config.yaml!) → harte Bounds.
# line_re matcht exakt "  key: <zahl>" — max_slippage_pct matcht NICHT
# max_slippage_pct_crypto (Doppelpunkt direkt nach dem Key).
TUNABLE_PARAMS: dict[str, dict] = {
    "sl.default_pct":            {"key": "default_pct",         "min": 1.5, "max": 5.0},
    "sizing.high_pct":           {"key": "high_pct",            "min": 3.0, "max": 10.0},
    "sizing.medium_pct":         {"key": "medium_pct",          "min": 2.0, "max": 8.0},
    "trading.max_slippage_pct":  {"key": "max_slippage_pct",    "min": 1.0, "max": 3.0},
    "trading.cash_target_max_pct": {"key": "cash_target_max_pct", "min": 20.0, "max": 40.0},
}


def _load_env() -> None:
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return
    import os
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _read_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text())
    except Exception:
        pass
    return {"active": None, "history": []}


def _write_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE_PATH)


def _read_config_value(param: str) -> float | None:
    """Liest den aktuellen Zahlenwert einer Whitelist-Zeile aus config.yaml."""
    spec = TUNABLE_PARAMS.get(param)
    if not spec:
        return None
    m = re.search(rf"^(\s*{re.escape(spec['key'])}:\s*)([\d.]+)",
                  CONFIG_PATH.read_text(encoding="utf-8"), re.MULTILINE)
    return float(m.group(2)) if m else None


def _apply_config_value(param: str, new_value: float) -> bool:
    """Ersetzt den Zahlenwert der Whitelist-Zeile. Backup vorher. False wenn
    die Zeile nicht eindeutig gefunden wurde (dann passiert NICHTS)."""
    spec = TUNABLE_PARAMS.get(param)
    if not spec:
        return False
    content = CONFIG_PATH.read_text(encoding="utf-8")
    pattern = rf"^(\s*{re.escape(spec['key'])}:\s*)([\d.]+)"
    matches = re.findall(pattern, content, re.MULTILINE)
    if len(matches) != 1:
        logger.error("[%s] Key %s ist %dx in config.yaml — Abbruch (erwartet 1x)",
                     WORKER_NAME, spec["key"], len(matches))
        return False
    shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_suffix(".yaml.bak-experiment"))
    content = re.sub(pattern, rf"\g<1>{new_value}", content, count=1, flags=re.MULTILINE)
    CONFIG_PATH.write_text(content, encoding="utf-8")
    return True


def _validate_proposal(proposal: dict) -> tuple[str, float] | None:
    """Haertet den LLM-Vorschlag: Whitelist-Check, Bounds-Clamp, echte Aenderung.
    None = verwerfen."""
    param = str(proposal.get("param", ""))
    spec = TUNABLE_PARAMS.get(param)
    if spec is None:
        return None
    try:
        value = float(proposal.get("new_value"))
    except (TypeError, ValueError):
        return None
    value = round(max(spec["min"], min(spec["max"], value)), 2)
    current = _read_config_value(param)
    if current is None or abs(value - current) < 1e-9:
        return None
    return param, value


def _window_metrics(db, since_iso: str, until_iso: str | None = None) -> dict:
    """Geschlossene Trades im Fenster: n, win_rate, avg_pnl_pct, sum_pnl_usd."""
    until_clause = "AND closed_at <= ?" if until_iso else ""
    params: list = [since_iso] + ([until_iso] if until_iso else [])
    row = db.fetchone(f"""
        SELECT COUNT(*) AS n,
               AVG(CASE WHEN pnl_usd > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
               AVG(pnl_pct) AS avg_pnl_pct,
               SUM(pnl_usd) AS sum_pnl_usd
        FROM trades
        WHERE status = 'CLOSED' AND closed_at >= ? {until_clause}
    """, params)
    d = dict(row) if row else {}
    return {
        "n": int(d.get("n") or 0),
        "win_rate": round(float(d.get("win_rate") or 0), 3),
        "avg_pnl_pct": round(float(d.get("avg_pnl_pct") or 0), 3),
        "sum_pnl_usd": round(float(d.get("sum_pnl_usd") or 0), 2),
    }


def _finish(state: dict, active: dict, decision: str, note: str,
            metrics: dict | None) -> None:
    active["ended_at"] = datetime.now(timezone.utc).isoformat()
    active["decision"] = decision
    active["decision_note"] = note[:300]
    active["result_metrics"] = metrics
    state["history"].append(active)
    state["active"] = None


def main() -> int:
    from bot.core.worker_lock import worker_lock

    with worker_lock(WORKER_NAME) as acquired:
        if not acquired:
            print(f"{WORKER_NAME}: SKIPPED (already running)")
            return 0

        t0 = time.monotonic()
        _load_env()

        from bot.db.connection import DB
        from bot.db.repo import StateRepo
        from bot.core.heartbeat import record_heartbeat
        from bot.core.llm_client import call_llm_json

        db = DB(db_path=PROJECT_ROOT / "data" / "trading.db")
        state_repo = StateRepo(db)
        try:
            record_heartbeat(state_repo, WORKER_NAME)
        except Exception:
            pass

        state = _read_state()
        active = state.get("active")
        now = datetime.now(timezone.utc)
        summary = ""

        # ── Phase 1: aktives Experiment pruefen ──────────────────────────────
        if active:
            started = datetime.fromisoformat(active["started_at"])
            age_days = (now - started).total_seconds() / 86400
            equity = state_repo.get_float("CURRENT_EQUITY", 0.0)
            start_equity = float(active.get("start_equity") or 0)

            rolled_back = False
            # Abbruch-Guard: CODE entscheidet, nicht das LLM
            if start_equity > 0 and equity > 0 and \
                    equity < start_equity * (1 - ABORT_EQUITY_DROP_PCT / 100):
                _apply_config_value(active["param"], float(active["old_value"]))
                _finish(state, active, "ABORTED",
                        f"Equity {equity:.0f} < -{ABORT_EQUITY_DROP_PCT}% vom Start "
                        f"({start_equity:.0f}) — Zwangs-Rollback", None)
                summary = f"ABORTED: {active['param']} → Rollback (Equity-Guard)"
                rolled_back = True
            elif age_days >= EVAL_AFTER_DAYS:
                metrics = _window_metrics(db, active["started_at"])
                baseline = active.get("baseline", {})
                if metrics["n"] < MIN_TRADES_EVAL:
                    if age_days >= MAX_RUNTIME_DAYS:
                        _apply_config_value(active["param"], float(active["old_value"]))
                        _finish(state, active, "ROLLBACK",
                                f"Nur {metrics['n']} Trades in {age_days:.0f}d — "
                                "keine Evidenz, alter Wert gilt", metrics)
                        summary = f"ROLLBACK (zu wenig Daten): {active['param']}"
                        rolled_back = True
                    else:
                        summary = (f"Experiment laeuft weiter ({metrics['n']}/"
                                   f"{MIN_TRADES_EVAL} Trades, Tag {age_days:.0f})")
                else:
                    # Hard-Rule: klare Verschlechterung → Rollback, egal was das LLM sagt
                    delta_pnl = metrics["avg_pnl_pct"] - float(baseline.get("avg_pnl_pct") or 0)
                    if delta_pnl < -0.5:
                        verdict, note = "ROLLBACK", f"avg_pnl {delta_pnl:+.2f}pp vs Baseline (Hard-Rule)"
                    else:
                        result = call_llm_json(f"""/no_think
Du bewertest ein A/B-Experiment eines Trading-Bots. Parameter
{active['param']}: {active['old_value']} → {active['new_value']}.
Hypothese: {active.get('hypothesis','?')}

Baseline (30d davor): {json.dumps(baseline)}
Experiment ({metrics['n']} Trades, {age_days:.0f}d): {json.dumps(metrics)}

KEEP nur bei klarer oder leichter Verbesserung der avg_pnl_pct/win_rate.
Bei Verschlechterung oder unklarem Bild: ROLLBACK (konservativ).
Antworte NUR mit JSON: {{"verdict": "KEEP|ROLLBACK", "note": "kurz, deutsch"}}""",
                            max_tokens=192, timeout_s=LLM_TIMEOUT_S, label=WORKER_NAME)
                        verdict = str((result or {}).get("verdict", "ROLLBACK")).upper()
                        if verdict not in ("KEEP", "ROLLBACK"):
                            verdict = "ROLLBACK"
                        note = str((result or {}).get("note", "LLM nicht verfuegbar → konservativ"))
                    if verdict == "ROLLBACK":
                        _apply_config_value(active["param"], float(active["old_value"]))
                        rolled_back = True
                    _finish(state, active, verdict, note, metrics)
                    summary = f"{verdict}: {active['param']} — {note[:100]}"
            else:
                summary = f"Experiment Tag {age_days:.0f}/{EVAL_AFTER_DAYS} — laeuft"
            _write_state(state)
            if rolled_back:
                logger.info("[%s] Rollback angewendet: %s → %s", WORKER_NAME,
                            active["param"], active["old_value"])

        # ── Phase 2: neues Experiment vorschlagen (nur wenn keins aktiv) ─────
        if state.get("active") is None and not summary.startswith("Experiment"):
            since_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            perf_30d = _window_metrics(db, since_30d)

            current_values = {p: _read_config_value(p) for p in TUNABLE_PARAMS}
            bounds = {p: [s["min"], s["max"]] for p, s in TUNABLE_PARAMS.items()}
            recent_history = [
                {"param": h.get("param"), "decision": h.get("decision"),
                 "note": (h.get("decision_note") or "")[:80]}
                for h in state["history"][-5:]
            ]

            result = call_llm_json(f"""/no_think
Du bist Tuning-Analyst eines autonomen Trading-Bots. Schlage GENAU EINE
Parameteraenderung vor, die die 30-Tage-Performance verbessern koennte.
Kleine Schritte (max ~20% Aenderung). Waehle NUR aus der Whitelist.

## Performance letzte 30 Tage
{json.dumps(perf_30d)}

## Aktuelle Werte
{json.dumps(current_values)}

## Erlaubte Bereiche [min, max]
{json.dumps(bounds)}

## Fruehere Experimente (nicht wiederholen was ROLLBACK war)
{json.dumps(recent_history, ensure_ascii=False)}

Antworte NUR mit JSON:
{{"param": "sl.default_pct", "new_value": 3.0,
  "hypothesis": "1 Satz: warum sollte das die avg_pnl_pct verbessern"}}""",
                max_tokens=256, timeout_s=LLM_TIMEOUT_S, label=WORKER_NAME)

            validated = _validate_proposal(result or {})
            if validated is None:
                summary = summary or "Kein valider Vorschlag — kein neues Experiment"
            else:
                param, new_value = validated
                old_value = _read_config_value(param)
                if _apply_config_value(param, new_value):
                    state["active"] = {
                        "param": param,
                        "old_value": old_value,
                        "new_value": new_value,
                        "hypothesis": str((result or {}).get("hypothesis", ""))[:250],
                        "started_at": now.isoformat(),
                        "start_equity": state_repo.get_float("CURRENT_EQUITY", 0.0),
                        "baseline": perf_30d,
                    }
                    _write_state(state)
                    summary = (f"NEU: {param} {old_value} → {new_value} — "
                               f"{state['active']['hypothesis'][:100]}")

        elapsed = time.monotonic() - t0
        print(f"{WORKER_NAME}: {summary or 'nichts zu tun'} ({elapsed:.1f}s)")

        if summary and not summary.startswith("Experiment laeuft"):
            try:
                sys.path.insert(0, str(SRC_DIR / "bot"))
                import discord_embeds as _DE
                _DE.post_alert_embed(
                    title="🧪 Config-Experiment",
                    description=summary[:500],
                    severity="INFO",
                )
            except Exception:
                pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
