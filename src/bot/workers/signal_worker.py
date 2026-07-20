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


_LLM_NEWS_FLAGS_PATH = PROJECT_ROOT / "data" / "llm_news_flags.json"


def _load_llm_news_flags() -> dict:
    """Laedt News/Earnings-Risk-Flags (fix/llm-news-flags, news_flags_worker,
    stuendlich). Nur daempfend: AVOID → Signal ueberspringen (bleibt FRESH,
    Flag-TTL 12h < Signal-TTL 24h), CAUTION → halbe Positionsgroesse."""
    try:
        if not _LLM_NEWS_FLAGS_PATH.exists():
            return {}
        data = _json_mod.loads(_LLM_NEWS_FLAGS_PATH.read_text())
        expires = data.get("auto_expires_at")
        if expires and _dt.fromisoformat(expires) < _dt.now(_tz.utc):
            return {}
        return data.get("flags", {})
    except Exception:
        return {}


def _get_signal_score_multiplier(signal_type: str, weights: dict) -> float:
    """Gibt Score-Multiplikator fuer Signal-Typ zurueck (1.0 = unveraendert).

    Fix/llm-combo-multiplier (2026-07-15): Combo-Signale wie
    'TREND_PULLBACK,GOLDEN_CROSS' wurden bisher nur als Exact-Match
    in llm_signal_weights.json gesucht — da die Keys aber nur die
    Einzelelemente enthalten (z.B. 'TREND_PULLBACK'=0.5), bekamen
    Combos immer 1.0 und feuerten mit vollem Score obwohl beide
    Komponenten gedämpft sind.

    Jetzt: Exact-Match Priorität, dann komponentenweise Split +
    Multiplikation (komponentenweise Daempfung multipliziert sich).
    """
    if not weights:
        return 1.0
    adj = weights.get("adjustments", {}).get(signal_type)
    if adj is not None:
        # fix/no-boost-weights: asymmetrische Rechte — die LLM darf
        # daempfen/skippen, NIE verstaerken. Hart geclampt (45fc9e1
        # versuchte 1.5x auf Basis von 6 Trades).
        return min(1.0, float(adj.get("score_multiplier", 1.0)))
    # Combo-Signal: Einzelkomponenten pruefen
    if "," in signal_type:
        parts = [p.strip() for p in signal_type.split(",") if p.strip()]
        product = 1.0
        for part in parts:
            part_adj = weights.get("adjustments", {}).get(part)
            if part_adj is not None:
                product *= min(1.0, float(part_adj.get("score_multiplier", 1.0)))
        return product
    return 1.0


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


# ── Diversity Gate -- Signal-Typ-Kategorisierung (Prio 4) ──────────────────────
# Verhindert Ueberkonzentration in einer einzigen Handelsstrategie.
# MAX_CATEGORY_FRACTION: max 45% der offenen Positionen in einer Kategorie.
SIGNAL_CATEGORY: dict[str, str] = {
    "BB_LOWER_RSI_OVERSOLD":   "MEAN_REVERSION",
    "BB_EXTREME_RSI_OVERSOLD": "MEAN_REVERSION",
    "RSI_EXTREME_OVERSOLD":    "MEAN_REVERSION",
    "BB_LOW_MACD_IMPROVING":   "MEAN_REVERSION",
    "MACD_TURN_BELOW_SMA20":   "TREND_FOLLOWING",
    "TREND_PULLBACK":          "TREND_FOLLOWING",
    "GOLDEN_CROSS":            "TREND_FOLLOWING",
}
MAX_CATEGORY_FRACTION = 0.45


