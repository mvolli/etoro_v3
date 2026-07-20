#!/usr/bin/env python3
"""eToro Trading Bot V3 — Execution Worker
src/bot/workers/execution_worker.py

Runs every 15 minutes at :04.
Executes APPROVED trades via the eToro API.

Schedule: 4,19,34,49 * * * * cd /path/to/etoro_v3 && python3 -m bot.workers.execution_worker
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,  # INFO→suppressed; nur Warnings/Errors auf stdout (Discord via cron)
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("execution_worker")


# ── Discord Embeds ─────────────────────────────────────────────────────────
try:
    from pathlib import Path as _Path
    _bot_dir = str(_Path(__file__).resolve().parent.parent)
    import sys as _sys
    if _bot_dir not in _sys.path:
        _sys.path.insert(0, _bot_dir)
    import discord_embeds as _DE
except Exception:
    _DE = None

_CHART_CTX: dict = {}


def _set_chart_ctx(client, instrument_id, symbol) -> None:
    """Kontext fuer den automatischen Entry-Chart im Fill-Embed."""
    _CHART_CTX.update(client=client, instrument_id=instrument_id, symbol=symbol)


def _entry_chart_png() -> bytes | None:
    """1H-Entry-Chart aus eToro-Candles (best effort, wirft nie)."""
    try:
        from bot.core.candle_chart import render_candles_png
        c = _CHART_CTX.get("client")
        iid = _CHART_CTX.get("instrument_id")
        if not c or not iid:
            return None
        return render_candles_png(
            c.get_candles(int(iid), "OneHour", 60),
            f"{_CHART_CTX.get('symbol')} — 1H (eToro)",
        )
    except Exception:
        return None


def _post(fn_name: str, **kwargs) -> None:
    """Best-effort Discord post. Never raises."""
    try:
        # feat/candle-charts: Fill-Embeds bekommen automatisch den 1H-Chart
        if fn_name == "post_trade_filled_embed" and _DE and hasattr(_DE, "attach_chart"):
            _png = _entry_chart_png()
            if _png:
                _DE.attach_chart(_png)
        if _DE and hasattr(_DE, fn_name):
            getattr(_DE, fn_name)(**kwargs)
    except Exception as _e:
        pass


def _load_config() -> dict:
    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def _load_env() -> None:
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        logger.warning(".env not found at %s — relying on existing environment", env_path)
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _position_ids_for_instrument(positions: list, instrument_id) -> set[str]:
    """Return the positionIDs of all positions for *instrument_id*."""
    ids: set[str] = set()
    for pos in positions:
        pos_iid = pos.get("instrumentID") or pos.get("instrumentId")
        if pos_iid == instrument_id:
            pid = str(pos.get("positionID") or pos.get("positionId") or "")
            if pid:
                ids.add(pid)
    return ids


def _find_new_position(positions: list, instrument_id, pre_existing_ids: set[str]):
    """Find a position for *instrument_id* whose positionID was NOT present
    before the order was submitted.

    fix/ghost-order-id-diff: the old check matched ANY position with the
    right instrument_id. With pyramiding (multiple fragments per instrument
    allowed in NORMAL/CAUTION), the OLD fragment matched immediately — a
    ghost order was booked as FILLED with the old position's entry price.
    Comparing against the pre-submit snapshot only accepts a genuinely NEW
    position. Positions without a positionID are only accepted when the
    instrument had no positions before (conservative fallback).
    """
    for pos in positions:
        pos_iid = pos.get("instrumentID") or pos.get("instrumentId")
        if pos_iid != instrument_id:
            continue
        pid = str(pos.get("positionID") or pos.get("positionId") or "")
        if pid:
            if pid not in pre_existing_ids:
                return pos
        elif not pre_existing_ids:
            return pos
    return None


# ── FAILED-Trade Requeue (fix/failed-trade-requeue) ──────────────────────────
# Ein transient gescheiterter Trade (API-Timeout, 5xx, Netzabriss) war bisher
# terminal: das Signal ist bei Trade-Erstellung CONSUMED, es gab keinen
# Cross-Cycle-Retry. Jetzt: EINMALIGER Requeue (requeue_count 0→1) innerhalb
# von REQUEUE_MAX_AGE_MIN. Bewusst NICHT requeued werden strukturelle Fehler:
# REJECTED (Gates/Regime), "Ghost order:" (eigene Blacklist-Maschinerie),
# "Blocked:" (allowOpenPosition=false etc.), "Market closed:". Der requeued
# Trade durchlaeuft im selben Zyklus ALLE Execution-Gates erneut (Regime,
# Market-Hours, Slippage, Duplicate-Guard).
REQUEUE_MAX_AGE_MIN = 60

_TRANSIENT_HTTP_RE = re.compile(r"HTTP 5\d\d\b")
_TRANSIENT_MARKERS = ("timeout", "timed out", "connection", "temporarily unavailab")


def is_transient_failure(rejection_reason: str | None) -> bool:
    """Pure classifier: True nur fuer transiente API-/Netzfehler.

    Akzeptiert werden ausschliesslich Reasons aus dem APIError-Pfad
    ("APIError: HTTP 5xx …" / Timeout / Connection) und dem
    Unexpected-Pfad mit eindeutigen Netz-Markern. Alles andere
    (Ghost order, Blocked, Market closed, Regime, Slippage) ist strukturell.
    """
    if not rejection_reason:
        return False
    reason = rejection_reason.strip()
    lowered = reason.lower()
    if reason.startswith("APIError"):
        return bool(_TRANSIENT_HTTP_RE.search(reason)) or any(
            m in lowered for m in _TRANSIENT_MARKERS
        )
    if reason.startswith("Unexpected error"):
        return any(m in lowered for m in _TRANSIENT_MARKERS)
    return False


def _age_minutes(created_at: str | None, now: datetime | None = None) -> float | None:
    """Age of a `trades.created_at` UTC timestamp in minutes (None if unparseable)."""
    if not created_at:
        return None
    try:
        ts = datetime.strptime(str(created_at).strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - ts).total_seconds() / 60.0


def classify_requeue(trade: dict, now: datetime | None = None) -> bool:
    """Pure decision: darf dieser FAILED-Trade genau einmal requeued werden?"""
    if int(trade.get("requeue_count") or 0) != 0:
        return False
    if not is_transient_failure(trade.get("rejection_reason")):
        return False
    age = _age_minutes(trade.get("created_at"), now)
    return age is not None and 0 <= age <= REQUEUE_MAX_AGE_MIN


# ── fix/ghost-defer-idempotent (2026-07-16) ──────────────────────────────────
# Ein DEFER liess bisher die orderId der bereits platzierten Order ungenutzt:
# der naechste Zyklus POSTete blind eine NEUE Order. Wenn die alte spaeter
# fuellte, hatten wir zwei Positionen fuer ein Sizing (Doppel-Fill) — die
# letzte strukturelle Ghost-/Duplikat-Quelle. Jetzt wird eine vorhandene
# orderId beim Wiederanlauf zuerst per GET /orders/{id} aufgeloest; neu
# gePOSTet wird NUR, wenn eToro die Order nachweislich nicht kennt (404).
DEFER_CAP = 3  # max. DEFER-Zyklen pro Order (Spiegel des Portfolio-Polling-Caps)

_SCALP_SIGNAL_TYPES = frozenset({
    "BB_LOWER_RSI_OVERSOLD", "BB_EXTREME_RSI_OVERSOLD",
    "RSI_EXTREME_OVERSOLD", "BB_LOW_MACD_IMPROVING",
})


def resolve_deferred_action(pf: dict, defer_count: int, cap: int = DEFER_CAP) -> str:
    """Pure decision: was passiert mit einem deferten Trade, dessen fruehere
    Order per GET /orders/{id} aufgeloest wurde?

    Rueckgabe:
      ACTIVE          — executed + Position vorhanden -> Trade bestaetigen
      DEFER           — pending / Zustand unklar (Transportfehler) -> warten
      REPOST          — eToro kennt die orderId nicht (404) -> neuer POST sicher
      FAILED_REJECTED — eToro hat abgelehnt (rejectionReason)
      FAILED          — eToro meldet Failed/Expired (echter API-Status)
      GHOST_FAILED    — Defer-Cap erreicht ohne Aufloesung -> Ghost-Maschinerie
    """
    status = pf.get("status")
    below_cap = defer_count < cap
    if status == "executed":
        if pf.get("positions"):
            return "ACTIVE"
        return "DEFER" if below_cap else "GHOST_FAILED"
    if status == "pending":
        if pf.get("is_timing_issue"):
            return "REPOST"
        return "DEFER" if below_cap else "GHOST_FAILED"
    if status == "rejected":
        return "FAILED_REJECTED"
    if status == "failed" and not pf.get("transport_error"):
        return "FAILED"
    # unknown / Transportfehler: Order-Zustand unbekannt — NIE blind neu POSTen
    return "DEFER" if below_cap else "GHOST_FAILED"


_MIN_AMOUNT_RE = re.compile(r"MinimumPositionAmount:\s*(\d+(?:\.\d+)?)")


def parse_min_position_amount(reason: str | None) -> float | None:
    """Extrahiert das Broker-Minimum aus eToro-Fehler 720 (pure, testbar)."""
    if not reason:
        return None
    m = _MIN_AMOUNT_RE.search(reason)
    return float(m.group(1)) if m else None


def is_internal_only_error(reason: str | None) -> bool:
    """eToro-Fehler 814: Instrument nur intern sichtbar, nie handelbar (pure)."""
    return bool(reason) and "visible internal only" in reason


def _learn_from_rejection(db, instrument_id, symbol: str, reason: str | None) -> None:
    """Aus Order-Ablehnungen lernen (fix/order-error-learning 2026-07-16):
    720 -> instruments.min_position_amount (signal_worker sized dann gar nicht
    erst darunter), 814 -> is_tradable=0 (Discovery/Signal filtern darauf).
    Best effort, wirft nie."""
    import logging as _logging
    log = _logging.getLogger(__name__)
    try:
        broker_min = parse_min_position_amount(reason)
        if broker_min:
            db.execute(
                "UPDATE instruments SET min_position_amount = ? WHERE instrument_id = ?",
                (broker_min, instrument_id),
            )
            log.info(
                "ExecutionWorker: %s Broker-Minimum $%.0f gelernt (eToro 720)",
                symbol, broker_min,
            )
        if is_internal_only_error(reason):
            db.execute(
                "UPDATE instruments SET is_tradable = 0, "
                "tradability_checked_at = datetime('now') WHERE instrument_id = ?",
                (instrument_id,),
            )
            log.info(
                "ExecutionWorker: %s dauerhaft nicht handelbar (eToro 814) — is_tradable=0",
                symbol,
            )
    except Exception as exc:
        log.warning(
            "ExecutionWorker: learn_from_rejection fehlgeschlagen fuer %s: %s",
            symbol, exc,
        )


def _apply_strategy_tag(db, trade: dict, api_position_id: str, symbol: str) -> None:
    """Scalp/Swing-Tagging fuers Trailing (best effort, wirft nie)."""
    import logging as _logging
    try:
        _sig_type = ""
        _signal_id = trade.get("signal_id")
        if _signal_id:
            _sig_row = db.fetchone(
                "SELECT signal_type FROM signals WHERE id = ?", (_signal_id,)
            )
            if _sig_row:
                _sig_type = str(_sig_row["signal_type"] or "")
        _strategy = (
            "scalp" if any(s in _sig_type for s in _SCALP_SIGNAL_TYPES) else "swing"
        )
        from bot.core.trailing_stop import set_strategy as _set_strategy
        _set_strategy(db, api_position_id, symbol, _strategy)
    except Exception as _strat_exc:
        _logging.getLogger(__name__).debug(
            "ExecutionWorker: strategy-tagging fehlgeschlagen fuer %s — %s",
            symbol, _strat_exc,
        )


def main() -> None:
    # ── Worker lock: prevent overlapping cron invocations ────────────────────
    from bot.core.worker_lock import worker_lock

    with worker_lock("execution_worker") as acquired:
        if not acquired:
            logger.warning("ExecutionWorker: previous run still active — skipping this cycle")
            print("ExecutionWorker: SKIPPED (already running)")
            return

        # ── 1. Setup ──────────────────────────────────────────────────────────────
        _load_env()
        cfg = _load_config()
    
        from bot.api.client import APIError, ClientConfig, EToroClient
        from bot.core.market_hours import is_market_open, get_market_status
        from bot.core.risk import apply_config as _apply_risk_config
        from bot.db.connection import DB
        from bot.db.repo import LogRepo, StateRepo, TradeRepo
        _apply_risk_config(cfg)  # fix/risk-config-wiring
    
        db_path = PROJECT_ROOT / cfg["db"]["path"]
        busy_timeout = cfg["db"].get("busy_timeout_ms", 5000)
        db = DB(db_path=db_path, busy_timeout_ms=busy_timeout)
    
        trade_repo = TradeRepo(db)
        state_repo = StateRepo(db)
        log_repo = LogRepo(db)

        # ── Heartbeat (dead-man's switch) ─────────────────────────────────────────
        from bot.core.heartbeat import record_heartbeat
        record_heartbeat(state_repo, "execution_worker")
        import time as _time_dur
        _t_run_start = _time_dur.monotonic()

        # ── Kill Switch check (fix/autonomy-hardening) ────────────────────────────
        # Previously the execution worker relied ONLY on the DB regime, which
        # the risk worker sets with up to 5 minutes latency. In that window,
        # already-APPROVED trades would execute despite an active kill switch.
        # Check the flag file directly AND reject all pending approvals so
        # stale APPROVED trades cannot fire later once the switch is lifted.
        from bot.core.kill_switch import is_kill_switch_active, get_reason
        if is_kill_switch_active():
            ks_reason = get_reason() or "Manual kill switch"
            stale_approved = trade_repo.get_by_status("APPROVED")
            for t in stale_approved:
                trade_repo.update_status(
                    t["id"],
                    "REJECTED",
                    rejection_reason=f"Kill switch active: {ks_reason}",
                )
            logger.warning(
                "ExecutionWorker: KILL SWITCH ACTIVE (%s) — %d APPROVED trade(s) rejected, exiting",
                ks_reason, len(stale_approved),
            )
            log_repo.write(
                "WARN",
                "execution_worker",
                f"Kill switch active — {len(stale_approved)} APPROVED trade(s) rejected ({ks_reason})",
            )
            print(f"ExecutionWorker: KILL SWITCH — {len(stale_approved)} approvals rejected, no execution")
            return

        api_key = os.environ.get("ETORO_API_KEY", "")
        user_key = os.environ.get("ETORO_USER_KEY", "")
        client_cfg = ClientConfig.from_dict(cfg.get("api", {}))
        client = EToroClient(api_key=api_key, user_key=user_key, config=client_cfg)

        # ── 2a. One-shot Requeue transient gescheiterter Trades ──────────────────
        # (fix/failed-trade-requeue — Details siehe classify_requeue oben.)
        try:
            _requeued = 0
            for _ft in trade_repo.get_by_status("FAILED"):
                if not classify_requeue(_ft):
                    continue
                _prev_reason = str(_ft.get("rejection_reason") or "")[:150]
                trade_repo.update_status(
                    _ft["id"],
                    "APPROVED",
                    requeue_count=1,
                    rejection_reason=f"REQUEUED (transient): {_prev_reason}",
                )
                logger.warning(
                    "ExecutionWorker: trade #%d (%s) REQUEUED after transient failure — %s",
                    _ft["id"], _ft.get("symbol", "?"), _prev_reason,
                )
                log_repo.write(
                    "WARN",
                    "execution_worker",
                    f"Trade #{_ft['id']} REQUEUED (one-shot) after transient failure",
                    {"symbol": _ft.get("symbol"), "prev_reason": _prev_reason},
                )
                _requeued += 1
            if _requeued:
                print(f"ExecutionWorker: {_requeued} transient FAILED trade(s) requeued")
        except Exception as _rq_exc:
            # Requeue ist Komfort, nie kritisch — darf den Zyklus nicht brechen.
            logger.error("ExecutionWorker: requeue scan failed: %s", _rq_exc)

        # ── 2. Fetch all APPROVED trades ──────────────────────────────────────────
        approved_trades = trade_repo.get_by_status("APPROVED")
    
        if not approved_trades:
            logger.info("ExecutionWorker: no APPROVED trades to process")
            logger.debug("ExecutionWorker: 0 trades processed, 0 filled, 0 failed")
            client.close()
            return
    
        logger.info("ExecutionWorker: found %d APPROVED trade(s)", len(approved_trades))
    
        # Also expire old signals (soft delete)
        from bot.db.repo import SignalRepo
        signal_repo = SignalRepo(db)
        expired_count = signal_repo.expire_old()
        if expired_count > 0:
            logger.info("ExecutionWorker: expired %d stale signals", expired_count)
    
        processed_count = 0
        filled_count = 0
        failed_count = 0
    
        # ── c) Duplicate-instrument guard ───────────────────────────────────────
        # get_by_status("APPROVED") can legitimately contain two different trade
        # rows for the same instrument_id (e.g. approved in two separate
        # signal_worker cycles before either got executed). Without a per-run
        # dedup, both would be submitted back-to-back, doubling exposure beyond
        # what sizing intended and increasing ghost-order surface area.
        # get_by_status() orders by created_at ASC, so the oldest (first
        # approved) trade per instrument executes; later duplicates are
        # rejected for this run.
        seen_instrument_ids: set = set()
    
        # ── 3. Process each APPROVED trade ────────────────────────────────────────
        for trade in approved_trades:
            trade_id = trade["id"]
            instrument_id = trade["instrument_id"]
            symbol = trade.get("symbol", str(instrument_id))
            _set_chart_ctx(client, instrument_id, symbol)
            amount_usd = float(trade.get("amount_usd", 0.0))
            stop_loss_pct = float(trade.get("stop_loss_pct") or cfg.get("sl", {}).get("default_pct", 3.0))
    
            if instrument_id in seen_instrument_ids:
                logger.warning(
                    "ExecutionWorker: trade #%d (%s) DUPLICATE instrument_id=%s "
                    "already processed this run — rejecting to avoid double exposure",
                    trade_id, symbol, instrument_id,
                )
                trade_repo.update_status(
                    trade_id,
                    "REJECTED",
                    rejection_reason=f"Duplicate instrument_id={instrument_id} in same execution batch",
                )
                log_repo.write(
                    "WARN",
                    "execution_worker",
                    f"Trade #{trade_id} REJECTED — duplicate instrument {symbol} in same batch",
                    {"symbol": symbol, "instrument_id": instrument_id},
                )
                continue
            seen_instrument_ids.add(instrument_id)
    
            # a. Atomic lock: APPROVED → SUBMITTING
            locked = trade_repo.lock_for_submission(trade_id)
            if not locked:
                logger.debug(
                    "ExecutionWorker: trade #%d already being processed — skipping", trade_id
                )
                continue
    
            processed_count += 1
            trade_repo.update_status(trade_id, "SUBMITTING", submitted_at=_utcnow())
    
            # a2. Deferred-Order-Resolution (fix/ghost-defer-idempotent):
            # Traegt der Trade bereits eine orderId aus einem frueheren POST,
            # wird DIESE Order aufgeloest statt blind eine zweite zu platzieren.
            # Laeuft bewusst VOR den Gates — eine platzierte Order aufzuloesen
            # ist Buchhaltung, keine neue Risiko-Entscheidung.
            _prev_order_id = str(trade.get("order_id") or "").strip()
            _prev_defers = int(trade.get("requeue_count") or 0)
            if _prev_order_id.isdigit() and _prev_defers > 0:
                pf_prev = client.get_order_status(int(_prev_order_id), env="real")
                _action = resolve_deferred_action(pf_prev, _prev_defers, DEFER_CAP)
                logger.info(
                    "ExecutionWorker: trade #%d deferred-order-check orderId=%s status=%s -> %s",
                    trade_id, _prev_order_id, pf_prev.get("status"), _action,
                )
                if _action == "ACTIVE":
                    pos = (pf_prev.get("positions") or [{}])[0]
                    api_position_id = str(
                        pos.get("positionID") or pos.get("positionId") or _prev_order_id
                    )
                    trade_repo.update_status(
                        trade_id, "ACTIVE",
                        api_position_id=api_position_id,
                        order_id=_prev_order_id,
                        entry_price=None,  # Reconciler backfillt openRate
                        confirmed_at=_utcnow(),
                    )
                    trade_repo.reset_ghost_failures(instrument_id)
                    _apply_strategy_tag(db, trade, api_position_id, symbol)
                    filled_count += 1
                    log_repo.write(
                        "INFO", "execution_worker",
                        f"Trade #{trade_id} ACTIVE: {symbol} BUY ${amount_usd:.2f} "
                        f"(deferte Order aufgeloest, positionID={api_position_id})",
                        {"trade_id": trade_id, "symbol": symbol,
                         "order_id": _prev_order_id, "api_position_id": api_position_id},
                    )
                    _post('post_trade_filled_embed',
                        symbol=symbol, direction='BUY', amount_usd=amount_usd,
                        position_id=api_position_id, entry_price=0.0,
                        sl_pct=stop_loss_pct,
                        reason=f"Deferte Order {_prev_order_id} aufgeloest — Kurs folgt via Reconciler",
                        dry_run=False,
                    )
                    continue
                if _action == "DEFER":
                    trade_repo.update_status(
                        trade_id, "APPROVED", requeue_count=_prev_defers + 1,
                    )
                    _post('post_alert_embed',
                        title=f"⏳ DEFER {_prev_defers + 1}/{DEFER_CAP} — {symbol}",
                        description=(
                            f"Order {_prev_order_id} noch nicht aufgeloest "
                            f"(Status: {pf_prev.get('status')}) — naechster Versuch in 15min."
                        ),
                        severity="INFO", channel="trades",
                    )
                    processed_count -= 1
                    continue
                if _action in ("FAILED_REJECTED", "FAILED"):
                    if _action == "FAILED_REJECTED":
                        _learn_from_rejection(
                            db, instrument_id, symbol, pf_prev.get("rejection_reason"),
                        )
                    _reason = (
                        f"Order rejected: {pf_prev.get('rejection_reason') or 'unknown'}"
                        if _action == "FAILED_REJECTED"
                        else "Order failed: statusID=Failed/Expired (deferte Order)"
                    )
                    trade_repo.update_status(
                        trade_id, "FAILED", rejection_reason=_reason[:200],
                    )
                    log_repo.write(
                        "WARN", "execution_worker",
                        f"Trade #{trade_id} {_action}: {symbol} — {_reason}",
                        {"symbol": symbol, "order_id": _prev_order_id},
                    )
                    _post('post_trade_failed_embed',
                        symbol=symbol, direction='BUY', amount_usd=amount_usd,
                        error=_reason[:150], dry_run=False,
                    )
                    failed_count += 1
                    continue
                if _action == "GHOST_FAILED":
                    ghost_count, bl_status = trade_repo.record_ghost_failure(instrument_id)
                    _reason = (
                        f"Ghost order: orderId={_prev_order_id} nach {_prev_defers} Defers "
                        f"nicht aufgeloest (letzter Status: {pf_prev.get('status')}, "
                        f"failure #{ghost_count}, blacklist: {bl_status})"
                    )
                    trade_repo.update_status(
                        trade_id, "FAILED", rejection_reason=_reason[:200],
                    )
                    log_repo.write(
                        "WARN", "execution_worker",
                        f"Trade #{trade_id} GHOST ORDER (Defer-Cap): {symbol} orderId={_prev_order_id}",
                        {"symbol": symbol, "order_id": _prev_order_id,
                         "ghost_count": ghost_count, "blacklist_status": bl_status},
                    )
                    _post('post_trade_failed_embed',
                        symbol=symbol, direction='BUY', amount_usd=amount_usd,
                        error=_reason[:150], dry_run=False,
                    )
                    failed_count += 1
                    continue
                # _action == "REPOST": 404 — eToro kennt die Order nicht (mehr).
                # Normaler Flow POSTet frisch; alle Gates greifen wie gewohnt.

            # b. Double-safety regime check (V5: NORMAL / CAUTION / DEFENSIVE / CRITICAL)
            regime = state_repo.get_regime()
            if regime in ("DEFENSIVE", "CRITICAL"):
                logger.warning(
                    "ExecutionWorker: %s regime at execution time — rejecting trade #%d (%s)",
                    regime, trade_id, symbol,
                )
                trade_repo.update_status(
                    trade_id,
                    "REJECTED",
                    rejection_reason=f"{regime} regime at execution time",
                )
                log_repo.write(
                    "WARN",
                    "execution_worker",
                    f"Trade #{trade_id} REJECTED — {regime} regime at execution time",
                    {"symbol": symbol, "amount_usd": amount_usd},
                )
                failed_count += 1
                continue
    
            # c. Market hours gate — statischer Check als Schnell-Skip ohne API-Call.
            #    Aktion: DEFER (bleibt APPROVED) statt FAILED — Execution-Worker
            #    wiederholt alle 15min. Wenn eToro öffnet, schlägt allowEntryOrders
            #    in open_position() an und die Order geht durch.
            if not is_market_open(symbol, fail_open=False):
                mkt_status = get_market_status(symbol)
                logger.debug(
                    'ExecutionWorker: %s — Markt statisch geschlossen (%s), DEFER bis Marktöffnung',
                    symbol, mkt_status,
                )
                # fix/submitting-revert: Trade ist bereits SUBMITTING (lock_for_submission).
                # Ohne Revert markiert der Reconciler es nach 5 min als stale FAILED.
                trade_repo.update_status(trade_id, "APPROVED")
                processed_count -= 1
                continue  # retry im nächsten Zyklus (15min)
    
            # c2. Slippage gate (fix/autonomy-hardening) — the signal price is
            #     stored at approval time but was previously never used. Block
            #     execution when the live price has drifted too far from it.
            from bot.core.risk import check_slippage_gate, get_max_slippage_pct
            signal_price = float(trade.get("signal_price") or 0.0)
            try:
                current_price = client.get_current_price(instrument_id)
            except Exception as _price_exc:
                # Fail-open: check_slippage_gate handles current_price=None gracefully
                logger.warning(
                    "ExecutionWorker: get_current_price failed for %s (%s) — slippage gate skipped",
                    symbol, _price_exc,
                )
                current_price = None
            slippage_gate = check_slippage_gate(
                symbol=symbol,
                signal_price=signal_price if signal_price > 0 else None,
                current_price=current_price,
                max_slippage_pct=get_max_slippage_pct(symbol, cfg),
            )
            if not slippage_gate.allowed:
                reason = slippage_gate.summary()
                # fix/slippage-blacklist: Block zählen — ab 3 Blocks/7d wird
                # das Instrument im signal_worker gar nicht mehr getradet.
                trade_repo.record_slippage_reject(instrument_id, symbol, source="execution")
                logger.warning(
                    "ExecutionWorker: trade #%d (%s) BLOCKED by slippage gate — %s",
                    trade_id, symbol, reason,
                )
                trade_repo.update_status(
                    trade_id,
                    "REJECTED",
                    rejection_reason=reason[:200],
                )
                log_repo.write(
                    "WARN",
                    "execution_worker",
                    f"Trade #{trade_id} REJECTED — slippage gate: {symbol}",
                    {
                        "symbol": symbol,
                        "signal_price": signal_price,
                        "current_price": current_price,
                        "reason": reason,
                    },
                )
                _post('post_trade_failed_embed',
                    symbol=symbol,
                    direction='BUY',
                    amount_usd=amount_usd,
                    error=f"Slippage-Gate: {reason[:150]}",
                    blocked=True,  # fix/risk-block-framing: Schutz, kein Fehler
                    dry_run=False,
                )
                failed_count += 1
                continue

            # c2b. Spread-Gate (feat/spread-gate, OSS-Fund 2026-07-16):
            #      (ask-bid)/mid aus der Echtzeit-Rate — schuetzt vor stillen
            #      Kosten illiquider Titel (EU-Smallcap-Klasse). Fail-open
            #      bei fehlendem bid/ask.
            from bot.core.risk import check_spread_gate
            # Krypto hat strukturell breitere Spreads (eToro 1-3%) — eigene
            # Schwelle, sonst waere Wochenend-Trading faktisch tot.
            try:
                _ac_row = db.fetchone(
                    "SELECT asset_class FROM instruments WHERE instrument_id = ?",
                    (instrument_id,),
                )
                _is_crypto = bool(_ac_row and _ac_row["asset_class"] == "crypto")
            except Exception:
                _is_crypto = False
            _max_spread = float(
                cfg.get("trading", {}).get("max_spread_pct_crypto", 3.0)
                if _is_crypto
                else cfg.get("trading", {}).get("max_spread_pct", 1.5)
            )
            _sp_ok, _spread_pct = check_spread_gate(client.get_rate(instrument_id), _max_spread)
            if not _sp_ok:
                reason = f"Spread-Gate: {_spread_pct:.2f}% > {_max_spread:.2f}% (bid/ask)"
                trade_repo.record_slippage_reject(instrument_id, symbol, source="spread")
                logger.warning(
                    "ExecutionWorker: trade #%d (%s) BLOCKED — %s", trade_id, symbol, reason,
                )
                trade_repo.update_status(trade_id, "REJECTED", rejection_reason=reason[:200])
                log_repo.write(
                    "WARN", "execution_worker",
                    f"Trade #{trade_id} REJECTED — {reason}",
                    {"symbol": symbol, "spread_pct": _spread_pct},
                )
                _post('post_trade_failed_embed',
                    symbol=symbol, direction='BUY', amount_usd=amount_usd,
                    error=reason[:150], blocked=True,  # fix/risk-block-framing
                    dry_run=False,
                )
                failed_count += 1
                continue

            # d0. Snapshot existing positionIDs for this instrument BEFORE
            #     submitting (fix/ghost-order-id-diff). Verification below
            #     only accepts a position whose ID is NOT in this set —
            #     an existing pyramiding fragment must never confirm a
            #     ghost order as FILLED.
            pre_existing_pos_ids: set[str] = set()
            try:
                _pre_positions = (
                    client.get_portfolio()
                    .get("clientPortfolio", {})
                    .get("positions", [])
                )
                pre_existing_pos_ids = _position_ids_for_instrument(
                    _pre_positions, instrument_id
                )
            except Exception as _pre_exc:
                # Fail-open: empty set = legacy behaviour (any position matches).
                logger.warning(
                    "ExecutionWorker: pre-submit portfolio snapshot failed (%s) — "
                    "falling back to any-position match for trade #%d",
                    _pre_exc, trade_id,
                )

            # d. Call eToro API to open the position
            try:
                result = client.open_position(
                    instrument_id=instrument_id,
                    amount_usd=amount_usd,
                    stop_loss_pct=stop_loss_pct,
                    symbol=symbol,
                    take_profit_pct=float(cfg.get("sl", {}).get("safety_tp_pct", 25.0)),
                )
    
                # d1. BLOCKED path — open_position() returned a soft-block dict
                if result.get("success") is False:
                    block_error = result.get("error", "Unknown block reason")
                    # allowEntryOrders=false = Markt temporär geschlossen → DEFER
                    # Trade bleibt APPROVED, wird beim nächsten Zyklus neu versucht.
                    if "allowEntryOrders" in block_error:
                        logger.debug(
                            "ExecutionWorker: trade #%d (%s) — allowEntryOrders=false, DEFER",
                            trade_id, symbol,
                        )
                        # fix/submitting-revert: Revert SUBMITTING → APPROVED für Retry.
                        trade_repo.update_status(trade_id, "APPROVED")
                        processed_count -= 1
                        continue  # retry im nächsten Zyklus (15min)
                    # Alle anderen Blocks (allowOpenPosition=false, SL gate, etc.) → FAILED
                    logger.warning(
                        "ExecutionWorker: trade #%d (%s) BLOCKED by open_position(): %s",
                        trade_id, symbol, block_error,
                    )
                    _learn_from_rejection(db, instrument_id, symbol, block_error)
                    trade_repo.update_status(
                        trade_id,
                        "FAILED",
                        rejection_reason=f"Blocked: {block_error}",
                    )
                    log_repo.write(
                        "WARN",
                        "execution_worker",
                        f"Trade #{trade_id} BLOCKED: {symbol} — {block_error}",
                        {"symbol": symbol, "block_reason": block_error},
                    )
                    _post('post_trade_failed_embed',
                        symbol=symbol,
                        direction='BUY',
                        amount_usd=amount_usd,
                        error=f"Pre-flight block: {block_error}",
                        dry_run=False,
                    )
                    failed_count += 1
                    continue
    
                # d2. Success — extract result fields
                # eToro v2 /trading/execution/orders returns ONLY the order ID on success.
                api_position_id = str(
                    result.get("positionId")
                    or result.get("position_id")
                    or result.get("orderId")      # v2 API returns orderId
                    or result.get("id")
                    or ""
                )
    
                # d3. Empty response guard — catches {} or unexpected API shape
                if not api_position_id:
                    logger.warning(
                        "ExecutionWorker: trade #%d (%s) open_position() returned no order ID (empty/unexpected response)",
                        trade_id, symbol,
                    )
    
                # Store order_id immediately so we can track it even if position verification fails
                if api_position_id:
                    trade_repo.update_status(
                        trade_id,
                        "SUBMITTING",
                        order_id=api_position_id,
                        submitted_at=_utcnow(),
                    )
    
                # d4. POST-FLIGHT ORDER-STATUS-CHECK (fix/ghost-order-elimination)
                # Verify order status via GET /orders/{orderId} BEFORE portfolio polling.
                # Distinguishes Rejected (with rejectionReason), Deferred (Pending),
                # True Ghost (Executed but no position), or timing issue (404).
                # Replaces 10 portfolio polls with 1 API call — eliminates ~80% false-positives.
                import time as _time  # früh importieren für Ghost-Retry
                post_flight_result = None
                if api_position_id:
                    try:
                        post_flight_result = client.get_order_status(
                            int(api_position_id), env="real"
                        )
                        logger.info(
                            "ExecutionWorker: trade #%d post-flight: orderId=%s status=%s positions=%s",
                            trade_id, api_position_id,
                            post_flight_result["status"],
                            "yes" if post_flight_result["positions"] else "no",
                        )
                    except Exception as _pf_exc:
                        logger.warning(
                            "ExecutionWorker: trade #%d post-flight check failed (%s) — "
                            "falling back to portfolio polling",
                            trade_id, _pf_exc,
                        )
                        post_flight_result = None
    
                # d4a. Post-flight decision tree
                if post_flight_result is not None:
                    pf = post_flight_result
                    if pf["status"] == "rejected":
                        logger.warning(
                            "ExecutionWorker: trade #%d REJECTED: %s",
                            trade_id, pf["rejection_reason"] or "unknown",
                        )
                        _learn_from_rejection(db, instrument_id, symbol, pf.get("rejection_reason"))
                        trade_repo.update_status(
                            trade_id,
                            "FAILED",
                            rejection_reason=f"Order rejected: {pf['rejection_reason'] or 'unknown'}",
                        )
                        log_repo.write(
                            "WARN", "execution_worker",
                            f"Trade #{trade_id} REJECTED: {symbol} — {pf['rejection_reason'] or 'unknown'}",
                            {"symbol": symbol, "order_id": api_position_id, "rejection_reason": pf["rejection_reason"]},
                        )
                        _post('post_trade_failed_embed',
                            symbol=symbol, direction='BUY', amount_usd=amount_usd,
                            error=f"Order rejected: {pf['rejection_reason'] or 'unknown'}", dry_run=False,
                        )
                        failed_count += 1
                        continue
    
                    if pf["status"] == "failed":
                        err_msg = pf["raw"].get("error", "unknown")
                        logger.warning("ExecutionWorker: trade #%d FAILED: %s", trade_id, err_msg)
                        trade_repo.update_status(
                            trade_id, "FAILED",
                            rejection_reason=f"Order failed: {err_msg}",
                        )
                        log_repo.write(
                            "WARN", "execution_worker",
                            f"Trade #{trade_id} FAILED: {symbol} — {err_msg}",
                            {"symbol": symbol, "order_id": api_position_id, "error": err_msg},
                        )
                        _post('post_trade_failed_embed',
                            symbol=symbol, direction='BUY', amount_usd=amount_usd,
                            error=f"Order failed: {err_msg}", dry_run=False,
                        )
                        failed_count += 1
                        continue
    
                    if pf["status"] == "pending":
                        # fix/ghost-defer-idempotent: Cap wie im Fallback-Pfad —
                        # ohne Cap deferte eine ewig-pendende Order unbegrenzt.
                        _pend_defers = int(trade.get("requeue_count") or 0)
                        if _pend_defers >= DEFER_CAP:
                            ghost_count, bl_status = trade_repo.record_ghost_failure(instrument_id)
                            _reason = (
                                f"Ghost order: orderId={api_position_id} dauerhaft pending "
                                f"({_pend_defers} Defers, failure #{ghost_count}, blacklist: {bl_status})"
                            )
                            trade_repo.update_status(
                                trade_id, "FAILED", rejection_reason=_reason[:200],
                            )
                            log_repo.write(
                                "WARN", "execution_worker",
                                f"Trade #{trade_id} GHOST ORDER (pending-Cap): {symbol} orderId={api_position_id}",
                                {"symbol": symbol, "order_id": api_position_id, "ghost_count": ghost_count},
                            )
                            _post('post_trade_failed_embed',
                                symbol=symbol, direction='BUY', amount_usd=amount_usd,
                                error=_reason[:150], dry_run=False,
                            )
                            failed_count += 1
                            continue
                        logger.info(
                            "ExecutionWorker: trade #%d DEFER %d/%d — order pending (market closed?)",
                            trade_id, _pend_defers + 1, DEFER_CAP,
                        )
                        trade_repo.update_status(
                            trade_id, "APPROVED",
                            requeue_count=_pend_defers + 1,
                        )
                        _post('post_alert_embed',
                            title=f"⏳ DEFER {_pend_defers + 1}/{DEFER_CAP} — {symbol}",
                            description=(
                                f"Order {api_position_id} pending bei eToro "
                                f"(Markt zu?) — naechster Versuch in 15min."
                            ),
                            severity="INFO", channel="trades",
                        )
                        processed_count -= 1
                        continue  # retry im nächsten Zyklus (15min)
    
                    if pf["status"] == "executed":
                        if pf["positions"] and len(pf["positions"]) > 0:
                            # Position confirmed by API — skip portfolio polling
                            pos = pf["positions"][0]
                            _order_ref = api_position_id  # orderId sichern, BEVOR die Variable zur positionID wird
                            api_position_id = str(pos.get("positionID") or pos.get("positionId") or api_position_id)
                            entry_price = 0.0  # nur Anzeige — DB bekommt None, Reconciler backfillt openRate
                            trade_repo.update_status(
                                trade_id, "ACTIVE",
                                api_position_id=api_position_id,
                                order_id=_order_ref,
                                entry_price=None,
                                confirmed_at=_utcnow(),
                            )
                            trade_repo.reset_ghost_failures(instrument_id)
                            filled_count += 1
                            logger.info(
                                "ExecutionWorker: trade #%d EXECUTED (post-flight confirmed) — %s $%.2f @ %.6f (positionID=%s)",
                                trade_id, symbol, amount_usd, entry_price, api_position_id,
                            )
                            log_repo.write(
                                "INFO", "execution_worker",
                                f"Trade #{trade_id} ACTIVE: {symbol} BUY ${amount_usd:.2f} (post-flight confirmed, positionID={api_position_id})",
                                {"trade_id": trade_id, "symbol": symbol, "instrument_id": instrument_id, "amount_usd": amount_usd, "entry_price": entry_price, "api_position_id": api_position_id, "stop_loss_pct": stop_loss_pct},
                            )
                            _post('post_trade_filled_embed',
                                symbol=symbol, direction='BUY', amount_usd=amount_usd,
                                position_id=api_position_id, entry_price=entry_price,
                                sl_pct=stop_loss_pct, dry_run=False,
                            )
                            # Strategy-tagging (scalp vs swing)
                            try:
                                _signal_id = trade.get("signal_id")
                                _sig_type = ""
                                if _signal_id:
                                    _sig_row = db.fetchone(
                                        "SELECT signal_type FROM signals WHERE id = ?", (_signal_id,)
                                    )
                                    if _sig_row:
                                        _sig_type = str(_sig_row["signal_type"] or "")
                                _SCALP_SIGNAL_TYPES = frozenset({
                                    "BB_LOWER_RSI_OVERSOLD", "BB_EXTREME_RSI_OVERSOLD",
                                    "RSI_EXTREME_OVERSOLD", "BB_LOW_MACD_IMPROVING",
                                })
                                _strategy = (
                                    "scalp" if any(s in _sig_type for s in _SCALP_SIGNAL_TYPES)
                                    else "swing"
                                )
                                from bot.core.trailing_stop import set_strategy as _set_strategy
                                _set_strategy(db, api_position_id, symbol, _strategy)
                            except Exception as _strat_exc:
                                logger.debug(
                                    "ExecutionWorker: strategy-tagging fehlgeschlagen für %s — %s",
                                    symbol, _strat_exc,
                                )
                            continue
                        else:
                            # Ghost detected! API says executed but no position
                            # KRITISCH FIX: Nicht sofort als Ghost klassifizieren —
                            # eToro kann mehrere Sekunden brauchen um Position zu erstellen.
                            # Retry 2x mit 3s间隔 bevor als Ghost gebucht.
                            ghost_confirmed = False
                            for _ghost_retry in range(2):
                                _time.sleep(3)
                                _pf_retry = client.get_order_status(
                                    int(api_position_id), env="real"
                                )
                                if _pf_retry.get("positions") and len(_pf_retry["positions"]) > 0:
                                    # Position nach Retry erschienen
                                    pos = _pf_retry["positions"][0]
                                    _order_ref = api_position_id
                                    api_position_id = str(pos.get("positionID") or pos.get("positionId") or api_position_id)
                                    entry_price = 0.0  # nur Anzeige — DB bekommt None (Reconciler-Backfill)
                                    trade_repo.update_status(
                                        trade_id, "ACTIVE",
                                        api_position_id=api_position_id,
                                        order_id=_order_ref,
                                        entry_price=None,
                                        confirmed_at=_utcnow(),
                                    )
                                    trade_repo.reset_ghost_failures(instrument_id)
                                    _apply_strategy_tag(db, trade, api_position_id, symbol)
                                    filled_count += 1
                                    logger.info(
                                        "ExecutionWorker: trade #%d EXECUTED (post-flight nach %ds Retry) — %s $%.2f @ %.6f (positionID=%s)",
                                        trade_id, (_ghost_retry + 1) * 3, symbol, amount_usd, entry_price, api_position_id,
                                    )
                                    log_repo.write(
                                        "INFO", "execution_worker",
                                        f"Trade #{trade_id} ACTIVE: {symbol} BUY ${amount_usd:.2f} (post-flight confirmed after {(_ghost_retry + 1) * 3}s retry, positionID={api_position_id})",
                                        {"trade_id": trade_id, "symbol": symbol, "instrument_id": instrument_id, "amount_usd": amount_usd, "entry_price": entry_price, "api_position_id": api_position_id, "stop_loss_pct": stop_loss_pct},
                                    )
                                    _post('post_trade_filled_embed',
                                        symbol=symbol, direction='BUY', amount_usd=amount_usd,
                                        position_id=api_position_id, entry_price=entry_price,
                                        sl_pct=stop_loss_pct, dry_run=False,
                                    )
                                    ghost_confirmed = True
                                    break
                    
                            if ghost_confirmed:
                                continue
                    
                            # Nach 2 Retries immer noch keine Position → echter Ghost
                            ghost_count, bl_status = trade_repo.record_ghost_failure(instrument_id)
                            ghost_detail = f"orderId={api_position_id} (executed, no position after 2x3s retry)"
                            logger.warning(
                                "ExecutionWorker: trade #%d (%s) GHOST ORDER (%s) — "
                                "failure #%d for this instrument (blacklist: %s)",
                                trade_id, symbol, ghost_detail, ghost_count, bl_status,
                            )
                            trade_repo.update_status(
                                trade_id, "FAILED",
                                rejection_reason=(
                                    f"Ghost order: {ghost_detail} — "
                                    f"eToro API returned EXECUTED but no position "
                                    f"(failure #{ghost_count}, blacklist: {bl_status})"
                                ),
                            )
                            log_repo.write(
                                "WARN", "execution_worker",
                                f"Trade #{trade_id} GHOST ORDER: {symbol} {ghost_detail} (failure #{ghost_count}, bl={bl_status})",
                                {"symbol": symbol, "api_position_id": api_position_id, "ghost_count": ghost_count, "blacklist_status": bl_status, "post_flight_raw": pf["raw"]},
                            )
                            if ghost_count >= 5:
                                _post(
                                    'post_watchdog_alert_embed',
                                    alert_type='GHOST_ORDER_ESCALATION',
                                    symbol=symbol,
                                    message=(
                                        f"⚠️ {symbol}: {ghost_count}+ konsekutive Ghost-Failures — "
                                        f"vermutlich strukturelles Problem, manuelle Prüfung nötig"
                                    ),
                                    severity='critical' if ghost_count >= 9 else 'high',
                                    details=(
                                        f"Blacklist: {bl_status} | "
                                        f"Last orderId: {api_position_id or 'N/A'} | "
                                        f"Instrument ID: {instrument_id}"
                                    ),
                                )
                            _post('post_trade_failed_embed',
                                symbol=symbol, direction='BUY', amount_usd=amount_usd,
                                error=f"Ghost order: {ghost_detail} (eToro API EXECUTED but no position) (#{ghost_count}, blacklist: {bl_status})",
                                dry_run=False,
                            )
                            failed_count += 1
                            continue
    
                # d5. FALLBACK: Portfolio polling (only if post-flight check unavailable)
                # Use exponential backoff polling instead of fixed 5s sleep.
                # Futures and some crypto instruments need more time to process.
                import time as _time
    
                max_attempts = 6          # Check up to 6 times
                initial_wait_s = 5        # Start at 5 seconds
                max_total_wait_s = 90     # fix/ghost-defer-hardening: 300s sprengte
                                          # das 120s-no_agent-Cron-Budget (Job-Kill
                                          # mitten im Poll → Trade haengt in
                                          # SUBMITTING). Langsame Fills faengt der
                                          # DEFER-Retry im naechsten 15-min-Zyklus.
    
                matching_pos = None
                total_waited = 0
    
                for attempt in range(max_attempts):
                    wait_time = min(initial_wait_s * (2 ** attempt), 30)  # Cap individual wait at 30s
                    if total_waited + wait_time > max_total_wait_s:
                        logger.info(
                            "ExecutionWorker: trade #%d reached max total wait (%ds), final check",
                            trade_id, max_total_wait_s,
                        )
                        break
    
                    _time.sleep(wait_time)
                    total_waited += wait_time
    
                    portfolio = client.get_portfolio()
                    positions = portfolio.get("clientPortfolio", {}).get("positions", [])
    
                    # Check if a NEW position appeared for this instrument
                    # (positionID not in the pre-submit snapshot — an existing
                    # pyramiding fragment must not count as fill confirmation)
                    matching_pos = _find_new_position(
                        positions, instrument_id, pre_existing_pos_ids
                    )
    
                    if matching_pos:
                        logger.info(
                            "ExecutionWorker: trade #%d position verified after %.1fs (%d attempts)",
                            trade_id, total_waited, attempt + 1,
                        )
                        break
    
                    logger.debug(
                        "ExecutionWorker: trade #%d position not yet visible (attempt %d/%d, waited %.1fs)",
                        trade_id, attempt + 1, max_attempts, total_waited,
                    )
    
                if not matching_pos:
                    # No position found after polling — check if we got a valid orderId
                    # from eToro. A valid orderId means eToro accepted the order but
                    # the position just hasn't materialized yet (Pre-Market, Spät-Execution, etc.).
                    # In that case: DEFER (revert to APPROVED for next 15min retry).
                    # No orderId = silent block → FAILED.
                    # fix/ghost-defer-hardening (Review 2026-07-14): DEFER ohne
                    # Cap waere ein Endlos-Retry — orderId-Ghosts sind der
                    # KLASSISCHE Ghost-Pattern (eToro akzeptiert, bucht nie).
                    # Ohne Cap: nie FAILED, nie ghost-gezaehlt, Blacklist
                    # ausgehungert, Retry-Spam alle 15 min. requeue_count dient
                    # als Defer-Zaehler (max 3); danach greift der normale
                    # FAILED+Ghost-Pfad. classify_requeue (One-Shot fuer
                    # transiente FAILED) prueft !=0 und requeued einen
                    # ausdeferten Trade folgerichtig nicht erneut.
                    _defer_count = int(trade.get("requeue_count") or 0)
                    if api_position_id and _defer_count < 3:
                        logger.info(
                            "ExecutionWorker: trade #%d DEFER %d/3 — orderId=%s empfangen, "
                            "Position noch nicht materialisiert (Pre-Market/Spät-Execution?)",
                            trade_id, _defer_count + 1, api_position_id,
                        )
                        trade_repo.update_status(
                            trade_id, "APPROVED", requeue_count=_defer_count + 1,
                        )
                        _post('post_alert_embed',
                            title=f"⏳ DEFER {_defer_count + 1}/3 — {symbol}",
                            description=(
                                f"Order {api_position_id} angenommen, Position noch "
                                f"nicht sichtbar — naechster Versuch in 15min."
                            ),
                            severity="INFO", channel="trades",
                        )
                        processed_count -= 1
                        continue  # retry im nächsten Zyklus (15min)
    
                    # Kein orderId (silent block) ODER Defer-Cap erreicht → Ghost
                    ghost_count, bl_status = trade_repo.record_ghost_failure(instrument_id)
    
                    ghost_detail = f"orderId={api_position_id}" if api_position_id else "no orderId returned (silent block)"
                    logger.warning(
                        "ExecutionWorker: trade #%d (%s) GHOST ORDER (%s) — "
                        "failure #%d for this instrument (blacklist: %s)",
                        trade_id, symbol, ghost_detail, ghost_count, bl_status,
                    )
                    trade_repo.update_status(
                        trade_id,
                        "FAILED",
                        rejection_reason=(
                            f"Ghost order: {ghost_detail} but position "
                            f"never materialized (failure #{ghost_count}, blacklist: {bl_status})"
                        ),
                    )
                    log_repo.write(
                        "WARN",
                        "execution_worker",
                        f"Trade #{trade_id} GHOST ORDER: {symbol} {ghost_detail} no position created (#{ghost_count}, bl={bl_status})",
                        {"symbol": symbol, "api_position_id": api_position_id or "", "ghost_count": ghost_count, "blacklist_status": bl_status},
                    )
    
                    # c) Eskalierter Alert ab #5 — separates, lautereres Embed
                    if ghost_count >= 5:
                        _post(
                            'post_watchdog_alert_embed',
                            alert_type='GHOST_ORDER_ESCALATION',
                            symbol=symbol,
                            message=(
                                f"⚠️ {symbol}: {ghost_count}+ konsekutive Ghost-Failures — "
                                f"vermutlich strukturelles Problem, manuelle Prüfung nötig"
                            ),
                            severity='critical' if ghost_count >= 9 else 'high',
                            details=(
                                f"Blacklist: {bl_status} | "
                                f"Last orderId: {api_position_id or 'N/A'} | "
                                f"Instrument ID: {instrument_id}"
                            ),
                        )
    
                    _post('post_trade_failed_embed',
                        symbol=symbol,
                        direction='BUY',
                        amount_usd=amount_usd,
                        error=f"Ghost order: {ghost_detail} but no position created (#{ghost_count}, blacklist: {bl_status})",
                        dry_run=False,
                    )
                    failed_count += 1
                    continue
    
                # Position verified — update trade with real data
                entry_price = 0.0  # default; will be overwritten if matching_pos exists
                if matching_pos:
                    entry_price = float(matching_pos.get("openRate", 0) or 0.0)
                    api_position_id = str(matching_pos.get("positionID", matching_pos.get("positionId", api_position_id)))
                    # Reset ghost failure counter on success
                    trade_repo.reset_ghost_failures(instrument_id)
    
                trade_repo.update_status(
                    trade_id,
                    "ACTIVE",
                    api_position_id=api_position_id,
                    order_id=api_position_id,
                    entry_price=entry_price if entry_price else None,
                    confirmed_at=_utcnow(),
                )
                filled_count += 1

                # ── Strategy-Tagging: scalp vs. swing basierend auf Signal-Typ ────────
                # Scalp (Mean-Reversion): frühe ATR×2-Profit-Stufe (min 2%)
                # Swing (Trend-Following): Standard ATR×6/10/18 Profit-Leiter
                _SCALP_SIGNAL_TYPES = frozenset({
                    "BB_LOWER_RSI_OVERSOLD", "BB_EXTREME_RSI_OVERSOLD",
                    "RSI_EXTREME_OVERSOLD", "BB_LOW_MACD_IMPROVING",
                })
                try:
                    _signal_id = trade.get("signal_id")
                    _sig_type = ""
                    if _signal_id:
                        _sig_row = db.fetchone(
                            "SELECT signal_type FROM signals WHERE id = ?", (_signal_id,)
                        )
                        if _sig_row:
                            _sig_type = str(_sig_row["signal_type"] or "")
                    _strategy = (
                        "scalp"
                        if any(s in _sig_type for s in _SCALP_SIGNAL_TYPES)
                        else "swing"
                    )
                    from bot.core.trailing_stop import set_strategy as _set_strategy
                    _set_strategy(db, api_position_id, symbol, _strategy)
                    logger.info(
                        "ExecutionWorker: %s strategy=%s (signal_type=%s)",
                        symbol, _strategy, _sig_type[:60] or "unbekannt",
                    )
                except Exception as _strat_exc:
                    logger.debug(
                        "ExecutionWorker: strategy-tagging fehlgeschlagen für %s — %s",
                        symbol, _strat_exc,
                    )

    
                fill_info = {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "instrument_id": instrument_id,
                    "amount_usd": amount_usd,
                    "entry_price": entry_price,
                    "api_position_id": api_position_id,
                    "stop_loss_pct": stop_loss_pct,
                }
    
                # Discord embed for filled trade
                _post('post_trade_filled_embed',
                    symbol=symbol,
                    direction='BUY',
                    amount_usd=amount_usd,
                    position_id=api_position_id,
                    entry_price=entry_price,
                    sl_pct=stop_loss_pct,
                    dry_run=False
                )
    
                log_repo.write(
                    "INFO",
                    "execution_worker",
                    f"Trade #{trade_id} ACTIVE: {symbol} BUY ${amount_usd:.2f} @ {entry_price}",
                    fill_info,
                )
                logger.info(
                    "ExecutionWorker: trade #%d FILLED — %s $%.2f @ %.6f",
                    trade_id, symbol, amount_usd, entry_price,
                )
    
            except APIError as exc:
                err_str = str(exc)
                logger.error(
                    "ExecutionWorker: API error on trade #%d (%s) — %s",
                    trade_id, symbol, exc,
                )
    
                # Classify error: DRAWDOWN/regime signal → REJECTED, otherwise FAILED
                if "DRAWDOWN" in err_str.upper() or "regime" in err_str.lower():
                    trade_repo.update_status(
                        trade_id,
                        "REJECTED",
                        rejection_reason=f"APIError (regime): {err_str[:200]}",
                    )
                    log_repo.write(
                        "WARN",
                        "execution_worker",
                        f"Trade #{trade_id} REJECTED via API regime error: {err_str[:200]}",
                        {"symbol": symbol, "status_code": exc.status_code},
                    )
                else:
                    trade_repo.update_status(
                        trade_id,
                        "FAILED",
                        rejection_reason=f"APIError: {err_str[:200]}",
                    )
                    log_repo.write(
                        "ERROR",
                        "execution_worker",
                        f"Trade #{trade_id} FAILED: {err_str[:200]}",
                        {"symbol": symbol, "status_code": exc.status_code},
                    )
                    # Discord embed for failed trade
                    _post('post_trade_failed_embed',
                        symbol=symbol,
                        direction='BUY',
                        amount_usd=amount_usd,
                        error=err_str[:200],
                        dry_run=False
                    )
                failed_count += 1
    
            except Exception as exc:
                logger.error(
                    "ExecutionWorker: Unexpected error on trade #%d (%s) — %s",
                    trade_id, symbol, exc,
                )
                trade_repo.update_status(
                    trade_id,
                    "FAILED",
                    rejection_reason=f"Unexpected error: {str(exc)[:200]}",
                )
                log_repo.write(
                    "ERROR",
                    "execution_worker",
                    f"Trade #{trade_id} FAILED (unexpected): {str(exc)[:200]}",
                    {"symbol": symbol},
                )
                # Discord embed for unexpected failure
                _post('post_trade_failed_embed',
                    symbol=symbol,
                    direction='BUY',
                    amount_usd=amount_usd,
                    error=str(exc)[:200],
                    dry_run=False
                )
                failed_count += 1
    
        # ── 4. Summary ────────────────────────────────────────────────────────────
        if filled_count > 0 or failed_count > 0:
            print(
                f"ExecutionWorker: {processed_count} trades processed, "
                f"{filled_count} filled, {failed_count} failed"
            )
        else:
            logger.debug("ExecutionWorker: %d processed, 0 filled, 0 failed", processed_count)
        try:
            from bot.core.heartbeat import record_duration as _rd
            _rd(state_repo, "execution_worker", _time_dur.monotonic() - _t_run_start)
        except Exception:
            pass
        log_repo.write(
            "INFO",
            "execution_worker",
            f"Run complete: processed={processed_count} filled={filled_count} failed={failed_count}",
        )
    
        client.close()
    
    
if __name__ == "__main__":
    main()
