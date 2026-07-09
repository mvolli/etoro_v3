#!/usr/bin/env python3
"""eToro Trading Bot V3 — Signal Worker
src/bot/workers/signal_worker.py

Runs every 15 minutes at :03.
Reads fresh signals, applies all risk gates, and creates APPROVED trades.

Schedule: 3,18,33,48 * * * * cd /path/to/etoro_v3 && python3 -m bot.workers.signal_worker
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("signal_worker")
import json as _json_mod
from datetime import datetime as _dt, timezone as _tz

_LLM_GHOST_BLACKLIST_PATH = PROJECT_ROOT / "data" / "llm_ghost_blacklist.json"


def _load_llm_ghost_blacklist() -> dict:
    try:
        if not _LLM_GHOST_BLACKLIST_PATH.exists():
            return {}
        data = _json_mod.loads(_LLM_GHOST_BLACKLIST_PATH.read_text())
        expires = data.get("auto_expires_at")
        if expires:
            from datetime import datetime, timezone
            if datetime.fromisoformat(expires) < datetime.now(timezone.utc):
                return {}
        return data
    except Exception:
        return {}


_LLM_SIGNAL_WEIGHTS_PATH = PROJECT_ROOT / "data" / "llm_signal_weights.json"


def _load_llm_signal_weights() -> dict:
    """Laedt LLM-Signal-Gewichtungen (autonom von llm_review_worker gesetzt)."""
    try:
        if not _LLM_SIGNAL_WEIGHTS_PATH.exists():
            return {}
        data = _json_mod.loads(_LLM_SIGNAL_WEIGHTS_PATH.read_text())
        expires = data.get("auto_expires_at")
        if expires:
            if _dt.fromisoformat(expires) < _dt.now(_tz.utc):
                return {}
        return data
    except Exception:
        return {}


def _get_signal_score_multiplier(signal_type: str, weights: dict) -> float:
    """Gibt Score-Multiplikator fuer Signal-Typ zurueck (1.0 = unveraendert)."""
    if not weights:
        return 1.0
    adj = weights.get("adjustments", {}).get(signal_type)
    if adj is None:
        return 1.0
    return float(adj.get("score_multiplier", 1.0))


def _is_signal_type_skipped(signal_type: str, weights: dict) -> tuple[bool, str]:
    """Prueft ob Signal-Typ durch LLM gesperrt wurde. Gibt (skip, reason) zurueck."""
    if not weights:
        return False, ""
    adj = weights.get("adjustments", {}).get(signal_type)
    if adj and adj.get("skip"):
        return True, adj.get("reason", "LLM: Signal-Typ deaktiviert")
    return False, ""


def _is_llm_ghost_blocked(symbol: str, blacklist: dict) -> bool:
    """Prueft ob Symbol durch LLM-Blacklist geblockt (Exchange-Suffix oder direkt)."""
    if not blacklist:
        return False
    if symbol in blacklist.get("symbols", []):
        return True
    exchanges = blacklist.get("exchanges", [])
    # Dot-Suffix (.L, .DE, .HE etc.)
    if "." in symbol:
        dot_suffix = "." + symbol.rsplit(".", 1)[-1]
        if dot_suffix in exchanges:
            return True
    # Pseudo-Suffixe fuer Nicht-Dot-Symbole
    if "-" in symbol:
        # Crypto: BTC-USD, ETH-USD, DOT-USD
        if "_CRYPTO" in exchanges:
            return True
    elif symbol.endswith(".FUT"):
        # Futures: LiveCattle.FUT
        if "_FUT" in exchanges:
            return True
    elif "." not in symbol and len(symbol) <= 7 and symbol.isupper():
        # Forex: EURJPY, EURGBP, USDCHF
        fx_ends = ("JPY", "GBP", "USD", "CHF", "EUR", "AUD", "CAD")
        if any(symbol.endswith(e) for e in fx_ends) or "/" in symbol:
            if "_FOREX" in exchanges:
                return True
    return False



def _signal_age_factor(generated_at_iso: str, ttl_minutes: int = 60) -> float:
    """Prio 5b: Altersstrafe 0% bei <=30min, -20% am TTL-Ende (linear).
    Aeltere Signale spiegeln veraltete Marktdaten — werden nachrangig sortiert."""
    try:
        from datetime import datetime, timezone
        gen_at = datetime.fromisoformat(generated_at_iso)
        if gen_at.tzinfo is None:
            gen_at = gen_at.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - gen_at).total_seconds() / 60
        if age_min <= 30:
            return 1.0
        penalty = min(0.2, (age_min - 30) / max(1, ttl_minutes - 30) * 0.2)
        return 1.0 - penalty
    except Exception:
        return 1.0


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

def _post(fn_name: str, **kwargs) -> None:
    """Best-effort Discord post. Never raises."""
    try:
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


def main() -> None:
    # ── Worker lock: prevent overlapping cron invocations ────────────────────
    from bot.core.worker_lock import worker_lock

    with worker_lock("signal_worker") as acquired:
        if not acquired:
            logger.warning("SignalWorker: previous run still active — skipping this cycle")
            print("SignalWorker: SKIPPED (already running)")
            return

        # ── 1. Setup ──────────────────────────────────────────────────────────────
        _load_env()
        cfg = _load_config()
    
        from bot.core.market_hours import is_market_open
        from bot.core.regime import get_regime_params
        from bot.core.risk import apply_config, check_buy_gate, get_score_boost
        apply_config(cfg)  # fix/risk-config-wiring: Limits/Schwellen aus config.yaml
        from bot.db.connection import DB
        from bot.db.repo import LogRepo, PortfolioRepo, SignalRepo, StateRepo, TradeRepo
    
        db_path = PROJECT_ROOT / cfg["db"]["path"]
        busy_timeout = cfg["db"].get("busy_timeout_ms", 5000)
        db = DB(db_path=db_path, busy_timeout_ms=busy_timeout)
    
        trade_repo = TradeRepo(db)
        signal_repo = SignalRepo(db)
        portfolio_repo = PortfolioRepo(db)
        state_repo = StateRepo(db)
        log_repo = LogRepo(db)

        # ── LLM Blacklist & Signal Weights (fix/llm-blacklist-wiring:
        #    beide Load-Funktionen existierten, wurden aber nie in main()
        #    aufgerufen — LLM-Blocklist und Signal-Weights waren toter Code) ──
        _llm_blacklist = _load_llm_ghost_blacklist()
        _llm_signal_weights = _load_llm_signal_weights()
        if _llm_blacklist:
            logger.info(
                "SignalWorker: LLM-Blackload geladen — %d Exchanges, %d Symbole, %d Stats",
                len(_llm_blacklist.get("exchanges", [])),
                len(_llm_blacklist.get("symbols", [])),
                len(_llm_blacklist.get("stats", {})),
            )
        if _llm_signal_weights:
            logger.info("SignalWorker: LLM-Signal-Weights geladen")

        # ── Signal-Type Cooldown pro Instrument (fix/signal-type-cooldown:
        #    BB_EXTREME_RSI_OVERSOLD feuerte 146x mit 70% Fail-Rate —
        #    gleiche Signale auf gleichem Instrument brauchen Mindestdauer
        #    zwischen Wiederholung) ──
        SIGNAL_TYPE_COOLDOWN_MINUTES = int(
            cfg.get("trading", {}).get("signal_type_cooldown_minutes", 60)
        )
        logger.info(
            "SignalWorker: Signal-Type-Cooldown = %d min", SIGNAL_TYPE_COOLDOWN_MINUTES
        )

        # ── API-Client für Pre-Trade-Preischeck (fix/slippage-precheck) ───────────
        # Best-effort: ohne Client/Keys läuft der Worker wie bisher — das
        # Execution-Gate bleibt die letzte Verteidigungslinie.
        _price_client = None
        try:
            from bot.api.client import ClientConfig, EToroClient
            _api_key = os.environ.get("ETORO_API_KEY", "")
            _user_key = os.environ.get("ETORO_USER_KEY", "")
            if _api_key and _user_key:
                _price_client = EToroClient(
                    api_key=_api_key, user_key=_user_key,
                    config=ClientConfig.from_dict(cfg.get("api", {})),
                )
        except Exception as _pc_exc:
            logger.warning("SignalWorker: Preis-Client nicht verfügbar (%s) — Pre-Check übersprungen", _pc_exc)

        # ── Heartbeat (dead-man's switch) — before kill-switch exit so an
        #    active kill switch does not look like a dead worker ─────────────
        from bot.core.heartbeat import record_heartbeat
        record_heartbeat(state_repo, "signal_worker")

        # ── Kill Switch check (V5) — abort immediately if active ──────────────────
        from bot.core.kill_switch import is_kill_switch_active, KILL_SWITCH_FILE
        if is_kill_switch_active():
            _ks_reason = KILL_SWITCH_FILE.read_text().strip() if KILL_SWITCH_FILE.exists() else 'Manual kill switch'
            print(f'SignalWorker: KILL SWITCH ACTIVE — no signals generated ({_ks_reason})')
            logger.warning('SignalWorker: KILL SWITCH ACTIVE — exiting without generating signals (%s)', _ks_reason)
            sys.exit(0)
    
        # ── 2. Check regime — V5: 4-level system, no hard block except legacy ────
        regime = state_repo.get_regime()
        from bot.core.regime import get_regime_params, get_risk_scalar, get_min_conviction
    
        regime_params = get_regime_params(regime)
        risk_scalar = get_risk_scalar(regime)
        min_conviction_for_regime = get_min_conviction(regime)
    
        # Log regime status
        print(f"SignalWorker: regime={regime} risk_scalar={risk_scalar:.2f} "
              f"min_conviction={min_conviction_for_regime}")
        log_repo.write("INFO", "signal_worker",
                       f"Regime: {regime} | scalar={risk_scalar:.2f} | min={min_conviction_for_regime}")
    
        # ── 3. Fetch fresh BUY signals — filtered by regime min conviction ────────
        all_signals = signal_repo.get_fresh(min_conviction=min_conviction_for_regime)
        # Filter to BUY signals only: exclude SELL signals (signal_type contains 'SELL' or 'OVERBOUGHT')
        buy_signals = [
            s for s in all_signals
            if 'SELL' not in (s.get('signal_type') or '').upper()
               and 'OVERBOUGHT' not in (s.get('signal_type') or '').upper()
        ]

        # Filter non-tradable instruments (is_tradable=0) — single bulk query.
        # is_tradable=NULL means never checked → allow (fail-open).
        if buy_signals:
            _iids = [s["instrument_id"] for s in buy_signals if s.get("instrument_id")]
            if _iids:
                _placeholders = ",".join("?" * len(_iids))
                _blocked = {
                    r["instrument_id"]
                    for r in db.fetchall(
                        f"SELECT instrument_id FROM instruments"
                        f" WHERE instrument_id IN ({_placeholders}) AND is_tradable = 0",
                        _iids,
                    )
                }
                if _blocked:
                    _before = len(buy_signals)
                    buy_signals = [s for s in buy_signals if s.get("instrument_id") not in _blocked]
                    logger.info(
                        "SignalWorker: %d Signal(e) wegen is_tradable=0 herausgefiltert",
                        _before - len(buy_signals),
                    )

        if not buy_signals:
            logger.info("SignalWorker: no fresh BUY signals with %s+ conviction", min_conviction_for_regime)
            print(f"SignalWorker: 0 signals evaluated, 0 trades approved")
            log_repo.write("INFO", "signal_worker",
                           f"No fresh BUY signals with {min_conviction_for_regime}+ conviction")
            _post('post_alert_embed',
                title=f'⚪ Signal Worker: No signals ({regime})',
                description=(
                    f'Regime: **{regime}** | min_conviction={min_conviction_for_regime} | scalar={risk_scalar:.2f}\n'
                    f'No fresh BUY signals found above conviction threshold.'
                ),
                severity='INFO',
                dry_run=False
            )
            return
    
        # ── 4. Current portfolio state ────────────────────────────────────────────
        # fix/autonomy-hardening: FAIL-CLOSED on missing equity. The previous
        # $10,000 default meant position sizing ran on a fabricated number
        # whenever CURRENT_EQUITY was empty or corrupt. No equity → no trades.
        equity = state_repo.get_equity()
        if equity <= 0.0:
            msg = ("CURRENT_EQUITY fehlt oder ist 0 — keine Trades möglich "
                   "(fail-closed). Reconciler prüfen.")
            logger.error("SignalWorker: %s", msg)
            log_repo.write("ERROR", "signal_worker", msg)
            print("SignalWorker: ABORT — equity unbekannt (fail-closed)")
            _post('post_alert_embed',
                title='🔴 Signal Worker: Equity unbekannt',
                description=msg,
                severity='CRITICAL',
                dry_run=False,
            )
            return
    
        total_exposure = portfolio_repo.get_total_exposure()
        position_count = portfolio_repo.get_position_count()

        # fix/autonomy-hardening: prefer REAL available cash (stored by the
        # reconciler from clientPortfolio.credit) over the equity−exposure
        # estimate, which ignores pending orders, fees and rounding.
        available_cash = state_repo.get_float("AVAILABLE_CASH", -1.0)
        if available_cash >= 0.0:
            cash_estimate = available_cash
        else:
            cash_estimate = max(0.0, equity - total_exposure)
            logger.info("SignalWorker: AVAILABLE_CASH nicht gesetzt — nutze Schätzung equity−exposure")

        # ── fix/autonomy-hardening: daily trade-count brake ───────────────────────
        # Hard ceiling on new trades per UTC day. Protects against signal
        # storms (e.g. a market-wide selloff generating 'oversold' BUYs on
        # every watchlist symbol at once).
        max_trades_per_day = int(cfg.get("trading", {}).get("max_trades_per_day", 12))
        if max_trades_per_day > 0:
            row = db.fetchone(
                "SELECT COUNT(*) AS n FROM trades "
                "WHERE created_at >= date('now') "
                "AND status NOT IN ('REJECTED','FAILED')",
            )
            trades_today = int(
                (row["n"] if isinstance(row, dict) else row[0]) if row else 0
            )
            if trades_today >= max_trades_per_day:
                msg = (f"Tageslimit erreicht: {trades_today}/{max_trades_per_day} "
                       f"Trades heute — keine weiteren Approvals bis Mitternacht UTC")
                logger.warning("SignalWorker: %s", msg)
                log_repo.write("WARN", "signal_worker", msg)
                print(f"SignalWorker: 0 signals evaluated, 0 trades approved ({msg})")
                _post('post_alert_embed',
                    title='🟡 Signal Worker: Tageslimit erreicht',
                    description=msg,
                    severity='WARNING',
                    dry_run=False,
                )
                return
    
        logger.info(
            "SignalWorker: equity=%.2f exposure=%.2f cash=%.2f positions=%d regime=%s scalar=%.2f",
            equity, total_exposure, cash_estimate, position_count, regime, risk_scalar,
        )
    
        # ── Sizing config — V5: apply risk_scalar (replaces buy_aggressiveness) ───
        sizing = cfg.get("sizing", {})
        conviction_pct: dict[str, float] = {
            "VERY_HIGH": sizing.get("very_high_pct", 8.0),
            "HIGH":      sizing.get("high_pct",      7.0),
            "MEDIUM":    sizing.get("medium_pct",     6.0),
            "LOW":       sizing.get("low_pct",        2.0),
        }
        # V5: risk_scalar replaces buy_aggressiveness (never >1.0 — no revenge trading)
        buy_aggressiveness: float = min(risk_scalar, 1.0)
    
        # ── 5. Rank & filter candidates BEFORE slicing to top-3 ────────────────────
        # V5 fix: market-open and blacklist checks used to run *inside* the loop
        # over the already-sliced top-3-by-score signals. That meant a closed
        # market (e.g. crypto, which is always "fresh") or a blacklisted
        # instrument could occupy one of only 3 scarce slots per 15-min cycle,
        # starving open/tradable equity markets of any chance to be evaluated —
        # even though their signals sat unused in the FRESH pool until the 6h TTL
        # expired. Filtering BEFORE ranking+slicing fixes this.
    
        def _resolve_symbol(instrument_id: int) -> str:
            """Look up ticker symbol for an instrument_id (signals table has none)."""
            try:
                inst_row = db.fetchone(
                    "SELECT symbol FROM instruments WHERE instrument_id=?",
                    (instrument_id,),
                )
                if inst_row:
                    return inst_row["symbol"] if isinstance(inst_row, dict) else inst_row[0]
            except Exception:
                pass
            snap = portfolio_repo.get_by_instrument(instrument_id)
            if snap:
                sym = snap[0].get("symbol", "")
                if sym:
                    return sym
            return str(instrument_id)
    
        skipped_closed: list[str] = []
        eligible: list[tuple[dict, str]] = []  # (signal, symbol) — open market, not blacklisted
    
        for signal in buy_signals:
            instrument_id = signal["instrument_id"]
            signal_id = signal.get("id")
    
            # Ghost blacklist check — skip blacklisted instruments
            if trade_repo.is_instrument_blacklisted(instrument_id):
                ghost_count = trade_repo.get_ghost_failure_count(instrument_id)
                logger.info(
                    "SignalWorker: %s BLACKLISTED (%d consecutive ghost failures) — skipping",
                    instrument_id, ghost_count,
                )
                signal_repo.update_signal_status(signal_id, "REJECTED")
                continue
    
            symbol = _resolve_symbol(instrument_id)

            if _is_llm_ghost_blocked(symbol, _llm_blacklist):
                logger.info("SignalWorker: %s LLM-Exchange-Blacklist", symbol)
                signal_repo.update_signal_status(signal_id, "REJECTED")
                continue

            # LLM Signal-Type Blacklist (deaktivierte Signal-Typen)
            _sig_type = signal.get("signal_type", "")
            _sig_skip, _sig_reason = _is_signal_type_skipped(_sig_type, _llm_signal_weights)
            if _sig_skip:
                logger.info("SignalWorker: %s Signal-Typ gesperrt (%s): %s",
                            symbol, _sig_type[:40], _sig_reason[:60])
                signal_repo.update_signal_status(signal_id, "REJECTED")
                continue

            # Signal-Type Cooldown (fix/signal-type-cooldown: gleiche
            # signal_type auf gleichem Instrument braucht Mindestdauer)
            _sig_type = signal.get("signal_type", "")
            if SIGNAL_TYPE_COOLDOWN_MINUTES > 0:
                if signal_repo.has_recent_signal(
                    instrument_id, _sig_type, SIGNAL_TYPE_COOLDOWN_MINUTES
                ):
                    logger.info(
                        "SignalWorker: %s signal_type '%s' im Cooldown (%d min) — REJECTED",
                        symbol, _sig_type[:60], SIGNAL_TYPE_COOLDOWN_MINUTES,
                    )
                    signal_repo.update_signal_status(signal_id, "REJECTED")
                    continue

            # Slippage-Blacklist: Instrumente mit >=3 Slippage-Rejects in 7d
            # werden hier herausgefiltert (NICHT erst im Kandidaten-Loop),
            # damit sie keine der 3 wertvollen Kandidaten-Slots blockieren.
            if trade_repo.is_slippage_blacklisted(instrument_id):
                logger.info(
                    "SignalWorker: %s Slippage-Blacklist (eligible-Filter) — Signal REJECTED",
                    symbol,
                )
                signal_repo.update_signal_status(signal_id, "REJECTED")
                continue

            # Market hours: statischer Check entfernt — allowEntryOrders
            # in open_position() prüft live ob eToro Trades erlaubt.
            eligible.append((signal, symbol))
    
        # Sort by boosted score descending — only among OPEN, non-blacklisted
        # signals. The boost (get_score_boost) prioritizes stocks/ETFs over
        # crypto/commodities/indices when raw scores are close, without
        # changing the underlying exposure caps (ASSET_CLASS_LIMITS in
        # risk.py still applies at the gate stage further down).
        eligible.sort(
            key=lambda t: (
                float(t[0].get("score", 0))
                * get_score_boost(t[1])
                * _get_signal_score_multiplier(t[0].get("signal_type", ""), _llm_signal_weights)
                * _signal_age_factor(t[0].get("generated_at", ""), ttl_minutes=60)
            ),
            reverse=True,
        )
    
        # Deduplicate: keep only the highest-score signal per instrument_id
        seen_instruments = set()
        unique_candidates: list[tuple[dict, str]] = []
        for signal, symbol in eligible:
            inst_id = signal["instrument_id"]
            if inst_id not in seen_instruments:
                seen_instruments.add(inst_id)
                unique_candidates.append((signal, symbol))
    
        # Take max 3 unique candidates — guaranteed open-market, non-blacklisted
        candidates = unique_candidates[:3]
    
        evaluated_count = 0
        approved_count = 0
        blocked_reasons: list[str] = []
    
        # Fetch open positions once for asset-class gate (list of {symbol, amount_usd})
        open_positions_raw = portfolio_repo.get_all()
        open_positions = [
            {"symbol": p.get("symbol", ""), "amount_usd": float(p.get("amount_usd") or 0.0)}
            for p in open_positions_raw
        ]
    
        for signal, symbol in candidates:
            instrument_id = signal["instrument_id"]
            conviction = signal.get("conviction", "MEDIUM")
            score = float(signal.get("score", 0))
            signal_id = signal.get("id")
    
            # a. Current amount + fragment count for pyramiding check
            snap_rows = portfolio_repo.get_by_instrument(instrument_id)
            current_symbol_amount = sum(
                float(r.get("amount_usd") or 0.0) for r in snap_rows
            )
            existing_fragments = len(snap_rows)
    
            # b. Buy amount based on conviction × risk_scalar (V5)
            pct = conviction_pct.get(conviction.upper(), conviction_pct["MEDIUM"])
            buy_amount = round((pct / 100.0) * equity * buy_aggressiveness, 2)
    
            # Enforce minimum from regime params
            min_buy = regime_params.get("min_buy_usd", 50.0)
            if buy_amount < min_buy:
                logger.info(
                    "SignalWorker: %s buy_amount $%.2f < regime min $%.2f — skipped",
                    symbol, buy_amount, min_buy,
                )
                continue
    
            # c. Run master buy gate V5
            # fix/sl-gate-wiring: entry_price/sl_price wurden als 0 übergeben —
            # das SL-Quality-Gate (Bible Rule 1) prüfte damit NIE etwas.
            # Jetzt: Signalpreis als Entry, SL daraus mit derselben Formel
            # berechnet, die später open_position() verwendet.
            from bot.core.risk import calculate_sl_price
            gate_entry_price = float(signal.get("price") or 0.0)
            gate_sl_price = (
                calculate_sl_price(
                    gate_entry_price, symbol,
                    float(cfg.get("sl", {}).get("default_pct", 3.0)),
                )
                if gate_entry_price > 0 else 0.0
            )

            gate = check_buy_gate(
                symbol=symbol,
                buy_amount=buy_amount,
                equity=equity,
                cash=cash_estimate,
                regime=regime,
                open_count=position_count,
                current_symbol_amount=current_symbol_amount,
                total_exposed=total_exposure,
                has_stop_loss=True,
                open_positions=open_positions,
                conviction=conviction,                   # V5: conviction gate
                existing_fragments=existing_fragments,   # V5: pyramiding gate
                entry_price=gate_entry_price,            # fix/sl-gate-wiring
                sl_price=gate_sl_price,
                max_fragments=int(cfg.get("trading", {}).get(
                    "max_fragments_per_instrument", 3)),  # Bible: Fragment-Limit
            )
    
            if gate.allowed:
                evaluated_count += 1

                # c2. Correlation Reduce-Tier (Bible V5): 0.60 <= r < 0.80 →
                # Größe halbieren. Das Block-Gate (≥0.80) lief bereits in
                # check_buy_gate; die Paare sind gecacht — dieser Aufruf
                # kostet nur SQLite-Lookups. Fail-open bei Fehlern.
                try:
                    from bot.core.correlation import get_size_factor
                    corr_factor, corr_reason = get_size_factor(symbol, open_positions)
                except Exception as _corr_exc:
                    corr_factor, corr_reason = 1.0, f'Korrelation-Sizing übersprungen: {_corr_exc}'
                if corr_factor < 1.0:
                    reduced = round(buy_amount * corr_factor, 2)
                    logger.info(
                        "SignalWorker: %s Größe reduziert $%.2f → $%.2f — %s",
                        symbol, buy_amount, reduced, corr_reason,
                    )
                    buy_amount = reduced
                    if buy_amount < min_buy:
                        logger.info(
                            "SignalWorker: %s reduzierte Größe $%.2f < Regime-Min $%.2f — skipped",
                            symbol, buy_amount, min_buy,
                        )
                        signal_repo.update_signal_status(signal_id, "REJECTED")
                        blocked_reasons.append(f'{symbol}: {corr_reason} → unter Min-Buy')
                        continue

                # d. Get signal price for execution (yfinance data)
                signal_price = float(signal.get("price") or 0.0) if signal.get("price") else None

                # d1. Slippage-Blacklist (fix/slippage-blacklist): Instrumente,
                #     die im 7-Tage-Fenster >=3x am Slippage-Gate scheiterten
                #     (LSE-Micro-Caps mit 7-22% Spread), bekommen KEINEN neuen
                #     Trade — sie können das Gate strukturell nie passieren
                #     und verbrannten nur Trade-Slots (VALT.L 13x/Woche).
                if trade_repo.is_slippage_blacklisted(instrument_id):
                    signal_repo.update_signal_status(signal_id, "REJECTED")
                    blocked_reasons.append(
                        f"{symbol}: Slippage-Blacklist (≥{trade_repo.SLIPPAGE_BLACKLIST_THRESHOLD} "
                        f"Rejects in {trade_repo.SLIPPAGE_WINDOW_DAYS}d — Spread unhandelbar)"
                    )
                    logger.info("SignalWorker: %s auf Slippage-Blacklist — Signal REJECTED, kein Trade", symbol)
                    continue

                # d2. Pre-Trade-Preischeck (fix/slippage-precheck): Live-Preis
                #     SCHON JETZT gegen den Signalpreis prüfen statt erst im
                #     execution_worker — ein unhandelbares Signal erzeugt so
                #     gar keinen Trade (kein Slot-Verbrauch, kein 15-min-Spam).
                if _price_client is not None and signal_price:
                    try:
                        from bot.core.risk import check_slippage_gate, get_max_slippage_pct
                        _live_price = _price_client.get_current_price(instrument_id)
                        _slip = check_slippage_gate(
                            symbol=symbol,
                            signal_price=signal_price,
                            current_price=_live_price,
                            max_slippage_pct=get_max_slippage_pct(symbol, cfg),
                        )
                        if not _slip.allowed:
                            trade_repo.record_slippage_reject(
                                instrument_id, symbol, source="signal_precheck"
                            )
                            signal_repo.update_signal_status(signal_id, "REJECTED")
                            blocked_reasons.append(f"{symbol}: Pre-Check {_slip.summary()[:120]}")
                            logger.info(
                                "SignalWorker: %s Pre-Trade-Preischeck BLOCK — %s (Signal REJECTED, kein Trade)",
                                symbol, _slip.summary(),
                            )
                            continue
                    except Exception as _slip_exc:
                        # Fail-open: Preis nicht ermittelbar → execution-Gate entscheidet
                        logger.debug("SignalWorker: Pre-Check für %s übersprungen (%s)", symbol, _slip_exc)

                # e. Create trade PENDING_APPROVAL → immediately APPROVED
                trade_id = trade_repo.create(
                    instrument_id=instrument_id,
                    symbol=symbol,
                    direction="BUY",
                    amount_usd=buy_amount,
                    stop_loss_pct=cfg.get("sl", {}).get("default_pct", 3.0),
                    signal_id=signal_id,
                    signal_price=signal_price,
                )
                from datetime import datetime, timezone
                trade_repo.update_status(
                    trade_id,
                    "APPROVED",
                    approved_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                )
                # Mark signal as consumed so it won't be re-processed
                signal_repo.update_signal_status(signal_id, "CONSUMED")
                approved_count += 1
    
                # Update running totals so subsequent signals see projected state
                total_exposure += buy_amount
                cash_estimate -= buy_amount
                position_count += 1
                open_positions.append({"symbol": symbol, "amount_usd": buy_amount})
    
                logger.info(
                    "SignalWorker: APPROVED trade #%d — %s %s $%.2f (conviction=%s score=%.2f signal_price=%.4f)",
                    trade_id, "BUY", symbol, buy_amount, conviction, score, signal_price or 0,
                )
                log_repo.write(
                    "INFO",
                    "signal_worker",
                    f"Trade APPROVED: {symbol} BUY ${buy_amount:.2f}",
                    {
                        "trade_id": trade_id,
                        "instrument_id": instrument_id,
                        "conviction": conviction,
                        "score": score,
                        "signal_price": signal_price,
                        "gate_reasons": gate.reasons,
                    },
                )
            else:
                evaluated_count += 1
                reason = gate.summary()
                blocked_reasons.append(f'{symbol}: {reason}')
                # Mark signal as rejected so it won't be re-processed
                signal_repo.update_signal_status(signal_id, "REJECTED")
                logger.info(
                    "SignalWorker: BLOCKED %s $%.2f — %s",
                    symbol, buy_amount, reason,
                )
                logger.info('SignalWorker: %s BLOCKED — %s', symbol, ', '.join(gate.reasons))
                log_repo.write(
                    "INFO",
                    "signal_worker",
                    f"Signal BLOCKED: {symbol}",
                    {
                        "instrument_id": instrument_id,
                        "conviction": conviction,
                        "score": score,
                        "reason": reason,
                    },
                )
    
        # ── 6. Summary ────────────────────────────────────────────────────────────
        print(f"SignalWorker: {evaluated_count} signals evaluated, {approved_count} trades approved")
        log_repo.write(
            "INFO",
            "signal_worker",
            f"Run complete: evaluated={evaluated_count} approved={approved_count} regime={regime}",
        )
    
        # ── 7. Discord summary ────────────────────────────────────────────────────
        if approved_count > 0:
            _post('post_alert_embed',
                title=f'🟢 Signal Worker: {approved_count} Trade(s) approved',
                description=(
                    f'Regime: **{regime}** | scalar={risk_scalar:.2f}\n'
                    f'Evaluated: {evaluated_count} | Approved: {approved_count}\n'
                    f'Equity: ${equity:,.0f} | Cash: ${cash_estimate:,.0f} ({cash_estimate/equity*100:.1f}%)'
                ),
                severity='INFO',
                dry_run=False
            )
        elif evaluated_count == 0 and skipped_closed:
            # All candidates had closed markets — post with context
            _post('post_alert_embed',
                title=f'🔴 Signal Worker: All markets closed ({regime})',
                description=(
                    f'Regime: **{regime}** | scalar={risk_scalar:.2f}\n'
                    f'Signals available: {len(buy_signals)} BUY signals (top 3 evaluated)\n'
                    f'Markets closed: {", ".join(skipped_closed[:5])}\n'
                    f'No trades — waiting for market open.'
                ),
                severity='INFO',
                dry_run=False
            )
        elif evaluated_count > 0 and approved_count == 0:
            # Signals evaluated but all blocked by risk gates
            _post('post_alert_embed',
                title=f'🟡 Signal Worker: All signals blocked ({regime})',
                description=(
                    f'Regime: **{regime}** | scalar={risk_scalar:.2f}\n'
                    f'Evaluated: {evaluated_count} | Approved: 0\n'
                    f'Equity: ${equity:,.0f} | Cash: ${cash_estimate:,.0f} ({cash_estimate/equity*100:.1f}%)\n'
                    f'Blocked reasons:\n' + "\n".join(f'• {r}' for r in blocked_reasons[:5])
                ),
                severity='INFO',
                dry_run=False
            )
        elif evaluated_count == 0:
            _post('post_alert_embed',
                title=f'⚪ Signal Worker: No signals ({regime})',
                description=f'Regime: **{regime}** | min_conviction={min_conviction_for_regime} | scalar={risk_scalar:.2f}',
                severity='INFO',
                dry_run=False
            )
    
    
if __name__ == "__main__":
    main()
