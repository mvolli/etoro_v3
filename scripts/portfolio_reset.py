#!/usr/bin/env python3
"""Kompletter Portfolio-Reset auf das Startkapital — Historie bleibt.

feat/portfolio-reset (2026-09-10, Entscheid VoLLi). Nach Wochen Optimierung
soll das neue Handelsverhalten auf einem SAUBEREN Buch beobachtet werden.
Wegzuwerfen ist das Portfolio, nicht das Wissen: Trades, trade_events,
Signale, Kelly-Historie und die LLM-Lerndateien bleiben unangetastet.

Zwei Stufen, weil sie unterschiedliche Voraussetzungen haben:

  prepare   — offline, braucht KEINEN gueltigen API-Key.
              Kill-Switch setzen, verwaiste LLM-Empfehlungen leeren.
              Fasst die Datenbank NICHT an.

  finalize  — braucht einen laufenden Reconciler-Durchgang davor.
              Bucht die Kapitalbewegung, setzt die Epoche und den
              operativen Zustand zurueck.

  status    — zeigt, was jede Stufe tun wuerde. Aendert nichts.

Warum die Trennung: die DB haelt heute 30 ACTIVE-Trades und 32
portfolio_snapshot-Zeilen, waehrend das Konto tatsaechlich flach ist. Diese
Positionen von Hand zu schliessen wuerde erfundene Schlusskurse und
CLOSE-Events ohne trade_id erzeugen — genau die Ledger-Luecke, die gerade
geschlossen wurde. Der Reconciler holt die echten Kurse aus der
API-Historie. `finalize` verweigert deshalb den Dienst, solange das Buch in
der DB nicht leer ist.

  Ansehen:   PYTHONPATH=src python3 scripts/portfolio_reset.py status
  Stufe 1:   PYTHONPATH=src python3 scripts/portfolio_reset.py prepare --apply
  Stufe 2:   PYTHONPATH=src python3 scripts/portfolio_reset.py finalize \
                 --equity 10000.00 --deposit 2209.75 \
                 --note "Reset auf 10k, Copy-Neustart" --apply

Ohne --apply passiert nirgends etwas.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DB_PATH   = PROJECT_ROOT / "data" / "trading.db"
KILL_FLAG = PROJECT_ROOT / "data" / "kill_switch.flag"
RECS_PATH = PROJECT_ROOT / "data" / "llm_position_recommendations.json"

# Operativer Zustand — beschreibt das PORTFOLIO, nicht die Historie.
# Alles hier wird beim Reset neu gesetzt oder geloescht.
_STATE_CLEAR = ("DRAWDOWN_REASON", "REGIME_ALERTED")

# Wieviel darf --equity vom Live-Kontostand abweichen? Kursbewegung
# zwischen Abfrage und Eingabe ist auf einem FLACHEN Konto ausgeschlossen
# (keine offenen Positionen) — bleibt Waehrungsumrechnung. 25 USD auf
# 10.000 sind 0,25 %.
_EQUITY_TOLERANZ = 25.0

# Bewusst NICHT angefasst:
#   PEAK_EQUITY     — All-Time-Hoch, reines Reporting. Steht ohnehin auf
#                     10.000 und wuerde durch einen Reset auf 10.000 nur
#                     denselben Wert bekommen.
#   equity_history  — Eingang fuer ROLLING_PEAK (30 Tage). Der hoechste
#                     Altwert ist 8.724,59 und damit KLEINER als 10.000;
#                     der rollende Peak landet nach dem Reset bei der neuen
#                     Equity, der Drawdown bei 0 %. Die Zeilen loeschen
#                     wuerde am Regime nichts aendern, aber die
#                     Kontoentwicklung unlesbar machen.
#   trades / trade_events / signals / entry_quality_events — Historie.
#   alle data/llm_*.json ausser den Empfehlungen — Lernstand.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# Stufe 1: prepare
# ─────────────────────────────────────────────────────────────────────────────

def _prepare(apply: bool) -> int:
    print("── Stufe 1: prepare (offline) " + "─" * 33)
    todo = []

    if KILL_FLAG.exists():
        print(f"  Kill-Switch          bereits gesetzt ({KILL_FLAG.name})")
    else:
        todo.append("kill")
        print(f"  Kill-Switch          setzen -> {KILL_FLAG.name}")
        print("                       (erzwingt CRITICAL, blockt jeden Kauf)")

    n_recs = _stale_recs()
    if n_recs:
        todo.append("recs")
        print(f"  LLM-Empfehlungen     {n_recs} verwaiste Eintraege leeren")
        print("                       llm_execution.py FUEHRT diese Datei aus —")
        print("                       EXIT/TIGHTEN auf Symbole, die es nicht")
        print("                       mehr gibt bzw. auf neue Positionen")
        print("                       gleichen Namens.")
    else:
        print("  LLM-Empfehlungen     leer oder nicht vorhanden")

    if not todo:
        print("\n  Nichts zu tun.")
        return 0
    if not apply:
        print("\n  (DRY-RUN — nichts geaendert. Mit --apply ausfuehren.)")
        return 0

    if "kill" in todo:
        KILL_FLAG.write_text(
            f"Portfolio-Reset {_now_iso()} — scripts/portfolio_reset.py prepare\n"
            "Entfernen mit: rm data/kill_switch.flag\n",
            encoding="utf-8")
        print(f"\n  gesetzt: {KILL_FLAG}")
    if "recs" in todo:
        # Leere LISTE statt Loeschen: der Watchdog (Abschnitt 3 in
        # scripts/etoro_kill_switch_watchdog.sh) misst das mtime dieser
        # Datei. Fehlt sie, rechnet er mit 99999 Minuten und alarmiert
        # "LLM-LERNSCHLEIFE STALE" — ein Fehlalarm mitten im Reset.
        RECS_PATH.write_text("[]\n", encoding="utf-8")
        print(f"  geleert: {RECS_PATH.name} (frisches mtime, Watchdog ruhig)")
    return 0


def _stale_recs() -> int:
    if not RECS_PATH.exists():
        return 0
    try:
        d = json.loads(RECS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return len(d) if isinstance(d, list) else 0


# ─────────────────────────────────────────────────────────────────────────────
# Stufe 2: finalize
# ─────────────────────────────────────────────────────────────────────────────

def _book_state(db) -> dict:
    out = {}
    for k in ("CURRENT_EQUITY", "AVAILABLE_CASH", "CURRENT_REGIME",
              "RISK_SCALAR", "POSITION_COUNT", "DRAWDOWN_PCT",
              "ROLLING_PEAK", "PEAK_EQUITY", "EPOCH_START"):
        r = db.fetchone("SELECT value FROM system_state WHERE key=?", (k,))
        out[k] = r["value"] if r else None
    r = db.fetchone("SELECT COUNT(*) AS n FROM trades WHERE status='ACTIVE'")
    out["active_trades"] = int(r["n"]) if r else 0
    r = db.fetchone("SELECT COUNT(*) AS n FROM portfolio_snapshot")
    out["snapshot_rows"] = int(r["n"]) if r else 0
    return out


def _live_equity() -> tuple[float | None, str]:
    """Kontostand direkt bei eToro nachfragen. (wert, quelle_oder_fehler)

    Die Gegenprobe zu --equity. Ein zu hoch angegebener Wert ist keine
    Schoenheitsfehler-Klasse von Fehler: `record_equity_snapshot()` schreibt
    ihn in `equity_history`, `get_rolling_peak()` zieht daraus den 30-Tage-
    Peak, und der naechste echte Portfolio-Lauf misst dann den Abstand
    zwischen Phantom-Peak und Wirklichkeit als Drawdown. Bei 10.000
    angegeben und 7.790 real waeren das 22 % — CRITICAL, also sofortiger
    Kaufstopp auf einem frisch zurueckgesetzten Konto.
    """
    try:
        import os
        env = {}
        with open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
        from bot.api.client import EToroClient, ClientConfig
        from bot.workers.reconciler import _extract_equity
        cli = EToroClient(env.get("ETORO_BOT_API_KEY", ""),
                          env.get("ETORO_BOT_USER_KEY", ""), ClientConfig())
        return float(_extract_equity(cli.get_portfolio())), "eToro /trading/info/real/pnl"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:120]}"


def _finalize(apply: bool, equity: float | None, deposit: float | None,
              note: str, force: bool) -> int:
    from bot.db.connection import DB
    from bot.db.repo import CapitalRepo, StateRepo

    with DB(DB_PATH) as db:
        st = _book_state(db)
        print("── Stufe 2: finalize " + "─" * 42)
        print(f"  ACTIVE-Trades in der DB      {st['active_trades']:>6}")
        print(f"  portfolio_snapshot-Zeilen    {st['snapshot_rows']:>6}")

        offen = st["active_trades"] + st["snapshot_rows"]
        if offen and not force:
            print("\n  ABBRUCH: das Buch in der Datenbank ist nicht leer.")
            print("  Erst den Reconciler laufen lassen — er holt die echten")
            print("  Schlusskurse aus der API-Historie und schreibt CLOSE-")
            print("  Events mit korrektem trade_id-Bezug. Von Hand geschlossen")
            print("  waeren die Kurse erfunden und die Ledger-Luecke zurueck.")
            print("\n  (--force uebergeht das. Nur wenn die 30 Positionen auf")
            print("   anderem Weg sauber abgeschlossen wurden.)")
            return 2

        if equity is None:
            print("\n  ABBRUCH: --equity fehlt.")
            print("  Der Kontostand wird NICHT geraten. Ein zu hoch gesetzter")
            print("  Wert landet ueber record_equity_snapshot() im rollenden")
            print("  30-Tage-Peak; der naechste echte Portfolio-Lauf misst")
            print("  dann einen Drawdown, den es nie gab, und das Regime")
            print("  faellt sofort auf DEFENSIVE oder CRITICAL.")
            return 2

        live, quelle = _live_equity()
        if live is None:
            print(f"\n  Live-Abgleich nicht moeglich  ({quelle})")
            if not force:
                print("\n  ABBRUCH: --equity bleibt ungeprueft.")
                print("  finalize laeuft ohnehin erst NACH einem erfolgreichen")
                print("  Reconciler-Durchgang — wenn der ging, geht auch diese")
                print("  Abfrage. Geht sie nicht, stimmt etwas anderes nicht.")
                print("  (--force uebergeht die Pruefung bewusst.)")
                return 2
            print("  --force: ungeprueft weiter.")
        else:
            abw = abs(live - equity)
            print(f"\n  Live-Equity ({quelle})")
            print(f"    laut Konto                 ${live:>12,.2f}")
            print(f"    laut --equity              ${equity:>12,.2f}")
            print(f"    Abweichung                 ${abw:>12,.2f}")
            if abw > _EQUITY_TOLERANZ and not force:
                print(f"\n  ABBRUCH: mehr als ${_EQUITY_TOLERANZ:,.2f} Abweichung.")
                print("  Der angegebene Wert wuerde als Phantom-Peak in")
                print("  equity_history landen und beim naechsten echten Lauf")
                print("  einen Drawdown erzeugen, den es nie gab.")
                return 2

        repo  = CapitalRepo(db)
        basis = repo.base()
        print(f"\n  Kapitalbasis bisher          ${basis:>12,.2f}")
        print(f"  Equity laut Konto (--equity) ${equity:>12,.2f}")

        from bot.core.trade_pnl import reconcile
        r = reconcile(db)
        erwartet = (r["start_equity"] + r["realized_usd"]
                    + r.get("unattributed_usd", 0.0) + r["unrealized_usd"])
        print(f"  erwartete Equity (Audit)     ${erwartet:>12,.2f}")
        vorschlag = round(equity - erwartet, 2)
        print(f"  -> rechnerische Differenz    ${vorschlag:>+12,.2f}")
        print("     (Vorschlag, KEIN Automatismus: die Differenz enthaelt")
        print("      auch das ungeklaerte Residuum. Nur echtes ein- oder")
        print("      ausgezahltes Geld gehoert ins Ledger.)")

        if deposit is None:
            print("\n  ABBRUCH: --deposit fehlt (0 ist erlaubt und explizit).")
            return 2
        if deposit and not note:
            print("\n  ABBRUCH: --note ist bei einer echten Buchung Pflicht.")
            return 2

        epoch = _now_iso()
        print(f"\n  Buchung ins Ledger           ${deposit:>+12,.2f}   {note or '—'}")
        print(f"  neue Basis                   ${basis + deposit:>12,.2f}")
        print(f"  EPOCH_START                  {epoch}")
        print(f"  EPOCH_START_EQUITY           ${equity:>12,.2f}")

        print("\n  Operativer Zustand:")
        _plan = [
            ("CURRENT_EQUITY",         st["CURRENT_EQUITY"],   f"{equity}"),
            ("AVAILABLE_CASH",         st["AVAILABLE_CASH"],   f"{equity}"),
            ("POSITION_COUNT",         st["POSITION_COUNT"],   "0"),
            ("DAY_START_EQUITY",       None,                   f"{equity}"),
            ("DAY_START_DATE",         None,                   epoch[:10]),
            ("ROLLING_PEAK",           st["ROLLING_PEAK"],     f"{equity}"),
            ("DRAWDOWN_PCT",           st["DRAWDOWN_PCT"],     "0.0000"),
            ("CURRENT_REGIME",         st["CURRENT_REGIME"],   "NORMAL"),
            ("RISK_SCALAR",            st["RISK_SCALAR"],      "1.0"),
            ("HIGH_WATERMARK_REACHED", None,                   "true"),
        ]
        for k, alt, neu in _plan:
            print(f"    {k:<24} {str(alt or '—'):>22}  ->  {neu}")
        print(f"    {'geloescht':<24} {', '.join(_STATE_CLEAR)}")
        print(f"    {'PEAK_EQUITY':<24} {str(st['PEAK_EQUITY']):>22}  ->  "
              "unveraendert (All-Time, nur Reporting)")

        if not apply:
            print("\n  (DRY-RUN — nichts geschrieben. Mit --apply ausfuehren.)")
            return 0

        if deposit:
            eid = repo.add(deposit, note, None)
            if eid is None:
                print("\n  FEHLER: Kapitalbuchung fehlgeschlagen — Abbruch.")
                return 1
            print(f"\n  gebucht (id={eid})")

        state = StateRepo(db)
        state.set("EPOCH_START", epoch)
        state.set("EPOCH_START_EQUITY", f"{equity}")
        state.set("EPOCH_NOTE", note or "Portfolio-Reset")
        for k, _alt, neu in _plan:
            state.set(k, neu)
        for k in _STATE_CLEAR:
            db.execute("DELETE FROM system_state WHERE key=?", (k,))
        print("  Zustand zurueckgesetzt.")
        print("\n  Der Kill-Switch bleibt gesetzt. Entfernen mit:")
        print("    rm data/kill_switch.flag")
    return 0


# ─────────────────────────────────────────────────────────────────────────────

def _status() -> int:
    from bot.db.connection import DB
    from bot.core.regime import apply_config, get_regime_params, get_risk_scalar

    # _REGIME_PARAMS traegt Defaults; die Config ueberschreibt sie erst beim
    # apply_config() der Worker. Ohne diesen Aufruf zeigte die Tabelle fuer
    # DEFENSIVE min_conviction=HIGH an, waehrend der Bot real gegen MEDIUM
    # prueft (regime.min_conviction.DEFENSIVE in config.yaml) — eine Zahl,
    # die genau da falsch ist, wo sie eine Entscheidung stuetzen soll.
    try:
        import yaml
        apply_config(yaml.safe_load(
            (PROJECT_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"  (Config nicht geladen: {exc} — Tabelle zeigt Code-Defaults)")

    with DB(DB_PATH) as db:
        st = _book_state(db)
    print("── Status " + "─" * 53)
    print(f"  Kill-Switch                  "
          f"{'GESETZT' if KILL_FLAG.exists() else 'nicht gesetzt'}")
    print(f"  verwaiste LLM-Empfehlungen   {_stale_recs()}")
    print(f"  ACTIVE-Trades in der DB      {st['active_trades']}")
    print(f"  portfolio_snapshot-Zeilen    {st['snapshot_rows']}")
    print(f"  Epoche                       {st['EPOCH_START'] or 'keine'}")

    alt, neu = st["CURRENT_REGIME"] or "NORMAL", "NORMAL"
    print(f"\n── Regime-Wechsel {alt} -> {neu} " + "─" * 20)
    pa, pn = get_regime_params(alt), get_regime_params(neu)
    print(f"  {'Parameter':<22}{alt:>14}{neu:>14}")
    for key in ("min_conviction", "signal_floor_usd", "max_trade_pct",
                "cash_min_pct", "buy_aggressiveness", "allow_pyramiding"):
        print(f"  {key:<22}{str(pa[key]):>14}{str(pn[key]):>14}")
    print(f"  {'risk_scalar':<22}{get_risk_scalar(alt):>14}"
          f"{get_risk_scalar(neu):>14}")
    print("\n  Das ist die eigentliche Folge des Resets: bei 0 % Drawdown")
    print("  laeuft der Bot in NORMAL. Positionen werden groesser, der")
    print("  Signal-Floor faellt, und LOW-Conviction-Signale werden wieder")
    print("  handelbar. Eine bewusste Entscheidung, kein Nebeneffekt.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    p1 = sub.add_parser("prepare")
    p1.add_argument("--apply", action="store_true")
    p2 = sub.add_parser("finalize")
    p2.add_argument("--equity", type=float, default=None,
                    help="tatsaechlicher Kontostand nach dem Reset (USD)")
    p2.add_argument("--deposit", type=float, default=None,
                    help="echte Kapitalbewegung, 0 wenn keine")
    p2.add_argument("--note", default="")
    p2.add_argument("--force", action="store_true",
                    help="offenes Buch in der DB uebergehen")
    p2.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.cmd == "prepare":
        return _prepare(args.apply)
    if args.cmd == "finalize":
        return _finalize(args.apply, args.equity, args.deposit,
                         args.note, args.force)
    return _status()


if __name__ == "__main__":
    raise SystemExit(main())
