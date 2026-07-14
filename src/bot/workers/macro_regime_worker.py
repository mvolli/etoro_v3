#!/usr/bin/env python3
"""eToro Trading Bot V3 — Forward-Regime-Advisor (fix/llm-macro-advisor)

Taeglich 08:00 CEST (vor EU-Open 09:00), AUCH am Wochenende — Crypto handelt
24/7, und ein Freitagabend-Event (Fed, Geopolitik) soll nicht erst Montag in
den Scalar einfliessen (Review 2026-07-14). Das Regime-System ist
rueckwaertsgewandt — es reagiert erst, wenn der Drawdown schon eingetreten
ist. Dieser Worker laesst das LLM den Marktzustand VORAUSSCHAUEND bewerten
(SPY/QQQ 1d+5d, VIX-Level und -Trend) und schreibt einen Daempfungsfaktor:

  system_state.LLM_MACRO_SCALAR   ∈ [0.5 .. 1.0]  (hart geclampt)
  system_state.LLM_MACRO_SET_AT   ISO-Timestamp (Konsument: TTL 26h)
  system_state.LLM_MACRO_REASON   Kurzbegruendung

Der signal_worker multipliziert buy_aggressiveness damit. NUR daempfend:
1.0 = neutral, nie >1.0 (kein LLM-getriebenes Aufdrehen). Das regelbasierte
Regime (CURRENT_REGIME/RISK_SCALAR) bleibt unangetastet — dieser Faktor
wirkt zusaetzlich und faellt bei LLM-Ausfall automatisch auf neutral zurueck
(kein State-Update → TTL laeuft ab → 1.0).
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("macro_regime_worker")

WORKER_NAME = "macro_regime_worker"
LLM_TIMEOUT_S = 60.0
SCALAR_MIN, SCALAR_MAX = 0.5, 1.0


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


def _clamp_scalar(value) -> float:
    """Harte Grenze [0.5, 1.0] — das LLM kann daempfen, nie boosten.
    Unparsbar → 1.0 (neutral, fail-open)."""
    try:
        return max(SCALAR_MIN, min(SCALAR_MAX, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _fetch_macro() -> dict:
    """SPY/QQQ 1d+5d, VIX Level + 5d-Delta."""
    ctx = {"spy_1d": None, "spy_5d": None, "qqq_1d": None, "qqq_5d": None,
           "vix": None, "vix_5d_delta": None}
    try:
        import yfinance as yf
        data = yf.download(["SPY", "QQQ", "^VIX"], period="10d", interval="1d",
                           group_by="ticker", auto_adjust=True, progress=False, threads=True)

        def _series(t):
            try:
                return data[t]["Close"].dropna()
            except Exception:
                return None

        for prefix, t in (("spy", "SPY"), ("qqq", "QQQ")):
            c = _series(t)
            if c is not None and len(c) >= 2:
                ctx[f"{prefix}_1d"] = round(float(c.iloc[-1] / c.iloc[-2] - 1) * 100, 2)
            if c is not None and len(c) >= 6:
                ctx[f"{prefix}_5d"] = round(float(c.iloc[-1] / c.iloc[-6] - 1) * 100, 2)
        v = _series("^VIX")
        if v is not None and len(v) >= 1:
            ctx["vix"] = round(float(v.iloc[-1]), 2)
        if v is not None and len(v) >= 6:
            ctx["vix_5d_delta"] = round(float(v.iloc[-1] - v.iloc[-6]), 2)
    except Exception as exc:
        logger.warning("[%s] Makro-Daten fehlgeschlagen: %s", WORKER_NAME, exc)
    return ctx


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

        macro = _fetch_macro()
        if macro["vix"] is None and macro["spy_1d"] is None:
            print(f"{WORKER_NAME}: keine Makro-Daten — kein Update (Konsument faellt auf 1.0 zurueck)")
            return 0

        regime = state_repo.get("CURRENT_REGIME") or "?"
        dd = state_repo.get("DRAWDOWN_PCT") or "?"

        prompt = f"""/no_think
Du bist Makro-Risiko-Advisor fuer einen autonomen Trading-Bot (nur Long-BUYs,
Mean-Reversion + Trend-Following, Haltedauer Tage). Bewerte das Marktumfeld
fuer die NAECHSTEN 1-2 Handelstage und gib einen Positionsgroessen-Faktor:

- 1.0  = normales Umfeld, volle Groesse
- 0.85 = leicht erhoehtes Risiko
- 0.7  = deutlich erhoehtes Risiko (VIX-Spike, breiter Abverkauf im Gang)
- 0.5  = akutes Stressumfeld (nur noch halbe Groesse)

Du kannst NUR daempfen (max 1.0). Sei nicht schreckhaft: normale Schwankungen
(SPY +-1%, VIX < 20) sind KEIN Grund unter 1.0 zu gehen.

## Daten
SPY: {macro['spy_1d']}% (1d), {macro['spy_5d']}% (5d)
QQQ: {macro['qqq_1d']}% (1d), {macro['qqq_5d']}% (5d)
VIX: {macro['vix']} (5d-Delta: {macro['vix_5d_delta']:+.1f})
Bot-Regime (rueckwaertsgewandt): {regime}, Drawdown {dd}%

Antworte NUR mit JSON:
{{"macro_scalar": 1.0, "assessment": "1-2 Saetze, deutsch"}}"""

        result = call_llm_json(prompt, max_tokens=256, temperature=0.1,
                               timeout_s=LLM_TIMEOUT_S, label=WORKER_NAME)

        if result is None:
            # Kein Update — der alte Wert laeuft per TTL (26h) im Konsumenten aus.
            print(f"{WORKER_NAME}: LLM nicht verfuegbar — kein Update (fail-open auf 1.0 via TTL)")
            return 0

        scalar = _clamp_scalar(result.get("macro_scalar"))
        reason = str(result.get("assessment", ""))[:250]
        now_iso = datetime.now(timezone.utc).isoformat()
        state_repo.set("LLM_MACRO_SCALAR", str(scalar))
        state_repo.set("LLM_MACRO_SET_AT", now_iso)
        state_repo.set("LLM_MACRO_REASON", reason)

        elapsed = time.monotonic() - t0
        print(f"{WORKER_NAME}: scalar={scalar} — {reason[:120]} ({elapsed:.1f}s)")

        if scalar < 1.0:
            try:
                sys.path.insert(0, str(SRC_DIR / "bot"))
                import discord_embeds as _DE
                _DE.post_alert_embed(
                    title=f"🌤️ Makro-Advisor: Positionsgroessen auf {int(scalar*100)}% gedaempft",
                    description=f"{reason}\n\nSPY {macro['spy_1d']}%/1d {macro['spy_5d']}%/5d | "
                                f"VIX {macro['vix']} ({macro['vix_5d_delta']:+.1f}/5d)\nTTL: 26h",
                    severity="INFO",
                )
            except Exception:
                pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
