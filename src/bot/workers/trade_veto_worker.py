#!/usr/bin/env python3
"""eToro Trading Bot V3 — Pre-Trade-Veto Worker (fix/llm-pretrade-veto)

Laeuft :04/:19/:34/:49 — im Fenster zwischen signal_worker (:03, erstellt
APPROVED-Trades) und execution_worker (:06, fuehrt sie aus). Bis jetzt war
die Entry-Entscheidung zu 100% mechanisch; das LLM sah Trades erst NACH der
Eroeffnung. Dieser Worker legt dem LLM die approved Trades mit Marktkontext,
News-Flags und den Outcomes seiner letzten Vetos vor.

ASYMMETRISCHE RECHTE (Kern-Designprinzip):
  APPROVE  → nichts tun (Default)
  REDUCE   → amount_usd auf 25–75% verkleinern (unter $200-Floor → VETO)
  VETO     → Trade REJECTED mit Grund
Das LLM kann NIE vergroessern oder Trades hinzufuegen. Ein halluziniertes
Veto kostet eine Gelegenheit — ein halluzinierter Boost wuerde Geld kosten.

Fail-open: LLM down/Timeout/Parse-Fehler → alle Trades laufen unveraendert.
Race-safe: alle UPDATEs mit "WHERE status='APPROVED'" — hat der
execution_worker den Trade schon (SUBMITTING/ACTIVE), greift nichts mehr.
Zusaetzlich order_id IS NULL (fix/veto-inflight, 2026-09-12): ein deferter
Trade steht wieder auf APPROVED, hat aber bereits eine Order beim Broker.
Trade #2174 (VU.PA) wurde so vetoed, waehrend Order 1582187392 lief —
eToro fuehrte sie 7 Minuten spaeter aus, die Position 3575889260 blieb
unverwaltet ohne Stop-Loss. Geld schon committed = LLM hat kein Wort mehr.

Lernschleife: jede Entscheidung landet in data/llm_veto_log.json; Vetos
werden nach >=24h gegen den Live-Preis bewertet (GOOD/MISSED_UPSIDE) und die
letzten 5 Grades in den naechsten Prompt injiziert. Seit 2026-10-17 zusaetzlich
`delta_28d_pct` fuer VETO und REDUCE (Zeitstopp der Kaeufe), Wochenbericht.

ALLE KAUFWEGE (VoLLi 2026-10-17, bot/core/buy_veto.py): geprueft werden alle
APPROVED-Kaeufe ohne gueltiges Urteil bei offenem Markt, auch CORE_SWEEP und
CORE_INDEX (dort nur VETO, kein REDUCE). Zuerst harte Regel-Sperren (AVOID,
Earnings, 8-K 1.03/3.01/4.02), dann das LLM mit frischen Headlines,
SEC-Meldungen und Truth-Social-Posts. Das Urteil steht in trades.veto_*; der
execution_worker kauft ohne Urteil nicht (wartet einen Zyklus, dann nur
Regeln + NO_LLM). Die fruehere :06-Deadline entfaellt: ein spaetes Urteil
gilt im naechsten Execution-Slot.
"""
from __future__ import annotations

import json
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
logger = logging.getLogger("trade_veto_worker")

WORKER_NAME = "trade_veto_worker"
VETO_LOG_PATH = PROJECT_ROOT / "data" / "llm_veto_log.json"
FLAGS_PATH = PROJECT_ROOT / "data" / "llm_news_flags.json"
LLM_TIMEOUT_S = 75.0    # kein Urteil -> execution_worker wartet, dann Fallback
MIN_BUY_FALLBACK = 50.0
REDUCE_MIN_PCT, REDUCE_MAX_PCT = 25, 75


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
    # feat/veto-alle-kaufwege (2026-10-17): zusaetzlich nach 28 Tagen (= Zeitstopp
    # der Kaeufe) fuer VETO UND REDUCE. 24 h sagt bei Wochen-Haltedauer wenig.
    due_28 = []
    for e in entries:
        if (e.get("decision") in ("VETO", "REDUCE") and "delta_28d_pct" not in e
                and e.get("yf_symbol") and e.get("signal_price")):
            try:
                if (now - datetime.fromisoformat(e["ts"])).total_seconds() >= 28 * 86400:
                    due_28.append(e)
            except Exception:
                pass
    if due_28:
        _grade_28d(due_28)
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


