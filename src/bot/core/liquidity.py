"""Liquidity-Tiering — Market-Cap/ADV-basierte Priorisierung (feat/liquidity-tiering).

Problem (Audit 2026-07-26): Im gesamten System existierte kein Market-Cap-
oder Liquiditaetsdatum. Ein LSE-Micro-Cap mit 15% Spread und NVDA bekamen im
Kandidaten-Ranking denselben Score-Boost (1.15 flat via get_score_boost) —
die Slippage-Blacklist griff erst NACHDEM 3 Trades in 7 Tagen verbrannt
waren (dokumentiert: 7 ACTIVE vs. 145 FAILED/REJECTED in einer Woche).

Loesung: instruments bekommt adv_usd (Average Dollar Volume, 20d) und
market_cap (USD-Naeherung). Aus beiden wird ein Tier-Faktor [0.6..1.1]
berechnet, der als vierter Term in den Ranking-Sort-Key des signal_worker
eingeht — High-Runner gewinnen die knappen Trade-Slots, Micro-Caps werden
nachrangig, ohne hart ausgeschlossen zu sein (das erledigt das ADV-Gate im
data_worker fuer die wirklich illiquiden Faelle).

Datenquellen:
- adv_usd: data_worker persistiert es im 5-Minuten-Zyklus aus dem ohnehin
  gefetchten OHLCV-DataFrame (20d Volumen-Mittel x Preis x FX-Naeherung);
  discovery_worker persistiert es beim Kandidaten-Store.
- market_cap: scripts/backfill_liquidity.py via yfinance fast_info
  (Batch-Backfill + Refresh, Cron-faehig).

FX-Faktoren sind bewusst GROBE Naeherungen — sie dienen nur dem Bucketing
in Tier-Grenzen (300M/2B/10B bzw. 1M/5M/50M USD), nicht der Bewertung.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("liquidity")

# ── Schwellen ────────────────────────────────────────────────────────────────

# Unterhalb dieses 20d-Dollar-Volumens speichert der data_worker keine
# BUY-Signale mehr (gleiche Groessenordnung wie discovery MIN_VOLUME_USD).
MIN_ADV_USD = 500_000.0

# Market-Cap-Tiers (USD-Naeherung)
MC_MEGA = 10e9    # >= 10B  -> leichter Boost
MC_MID = 2e9      # >= 2B   -> neutral
MC_SMALL = 300e6  # >= 300M -> gedaempft; darunter Micro-Cap -> stark gedaempft

# ADV-Tiers (USD-Naeherung, 20d-Mittel)
ADV_DEEP = 50e6   # >= 50M  -> leichter Boost
ADV_OK = 5e6      # >= 5M   -> neutral
ADV_THIN = 1e6    # >= 1M   -> gedaempft; darunter -> stark gedaempft

# Faktor-Grenzen: nie unter 0.6 (Signal bleibt sichtbar, verliert nur den
# Slot-Wettbewerb), nie ueber 1.1 (kein Runaway-Boost auf den Asset-Boost).
FACTOR_FLOOR = 0.6
FACTOR_CAP = 1.1

# ── FX-Naeherungen fuer Nicht-USD-Boersen (nur fuers Tier-Bucketing) ─────────
# .L ist in GBX (Pence): 0.01 GBP x ~1.27 USD/GBP.
_FX_APPROX: dict[str, float] = {
    ".L": 0.0127,
    ".DE": 1.08, ".F": 1.08, ".PA": 1.08, ".AS": 1.08, ".MI": 1.08,
    ".MC": 1.08, ".BR": 1.08, ".HE": 1.08, ".VI": 1.08, ".LS": 1.08,
    ".IR": 1.08,
    ".SW": 1.10,
    ".ST": 0.095, ".OL": 0.095,
    ".CO": 0.145,
    ".T": 0.0067,
    ".HK": 0.128,
    ".AX": 0.66, ".NZ": 0.60,
    ".TO": 0.73, ".V": 0.73,
    ".SI": 0.74,
    ".KS": 0.00072, ".KQ": 0.00072,
    ".TW": 0.031, ".TWO": 0.031,
    ".WA": 0.25,
}


def currency_factor(symbol: str) -> float:
    """Grobe FX-Naeherung Boersenwaehrung -> USD anhand des Yahoo-Suffix.

    Default 1.0 (US-Symbole ohne Suffix, USD-notierte Kryptos etc.).
    """
    if not symbol or "." not in symbol:
        return 1.0
    suffix = "." + symbol.rsplit(".", 1)[-1].upper()
    return _FX_APPROX.get(suffix, 1.0)


def is_quote_currency_volume(yf_symbol: Optional[str]) -> bool:
    """Meldet yfinance das Volumen fuer dieses Symbol bereits in der Quote-Waehrung?

    Gilt fuer Krypto-Paare (BTC-USD, ADA-USD, ...). Dort ist "Volume" der
    Dollar-Umsatz, nicht die Stueckzahl.
    """
    return bool(yf_symbol) and yf_symbol.upper().endswith("-USD")


def compute_adv_usd(df: Any, symbol: str, price: Optional[float] = None,
                    yf_symbol: Optional[str] = None) -> Optional[float]:
    """20d-Durchschnitts-Dollar-Volumen aus einem OHLCV-DataFrame (yfinance).

    Gibt None zurueck, wenn Volumen-Daten fehlen/kaputt sind — der Aufrufer
    entscheidet, ob unknown neutral behandelt wird (Ranking) oder blockt.

    fix/crypto-adv-double-count (2026-08-28): Bei Krypto-Paaren (…-USD) meldet
    yfinance das Volumen BEREITS in Dollar. Es mit dem Preis zu multiplizieren
    zaehlt ihn doppelt. Nachgerechnet an den Rohdaten: BTC-USD hatte ein
    avg_Volume von 31,5 Mrd bei einem Preis von 79.821 $ — als Stueckzahl
    gelesen waeren das mehr Bitcoin pro Tag als je existieren werden.
    adv_usd stand dadurch bei 2,5 Billiarden statt 31,5 Mrd.

    Die Verzerrung ging in beide Richtungen: Preis > 1 blaeht auf, Preis < 1
    drueckt herunter. Guenstige Altcoins (ADA bei 0,21 $ -> Faktor 4,8 zu
    klein) fielen dadurch unter MIN_ADV_USD und ihre BUY-Signale wurden als
    illiquide verworfen, obwohl sie es nicht sind.

    *yf_symbol* ist das yfinance-Symbol; *symbol* bleibt das Boersen-/eToro-
    Symbol fuer currency_factor. Fehlt yf_symbol, wird auf symbol
    zurueckgegriffen — das deckt Aufrufer ab, die ohnehin yf-Namen uebergeben.
    """
    try:
        vol = df["Volume"].dropna()
        if len(vol) == 0:
            return None
        avg_vol = float(vol.iloc[-20:].mean())
        if price is None:
            closes = df["Close"].dropna()
            if len(closes) == 0:
                return None
            price = float(closes.iloc[-1])
        if is_quote_currency_volume(yf_symbol or symbol):
            adv = avg_vol
        else:
            adv = avg_vol * float(price) * currency_factor(symbol)
        if adv <= 0:
            return None
        return adv
    except Exception:
        return None


def liquidity_factor(market_cap: Optional[float], adv_usd: Optional[float]) -> float:
    """Tier-Faktor [0.6..1.1] aus Market-Cap und/oder ADV.

    Unbekannt (beide None) -> 1.0 neutral: der Bestand waechst organisch
    (data_worker schreibt ADV im 5-Minuten-Takt), ein Unbekannt-Malus wuerde
    am ersten Tag das gesamte Nicht-US-Universum pauschal abwerten.
    Sind beide bekannt, gewinnt der SCHLECHTERE Wert (min) — ein Mega-Cap
    mit ausgetrocknetem Handelsvolumen ist ein Liquiditaetsrisiko, ein
    Micro-Cap mit einem Volumen-Spike bleibt ein Micro-Cap.
    """
    mc_f: Optional[float] = None
    if market_cap is not None and market_cap > 0:
        if market_cap >= MC_MEGA:
            mc_f = 1.1
        elif market_cap >= MC_MID:
            mc_f = 1.0
        elif market_cap >= MC_SMALL:
            mc_f = 0.85
        else:
            mc_f = 0.65

    adv_f: Optional[float] = None
    if adv_usd is not None and adv_usd > 0:
        if adv_usd >= ADV_DEEP:
            adv_f = 1.05
        elif adv_usd >= ADV_OK:
            adv_f = 1.0
        elif adv_usd >= ADV_THIN:
            adv_f = 0.9
        else:
            adv_f = 0.7

    if mc_f is None and adv_f is None:
        return 1.0
    if mc_f is None:
        result = adv_f
    elif adv_f is None:
        result = mc_f
    else:
        result = min(mc_f, adv_f)
    return max(FACTOR_FLOOR, min(FACTOR_CAP, float(result)))


# ── DB-Persistenz ────────────────────────────────────────────────────────────

_COLUMNS_READY = False


def ensure_liquidity_columns(db: Any) -> None:
    """Lazy-Migration (idempotent, Muster _ensure_instrument_atr_columns)."""
    global _COLUMNS_READY
    if _COLUMNS_READY:
        return
    for ddl in (
        "ALTER TABLE instruments ADD COLUMN adv_usd REAL",
        "ALTER TABLE instruments ADD COLUMN adv_updated_at TEXT",
        "ALTER TABLE instruments ADD COLUMN market_cap REAL",
        "ALTER TABLE instruments ADD COLUMN market_cap_updated_at TEXT",
    ):
        try:
            db.execute(ddl)
        except Exception:
            pass  # Spalte existiert bereits
    _COLUMNS_READY = True


def update_adv(db: Any, instrument_id: int, adv_usd: float) -> None:
    """Persistiert das 20d-Dollar-Volumen fuer *instrument_id*."""
    ensure_liquidity_columns(db)
    try:
        db.execute(
            "UPDATE instruments SET adv_usd = ?, adv_updated_at = datetime('now') "
            "WHERE instrument_id = ?",
            (float(adv_usd), instrument_id),
        )
    except Exception as exc:
        logger.debug("update_adv(%s) failed: %s", instrument_id, exc)


def update_market_cap(db: Any, instrument_id: int, market_cap_usd: float) -> None:
    """Persistiert die Market-Cap (USD-Naeherung) fuer *instrument_id*."""
    ensure_liquidity_columns(db)
    try:
        db.execute(
            "UPDATE instruments SET market_cap = ?, market_cap_updated_at = datetime('now') "
            "WHERE instrument_id = ?",
            (float(market_cap_usd), instrument_id),
        )
    except Exception as exc:
        logger.debug("update_market_cap(%s) failed: %s", instrument_id, exc)


def load_liquidity_map(db: Any, instrument_ids: list[int]) -> dict[int, float]:
    """Laedt {instrument_id: liquidity_factor} fuer die uebergebenen IDs.

    Fail-open auf 1.0: fehlt die Spalte oder scheitert die Query, wird
    neutral gerankt (Ranking-Feature darf den Signalfluss nie stoppen).
    """
    if not instrument_ids:
        return {}
    try:
        ensure_liquidity_columns(db)
        placeholders = ",".join("?" for _ in instrument_ids)
        rows = db.fetchall(
            f"SELECT instrument_id, market_cap, adv_usd FROM instruments "
            f"WHERE instrument_id IN ({placeholders})",
            tuple(instrument_ids),
        )
        return {
            row[0]: liquidity_factor(row[1], row[2])
            for row in rows
        }
    except Exception as exc:
        logger.debug("load_liquidity_map failed: %s", exc)
        return {}
