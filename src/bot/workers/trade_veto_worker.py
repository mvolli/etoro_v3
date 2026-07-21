#!/usr/bin/env python3
"""eToro Trading Bot V3 — Pre-Trade-Veto Worker (fix/llm-pretrade-veto)

Laeuft :04/:19/:34/:49 — im Fenster zwischen signal_worker (:03, erstellt
APPROVED-Trades) und execution_worker (:06, fuehrt sie aus). Bis jetzt war
die Entry-Entscheidung zu 100% mechanisch; das LLM sah Trades erst NACH der
Eroeffnung. Dieser Worker legt dem LLM die approved Trades mit Marktkontext,
News-Flags und den Outcomes seiner letzten Vetos vor.

ASYMMETRISCHE RECHTE (Kern-Designprinzip):
  APPROVE  → nichts tun (Default)
  REDUCE   → amount_usd auf 25–75% verkleinern (unter min_buy → VETO)
  VETO     → Trade REJECTED mit Grund
Das LLM kann NIE vergroessern oder Trades hinzufuegen. Ein halluziniertes
Veto kostet eine Gelegenheit — ein halluzinierter Boost wuerde Geld kosten.

Fail-open: LLM down/Timeout/Parse-Fehler → alle Trades laufen unveraendert.
Race-safe: alle UPDATEs mit "WHERE status='APPROVED'" — hat der
execution_worker den Trade schon (SUBMITTING/ACTIVE), greift nichts mehr.

Lernschleife: jede Entscheidung landet in data/llm_veto_log.json; Vetos
werden nach >=24h gegen den Live-Preis bewertet (GOOD/MISSED_UPSIDE) und die
letzten 5 Grades in den naechsten Prompt injiziert.
"""
from __future__ import annotations

import json
import logging
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
logger = logging.getLogger("trade_veto_worker")

WORKER_NAME = "trade_veto_worker"
VETO_LOG_PATH = PROJECT_ROOT / "data" / "llm_veto_log.json"
FLAGS_PATH = PROJECT_ROOT / "data" / "llm_news_flags.json"
LLM_TIMEOUT_S = 75.0    # :04 + 75s < :06-Execution; darueber fail-open
MIN_BUY_FALLBACK = 50.0
REDUCE_MIN_PCT, REDUCE_MAX_PCT = 25, 75


def _seconds_until_execution(now: datetime | None = None) -> float:
    """Sekunden bis zum naechsten execution_worker-Slot (:06/:21/:36/:51).

    fix/veto-deadline (Review 2026-07-14): der LLM-Timeout wird hierauf
    gekappt — ein Veto, das erst NACH der Execution ankaeme, ist wertlos
    (der Race-Guard macht es zum NOOP). Bei zu knappem Fenster (<25s, z.B.
    Cron-Verzoegerung oder langsamer yfinance) wird der LLM-Call komplett
    uebersprungen: fail-open ist besser als ein totes Rennen."""
    now = now or datetime.now(timezone.utc)
    mins_ahead = (6 - now.minute) % 15
    secs = mins_ahead * 60 - now.second - now.microsecond / 1e6
    if secs <= 0:
        secs += 15 * 60
    return secs


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
            import os as _os
            _os.environ.setdefault(key.strip(), value.strip())


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _load_news_flags() -> dict:
    data = _load_json(FLAGS_PATH)
    expires = data.get("auto_expires_at")
    if expires:
        try:
            if datetime.fromisoformat(expires) < datetime.now(timezone.utc):
                return {}
        except Exception:
            return {}
    return data.get("flags", {})


def _veto_log_read() -> list[dict]:
    data = _load_json(VETO_LOG_PATH)
    return data.get("entries", []) if isinstance(data, dict) else []


def _veto_log_write(entries: list[dict]) -> None:
    tmp = VETO_LOG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"entries": entries[-300:]}, indent=1, ensure_ascii=False))
    tmp.replace(VETO_LOG_PATH)


