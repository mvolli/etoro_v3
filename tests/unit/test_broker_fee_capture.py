"""feat/broker-fee-capture (2026-09-12): eToros Rohfelder mitschreiben.

Messung vom 12.09.2026 gegen das Live-Konto:
  Equity 9.892,29 gegen EPOCH_START_EQUITY 10.000  ->  -107,71
  davon realisiert (Broker-netProfit, 12 Closes)   ->    -7,36
  unerklaerte Luecke                                ->   -88,91
Die eigene Schaetzung des Bots (`_fill_cost`, halber Spread) buchte im
selben Fenster $6,10 — Faktor 14 daneben.

`totalExternalFees` ist KEIN Dollarbetrag, sondern ein Satz in Prozent
(1.0 bei 42 Positionen, 2.0 bei 11 Fremdwaehrungswerten, 0.66 Krypto,
0.25 bei einem). Satz x Einsatz ergibt $87,07 gegen die Luecke von $88,91.
Die Summe der Rohwerte ($65,57) ist als Dollarbetrag bedeutungslos —
genau diese Verwechslung hat die Untersuchung zuerst fehlgeleitet, daher
heisst die Spalte broker_fee_pct und nicht broker_fee_usd.

Hier wird nur erfasst. Das Feld fliesst in KEINE Geldentscheidung.
"""
from __future__ import annotations

import pytest

from bot.db.connection import DB
from bot.db.repo import PortfolioRepo
from bot.workers.reconciler import _build_snapshot_record


@pytest.fixture()
def db(tmp_path):
    d = DB(db_path=tmp_path / "t.db")
    # bewusst das ALTE Schema ohne die neuen Spalten
    d.execute("""
        CREATE TABLE portfolio_snapshot (
            api_position_id TEXT PRIMARY KEY, instrument_id INTEGER, symbol TEXT,
            is_buy INTEGER NOT NULL DEFAULT 1, amount_usd REAL, open_price REAL,
            current_price REAL, unrealized_pnl REAL, unrealized_pnl_pct REAL,
            stop_loss_rate REAL, is_no_stop_loss INTEGER DEFAULT 0,
            last_synced TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    return d


def _pos(**kw):
    base = {
        "positionID": 3576530306, "instrumentID": 1001, "amount": 166.24,
        "openRate": 333.44, "units": 0.4986, "isNoStopLoss": False,
        "stopLossRate": 321.37, "totalExternalFees": 1.0,
        "openConversionRate": 1.0,
        "unrealizedPnL": {"pnL": -0.63, "closeRate": 332.18},
    }
    base.update(kw)
    return base


# ── Migration auf der Live-DB ────────────────────────────────────────────────

def test_repo_ruestet_fehlende_spalten_nach(db):
    vorher = {r[1] for r in db.fetchall("PRAGMA table_info(portfolio_snapshot)")}
    assert "broker_fee_pct" not in vorher
    PortfolioRepo(db)
    nachher = {r[1] for r in db.fetchall("PRAGMA table_info(portfolio_snapshot)")}
    assert {"broker_fee_pct", "open_conversion_rate"} <= nachher


def test_zweimal_anlegen_ist_folgenlos(db):
    PortfolioRepo(db)
    PortfolioRepo(db)          # darf nicht an "duplicate column" scheitern
    cols = [r[1] for r in db.fetchall("PRAGMA table_info(portfolio_snapshot)")]
    assert cols.count("broker_fee_pct") == 1


# ── Rohfelder landen unveraendert in der Zeile ───────────────────────────────

def test_gebuehrensatz_wird_uebernommen(db):
    repo = PortfolioRepo(db)
    record, _ = _build_snapshot_record(_pos(), {1001: "AAPL"})
    repo.upsert(record)
    row = db.fetchone("SELECT broker_fee_pct, open_conversion_rate "
                      "FROM portfolio_snapshot WHERE api_position_id='3576530306'")
    assert row["broker_fee_pct"] == 1.0
    assert row["open_conversion_rate"] == 1.0


def test_fremdwaehrung_traegt_satz_2_und_echten_umrechnungskurs(db):
    # Live-Beispiel: JPY-Wert, openConversionRate ~0.00647, Satz 2.0
    repo = PortfolioRepo(db)
    record, _ = _build_snapshot_record(
        _pos(positionID=3575723392, instrumentID=14567, amount=169.89,
             totalExternalFees=2.0, openConversionRate=0.00647337),
        {14567: "TEST.T"})
    repo.upsert(record)
    row = db.fetchone("SELECT broker_fee_pct, open_conversion_rate, amount_usd "
                      "FROM portfolio_snapshot WHERE api_position_id='3575723392'")
    assert row["broker_fee_pct"] == 2.0
    assert row["open_conversion_rate"] == pytest.approx(0.00647337)
    # Satz x Einsatz = Dollarbetrag; der Satz selbst ist es NICHT.
    assert row["broker_fee_pct"] / 100.0 * row["amount_usd"] == pytest.approx(3.3978)


def test_fehlende_felder_bleiben_null_statt_null_komma_null(db):
    """Ein altes Konto ohne die Felder darf nicht als "Gebuehr 0%" gelten —
    das waere eine Aussage, die die API gar nicht gemacht hat."""
    repo = PortfolioRepo(db)
    p = _pos(); p.pop("totalExternalFees"); p.pop("openConversionRate")
    record, _ = _build_snapshot_record(p, {1001: "AAPL"})
    repo.upsert(record)
    row = db.fetchone("SELECT broker_fee_pct, open_conversion_rate FROM portfolio_snapshot")
    assert row["broker_fee_pct"] is None
    assert row["open_conversion_rate"] is None


def test_unbrauchbarer_wert_kippt_die_zeile_nicht(db):
    repo = PortfolioRepo(db)
    record, _ = _build_snapshot_record(_pos(totalExternalFees="n/a"), {1001: "AAPL"})
    repo.upsert(record)
    row = db.fetchone("SELECT broker_fee_pct, amount_usd FROM portfolio_snapshot")
    assert row["broker_fee_pct"] is None
    assert row["amount_usd"] == 166.24     # der Rest der Zeile bleibt intakt


def test_erneuter_sync_aktualisiert_statt_zu_duplizieren(db):
    repo = PortfolioRepo(db)
    r1, _ = _build_snapshot_record(_pos(), {1001: "AAPL"})
    repo.upsert(r1)
    r2, _ = _build_snapshot_record(_pos(totalExternalFees=2.0), {1001: "AAPL"})
    repo.upsert(r2)
    rows = db.fetchall("SELECT broker_fee_pct FROM portfolio_snapshot")
    assert len(rows) == 1 and rows[0]["broker_fee_pct"] == 2.0
