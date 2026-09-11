#!/usr/bin/env python3
"""feat/portfolio-reset (2026-09-10).

Der Reset wirft das PORTFOLIO weg, nicht das Wissen. Zwei Sichten muessen
sich deshalb sauber trennen lassen:

  Audit-Sicht  (since=None) — Basis aus dem Kapital-Ledger, realisiert ueber
                die ganze Kontohistorie. Das Residuum behaelt sein
                Gedaechtnis.
  Epoch-Sicht  (since=ISO)  — Basis EPOCH_START_EQUITY, realisiert nur ab
                dem Reset.

Die gefaehrliche Kombination ist epoch-gefilterte Realisierung gegen
kumulative Kapitalbasis: das Residuum bekaeme dann die gesamte
Vorgeschichte aufgebuerdet. Beide Fenster muessen zusammen umziehen.
"""
from __future__ import annotations

import sqlite3

import pytest

from bot.core.trade_pnl import (realized_by_trade, realized_unattributed,
                                reconcile)
from bot.db.connection import DB

_SCHEMA = """
CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE portfolio_snapshot (api_position_id TEXT PRIMARY KEY, unrealized_pnl REAL);
CREATE TABLE capital_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT, amount_usd REAL,
    note TEXT, created_at TEXT
);
CREATE TABLE trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, event_at TEXT,
    close_pct REAL, event_type TEXT, amount_usd REAL, pnl_pct REAL, pnl_usd REAL,
    cost_usd REAL
);
"""

# GENAU das Format, in dem trade_events.event_at real steht (repo._utcnow).
# Die erste Fassung dieses Tests nahm isoformat() mit 'T' und Offset — und
# stimmte mit sich selbst ueberein, weil die Fixture dasselbe falsche Format
# einsetzte. Ein Test, der beide Seiten gleich falsch macht, prueft nichts.
EPOCH = "2026-09-10 21:00:00"


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO capital_events (occurred_at, amount_usd, note)"
                " VALUES ('2026-06-24 19:44:00', 10000.0, 'Start')")
    # vor der Epoche: -300 USD realisiert, davon -50 ohne Trade-Bezug
    con.execute("INSERT INTO trade_events (trade_id, event_at, close_pct,"
                " event_type, amount_usd, pnl_pct, pnl_usd)"
                " VALUES (1, '2026-08-01 10:00:00', 100, 'CLOSE',"
                " 500.0, -50.0, -250.0)")
    con.execute("INSERT INTO trade_events (trade_id, event_at, close_pct,"
                " event_type, amount_usd, pnl_pct, pnl_usd)"
                " VALUES (NULL, '2026-08-02 10:00:00', 100, 'CLOSE',"
                " 100.0, -50.0, -50.0)")
    # nach der Epoche: +120 USD realisiert, davon +20 ohne Trade-Bezug
    con.execute("INSERT INTO trade_events (trade_id, event_at, close_pct,"
                " event_type, amount_usd, pnl_pct, pnl_usd)"
                " VALUES (2, '2026-09-11 10:00:00', 100, 'CLOSE',"
                " 400.0, 25.0, 100.0)")
    con.execute("INSERT INTO trade_events (trade_id, event_at, close_pct,"
                " event_type, amount_usd, pnl_pct, pnl_usd)"
                " VALUES (NULL, '2026-09-11 11:00:00', 100, 'CLOSE',"
                " 80.0, 25.0, 20.0)")
    con.execute("INSERT INTO system_state VALUES ('CURRENT_EQUITY','10120.0')")
    con.execute("INSERT INTO system_state VALUES ('EPOCH_START_EQUITY','10000.0')")
    con.commit()
    con.close()
    return DB(p)


# ── die Filter selbst ────────────────────────────────────────────────────────

def test_realized_by_trade_ohne_since_ist_kumulativ(db):
    """Default-Verhalten unveraendert — das ist der Kelly-Pfad."""
    r = realized_by_trade(db)
    assert set(r) == {1, 2}
    assert sum(v["realized_usd"] for v in r.values()) == pytest.approx(-150.0)


def test_realized_by_trade_mit_since_nur_ab_epoche(db):
    r = realized_by_trade(db, since=EPOCH)
    assert set(r) == {2}
    assert r[2]["realized_usd"] == pytest.approx(100.0)


def test_unattributed_ohne_since_ist_kumulativ(db):
    u = realized_unattributed(db)
    assert u["tranchen"] == 2
    assert u["realized_usd"] == pytest.approx(-30.0)


def test_unattributed_mit_since_nur_ab_epoche(db):
    u = realized_unattributed(db, since=EPOCH)
    assert u["tranchen"] == 1
    assert u["realized_usd"] == pytest.approx(20.0)


# ── die beiden Sichten ───────────────────────────────────────────────────────

def test_audit_sicht_behaelt_das_gedaechtnis(db):
    """10.000 Basis + (-150) + (-30) = 9.820 erwartet gegen 10.120 Equity."""
    r = reconcile(db)
    assert r["since"] is None
    assert r["start_equity"] == pytest.approx(10_000.0)
    assert r["realized_usd"] == pytest.approx(-150.0)
    assert r["unattributed_usd"] == pytest.approx(-30.0)
    assert r["residual_usd"] == pytest.approx(300.0)


