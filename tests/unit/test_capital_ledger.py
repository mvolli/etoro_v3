#!/usr/bin/env python3
"""fix/capital-ledger (2026-09-10).

`reconcile()` hatte 10.000 USD fest verdrahtet und unterstellte, dass seit
dem Kontostart weder ein- noch ausgezahlt wurde. Bei der ersten Einzahlung
waere der Bericht STILL falsch geworden: frisches Kapital haette wie
verschwundene Kosten ausgesehen.
"""
from __future__ import annotations

import sqlite3

import pytest

from bot.core.trade_pnl import reconcile
from bot.db.connection import DB
from bot.db.repo import CapitalRepo

_SCHEMA = """
CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE portfolio_snapshot (api_position_id TEXT PRIMARY KEY, unrealized_pnl REAL);
CREATE TABLE trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, event_at TEXT,
    close_pct REAL, event_type TEXT, amount_usd REAL, pnl_pct REAL, pnl_usd REAL
);
"""


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO system_state VALUES ('CURRENT_EQUITY','9000.0')")
    con.commit()
    con.close()
    return DB(p)


def test_seed_setzt_das_startkapital(db):
    assert CapitalRepo(db).base() == pytest.approx(10_000.0)


def test_reconcile_nimmt_die_ledger_basis(db):
    """Ohne Buchung: Equity 9.000 gegen Basis 10.000 -> Residuum -1.000."""
    assert reconcile(db)["residual_usd"] == pytest.approx(-1000.0)


def test_einzahlung_verschiebt_das_residuum_NICHT(db):
    """Der Kernfall: frisches Geld darf nicht wie gefundene Kosten wirken.

    Equity steigt um 1.000 UND die Basis steigt um 1.000 -> Residuum bleibt.
    """
    vorher = reconcile(db)["residual_usd"]
    CapitalRepo(db).add(1000.0, "Auffuellung")
    db.execute("UPDATE system_state SET value='10000.0' WHERE key='CURRENT_EQUITY'")
    assert reconcile(db)["residual_usd"] == pytest.approx(vorher)


def test_ohne_ledger_waere_die_einzahlung_ein_scheingewinn(db):
    """Gegenprobe: mit der alten festen Basis saehe es aus wie -0 Residuum."""
    CapitalRepo(db).add(1000.0, "Auffuellung")
    db.execute("UPDATE system_state SET value='10000.0' WHERE key='CURRENT_EQUITY'")
    mit_ledger = reconcile(db)["residual_usd"]
    alt_verdrahtet = reconcile(db, start_equity=10_000.0)["residual_usd"]
    assert mit_ledger == pytest.approx(-1000.0)
    assert alt_verdrahtet == pytest.approx(0.0)     # der stille Fehler
    assert mit_ledger != alt_verdrahtet


def test_auszahlung_wirkt_umgekehrt(db):
    CapitalRepo(db).add(-2000.0, "Entnahme")
    assert CapitalRepo(db).base() == pytest.approx(8000.0)
    assert reconcile(db)["residual_usd"] == pytest.approx(1000.0)


def test_expliziter_wert_gewinnt_weiterhin(db):
    """Tests und Sonderfaelle duerfen die Basis weiter vorgeben."""
    assert reconcile(db, start_equity=9000.0)["residual_usd"] == pytest.approx(0.0)


def test_seed_laeuft_nur_einmal(db):
    CapitalRepo(db); CapitalRepo(db); CapitalRepo(db)
    assert len(CapitalRepo(db).all()) == 1


def test_leeres_ledger_liefert_None_statt_null(tmp_path):
    """Ein stilles 0.0 waere als Startkapital katastrophal."""
    import sqlite3
    q = tmp_path / "leer.db"
    con = sqlite3.connect(q)
    con.executescript(_SCHEMA)
    con.execute("CREATE TABLE capital_events (id INTEGER PRIMARY KEY,"
                " occurred_at TEXT, amount_usd REAL, note TEXT,"
                " created_at TEXT)")
    con.commit(); con.close()
    d = DB(q)
    repo = CapitalRepo.__new__(CapitalRepo)   # ohne _ensure_schema/Seed
    repo.db = d
    assert repo.base() is None


def test_defekte_db_liefert_None(db):
    class _Broken:
        def fetchone(self, *a, **k):
            raise RuntimeError("weg")
    repo = CapitalRepo.__new__(CapitalRepo)
    repo.db = _Broken()
    assert repo.base() is None
