#!/usr/bin/env python3
"""eToro Trading Bot V3 — News/Earnings Risk-Flags Worker (fix/llm-news-flags)

Stuendlich (:15). Das System war komplett TA-blind fuer Ereignisse: es kaufte
den RSI-Dip auch dann, wenn der Dip eine laufende Untersuchung war oder morgen
Earnings anstehen. Dieser Worker holt Headlines + Earnings-Termine fuer offene
Positionen und FRESH-Signal-Kandidaten und kondensiert sie zu Risk-Flags:

  data/llm_news_flags.json = {generated_at, auto_expires_at (12h),
                              flags: {SYMBOL: {flag, severity, reason, source}}}

DESIGN-PRINZIP (asymmetrische Rechte): Flags koennen Trades nur DAEMPFEN
(AVOID = Signal ueberspringen, CAUTION = halbe Groesse) — nie boosten.
Ein halluziniertes Flag kostet eine Gelegenheit, kein Geld.

Earnings-Flags sind REGELBASIERT (kein LLM noetig): Termin binnen 2 Tagen →
AVOID. Das LLM bewertet nur die Headlines. Fail-open ueberall: kein File /
abgelaufen / LLM down → keine Flags → Verhalten wie bisher.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("news_flags_worker")

WORKER_NAME = "news_flags_worker"
FLAGS_PATH = PROJECT_ROOT / "data" / "llm_news_flags.json"
FLAGS_TTL_HOURS = 12
NEWS_SYMBOL_CAP = 20        # Headlines: Positionen zuerst, dann Kandidaten
EARNINGS_SYMBOL_CAP = 12    # Earnings-Kalender ist der teurere yf-Call
NEWS_MAX_AGE_H = 36         # aeltere Headlines ignorieren
EARNINGS_AVOID_DAYS = 2     # Earnings binnen N Tagen → AVOID
LLM_TIMEOUT_S = 60.0

VALID_FLAGS = {"AVOID", "CAUTION"}


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


def _gather_symbols(db) -> list[dict]:
    """Offene Positionen zuerst (die schuetzen wir), dann FRESH-Kandidaten."""
    rows: list[dict] = []
    seen: set[str] = set()
    for sql in (
        """SELECT DISTINCT i.symbol, i.yfinance_symbol
           FROM portfolio_snapshot ps
           JOIN instruments i ON i.instrument_id = ps.instrument_id
           WHERE i.yfinance_symbol IS NOT NULL AND i.yfinance_symbol != ''""",
        """SELECT DISTINCT i.symbol, i.yfinance_symbol
           FROM signals s
           JOIN instruments i ON i.instrument_id = s.instrument_id
           WHERE s.status = 'FRESH' AND s.expires_at > datetime('now','utc')
             AND i.yfinance_symbol IS NOT NULL AND i.yfinance_symbol != ''""",
    ):
        try:
            for r in db.fetchall(sql):
                sym = r["symbol"]
                if sym not in seen:
                    seen.add(sym)
                    rows.append({"symbol": sym, "yf": r["yfinance_symbol"]})
        except Exception as exc:
            logger.warning("[%s] Symbol-Query fehlgeschlagen: %s", WORKER_NAME, exc)
    return rows


def _extract_headline(item: dict) -> tuple[str, float]:
    """yfinance-News-Item → (title, epoch). Kennt altes und neues Format."""
    content = item.get("content") if isinstance(item.get("content"), dict) else None
    title = (content or item).get("title") or ""
    ts = item.get("providerPublishTime") or 0
    if not ts and content:
        pub = content.get("pubDate") or ""
        try:
            ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
    return title.strip(), float(ts or 0)


def _fetch_news(symbols: list[dict]) -> dict[str, list[str]]:
    """{symbol: [headline, ...]} — nur Headlines juenger als NEWS_MAX_AGE_H."""
    import yfinance as yf
    cutoff = time.time() - NEWS_MAX_AGE_H * 3600
    out: dict[str, list[str]] = {}
    for entry in symbols[:NEWS_SYMBOL_CAP]:
        try:
            items = yf.Ticker(entry["yf"]).news or []
            heads = []
            for item in items[:8]:
                title, ts = _extract_headline(item)
                if title and (ts == 0 or ts >= cutoff):
                    heads.append(title[:160])
            if heads:
                out[entry["symbol"]] = heads[:5]
        except Exception:
            pass
    return out


def _fetch_earnings_flags(symbols: list[dict]) -> dict[str, dict]:
    """Regelbasiert: Earnings-Termin binnen EARNINGS_AVOID_DAYS → AVOID."""
    import yfinance as yf
    flags: dict[str, dict] = {}
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=EARNINGS_AVOID_DAYS)
    for entry in symbols[:EARNINGS_SYMBOL_CAP]:
        try:
            cal = yf.Ticker(entry["yf"]).calendar
            dates = []
            if isinstance(cal, dict):
                dates = cal.get("Earnings Date") or []
            elif cal is not None and hasattr(cal, "loc"):  # Legacy-DataFrame
                try:
                    dates = list(cal.loc["Earnings Date"])
                except Exception:
                    dates = []
            for d in dates:
                d_date = d.date() if hasattr(d, "date") else d
                if today <= d_date <= horizon:
                    flags[entry["symbol"]] = {
                        "flag": "AVOID", "severity": "HIGH",
                        "reason": f"Earnings am {d_date.isoformat()}",
                        "source": "earnings_calendar",
                    }
                    break
        except Exception:
            pass
    return flags


def _parse_llm_flags(result: dict | None) -> dict[str, dict]:
    """Validiert die LLM-Antwort hart: nur AVOID/CAUTION, nur bekannte Felder.
    Alles andere wird verworfen (Halluzinations-Schutz)."""
    out: dict[str, dict] = {}
    if not isinstance(result, dict):
        return out
    for sym, entry in (result.get("flags") or {}).items():
        if not isinstance(entry, dict):
            continue
        flag = str(entry.get("flag", "")).upper()
        if flag not in VALID_FLAGS:
            continue
        out[str(sym)] = {
            "flag": flag,
            "severity": "HIGH" if flag == "AVOID" else "MEDIUM",
            "reason": str(entry.get("reason", ""))[:200],
            "source": "news_llm",
        }
    return out


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
        try:
            record_heartbeat(StateRepo(db), WORKER_NAME)
        except Exception:
            pass

        # Makro-Pass huckepack (fix/macro-fold, 2026-07-15): ersetzt den
        # eigenen Taeglich-08:00-Cron (Job 6d23c9d78542, disabled). Der
        # Alters-Trigger macht ihn selbstheilend: ein verpasster Lauf wird
        # im naechsten Stundenzyklus nachgeholt statt 24h-Loch (fail-open
        # 1.0 via TTL). Muss VOR db.close() laufen (DB noch offen).
        try:
            from bot.core.macro_advisor import (REFRESH_AGE_HOURS,
                                                macro_scalar_age_hours,
                                                run_macro_pass)
            _age = macro_scalar_age_hours(StateRepo(db))
            if _age is None or _age > REFRESH_AGE_HOURS:
                logger.info(
                    "[%s] Makro-Scalar %s — starte Makro-Pass",
                    WORKER_NAME,
                    "nie gesetzt" if _age is None else f"{_age:.1f}h alt",
                )
                run_macro_pass(StateRepo(db))
        except Exception as exc:
            logger.warning("[%s] Makro-Pass fehlgeschlagen: %s", WORKER_NAME, exc)

        symbols = _gather_symbols(db)
        db.close()  # yfinance-Phase ohne offene DB-Connection (Lock-Hygiene)
        if not symbols:
            print(f"{WORKER_NAME}: keine Symbole (keine Positionen/Signale)")
            return 0

        # 1. Regelbasierte Earnings-Flags (kein LLM)
        flags = _fetch_earnings_flags(symbols)

        # 2. Headlines → LLM-Bewertung (nur wenn es Headlines gibt)
        news = _fetch_news(symbols)
        if news:
            lines = [f"{sym}:\n" + "\n".join(f"  - {h}" for h in heads)
                     for sym, heads in news.items()]
            prompt = f"""/no_think
