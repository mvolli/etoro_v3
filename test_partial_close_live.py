#!/usr/bin/env python3
"""
test_partial_close_live.py — Live-API-Verifikation für UnitsToDeduct

WICHTIG: Das ist ein eigenständiges Skript, KOMPLETT UNABHÄNGIG vom
Produktions-Bot (nutzt NICHT EToroClient/open_position/close_position aus
client.py). Es macht rohe requests-Calls, damit garantiert nichts mit der
Live-Bot-Logik (Gates, Signal-Worker, DB) interferiert.

Was es macht:
  1. Öffnet eine minimale Test-Position ($50, AAPL — hochliquide, damit
     Spread/Slippage-Risiko bei so kleiner Größe vernachlässigbar ist).
  2. Fragt die Position ab, um die tatsächliche Antwortstruktur zu sehen
     (positionID, amount, openRate — Feldnamen werden geloggt, nicht
     angenommen).
  3. Berechnet units_to_deduct für einen 50%-Partial-Close (bewusst 50%
     statt der Live-Werte 20/20/30%, damit die Größenänderung im Portfolio
     leicht mit bloßem Auge nachvollziehbar ist: $50 → sollte $25 werden).
  4. Fragt VOR dem Partial-Close-Call nochmal explizit nach Bestätigung.
  5. Führt den Partial-Close aus, loggt die rohe Antwort.
  6. Vergleicht Position vorher/nachher — PASS/FAIL wird explizit ausgegeben.
  7. Schließt die Restposition IMMER vollständig (finally-Block), egal wie
     der Test ausging — es bleibt keine offene Test-Position zurück.

Kosten-Einschätzung: Bewegt kurzzeitig ~$50 echtes Kapital. Realistischer
Verlust durch Spread/Slippage bei AAPL in dieser Größenordnung: im Bereich
weniger Cent bis niedriger einstelliger Dollar-Betrag, nicht mehr.

Nutzung:
    cd /home/mvolli/.hermes/workspace/etoro_v3
    python3 test_partial_close_live.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests

BASE_URL = "https://public-api.etoro.com/api/v1"
BASE_URL_V2 = "https://public-api.etoro.com/api/v2"

# ── Test-Parameter — bewusst konservativ gewählt ────────────────────────────
TEST_INSTRUMENT_ID = 1001          # AAPL — hochliquide, minimales Slippage
TEST_SYMBOL = "AAPL"
TEST_AMOUNT_USD = 50.0             # = MIN_BUY_USD aus config.yaml, kleinste
                                    #   Größe, die der Bot selbst je nutzt
PARTIAL_CLOSE_PCT = 50.0           # 50% statt Live-Werten (20/20/30), damit
                                    # die Änderung leicht sichtbar ist
POLL_WAIT_S = 5


def _load_env() -> None:
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        print(f"⚠️  .env nicht gefunden unter {env_path} — nutze bestehende Umgebung")
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _headers() -> dict:
    return {
        "x-api-key": os.environ["ETORO_API_KEY"],
        "x-user-key": os.environ["ETORO_USER_KEY"],
        "x-request-id": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _confirm(prompt: str) -> bool:
    resp = input(f"\n{prompt}\nTippe exakt 'JA' zum Fortfahren, alles andere bricht ab: ")
    return resp.strip() == "JA"


def _pretty(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def open_test_position() -> dict:
    print(f"\n→ POST {BASE_URL_V2}/trading/execution/orders")
    body = {
        "transaction": "Buy",
        "instrumentId": TEST_INSTRUMENT_ID,
        "amount": TEST_AMOUNT_USD,
        "leverage": 1,
        "isNoStopLoss": False,
        "stopLossRate": 0,  # wird unten korrekt gesetzt, s. Hinweis
    }
    # Hinweis: der Produktions-Code berechnet stopLossRate relativ zum
    # aktuellen Preis (siehe calculate_sl_price in risk.py). Für DIESEN
    # isolierten Test lassen wir eToro den Standard-SL setzen, indem wir
    # stopLossRate weglassen — falls die API das nicht akzeptiert, zeigt
    # uns die Fehlermeldung sofort, ob stopLossRate hier Pflicht ist.
    body.pop("stopLossRate", None)
    body.pop("isNoStopLoss", None)

    print(f"Body: {_pretty(body)}")
    resp = requests.post(
        f"{BASE_URL_V2}/trading/execution/orders", headers=_headers(), json=body
    )
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:2000]}")
    resp.raise_for_status()
    return resp.json()


def get_portfolio() -> dict:
    print(f"\n→ GET {BASE_URL}/trading/info/real/pnl")
    resp = requests.get(f"{BASE_URL}/trading/info/real/pnl", headers=_headers())
    print(f"Status: {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def find_test_position(portfolio: dict) -> dict | None:
    positions = (
        portfolio.get("clientPortfolio", {}).get("positions")
        or portfolio.get("positions")
        or []
    )
    for pos in positions:
        iid = pos.get("instrumentID") or pos.get("instrumentId")
        if iid is not None and int(iid) == TEST_INSTRUMENT_ID:
            return pos
    return None


def partial_close(position_id, units_to_deduct: float) -> dict:
    endpoint = f"{BASE_URL}/trading/execution/market-close-orders/positions/{position_id}"
    body = {"instrumentId": TEST_INSTRUMENT_ID, "UnitsToDeduct": units_to_deduct}
    print(f"\n→ POST {endpoint}")
    print(f"Body: {_pretty(body)}")
    resp = requests.post(endpoint, headers=_headers(), json=body)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:2000]}")
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def full_close(position_id) -> dict:
    endpoint = f"{BASE_URL}/trading/execution/market-close-orders/positions/{position_id}"
    body = {"instrumentId": TEST_INSTRUMENT_ID}
    print(f"\n→ POST {endpoint} (VOLLSTÄNDIGES Cleanup-Close)")
    resp = requests.post(endpoint, headers=_headers(), json=body)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:2000]}")
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def poll_position(
    expected_amount_predicate,
    max_attempts: int = 8,
    initial_wait_s: float = 3,
) -> tuple[dict | None, list[float]]:
    """Poll get_portfolio() with exponential backoff until
    expected_amount_predicate(pos_or_None) returns True, or attempts run out.

    Mirrors execution_worker.py's ghost-order polling (3s→6s→12s→24s...,
    capped) instead of a fixed sleep — a fixed 5s wait was exactly the bug
    class this codebase already fixed once (commit 05f7219) because eToro
    processes close/partial-close orders asynchronously (statusID in the
    immediate response means 'accepted', not 'completed').
    """
    waited_total = []
    for attempt in range(max_attempts):
        wait_time = min(initial_wait_s * (2 ** attempt), 30)
        print(f"  … warte {wait_time:.0f}s (Versuch {attempt + 1}/{max_attempts})")
        time.sleep(wait_time)
        waited_total.append(wait_time)

        portfolio = get_portfolio()
        pos = find_test_position(portfolio)
        if expected_amount_predicate(pos):
            print(f"  ✓ Erwarteter Zustand erreicht nach {sum(waited_total):.0f}s")
            return pos, waited_total
    print(f"  ⚠️  Erwarteter Zustand NICHT erreicht nach {sum(waited_total):.0f}s "
          f"({max_attempts} Versuche) — letzter beobachteter Stand wird gewertet")
    portfolio = get_portfolio()
    return find_test_position(portfolio), waited_total


def main() -> int:
    _load_env()
    if "ETORO_API_KEY" not in os.environ or "ETORO_USER_KEY" not in os.environ:
        print("❌ ETORO_API_KEY / ETORO_USER_KEY nicht in der Umgebung gefunden.")
        return 1

    print("═" * 70)
    print(" LIVE-API-TEST: Partial-Close (UnitsToDeduct)")
    print(" ⚠️  BEWEGT ECHTES KAPITAL — kein Demo-Modus")
    print("═" * 70)
    print(f" Instrument:  {TEST_SYMBOL} (ID {TEST_INSTRUMENT_ID})")
    print(f" Test-Betrag: ${TEST_AMOUNT_USD:.2f}")
    print(f" Partial-Close-Ziel: {PARTIAL_CLOSE_PCT:.0f}% (zur Kontrolle: ${TEST_AMOUNT_USD * (1 - PARTIAL_CLOSE_PCT/100):.2f} sollten übrig bleiben)")
    print("═" * 70)

    if not _confirm(
        f"Schritt 1: Test-Position über ${TEST_AMOUNT_USD:.2f} in {TEST_SYMBOL} ÖFFNEN?"
    ):
        print("Abgebrochen — keine Order gesendet.")
        return 0

    position_id = None
    try:
        open_resp = open_test_position()
        print(f"\n✓ Order gesendet. Warte {POLL_WAIT_S}s, dann Portfolio-Check...")
        time.sleep(POLL_WAIT_S)

        portfolio = get_portfolio()
        pos_before = find_test_position(portfolio)
        if not pos_before:
            print("❌ Keine Position für AAPL im Portfolio gefunden — Order evtl. nicht "
                  "materialisiert (Ghost-Order-Fall). Test kann nicht fortgesetzt werden.")
            print(f"Rohe open_position-Antwort zur Diagnose: {_pretty(open_resp)}")
            return 1

        position_id = pos_before.get("positionID") or pos_before.get("positionId")
        amount_before = float(pos_before.get("amount", 0))
        open_rate = float(pos_before.get("openRate", 0) or 0)

        print(f"\n✓ Position gefunden: positionID={position_id}, "
              f"amount=${amount_before:.2f}, openRate={open_rate}")
        print(f"Rohe Positions-Struktur (Feldnamen zur Kontrolle):\n{_pretty(pos_before)}")

        if open_rate <= 0:
            print("❌ openRate=0 — kann units_to_deduct nicht berechnen. Abbruch, gehe zu Cleanup.")
            return 1

        total_units = amount_before / open_rate
        units_to_deduct = round(total_units * (PARTIAL_CLOSE_PCT / 100.0), 8)

        if not _confirm(
            f"Schritt 2: Partial-Close mit UnitsToDeduct={units_to_deduct:.8f} "
            f"({PARTIAL_CLOSE_PCT:.0f}% von {total_units:.8f} Units) AUSFÜHREN?"
        ):
            print("Abgebrochen — gehe direkt zu Cleanup (volle Position wird geschlossen).")
            return 0

        partial_close(position_id, units_to_deduct)

        print(f"\nPolle Portfolio mit exponentiellem Backoff, bis sich der Betrag "
              f"ändert (max. ~4 Min, wie execution_worker.py's Ghost-Order-Polling) — "
              f"ein fixer 5s-Sleep war GENAU der Bug, den wir in execution_worker.py "
              f"schon einmal fixen mussten (asynchrone Order-Verarbeitung)...")

        def _amount_changed(pos) -> bool:
            if pos is None:
                return True  # Position komplett weg zählt auch als "Zustand geändert"
            return abs(float(pos.get("amount", 0)) - amount_before) > 0.01

        pos_after, waited = poll_position(_amount_changed, max_attempts=8, initial_wait_s=3)

        print("\n" + "═" * 70)
        print(" ERGEBNIS")
        print("═" * 70)
        print(f" Gewartet: {sum(waited):.0f}s über {len(waited)} Polling-Versuche")
        if pos_after is None:
            print("❌ FAIL — Position komplett verschwunden. UnitsToDeduct hat vermutlich "
                  "die GESAMTE Position geschlossen statt nur einen Teil.")
        else:
            amount_after = float(pos_after.get("amount", 0))
            expected = amount_before * (1 - PARTIAL_CLOSE_PCT / 100.0)
            diff_pct = abs(amount_after - expected) / max(expected, 0.01) * 100
            print(f" Vorher:   ${amount_before:.2f}")
            print(f" Erwartet: ${expected:.2f} (nach {PARTIAL_CLOSE_PCT:.0f}% Close)")
            print(f" Tatsächlich: ${amount_after:.2f}")
            if diff_pct < 5:
                print(f" ✅ PASS — Abweichung {diff_pct:.1f}% (innerhalb Toleranz für Spread/Rundung)")
            elif abs(amount_after - amount_before) < 0.01:
                print(f" ❌ FAIL — Betrag auch nach {sum(waited):.0f}s Polling unverändert. "
                      f"UnitsToDeduct hatte KEINE Wirkung (falscher Feldname/Einheit, "
                      f"nicht nur ein Timing-Problem).")
            else:
                print(f" ⚠️  UNKLAR — Abweichung {diff_pct:.1f}%, weder klar PASS noch FAIL. "
                      f"Bitte rohe Antworten oben manuell prüfen.")

        return 0

    finally:
        if position_id:
            print("\n" + "═" * 70)
            print(" CLEANUP — schließe Restposition vollständig")
            print("═" * 70)
            try:
                full_close(position_id)
                print("✓ Cleanup-Close gesendet. Verifiziere mit Polling...")

                def _fully_closed(pos) -> bool:
                    return pos is None

                final_pos, final_wait = poll_position(
                    _fully_closed, max_attempts=8, initial_wait_s=3
                )
                if final_pos is None:
                    print(f"✅ CLEANUP BESTÄTIGT — keine {TEST_SYMBOL}-Position mehr offen "
                          f"(nach {sum(final_wait):.0f}s).")
                else:
                    print(f"❌ CLEANUP NICHT BESTÄTIGT — nach {sum(final_wait):.0f}s ist noch "
                          f"eine {TEST_SYMBOL}-Position offen (amount=${float(final_pos.get('amount', 0)):.2f}). "
                          f"⚠️  BITTE MANUELL IM ETORO-KONTO PRÜFEN UND SCHLIESSEN!")
            except Exception as exc:
                print(f"❌ CLEANUP FEHLGESCHLAGEN: {exc}")
                print(f"⚠️  MANUELL PRÜFEN: Position {position_id} ({TEST_SYMBOL}) im "
                      f"eToro-Konto — evtl. noch offen, bitte manuell schließen!")


if __name__ == "__main__":
    sys.exit(main())