def _grade_28d(entries: list[dict]) -> None:
    """Kursveraenderung 28 Tage nach Veto/Reduce (negativ = Veto lag richtig). In-place."""
    try:
        import yfinance as yf
        syms = sorted({e["yf_symbol"] for e in entries})
        data = yf.download(syms, period="5d", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False, threads=True)
        for e in entries:
            try:
                closes = (data[e["yf_symbol"]]["Close"] if len(syms) > 1 else data["Close"]).dropna()
                e["delta_28d_pct"] = round((float(closes.iloc[-1]) / float(e["signal_price"]) - 1) * 100, 2)
            except Exception:
                e["delta_28d_pct"] = None     # kein Kurs (delistet) -> nicht endlos neu versuchen
    except Exception as exc:
        logger.debug("[%s] 28d-Bewertung uebersprungen: %s", WORKER_NAME, exc)


def _signal_rejected(db, trade: dict) -> None:
    """Markiert das Signal des abgelehnten Trades ebenfalls als REJECTED.

    fix/veto-signal-status (2026-09-25): der Veto-Pfad setzte nur den TRADE
    auf REJECTED, das Signal blieb CONSUMED. Damit merkte sich nichts, dass
    diese Konstellation nicht durchgeht — `has_fresh_signal` blockt auf
    status IN ('FRESH','REJECTED'), CONSUMED faellt durch. Der data_worker
    erzeugte dasselbe Signal auf unveraendertem Zustand neu, der Veto
    verbrannte erneut einen LLM-Call und einen der knappen Kandidaten-Slots.

    Gemessen am 2026-09-25 ueber 14 Tage: 198 "LLM-Reduce unter Min-Buy"-
    Ablehnungen auf 80 Instrumenten (2,5 je Instrument), und in ALLEN 198
    Faellen stand das Signal danach auf CONSUMED.

    Der Reduce-Fall ist dabei weitgehend vorherbestimmt: REDUCE_MIN_PCT/MAX
    = 25/75, min_buy_usd = 50. Ein Trade am $50-Floor landet bei JEDER
    erlaubten Reduktion darunter ($12,50-$37,50); erst ab $66,67 ueberlebt
    wenigstens ein 75-%-Reduce. Die 198 Faelle lagen bei $50,00-$99,30
    (Ø $70,11) — also genau in der Zone, in der die Ablehnung feststeht,
    bevor die LLM ueberhaupt gefragt wird.

    Der signal_worker kennt diese Konvention laengst (`update_signal_status
    (signal_id, "REJECTED")` an 8 Stellen, u. a. `_reject_below_floor_impl`
    fuer exakt denselben Fall "Betrag unter Floor"). Der Veto-Worker war der
    Ausreisser; hier wird die bestehende Konvention nachgezogen, kein neuer
    Mechanismus gebaut.

    Wirkung: `has_fresh_signal` blockt die Neuerzeugung bis zum Signal-TTL
    (60 min). Transiente Gruende heilen danach automatisch — dieselbe
    Semantik wie fix/rejected-signal-dedup (2026-07-20, ART.L: 12 identische
    Zeilen an einem Vormittag).

    Fail-open: ein Fehler hier darf den Veto-Lauf nie kippen. Die
    Trade-Ablehnung ist bereits geschrieben und bleibt gueltig.
    """
    sid = trade.get("signal_id")
    if not sid:
        return
    try:
        db.execute("UPDATE signals SET status='REJECTED' WHERE id=?", (sid,))
    except Exception:
        logger.debug("[%s] Signal-Status nicht gesetzt (Trade bleibt abgelehnt)",
                     WORKER_NAME, exc_info=True)


def _reduce_floor(cfg: dict) -> float:
    """Untergrenze fuer REDUCE; darunter wird der Trade zum Veto.

    VoLLi 2026-10-16: REDUCE unter die Eroeffnungs-Mindestgroesse ($200,
    signal_floor_usd) ist ein Veto. Vorher galt trading.min_buy_usd ($50),
    und execution_worker b4 rundete einen auf $61 verkleinerten Trade wieder
    auf $200 hoch — die LLM-Verkleinerung war still wirkungslos.
    """
    floor = float((cfg.get("trading", {}) or {}).get("min_buy_usd", MIN_BUY_FALLBACK))
    try:
        from bot.core.regime import get_regime_params
        return max(floor, float(get_regime_params("NORMAL").get("signal_floor_usd", 200.0)))
    except Exception:  # noqa: BLE001
        return max(floor, 200.0)