def _get_signal_category(signal_type: str) -> str:
    """Gibt die Diversitaets-Kategorie fuer einen Signal-Typ zurueck.

    fix/diversity-combo-types (2026-07-14): signal_type ist in der DB meist
    ein Komma-Kombo ("TREND_PULLBACK,GOLDEN_CROSS") — der alte Exact-Match
    lieferte dafuer immer UNKNOWN, womit das Diversity-Gate fuer ~86% der
    Signale wirkungslos war. Jetzt: Teile splitten und mappen.
      - alle bekannten Teile in einer Kategorie → diese Kategorie
      - Teile aus beiden Kategorien → "MIXED" (eigene 45%-Kappe)
      - kein Teil bekannt → "UNKNOWN" (fail-open) + Warnung, damit neue
        Signal-Typen beim Einfuehren auffallen (SIGNAL_CATEGORY pflegen!)
    """
    parts = [p.strip() for p in (signal_type or "").split(",") if p.strip()]
    counts: dict[str, int] = {}
    unknown: list[str] = []
    for p in parts:
        cat = SIGNAL_CATEGORY.get(p)
        if cat is None:
            unknown.append(p)
        else:
            counts[cat] = counts.get(cat, 0) + 1
    if unknown and parts:
        logger.warning(
            "SignalWorker: Signal-Typ(en) %s nicht in SIGNAL_CATEGORY — "
            "Diversity-Gate fail-open, bitte Kategorie-Map pflegen",
            ",".join(unknown[:3]),
        )
    if not counts:
        return "UNKNOWN"
    if len(counts) == 1:
        return next(iter(counts))
    # fix/diversity-majority (2026-07-15): vorher galt JEDER Kombo mit
    # beiden Familien als MIXED — aber 2xMR+1xTF ist semantisch ein
    # Mean-Reversion-Entry mit Trend-Bestaetigung. Mehrheitsregel;
    # nur echter Gleichstand bleibt MIXED. (Live-Portfolio 2026-07-15:
    # MIXED 6/11 → 2/11, MR 1 → 5 — ehrlichere Kappen-Verteilung.)
    mr = counts.get("MEAN_REVERSION", 0)
    tf = counts.get("TREND_FOLLOWING", 0)
    if mr > tf:
        return "MEAN_REVERSION"
    if tf > mr:
        return "TREND_FOLLOWING"
    return "MIXED"


