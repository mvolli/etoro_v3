#!/usr/bin/env python3
"""eToro Trading Bot V3 — Makro-Regime-Advisor (CLI-Wrapper).

fix/macro-fold (2026-07-15): Die Logik lebt in bot/core/macro_advisor.py und
läuft produktiv HUCKEPACK im stündlichen news_flags_worker (Alters-Trigger:
LLM_MACRO_SET_AT fehlt oder >23h alt — selbstheilend statt fixem 08:00-Cron).
Der eigene Cron-Job 6d23c9d78542 ist deaktiviert.

Diese Datei bleibt als manuelles Werkzeug: ein Aufruf erzwingt einen
sofortigen Makro-Pass (z.B. nach einem Breaking-News-Event).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("macro_regime_worker")

# Re-Export für bestehende Tests/Importe (test_llm_advisors.py)
from bot.core.macro_advisor import _clamp_scalar, run_macro_pass  # noqa: E402,F401


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


def main() -> int:
    from bot.core.worker_lock import worker_lock

    with worker_lock("macro_regime_worker") as acquired:
        if not acquired:
            print("macro_regime_worker: SKIPPED (already running)")
            return 0
        _load_env()
        from bot.db.connection import DB
        from bot.db.repo import StateRepo
        db = DB(db_path=PROJECT_ROOT / "data" / "trading.db")
        ok = run_macro_pass(StateRepo(db))
        print(f"macro_regime_worker: {'aktualisiert' if ok else 'kein Update (fail-open)'}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
