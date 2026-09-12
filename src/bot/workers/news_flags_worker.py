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

# feat/analyst-targets (2026-07-26): Analysten-Kursziele als drittes
# regelbasiertes Kriterium. Preis DEUTLICH ueber dem Konsens-Kursziel =
# kein Aufwaertspotenzial laut Street → daempfen. Asymmetrisch wie alles
# hier: nur AVOID/CAUTION, nie Boost. Keine Abdeckung (EU/Asia-Micro-Caps
# haben oft keine) → kein Flag.
ANALYST_SYMBOL_CAP = 12
ANALYST_CAUTION_ABOVE_PCT = 5.0    # Preis > Kursziel +5%  → CAUTION
ANALYST_AVOID_ABOVE_PCT = 25.0     # Preis > Kursziel +25% → AVOID

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


def _capped(symbols: list[dict], cap: int) -> list[dict]:
    """Alle GEHALTENEN Positionen + garantierte Kandidaten-Quote.

    fix/news-coverage (2026-08-12): vorher schnitt `symbols[:CAP]` hart ab.
    Bei 54 Live-Symbolen gegen EARNINGS_SYMBOL_CAP=12 blieben 42 offene
    Positionen ungeprueft — und Earnings sind der teuerste blinde Fleck, den
    dieser Bot haben kann: ein Termin ist ein Gap-Risiko, gegen das der
    Software-Trailing-Stop (eToro hat keinen SL-Update-Endpoint) nicht
    schuetzt. Was im Depot liegt, wird seither immer geprueft.

    fix/news-candidate-floor (2026-09-12): dieselbe Zeile liess die
    Kandidaten verhungern. `budget = max(cap, len(held))` ergibt bei 51
    gehaltenen Positionen und cap=20 ein Budget von 51 — und
    `rest[:51-51]` ist LEER. Gemessen am 12.09.: Positionen 51/51 geprueft,
    Kandidaten 0/8.

    Das ist eine Umkehrung, denn die Flags wirken fast nur auf Kandidaten:
    im signal_worker heisst AVOID "Signal ueberspringen" und CAUTION "halbe
    Groesse" — beides Entscheidungen VOR dem Kauf. Die Menge mit voller
    Abdeckung konnte die Flags kaum nutzen, die Menge, die sie braucht,
    bekam keine. Kandidaten haben jetzt eine eigene, vom Depotstand
    unabhaengige Quote in Hoehe des Caps.
    """
    held = [s for s in symbols if s.get("held")]
    rest = [s for s in symbols if not s.get("held")]
    return held + rest[: max(0, int(cap))]


def _gather_symbols(db) -> list[dict]:
    """Offene Positionen zuerst (die schuetzen wir), dann FRESH-Kandidaten."""
    rows: list[dict] = []
    seen: set[str] = set()
    for idx, sql in enumerate((
        """SELECT DISTINCT i.symbol, i.yfinance_symbol
           FROM portfolio_snapshot ps
           JOIN instruments i ON i.instrument_id = ps.instrument_id
           WHERE i.yfinance_symbol IS NOT NULL AND i.yfinance_symbol != ''""",
        """SELECT DISTINCT i.symbol, i.yfinance_symbol
           FROM signals s
           JOIN instruments i ON i.instrument_id = s.instrument_id
           WHERE s.status = 'FRESH' AND s.expires_at > datetime('now')
             AND i.yfinance_symbol IS NOT NULL AND i.yfinance_symbol != ''""",
    )):
        try:
            for r in db.fetchall(sql):
                sym = r["symbol"]
                if sym not in seen:
                    seen.add(sym)
                    # idx 0 = offene Position (immer pruefen), 1 = Kandidat
                    rows.append({"symbol": sym, "yf": r["yfinance_symbol"],
                                 "held": idx == 0})
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
    for entry in _capped(symbols, NEWS_SYMBOL_CAP):
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


def _earnings_flag_for(yf_symbol: str) -> dict | None:
    """Ein Symbol: Earnings-Termin binnen EARNINGS_AVOID_DAYS → AVOID.

    Herausgezogen (feat/signal-news-pull 2026-09-12), damit der stuendliche
    Worker und der synchrone Pull im signal_worker dieselbe Regel teilen.
    """
    import yfinance as yf
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=EARNINGS_AVOID_DAYS)
    try:
        cal = yf.Ticker(yf_symbol).calendar
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
                return {
                    "flag": "AVOID", "severity": "HIGH",
                    "reason": f"Earnings am {d_date.isoformat()}",
                    "source": "earnings_calendar",
                }
    except Exception:
        pass
    return None