def test_epoch_sicht_startet_sauber(db):
    """10.000 Epoch-Basis + 100 + 20 = 10.120 == Equity -> Residuum 0."""
    r = reconcile(db, since=EPOCH)
    assert r["since"] == EPOCH
    assert r["start_equity"] == pytest.approx(10_000.0)
    assert r["realized_usd"] == pytest.approx(100.0)
    assert r["unattributed_usd"] == pytest.approx(20.0)
    assert r["residual_usd"] == pytest.approx(0.0)


def test_epoch_sicht_zieht_die_basis_mit(db):
    """Der eigentliche Fehler, gegen den dieser Test steht.

    Wuerde die Epoch-Sicht die KUMULATIVE Ledger-Basis nehmen, kaeme
    dasselbe Residuum heraus wie in der Audit-Sicht — die Vorgeschichte
    waere der neuen Epoche angelastet. Beide muessen sich unterscheiden.
    """
    audit = reconcile(db)
    epoch = reconcile(db, since=EPOCH)
    assert audit["residual_usd"] != epoch["residual_usd"]
    assert epoch["residual_usd"] == pytest.approx(0.0)


def test_epoch_sicht_ohne_marker_bricht_ab(db):
    """Lieber ein Fehler als eine still falsche Zahl."""
    db.execute("DELETE FROM system_state WHERE key='EPOCH_START_EQUITY'")
    with pytest.raises(ValueError, match="EPOCH_START_EQUITY"):
        reconcile(db, since=EPOCH)


def test_expliziter_start_equity_gewinnt_auch_mit_since(db):
    r = reconcile(db, start_equity=5_000.0, since=EPOCH)
    assert r["start_equity"] == pytest.approx(5_000.0)


# ── Zeitformat ───────────────────────────────────────────────────────────────

def test_epoche_am_reset_tag_verschluckt_nichts(db):
    """Der Grenzfall, an dem ein falsches Format nur EINEN Tag lang wehtut.

    `event_at` steht als "%Y-%m-%d %H:%M:%S" in der DB, der Vergleich ist
    lexikalisch, und ' ' (0x20) sortiert vor 'T' (0x54). Eine mit
    isoformat() erzeugte Epoche waere groesser als JEDES Ereignis desselben
    Tages — die ersten Trades nach dem Reset waeren still aus der Rechnung
    gefallen, ab dem Folgetag haette wieder alles gestimmt. Genau die Sorte
    Fehler, die man im Betrieb nicht bemerkt.
    """
    db.execute("INSERT INTO trade_events (trade_id, event_at, close_pct,"
               " event_type, amount_usd, pnl_pct, pnl_usd)"
               " VALUES (3, '2026-09-10 22:00:00', 100, 'CLOSE',"
               " 200.0, 5.0, 10.0)")
    # Epoche 21:00 desselben Tages, im DB-Format -> Ereignis 22:00 zaehlt
    assert 3 in realized_by_trade(db, since=EPOCH)
    # zur Gegenprobe: mit dem 'T'-Format faellt es heraus
    assert 3 not in realized_by_trade(db, since="2026-09-10T21:00:00+00:00")


def test_now_iso_liefert_das_db_format():
    """Die Quelle des Epoch-Markers, direkt geprueft."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "portfolio_reset",
        Path(__file__).resolve().parents[2] / "scripts" / "portfolio_reset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ts = mod._now_iso()
    assert "T" not in ts and "+" not in ts
    assert len(ts) == 19 and ts[10] == " "


# ── Kostenfenster muss zum Residuum passen ──────────────────────────────────

def test_recorded_costs_folgt_dem_epoch_fenster(db):
    """fix/epoch-kostenfenster (2026-09-12).

    Der Tagesbericht vom 2026-09-11 stellte ein epoch-bezogenes Residuum
    (-87.92 USD seit dem Reset) neben kumulative Kostenzahlen aus der
    ganzen Kontohistorie: "Fills gesamt: 1775 -> $0.05 je Fill" und
    "davon im Residuum erklaert: 12 %". Beides eine Quote aus zwei
    verschiedenen Zeitraeumen. Richtig gerechnet waren es 69 Fills und
    $1.27 je Fill — Faktor 25.
    """
    from bot.core.trade_pnl import recorded_costs
    db.execute("UPDATE trade_events SET cost_usd = 1.0 WHERE id IS NOT NULL")

    kumulativ = recorded_costs(db)
    epoche = recorded_costs(db, since=EPOCH)

    assert kumulativ["fills_gesamt"] == 4
    assert epoche["fills_gesamt"] == 2, "Kostenfenster ignoriert die Epoche"
    assert epoche["cost_usd"] < kumulativ["cost_usd"]


def test_recorded_costs_ohne_since_unveraendert(db):
    """Die kumulative Sicht bleibt, was sie war."""
    from bot.core.trade_pnl import recorded_costs
    assert recorded_costs(db)["fills_gesamt"] == 4
