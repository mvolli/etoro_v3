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
    # Combo-Signal: Einzelkomponenten + Teilmengen-Combos pruefen
    if "," in signal_type:
        sig_parts = _split_signal_type(signal_type)
        sig_set = set(sig_parts)
        product = 1.0
        for part in sig_parts:
            part_adj = weights.get("adjustments", {}).get(part)
            if part_adj is not None:
                product *= min(1.0, float(part_adj.get("score_multiplier", 1.0)))
        # fix/combo-subset-propagation (2026-07-26): Combo-Keys wie
        # 'BB_LOWER_RSI_OVERSOLD,BB_EXTREME_RSI_OVERSOLD'=0.4 griffen
        # bisher NICHT auf Obermengen-Combos (Dreier-Combo mit denselben
        # Komponenten lief mit 1.0 trotz 2.7% WR im Scorecard). Jetzt:
        # jeder Weights-Key, dessen Komponenten Teilmenge der Signal-
        # Komponenten sind, wirkt; die staerkste Daempfung gewinnt (min).
        result = product
        for key, key_adj in weights.get("adjustments", {}).items():
            if "," not in key or key_adj is None:
                continue
            if set(_split_signal_type(key)) <= sig_set:
                result = min(result, min(1.0, float(key_adj.get("score_multiplier", 1.0))))
        return result
    return 1.0


def _split_signal_type(signal_type: str) -> list[str]:
    """Zerlegt einen (Combo-)Signal-Typ-String in seine Komponenten."""
    return [p.strip() for p in signal_type.split(",") if p.strip()]


def _is_signal_type_skipped(signal_type: str, weights: dict) -> tuple[bool, str]:
    """Prueft ob Signal-Typ durch LLM gesperrt wurde. Gibt (skip, reason) zurueck.

    fix/skip-combo-decomposition (2026-07-26): bisher nur Exact-Match —
    ein skip auf eine Komponente (z.B. 'TREND_PULLBACK') blockte still
    KEINE Combo, die sie enthielt. Jetzt symmetrisch zum Multiplier:
    Komponenten + Teilmengen-Combo-Keys werden mitgeprueft.
    """
    if not weights:
        return False, ""
    adjustments = weights.get("adjustments", {})
    adj = adjustments.get(signal_type)
    if adj and adj.get("skip"):
        return True, adj.get("reason", "LLM: Signal-Typ deaktiviert")
    if "," in signal_type:
        sig_set = set(_split_signal_type(signal_type))
        for key, key_adj in adjustments.items():
            if not key_adj or not key_adj.get("skip"):
                continue
            if set(_split_signal_type(key)) <= sig_set:
                return True, key_adj.get(
                    "reason", f"LLM: Komponente {key} deaktiviert"
                )
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



def _signal_age_factor(generated_at_iso: str, ttl_minutes: int = 1440) -> float:
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



def _signal_performance_decay(
    signal_type: str,
    db_path: Path,
    lookback_days: int = 14,
    min_trades: int = 5,
) -> float:
    """Dampft den Score eines Signal-Typs basierend auf 14-Tage-Performance.

    fix/signal-performance-decay (2026-08-03): Bisher feuern Signale
    weiter mit vollem (LLM-)Score, obwohl sie in der Backtest-Periode
    verlieren (z.B. CORE_SWEEP: -2.99% avg_pnl, 35% WR in 7 Tagen).
    Diese Funktion liest die CLOSED-Trades der letzten N Tage und
    berechnet einen multiplikativen Decay [0.3..1.0], der Signale mit
    schlechter Performance im Ranking nach unten ruckt - ohne sie zu
    blocken (LLM kann trotzdem skippen).

    Regel:
      - < min_trades Trades -> keine Aussage, fail-open
      - WR >= 40% AND avg_pnl >= 0 -> keine Dampfung
      - WR < 30% -> 0.3 (stark gedampft)
      - WR 30-35% -> 0.5
      - WR 35-40% -> 0.7
      - avg_pnl < -2.0% -> extra 0.8 Multiplikator
    """
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = _sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT pnl_usd FROM trades "
            "WHERE status = 'CLOSED' AND pnl_usd IS NOT NULL "
            "AND created_at >= datetime('now', ?) "
            "AND signal_id IN ("
            "  SELECT id FROM signals WHERE signal_type = ?"
            ")",
            (f"-{lookback_days} days", signal_type),
        )
        rows = cur.fetchall()
        conn.close()

        n = len(rows)
        if n < min_trades:
            return 1.0

        wins = sum(1 for r in rows if float(r["pnl_usd"]) > 0)
        wr = wins * 100.0 / n
        avg_pnl = sum(float(r["pnl_usd"]) for r in rows) / n

        if wr >= 40 and avg_pnl >= 0:
            return 1.0
        if wr < 30:
            decay = 0.3
        elif wr < 35:
            decay = 0.5
        else:
            decay = 0.7
        if avg_pnl < -2.0:
            decay *= 0.8
        return max(0.3, decay)
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
    # fix/diversity-mixed-cap (2026-08-28): CORE_SWEEP fehlte, das Gate lief
    # dafuer fail-open und schrieb in JEDEM Lauf eine Warnung.
    "CORE_SWEEP":              "CORE",
}
MAX_CATEGORY_FRACTION = 0.45

# fix/diversity-mixed-cap (2026-08-28, Entscheid VoLLi): Kappe pro Kategorie
# ueberschreibbar (`diversity.category_overrides.<KATEGORIE>`), 1.0 = aus.
#
# MIXED steht per Default auf 1.0 (keine Kappe). Grund: MIXED ist KEINE
# Strategie, sondern der Rest-Topf fuer Gleichstaende in
# _get_signal_category() — "eine Komponente MR, eine TF". Er bekam
# dieselbe 45 %-Kappe wie die echten Strategien, obwohl ein halb-MR/halb-TF-
# Setup eher diversifizierter als konzentrierter ist.
#
# Messung, die die Entscheidung traegt (2026-08-28, live):
#   - MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING loest als 1 TF + 1 MR zu
#     MIXED auf. Das ist der BESTE Typ des Bots (n=41, WR 51,2 %, +55,99 USD)
#     und stellt 61 % aller Kaufsignale der letzten 7 Tage.
#   - Das Buch fuellte sich damit auf 10/14 = 71,4 % > 45 %. Ab da wurde jedes
#     neue MIXED-Signal im Precheck uebersprungen (bleibt FRESH, erzeugt keinen
#     trades-Eintrag — deshalb null sichtbare Ablehnungen).
#   - Raus kam der Bot nicht: nachruecken konnte nur wieder MIXED. Deadlock.
#     Haeufigster Blocker in 36 von 96 Laeufen; 102 Signale -> 20 Kandidaten
#     -> 1 genehmigter Trade in 24 h bei 88 % Cash.
# MEAN_REVERSION/TREND_FOLLOWING/CORE behalten die 45 %-Kappe — dort schuetzt
# sie gegen echte Strategie-Konzentration.
CATEGORY_FRACTION_OVERRIDES: dict[str, float] = {"MIXED": 1.0}


def apply_diversity_config(cfg: dict) -> None:
    """Setzt MAX_CATEGORY_FRACTION und die Pro-Kategorie-Overrides aus der Config.

    Fail-safe wie regime.apply_config: unbrauchbare Werte werden protokolliert
    und ignoriert, der Default bleibt stehen.

    ACHTUNG (dritte Auflage der "config wiring lie", vgl. facfa5a): Wer eine
    Modul-Konstante liest, muss deren apply_config im EIGENEN Prozess
    aufrufen. Hier ist es dasselbe Modul wie der Leser — der Aufruf steht
    trotzdem explizit in main(), damit er beim Kopieren nicht verlorengeht.
    """
    global MAX_CATEGORY_FRACTION
    dv = (cfg or {}).get("diversity", {}) or {}
    try:
        _f = float(dv.get("max_category_fraction", MAX_CATEGORY_FRACTION))
        if 0.0 < _f <= 1.0:
            MAX_CATEGORY_FRACTION = _f
        else:
            logger.error("diversity.max_category_fraction %r ausserhalb (0,1] — Default bleibt", _f)
    except (TypeError, ValueError):
        logger.error("diversity.max_category_fraction unlesbar — Default bleibt")
    ov = dv.get("category_overrides") or {}
    if not isinstance(ov, dict):
        return
    for key, value in ov.items():
        try:
            _v = float(value)
        except (TypeError, ValueError):
            logger.error("diversity.category_overrides[%s]=%r unlesbar — ignoriert", key, value)
            continue
        if not (0.0 < _v <= 1.0):
            logger.error("diversity.category_overrides[%s]=%r ausserhalb (0,1] — ignoriert", key, value)
            continue
        CATEGORY_FRACTION_OVERRIDES[str(key).upper()] = _v