Du bist Risiko-Screener fuer einen autonomen Trading-Bot. Unten stehen aktuelle
Headlines pro Symbol. Markiere NUR Symbole mit klar NEGATIVEM Ereignisrisiko:
- AVOID: schwerwiegend (Betrugsvorwurf, Untersuchung, Gewinnwarnung, Delisting,
  Insolvenz, ueberraschender CEO-Abgang, Kurssturz-Ausloeser)
- CAUTION: erhoehte Unsicherheit (Downgrade, Klage, schwacher Ausblick)
Normale/neutrale/positive News: Symbol WEGLASSEN. Im Zweifel WEGLASSEN.

{chr(10).join(lines)}

Antworte NUR mit JSON:
{{"flags": {{"SYMBOL": {{"flag": "AVOID|CAUTION", "reason": "kurz, deutsch"}}}}}}"""
            llm_flags = _parse_llm_flags(call_llm_json(
                prompt, max_tokens=768, timeout_s=LLM_TIMEOUT_S, label=WORKER_NAME,
            ))
            # Earnings-Regel gewinnt bei Konflikt (deterministisch > LLM)
            for sym, entry in llm_flags.items():
                flags.setdefault(sym, entry)

        now = datetime.now(timezone.utc)
        payload = {
            "generated_at": now.isoformat(),
            "auto_expires_at": (now + timedelta(hours=FLAGS_TTL_HOURS)).isoformat(),
            "symbols_scanned": len(symbols),
            "headlines_seen": sum(len(v) for v in news.values()),
            "flags": flags,
        }
        FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = FLAGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        tmp.replace(FLAGS_PATH)

        elapsed = time.monotonic() - t0
        summary = (f"{WORKER_NAME}: {len(symbols)} Symbole, "
                   f"{payload['headlines_seen']} Headlines, {len(flags)} Flags, {elapsed:.1f}s")
        print(summary)

        if flags:
            try:
                sys.path.insert(0, str(SRC_DIR / "bot"))
                import discord_embeds as _DE
                _DE.post_alert_embed(
                    title=f"📰 News-Flags: {len(flags)} Symbol(e) markiert",
                    description="\n".join(
                        f"• **{s}** {f['flag']}: {f['reason'][:100]}"
                        for s, f in list(flags.items())[:8]
                    ),
                    severity="WARNING",
                )
            except Exception:
                pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
