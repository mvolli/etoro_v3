"""llm_execution.py — Autonome Ausfuehrung von LLM Position-Empfehlungen.

Wird vom reconciler.py aufgerufen (nach jedem Portfolio-Sync).
Liest data/llm_position_recommendations.json und fuehrt aus:
  EXIT    -> client.close_position() (nur bei offenem Markt)
  TIGHTEN -> mark_momentum_faded() (DB-Flag, kein API-Call noetig)

Safety:
  - MAX_REC_AGE_MIN: Aeltere Empfehlungen ignoriert (Stale-Data-Schutz)
  - is_market_open(...): EXIT nur bei offenem Markt; TIGHTEN immer.
    Boersenfelder kommen aus instruments (resolve_market_fields) — das
    blosse rec-Symbol trifft sonst die falsche Boerse.
  - executed: True verhindert Doppel-Ausfuehrung
  - Bei API-Fehler: executed bleibt False -> naechster Cycle retried
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.api.client import EToroClient
    from bot.db.db import DB
    from bot.db.repos.log_repo import LogRepo

logger = logging.getLogger(__name__)

MAX_REC_AGE_MIN = 100

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RECS_PATH = PROJECT_ROOT / "data" / "llm_position_recommendations.json"
OUTCOMES_PATH = PROJECT_ROOT / "data" / "llm_position_review_outcomes.json"


def _get_de():
    """discord_embeds-Modul laden (bot.-Namespace). None wenn nicht ladbar.

    feat/pnl-nachreport: attach_chart/post/get_last_post muessen an
    DERSELBEN Modulinstanz laufen (One-Shot-Slots) — daher ein Loader
    statt verstreuter from-Imports.
    """
    try:
        from bot import discord_embeds as _de
        return _de
    except Exception:
        return None


def _discord(fn_name: str, **kwargs):
    try:
        de = _get_de()
        if de is not None and hasattr(de, fn_name):
            return getattr(de, fn_name)(**kwargs)
    except Exception:
        pass
    return None


def _append_outcome_entry(
    rec: dict,
    position_id: str,
    recommendation: str,
    close_pct: float,
    now: "datetime",
) -> None:
    """Schreibt Outcome-Entry nach EXIT/TIGHTEN in llm_position_review_outcomes.json.

    Wird von _backfill_outcomes() (position_review_worker) nach 24h ausgewertet.
    """
    try:
        pnl_at_exec   = None
        price_at_exec = None
        try:
            import sqlite3 as _sq3
            _db_path = OUTCOMES_PATH.parent / "trading.db"
            _conn = _sq3.connect(f"file:{_db_path}?mode=ro", uri=True)
            _conn.row_factory = _sq3.Row
            _snap = _conn.execute(
                "SELECT unrealized_pnl_pct, current_price "
                "FROM portfolio_snapshot WHERE api_position_id = ?",
                (position_id,),
            ).fetchone()
            _conn.close()
            if _snap:
                pnl_at_exec   = float(_snap["unrealized_pnl_pct"]) if _snap["unrealized_pnl_pct"] is not None else None
                price_at_exec = float(_snap["current_price"])       if _snap["current_price"]       is not None else None
        except Exception:
            pass

        entry = {
            "ts_recommended":     rec.get("ts"),
            "ts_executed":        now.isoformat()[:19],
            "symbol":             rec.get("symbol", "?"),
            "recommendation":     recommendation,
            "close_pct":          close_pct,
            "reason":             rec.get("reason", ""),
            "pnl_at_execution":   pnl_at_exec,
            "price_at_execution": price_at_exec,
            "outcome_checked":    False,
            "outcome_grade":      None,
            "outcome_pnl_delta":  None,
        }

        outcomes: list = []
        if OUTCOMES_PATH.exists():
            try:
                outcomes = json.loads(OUTCOMES_PATH.read_text(encoding="utf-8"))
                if not isinstance(outcomes, list):
                    outcomes = []
            except Exception:
                outcomes = []
        outcomes.append(entry)
        OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTCOMES_PATH.write_text(
            json.dumps(outcomes, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as _oe:
        logger.debug("[llm_execution] Outcome-Entry fehlgeschlagen: %s", _oe)


def execute_llm_recommendations(
    client: "EToroClient",
    db: "DB",
    live_position_ids: set,
    log_repo: "LogRepo",
    dry_run: bool = False,
) -> dict:
    """Fuehrt EXIT- und TIGHTEN-Empfehlungen aus llm_position_recommendations.json aus.

    Parameters
    ----------
    client : EToroClient
        Aktiver API-Client (muss noch offen sein).
    db : DB
        DB-Verbindung fuer mark_momentum_faded().
    live_position_ids : set[str]
        Menge der aktuell bekannten api_position_ids (aus Portfolio-Sync).
    log_repo : LogRepo
        Fuer strukturiertes Logging.
    dry_run : bool
        True = keine echten API-Calls, nur Logging.
    """
    from bot.core.trailing_stop import mark_momentum_faded
    from bot.core.market_hours import is_market_open, resolve_market_fields

    stats = {"exit_count": 0, "tighten_count": 0, "skip_count": 0, "errors": []}

    if not RECS_PATH.exists():
        return stats

    try:
        recs = json.loads(RECS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[llm_execution] Konnte %s nicht lesen: %s", RECS_PATH, e)
        return stats

    now = datetime.now(timezone.utc)
    changed = False

    for rec in recs:
        if rec.get("executed"):
            continue

        ts_str = rec.get("ts")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_min = (now - ts).total_seconds() / 60
        except Exception:
            continue

        if age_min > MAX_REC_AGE_MIN:
            stats["skip_count"] += 1
            continue

        recommendation = rec.get("recommendation")
        if recommendation not in ("EXIT", "TIGHTEN"):
            continue

        symbol      = rec.get("symbol", "?")
        position_id = rec.get("position_id")
        instr_id    = rec.get("instrument_id")
        reason      = rec.get("reason", "LLM-Empfehlung")

        # Position noch aktiv?
        if position_id and position_id not in live_position_ids:
            logger.info("[llm_execution] %s %s: Position nicht mehr aktiv", recommendation, symbol)
            rec["executed"]        = True
            rec["executed_at"]     = now.isoformat()[:19]
            rec["executed_reason"] = "position_already_closed"
            changed = True
            stats["skip_count"] += 1
            continue

        # close_pct aus Recommendation (LLM kann Teilverkauf statt Vollverkauf wählen)
        rec_close_pct = float(rec.get("close_pct") or 100.0)

        # ── EXIT / TIGHTEN: gemeinsame Execution via _execute_partial_or_full ─
        if recommendation in ("EXIT", "TIGHTEN"):
            if not position_id or not instr_id:
                msg = "%s %s: position_id oder instrument_id fehlt" % (recommendation, symbol)
                logger.warning("[llm_execution] %s", msg)
                stats["errors"].append(msg)
                stats["skip_count"] += 1
                continue

            # fix/market-key-unknown: yfinance_symbol + Kategorie mitgeben.
            # Das Symbol aus der Recommendation allein liess den Guard bei
            # Nicht-US-Werten die NYSE fragen. Fail-open bleibt: ein EXIT ist
            # Risikoabbau und darf an einem unbekannten Kalender nicht haengen.
            try:
                _mf = resolve_market_fields(db, instr_id)
                _mh_sym, _mh_yf, _mh_cat = _mf if _mf else (symbol, "", "")
                market_open = is_market_open(_mh_sym or symbol, _mh_yf, _mh_cat)
            except Exception:
                market_open = True

            if not market_open and recommendation == "EXIT":
                logger.info("[llm_execution] EXIT %s: Markt geschlossen — retry", symbol)
                stats["skip_count"] += 1
                continue

            # TIGHTEN ohne close_pct (oder close_pct=0): nur momentum_faded setzen
            if recommendation == "TIGHTEN" and rec_close_pct <= 0:
                if not dry_run:
                    try:
                        mark_momentum_faded(db, position_id, symbol)
                        rec["executed"] = True
                        rec["executed_at"] = now.isoformat()[:19]
                        rec["executed_reason"] = "llm_tighten_momentum_faded"
                        changed = True
                        stats["tighten_count"] += 1
                        _discord("post_alert_embed",
                                 title="LLM TIGHTEN (indirekt): %s" % symbol,
                                 description="**Grund:** %s\nmomentum_faded gesetzt." % reason,
                                 severity="INFO", channel="trades")
                    except Exception as exc:
                        stats["errors"].append("TIGHTEN %s: %s" % (symbol, exc))
                else:
                    stats["tighten_count"] += 1
                continue

            # Direkter Teilverkauf (TIGHTEN mit close_pct ODER EXIT)
            logger.info("[llm_execution] %s %s %.0f%% (position=%s) %s",
                        recommendation, symbol, rec_close_pct, position_id,
                        "[DRY-RUN]" if dry_run else "ausfuehren...")

            if not dry_run:
                try:
                    # feat/pnl-nachreport: Snapshot VOR dem Close — liefert
                    # pnl_pct/Preise fuer Embed + Chart (Portfolio wurde vom
                    # Reconciler unmittelbar zuvor synchronisiert).
                    _snap = None
                    try:
                        _snap = db.fetchone(
                            "SELECT amount_usd, open_price, current_price, "
                            "unrealized_pnl_pct FROM portfolio_snapshot "
                            "WHERE api_position_id = ?",
                            (str(position_id),),
                        )
                    except Exception:
                        pass

                    # Fuer Teilverkauf: ECHTE units aus dem Live-Portfolio.
                    # fix/tighten-full-close (2026-07-14, HLAG.DE Trade #385):
                    # der alte Pfad hatte einen Operator-Praezedenz-Bug
                    # ("file:%s" % a / b → TypeError, still geschluckt) →
                    # units_to_deduct blieb None → close_position(None) =
                    # VOLLVERKAUF statt TIGHTEN 25%. Zusaetzlich ignorierte
                    # amount_usd/open_price die Waehrungsumrechnung.
                    units_to_deduct = None
                    if rec_close_pct < 100.0:
                        _live_units = client.get_position_units(position_id)
                        if _live_units and _live_units > 0:
                            units_to_deduct = round(_live_units * (rec_close_pct / 100.0), 8)
                        # FAIL-SAFE: ohne verlaessliche units wird der Teil-
                        # verkauf UEBERSPRUNGEN — ein Berechnungsfehler darf
                        # die Aktion NIE vergroessern (None = Vollverkauf!).
                        if not units_to_deduct or units_to_deduct <= 0:
                            _msg = ("%s: units fuer %s %.0f%% nicht ermittelbar — "
                                    "UEBERSPRUNGEN (kein Vollverkauf-Fallback)"
                                    % (symbol, recommendation, rec_close_pct))
                            logger.error("[llm_execution] %s", _msg)
                            stats["errors"].append(_msg)
                            continue

                    result = client.close_position(
                        position_id=position_id,
                        instrument_id=instr_id,
                        units_to_deduct=units_to_deduct,
                    )
                    if not result:
                        raise RuntimeError("close_position() gab leeres Ergebnis zurueck")

                    rec["executed"]        = True
                    rec["executed_at"]     = now.isoformat()[:19]
                    rec["executed_reason"] = "llm_%s_%.0fpct" % (recommendation.lower(), rec_close_pct)
                    changed = True
                    if recommendation == "EXIT":
                        stats["exit_count"] += 1
                    else:
                        stats["tighten_count"] += 1

                    # Prio 2a: Outcome-Tracking -- Entry fuer spaetere Backfill schreiben
                    _append_outcome_entry(rec, position_id, recommendation, rec_close_pct, now)

                    log_repo.write("INFO", "llm_execution",
                                   "LLM %s %.0f%% ausgefuehrt: %s" % (recommendation, rec_close_pct, symbol),
                                   {"symbol": symbol, "position_id": position_id,
                                    "close_pct": rec_close_pct, "reason": reason,
                                    "age_min": round(age_min, 1)})
                    # feat/pnl-nachreport (2026-07-28): vorher ging ein blosses
                    # post_alert_embed nach #etoro-trading (falscher Channel,
                    # kein PnL, kein Chart). Jetzt: richtiges Close-Embed mit
                    # Trade-Story-Chart nach #trades + trade_events-Record;
                    # der Reconciler traegt das echte netProfit spaeter nach
                    # und editiert dieses Embed.
                    try:
                        _entry = float(_snap["open_price"]) if _snap and _snap["open_price"] else None
                        _cur = float(_snap["current_price"]) if _snap and _snap["current_price"] else None
                        _live_pct = (float(_snap["unrealized_pnl_pct"])
                                     if _snap and _snap["unrealized_pnl_pct"] is not None else None)
                        _amt = float(_snap["amount_usd"]) if _snap and _snap["amount_usd"] else 0.0
                        _closed_slice = _amt * rec_close_pct / 100.0

                        _trow = None
                        try:
                            _trow = db.fetchone(
                                "SELECT id, confirmed_at, created_at FROM trades "
                                "WHERE api_position_id = ? ORDER BY id DESC LIMIT 1",
                                (str(position_id),),
                            )
                        except Exception:
                            pass
                        _opened_at = (_trow["confirmed_at"] or _trow["created_at"]) if _trow else None

                        de = _get_de()
                        _chart_ok = False
                        if de is not None and hasattr(de, "attach_chart"):
                            try:
                                from bot.core.candle_chart import trade_story_png_v2
                                _events = []
                                if _entry:
                                    _events.append({"ts": _opened_at, "type": "ENTRY",
                                                    "price": _entry,
                                                    "label": "Entry %g" % _entry})
                                if _cur:
                                    _ev_type = ("PARTIAL_CLOSE" if rec_close_pct < 99.5
                                                else "EXIT")
                                    _lbl = ("-%.0f%% @%g" % (rec_close_pct, _cur)
                                            if _ev_type == "PARTIAL_CLOSE"
                                            else "Exit %g" % _cur)
                                    _events.append({"ts": None, "type": _ev_type,
                                                    "price": _cur, "label": _lbl})
                                _png = trade_story_png_v2(client, int(instr_id), symbol,
                                                          _events, opened_at=_opened_at)
                                if _png:
                                    de.attach_chart(_png)
                                    _chart_ok = True
                            except Exception:
                                pass

                        _reason_txt = "KI %s: %s" % (recommendation, reason)
                        _post_ok = _discord(
                            "post_position_closed_embed",
                            symbol=symbol,
                            amount_usd=_closed_slice,
                            position_id=str(position_id),
                            entry_price=_entry or 0.0,
                            close_price=_cur or 0.0,
                            pnl_usd=None,           # echtes netProfit folgt per Nachreport
                            pnl_pct=_live_pct,
                            reason=_reason_txt,
                            close_pct=rec_close_pct,
                        )
                        from bot.core.event_log import record_posted_event
                        record_posted_event(
                            db, de, symbol=symbol,
                            event_type=("PARTIAL_CLOSE" if rec_close_pct < 99.5
                                        else "CLOSE"),
                            source=("llm_exit" if recommendation == "EXIT"
                                    else "llm_tighten"),
                            post_result=_post_ok,
                            trade_id=(_trow["id"] if _trow else None),
                            position_id=str(position_id),
                            instrument_id=int(instr_id),
                            close_pct=rec_close_pct,
                            units=units_to_deduct,
                            price=_cur, amount_usd=_closed_slice,
                            pnl_usd=None, pnl_pct=_live_pct,
                            pnl_source="derived", reason=_reason_txt,
                            chart_posted=_chart_ok, reported_final=False,
                        )
                    except Exception as _emb_exc:
                        logger.debug("[llm_execution] Close-Embed fehlgeschlagen: %s", _emb_exc)
                    logger.info("[llm_execution] %s %.0f%% %s abgeschlossen", recommendation, rec_close_pct, symbol)

                except Exception as exc:
                    msg = "%s %s API-Fehler: %s" % (recommendation, symbol, exc)
                    logger.error("[llm_execution] %s", msg)
                    stats["errors"].append(msg)
                    log_repo.write("ERROR", "llm_execution", msg,
                                   {"symbol": symbol, "position_id": position_id})
            else:
                if recommendation == "EXIT":
                    stats["exit_count"] += 1
                else:
                    stats["tighten_count"] += 1

    if changed:
        RECS_PATH.write_text(json.dumps(recs, indent=2, ensure_ascii=False), encoding="utf-8")

    total = stats["exit_count"] + stats["tighten_count"]
    if total > 0:
        logger.info("[llm_execution] Ausgefuehrt: %d EXIT, %d TIGHTEN, %d uebersprungen",
                    stats["exit_count"], stats["tighten_count"], stats["skip_count"])

    return stats
