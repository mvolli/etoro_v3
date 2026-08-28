#!/usr/bin/env python3
"""
scripts/expand_crypto_commodity_watchlist.py

Erweitert die Krypto- und Rohstoff-Watchlist um geprüfte, liquide Kandidaten.

Hintergrund (2026-08-28): Die Watchlist enthielt 323 Aktien, aber nur 17 Krypto
und 21 Rohstoffe. Krypto ist nach geschlossenen Trades die beste Anlageklasse
(n=11, WR 63,6 %, +66,88 USD gegen Aktien n=276, WR 33,7 %, -471,07 USD) und
bekam die geringste Aufmerksamkeit. Vorsicht: n=11 trägt keine starke Aussage —
deshalb wird hier nur die Beobachtungsfläche vergrößert, nichts umgewichtet.

Warum die 693 Krypto-Instrumente brachlagen — eine Henne-Ei-Schleife:
  sync_instrument_catalog.py legt Neuzugänge mit is_active=0, is_tradable=NULL an
  sync_instrument_tradability.py prüft aber nur WHERE is_active = 1
  → Neuzugänge werden nie geprüft und können sich nie qualifizieren.
Dieses Script durchbricht sie, indem es die Handelbarkeit selbst abfragt, bevor
es etwas aktiviert.

Prüfkette pro Kandidat — nur wer ALLE Stufen besteht, wird aufgenommen:
  1. eToro-Eligibility (allowOpenPosition) — nicht handelbar → raus
  2. yfinance liefert überhaupt Kursdaten unter <SYMBOL>-USD → sonst raus
  3. Mindesthistorie für die Indikatoren (SMA20/MACD/BB brauchen Vorlauf)
  4. Liquidität: 20d-Durchschnittsumsatz >= --min-adv
Erst danach: is_active=1, yfinance_symbol setzen, Watchlist-Eintrag anlegen.

WICHTIG zur Liquidität: yfinance meldet bei Krypto-Paaren (…-USD) das Volumen
bereits in Dollar. Es mit dem Preis zu multiplizieren zählt ihn doppelt — genau
der Fehler, den fix/crypto-adv-double-count behoben hat. Dieses Script nutzt
bot.core.liquidity.compute_adv_usd und erbt die Korrektur.

Idempotent: bereits gelistete Instrumente werden übersprungen, ein zweiter Lauf
ändert nichts. Default ist ein Trockenlauf; Schreiben nur mit --apply.

Beispiele:
  PYTHONPATH=src python3 scripts/expand_crypto_commodity_watchlist.py
  PYTHONPATH=src python3 scripts/expand_crypto_commodity_watchlist.py --apply
  PYTHONPATH=src python3 scripts/expand_crypto_commodity_watchlist.py \\
      --asset-class crypto --min-adv 10000000 --max-add 30 --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("watchlist_expand")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

BATCH_SIZE = 100          # IDs pro Eligibility-Request (API-Maximum)
SLEEP_BETWEEN_BATCHES = 3.0
YF_CHUNK = 25             # Symbole pro yfinance-Download
SLEEP_BETWEEN_YF = 1.0

# Kategorien, unter denen die neuen Einträge laufen. Bewusst eigene Namen:
# so bleibt im Nachhinein erkennbar, was aus dieser Erweiterung stammt.
CATEGORY = {"crypto": "crypto.expanded", "commodity": "commodities.expanded"}


def _load_env() -> None:
    # Die Worker laufen ueber ~/.hermes/scripts/v3_*.sh, die
    # /home/mvolli/.hermes/.env sourcen — im Repo liegt keine .env.
    # Beide Orte probieren, damit das Script auch eigenstaendig laeuft.
    kandidaten = [PROJECT_ROOT / ".env", Path.home() / ".hermes" / ".env"]
    env_file = next((p for p in kandidaten if p.exists()), None)
    if env_file is None:
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _candidates(db, asset_class: str) -> list[dict]:
    """Instrumente der Klasse, die noch nicht auf der Watchlist stehen."""
    rows = db.fetchall(
        """
        SELECT i.instrument_id, i.symbol, i.name, i.yfinance_symbol,
               i.is_active, i.is_tradable, i.min_position_amount
        FROM instruments i
        WHERE i.asset_class = ?
          AND i.instrument_id NOT IN (
              SELECT instrument_id FROM watchlist WHERE instrument_id IS NOT NULL
          )
        ORDER BY i.symbol
        """,
        (asset_class,),
    )
    return [dict(r) for r in rows]


def _yf_symbol_for(row: dict, asset_class: str) -> str | None:
    """yfinance-Ticker bestimmen. Krypto folgt der Konvention <SYMBOL>-USD."""
    if row.get("yfinance_symbol"):
        return str(row["yfinance_symbol"])
    if asset_class != "crypto":
        return None          # Rohstoffe ohne hinterlegtes Symbol raten wir nicht
    sym = (row.get("symbol") or "").strip().upper()
    if not sym or not sym.replace("-", "").replace(".", "").isalnum():
        return None
    return sym if sym.endswith("-USD") else f"{sym}-USD"


def _check_tradability(client, rows: list[dict]) -> dict[int, bool]:
    """eToro-Eligibility batchweise. API-Fehler → Kandidat gilt als ungeprüft."""
    ergebnis: dict[int, bool] = {}
    ids = [r["instrument_id"] for r in rows]
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start:start + BATCH_SIZE]
        try:
            resp = client.post(
                "/trading/info/eligibility",
                {"instrumentIds": chunk, "currency": "USD"},
                v2=True,
            )
        except Exception as exc:
            logger.warning("Eligibility-Fehler (Batch %d): %s — übersprungen",
                           start // BATCH_SIZE + 1, exc)
            time.sleep(SLEEP_BETWEEN_BATCHES)
            continue
        for e in resp.get("eligibilities", []):
            iid = e.get("instrumentId")
            if iid is not None:
                ergebnis[int(iid)] = bool(e.get("allowOpenPosition", False))
        for iid in resp.get("notFoundInstrumentIds", []):
            ergebnis[int(iid)] = False
        logger.info("  Eligibility Batch %d/%d: %d Antworten",
                    start // BATCH_SIZE + 1,
                    (len(ids) + BATCH_SIZE - 1) // BATCH_SIZE, len(ergebnis))
        if start + BATCH_SIZE < len(ids):
            time.sleep(SLEEP_BETWEEN_BATCHES)
    return ergebnis


def _measure_liquidity(paare: list[tuple[dict, str]], min_history: int) -> dict[int, dict]:
    """ADV und Historienlänge je Kandidat über yfinance."""
    import yfinance as yf
    from bot.core import liquidity as liq

    out: dict[int, dict] = {}
    for start in range(0, len(paare), YF_CHUNK):
        chunk = paare[start:start + YF_CHUNK]
        tickers = " ".join(y for _, y in chunk)
        try:
            data = yf.download(tickers, period="6mo", interval="1d",
                               group_by="ticker", progress=False,
                               auto_adjust=False, threads=True)
        except Exception as exc:
            logger.warning("yfinance-Fehler (Block %d): %s", start // YF_CHUNK + 1, exc)
            time.sleep(SLEEP_BETWEEN_YF)
            continue

        for row, yfs in chunk:
            try:
                df = data[yfs] if len(chunk) > 1 else data
                df = df.dropna(subset=["Close"])
            except Exception:
                continue
            if df is None or len(df) < min_history:
                out[row["instrument_id"]] = {"kerzen": 0 if df is None else len(df),
                                             "adv": None}
                continue
            adv = liq.compute_adv_usd(df, row["symbol"], yf_symbol=yfs)
            # Relative Streuung als Stablecoin-Erkennung: USDC & Co. stehen
            # konstant bei 1,00 und liefern nur Rauschen statt Umkehrpunkten.
            # Datengetrieben statt Namensliste — neue Stablecoins fallen
            # dadurch von selbst durch.
            closes = df["Close"].astype(float)
            mittel = float(closes.mean())
            vola = float(closes.std() / mittel) if mittel > 0 else 0.0
            out[row["instrument_id"]] = {"kerzen": len(df), "adv": adv,
                                         "preis": float(closes.iloc[-1]),
                                         "vola": vola}
        logger.info("  yfinance %d/%d Symbole gemessen",
                    min(start + YF_CHUNK, len(paare)), len(paare))
        if start + YF_CHUNK < len(paare):
            time.sleep(SLEEP_BETWEEN_YF)
    return out


def _apply(db, treffer: list[dict], asset_class: str) -> int:
    """Aktivieren, yfinance-Symbol setzen, Watchlist-Eintrag anlegen. Idempotent."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    kat = CATEGORY[asset_class]
    n = 0
    for t in treffer:
        vorhanden = db.fetchone(
            "SELECT 1 FROM watchlist WHERE instrument_id = ?", (t["instrument_id"],)
        )
        if vorhanden:
            continue
        db.execute(
            "UPDATE instruments SET is_active = 1, is_tradable = 1, "
            "yfinance_symbol = ?, tradability_checked_at = ? WHERE instrument_id = ?",
            (t["yf"], now_iso, t["instrument_id"]),
        )
        db.execute(
            "INSERT INTO watchlist (symbol, instrument_id, category, added_at) "
            "VALUES (?, ?, ?, ?)",
            (t["symbol"], t["instrument_id"], kat, now_iso),
        )
        n += 1
    return n


