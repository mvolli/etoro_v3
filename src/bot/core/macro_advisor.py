"""macro_advisor.py — Forward-Makro-Pass (LLM-Dämpfungsfaktor 0.5–1.0).

fix/macro-fold (2026-07-15): extrahiert aus macro_regime_worker.py. Der Pass
läuft jetzt huckepack im stündlichen news_flags_worker mit ALTERS-TRIGGER
(LLM_MACRO_SET_AT fehlt oder >23h) statt eigenem Täglich-08:00-Cron —
selbstheilend: ein verpasster Lauf wird im nächsten Stundenzyklus nachgeholt
(vorher: 24h-Loch bis zum nächsten Kalendertag, fail-open auf 1.0 via TTL).

Semantik unverändert: NUR dämpfend [0.5..1.0], hart geclampt; das regel-
basierte Regime (CURRENT_REGIME/RISK_SCALAR) bleibt unangetastet; Konsument
signal_worker fällt bei fehlendem/stalem Wert (TTL 26h) auf 1.0 zurück.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LLM_TIMEOUT_S = 60.0
SCALAR_MIN, SCALAR_MAX = 0.5, 1.0
REFRESH_AGE_HOURS = 23.0   # Alters-Trigger für den huckepack-Aufruf

_SRC_DIR = Path(__file__).resolve().parent.parent.parent  # .../src


def _clamp_scalar(value) -> float:
    """Harte Grenze [0.5, 1.0] — das LLM kann dämpfen, nie boosten.
    Unparsbar → 1.0 (neutral, fail-open)."""
    try:
        return max(SCALAR_MIN, min(SCALAR_MAX, float(value)))
    except (TypeError, ValueError):
        return 1.0


def macro_scalar_age_hours(state_repo) -> float | None:
    """Alter des letzten Makro-Passes in Stunden. None = nie gelaufen/unparsbar."""
    try:
        raw = state_repo.get("LLM_MACRO_SET_AT") or ""
        if not raw:
            return None
        at = datetime.fromisoformat(raw)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - at).total_seconds() / 3600.0
    except Exception:
        return None


def _fetch_macro() -> dict:
    """SPY/QQQ 1d+5d, VIX Level + 5d-Delta."""
    ctx = {"spy_1d": None, "spy_5d": None, "qqq_1d": None, "qqq_5d": None,
           "vix": None, "vix_5d_delta": None}
    try:
        import yfinance as yf
        data = yf.download(["SPY", "QQQ", "^VIX"], period="10d", interval="1d",
                           group_by="ticker", auto_adjust=True, progress=False, threads=True)

        def _series(t):
            try:
                return data[t]["Close"].dropna()
            except Exception:
                return None

        for prefix, t in (("spy", "SPY"), ("qqq", "QQQ")):
            c = _series(t)
            if c is not None and len(c) >= 2:
                ctx[f"{prefix}_1d"] = round(float(c.iloc[-1] / c.iloc[-2] - 1) * 100, 2)
            if c is not None and len(c) >= 6:
                ctx[f"{prefix}_5d"] = round(float(c.iloc[-1] / c.iloc[-6] - 1) * 100, 2)
        v = _series("^VIX")
        if v is not None and len(v) >= 1:
            ctx["vix"] = round(float(v.iloc[-1]), 2)
        if v is not None and len(v) >= 6:
            ctx["vix_5d_delta"] = round(float(v.iloc[-1] - v.iloc[-6]), 2)
    except Exception as exc:
        logger.warning("[macro_advisor] Makro-Daten fehlgeschlagen: %s", exc)
    return ctx


def run_macro_pass(state_repo) -> bool:
    """Führt einen kompletten Makro-Pass aus (Fetch → LLM → Clamp → State).

    Returns True, wenn LLM_MACRO_* aktualisiert wurde; False bei Daten-/
    LLM-Ausfall (dann KEIN State-Update — der Konsument fällt via 26h-TTL
    auf neutral 1.0 zurück)."""
    from bot.core.llm_client import call_llm_json

    macro = _fetch_macro()
    if macro["vix"] is None and macro["spy_1d"] is None:
        logger.info("[macro_advisor] keine Makro-Daten — kein Update (TTL-Fallback 1.0)")
        return False

    regime = state_repo.get("CURRENT_REGIME") or "?"
    dd = state_repo.get("DRAWDOWN_PCT") or "?"

    # Fear&Greed-Index (alternative.me, gratis/ohne Auth — OSS-Fund 2026-07-16)
    try:
        import json as _json
        import urllib.request as _url
        with _url.urlopen("https://api.alternative.me/fng/?limit=1", timeout=5) as _r:
            _fng = _json.loads(_r.read())["data"][0]
        macro["fear_greed"] = f"{_fng['value']} ({_fng['value_classification']})"
    except Exception:
        macro["fear_greed"] = "?"

    prompt = f"""/no_think