def _fetch_earnings_flags(symbols: list[dict]) -> dict[str, dict]:
    """Regelbasiert: Earnings-Termin binnen EARNINGS_AVOID_DAYS → AVOID."""
    flags: dict[str, dict] = {}
    for entry in _capped(symbols, EARNINGS_SYMBOL_CAP):
        flag = _earnings_flag_for(entry["yf"])
        if flag:
            flags[entry["symbol"]] = flag
    return flags


def _evaluate_analyst_target(current: float | None, mean_target: float | None) -> dict | None:
    """Pure Bewertungslogik: Preis vs. Konsens-Kursziel → Flag-Dict oder None."""
    if not current or not mean_target or current <= 0 or mean_target <= 0:
        return None
    above_pct = (current / mean_target - 1.0) * 100.0
    if above_pct > ANALYST_AVOID_ABOVE_PCT:
        return {
            "flag": "AVOID", "severity": "HIGH",
            "reason": f"Preis {above_pct:.0f}% ueber Analysten-Kursziel ({mean_target:.2f})",
            "source": "analyst_target",
        }
    if above_pct > ANALYST_CAUTION_ABOVE_PCT:
        return {
            "flag": "CAUTION", "severity": "MEDIUM",
            "reason": f"Preis {above_pct:.0f}% ueber Analysten-Kursziel ({mean_target:.2f})",
            "source": "analyst_target",
        }
    return None


def _fetch_analyst_flags(symbols: list[dict]) -> dict[str, dict]:
    """Regelbasiert: Preis deutlich ueber Konsens-Kursziel → CAUTION/AVOID.

    yfinance analyst_price_targets liefert {'current', 'mean', ...} in einem
    Call — kein separater Preis-Fetch noetig. Fail-open pro Symbol.
    """
    flags: dict[str, dict] = {}
    for entry in _capped(symbols, ANALYST_SYMBOL_CAP):
        flag = _analyst_flag_for(entry["yf"])
        if flag:
            flags[entry["symbol"]] = flag
    return flags


def _analyst_flag_for(yf_symbol: str) -> dict | None:
    """Ein Symbol: Preis vs. Konsens-Kursziel. Fail-open auf None."""
    import yfinance as yf
    try:
        targets = yf.Ticker(yf_symbol).analyst_price_targets or {}
        return _evaluate_analyst_target(targets.get("current"), targets.get("mean"))
    except Exception:
        return None


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

        # 1b. Regelbasierte Analysten-Kursziel-Flags (feat/analyst-targets).
        # Earnings-Flag gewinnt bei Konflikt (setdefault).
        for sym, entry in _fetch_analyst_flags(symbols).items():
            flags.setdefault(sym, entry)

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
        try:
            from bot.core.heartbeat import record_duration as _rd
            # fix/duration-closed-db (2026-07-26): db wurde oben geschlossen —
            # der Record lief seit je ins Leere (still geschlucktes Except).
            _db2 = DB(db_path=PROJECT_ROOT / "data" / "trading.db")
            _rd(StateRepo(_db2), WORKER_NAME, elapsed)
            _db2.close()
        except Exception:
            pass
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


# ── feat/signal-news-pull (2026-09-12) ───────────────────────────────────────
# Gemessen: 93,4 % der 211 Epochen-Trades liefen auf Signalen, die NACH dem
# letzten stuendlichen News-Lauf geboren wurden. Der Abstand von Signalgeburt
# zu Freigabe betraegt im Schnitt 3,0 Minuten — Signal und Kauf fallen in
# denselben 15-Minuten-Zyklus. Der stuendliche Worker kann ein Symbol vor
# seinem ersten Kauf strukturell nicht sehen; die Kandidatenquote aus
# fix/news-candidate-floor hilft nur den 6,6 %, die eine Stunde ueberleben.
#
# Deshalb ein synchroner Pull direkt im signal_worker, fuer die <= 5
# Kandidaten eines Zyklus. Bewusst NUR die regelbasierten Kriterien
# (Earnings-Termin, Analysten-Kursziel): sie sind deterministisch und
# brauchen keinen LLM-Round-Trip. Die Headline-Bewertung bleibt beim
# stuendlichen Worker.

