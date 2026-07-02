#!/usr/bin/env python3
"""
resolve_symbol_conflicts.py — Analysiert die 184 UNIQUE-Konflikte aus dem
letzten audit_instrument_symbols.py --apply Lauf.

Fuer jedes Konflikt-Paar (instrument_id will Symbol X werden, aber
conflict_id hat X schon) wird gegen die Live-API geprueft:
  - Ist instrument_id aktuell handelbar (allowOpenPosition)?
  - Ist conflict_id aktuell handelbar (allowOpenPosition)?
  - Hat eine der beiden IDs Trade-Historie in der lokalen DB (trades-
    Tabelle)? Das ist ein Sicherheitssignal, KEIN Entscheidungskriterium
    fuer sich allein — eine ID mit echter Trade-Historie sollte nicht
    leichtfertig als "tot" behandelt werden, selbst wenn sie aktuell
    nicht mehr eligible ist (z.B. Position koennte noch offen sein).

Empfehlung pro Paar (nur Vorschlag, wird NICHT automatisch angewendet):
  - "conflict_id ist vermutlich tot"   : instrument_id eligible, conflict_id nicht
  - "instrument_id ist vermutlich tot" : conflict_id eligible, instrument_id nicht
  - "BEIDE eligible — manuell klaeren" : beide aktuell handelbar (echte
    Duplikate, z.B. zwei Listings)
  - "KEINE eligible — beide vermutlich tot": weder noch handelbar

Schreibt einen Report nach data/conflict_resolution_report.csv.
AENDERT NICHTS an der Datenbank.

Nutzung:
    python3 scripts/resolve_symbol_conflicts.py
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml  # type: ignore[import]

from bot.api.client import APIError, ClientConfig, EToroClient
from bot.db.connection import DB

CONFLICTS_FILE = PROJECT_ROOT / "data" / "audit_instrument_symbols_conflicts.csv"
REPORT_FILE = PROJECT_ROOT / "data" / "conflict_resolution_report.csv"

DELAY_S = 0.5


def _load_env_keys() -> tuple[str, str]:
    import os
    import re

    env_path = Path.home() / ".hermes" / ".env"
    env_vars: dict[str, str] = {}
    if env_path.exists():
        with env_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^([A-Z0-9_]+)\s*=\s*(.+)$", line)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
                    env_vars[key] = val
    api_key = env_vars.get("ETORO_API_KEY") or os.environ.get("ETORO_API_KEY", "")
    user_key = env_vars.get("ETORO_USER_KEY") or os.environ.get("ETORO_USER_KEY", "")
    if not api_key or not user_key:
        raise RuntimeError("ETORO_API_KEY / ETORO_USER_KEY not found")
    return api_key, user_key


def _check_eligible(client: EToroClient, instrument_id: int) -> tuple[bool | None, str]:
    """Returns (allowOpenPosition or None if unknown, detail string)."""
    try:
        elig = client.get_instrument_eligibility(instrument_id)
        if not elig:
            return None, "keine Eligibility-Daten zurueckgegeben"
        allowed = elig.get("allowOpenPosition")
        return bool(allowed), f"allowOpenPosition={allowed}"
    except APIError as exc:
        if exc.status_code == 404:
            return False, "404 — Instrument existiert nicht mehr bei eToro"
        return None, f"API-Fehler ({exc.status_code}): {exc}"
    except Exception as exc:
        return None, f"Unerwarteter Fehler: {exc}"


def main() -> int:
    if not CONFLICTS_FILE.exists():
        print(f"FATAL: {CONFLICTS_FILE} nicht gefunden")
        return 1

    with CONFLICTS_FILE.open(newline="", encoding="utf-8") as fh:
        conflicts = list(csv.DictReader(fh))

    print(f"{len(conflicts)} Konflikte zu analysieren (2 API-Calls pro Paar)...\n")

    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    with cfg_path.open() as fh:
        cfg = yaml.safe_load(fh)

    db_path = PROJECT_ROOT / cfg.get("db", {}).get("path", "data/trading.db")
    db = DB(db_path=db_path, busy_timeout_ms=5000)

    try:
        api_key, user_key = _load_env_keys()
    except RuntimeError as exc:
        print(f"FATAL: {exc}")
        return 1

    api_cfg = cfg.get("api", {})
    client = EToroClient(api_key=api_key, user_key=user_key, config=ClientConfig.from_dict(api_cfg))

    results = []
    for i, row in enumerate(conflicts, 1):
        iid = int(row["instrument_id"])
        target = row["target_symbol"]
        conflict_id = int(row["conflict_id"])

        print(f"[{i}/{len(conflicts)}] id={iid} vs id={conflict_id} (Ziel: '{target}')...")

        iid_eligible, iid_detail = _check_eligible(client, iid)
        time.sleep(DELAY_S)
        conflict_eligible, conflict_detail = _check_eligible(client, conflict_id)
        time.sleep(DELAY_S)

        # Trade-Historie als Sicherheitssignal (nicht als alleiniges Kriterium)
        iid_has_trades = db.fetchone(
            "SELECT 1 FROM trades WHERE instrument_id = ? LIMIT 1", (iid,)
        ) is not None
        conflict_has_trades = db.fetchone(
            "SELECT 1 FROM trades WHERE instrument_id = ? LIMIT 1", (conflict_id,)
        ) is not None

        if iid_eligible is True and conflict_eligible is False:
            recommendation = f"id={conflict_id} ist vermutlich tot — id={iid} korrigieren, id={conflict_id} pruefen"
        elif conflict_eligible is True and iid_eligible is False:
            recommendation = f"id={iid} ist vermutlich tot — id={iid} NICHT korrigieren, id={conflict_id} behaelt '{target}'"
        elif iid_eligible is True and conflict_eligible is True:
            recommendation = "BEIDE eligible — echtes Duplikat, manuell klaeren (evtl. unterschiedliche Boersen/Listings)"
        elif iid_eligible is False and conflict_eligible is False:
            recommendation = "KEINE eligible — beide vermutlich tot, niedrige Prioritaet"
        else:
            recommendation = "UNKLAR — Eligibility-Check fehlgeschlagen, manuell pruefen"

        if iid_has_trades or conflict_has_trades:
            recommendation += " [ACHTUNG: Trade-Historie vorhanden — extra vorsichtig sein]"

        results.append({
            "instrument_id": iid,
            "instrument_id_eligible": iid_eligible,
            "instrument_id_detail": iid_detail,
            "instrument_id_has_trades": iid_has_trades,
            "target_symbol": target,
            "conflict_id": conflict_id,
            "conflict_id_eligible": conflict_eligible,
            "conflict_id_detail": conflict_detail,
            "conflict_id_has_trades": conflict_has_trades,
            "recommendation": recommendation,
        })
        print(f"    -> {recommendation}")

    client.close()

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_FILE.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(results[0].keys()) if results else [])
        writer.writeheader()
        writer.writerows(results)

    print(f"\nReport geschrieben: {REPORT_FILE}")

    # Kurze Zusammenfassung
    from collections import Counter
    summary = Counter()
    for r in results:
        if "ist vermutlich tot" in r["recommendation"] and f"id={r['conflict_id']}" in r["recommendation"].split("—")[0]:
            summary["conflict_id vermutlich tot"] += 1
        elif "ist vermutlich tot" in r["recommendation"]:
            summary["instrument_id vermutlich tot"] += 1
        elif "BEIDE eligible" in r["recommendation"]:
            summary["echte Duplikate"] += 1
        elif "KEINE eligible" in r["recommendation"]:
            summary["beide tot"] += 1
        else:
            summary["unklar"] += 1

    print("\nZusammenfassung:")
    for k, v in summary.most_common():
        print(f"  {k}: {v}")

    trades_flagged = sum(1 for r in results if "ACHTUNG" in r["recommendation"])
    if trades_flagged:
        print(f"\n⚠️  {trades_flagged} Paare haben Trade-Historie an einer der IDs — diese "
              f"besonders vorsichtig pruefen, NICHT blind der Empfehlung folgen.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