def _deployment_boost_applies(cash_pct: float, cash_max_pct: float,
                              regime: str, macro_scalar: float,
                              has_news_flag: bool) -> bool:
    """fix/cash-deployment (2026-07-15): Deployment-Boost NUR im
    Schoenwetterfenster — Cash ueber Zielband, Regime NORMAL, Makro-Scalar
    neutral (1.0) und kein News-Flag fuer das Symbol. Die Gates verhindern,
    dass Deployment-Druck gegen die Daempfungs-Philosophie arbeitet
    (Worst Case Kelly 1.5 x Boost 1.25 ~ 1.9x waere ohne sie inakzeptabel;
    absolute Caps Instrument 10% / Exposure 75% bleiben nachgelagert)."""
    return (cash_pct > cash_max_pct
            and regime == "NORMAL"
            and macro_scalar >= 1.0
            and not has_news_flag)


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

        import time as _time_dur
        _t_run_start = _time_dur.monotonic()

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

        _news_flags = _load_llm_news_flags()
        if _news_flags:
            logger.info(
                "SignalWorker: %d News-Risk-Flag(s) aktiv: %s",
                len(_news_flags), ", ".join(list(_news_flags)[:6]),
            )

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
        logger.debug("SignalWorker: regime=%s risk_scalar=%.2f min_conviction=%s", regime, risk_scalar, min_conviction_for_regime)
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
            logger.debug("SignalWorker: 0 signals evaluated, 0 trades approved")
            log_repo.write("INFO", "signal_worker",
                           f"No fresh BUY signals with {min_conviction_for_regime}+ conviction")
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

        # LLM-Makro-Daempfung (fix/llm-macro-advisor): forward-looking Faktor
        # vom macro_regime_worker (taeglich 08:00 CEST). Nur daempfend
        # [0.5..1.0], TTL 26h — fehlt/veraltet/unparsbar → 1.0 (fail-open).
        # Das regelbasierte Regime bleibt unangetastet; wirkt multiplikativ.
        try:
            _macro_raw = state_repo.get("LLM_MACRO_SCALAR")
            _macro_at = state_repo.get("LLM_MACRO_SET_AT") or ""
            _macro = 1.0
            if _macro_raw and _macro_at:
                _at = _dt.fromisoformat(_macro_at)
                if _at.tzinfo is None:
                    _at = _at.replace(tzinfo=_tz.utc)
                if (_dt.now(_tz.utc) - _at).total_seconds() <= 26 * 3600:
                    _macro = max(0.5, min(1.0, float(_macro_raw)))
            if _macro < 1.0:
                buy_aggressiveness *= _macro
                logger.info(
                    "SignalWorker: LLM-Makro-Scalar %.2f aktiv — aggressiveness=%.2f (%s)",
                    _macro, buy_aggressiveness,
                    (state_repo.get("LLM_MACRO_REASON") or "")[:80],
                )
        except Exception as _mx:
            logger.debug("SignalWorker: Makro-Scalar uebersprungen: %s", _mx)
    
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
    
        _ASSET_CLASS_TO_CATEGORY = {
            "forex": "forex", "commodity": "commodities",
            "index": "indices", "crypto": "crypto",
        }

        def _resolve_market_fields(instrument_id: int) -> tuple[str, str]:
            """yfinance_symbol + market_hours-Kategorie fuer den Market-Check.
            Ohne yf_symbol wuerde z.B. ein Forex-Symbol (EURJPY) als US-Aktie
            eingestuft und faelschlich an US-Boersenzeiten gebunden."""
            try:
                row = db.fetchone(
                    "SELECT yfinance_symbol, asset_class FROM instruments WHERE instrument_id=?",
                    (instrument_id,),
                )
                if row:
                    yf_sym = row["yfinance_symbol"] or ""
                    cat = _ASSET_CLASS_TO_CATEGORY.get((row["asset_class"] or "").lower(), "")
                    return yf_sym, cat
            except Exception:
                pass
            return "", ""

        # Diversity-Gate: Kategorie-Verteilung aller offenen Positionen —
        # VOR dem eligible-Loop, damit der Precheck unten Kandidaten an der
        # Kappe gar nicht erst in die knappen Slots laesst (fix/diversity-
        # slot-guard, 2026-07-15).
        _open_signal_cats: dict[str, int] = {}
        try:
            # fix/diversity-fanout (2026-07-14): COUNT(*) zaehlte JOIN-Paare —
            # DISTINCT api_position_id zaehlt echte Positionen (konsistent zum
            # Nenner position_count).
            _cat_rows = db.fetchall("""
                SELECT sig.signal_type, COUNT(DISTINCT ps.api_position_id) as n
                FROM portfolio_snapshot ps
                JOIN trades t ON t.instrument_id = ps.instrument_id AND t.status = 'ACTIVE'
                JOIN signals sig ON sig.id = t.signal_id
                GROUP BY sig.signal_type
            """)
            for _r in _cat_rows:
                _cat = _get_signal_category(str(_r["signal_type"]))
                _open_signal_cats[_cat] = _open_signal_cats.get(_cat, 0) + int(_r["n"])
        except Exception as _dg_exc:
            logger.debug("SignalWorker: Diversity-Gate Daten nicht verfuegbar: %s", _dg_exc)

        skipped_closed: list[str] = []
        skipped_diversity: list[str] = []
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

            # Diversity-Precheck (fix/diversity-slot-guard, 2026-07-15):
            # Kandidaten, deren Kategorie bereits an der 45%-Kappe ist,
            # wuerden im Gate deterministisch geblockt — sie duerfen keinen
            # der 3-5 knappen Slots belegen (Vorfall 2026-07-15: alle 5
            # Slots an MIXED/TF-Kandidaten verschwendet, 0 Trades trotz
            # Pool). Skip statt REJECT: gibt ein Exit Kapazitaet frei, ist
            # das Signal (TTL 24h) sofort wieder Kandidat.
            _pre_cat = _get_signal_category(signal.get("signal_type", ""))
            if (_pre_cat != "UNKNOWN" and position_count > 0
                    and _open_signal_cats.get(_pre_cat, 0) / position_count
                        >= MAX_CATEGORY_FRACTION):
                skipped_diversity.append(f"{symbol}({_pre_cat})")
                continue

            # News/Earnings-Risk-Flag (fix/llm-news-flags): AVOID → Signal
            # ueberspringen, bleibt FRESH (Flag-TTL 12h laeuft vor Signal-TTL
            # 24h ab — das Ereignis kann vorbeigehen). Kein REJECT.
            _nf = _news_flags.get(symbol)
            if _nf and _nf.get("flag") == "AVOID":
                logger.info(
                    "SignalWorker: %s News-Flag AVOID (%s) — uebersprungen",
                    symbol, (_nf.get("reason") or "")[:80],
                )
                continue

            # Market hours (fix/market-hours-slot-guard): Signale geschlossener
            # Boersen bleiben FRESH (kein REJECT — sie werden gueltig, sobald
            # der Markt oeffnet, z.B. EU-Preload ueber Nacht), belegen aber
            # keinen der 3 knappen Kandidaten-Slots pro 15-min-Zyklus.
            # allowEntryOrders in open_position() bleibt die letzte
            # Verteidigungslinie fuer Feiertage/Halts, die der statische
            # Kalender nicht kennt.
            _yf_sym, _mh_category = _resolve_market_fields(instrument_id)
            if not is_market_open(symbol, _yf_sym, _mh_category, fail_open=False):
                skipped_closed.append(symbol)
                continue

            eligible.append((signal, symbol))
    
        if skipped_diversity:
            logger.info(
                "SignalWorker: %d Kandidat(en) am Diversity-Precheck uebersprungen "
                "(Kategorie an 45%%-Kappe, Signal bleibt FRESH): %s",
                len(skipped_diversity), ", ".join(skipped_diversity[:6]),
            )

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
    
        # Adaptive Kandidaten-Slots (fix/adaptive-slots): 3 Standard. 5 wenn
        # Kapital brach liegt (cash > cash_target_max_pct der Equity) UND der
        # Pool >= 4 HIGH/VERY_HIGH-Kandidaten hat — an starken Signaltagen
        # soll ueberschuessiges Cash arbeiten, ohne die Qualitaetsschwelle zu
        # senken. Alle nachgelagerten Gates (Exposure, Cash-Floor, Kelly,
        # Diversity, Slippage) gelten unveraendert pro Kandidat.
        # fix/cash-deployment (2026-07-15, Umbau der adaptiven Slots):
        # vorher nahmen die Extra-Slots einfach Top-4/5 des Pools — Slot 4/5
        # konnten MEDIUM-Kandidaten sein, die >=4-HIGH+-Bedingung war nur
        # ein Proxy. Jetzt: Basis 3 Slots fuer alle; bei Cash-Ueberschuss
        # werden Slots 4-5 AUSSCHLIESSLICH mit HIGH/VERY_HIGH aus dem Rest
        # befuellt — Qualitaet der Extra-Slots ist strukturell garantiert,
        # eine Mindestanzahl-Schwelle ist damit ueberfluessig.
        candidates = unique_candidates[:3]
        try:
            _cash_max_pct = float(cfg.get("trading", {}).get("cash_target_max_pct", 30.0))
            _cash_pct = (cash_estimate / equity * 100.0) if equity > 0 else 0.0
            if _cash_pct > _cash_max_pct:
                _extra = [
                    (_s, _sym) for _s, _sym in unique_candidates[3:]
                    if (_s.get("conviction") or "").upper() in ("HIGH", "VERY_HIGH")
                ][:2]
                if _extra:
                    candidates = candidates + _extra
                    logger.info(
                        "SignalWorker: Adaptive Slots 3->%d (Cash %.1f%% > %.1f%%, "
                        "Extra-Slots nur HIGH+): %s",
                        len(candidates), _cash_pct, _cash_max_pct,
                        ", ".join(_sym for _s, _sym in _extra),
                    )
        except Exception:
            pass
    
        evaluated_count = 0
        approved_count = 0
        approved_trades_info: list[dict] = []
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

            # Kelly: dynamische Groessenkorrektur basierend auf Signal-Performance (Prio 1)
            # Half-Kelly [0.3, 1.5]: mehr Kapital in bewiesene Signale, weniger in schwache.
            try:
                from bot.core.sizing import kelly_size_factor
                _k = kelly_size_factor(signal.get("signal_type", ""), db)
                if _k != 1.0:
                    _old_amt = buy_amount
                    buy_amount = round(buy_amount * _k, 2)
                    logger.info(
                        "SignalWorker: Kelly: signal_type=%s k=%.2f amount $%.2f->$%.2f",
                        signal.get("signal_type", ""), _k, _old_amt, buy_amount,
                    )
            except Exception as _ke:
                logger.debug("SignalWorker: Kelly-Faktor uebersprungen: %s", _ke)

            # News-Flag CAUTION → halbe Groesse (fix/llm-news-flags, nur daempfend)
            _nf = _news_flags.get(symbol)
            if _nf and _nf.get("flag") == "CAUTION":
                buy_amount = round(buy_amount * 0.5, 2)
                logger.info(
                    "SignalWorker: %s News-Flag CAUTION — Groesse halbiert auf $%.2f (%s)",
                    symbol, buy_amount, (_nf.get("reason") or "")[:60],
                )

            # Deployment-Boost (fix/cash-deployment 2026-07-15): brachliegendes
            # Kapital arbeiten lassen. Config-Default 1.0 = AUS — auf 1.25
            # erhoehen, sobald der Stale-Exit scharf und bewaehrt ist (erst
            # Kapital-Freisetzung beweisen, dann Deployment-Druck — sonst ist
            # die Equity-Attribution zerstoert). Hart geclampt <= 1.5.
            try:
                _boost = float(cfg.get("trading", {}).get("deployment_boost", 1.0))
                _boost = max(1.0, min(1.5, _boost))
                if _boost > 1.0 and _deployment_boost_applies(
                    cash_pct=(cash_estimate / equity * 100.0) if equity > 0 else 0.0,
                    cash_max_pct=float(cfg.get("trading", {}).get("cash_target_max_pct", 30.0)),
                    regime=regime,
                    macro_scalar=_macro,
                    has_news_flag=symbol in _news_flags,
                ):
                    _old_amt = buy_amount
                    buy_amount = round(buy_amount * _boost, 2)
                    logger.info(
                        "SignalWorker: Deployment-Boost aktiv x%.2f — $%.2f -> $%.2f "
                        "(Cash-Ueberschuss, NORMAL, Makro neutral)",
                        _boost, _old_amt, buy_amount,
                    )
            except Exception as _db_exc:
                logger.debug("SignalWorker: Deployment-Boost uebersprungen: %s", _db_exc)
    
            # Enforce minimum from regime params
            min_buy = regime_params.get("min_buy_usd", 50.0)
            if buy_amount < min_buy:
                # fix/min-buy-slot-leak (2026-07-14): vorher nur `continue` ohne
                # Status-Update — das Signal blieb FRESH und belegte JEDEN
                # Zyklus erneut einen Kandidaten-Slot bis zum 24h-TTL (Kelly
                # 0.3x oder CAUTION-Halbierung aendern sich innerhalb des TTL
                # nicht). REJECT gibt den Slot frei.
                logger.info(
                    "SignalWorker: %s buy_amount $%.2f < regime min $%.2f — Signal REJECTED",
                    symbol, buy_amount, min_buy,
                )
                signal_repo.update_signal_status(signal_id, "REJECTED")
                blocked_reasons.append(
                    f"{symbol}: Groesse ${buy_amount:.2f} < Min ${min_buy:.0f} (Kelly/CAUTION)"
                )
                continue

            # Broker-Minimum (fix/order-error-learning 2026-07-16): eToro-Fehler
            # 720 nennt pro Instrument ein Mindest-Positionsvolumen (NATGAS:
            # $1000 bei x1); der execution_worker lernt den Wert aus der
            # Ablehnung in instruments.min_position_amount. Unterhalb wird gar
            # nicht erst approved — Groesse wird NIE hochskaliert (Sizing-Treue).
            _broker_min = None
            try:
                _min_row = signal_repo.db.fetchone(
                    "SELECT min_position_amount FROM instruments WHERE instrument_id = ?",
                    (signal.get("instrument_id"),),
                )
                if _min_row and _min_row["min_position_amount"]:
                    _broker_min = float(_min_row["min_position_amount"])
            except Exception:
                _broker_min = None  # Spalte fehlt (aeltere Test-DBs) -> fail-open
            if _broker_min and buy_amount < _broker_min:
                logger.info(
                    "SignalWorker: %s buy_amount $%.2f < Broker-Minimum $%.0f — Signal REJECTED",
                    symbol, buy_amount, _broker_min,
                )
                signal_repo.update_signal_status(signal_id, "REJECTED")
                blocked_reasons.append(
                    f"{symbol}: ${buy_amount:.2f} < Broker-Min ${_broker_min:.0f} (eToro 720)"
                )
                continue

            # Post-Loss-Cooldown (fix/post-loss-cooldown 2026-07-17): nach
            # einem Verlust-Close desselben Instruments X Stunden keinen
            # neuen BUY — verhindert das LUS1.DE-Muster (#446 oeffnete in
            # der Minute des #439-Close und starb 12min spaeter am SL).
            # Oversold-Signale feuern nach einem SL-Kill naturgemaess sofort
            # wieder; ein frischer Verlust ist aber die Widerlegung der
            # Einstiegsthese, kein neues Setup.
            _cd_h = float(cfg.get("trading", {}).get("post_loss_cooldown_h", 24.0))
            if _cd_h > 0:
                try:
                    _cd_row = signal_repo.db.fetchone(
                        "SELECT closed_at FROM trades "
                        "WHERE instrument_id = ? AND status = 'CLOSED' "
                        "AND pnl_usd < 0 AND closed_at >= datetime('now', ?) "
                        "ORDER BY closed_at DESC LIMIT 1",
                        (signal.get("instrument_id"), f"-{_cd_h} hours"),
                    )
                except Exception:
                    _cd_row = None  # fail-open
                if _cd_row:
                    logger.info(
                        "SignalWorker: %s Post-Loss-Cooldown (%sh) — Verlust-Close %s, Signal REJECTED",
                        symbol, _cd_h, _cd_row["closed_at"],
                    )
                    signal_repo.update_signal_status(signal_id, "REJECTED")
                    blocked_reasons.append(
                        f"{symbol}: Post-Loss-Cooldown ({_cd_h:.0f}h seit Verlust-Close)"
                    )
                    continue

            # MR-Sperre ausserhalb NORMAL (User-Entscheid 2026-07-17): 9/12
            # der juengsten Verlust-Trades waren Mean-Reversion-Kaeufe unter
            # der SMA20 im schwachen Markt (Messer-Fangen). Reine
            # MEAN_REVERSION-Signale werden in CAUTION/DEFENSIVE nicht
            # approved; MIXED und TREND_FOLLOWING bleiben erlaubt.
            if (
                cfg.get("trading", {}).get("block_mean_reversion_in_caution", True)
                and regime != "NORMAL"
                and _get_signal_category(str(signal.get("signal_type") or "")) == "MEAN_REVERSION"
            ):
                logger.info(
                    "SignalWorker: %s MEAN_REVERSION in %s geblockt — Signal REJECTED",
                    symbol, regime,
                )
                signal_repo.update_signal_status(signal_id, "REJECTED")
                blocked_reasons.append(f"{symbol}: MEAN_REVERSION in {regime} gesperrt")
                continue

            # MACD-Bestaetigungspflicht fuer Oversold (feat/strategy-gates
            # 2026-07-20, 30d-DB-Fakten): Oversold-Kombis OHNE MACD-
            # Komponente = WR 8% (63 Trades, -159 USD); MIT = WR 32%.
            # Alle grossen Gewinner (BABA/CVX/LHYFE) hatten die MACD-Wende
            # dabei, alle Messer-Kills (HDF -31$, RWAY, LUS1 bei RSI 11-21)
            # nicht. Reines Oversold ist der Preis im freien Fall — die
            # MACD-Wende ist der Beleg, dass der Fall bremst.
            _st_upper = str(signal.get("signal_type") or "").upper()
            if (
                cfg.get("trading", {}).get("require_macd_confirmation_for_oversold", True)
                and "OVERSOLD" in _st_upper
                and "MACD" not in _st_upper
            ):
                logger.info(
                    "SignalWorker: %s Oversold ohne MACD-Bestaetigung (%s) — Signal REJECTED",
                    symbol, _st_upper[:60],
                )
                signal_repo.update_signal_status(signal_id, "REJECTED")
                blocked_reasons.append(f"{symbol}: Oversold ohne MACD-Wende (Messer-Schutz)")
                continue
    
            # c. Run master buy gate V5
            # fix/sl-gate-wiring: entry_price/sl_price wurden als 0 übergeben —
            # das SL-Quality-Gate (Bible Rule 1) prüfte damit NIE etwas.
            # Jetzt: Signalpreis als Entry, SL daraus mit derselben Formel
            # berechnet, die später open_position() verwendet.
            from bot.core.risk import adaptive_sl_pct, calculate_sl_price
            gate_entry_price = float(signal.get("price") or 0.0)

            # feat/strategy-gates (2026-07-20): Stop atmet mit der Tagesvola
            # (11/17 SL-Kills hatten ATR > Fix-SL — Rauschen, nicht Trend).
            # Sizing skaliert gegenlaeufig (Risk-Parity, Faktor-Floor 0.6),
            # damit das Dollar-Risiko pro Trade konstant bleibt; das Broker-
            # Minimum sichert der Execution-Preflight ab.
            _sl_default = float(cfg.get("sl", {}).get("default_pct", 3.0))
            _sl_pct_final = _sl_default
            if cfg.get("sl", {}).get("atr_adaptive", True):
                try:
                    _atr_row = signal_repo.db.fetchone(
                        "SELECT atr_pct FROM instruments WHERE instrument_id = ?",
                        (signal.get("instrument_id"),),
                    )
                    _sl_pct_final = adaptive_sl_pct(
                        _sl_default,
                        _atr_row["atr_pct"] if _atr_row else None,
                        multiple=float(cfg.get("sl", {}).get("atr_multiple", 1.5)),
                        max_pct=float(cfg.get("sl", {}).get("max_pct", 6.0)),
                    )
                except Exception:
                    _sl_pct_final = _sl_default
                if _sl_pct_final > _sl_default and buy_amount > 0:
                    _parity = max(_sl_default / _sl_pct_final, 0.6)
                    buy_amount = round(buy_amount * _parity, 2)
                    logger.info(
                        "SignalWorker: %s ATR-SL %.2f%% (Default %.2f%%) — Sizing x%.2f (Risk-Parity)",
                        symbol, _sl_pct_final, _sl_default, _parity,
                    )

            gate_sl_price = (
                calculate_sl_price(gate_entry_price, symbol, _sl_pct_final)
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

                # Diversity-Gate (Prio 4): max 45% offener Positionen in einer Kategorie
                _sig_cat = _get_signal_category(signal.get("signal_type", ""))
                if _sig_cat != "UNKNOWN" and position_count > 0:
                    _cat_n = _open_signal_cats.get(_sig_cat, 0)
                    if _cat_n / position_count >= MAX_CATEGORY_FRACTION:
                        logger.info(
                            "SignalWorker: Diversity-Gate: %s (%s) %d/%d Pos. (%.0f%%>=%.0f%%) -- geblockt",
                            _sig_cat,
                            signal.get("signal_type", ""),
                            _cat_n,
                            position_count,
                            _cat_n / position_count * 100,
                            MAX_CATEGORY_FRACTION * 100,
                        )
                        signal_repo.update_signal_status(signal_id, "REJECTED")
                        blocked_reasons.append(
                            f"{symbol}: Diversity-Gate {_sig_cat} {_cat_n}/{position_count}"
                        )
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
                    stop_loss_pct=_sl_pct_final,  # feat/strategy-gates: ATR-adaptiv
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
                approved_trades_info.append({
                    "symbol":       symbol,
                    "amount_usd":   buy_amount,
                    "signal_type":  signal.get("signal_type", ""),
                    "conviction":   conviction,
                    "score":        score,
                    "signal_price": signal_price,
                })
    
                # Update running totals so subsequent signals see projected state
                total_exposure += buy_amount
                cash_estimate -= buy_amount
                position_count += 1
                open_positions.append({"symbol": symbol, "amount_usd": buy_amount})
                # Kategorie-Projektion aktualisieren, damit das Gate fuer die
                # naechsten Kandidaten dieses Laufs den neuen Stand sieht
                _appr_cat = _get_signal_category(signal.get("signal_type", ""))
                if _appr_cat != "UNKNOWN":
                    _open_signal_cats[_appr_cat] = _open_signal_cats.get(_appr_cat, 0) + 1
    
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
    
        try:
            from bot.core.heartbeat import record_duration as _rd
            _rd(state_repo, "signal_worker", _time_dur.monotonic() - _t_run_start)
        except Exception:
            pass

        # ── 6. Summary ────────────────────────────────────────────────────────────
        if approved_count > 0:
            print(f"SignalWorker: {evaluated_count} signals evaluated, {approved_count} trades approved")
        else:
            logger.debug("SignalWorker: %d signals evaluated, 0 trades approved", evaluated_count)
        log_repo.write(
            "INFO",
            "signal_worker",
            f"Run complete: evaluated={evaluated_count} approved={approved_count} regime={regime}",
        )
    
        # ── 7. Discord summary ────────────────────────────────────────────────────
        if approved_count > 0:
            _post(
                'post_signal_worker_embed',
                approved_trades=approved_trades_info,
                regime=regime,
                risk_scalar=risk_scalar,
                evaluated_count=evaluated_count,
                equity=equity,
                cash=cash_estimate,
                total_exposure=total_exposure,
                position_count=position_count,
            )
        elif evaluated_count == 0 and skipped_closed:
            # All candidates had closed markets — Normalzustand nachts/Wochenende.
            # Drossel 1x/6h (fix/market-hours-slot-guard: Pfad war vorher toter
            # Code; ungebremst wuerde er alle 15 min posten).
            try:
                from datetime import datetime as _dt2, timezone as _tz2
                _last = state_repo.get("SIGNAL_CLOSED_POSTED_AT") or ""
                _post_now = True
                if _last:
                    _last_dt = _dt2.fromisoformat(_last)
                    if _last_dt.tzinfo is None:
                        _last_dt = _last_dt.replace(tzinfo=_tz2.utc)
                    _post_now = (_dt2.now(_tz2.utc) - _last_dt).total_seconds() >= 6 * 3600
                if _post_now:
                    state_repo.set("SIGNAL_CLOSED_POSTED_AT", _dt2.now(_tz2.utc).isoformat())
                    _post('post_alert_embed',
                        title=f'🔴 Signal Worker: All markets closed ({regime})',
                        description=(
                            f'Regime: **{regime}** | scalar={risk_scalar:.2f}\n'
                            f'Signals available: {len(buy_signals)} BUY signals\n'
                            f'Markets closed: {", ".join(skipped_closed[:5])}\n'
                            f'No trades — waiting for market open.'
                        ),
                        severity='INFO',
                        dry_run=False
                    )
            except Exception:
                pass
        elif evaluated_count > 0 and approved_count == 0:
            # Throttle auf 1x/Stunde -- bei dauerhaftem Exposure-Gate kein Spam
            try:
                from datetime import datetime as _dt, timezone as _tz
                _last = state_repo.get("SIGNAL_BLOCKED_POSTED_AT") or ""
                _post_now = True
                if _last:
                    _last_dt = _dt.fromisoformat(_last)
                    if _last_dt.tzinfo is None:
                        _last_dt = _last_dt.replace(tzinfo=_tz.utc)
                    _post_now = (_dt.now(_tz.utc) - _last_dt).total_seconds() >= 3600
                if _post_now:
                    state_repo.set("SIGNAL_BLOCKED_POSTED_AT", _dt.now(_tz.utc).isoformat())
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
            except Exception:
                pass
        # (kein Post bei 0 ausgewerteten Signalen -- monitor_worker uebernimmt Routine)
    
    
if __name__ == "__main__":
    main()