FLAG_RANG = {"AVOID": 2, "CAUTION": 1}

# Nur diese Anlageklassen haben ueberhaupt Earnings-Termine und
# Analysten-Kursziele. Fuer Krypto/Rohstoff/Index/Forex liefert yfinance
# ein 404 auf die Fundamentaldaten — gemessen am 12.09.: von 5 Kandidaten
# waren 2 Krypto, das waren 4 verschwendete Netzabrufe und vier
# ERROR-Zeilen aus yfinance im Log. Der Pull laeuft im 180-s-Fenster vor
# dem Execution-Slot; verschwendete Calls sind dort teurer als anderswo.
FUNDAMENTAL_KLASSEN = frozenset({"stock", "etf"})


def staerkeres_flag(alt: dict | None, neu: dict | None) -> dict | None:
    """Verschmelzung: nur verschaerfen, nie abschwaechen.

    Ein frisches Flag aus dem stuendlichen Lauf darf durch einen
    fehlgeschlagenen oder leeren Pull nicht verlorengehen — und umgekehrt.
    Bei Gleichstand gewinnt das bestehende (der stuendliche Lauf kennt
    zusaetzlich die Headline-Bewertung).
    """
    if not neu:
        return alt
    if not alt:
        return neu
    return neu if FLAG_RANG.get(neu.get("flag"), 0) > FLAG_RANG.get(alt.get("flag"), 0) else alt


def pull_regel_flags(
    entries: list[dict],
    budget_s: float = 45.0,
    _earnings=None,
    _analyst=None,
) -> tuple[dict[str, dict], bool]:
    """Synchroner, regelbasierter Flag-Pull fuer wenige Symbole.

    `entries`: [{"symbol": ..., "yf": ..., "asset_class": ...}, ...] — der
    Aufrufer waehlt aus, hier wird NICHT zusaetzlich gedeckelt. Eintraege
    mit einer asset_class ausserhalb von FUNDAMENTAL_KLASSEN werden
    uebersprungen; fehlt das Feld, wird geprueft (fail-open).

    `budget_s` ist ein harter Wall-Clock-Deckel ueber den GESAMTEN Pull,
    geprueft zwischen den Symbolen. Ein try/except je Symbol begrenzt die
    Gesamtlatenz nicht — eine haengende yfinance-Verbindung sitzt 30 s, und
    der signal_worker hat bis zum Execution-Slot nur 180 s (davon ~40 s
    bereits verbraucht).

    Rueckgabe: ({symbol: flag}, abgebrochen). `abgebrochen` sagt, ob das
    Budget gerissen wurde — der Aufrufer soll das protokollieren koennen,
    damit ein stiller Teil-Pull sichtbar bleibt.
    """
    earnings_fn = _earnings or _earnings_flag_for
    analyst_fn = _analyst or _analyst_flag_for
    flags: dict[str, dict] = {}
    if not entries:
        return flags, False

    ende = time.monotonic() + max(0.0, float(budget_s))
    for entry in entries:
        if time.monotonic() >= ende:
            logger.warning(
                "[%s] News-Pull: Zeitbudget %.0fs erschoepft — %d von %d "
                "Symbolen geprueft", WORKER_NAME, budget_s,
                len(flags), len(entries),
            )
            return flags, True
        yf_sym = entry.get("yf") or entry.get("symbol")
        sym = entry.get("symbol")
        if not yf_sym or not sym:
            continue
        klasse = entry.get("asset_class")
        if klasse and str(klasse).lower() not in FUNDAMENTAL_KLASSEN:
            # Krypto/Rohstoff/Index/Forex haben weder Earnings noch
            # Kursziele — der Call waere ein garantiertes 404.
            continue
        try:
            treffer = earnings_fn(yf_sym)
            # Earnings ist AVOID und damit das staerkste Flag — der zweite,
            # teurere Call entfaellt dann.
            if treffer is None and time.monotonic() < ende:
                treffer = analyst_fn(yf_sym)
        except Exception as exc:
            # Je Symbol abfangen, nicht nur um den ganzen Pull herum: sonst
            # reisst ein einziges kaputtes Instrument die Flags aller
            # uebrigen Kandidaten mit — und die waeren dann ungeprueft
            # gekauft worden.
            logger.debug("[%s] News-Pull: %s uebersprungen — %s",
                         WORKER_NAME, yf_sym, exc)
            continue
        if treffer:
            flags[sym] = treffer
    return flags, False