def _max_fraction_for(category: str) -> float:
    """Kappe fuer eine Kategorie; 1.0 bedeutet effektiv keine Kappe."""
    return CATEGORY_FRACTION_OVERRIDES.get(str(category).upper(), MAX_CATEGORY_FRACTION)


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


DEPLOYMENT_BOOST_REGIMES: tuple[str, ...] = ("NORMAL", "CAUTION", "DEFENSIVE")


def apply_deployment_config(cfg: dict) -> None:
    """Setzt DEPLOYMENT_BOOST_REGIMES aus `trading.deployment_boost_regimes`.

    CRITICAL wird immer entfernt — dort ist der Stillstand gewollt.
    """
    global DEPLOYMENT_BOOST_REGIMES
    raw = ((cfg or {}).get("trading", {}) or {}).get("deployment_boost_regimes")
    if raw is None:
        return
    if not isinstance(raw, (list, tuple)):
        logger.error("trading.deployment_boost_regimes %r ist keine Liste — Default bleibt", raw)
        return
    vals = tuple(str(r).upper() for r in raw if str(r).upper() != "CRITICAL")
    if not vals:
        logger.error("trading.deployment_boost_regimes leer (nach CRITICAL-Filter) — Default bleibt")
        return
    DEPLOYMENT_BOOST_REGIMES = vals


from bot.core.regime import (
    SIZING_PARITY_FLOOR as _PARITY_FLOOR,
    dust_floor_usd as _dust_floor_usd,
)


def _build_signal_report(db, all_signals, *, approved_syms=None,
                         blocked_reasons=None, skip_map=None,
                         candidate_ids=None) -> list:
    """Eine Zeile je Signal fuer den Discord-Bericht.

    Der Embed soll auf einen Blick zeigen, WELCHE Signale ein Lauf gesehen hat
    und was daraus wurde. Die Richtung kommt aus dem signal_type — die
    signals-Tabelle mischt Kauf und Verkauf, und genau diese Vermischung hat
    am 2026-08-29 eine Diagnose fehlgeleitet (463 von 465 vermeintlich
    "verfallenen" Kryptosignalen waren OVERBOUGHT, also SELL).

    Das Ergebnis wird NACHTRAEGLICH aus den vorhandenen Sammlungen
    zusammengesetzt statt an jedem der ~12 continue-Punkte mitgefuehrt: das
    haelt die Schleife unveraendert und kann nichts kaputtmachen.
    """
    approved_syms = approved_syms or set()
    skip_map = skip_map or {}
    candidate_ids = candidate_ids if candidate_ids is not None else None

    ids = [s.get("instrument_id") for s in all_signals if s.get("instrument_id")]
    sym_by_id = {}
    if ids:
        try:
            _ph = ",".join("?" * len(ids))
            for r in (db.fetchall(
                    f"SELECT instrument_id, symbol FROM instruments "
                    f"WHERE instrument_id IN ({_ph})", list(ids)) or []):
                sym_by_id[int(r["instrument_id"])] = str(r["symbol"])
        except Exception:
            pass

    # "SYMBOL: Grund" -> Grund
    by_sym = {}
    for _br in (blocked_reasons or []):
        if ":" in str(_br):
            _s, _r = str(_br).split(":", 1)
            by_sym.setdefault(_s.strip(), _r.strip())
    # Filtername je Symbol (Eintraege koennen "SYM[kategorie]" sein)
    skip_by_sym = {}
    for _k, _v in skip_map.items():
        for _e in _v:
            skip_by_sym.setdefault(str(_e).split("[")[0].strip(), str(_k))

    rows = []
    for s in all_signals:
        st = str(s.get("signal_type") or "?")
        up = st.upper()
        direction = "SELL" if ("SELL" in up or "OVERBOUGHT" in up) else "BUY"
        sym = sym_by_id.get(s.get("instrument_id"), f"id{s.get('instrument_id')}")
        if direction == "SELL":
            outcome = "Verkaufssignal — kein Kaufkandidat"
        elif sym in approved_syms:
            outcome = "genehmigt"
        elif sym in by_sym:
            outcome = by_sym[sym]
        elif sym in skip_by_sym:
            outcome = skip_by_sym[sym]
        elif candidate_ids is not None and s.get("id") not in candidate_ids:
            outcome = "nicht bewertet (Slots belegt)"
        else:
            outcome = "bewertet, kein Trade"
        rows.append({
            "symbol":      sym,
            "signal_type": st,
            "conviction":  s.get("conviction") or "?",
            "score":       s.get("score") or 0,
            "direction":   direction,
            "outcome":     outcome,
        })
    # Je (Symbol, Richtung) nur das staerkste Signal — ein Instrument kann
    # mehrere frische Signale tragen (verschiedene Zyklen, gleiche Bedingung).
    # Die Anzahl wird angehaengt, damit die Verdichtung sichtbar bleibt.
    best = {}
    for r in rows:
        k = (r["symbol"], r["direction"])
        prev = best.get(k)
        if prev is None or float(r["score"] or 0) > float(prev["score"] or 0):
            r = dict(r)
            r["_n"] = (prev or {}).get("_n", 0) + 1
            best[k] = r
        else:
            prev["_n"] = prev.get("_n", 1) + 1
    rows = []
    for r in best.values():
        if r.get("_n", 1) > 1:
            r["symbol"] = f"{r['symbol']} x{r['_n']}"
        r.pop("_n", None)
        rows.append(r)

    # Kaufsignale zuerst, darin nach Score absteigend.
    rows.sort(key=lambda r: (r["direction"] == "SELL", -float(r["score"] or 0)))
    return rows


def _reject_below_floor(log_repo, signal_repo, blocked_reasons, *, symbol,
                        signal_id, amount, floor, kind, stage, detail=""):
    """Signal wegen Groessen-Untergrenze verwerfen — und es PERSISTIEREN.

    Bis 2026-08-28 gingen diese Rejects ausschliesslich nach logger.info.
    In system_log standen ueber 30 Tage 0 Treffer, in den Cron-Outputs
    ebenfalls 0 — wie oft die Korrelations- und Regionen-Pruefungen einen
    Trade verwarfen, war schlicht nicht feststellbar. Ein Filter, dessen
    Wirkung man nicht messen kann, laesst sich auch nicht bewerten (dasselbe
    Muster wie bei DRAWDOWN_REASON).

    kind: SIGNAL_FLOOR (vor den Haircuts) | DUST_FLOOR (Endbetrag).
    """
    logger.info(
        "SignalWorker: %s %s $%.2f < $%.2f (%s) — Signal REJECTED%s",
        symbol, kind, amount, floor, stage, f" [{detail}]" if detail else "",
    )
    try:
        signal_repo.update_signal_status(signal_id, "REJECTED")
    except Exception:
        logger.debug("update_signal_status fehlgeschlagen", exc_info=True)
    blocked_reasons.append(
        f"{symbol}: ${amount:.2f} < {kind} ${floor:.0f} ({stage})"
    )
    try:
        log_repo.write(
            "INFO", "signal_worker",
            f"Signal BLOCKED: {symbol} unter {kind} (${amount:.2f} < ${floor:.2f})",
            {
                "symbol": symbol, "signal_id": signal_id,
                "amount_usd": round(float(amount), 2),
                "floor_usd": round(float(floor), 2),
                "floor_kind": kind, "stage": stage, "detail": detail,
            },
        )
    except Exception:
        logger.debug("log_repo.write fehlgeschlagen (fail-open)", exc_info=True)


