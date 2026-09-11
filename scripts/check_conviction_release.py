#!/usr/bin/env python3
"""Was hat der naechtliche LLM-Review aus der MEDIUM-Freigabe gemacht?

Entscheid VoLLi 2026-09-11: TREND_PULLBACK,GOLDEN_CROSS laeuft fuer MEDIUM
wieder voll (1.0), HIGH bleibt gedaempft (0.25). Gesetzt wurde das VON HAND,
nachdem sechs Reviews in Folge den ganzen Typ gedaempft hatten, obwohl ihre
eigene Begruendung nur die HIGH-Variante betraf.

Die Freigabe ist NICHT gegen die LLM geschuetzt: Verschaerfung ist die
Richtung, die die Ratsche bewusst nicht blockiert. Dieses Script beantwortet
genau eine Frage — was ist nach dem naechsten Lauf davon uebrig?

  PYTHONPATH=src python3 scripts/check_conviction_release.py

Liest nur. Aendert nichts.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

WEIGHTS = PROJECT_ROOT / "data" / "llm_signal_weights.json"
BASELINE = (PROJECT_ROOT / "data" / "archiv"
            / "llm_signal_weights_nach_medium_freigabe_2026-09-11.json")
TYP = "TREND_PULLBACK,GOLDEN_CROSS"
STUFEN = ("HIGH", "MEDIUM", "LOW", "VERY_HIGH")


def _eintrag(pfad: Path) -> dict:
    try:
        return json.loads(pfad.read_text(encoding="utf-8")).get(
            "adjustments", {}).get(TYP, {}) or {}
    except Exception as exc:
        print(f"  ({pfad.name} nicht lesbar: {exc})")
        return {}


def main() -> int:
    from bot.workers.signal_worker import _get_signal_score_multiplier as mult

    aktuell_datei = json.loads(WEIGHTS.read_text(encoding="utf-8"))
    soll, ist = _eintrag(BASELINE), _eintrag(WEIGHTS)

    print("── MEDIUM-Freigabe: was ist davon uebrig? " + "─" * 21)
    print(f"  Gewichte zuletzt geschrieben: {aktuell_datei.get('updated_at')}")
    print()
    print(f"  {'Stufe':<12}{'gesetzt (11.09.)':>18}{'jetzt':>10}   Bewertung")
    print("  " + "-" * 62)
    abweichung = False
    for stufe in STUFEN:
        s = mult(TYP, {"adjustments": {TYP: soll}}, stufe) if soll else None
        i = mult(TYP, {"adjustments": {TYP: ist}}, stufe) if ist else None
        if s is None or i is None:
            note = "Eintrag fehlt"
        elif abs(s - i) < 1e-9:
            note = "unveraendert"
        else:
            note = ("GELOCKERT" if i > s else "NACHGEDAEMPFT")
            abweichung = True
        print(f"  {stufe:<12}{str(s):>18}{str(i):>10}   {note}")

    print()
    print(f"  by_conviction erhalten?  "
          f"{'ja' if ist.get('by_conviction') else 'NEIN — Feld ist weg'}")
    print(f"  _decided_by erhalten?    "
          f"{ist.get('_decided_by') or 'NEIN'}")
    if ist.get("_ratchet_frozen"):
        print(f"  Ratsche: eingefroren, Vorschlag war {ist.get('_proposed')}")
    if ist.get("_ratchet_reason"):
        print(f"  Ratschen-Notiz: {ist['_ratchet_reason']}")
    if ist.get("reason"):
        print(f"\n  Begruendung im aktuellen Eintrag:\n    {ist['reason'][:300]}")

    print("\n── Hat die LLM die Conviction-Ebene selbst genutzt? " + "─" * 12)
    fremde = {k: v for k, v in (aktuell_datei.get("adjustments") or {}).items()
              if k != TYP and isinstance(v, dict) and v.get("by_conviction")}
    if fremde:
        for k, v in fremde.items():
            print(f"  {k}: {v['by_conviction']}")
    else:
        print("  Kein anderer Typ nutzt by_conviction.")

    print("\n" + ("  ERGEBNIS: die Freigabe wurde veraendert — siehe oben."
                  if abweichung else
                  "  ERGEBNIS: die Freigabe steht unveraendert."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