def _apply_decision(db, trade: dict, decision: dict, min_buy: float) -> str:
    """Wendet eine validierte LLM-Entscheidung race-safe an.
    Rueckgabe: 'VETO' | 'REDUCE' | 'APPROVE' | 'NOOP' (Race verloren/ungueltig)."""
    action = str(decision.get("decision", "APPROVE")).upper()
    reason = str(decision.get("reason", ""))[:180]
    trade_id = trade["id"]

    if action == "VETO":
        cur = db.execute(
            "UPDATE trades SET status='REJECTED', "
            "rejection_reason=? WHERE id=? AND status='APPROVED' "
            "AND (order_id IS NULL OR order_id = '')",
            (f"LLM-Veto: {reason}", trade_id),
        )
        if cur.rowcount:
            _signal_rejected(db, trade)
            return "VETO"
        return "NOOP"

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
                "WHERE id=? AND status='APPROVED' "
                "AND (order_id IS NULL OR order_id = '')",
                (f"LLM-Reduce unter Min-Buy: {reason}", trade_id),
            )
            if cur.rowcount:
                _signal_rejected(db, trade)
                return "VETO"
            return "NOOP"
        cur = db.execute(
            "UPDATE trades SET amount_usd=? WHERE id=? AND status='APPROVED' "
            "AND (order_id IS NULL OR order_id = '')",
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


MAX_TRADES_PER_RUN = 8   # Kontext je Trade kostet Netz-Calls; Rest im naechsten Lauf


def _record_verdict(db, trade_id: int, decision: str, reason: str) -> None:
    """Urteil an einen bereits abgelehnten Trade schreiben (VETO/RULE_VETO)."""
    try:
        from bot.core.buy_veto import _iso, _now
        db.execute("UPDATE trades SET veto_decision=?, veto_reason=?, veto_at=? WHERE id=?",
                   (decision, str(reason)[:200], _iso(_now()), trade_id))
    except Exception:  # noqa: BLE001
        logger.debug("[%s] Urteil #%s nicht geschrieben", WORKER_NAME, trade_id, exc_info=True)


def _is_core(trade: dict) -> bool:
    from bot.core.buy_veto import CORE_TYPES
    return str(trade.get("signal_type") or "") in CORE_TYPES


def _market_open(db, trade: dict) -> bool:
    try:
        from bot.core.market_hours import is_market_open, resolve_market_fields
        mf = resolve_market_fields(db, trade["instrument_id"])
        sym, yf_sym, cat = mf if mf else (trade["symbol"], "", "")
        return bool(is_market_open(sym or trade["symbol"], yf_sym, cat, fail_open=False))
    except Exception:  # noqa: BLE001
        return True   # im Zweifel pruefen (kostet nur einen LLM-Call)


def _trade_context(trade: dict, posts: list[dict]) -> dict:
    """Frische Quellen je Trade: Headlines, SEC-Meldungen (US), relevante Truth-Posts."""
    from bot.core import buy_veto as bv
    ctx = {"headlines": [], "sec": [], "truth": []}
    yf_sym = bv.yahoo_symbol(trade)
    if str(trade.get("asset_class") or "") in ("stock", "etf"):
        ctx["headlines"] = bv.fresh_headlines(yf_sym, name=trade.get("name"))
    if bv.is_us_stock(trade["symbol"], trade.get("asset_class")):
        cik = bv.sec_cik(trade["symbol"])
        fl = bv.sec_recent_filings(cik) if cik else None
        if fl:
            ctx["sec"] = [f"{f['date']} {f['form']}" + (f" Items {f['items']}" if f.get("items") else "")
                          for f in fl if not str(f["form"]).startswith(("4", "144", "3", "5"))][:8]
    kws = [] if _is_core(trade) else bv.trade_keywords(trade)
    ctx["truth"] = bv.relevant_posts(posts, kws, limit=5)
    return ctx


def main() -> int:
    from bot.core.worker_lock import worker_lock

    with worker_lock(WORKER_NAME) as acquired:
        if not acquired:
            print(f"{WORKER_NAME}: SKIPPED (already running)")
            return 0

        t0 = time.monotonic()
        from bot.config import load_hermes_env
        load_hermes_env()

        import yaml
        from bot.db.connection import DB
        from bot.db.repo import StateRepo
        from bot.core.heartbeat import record_heartbeat
        from bot.core.llm_client import call_llm_json
        from bot.core import buy_veto as bv

        cfg = {}
        try:
            cfg = yaml.safe_load((PROJECT_ROOT / "config" / "config.yaml").read_text()) or {}
        except Exception:
            pass
        min_buy = _reduce_floor(cfg)

        db = DB(db_path=PROJECT_ROOT / "data" / "trading.db")
        bv.ensure_columns(db)
        try:
            record_heartbeat(StateRepo(db), WORKER_NAME)
        except Exception:
            pass

        log_entries = _veto_log_read()

        # feat/veto-alle-kaufwege (VoLLi 2026-10-17): ALLE Kauf-Trades, auch
        # CORE_SWEEP/CORE_INDEX und Trades ohne Signal. Geprueft wird, was kein
        # gueltiges Urteil hat und jetzt handelbar ist; der execution_worker
        # kauft ohne Urteil nicht (bot/core/buy_veto.execution_gate).
        rows = [dict(r) for r in db.fetchall(
            bv.TRADE_CTX_SQL + " WHERE t.status = 'APPROVED' "
            "AND (t.order_id IS NULL OR t.order_id = '') "
            "AND COALESCE(t.direction, 'BUY') = 'BUY' ORDER BY t.id")]
        trades = [t for t in rows if bv.needs_verdict(t) and _market_open(db, t)][:MAX_TRADES_PER_RUN]
        if not trades:
            _backfill_outcomes(log_entries)
            _veto_log_write(log_entries)
            logger.debug("[%s] keine Trades ohne Urteil — nur Backfill", WORKER_NAME)
            return 0

        now_iso = datetime.now(timezone.utc).isoformat()
        vetoed, reduced = [], []

        # ── 1. Harte Regel-Sperren (ohne LLM) ────────────────────────────────
        flags = _load_news_flags()
        remaining = []
        for t in trades:
            reason = bv.rule_block_reason(t, flags=flags)
            if not reason:
                remaining.append(t)
                continue
            cur = db.execute(
                "UPDATE trades SET status='REJECTED', rejection_reason=? WHERE id=? "
                "AND status='APPROVED' AND (order_id IS NULL OR order_id = '')",
                (f"Regel-Veto: {reason}"[:200], t["id"]))
            if cur.rowcount:
                _signal_rejected(db, t)
                _record_verdict(db, t["id"], "RULE_VETO", reason)
                vetoed.append((t["symbol"], f"Regel: {reason[:110]}"))
                log_entries.append({"ts": now_iso, "trade_id": t["id"], "symbol": t["symbol"],
                                    "yf_symbol": bv.yahoo_symbol(t), "decision": "VETO",
                                    "rule": True, "reason": f"Regel: {reason}"[:180],
                                    "signal_price": t.get("signal_price"),
                                    "signal_type": t.get("signal_type")})
                logger.info("[%s] REGEL-VETO %s — %s", WORKER_NAME, t["symbol"], reason)

        # ── 2. LLM-Urteil mit frischen Quellen ───────────────────────────────
        result = None
        if remaining:
            market = _fetch_market_context()
            posts = bv.truth_posts()
            ctxs = {t["id"]: _trade_context(t, posts) for t in remaining}
            positions = [dict(r) for r in db.fetchall(
                "SELECT symbol, unrealized_pnl_pct FROM portfolio_snapshot LIMIT 25")]

            recent_outcomes = [e for e in log_entries
                               if e.get("outcome") in ("GOOD", "MISSED_UPSIDE")][-5:]
            outcomes_block = "\n".join(
                f"- {e['symbol']}: VETO → {e['outcome']} ({e.get('outcome_delta_pct', '?')}% seither)"
                for e in recent_outcomes
            ) or "- noch keine bewerteten Vetos"

            def _trade_line(t: dict) -> str:
                c = ctxs[t["id"]]
                kern = _is_core(t)
                s = (f"- trade_id={t['id']} {t['symbol']} ({t.get('name') or '?'}; "
                     f"{t.get('sector') or t.get('asset_class') or '?'}): ${float(t['amount_usd'] or 0):.0f}, "
                     f"Signal={t.get('signal_type') or '?'} ({t.get('conviction') or '?'}, "
                     f"Score {float(t.get('score') or 0):.0f}), RSI={t['rsi'] if t.get('rsi') is not None else '?'}")
                if kern:
                    s += ("\n  [KERN-KAUF: nur VETO bei konkretem System- oder Firmenereignis; "
                          "KEIN REDUCE; im Zweifel APPROVE]")
                if t["symbol"] in flags:
                    s += f"\n  News-Flag: {flags[t['symbol']]['flag']} ({str(flags[t['symbol']].get('reason', ''))[:80]})"
                if c["headlines"]:
                    s += "\n  Headlines 48h: " + " | ".join(c["headlines"])
                if c["sec"]:
                    s += "\n  SEC-Meldungen 10 Tage: " + "; ".join(c["sec"])
                if c["truth"]:
                    s += "\n  Truth-Social-Posts dazu: " + " | ".join(
                        f"[{p['ts'][5:16]}] {p['text'][:220]}" for p in c["truth"])
                return s

            trades_block = "\n".join(_trade_line(t) for t in remaining)
            pos_block = ", ".join(
                f"{p['symbol']} {p.get('unrealized_pnl_pct') or 0:+.1f}%" for p in positions
            ) or "keine"

            scorecard_block = "- keine Scorecard-Daten"
            try:
                _sc = json.loads(
                    (PROJECT_ROOT / "data" / "signal_scorecard.json").read_text(encoding="utf-8")
                )
                _by_combo = {c["signal"]: c for c in _sc.get("combos", [])}
                _sc_lines = []
                for _st in sorted({t.get("signal_type") or "" for t in remaining if t.get("signal_type")}):
                    if _st in bv.CORE_TYPES:
                        continue
                    _c = _by_combo.get(_st)
                    if _c:
                        _sc_lines.append(
                            f"- {_st}: WR {_c['win_rate_pct']}% (n={_c['n']}, "
                            f"{_c['pnl_usd']:+.0f}$, {_c['sl_kills']} SL-Kills)"
                        )
                    else:
                        _sc_lines.append(f"- {_st}: keine 30d-Historie")
                if _sc_lines:
                    scorecard_block = "\n".join(_sc_lines)
            except Exception:
                pass

            prompt = f"""/no_think
Du bist die letzte Pruefinstanz vor dem Kauf eines autonomen eToro-Bots. Die
Trades unten haben alle mechanischen Gates bestanden; harte Regel-Sperren
(News-AVOID, Earnings binnen 2 Tagen, 8-K Insolvenz/Delisting/Bilanzkorrektur)
sind bereits geprueft. Deine Rechte sind ASYMMETRISCH: verhindern oder
verkleinern, NIE vergroessern. Haltedauer der Kaeufe: Wochen bis Monate.

Vetoe nur bei KONKRETEM Warnsignal in den Quellen: Prognose gesenkt,
Bilanz-/Buchhaltungsproblem, Klage/Ermittlung, CEO-/CFO-Abgang, Uebernahme
gescheitert, Zoll-/Sanktionsrisiko fuer genau diese Firma, Branche oder dieses
Land (z. B. aus Truth-Social-Posts), Kurssturz-Ausloeser. Allgemeine
Marktstimmung, Bewertung oder fehlende News sind KEIN Grund. Im Zweifel: APPROVE.

## Markt
SPY {market['spy_1d_pct']}% | QQQ {market['qqq_1d_pct']}% | VIX {market['vix']} ({market['vix_label']})

## Signal-Scorecard (30d, DB-verifiziert)
{scorecard_block}

## Offene Positionen
{pos_block}

## Deine letzten Veto-Outcomes
{outcomes_block}

## Zu pruefende Kaeufe (mit frischen Quellen)
{trades_block}

Antworte NUR mit JSON, ein Eintrag je trade_id:
{{"decisions": [{{"trade_id": N, "decision": "APPROVE|REDUCE|VETO",
  "reduce_to_pct": 50, "reason": "kurz, deutsch, mit Quelle"}}]}}"""

            result = call_llm_json(prompt, max_tokens=1024, temperature=0.05,
                                   timeout_s=LLM_TIMEOUT_S, label=WORKER_NAME)

            if result and isinstance(result.get("decisions"), list):
                by_id = {t["id"]: t for t in remaining}
                decided = set()
                for dec in result["decisions"]:
                    try:
                        trade = by_id.get(int(dec.get("trade_id", -1)))
                    except (TypeError, ValueError):
                        trade = None
                    if trade is None or trade["id"] in decided:
                        continue
                    decided.add(trade["id"])
                    action = str(dec.get("decision", "APPROVE")).upper()
                    _v_reason = str(dec.get("reason", "")).strip() or "kein Grund angegeben"
                    if action == "REDUCE" and _is_core(trade):
                        # VoLLi 17.10.: Kern-Kaeufe nur hartes Veto, kein REDUCE.
                        action, _v_reason = "APPROVE", f"REDUCE bei Kern ignoriert: {_v_reason}"
                    if action not in ("VETO", "REDUCE"):
                        bv.set_verdict(db, trade["id"], "APPROVE", _v_reason)
                        continue
                    outcome = _apply_decision(db, trade, dict(dec, decision=action), min_buy)
                    if outcome == "VETO":
                        _record_verdict(db, trade["id"], "VETO", _v_reason)
                    elif outcome == "REDUCE":
                        bv.set_verdict(db, trade["id"], "REDUCE", _v_reason)
                    if outcome in ("VETO", "REDUCE"):
                        (vetoed if outcome == "VETO" else reduced).append(
                            (trade["symbol"], _v_reason[:120])
                        )
                        log_entries.append({
                            "ts": now_iso, "trade_id": trade["id"],
                            "symbol": trade["symbol"], "yf_symbol": bv.yahoo_symbol(trade),
                            "decision": outcome, "reason": _v_reason[:180],
                            "signal_price": trade.get("signal_price"),
                            "signal_type": trade.get("signal_type"),
                        })
                        logger.info("[%s] %s %s — %s", WORKER_NAME, outcome,
                                    trade["symbol"], _v_reason[:120])
                # Vom LLM nicht erwaehnte Trades: kein Einwand.
                for t in remaining:
                    if t["id"] not in decided:
                        bv.set_verdict(db, t["id"], "APPROVE", "LLM ohne Einwand (nicht erwaehnt)")
            else:
                # Kein Urteil: der execution_worker wartet einen Zyklus und
                # faellt dann auf die Regel-Pruefung zurueck (NO_LLM).
                logger.warning("[%s] LLM nicht verfuegbar — kein Urteil, %d Kaeufe warten",
                               WORKER_NAME, len(remaining))
                try:
                    from bot.db.repo import LogRepo
                    LogRepo(db).write("WARN", WORKER_NAME,
                                      f"LLM ohne Urteil — {len(remaining)} Kauf/Kaeufe warten "
                                      f"(Fallback NO_LLM nach {bv.WAIT_MAX_MIN} min)",
                                      {"trade_ids": [t["id"] for t in remaining]})
                except Exception:
                    pass
        _veto_log_write(log_entries)

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
        elif remaining and result:
            # feat/result-embeds (2026-07-16) + fix/veto-allclear-dedup
            # (2026-07-22): Freigabe nur posten, wenn sich das Set geaendert hat.
            _ac_key = ",".join(sorted(str(t.get("id")) for t in remaining))
            _ac_post = True
            try:
                from bot.db.repo import StateRepo as _SR_ac
                _ac_sr = _SR_ac(db)
                if _ac_sr.get("VETO_ALLCLEAR_IDS") == _ac_key:
                    _ac_post = False
                else:
                    _ac_sr.set("VETO_ALLCLEAR_IDS", _ac_key)
            except Exception:
                _ac_post = True
            if _ac_post:
                try:
                    sys.path.insert(0, str(SRC_DIR / "bot"))
                    import discord_embeds as _DE
                    _DE.post_alert_embed(
                        title=f"🟢 Pre-Trade-Veto: {len(remaining)} geprueft — alle freigegeben",
                        description=", ".join(
                            str(t.get("symbol") or t.get("instrument_id")) for t in remaining
                        )[:1000],
                        severity="INFO",
                        channel="trades",
                    )
                except Exception:
                    pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