def _deployment_boost_applies(cash_pct: float, cash_max_pct: float,
                              regime: str, macro_scalar: float,
                              has_news_flag: bool) -> bool:
    """fix/cash-deployment (2026-07-15): Deployment-Boost nur bei Cash ueber
    Zielband, neutralem Makro-Scalar und ohne News-Flag fuer das Symbol.

    fix/deployment-boost-regimes (2026-08-28, Entscheid VoLLi): Die
    Regime-Bedingung war `regime == "NORMAL"`. Damit war der Mechanismus, der
    brachliegendes Kapital arbeiten lassen soll, genau dann aus, wenn am
    meisten Kapital brachliegt — der Bot stand 5 Tage in DEFENSIVE bei 88 %
    Cash, `deployment_boost: 1.25` hat in dieser Zeit kein einziges Mal
    gefeuert.

    Die urspruengliche Begruendung ("Worst Case Kelly 1.5 x Boost 1.25 ~ 1.9x
    waere inakzeptabel") traegt ausserhalb NORMAL nicht, weil der
    Regime-risk_scalar VOR dem Boost multipliziert wird:
        NORMAL     1.00 x 1.5 x 1.25 = 1.88x   <- das gemeinte Risiko
        CAUTION    0.75 x 1.5 x 1.25 = 1.41x
        DEFENSIVE  0.50 x 1.5 x 1.25 = 0.94x   <- UNTER dem NORMAL-Normalfall
    Der Boost hebt die Groesse in DEFENSIVE also hoechstens auf das Niveau,
    das ohne Regime-Daempfung ohnehin gaelte. CRITICAL bleibt bewusst
    ausgeschlossen (dort ist der Stillstand gewollt), ebenso bleiben die
    absoluten Caps Instrument 10 % / Exposure 75 % nachgelagert wirksam.
    """
    return (cash_pct > cash_max_pct
            and str(regime).upper() in DEPLOYMENT_BOOST_REGIMES
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
    
        from bot.core.market_hours import (
            is_market_open, resolve_market_fields as _resolve_mf,
        )
        from bot.core.regime import get_regime_params
        from bot.core.regime import apply_config as apply_regime_config
        from bot.core.risk import apply_config, check_buy_gate, get_score_boost
        apply_config(cfg)  # fix/risk-config-wiring: Limits/Schwellen aus config.yaml
        # fix/regime-min-conviction-config (2026-08-28): regime.apply_config lief
        # nur im risk_worker (Regime-ERKENNUNG). get_min_conviction() unten liest
        # aber _REGIME_PARAMS in DIESEM Prozess — ohne diesen Aufruf haette der
        # Config-Wert regime.min_conviction keinerlei Wirkung.
        apply_regime_config(cfg)
        # fix/diversity-mixed-cap + fix/deployment-boost-regimes (2026-08-28):
        # beide lesen Modul-Konstanten DIESES Moduls — ohne die Aufrufe waeren
        # die Config-Werte wirkungslos (vgl. Kommentar zu apply_regime_config).
        apply_diversity_config(cfg)
        apply_deployment_config(cfg)
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
            _api_key = os.environ.get("ETORO_BOT_API_KEY", "")
            _user_key = os.environ.get("ETORO_BOT_USER_KEY", "")
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
            # Auch OHNE Kaufsignal posten (feat/signal-report 2026-08-29).
            # Genau das ist der haeufigste Fall am Wochenende und nachts, und
            # genau da will man sehen, dass der Lauf stattgefunden hat und was
            # im Pool lag — bisher endete der Worker hier stumm.
            try:
                _post(
                    'post_signal_worker_embed',
                    approved_trades=[],
                    regime=regime,
                    risk_scalar=risk_scalar,
                    evaluated_count=0,
                    equity=state_repo.get_equity(),
                    cash=state_repo.get_float("AVAILABLE_CASH", 0.0),
                    total_exposure=portfolio_repo.get_total_exposure(),
                    position_count=portfolio_repo.get_position_count(),
                    signal_report=_build_signal_report(db, all_signals),
                )
            except Exception:
                logger.debug("Signalbericht-Post fehlgeschlagen", exc_info=True)
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
    
        def _resolve_market_fields(instrument_id: int) -> tuple[str, str]:
            """yfinance_symbol + market_hours-Kategorie fuer den Market-Check.
            Ohne yf_symbol wuerde z.B. ein Forex-Symbol (EURJPY) als US-Aktie
            eingestuft und faelschlich an US-Boersenzeiten gebunden.

            Duennes Adapter um market_hours.resolve_market_fields() — das
            Signal-Symbol steht hier schon fest, gebraucht werden nur die
            beiden Zusatzfelder."""
            _mf = _resolve_mf(db, instrument_id)
            return (_mf[1], _mf[2]) if _mf else ("", "")

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
        # feat/eligible-counters (2026-08-24): Der Filter verschluckte 17 von 18
        # frischen Signalen, ohne zu sagen woran. Ohne diese Zaehler bleibt nur
        # Raten — pro Lauf steht jetzt in einer Zeile, welcher Zweig wie viele
        # Kandidaten aussortiert hat, mit Beispielsymbolen.
        from collections import defaultdict as _dd
        _skip: dict[str, list[str]] = _dd(list)

        # feat/commodity (2026-08-24): Rohstoffe sind ein bewusst kleines
        # Experiment — max. 1 Position, feste Groesse. Es gibt bisher KEINE
        # verwertbare Evidenz (6 geschlossene Trades, alle exakt 0.0 %), das
        # Limit haelt das Risiko klein und sammelt trotzdem Datenpunkte.
        # feat/rebuy-cooldown (2026-08-24): Nachkauf desselben Instruments erst
        # nach N Stunden. Grund: LEG.DE, IBE.MC, MAU.PA und 6753.T wurden je
        # zwei- bis dreimal gekauft, immer EXAKT einen 15-Minuten-Zyklus
        # auseinander. Die bestehende Sperre (get_approved_instrument_ids)
        # deckt nur status='APPROVED' ab — "Instrumente, die auf Ausfuehrung
        # warten". Die Bestaetigung erfolgt aber rund 3 Minuten nach der
        # Freigabe, der naechste Zyklus kommt nach 15: das Fenster der Sperre
        # ist zu diesem Zeitpunkt immer schon geschlossen, und eine ACTIVE
        # Position blockiert nichts.
        #
        # Bewusst zeitbasiert statt "nur eine Position je Instrument":
        # Nachkaufen soll erlaubt bleiben, nur nicht im Minutentakt.
        #
        # created_at ist UTC (approved_at dagegen lokal — die beiden NICHT
        # mischen, sonst verrutscht der Vergleich um zwei Stunden).
        _rebuy_h = float((cfg.get("trading", {}) or {}).get("rebuy_cooldown_hours", 6.0))
        _recent_buys: set[int] = set()
        if _rebuy_h > 0:
            try:
                _recent_buys = {
                    r["instrument_id"] for r in db.fetchall(
                        "SELECT DISTINCT instrument_id FROM trades "
                        "WHERE status IN ('APPROVED','SUBMITTING','ACTIVE') "
                        "AND created_at > datetime('now', ?)",
                        (f"-{_rebuy_h} hours",),
                    )
                }
            except Exception:
                _recent_buys = set()

        _comm_cfg = ((cfg.get("trading", {}) or {}).get("commodity", {}) or {})
        _comm_ids: set[int] = set()
        _comm_open = 0
        try:
            _comm_ids = {
                r["instrument_id"] for r in db.fetchall(
                    "SELECT instrument_id FROM instruments WHERE asset_class = 'commodity'")
            }
            _comm_open = len(db.fetchall(
                "SELECT p.instrument_id FROM portfolio_snapshot p "
                "JOIN instruments i ON i.instrument_id = p.instrument_id "
                "WHERE i.asset_class = 'commodity'"))
        except Exception:
            _comm_ids, _comm_open = set(), 0
    
        # APPROVED-Check: Instrumente mit bereits APPROVED-Trade vorab laden
        _approved_ids: set[int] = set()
        try:
            _approved_ids = trade_repo.get_approved_instrument_ids()
        except Exception:
            _approved_ids = set()  # fail-open wenn Methode fehlt
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
                # fix (2026-08-24): hier fehlte das continue — ein gesperrtes
                # Instrument wurde als REJECTED markiert, lief aber weiter durch
                # den Filter und konnte trotzdem im eligible-Pool landen.
                _skip["ghost_blacklist"].append(str(instrument_id))
                continue

            # APPROVED-Check: kein neues Signal fuer Instrument mit
            # bereits APPROVED-Trade (fix/duplicate-instrument-approval 2026-07-27)
            # Vorher: execution_worker markierte Duplikate als REJECTED,
            # aber signal_worker generierte sie trotzdem — 83/176 REJECTED.
            if instrument_id in _approved_ids:
                logger.info(
                    "SignalWorker: instrument_id %d hat bereits APPROVED-Trade — SKIP",
                    instrument_id,
                )
                signal_repo.update_signal_status(signal_id, "REJECTED")
                _skip["bereits_approved"].append(str(instrument_id))
                continue
    
            symbol = _resolve_symbol(instrument_id)

            # feat/rebuy-cooldown: frisch gekauft -> kein Nachkauf.
            # Skip statt REJECT: nach Ablauf der Sperrfrist ist das Signal
            # sofort wieder Kandidat, sofern es dann noch gilt.
            if instrument_id in _recent_buys:
                _skip["nachkauf_cooldown"].append(symbol)
                continue

            # feat/commodity: hoechstens N Rohstoffpositionen gleichzeitig.
            # Skip statt REJECT — schliesst die offene Position, ist das
            # Signal sofort wieder Kandidat.
            if instrument_id in _comm_ids:
                if not _comm_cfg.get("enabled", False):
                    _skip["commodity_aus"].append(symbol)
                    continue
                if _comm_open >= int(_comm_cfg.get("max_positions", 1)):
                    _skip["commodity_limit"].append(symbol)
                    continue

            if _is_llm_ghost_blocked(symbol, _llm_blacklist):
                logger.info("SignalWorker: %s LLM-Exchange-Blacklist", symbol)
                signal_repo.update_signal_status(signal_id, "REJECTED")
                _skip["llm_exchange_blacklist"].append(symbol)
                continue

            # LLM Signal-Type Blacklist (deaktivierte Signal-Typen)
            _sig_type = signal.get("signal_type", "")
            _sig_skip, _sig_reason = _is_signal_type_skipped(_sig_type, _llm_signal_weights)
            if _sig_skip:
                logger.info("SignalWorker: %s Signal-Typ gesperrt (%s): %s",
                            symbol, _sig_type[:40], _sig_reason[:60])
                signal_repo.update_signal_status(signal_id, "REJECTED")
                _skip["llm_signaltyp_gesperrt"].append(symbol)
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
                    _skip["signaltyp_cooldown"].append(symbol)
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
                _skip["slippage_blacklist"].append(symbol)
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
                    and _max_fraction_for(_pre_cat) < 1.0
                    and _open_signal_cats.get(_pre_cat, 0) / position_count
                        >= _max_fraction_for(_pre_cat)):
                skipped_diversity.append(f"{symbol}({_pre_cat})")
                _skip["diversity_kappe"].append(symbol)
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
                _skip["news_avoid"].append(symbol)
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
                _skip["markt_geschlossen"].append(f"{symbol}[{_mh_category}]")
                continue

            eligible.append((signal, symbol))
    
        # feat/eligible-counters: eine Zeile pro Lauf, warum aussortiert wurde.
        _in = len(buy_signals)
        _out = len(eligible)
        if _in:
            _parts = " ".join(
                f"{k}={len(v)}" for k, v in sorted(_skip.items(), key=lambda kv: -len(kv[1]))
            ) or "keine"
            logger.info(
                "SignalWorker: eligible-Filter %d Signale -> %d Kandidaten | %s",
                _in, _out, _parts,
            )
            for _k, _v in sorted(_skip.items(), key=lambda kv: -len(kv[1]))[:4]:
                logger.info("SignalWorker:   %s (%d): %s", _k, len(_v), ", ".join(_v[:8]))
            try:
                log_repo.write(
                    "INFO", "signal_worker",
                    f"eligible-Filter: {_in} -> {_out} | {_parts}",
                    {"skip_counts": {k: len(v) for k, v in _skip.items()}},
                )
            except Exception:
                pass

        if skipped_diversity:
            logger.info(
                "SignalWorker: %d Kandidat(en) am Diversity-Precheck uebersprungen "
                "(Kategorie an 45%%-Kappe, Signal bleibt FRESH): %s",
                len(skipped_diversity), ", ".join(skipped_diversity[:6]),
            )

        # Sort by boosted score descending — only among OPEN, non-blacklisted
        # signals. get_score_boost gewichtet nach Anlageklasse; die Deckel
        # selbst bleiben davon unberuehrt (ASSET_CLASS_LIMITS in risk.py
        # greift weiter unten am Gate).
        #
        # ACHTUNG (2026-08-29): Hier stand bis heute, der Boost bevorzuge
        # Aktien/ETFs GEGENUEBER Krypto. Das stimmt seit dem 2026-08-24 nicht
        # mehr — CRYPTO wurde von 0.85 auf 1.15 gehoben und liegt damit
        # gleichauf mit Aktien (DEFAULT_STOCK_SCORE_BOOST 1.15). Der veraltete
        # Kommentar hat die Ursachensuche zum Wochenend-Stillstand zunaechst in
        # die falsche Richtung geschickt: die Vermutung "Krypto wird
        # wegsortiert" war seit fuenf Tagen nicht mehr zutreffend. Gemessen
        # entstehen am Wochenende 5.5 Krypto-Kaufsignale pro Tag gegen 4.9
        # werktags — die Klasse laeuft, sie ist nur klein.
        #
        # feat/liquidity-tiering (2026-07-26): fuenfter Term im Sort-Key —
        # Market-Cap/ADV-Tier-Faktor [0.6..1.1] aus instruments. High-Runner
        # gewinnen die knappen Slots, Micro-Caps werden nachrangig sortiert
        # (nicht geblockt). Unbekannt = 1.0 neutral, fail-open.
        _liquidity_map: dict[int, float] = {}
        if bool(cfg.get("trading", {}).get("liquidity_tiering", True)):
            try:
                from bot.core.liquidity import load_liquidity_map
                _liquidity_map = load_liquidity_map(
                    db, [s["instrument_id"] for s, _ in eligible]
                )
            except Exception:
                _liquidity_map = {}
        eligible.sort(
            key=lambda t: (
                float(t[0].get("score", 0))
                * get_score_boost(t[1])
                * _get_signal_score_multiplier(t[0].get("signal_type", ""), _llm_signal_weights)
                * _signal_age_factor(t[0].get("generated_at", ""), ttl_minutes=1440)
                * _liquidity_map.get(t[0]["instrument_id"], 1.0)
                * _signal_performance_decay(
                    t[0].get("signal_type", ""), db_path
                )
            ),
            reverse=True,
        )
        _dampened = {
            sym: f for (s, sym) in eligible
            if (f := _liquidity_map.get(s["instrument_id"], 1.0)) < 1.0
        }
        if _dampened:
            logger.info(
                "SignalWorker: Liquidity-Tiering daempft %d Kandidat(en): %s",
                len(_dampened),
                ", ".join(f"{sym}={f:.2f}" for sym, f in list(_dampened.items())[:8]),
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
        # 2026-08-24: Basis-Slots konfigurierbar (war fest 3).
        # KORREKTUR 2026-08-25: Die urspruengliche Begruendung war falsch. Sie
        # lautete, 97-99 % der Signale liefen ungenutzt in die TTL, weil pro
        # Zyklus nur 3 Kandidaten geprueft wuerden — der Durchsatz sei der
        # bindende Engpass. Nachgemessen: von 930 Signalen in drei Tagen waren
        # 897 VERKAUFSsignale (791x BB_UPPER_RSI_OVERBOUGHT). Der signal_worker
        # ist der Kauf-Pfad und verwirft sie regulaer; nur 33 waren ueberhaupt
        # kauffaehig. Ein Zyklus mit freien Slots protokollierte evaluated=1 —
        # die Slots banden also nicht. Es gibt schlicht wenige Kaufgelegenheiten.
        # Der Wert 5 steht damit OHNE belegte Grundlage; er ist vermutlich
        # wirkungslos (alle nachgelagerten Gates gelten unveraendert pro
        # Kandidat), aber niemand hat ihn nach der Widerlegung neu begruendet.
        # Wer ihn anfasst: erst zaehlen, wie viele Kandidaten pro Zyklus
        # tatsaechlich anstehen, dann entscheiden.
        _base_slots = int(cfg.get("trading", {}).get("candidate_slots", 5))
        candidates = unique_candidates[:max(1, _base_slots)]
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

        # feat/sector-backfill (2026-08-12): Sektor-Map fuer das Asset-Class-Gate.
        # ASSET_CLASS_MAP deckt ~65 US-Ticker ab; gemessen fielen 74.2% des
        # Equity fail-open durch das Gate. instruments.sector (yfinance,
        # befuellt von scripts/sync_instrument_sectors.py) schliesst die Luecke.
        # BEWUSST per Default AUS: erst wenn der Backfill durch ist und die
        # Sektor-Verteilung des Buchs gemessen wurde, ist ein 20%-Cap eine
        # informierte Entscheidung statt eines Blindflugs.
        _sector_map: dict[str, str] = {}
        if bool((cfg.get("sector_limits", {}) or {}).get("enforce_db_sectors", False)):
            try:
                _sector_map = {
                    str(r["symbol"]).upper(): str(r["sector"])
                    for r in (signal_repo.db.fetchall(
                        "SELECT symbol, sector FROM instruments "
                        "WHERE sector IS NOT NULL AND sector != '' AND sector != 'unknown'"
                    ) or [])
                }
                logger.info("SignalWorker: Sektor-Map aktiv (%d Instrumente)", len(_sector_map))
            except Exception as _sec_exc:
                # Fail-open: fehlt die Spalte oder kippt die Query, verhaelt
                # sich das Gate wie vor dem Backfill.
                logger.warning("SignalWorker: Sektor-Map nicht ladbar (%s) — Gate fail-open", _sec_exc)
                _sector_map = {}

        # feat/region-damper (2026-08-12): market_region ist bereits gepflegt,
        # es braucht keinen Backfill. Fail-open wie die Sektor-Map.
        _region_by_symbol: dict[str, str] = {}
        try:
            _region_by_symbol = {
                str(r["symbol"]).upper(): str(r["market_region"])
                for r in (signal_repo.db.fetchall(
                    "SELECT symbol, market_region FROM instruments "
                    "WHERE market_region IS NOT NULL AND market_region != ''"
                ) or [])
            }
        except Exception as _reg_exc:
            logger.warning("SignalWorker: Regionen-Map nicht ladbar (%s) — Damper aus", _reg_exc)
            _region_by_symbol = {}
    
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
            # feat/sizing-trace (2026-08-28): Herleitung pro Trade mitschreiben.
            # Der Embed zeigte nur den Endbetrag; die Faktoren lagen verstreut im
            # Log. Reines Protokollieren — die Rechnung selbst bleibt unangetastet.
            _trace = [f"Basis {conviction} {pct:.1f}% x ${equity:,.0f} "
                      f"x {buy_aggressiveness:.2f} = ${buy_amount:,.2f}"]

            # Kelly: dynamische Groessenkorrektur basierend auf Signal-Performance (Prio 1)
            # Risk-neutral Scale (fix/kelly-risk-neutral 2026-08-22): gewichtetes
            # Mittel ~0.30 (Kalibrierung an 243 Closed Trades) — das Risiko-Niveau
            # des Kontos bleibt beim getesteten Wert. Proven Lossbringer konnen bis
            # kelly_min_factor (default 0.15) runterskaliert werden, Proven Edges
            # steigen ueber den Base-Wert. Params: config.yaml sizing.kelly_*.
            try:
                from bot.core.sizing import kelly_size_factor
                _k = kelly_size_factor(
                    signal.get("signal_type", ""),
                    db,
                )
                if _k != 1.0:
                    _old_amt = buy_amount
                    buy_amount = round(buy_amount * _k, 2)
                    logger.info(
                        "SignalWorker: Kelly: signal_type=%s k=%.2f amount $%.2f->$%.2f",
                        signal.get("signal_type", ""), _k, _old_amt, buy_amount,
                    )
                    _trace.append(f"Kelly x{_k:.2f} = ${buy_amount:,.2f}")
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
                _trace.append(f"News CAUTION x0.50 = ${buy_amount:,.2f}")

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
                    _trace.append(f"Deployment-Boost x{_boost:.2f} = ${buy_amount:,.2f}")
            except Exception as _db_exc:
                logger.debug("SignalWorker: Deployment-Boost uebersprungen: %s", _db_exc)

            # feat/entry-quality PHASE 2 (2026-08-24): mode-abhaengige Wirkung.
            # Die Evaluation (mit vollen Indikatoren) wurde am Signal-Gen in
            # entry_quality_events erfasst (data_worker); hier wird der
            # kombinierte Sizing-Multiplikator fuer das konkrete Signal gelesen.
            #   mode=live   -> buy_amount wird skaliert, Event als applied=1
            #                  markiert. Nur Soft-Gates (Floor min_size_mult),
            #                  keine Hard-Blocks — der Trade findet statt, nur
            #                  kleiner. Das haelt die Datensammlung am Laufen.
            #   mode=shadow -> nur would-be-Logging, keine Aenderung.
            # Laeuft VOR dem Signal-Floor, damit ein herunterskalierter
            # Betrag unter dem Minimum regulaer aussortiert wird.
            # Fail-open: bei jedem Fehler bleibt buy_amount unveraendert.
            #
            # apply_sizing hebt auf den SIGNAL-Floor an (regimeabhaengig), nicht
            # auf den Dust-Floor: das ist ein Boden fuer die BASISgroesse. Die
            # ATR-Risk-Parity darf danach bis SIZING_PARITY_FLOOR weiter nach
            # unten skalieren — aus "auf 100 $ angehoben" werden also 60 $, und
            # das ist beabsichtigt: hoher ATR heisst kleiner, nicht gar nicht.
            # Der Dust-Floor (e0) faengt den Endbetrag ab und ist genau so
            # abgeleitet, dass er diesen Korridor nicht zuschnuert.
            # Gemessen ab 2026-07-26 sind die kleinen Positionen die profitablen
            # (< 100 $: n=65, WR 44.6 %, +29.23 USD), deshalb NICHT "reparieren".
            try:
                from bot.core import entry_quality as _eq
                _eq_mode = str(
                    ((cfg.get("trading", {}) or {}).get("entry_quality", {}) or {}).get("mode", "shadow")
                ).lower()
                _eq_sm = _eq.latest_size_mult(db, signal_id)
                if _eq_sm != 1.0:
                    _eq_new_amt = round(buy_amount * _eq_sm, 2)
                    if _eq_mode == "live":
                        _eq_new_amt, _eq_floored = _eq.apply_sizing(
                            buy_amount, _eq_sm,
                            float(regime_params.get("signal_floor_usd", 50.0)),
                        )
                        _mb = float(regime_params.get("signal_floor_usd", 50.0))
                        logger.info(
                            "SignalWorker: ENTRY-QUALITY %s (live): size_mult=%.2f "
                            "$%.2f -> $%.2f%s",
                            symbol, _eq_sm, buy_amount, _eq_new_amt,
                            (f" [Basisgroesse auf Signal-Floor ${_mb:.0f} "
                             f"angehoben — ATR-Risk-Parity darf bis zum "
                             f"Dust-Floor darunter skalieren]")
                            if _eq_floored else "",
                        )
                        buy_amount = _eq_new_amt
                        _trace.append(
                            f"Entry-Quality x{_eq_sm:.2f}"
                            + (f" -> Signal-Floor ${_mb:,.0f}" if _eq_floored else "")
                            + f" = ${buy_amount:,.2f}"
                        )
                        _eq.mark_applied(db, signal_id)
                    else:
                        logger.info(
                            "SignalWorker: ENTRY-QUALITY %s (shadow): size_mult=%.2f "
                            "$%.2f -> $%.2f%s",
                            symbol, _eq_sm, buy_amount, _eq_new_amt,
                            " WOULD-BLOCK" if _eq_sm <= 0.0 else "",
                        )
            except Exception:
                logger.debug("entry_quality: sizing fehlgeschlagen (fail-open)", exc_info=True)

            # feat/commodity: feste Margin statt Sizing-Kette. Die Kette
            # wuerde fuer einen Rohstoff rund 150 USD ergeben — zu wenig, um
            # mit Hebel 2 die 1000-USD-Mindest-Exposure zu erreichen. Fest
            # bedeutet zugleich: keine Kelly-/Regime-Skalierung nach oben,
            # der Einsatz ist auf genau diesen Betrag begrenzt.
            if signal.get("instrument_id") in _comm_ids and _comm_cfg.get("enabled", False):
                _comm_amt = float(_comm_cfg.get("position_usd", 500.0))
                if _comm_amt != buy_amount:
                    logger.info(
                        "SignalWorker: %s Rohstoff — feste Groesse $%.2f statt $%.2f "
                        "(Hebel %s, Exposure $%.2f)",
                        symbol, _comm_amt, buy_amount,
                        _comm_cfg.get("leverage", 2),
                        _comm_amt * float(_comm_cfg.get("leverage", 2)),
                    )
                    buy_amount = _comm_amt

            # max_trade_pct-Klammer (2026-08-29): der execution_worker
            # verwirft in DEFENSIVE jeden Trade ueber max_trade_pct des Equity
            # (regime.py; DEFENSIVE 3.0 %). Der signal_worker kannte diese
            # Grenze bisher nicht und konnte Betraege genehmigen, die dort
            # sicher sterben — verbrannter Kandidaten-Slot und ein Trade, der
            # zweimal im Log auftaucht.
            #
            # Latent seit 076e661 (2026-08-28), das den Deployment-Boost auf
            # CAUTION/DEFENSIVE ausweitete: 5.0 % x 0.5 x 1.25 = 261.15 USD
            # gegen einen Deckel von 250.71 USD. Bisher 0 Faelle in trades,
            # weil der Boost enge Bedingungen hat und der Bot kaum handelte.
            # Mit der Basis auf 6.0 % liegt sie exakt AUF dem Deckel, jeder
            # Boost reisst ihn also.
            #
            # Klammern statt verwerfen: die Groesse wird auf das Erlaubte
            # gestutzt, der Trade findet statt.
            #
            # BEWUSST nur in DEFENSIVE — die Klammer spiegelt exakt den Guard
            # im execution_worker (dort `if regime == "DEFENSIVE"`). In
            # NORMAL/CAUTION ist max_trade_pct nicht durchgesetzt; sie auch
            # dort anzuwenden waere keine Spiegelung, sondern eine neue
            # Beschraenkung — und sie wuerde die 6-%-Basis (501.41 USD) sofort
            # wieder auf 417.84 stutzen (NORMAL max_trade_pct 5.0 %), also
            # genau das aufheben, wofuer sie eingefuehrt wurde.
            #
            # Dass conviction_pct 6.0 und NORMAL max_trade_pct 5.0 einander
            # widersprechen, bleibt damit offen und ist eine eigene
            # Entscheidung: entweder max_trade_pct nachziehen oder die Basis
            # senken. Nicht hier nebenbei mitentscheiden.
            _mt_pct = float(regime_params.get("max_trade_pct", 100.0))
            if regime == "DEFENSIVE" and _mt_pct > 0 and equity > 0:
                _mt_cap = round(equity * _mt_pct / 100.0, 2)
                if buy_amount > _mt_cap:
                    logger.info(
                        "SignalWorker: %s auf max_trade_pct geklammert "
                        "$%.2f -> $%.2f (%.1f%% von $%.2f, Regime %s)",
                        symbol, buy_amount, _mt_cap, _mt_pct, equity, regime,
                    )
                    _trace.append(
                        f"max_trade_pct {_mt_pct:.1f}% = ${_mt_cap:,.2f}")
                    buy_amount = _mt_cap

            # SIGNAL-FLOOR (P1): "lohnt sich dieses Signal ueberhaupt?"
            # Regimeabhaengig, geprueft VOR den situativen Haircuts
            # (ATR-Risk-Parity, Korrelation, Region). Eine Groesse, die schon
            # hier zu klein ist, traegt die These nicht — unabhaengig davon,
            # was die Haircuts spaeter noch machen.
            signal_floor = float(regime_params.get("signal_floor_usd", 50.0))
            # DUST-FLOOR: Broker-Oekonomie, gilt fuer den ENDbetrag (siehe e0).
            dust_floor = _dust_floor_usd(
                regime, float(cfg.get("trading", {}).get("min_buy_usd", 50.0))
            )
            if buy_amount < signal_floor:
                # fix/min-buy-slot-leak (2026-07-14): vorher nur `continue` ohne
                # Status-Update — das Signal blieb FRESH und belegte JEDEN
                # Zyklus erneut einen Kandidaten-Slot bis zum 24h-TTL (Kelly
                # 0.3x oder CAUTION-Halbierung aendern sich innerhalb des TTL
                # nicht). REJECT gibt den Slot frei.
                _reject_below_floor(
                    log_repo, signal_repo, blocked_reasons,
                    symbol=symbol, signal_id=signal_id, amount=buy_amount,
                    floor=signal_floor, kind="SIGNAL_FLOOR", stage="vor Haircuts",
                    detail="Kelly/News/Regime",
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
                    _parity = max(_sl_default / _sl_pct_final, _PARITY_FLOOR)
                    buy_amount = round(buy_amount * _parity, 2)
                    _trace.append(
                        f"ATR-Risk-Parity x{_parity:.2f} (SL {_sl_pct_final:.2f}% "
                        f"statt {_sl_default:.2f}%) = ${buy_amount:,.2f}"
                    )
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
                    _trace.append(f"Korrelation x{corr_factor:.2f} = ${buy_amount:,.2f}")
                    # Gegen den DUST-Floor, nicht gegen den Signal-Floor: der
                    # Korrelations-Haircut ist als "halbieren statt blocken"
                    # gemeint. Gegen den Regime-Floor geprueft wirkte er
                    # faktisch als Block, und zwar haerter als die
                    # ATR-Risk-Parity, die gar nicht geprueft wurde — derselbe
                    # Endbetrag wurde also akzeptiert oder verworfen, je
                    # nachdem WELCHER Daempfer ihn erzeugt hatte.
                    if buy_amount < dust_floor:
                        _reject_below_floor(
                            log_repo, signal_repo, blocked_reasons,
                            symbol=symbol, signal_id=signal_id, amount=buy_amount,
                            floor=dust_floor, kind="DUST_FLOOR",
                            stage="nach Korrelation", detail=corr_reason,
                        )
                        continue

                # Regionen-Damper (feat/region-damper 2026-08-12): die reale
                # Klumpenlage des Buchs ist geografisch (EU 34.8%, ASIA_CN
                # 18.0% des Equity), gemessen hat das bisher kein Gate.
                # Bewusst Damper statt Block: EU ist die Haupt-Signalquelle,
                # ein harter Cap darunter wuerde sie abschalten. Ueber dem
                # Soft-Cap schrumpft die Groesse, der Bot baut den Klumpen
                # also handelnd ab statt stillzustehen.
                if _region_by_symbol and equity > 0:
                    _my_region = _region_by_symbol.get(symbol.upper())
                    if _my_region:
                        _region_usd = sum(
                            p["amount_usd"] for p in open_positions
                            if _region_by_symbol.get(p.get("symbol", "").upper()) == _my_region
                        )
                        from bot.core.risk import region_size_factor
                        _rf, _rreason = region_size_factor(_region_usd / equity * 100.0)
                        if _rf == 0.0:
                            logger.info("SignalWorker: %s %s — geblockt", symbol, _rreason)
                            signal_repo.update_signal_status(signal_id, "REJECTED")
                            blocked_reasons.append(f"{symbol}: {_rreason}")
                            continue
                        if _rf < 1.0:
                            _before = buy_amount
                            buy_amount = round(buy_amount * _rf, 2)
                            _trace.append(f"Region x{_rf:.2f} = ${buy_amount:,.2f}")
                            logger.info(
                                "SignalWorker: %s Groesse $%.2f → $%.2f — %s",
                                symbol, _before, buy_amount, _rreason,
                            )
                            if buy_amount < dust_floor:
                                _reject_below_floor(
                                    log_repo, signal_repo, blocked_reasons,
                                    symbol=symbol, signal_id=signal_id,
                                    amount=buy_amount, floor=dust_floor,
                                    kind="DUST_FLOOR", stage="nach Region",
                                    detail=_rreason,
                                )
                                continue

                # Diversity-Gate (Prio 4): max 45% offener Positionen in einer Kategorie
                _sig_cat = _get_signal_category(signal.get("signal_type", ""))
                _cat_cap = _max_fraction_for(_sig_cat)
                if _sig_cat != "UNKNOWN" and position_count > 0 and _cat_cap < 1.0:
                    _cat_n = _open_signal_cats.get(_sig_cat, 0)
                    if _cat_n / position_count >= _cat_cap:
                        logger.info(
                            "SignalWorker: Diversity-Gate: %s (%s) %d/%d Pos. (%.0f%%>=%.0f%%) -- geblockt",
                            _sig_cat,
                            signal.get("signal_type", ""),
                            _cat_n,
                            position_count,
                            _cat_n / position_count * 100,
                            _cat_cap * 100,
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

                # e0. DUST-FLOOR, letzte Instanz vor der Order.
                # Bis 2026-08-28 endete die Kette ohne Pruefung des ENDbetrags
                # gegen einen regimebewussten Boden: der Regime-Floor lief bei
                # P1 (vor den Haircuts), danach griff nur noch das globale
                # check_min_buy_gate (config trading.min_buy_usd, 50 $).
                # Trade #1750 DOM.ST wurde so am 28.08. 12:33 mit 79.00 $ in
                # DEFENSIVE genehmigt (Regime-Floor 100 $): Kelly 209->131.66,
                # ATR-Risk-Parity x0.60 -> 79.00, und 79 > 50 passierte.
                # Diese Pruefung steht bewusst NACH allen Multiplikatoren —
                # jeder kuenftige Daempfer laeuft automatisch dagegen.
                if buy_amount < dust_floor:
                    _reject_below_floor(
                        log_repo, signal_repo, blocked_reasons,
                        symbol=symbol, signal_id=signal_id, amount=buy_amount,
                        floor=dust_floor, kind="DUST_FLOOR",
                        stage="Kettenende", detail="; ".join(_trace[-2:]),
                    )
                    continue

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
                    "sizing_trace": list(_trace),
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
    
        # ── 5b. Core-Sweep: entkoppelt vom illiquiden Small-Cap-
        # Signalfluss. Plant IMMER (Dry-Log); setzt nur bei core_sweep.enabled=true
        # um — ueber dieselbe create->APPROVED->execution-Bahn wie normale Signale
        # (erbt SL-Clamp, Market-Open-Guard, Ghost-Order-Pipeline). Fail-open:
        # ein Fehler hier darf den regulaeren Signallauf nie kippen.
        try:
            from bot.core.core_sweep import plan_core_sweep, is_enabled as _cs_enabled
            _held_ids = set()
            for _p in open_positions_raw:
                try:
                    _held_ids.add(int(_p.get("instrument_id")))
                except (TypeError, ValueError):
                    pass
            # fix/core-sweep-duplicate-approval (2026-07-29): _held_ids kannte
            # bisher NUR offene Positionen. Ein Instrument mit bereits
            # APPROVED-Trade wurde deshalb jeden 15-min-Zyklus erneut
            # eingeplant, und der execution_worker verwarf es als
            # "Duplicate instrument_id in same execution batch" — 143 von 143
            # Duplikat-Rejects der letzten 3 Tage stammten aus CORE_SWEEP.
            # Der normale Signalpfad hat diesen Guard seit
            # fix/duplicate-instrument-approval (2026-07-27) bereits.
            # BEWUSST frisch abgefragt statt _approved_ids (Zeile ~654)
            # wiederzuverwenden: das Set stammt von VOR der Signal-Schleife,
            # die selbst Trades anlegt — und die Core-Sweep-Whitelist wird aus
            # genau denselben starken FRESH-Signalen gefuellt, ein Instrument
            # in beiden Pfaden ist also der Normalfall, nicht die Ausnahme.
            try:
                _held_ids |= trade_repo.get_approved_instrument_ids(
                    ("APPROVED", "SUBMITTING")
                )
            except Exception:
                pass  # fail-open: execution_worker-Guard bleibt letzte Linie
            _wl = (cfg.get("trading", {}).get("core_sweep", {}) or {}).get("whitelist", {}) or {}
            _wl_ids = []
            for _v in _wl.values():
                try:
                    _wl_ids.append(int(_v))
                except (TypeError, ValueError):
                    pass
            _atr_by_id, _rsi_by_id = {}, {}
            if _wl_ids:
                _ph = ",".join("?" for _ in _wl_ids)
                for _r in (signal_repo.db.fetchall(
                        f"SELECT instrument_id, atr_pct FROM instruments "
                        f"WHERE instrument_id IN ({_ph})", tuple(_wl_ids)) or []):
                    if _r["atr_pct"] is not None:
                        _atr_by_id[int(_r["instrument_id"])] = float(_r["atr_pct"])
                for _r in (signal_repo.db.fetchall(
                        f"SELECT instrument_id, MAX(generated_at) AS g, rsi FROM signals "
                        f"WHERE instrument_id IN ({_ph}) AND rsi IS NOT NULL "
                        f"GROUP BY instrument_id", tuple(_wl_ids)) or []):
                    if _r["rsi"] is not None:
                        _rsi_by_id[int(_r["instrument_id"])] = float(_r["rsi"])
            # fix/core-sweep-portfolio-gates (2026-08-12): Core-Sweep sieht ab
            # jetzt dieselben Portfolio-Grenzen wie der regulaere Signalpfad.
            # total_exposure ist hier bereits um die in dieser Schleife
            # approbierten Buys hochgezaehlt (Zeile ~1295) — der Sweep plant
            # also gegen den Stand NACH den Signal-Trades, nicht davor.
            from bot.core.risk import MAX_TOTAL_EXPOSURE_PCT as _cs_max_exp
            from bot.core.correlation import check_correlation_gate as _cs_corr
            _sweep_orders, _sweep_reasons = plan_core_sweep(
                cfg, equity=equity, cash=cash_estimate, regime=regime,
                held_instrument_ids=_held_ids, atr_by_id=_atr_by_id, rsi_by_id=_rsi_by_id,
                db=signal_repo.db,
                total_exposed=total_exposure,
                max_exposure_pct=_cs_max_exp,
                open_positions=open_positions,
                correlation_gate=_cs_corr,
            )
            if _sweep_reasons:
                logger.info("SignalWorker: %s", _sweep_reasons[0])
            _cs_live = _cs_enabled(cfg)
            _cs_news_skipped: list[str] = []
            for _o in _sweep_orders:
                # feat/core-sweep-news (2026-08-24): News-Flags gelten auch hier.
                # Der Sweep lief bisher an ihnen vorbei — am 24.08. kaufte er
                # NVDA (AVOID: Earnings am 26.08.) und JNJ (CAUTION: Talc-
                # Rechtsrisiko) fuer je 162.30 USD, waehrend derselbe Titel im
                # Signal-Pfad blockiert worden waere.
                # Begruendung fuer die Ausnahme war stets "kein Signal-Trade" —
                # das traegt bei Kelly und Veto, aber nicht hier: Earnings in
                # zwei Tagen sind ein EREIGNISrisiko und betreffen jeden Kauf,
                # unabhaengig vom Pfad.
                _cs_nf = _news_flags.get(_o.symbol) or {}
                if _cs_nf.get("flag") == "AVOID":
                    _cs_news_skipped.append(_o.symbol)
                    logger.info(
                        "SignalWorker: Core-Sweep %s uebersprungen — News-Flag AVOID (%s)",
                        _o.symbol, (_cs_nf.get("reason") or "")[:70],
                    )
                    continue
                if not _cs_live:
                    logger.info(
                        "SignalWorker: [DRY] Core-Sweep wuerde $%.2f in %s (id=%s) deployen",
                        _o.amount_usd, _o.symbol, _o.instrument_id)
                    log_repo.write("INFO", "signal_worker",
                                   f"[DRY] Core-Sweep: ${_o.amount_usd:.2f} {_o.symbol}")
                    continue
                from bot.core.risk import adaptive_sl_pct as _cs_adaptive
                _cs_sl = _cs_adaptive(
                    float(cfg.get("sl", {}).get("default_pct", 3.0)),
                    _atr_by_id.get(_o.instrument_id),
                    multiple=float(cfg.get("sl", {}).get("atr_multiple", 1.5)),
                    max_pct=float(cfg.get("sl", {}).get("max_pct", 6.0)),
                )
                # feat/core-sweep-signal-tag (2026-07-26): synthetisches
                # CONSUMED-Signal 'CORE_SWEEP' statt signal_id=None — vorher
                # waren Core-Sweep-Trades fuer Scorecard, Kelly und jede
                # trades-JOIN-signals-Analyse unsichtbar (Cash-Deployment-
                # Pfad hatte keine Lernschleife).
                _cs_sig_id = None
                try:
                    _cs_sig_id = signal_repo.create(
                        instrument_id=_o.instrument_id,
                        signal_type="CORE_SWEEP",
                        conviction="MEDIUM",
                        score=0.0,
                        rsi=_rsi_by_id.get(_o.instrument_id),
                        ttl_minutes=5,
                    )
                    signal_repo.update_signal_status(_cs_sig_id, "CONSUMED")
                except Exception:
                    _cs_sig_id = None
                # feat/entry-quality (2026-08-22) SHADOW-Modus: Core-Sweep-
                # Regime-Gate loggen + entry_quality_events recorden, OHNE die
                # Order zu aendern. Volle Indikatoren sind hier nicht
                # verfuegbar (Sweep plant gegen _atr_by_id/_rsi_by_id) — das
                # core_sweep_regime-Gate braucht nur das Regime; der
                # Trend-Override faellt fail-open aus (keine Daten = kein
                # Override). Basis der Phase-1-Shadow-Auswertung
                # (CORE_SWEEP-Regime-Druck: -$171 Drag).
                # PHASE 2: _cs_amt traegt den ggf. herunterskalierten Betrag.
                # Wird vor dem Gate gesetzt, damit ein Fehler im Gate den
                # urspruenglichen Betrag unveraendert laesst (fail-open).
                _cs_amt = _o.amount_usd

                # feat/core-sweep-news: CAUTION halbiert, wie im Signal-Pfad.
                if _cs_nf.get("flag") == "CAUTION":
                    _cs_amt = round(_cs_amt * 0.5, 2)
                    logger.info(
                        "SignalWorker: Core-Sweep %s News-Flag CAUTION — Groesse "
                        "halbiert auf $%.2f (%s)",
                        _o.symbol, _cs_amt, (_cs_nf.get("reason") or "")[:60],
                    )

                # feat/core-sweep-kelly (2026-08-24): Der Sweep war der EINZIGE
                # Kaufpfad ohne Kelly-Skalierung — er bekam implizit Faktor 1.0,
                # waehrend jeder Signal-Trade nach seinem gemessenen Edge
                # dimensioniert wurde. Gemessen an 73 geschlossenen Sweep-Trades:
                # WR 32.9 %, avg -0.99 %, -171 USD Ergebnis — bei der GROESSTEN
                # Durchschnittsgroesse im ganzen Bestand (234 USD gegen 67 USD
                # bei den besten Signalen). Der Sweep hat mit n=73 zugleich die
                # belastbarste Stichprobe ueberhaupt, die Schrumpfung greift hier
                # also am wenigsten. Fail-open: Fehler laesst den Betrag stehen.
                try:
                    from bot.core.sizing import kelly_size_factor as _cs_ksf
                    from bot.core import entry_quality as _cs_eq
                    _cs_kf = _cs_ksf("CORE_SWEEP", db)
                    if _cs_kf < 1.0:
                        _cs_amt, _cs_floored = _cs_eq.apply_sizing(
                            _cs_amt, _cs_kf,
                            float(regime_params.get("signal_floor_usd", 50.0)),
                        )
                        logger.info(
                            "SignalWorker: CORE-SWEEP KELLY %s: factor=%.3f "
                            "$%.2f -> $%.2f%s",
                            _o.symbol, _cs_kf, _o.amount_usd, _cs_amt,
                            " [auf Signal-Floor angehoben]" if _cs_floored else "",
                        )
                except Exception:
                    logger.debug("core-sweep kelly fehlgeschlagen (fail-open)",
                                 exc_info=True)
                try:
                    from bot.core import entry_quality as _eq
                    _eq_ev_cs = _eq.evaluate(
                        cfg, symbol=_o.symbol, signal_type="CORE_SWEEP",
                        indicators={}, regime=regime or "NORMAL",
                        is_core_sweep=True,
                    )
                    _eq_mode = str(
                        ((cfg.get("trading", {}) or {}).get("entry_quality", {}) or {}).get("mode", "shadow")
                    ).lower()
                    _eq_cs_applied = bool(_eq_ev_cs.hits) and _eq_mode == "live"
                    _eq.ensure_table(db)
                    _eq.record(
                        db, _eq_ev_cs, mode=_eq_mode, applied=_eq_cs_applied,
                        signal_id=_cs_sig_id, instrument_id=_o.instrument_id,
                        is_core_sweep=True,
                    )
                    if _eq_ev_cs.hits:
                        if _eq_cs_applied:
                            _cs_amt, _cs_floored = _eq.apply_sizing(
                                _cs_amt, _eq_ev_cs.size_mult,
                                float(regime_params.get("signal_floor_usd", 50.0)),
                            )
                            logger.info(
                                "SignalWorker: ENTRY-QUALITY CORE_SWEEP %s (live, %s): %s "
                                "-> size_mult=%.2f $%.2f -> $%.2f%s",
                                _o.symbol, regime, _eq_ev_cs.reasons,
                                _eq_ev_cs.size_mult, _o.amount_usd, _cs_amt,
                                " [auf Signal-Floor angehoben]" if _cs_floored else "",
                            )
                        else:
                            logger.info(
                                "SignalWorker: ENTRY-QUALITY CORE_SWEEP %s (shadow, %s): %s%s",
                                _o.symbol, regime, _eq_ev_cs.reasons,
                                " WOULD-BLOCK" if _eq_ev_cs.blocked else "",
                            )
                except Exception:
                    logger.debug("entry_quality: core-sweep gate fehlgeschlagen (fail-open)", exc_info=True)
                _cs_tid = trade_repo.create(
                    instrument_id=_o.instrument_id, symbol=_o.symbol, direction="BUY",
                    amount_usd=_cs_amt, stop_loss_pct=_cs_sl,
                    signal_id=_cs_sig_id, signal_price=None,
                )
                from datetime import datetime as _csdt, timezone as _cstz
                trade_repo.update_status(
                    _cs_tid, "APPROVED",
                    approved_at=_csdt.now(_cstz.utc).strftime("%Y-%m-%d %H:%M:%S"))
                approved_count += 1
                cash_estimate -= _cs_amt
                # fix/core-sweep-portfolio-gates: Exposure mitfuehren wie im
                # Signalpfad (Zeile ~1295), damit spaetere Leser im selben Lauf
                # den Stand INKL. Sweep sehen.
                total_exposure += _cs_amt
                position_count += 1
                _held_ids.add(_o.instrument_id)
                approved_trades_info.append({
                    "symbol": _o.symbol, "amount_usd": _cs_amt,
                    "signal_type": "CORE_SWEEP", "conviction": "CORE",
                    "score": 0.0, "signal_price": None,
                })
                logger.info(
                    "SignalWorker: CORE-SWEEP APPROVED #%d — %s $%.2f (SL %.2f%%)",
                    _cs_tid, _o.symbol, _cs_amt, _cs_sl)
                log_repo.write(
                    "INFO", "signal_worker",
                    f"Core-Sweep APPROVED: {_o.symbol} BUY ${_cs_amt:.2f}",
                    {"trade_id": _cs_tid, "instrument_id": _o.instrument_id})
            if _cs_news_skipped:
                logger.info(
                    "SignalWorker: Core-Sweep — %d Titel wegen News-Flag AVOID "
                    "uebersprungen: %s",
                    len(_cs_news_skipped), ", ".join(_cs_news_skipped[:8]),
                )
                log_repo.write(
                    "INFO", "signal_worker",
                    f"Core-Sweep: {len(_cs_news_skipped)} Titel wegen News-AVOID uebersprungen",
                    {"symbols": _cs_news_skipped[:12]},
                )
        except Exception as _cs_exc:
            logger.warning("SignalWorker: Core-Sweep-Pass uebersprungen: %s", _cs_exc)

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
    
        # ── 7. Discord summary (immer, feat/signal-report 2026-08-29) ─────────
        # Vorher gab es drei Zweige: Embed nur bei approved > 0, sonst
        # gedrosselte Alert-Embeds ("All markets closed" 1x/6h, "All signals
        # blocked" 1x/Std) — und bei 0 ausgewerteten Signalen gar nichts.
        # Damit war die haeufigste Frage nicht zu beantworten: WELCHE Signale
        # hat der Lauf gesehen und was ist daraus geworden? Jetzt geht immer
        # ein Post raus, mit Kaufsignalen gruen und Verkaufssignalen rot.
        # Die Drossel-Zustaende SIGNAL_CLOSED_POSTED_AT / SIGNAL_BLOCKED_POSTED_AT
        # werden nicht mehr geschrieben; sie bleiben harmlos in system_state
        # stehen (kein Leser mehr).
        try:
            _report = _build_signal_report(
                db, all_signals,
                approved_syms={t_["symbol"] for t_ in approved_trades_info},
                blocked_reasons=blocked_reasons,
                skip_map=_skip,
                candidate_ids={s_.get("id") for s_, _ in candidates},
            )
        except Exception:
            logger.debug("Signalbericht fehlgeschlagen (fail-open)", exc_info=True)
            _report = []

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
            signal_report=_report,
        )
    
    
if __name__ == "__main__":
    main()