def _verarbeite(db, client, asset_class: str, args) -> list[dict]:
    logger.info("=== %s ===", asset_class.upper())
    kandidaten = _candidates(db, asset_class)
    logger.info("Kandidaten (noch nicht auf der Watchlist): %d", len(kandidaten))
    if not kandidaten:
        return []

    paare = [(r, y) for r in kandidaten if (y := _yf_symbol_for(r, asset_class))]
    logger.info("davon mit brauchbarem yfinance-Ticker: %d", len(paare))
    if not paare:
        return []

    if args.limit_probe:
        paare = paare[: args.limit_probe]
        logger.info("auf %d begrenzt (--limit-probe)", len(paare))

    logger.info("Stufe 1 — Handelbarkeit bei eToro ...")
    elig = _check_tradability(client, [r for r, _ in paare])
    handelbar = [(r, y) for r, y in paare if elig.get(r["instrument_id"]) is True]
    logger.info("  handelbar: %d von %d (ungeprüft/abgelehnt: %d)",
                len(handelbar), len(paare), len(paare) - len(handelbar))
    if not handelbar:
        return []

    logger.info("Stufe 2+3+4 — Kursdaten, Historie, Liquidität ...")
    messung = _measure_liquidity(handelbar, args.min_history)

    treffer = []
    ohne_daten = zu_kurz = zu_illiquide = stabil = doppelt = falsch_klassifiziert = 0
    gesehene_ticker: dict[str, str] = {}
    for r, y in handelbar:
        # Falsch klassifizierte Krypto-Futures: BHCA.FUT, BITA.FUT und
        # BTIA.FUT zeigen alle drei auf BTC-USD, DGOA.FUT auf DOGE-USD usw.
        # Als Rohstoff aufgenommen wuerden daraus mehrere "verschiedene"
        # Positionen, die in Wahrheit dasselbe Underlying halten — die
        # Korrelations- und Klumpen-Gates greifen dagegen nicht, weil sie
        # nach Symbol arbeiten.
        if asset_class == "commodity" and y.upper().endswith("-USD"):
            # Zwei verschiedene Faelle, beide unbrauchbar:
            #   echt falsch klassifiziert: XLRA.FUT -> XLM-USD, DGOA.FUT ->
            #     DOGE-USD, BHCA/BITA/BTIA.FUT -> alle drei BTC-USD. Als
            #     Rohstoff aufgenommen entstuenden mehrere "verschiedene"
            #     Positionen auf dasselbe Underlying; die Korrelations- und
            #     Klumpen-Gates greifen nicht, weil sie nach Symbol arbeiten.
            #   beschaedigter Eintrag: OIL.MICRO -> "MICRO WT-USD",
            #     STEEL.FUT -> "STEEL (E-USD" — abgeschnittene NAMEN mit
            #     angehaengtem -USD, keine gueltigen Ticker.
            beschaedigt = any(ch in y for ch in " ()")
            logger.info("  %s -> %s: %s — uebersprungen", r["symbol"], y,
                        "beschaedigtes yfinance_symbol" if beschaedigt
                        else "Krypto-Paar unter asset_class=commodity")
            falsch_klassifiziert += 1
            continue
        # Gueltige yfinance-Ticker haben weder Leerzeichen noch Klammern.
        if any(ch in y for ch in " ()"):
            logger.info("  %s -> %s: beschaedigtes yfinance_symbol — uebersprungen",
                        r["symbol"], y)
            falsch_klassifiziert += 1
            continue
        # Zwei Instrumente auf denselben Ticker: dieselbe Gefahr.
        if y.upper() in gesehene_ticker:
            logger.info("  %s -> %s: Ticker schon von %s belegt — uebersprungen",
                        r["symbol"], y, gesehene_ticker[y.upper()])
            doppelt += 1
            continue

        m = messung.get(r["instrument_id"])
        if not m or m.get("adv") is None:
            if m and m.get("kerzen", 0) and m["kerzen"] < args.min_history:
                zu_kurz += 1
            else:
                ohne_daten += 1
            continue
        if m["adv"] < args.min_adv:
            zu_illiquide += 1
            continue
        if m.get("vola", 0.0) < args.min_vola:
            logger.info("  %s -> %s: Streuung %.2f%% < %.2f%% — Stablecoin, "
                        "uebersprungen", r["symbol"], y,
                        m.get("vola", 0.0) * 100, args.min_vola * 100)
            stabil += 1
            continue

        gesehene_ticker[y.upper()] = r["symbol"]
        treffer.append({**r, "yf": y, "adv": m["adv"], "kerzen": m["kerzen"],
                        "preis": m.get("preis"), "vola": m.get("vola")})

    logger.info("  ohne Kursdaten: %d | Historie < %d Tage: %d | ADV < %s: %d",
                ohne_daten, args.min_history, zu_kurz, f"{args.min_adv:,.0f}",
                zu_illiquide)
    logger.info("  Stablecoin: %d | Doppel-Ticker: %d | falsch klassifiziert: %d",
                stabil, doppelt, falsch_klassifiziert)

    treffer.sort(key=lambda t: t["adv"], reverse=True)
    if args.max_add and len(treffer) > args.max_add:
        logger.info("  %d bestanden, auf die %d liquidesten begrenzt",
                    len(treffer), args.max_add)
        treffer = treffer[: args.max_add]
    return treffer


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--asset-class", choices=("crypto", "commodity", "both"),
                   default="both")
    p.add_argument("--min-adv", type=float, default=5_000_000.0,
                   help="Mindest-Tagesumsatz in USD (Default 5 Mio; der "
                        "Signalfilter MIN_ADV_USD liegt bei 500k)")
    p.add_argument("--min-history", type=int, default=90,
                   help="Mindestanzahl Tageskerzen (Default 90)")
    p.add_argument("--min-vola", type=float, default=0.02,
                   help="Mindest-Streuung des Kurses (std/mittel) ueber den "
                        "Zeitraum. Default 0.02 = 2 %%; haelt Stablecoins "
                        "draussen, die nur Rauschen liefern")
    p.add_argument("--max-add", type=int, default=40,
                   help="Obergrenze neuer Einträge je Klasse (Default 40)")
    p.add_argument("--limit-probe", type=int, default=0,
                   help="nur die ersten N Kandidaten prüfen (Testlauf)")
    p.add_argument("--apply", action="store_true",
                   help="tatsächlich schreiben (ohne dies: Trockenlauf)")
    args = p.parse_args()

    _load_env()
    api_key = os.environ.get("ETORO_BOT_API_KEY", "")
    user_key = os.environ.get("ETORO_BOT_USER_KEY", "")
    if not api_key or not user_key:
        logger.critical("ETORO_BOT_API_KEY / ETORO_BOT_USER_KEY fehlen — Abbruch")
        return 1

    from bot.api.client import ClientConfig, EToroClient
    from bot.config import load_config
    from bot.db.connection import DB

    cfg = load_config()
    db = DB(db_path=PROJECT_ROOT / cfg.db.path)
    api_cfg = {}
    if hasattr(cfg, "api"):
        api_cfg = cfg.api if isinstance(cfg.api, dict) else vars(cfg.api)
    client = EToroClient(api_key=api_key, user_key=user_key,
                         config=ClientConfig.from_dict(api_cfg))

    klassen = (["crypto", "commodity"] if args.asset_class == "both"
               else [args.asset_class])

    gesamt = 0
    for kl in klassen:
        treffer = _verarbeite(db, client, kl, args)
        print()
        print(f"  {'=' * 78}")
        print(f"  {kl.upper()} — {len(treffer)} Kandidaten bestehen alle Prüfungen")
        print(f"  {'=' * 78}")
        if treffer:
            print(f"  {'Symbol':14s} {'yfinance':14s} {'ADV (USD)':>18s} "
                  f"{'Kerzen':>7s} {'Preis':>12s} {'Streuung':>9s}")
            for t in treffer:
                pr = f"{t['preis']:,.4f}" if t.get("preis") else "-"
                vo = f"{t['vola'] * 100:.1f}%" if t.get("vola") is not None else "-"
                print(f"  {str(t['symbol'])[:14]:14s} {t['yf'][:14]:14s} "
                      f"{t['adv']:18,.0f} {t['kerzen']:7d} {pr:>12s} {vo:>9s}")
        print()

        if args.apply and treffer:
            n = _apply(db, treffer, kl)
            logger.info("%s: %d Einträge angelegt (Kategorie %s)",
                        kl, n, CATEGORY[kl])
            gesamt += n
        elif treffer:
            logger.info("%s: TROCKENLAUF — nichts geschrieben. Mit --apply anwenden.", kl)

    if args.apply:
        logger.info("Fertig: %d neue Watchlist-Einträge insgesamt.", gesamt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
