#!/usr/bin/env python3
"""
scripts/fix_yfinance_symbol_mismatches.py

Korrigiert instruments.yfinance_symbol dort, wo es auf einen ANDEREN Titel
oder eine andere Aktiengattung zeigt als das eToro-Symbol.

Hintergrund (2026-08-28): Der Identity-Guard der Discovery meldete
"Identity MISMATCH: lokal 'CMI' != eToro 'CTRM'". Die instrument_id ist dabei
korrekt — falsch ist das yfinance_symbol. Konkret holte der Bot damit Kurse
eines fremden Unternehmens und haette darauf Signale gerechnet:

  ID    6210  eToro CTRM (Castor Maritime)  -> yf CMI  = Cummins Inc
  ID    6586  eToro AIV  (Apartment Inv.)   -> yf AIA  = iShares Asia 50 ETF
  ID    2231  eToro ERIC-A.ST (Ericsson A)  -> yf ERIC-B.ST  (andere Gattung)
  ID    2234  eToro VOLV-A.ST (Volvo A)     -> yf VOLV-B.ST  (andere Gattung)
  ID 1013116  eToro SEBC.ST   (SEB C)       -> yf SEB-A.ST   (andere Gattung)
  ID   13845  eToro SLPb.ST   (SLP B)       -> yf SLPA.ST    (andere Gattung)
  ID 1013815  eToro BIFB.CO   (Broendby B)  -> yf BIFA.CO    (andere Gattung)
  ID   12545  eToro EAD.DE    (Erlebnis Ak.)-> yf EAA.DE     (fremdes Kuerzel)

A- und B-Aktien haben unterschiedliche Kurse und Stimmrechte — ein Signal auf
der B-Aktie und eine Order auf der A-Aktie sind zwei verschiedene Geschaefte.

Schaden bisher: keiner. Keines der Instrumente stand auf der Watchlist, und
ausgefuehrt wurde nie eines (ERIC-B.ST: 19 REJECTED, 5 FAILED, 0 CLOSED).
Der Guard hat sie zuverlaessig geblockt. Das hier raeumt die Ursache auf,
damit sie nicht bei der naechsten Discovery-Rotation erneut auflaufen.

Vorgehen je Kandidat:
  1. Vorschlag bilden (eToro-Symbol, ggf. mit Bindestrich vor der Gattung)
  2. Gegen yfinance pruefen: liefert der Ticker Kursdaten?
  3. Nur dann schreiben. Ohne Bestaetigung bleibt der alte Wert stehen.

Idempotent: ein zweiter Lauf findet nichts mehr. Trockenlauf ist Standard.

  PYTHONPATH=src python3 scripts/fix_yfinance_symbol_mismatches.py
  PYTHONPATH=src python3 scripts/fix_yfinance_symbol_mismatches.py --apply
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("yf_symbol_fix")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Boersensuffixe, bei denen eToro die Gattung ohne Bindestrich schreibt,
# yfinance aber mit (SDIPB.ST <-> SDIP-B.ST). Nur diese werden umgeformt.
BINDESTRICH_BOERSEN = (".ST", ".CO", ".OL", ".HE")


def _gattung(sym: str):
    """(Basis, Gattungsbuchstabe, Suffix) oder None."""
    if not sym:
        return None
    m = re.match(r"^([A-Z0-9]+?)-?([ABCD])(\.[A-Z]+)$", sym.upper())
    return (m.group(1), m.group(2), m.group(3)) if m else None


# Platzhalter-Muster aus gescheiterten Resolve-Versuchen (siehe
# scripts/audit_instrument_symbols.py v5): '<Kuerzel>_<id>', '.old', Ziffern
# hinter dem Boersensuffix. Fuer die ist das eToro-Symbol KEIN gueltiger
# Ticker — dass yfinance darauf Daten liefert, beweist dann nichts ausser
# einer Namensgleichheit mit einem fremden Titel.
_PLATZHALTER = re.compile(r"_\d+$|[._]old$|\.[A-Z]+\d+$|^[A-Z]{1,4}_", re.I)


def _ist_platzhalter(sym: str) -> bool:
    return bool(sym) and bool(_PLATZHALTER.search(sym))


def _vorschlag(etoro_symbol: str) -> str:
    """yfinance-Schreibweise des eToro-Symbols."""
    g = _gattung(etoro_symbol)
    if g and g[2] in BINDESTRICH_BOERSEN:
        return f"{g[0]}-{g[1]}{g[2]}"
    return etoro_symbol


def _kandidaten(db) -> list[dict]:
    """Instrumente, deren yfinance_symbol auf einen anderen Titel zeigt."""
    rows = db.fetchall(
        """
        SELECT i.instrument_id, i.symbol, i.yfinance_symbol, i.name,
               i.is_active, i.is_tradable
        FROM instruments i
        WHERE i.yfinance_symbol IS NOT NULL AND i.yfinance_symbol <> ''
          AND i.symbol <> i.yfinance_symbol
        """
    )
    out = []
    for r in rows:
        sym, yf = (r["symbol"] or "").upper(), (r["yfinance_symbol"] or "").upper()
        gs, gy = _gattung(sym), _gattung(yf)
        # Fall A: gleiche Basis, ANDERE Gattung (ERIC-A vs ERIC-B)
        if gs and gy and gs[0] == gy[0] and gs[2] == gy[2] and gs[1] != gy[1]:
            out.append(dict(r))
            continue
        # Fall B: das yfinance_symbol ist ein GEKUERZTER Ticker, der zufaellig
        # einem anderen Unternehmen gehoert (HPQ -> HP = Helmerich & Payne,
        # MANU -> MU = Micron, MOS -> MC = Moelis).
        #
        # NICHT anfassen: ADR-auf-Heimatboerse. SONY -> 6758.T, SAN -> SAN.MC,
        # HSBC -> HSBA.L sind gewollt — derselbe Titel an seiner Hauptboerse,
        # mit besserer Datenqualitaet als das ADR. Erkennbar daran, dass das
        # yfinance_symbol ein BOERSENSUFFIX traegt, das eToro-Symbol aber
        # nicht. Ein erster Entwurf ohne diese Unterscheidung schlug 294
        # Kandidaten vor und haette jede dieser Zuordnungen zerstoert.
        if _vorschlag(sym).upper() == yf:
            continue          # nur Bindestrich-Variante, korrekt
        if "." in yf and "." not in sym:
            continue          # ADR -> Heimatboerse, beabsichtigt
        if "." in yf and "." in sym and yf.rsplit(".", 1)[1] != sym.rsplit(".", 1)[1]:
            continue          # andere Boerse, ebenfalls eine bewusste Wahl
        fremd = db.fetchone(
            "SELECT instrument_id, name FROM instruments "
            "WHERE UPPER(symbol) = ? AND instrument_id <> ?",
            (yf, r["instrument_id"]),
        )
        if fremd and (fremd["name"] or "") != (r["name"] or ""):
            d = dict(r)
            d["_fremd"] = f"ID {fremd['instrument_id']} '{fremd['name']}'"
            out.append(d)
    return out


def _yf_liefert_daten(ticker: str) -> tuple[bool, str]:
    import yfinance as yf
    try:
        df = yf.Ticker(ticker).history(period="1mo")
    except Exception as exc:
        return False, f"Fehler: {exc}"
    if df is None or df.empty:
        return False, "keine Kursdaten"
    return True, f"{len(df)} Kerzen, zuletzt {float(df['Close'].iloc[-1]):.2f}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true",
                   help="tatsaechlich schreiben (ohne dies: Trockenlauf)")
    p.add_argument("--only-active", action="store_true",
                   help="nur is_active=1 behandeln")
    args = p.parse_args()

    from bot.config import load_config
    from bot.db.connection import DB

    cfg = load_config()
    db = DB(db_path=PROJECT_ROOT / cfg.db.path)

    kand = _kandidaten(db)
    if args.only_active:
        kand = [k for k in kand if k["is_active"] == 1]
    logger.info("Kandidaten mit fehlerhaftem yfinance_symbol: %d", len(kand))
    if not kand:
        logger.info("Nichts zu tun.")
        return 0

    geaendert = uebersprungen = 0
    print()
    for k in kand:
        neu = _vorschlag(k["symbol"])
        kennung = f"ID {k['instrument_id']:>8} {k['symbol']:<14}"
        if neu.upper() == (k["yfinance_symbol"] or "").upper():
            print(f"  {kennung} bereits korrekt")
            continue
        if _ist_platzhalter(k["symbol"]):
            print(f"  {kennung} {k['yfinance_symbol']} -> {neu:<18} UEBERSPRUNGEN "
                  f"(eToro-Symbol ist ein Platzhalter, kein Ticker)")
            uebersprungen += 1
            continue
        ok, detail = _yf_liefert_daten(neu)
        pfeil = f"{k['yfinance_symbol']} -> {neu}"
        grund = k.get("_fremd", "andere Gattung")
        if not ok:
            print(f"  {kennung} {pfeil:<30} UEBERSPRUNGEN ({detail})")
            uebersprungen += 1
            continue
        print(f"  {kennung} {pfeil:<30} OK ({detail})  [{grund}]")
        if args.apply:
            db.execute(
                "UPDATE instruments SET yfinance_symbol = ?, last_updated = ? "
                "WHERE instrument_id = ?",
                (neu, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                 k["instrument_id"]),
            )
        geaendert += 1

    print()
    if args.apply:
        logger.info("%d korrigiert, %d uebersprungen.", geaendert, uebersprungen)
    else:
        logger.info("TROCKENLAUF: %d waeren korrigiert, %d uebersprungen. "
                    "Mit --apply anwenden.", geaendert, uebersprungen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
