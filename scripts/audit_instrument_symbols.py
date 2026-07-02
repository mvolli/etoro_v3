#!/usr/bin/env python3
"""
audit_instrument_symbols.py (v5) - Systematischer Abgleich ALLER lokalen
instrument_id -> symbol Zuordnungen gegen die Live-eToro-API.

v5 Aenderung gegenueber v4: v4's Bisektions-Fallback war korrekt in der
Isolation, aber ruinoes teuer, wenn viele IDs im selben Chunk kaputt
sind (nicht nur einzelne Ausreisser) - beobachtet: ~1.7% Fortschritt in
10 Minuten, projiziert auf ~9-10 Stunden Gesamtlaufzeit.

Root Cause: viele lokale Symbole folgen dem Muster '<Kuerzel>_<id>'
(z.B. 'DC_787', 'AI_1134', 'LMC_1136') - das ist erkennbar ein
automatisch generierter Platzhalter aus einem alten, gescheiterten
Resolve-Versuch, keine echte Ticker-Bezeichnung. Diese Instrumente
sind vermutlich bei eToro delisted/ungueltig und liefern JEDE
Anfrage (einzeln oder im Batch) mit HTTP 500 - Bisektion versucht das
trotzdem "herauszufinden" und verschwendet dabei massiv Requests.

v5 erkennt dieses Platzhalter-Muster VOR jedem API-Call und
ueberspringt die Verifikation fuer diese IDs komplett (landen direkt
in "nicht pruefbar" mit einer erklaerenden Begruendung, ohne jede
Anfrage) - dadurch bleibt fuer die Bisektion nur noch der viel
kleinere Anteil echter, unerwarteter Fehler uebrig.

Nutzung identisch zu v3/v4:
    python3 scripts/audit_instrument_symbols.py
    python3 scripts/audit_instrument_symbols.py --resume
    python3 scripts/audit_instrument_symbols.py --resume --apply
    python3 scripts/audit_instrument_symbols.py --resume --apply --yes-to-all

Requires ETORO_API_KEY / ETORO_USER_KEY (aus ~/.hermes/.env oder Umgebung).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import yaml  # type: ignore[import]

from bot.api.client import APIError, ClientConfig, EToroClient
from bot.db.connection import DB

PROGRESS_FILE = PROJECT_ROOT / "data" / "audit_instrument_symbols_progress.json"
REPORT_FILE = PROJECT_ROOT / "data" / "audit_instrument_symbols_report.csv"

CHUNK_SIZE = 50
DELAY_BETWEEN_CHUNKS_S = 1.0
MAX_RETRY_ATTEMPTS = 5
RETRY_BACKOFF_BASE_S = 5.0  # 5s -> 10s -> 20s -> 40s -> 80s

# '<Kuerzel>_<id>' - z.B. 'DC_787', 'AI_1134'. Nur ein Kandidat, wenn die
# Zahl am Ende EXAKT der eigenen instrument_id entspricht - sonst koennte
# es ein echter Ticker mit Unterstrich sein (unwahrscheinlich, aber
# sicherheitshalber streng geprueft statt pauschal alles mit '_' zu werten).
_PLACEHOLDER_PATTERN = re.compile(r"^[A-Z]{1,6}_(\d+)$")


def _looks_like_placeholder(instrument_id: int, symbol: str) -> bool:
    m = _PLACEHOLDER_PATTERN.match((symbol or "").strip().upper())
    if not m:
        return False
    try:
        return int(m.group(1)) == int(instrument_id)
    except ValueError:
        return False


def _load_env_keys() -> tuple[str, str]:
    import os
    import re as _re

    env_path = Path.home() / ".hermes" / ".env"
    env_vars: dict[str, str] = {}
    if env_path.exists():
        with env_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = _re.match(r"^([A-Z0-9_]+)\s*=\s*(.+)$", line)
                if m:
                    key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
                    env_vars[key] = val

    api_key = env_vars.get("ETORO_API_KEY") or os.environ.get("ETORO_API_KEY", "")
    user_key = env_vars.get("ETORO_USER_KEY") or os.environ.get("ETORO_USER_KEY", "")
    if not api_key or not user_key:
        raise RuntimeError("ETORO_API_KEY / ETORO_USER_KEY not found")
    return api_key, user_key


def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {"checked_ids": [], "mismatches": [], "unverifiable": [], "ok_count": 0}


def _save_progress(progress: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress, indent=2, ensure_ascii=False))
    tmp.rename(PROGRESS_FILE)


def _resolve_ids_recursive(
    client: EToroClient, ids: list[int], depth: int = 0
) -> tuple[dict[int, dict], dict[int, str]]:
    """Resolve metadata for `ids` via the batch endpoint.

    429 -> backoff + retry at same size. Any other error -> bisect into
    two halves, recurse. Returns (meta_by_id, error_by_id).
    """
    if not ids:
        return {}, {}

    last_error_msg = None
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            meta = client.get_instruments_metadata_batch(ids, chunk_size=len(ids))
            return meta, {}
        except APIError as exc:
            if exc.status_code == 429:
                wait_s = RETRY_BACKOFF_BASE_S * (2 ** attempt)
                print(f"    Rate-Limit (429) fuer {len(ids)} IDs - warte {wait_s:.0f}s "
                      f"(Versuch {attempt + 1}/{MAX_RETRY_ATTEMPTS})...")
                time.sleep(wait_s)
                continue
            last_error_msg = f"API-Fehler ({exc.status_code}): {exc}"
            break
        except Exception as exc:
            last_error_msg = f"Unerwarteter Fehler: {exc}"
            break
    else:
        last_error_msg = f"Rate-Limit nach {MAX_RETRY_ATTEMPTS} Versuchen nicht ueberwunden"

    if len(ids) == 1:
        return {}, {ids[0]: last_error_msg}

    mid = len(ids) // 2
    left_ids, right_ids = ids[:mid], ids[mid:]
    print(f"    Chunk-Fehler bei {len(ids)} IDs ({last_error_msg}) - "
          f"bisektiere in {len(left_ids)} + {len(right_ids)}...")
    meta_l, err_l = _resolve_ids_recursive(client, left_ids, depth + 1)
    time.sleep(DELAY_BETWEEN_CHUNKS_S)
    meta_r, err_r = _resolve_ids_recursive(client, right_ids, depth + 1)
    time.sleep(DELAY_BETWEEN_CHUNKS_S)
    meta_l.update(meta_r)
    err_l.update(err_r)
    return meta_l, err_l


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit all instrument_id->symbol mappings against live eToro API")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes-to-all", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    parser.add_argument("--no-placeholder-filter", action="store_true",
                         help="Platzhalter-Vorfilter deaktivieren (nicht empfohlen - fuehrt zu massiver Bisektion)")
    args = parser.parse_args()

    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    if not cfg_path.exists():
        print("FATAL: config/config.yaml not found")
        return 1
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

    rows = db.fetchall("SELECT instrument_id, symbol, asset_class FROM instruments ORDER BY instrument_id")
    all_rows = [
        (row["instrument_id"] if isinstance(row, dict) else row[0],
         row["symbol"] if isinstance(row, dict) else row[1])
        for row in rows
    ]
    local_symbol_by_id = {iid: sym for iid, sym in all_rows}

    progress = _load_progress() if args.resume else {"checked_ids": [], "mismatches": [], "unverifiable": [], "ok_count": 0}
    checked_ids = set(progress["checked_ids"])
    mismatches = progress["mismatches"]
    unverifiable = progress["unverifiable"]
    ok_count = progress["ok_count"]

    # ── Platzhalter-Vorfilter: bekannter Datenmuell, keine API-Anfrage ──────
    if not args.no_placeholder_filter:
        placeholder_count = 0
        for iid, sym in all_rows:
            if iid in checked_ids:
                continue
            if _looks_like_placeholder(iid, sym):
                unverifiable.append({
                    "instrument_id": iid,
                    "local_symbol": sym,
                    "reason": (
                        f"Platzhalter-Muster erkannt ('{sym}' == Kuerzel + eigene ID) - "
                        f"vermutlich delisted/ungueltiges Instrument, keine API-Anfrage versucht"
                    ),
                })
                checked_ids.add(iid)
                placeholder_count += 1
        if placeholder_count:
            print(f"Platzhalter-Vorfilter: {placeholder_count} IDs erkannt und uebersprungen "
                  f"(kein API-Call), z.B. '<Kuerzel>_<id>'-Muster wie 'DC_787'.\n")
            progress.update(checked_ids=list(checked_ids), mismatches=mismatches,
                             unverifiable=unverifiable, ok_count=ok_count)
            _save_progress(progress)

    remaining_ids = [iid for iid, _ in all_rows if iid not in checked_ids]
    total_chunks = (len(remaining_ids) + args.chunk_size - 1) // max(args.chunk_size, 1)

    if args.resume and checked_ids:
        print(f"Resume: {len(checked_ids)} bereits erledigt, {len(remaining_ids)} verbleibend "
              f"({total_chunks} Chunks a {args.chunk_size}).\n")
    else:
        print(f"Lauf: {len(remaining_ids)} zu pruefende Instrumente, {total_chunks} Chunks a {args.chunk_size} IDs.\n")

    try:
        for start in range(0, len(remaining_ids), args.chunk_size):
            chunk_ids = remaining_ids[start : start + args.chunk_size]
            chunk_num = start // args.chunk_size + 1
            print(f"Chunk {chunk_num}/{total_chunks} ({len(chunk_ids)} IDs)...")

            meta_by_id, err_by_id = _resolve_ids_recursive(client, chunk_ids)

            for iid, reason in err_by_id.items():
                unverifiable.append({
                    "instrument_id": iid,
                    "local_symbol": local_symbol_by_id.get(iid, "?"),
                    "reason": reason,
                })
                checked_ids.add(iid)

            for iid in chunk_ids:
                if iid in err_by_id:
                    continue
                local_symbol = local_symbol_by_id.get(iid, "?")
                meta = meta_by_id.get(iid)

                if not meta:
                    unverifiable.append({"instrument_id": iid, "local_symbol": local_symbol,
                                          "reason": "nicht in Batch-Antwort enthalten"})
                    checked_ids.add(iid)
                    continue

                live_symbol = (
                    meta.get("symbolFull") or meta.get("internalSymbolFull")
                    or meta.get("symbol") or meta.get("ticker") or meta.get("displayName") or ""
                )
                if not live_symbol:
                    unverifiable.append({"instrument_id": iid, "local_symbol": local_symbol,
                                          "reason": "kein Symbolfeld in Antwort"})
                    checked_ids.add(iid)
                    continue

                norm_local = EToroClient._normalize_symbol_for_comparison(local_symbol)
                norm_live = EToroClient._normalize_symbol_for_comparison(str(live_symbol))

                if norm_local == norm_live:
                    ok_count += 1
                else:
                    mismatches.append({"instrument_id": iid, "local_symbol": local_symbol, "live_symbol": live_symbol})
                    print(f"  MISMATCH: id={iid}  lokal='{local_symbol}'  live='{live_symbol}'")

                checked_ids.add(iid)

            progress.update(checked_ids=list(checked_ids), mismatches=mismatches,
                             unverifiable=unverifiable, ok_count=ok_count)
            _save_progress(progress)
            time.sleep(DELAY_BETWEEN_CHUNKS_S)

    except KeyboardInterrupt:
        print("\n\nAbgebrochen (Ctrl-C) - Fortschritt gespeichert. Mit --resume weitermachen.")
        client.close()
        return 1

    client.close()

    print("\n" + "=" * 70)
    print(" ERGEBNIS (vollstaendig)")
    print("=" * 70)
    print(f" Geprueft:       {len(checked_ids)} / {len(all_rows)}")
    print(f" OK:             {ok_count}")
    print(f" Mismatches:     {len(mismatches)}")
    print(f" Nicht pruefbar: {len(unverifiable)}")

    if mismatches:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with REPORT_FILE.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["instrument_id", "local_symbol", "live_symbol"])
            writer.writeheader()
            writer.writerows(mismatches)
        print(f"\nVollstaendiger Mismatch-Report geschrieben: {REPORT_FILE}")
        print(f"({len(mismatches)} Zeilen - vor --apply gerne erst durchsehen)")

    if unverifiable:
        placeholder_skipped = sum(1 for u in unverifiable if "Platzhalter-Muster" in u.get("reason", ""))
        print(f"\n{len(unverifiable)} nicht pruefbar "
              f"(davon {placeholder_skipped} Platzhalter ohne API-Call uebersprungen):")
        real_errors = [u for u in unverifiable if "Platzhalter-Muster" not in u.get("reason", "")]
        for u in real_errors[:10]:
            print(f"  id={u['instrument_id']}  lokal='{u['local_symbol']}'  -> {u['reason']}")
        if len(real_errors) > 10:
            print(f"  ... und {len(real_errors) - 10} weitere echte Fehler (siehe Progress-Datei)")

    if not mismatches:
        print("\nKeine weiteren Fehler gefunden.")
        return 0

    if not args.apply:
        print(f"\nDry-Run beendet - keine Aenderungen vorgenommen. "
              f"CSV-Report liegt unter {REPORT_FILE}. Zum Anwenden: --resume --apply")
        return 0

    print("\n" + "=" * 70)
    print(" KORREKTUREN")
    print("=" * 70)
    applied = 0
    conflicts = []

    for m in mismatches:
        iid = m["instrument_id"]
        target_symbol = m["live_symbol"]

        conflict_row = db.fetchone(
            "SELECT instrument_id FROM instruments WHERE symbol = ? AND instrument_id != ?",
            (target_symbol, iid),
        )
        if conflict_row:
            conflict_id = conflict_row["instrument_id"] if isinstance(conflict_row, dict) else conflict_row[0]
            print(f"  KONFLIKT: id={iid}: '{target_symbol}' existiert bereits unter id={conflict_id} "
                  f"- UEBERSPRUNGEN, braucht manuelle Pruefung")
            conflicts.append({"instrument_id": iid, "target_symbol": target_symbol, "conflict_id": conflict_id})
            continue

        if not args.yes_to_all:
            resp = input(f"  id={iid}: '{m['local_symbol']}' -> '{target_symbol}' korrigieren? [j/N] ")
            if resp.strip().lower() != "j":
                print(f"  uebersprungen (manuell abgelehnt)")
                continue

        db.execute(
            "UPDATE instruments SET symbol = ?, last_updated = datetime('now') WHERE instrument_id = ?",
            (target_symbol, iid),
        )
        print(f"  OK: id={iid}: korrigiert auf '{target_symbol}'")
        applied += 1

    print(f"\n{applied} Korrektur(en) angewendet, {len(conflicts)} Konflikt(e) uebersprungen.")
    if conflicts:
        conflict_file = PROJECT_ROOT / "data" / "audit_instrument_symbols_conflicts.csv"
        with conflict_file.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["instrument_id", "target_symbol", "conflict_id"])
            writer.writeheader()
            writer.writerows(conflicts)
        print(f"Konflikte geschrieben nach: {conflict_file} (manuelle Pruefung noetig)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