def _backfill_outcomes(entries: list[dict]) -> None:
    """Vetos >=24h alt gegen Live-Preis bewerten: gefallen → GOOD (Veto hat
    Verlust vermieden), gestiegen → MISSED_UPSIDE. In-place."""
    now = datetime.now(timezone.utc)

    # Hygiene (fix/veto-backfill-no-data): Vetos ohne yf_symbol/signal_price
    # sind nie bewertbar — vorher blieben sie still und ewig "pending".
    # Nach 7 Tagen: outcome=NO_DATA (taucht nicht im Lern-Prompt auf).
    no_data = 0
    for e in entries:
        if (e.get("decision") == "VETO" and not e.get("outcome")
                and not (e.get("yf_symbol") and e.get("signal_price"))):
            no_data += 1
            try:
                if (now - datetime.fromisoformat(e["ts"])).total_seconds() >= 7 * 86400:
                    e["outcome"] = "NO_DATA"
            except Exception:
                e["outcome"] = "NO_DATA"
    if no_data:
        logger.info("[%s] %d Veto(s) ohne yf_symbol/signal_price — nicht bewertbar "
                    "(NO_DATA nach 7d)", WORKER_NAME, no_data)

    pending = [e for e in entries
               if e.get("decision") == "VETO" and not e.get("outcome")
               and e.get("yf_symbol") and e.get("signal_price")]
    to_check = []
    for e in pending:
        try:
            ts = datetime.fromisoformat(e["ts"])
            if (now - ts).total_seconds() >= 24 * 3600:
                to_check.append(e)
        except Exception:
            pass
    if not to_check:
        return
    try:
        import yfinance as yf
        syms = sorted({e["yf_symbol"] for e in to_check})
        data = yf.download(syms, period="2d", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False, threads=True)
        for e in to_check:
            try:
                closes = (data[e["yf_symbol"]]["Close"] if len(syms) > 1
                          else data["Close"]).dropna()
                px = float(closes.iloc[-1])
                delta_pct = (px / float(e["signal_price"]) - 1) * 100
                e["outcome"] = "GOOD" if delta_pct < 0 else "MISSED_UPSIDE"
                e["outcome_delta_pct"] = round(delta_pct, 2)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("[%s] Outcome-Backfill uebersprungen: %s", WORKER_NAME, exc)


def _apply_decision(db, trade: dict, decision: dict, min_buy: float) -> str:
    """Wendet eine validierte LLM-Entscheidung race-safe an.
    Rueckgabe: 'VETO' | 'REDUCE' | 'APPROVE' | 'NOOP' (Race verloren/ungueltig)."""
    action = str(decision.get("decision", "APPROVE")).upper()
    reason = str(decision.get("reason", ""))[:180]
    trade_id = trade["id"]

    if action == "VETO":
        cur = db.execute(
            "UPDATE trades SET status='REJECTED', "
            "rejection_reason=? WHERE id=? AND status='APPROVED'",
            (f"LLM-Veto: {reason}", trade_id),
        )
        return "VETO" if cur.rowcount else "NOOP"

    if action == "REDUCE":
        try:
            keep_pct = float(decision.get("reduce_to_pct", 50))
        except (TypeError, ValueError):
            keep_pct = 50.0
        keep_pct = max(REDUCE_MIN_PCT, min(REDUCE_MAX_PCT, keep_pct))
        new_amount = round(float(trade["amount_usd"]) * keep_pct / 100.0, 2)
        if new_amount < min_buy:
            cur = db.execute(
                "UPDATE trades SET status='REJECTED', rejection_reason=? "
                "WHERE id=? AND status='APPROVED'",
                (f"LLM-Reduce unter Min-Buy: {reason}", trade_id),
            )
            return "VETO" if cur.rowcount else "NOOP"
        cur = db.execute(
            "UPDATE trades SET amount_usd=? WHERE id=? AND status='APPROVED'",
            (new_amount, trade_id),
        )
        return "REDUCE" if cur.rowcount else "NOOP"

    return "APPROVE"


