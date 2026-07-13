"""llm_execution.py — Autonome Ausfuehrung von LLM Position-Empfehlungen.

Wird vom reconciler.py aufgerufen (nach jedem Portfolio-Sync).
Liest data/llm_position_recommendations.json und fuehrt aus:
  EXIT    -> client.close_position() (nur bei offenem Markt)
  TIGHTEN -> mark_momentum_faded() (DB-Flag, kein API-Call noetig)

Safety:
  - MAX_REC_AGE_MIN: Aeltere Empfehlungen ignoriert (Stale-Data-Schutz)
  - is_market_open(symbol): EXIT nur bei offenem Markt; TIGHTEN immer
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


def _discord(fn_name: str, **kwargs) -> None:
    try:
        from bot.discord_embeds import post_alert_embed
        if fn_name == "post_alert_embed":
            post_alert_embed(**kwargs)
    except Exception:
        pass


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
    from bot.core.market_hours import is_market_open

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

            try:
                market_open = is_market_open(symbol)
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
                                 severity="INFO")
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
                    # Fuer Teilverkauf: units_to_deduct berechnen
                    units_to_deduct = None
                    if rec_close_pct < 100.0:
                        import sqlite3 as _sq3
                        try:
                            _conn = _sq3.connect("file:%s?mode=ro" % RECS_PATH.parent.parent / "data/trading.db", uri=True)
                            _conn.row_factory = _sq3.Row
                            _snap = _conn.execute(
                                "SELECT amount_usd, open_price FROM portfolio_snapshot WHERE api_position_id = ?",
                                (position_id,)
                            ).fetchone()
                            _conn.close()
                            if _snap and _snap["open_price"]:
                                _total_units = float(_snap["amount_usd"]) / float(_snap["open_price"])
                                units_to_deduct = round(_total_units * (rec_close_pct / 100.0), 8)
                        except Exception as _ue:
                            logger.debug("[llm_execution] units-Berechnung fehlgeschlagen: %s", _ue)

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

                    log_repo.write("INFO", "llm_execution",
                                   "LLM %s %.0f%% ausgefuehrt: %s" % (recommendation, rec_close_pct, symbol),
                                   {"symbol": symbol, "position_id": position_id,
                                    "close_pct": rec_close_pct, "reason": reason,
                                    "age_min": round(age_min, 1)})
                    _icon = "KI EXIT" if recommendation == "EXIT" else "KI TIGHTEN"
                    desc = "**Grund:** %s\n**%.0f%% der Position** geschlossen." % (reason, rec_close_pct)
                    _discord("post_alert_embed",
                             title="%s: %s" % (_icon, symbol),
                             description=desc,
                             severity="WARNING" if recommendation == "EXIT" else "INFO")
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