Du bist Makro-Risiko-Advisor fuer einen autonomen Trading-Bot (nur Long-BUYs,
Mean-Reversion + Trend-Following, Haltedauer Tage). Bewerte das Marktumfeld
fuer die NAECHSTEN 1-2 Handelstage und gib einen Positionsgroessen-Faktor:

- 1.0  = normales Umfeld, volle Groesse
- 0.85 = leicht erhoehtes Risiko
- 0.7  = deutlich erhoehtes Risiko (VIX-Spike, breiter Abverkauf im Gang)
- 0.5  = akutes Stressumfeld (nur noch halbe Groesse)

Du kannst NUR daempfen (max 1.0). Sei nicht schreckhaft: normale Schwankungen
(SPY +-1%, VIX < 20) sind KEIN Grund unter 1.0 zu gehen.

## Daten
SPY: {macro['spy_1d']}% (1d), {macro['spy_5d']}% (5d)
QQQ: {macro['qqq_1d']}% (1d), {macro['qqq_5d']}% (5d)
VIX: {macro['vix']} (5d-Delta: {macro['vix_5d_delta'] if macro['vix_5d_delta'] is not None else '?'})
Crypto Fear&Greed: {macro['fear_greed']}
Bot-Regime (rueckwaertsgewandt): {regime}, Drawdown {dd}%

Antworte NUR mit JSON:
{{"macro_scalar": 1.0, "assessment": "1-2 Saetze, deutsch"}}"""

    result = call_llm_json(prompt, max_tokens=256, temperature=0.1,
                           timeout_s=LLM_TIMEOUT_S, label="macro_advisor")
    if result is None:
        logger.warning("[macro_advisor] LLM nicht verfuegbar — kein Update (TTL-Fallback 1.0)")
        return False

    scalar = _clamp_scalar(result.get("macro_scalar"))
    reason = str(result.get("assessment", ""))[:250]
    state_repo.set("LLM_MACRO_SCALAR", str(scalar))
    state_repo.set("LLM_MACRO_SET_AT", datetime.now(timezone.utc).isoformat())
    state_repo.set("LLM_MACRO_REASON", reason)
    logger.info("[macro_advisor] scalar=%s — %s", scalar, reason[:120])

    if scalar < 1.0:
        try:
            _bot_dir = str(_SRC_DIR / "bot")
            if _bot_dir not in sys.path:
                sys.path.insert(0, _bot_dir)
            import discord_embeds as _DE
            _DE.post_alert_embed(
                title=f"🌤️ Makro-Advisor: Positionsgroessen auf {int(scalar*100)}% gedaempft",
                description=f"{reason}\n\nSPY {macro['spy_1d']}%/1d {macro['spy_5d']}%/5d | "
                            f"VIX {macro['vix']} ({macro['vix_5d_delta']}/5d)\nTTL: 26h",
                severity="INFO",
            )
        except Exception:
            pass
    return True