def _fetch_market_context() -> dict:
    ctx = {"spy_1d_pct": None, "qqq_1d_pct": None, "vix": None, "vix_label": "UNBEKANNT"}
    try:
        import yfinance as yf
        data = yf.download(["SPY", "QQQ", "^VIX"], period="5d", interval="1d",
                           group_by="ticker", auto_adjust=True, progress=False, threads=True)
        for key, t in (("spy_1d_pct", "SPY"), ("qqq_1d_pct", "QQQ")):
            try:
                c = data[t]["Close"].dropna()
                if len(c) >= 2:
                    ctx[key] = round(float(c.iloc[-1] / c.iloc[-2] - 1) * 100, 2)
            except Exception:
                pass
        try:
            vix = round(float(data["^VIX"]["Close"].dropna().iloc[-1]), 2)
            ctx["vix"] = vix
            ctx["vix_label"] = "NORMAL" if vix < 20 else ("ERHOEHT" if vix < 30 else "HOCH")
        except Exception:
            pass
    except Exception:
        pass
    return ctx


def main() -> int:
    from bot.core.worker_lock import worker_lock

    with worker_lock(WORKER_NAME) as acquired:
        if not acquired:
            print(f"{WORKER_NAME}: SKIPPED (already running)")
            return 0

        t0 = time.monotonic()
        _load_env()

        import yaml
        from bot.db.connection import DB
        from bot.db.repo import StateRepo
        from bot.core.heartbeat import record_heartbeat
        from bot.core.llm_client import call_llm_json

        cfg = {}
        try:
            cfg = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text()) or {}
        except Exception:
            pass
        min_buy = float(cfg.get("trading", {}).get("min_buy_usd", MIN_BUY_FALLBACK))

        db = DB(db_path=PROJECT_ROOT / "data" / "trading.db")
        try:
            record_heartbeat(StateRepo(db), WORKER_NAME)
        except Exception:
            pass

        log_entries = _veto_log_read()

        trades = [dict(r) for r in db.fetchall("""
            SELECT t.id, t.symbol, t.instrument_id, t.amount_usd, t.signal_price,
                   s.signal_type, s.conviction, s.score, s.rsi, s.bb_pct,
                   i.yfinance_symbol
            FROM trades t
            LEFT JOIN signals s ON s.id = t.signal_id
            LEFT JOIN instruments i ON i.instrument_id = t.instrument_id
            WHERE t.status = 'APPROVED'
        """)]
        if not trades:
            # Trade-freier Zyklus = kein Zeitdruck → hier laeuft der
            # Outcome-Backfill (fix/veto-deadline: vor dem LLM-Call kostete
            # er yfinance-Sekunden, die im :04→:06-Fenster fehlen).
            _backfill_outcomes(log_entries)
            _veto_log_write(log_entries)
            logger.debug("[%s] keine APPROVED-Trades — nur Backfill", WORKER_NAME)
            return 0

        deadline_s = _seconds_until_execution()
        if deadline_s < 25:
            logger.warning(
                "[%s] Nur %.0fs bis zum Execution-Slot — LLM-Veto uebersprungen "
                "(fail-open, %d Trades laufen mechanisch)",
                WORKER_NAME, deadline_s, len(trades),
            )
            print(f"{WORKER_NAME}: Zeitfenster zu knapp ({deadline_s:.0f}s) — fail-open")
            return 0

        market = _fetch_market_context()
        flags = _load_news_flags()
        positions = [dict(r) for r in db.fetchall(
            "SELECT symbol, unrealized_pnl_pct FROM portfolio_snapshot LIMIT 25")]

        recent_outcomes = [e for e in log_entries
                           if e.get("outcome") in ("GOOD", "MISSED_UPSIDE")][-5:]
        outcomes_block = "\n".join(
            f"- {e['symbol']}: VETO → {e['outcome']} ({e.get('outcome_delta_pct', '?')}% seither)"
            for e in recent_outcomes
        ) or "- noch keine bewerteten Vetos"

        trades_block = "\n".join(
            f"- trade_id={t['id']} {t['symbol']}: ${t['amount_usd']:.0f}, "
            f"Signal={t['signal_type'] or '?'} ({t['conviction'] or '?'}, Score {t['score'] or 0:.0f}), "
            f"RSI={t['rsi'] if t['rsi'] is not None else '?'}"
            + (f", NEWS-FLAG: {flags[t['symbol']]['flag']} ({flags[t['symbol']]['reason'][:80]})"
               if t["symbol"] in flags else "")
            for t in trades
        )
        pos_block = ", ".join(
            f"{p['symbol']} {p.get('unrealized_pnl_pct') or 0:+.1f}%" for p in positions
        ) or "keine"

        # feat/signal-scorecard: 30d-DB-Bilanz der Signal-Kombos der
        # Kandidaten — die LLM sieht Fakten statt zu raten. Datei wird
        # vom llm_review_worker taeglich refresht; fail-open.
        scorecard_block = "- keine Scorecard-Daten"
        try:
            _sc = json.loads(
                (PROJECT_ROOT / "data" / "signal_scorecard.json").read_text(encoding="utf-8")
            )
            _by_combo = {c["signal"]: c for c in _sc.get("combos", [])}
            _sc_lines = []
            for _st in sorted({t.get("signal_type") or "" for t in trades if t.get("signal_type")}):
                _c = _by_combo.get(_st)
                if _c:
                    _sc_lines.append(
                        f"- {_st}: WR {_c['win_rate_pct']}% (n={_c['n']}, "
                        f"{_c['pnl_usd']:+.0f}$, {_c['sl_kills']} SL-Kills)"
                    )
                else:
                    _sc_lines.append(f"- {_st}: keine 30d-Historie — konservativ pruefen")
            _ms = _sc.get("macd_split", {})
            if _ms.get("with") and _ms.get("without"):
                _sc_lines.append(
                    f"- Merksatz: Kombos MIT MACD-Komponente WR {_ms['with']['win_rate_pct']}% "
                    f"vs OHNE {_ms['without']['win_rate_pct']}% — Oversold ohne MACD-Wende ist ein Messer."
                )
            if _sc_lines:
                scorecard_block = "\n".join(_sc_lines)
        except Exception:
            pass

        prompt = f"""/no_think
Du bist die letzte Pruefinstanz vor der Order-Ausfuehrung eines autonomen
eToro-Bots. Die Trades unten haben alle mechanischen Gates bereits bestanden.
Deine Rechte sind ASYMMETRISCH: du darfst verhindern oder verkleinern, NIE
vergroessern. Vetoe nur bei konkretem Grund (News-Flag, klar feindliches
Marktumfeld fuer diesen Trade-Typ, offensichtliche Haeufung). Im Zweifel: APPROVE.

## Markt
SPY {market['spy_1d_pct']}% | QQQ {market['qqq_1d_pct']}% | VIX {market['vix']} ({market['vix_label']})

## Signal-Scorecard (30d, DB-verifiziert — Kombos mit WR < 25% bei n >= 5 brauchen einen konkreten Grund fuer APPROVE)
{scorecard_block}

## Offene Positionen
{pos_block}

## Deine letzten Veto-Outcomes (daraus lernen!)
{outcomes_block}

## Zu pruefende Trades
{trades_block}

Antworte NUR mit JSON:
{{"decisions": [{{"trade_id": N, "decision": "APPROVE|REDUCE|VETO",
  "reduce_to_pct": 50, "reason": "kurz, deutsch"}}]}}"""

        # Timeout dynamisch: Restzeit bis :06 minus 15s Sicherheitsmarge fuer
        # Anwendung + Log. Bei fruehem Start bleibt es bei LLM_TIMEOUT_S.
        _remaining = max(10.0, _seconds_until_execution() - 15.0)
        result = call_llm_json(prompt, max_tokens=768, temperature=0.05,
                               timeout_s=min(LLM_TIMEOUT_S, _remaining),
                               label=WORKER_NAME)

        vetoed, reduced = [], []
        if result and isinstance(result.get("decisions"), list):
            by_id = {t["id"]: t for t in trades}
            now_iso = datetime.now(timezone.utc).isoformat()
            for dec in result["decisions"]:
                try:
                    trade = by_id.get(int(dec.get("trade_id", -1)))
                except (TypeError, ValueError):
                    trade = None
                if trade is None:
                    continue
                outcome = _apply_decision(db, trade, dec, min_buy)
                if outcome in ("VETO", "REDUCE"):
                    # fix/veto-reason (2026-07-20): den LLM-Grund je Symbol
                    # mitfuehren — das Embed zeigte bisher nur Symbolnamen,
                    # die eigentliche Aussage ("warum verkleinert?") fehlte.
                    _v_reason = str(dec.get("reason", "")).strip() or "kein Grund angegeben"
                    (vetoed if outcome == "VETO" else reduced).append(
                        (trade["symbol"], _v_reason[:120])
                    )
                    log_entries.append({
                        "ts": now_iso, "trade_id": trade["id"],
                        "symbol": trade["symbol"], "yf_symbol": trade.get("yfinance_symbol"),
                        "decision": outcome, "reason": _v_reason[:180],
                        "signal_price": trade.get("signal_price"),
                    })
                    logger.info("[%s] %s %s — %s", WORKER_NAME, outcome,
                                trade["symbol"], _v_reason[:120])
            _veto_log_write(log_entries)
        elif result is None:
            logger.warning("[%s] LLM nicht verfuegbar — fail-open, %d Trades unveraendert",
                           WORKER_NAME, len(trades))

        # Entscheidungen sind angewendet — jetzt ohne Zeitdruck den
        # Outcome-Backfill nachziehen (Lernschleife aktuell halten).
        _backfill_outcomes(log_entries)
        _veto_log_write(log_entries)

        elapsed = time.monotonic() - t0
        try:
            from bot.core.heartbeat import record_duration as _rd
            from bot.db.repo import StateRepo as _SR_dur
            _rd(_SR_dur(db), WORKER_NAME, elapsed)
        except Exception:
            pass
        print(f"{WORKER_NAME}: {len(trades)} geprueft, {len(vetoed)} Veto, "
              f"{len(reduced)} verkleinert, {elapsed:.1f}s")

        if vetoed or reduced:
            try:
                sys.path.insert(0, str(SRC_DIR / "bot"))
                import discord_embeds as _DE
                _v_lines = [f"🛑 **{sym}** — {rsn}" for sym, rsn in vetoed]
                _v_lines += [f"📉 **{sym}** verkleinert — {rsn}" for sym, rsn in reduced]
                _DE.post_alert_embed(
                    title=f"🛑 Pre-Trade-Veto: {len(vetoed)} Veto, {len(reduced)} verkleinert",
                    description="\n".join(_v_lines)[:1900] or "—",
                    severity="WARNING",
                    channel="trades",
                )
            except Exception:
                pass
        elif trades:
            # feat/result-embeds (2026-07-16): auch die Freigabe ist ein
            # Ergebnis — sonst ist ein pruefender LLM-Lauf unsichtbar.
            # fix/veto-allclear-dedup (2026-07-22): NUR posten wenn sich das
            # Set der APPROVED-Trades geaendert hat. Ein bei geschlossenem
            # Markt haengender Trade (BTC #472) erzeugte sonst alle 15min
            # dasselbe "alle freigegeben"-Embed (User-Beschwerde 2x).
            _ac_key = ",".join(sorted(str(t.get("id")) for t in trades))
            _ac_post = True
            try:
                from bot.db.repo import StateRepo as _SR_ac
                _ac_sr = _SR_ac(db)
                if _ac_sr.get("VETO_ALLCLEAR_IDS") == _ac_key:
                    _ac_post = False
                else:
                    _ac_sr.set("VETO_ALLCLEAR_IDS", _ac_key)
            except Exception:
                _ac_post = True  # fail-open: lieber ein Embed zu viel
            if _ac_post:
                try:
                    sys.path.insert(0, str(SRC_DIR / "bot"))
                    import discord_embeds as _DE
                    _DE.post_alert_embed(
                        title=f"🟢 Pre-Trade-Veto: {len(trades)} geprueft — alle freigegeben",
                        description=", ".join(
                            str(t.get("symbol") or t.get("instrument_id")) for t in trades
                        )[:1000],
                        severity="INFO",
                        channel="trades",
                    )
                except Exception:
                    pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
