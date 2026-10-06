"""fix/pc-shadow-amount-contract (2026-10-06): amount_usd ist der Wert ZUM
ENTSCHEIDUNGSZEITPUNKT, nicht der Restwert danach.

Die Spaltenbeschreibung lautete "current position value (USD)" und hat genau
diese Verwechslung ausgeloest — die Rueckfrage lautete, ob bei G24.DE der
Live-Snapshot ($45,00) statt des Ledger-Werts ($60,00) gewinnen sollte.

Der Live-Verlauf entscheidet es auf die Sekunde:

    2026-09-29 10:07:07  OPEN                       $60,00
    2026-10-02 12:41:39  Shadow-Entscheidung (25%)  $60,00   <- Ledger
    2026-10-02 12:41:40  PARTIAL_CLOSE ausgefuehrt  -$15,00
    2026-10-06 09:21:27  portfolio_snapshot          $45,00

Der Snapshot steht auf $45,00, WEIL diese Entscheidung eine Sekunde spaeter
ausgefuehrt wurde. Wer ihn eintraegt, protokolliert eine Entscheidung auf
einer Position, die durch eben diese Entscheidung schon verkleinert war.
"""
from __future__ import annotations

import pytest

from bot.core import partial_close_policy as pcp
from bot.db.connection import DB


@pytest.fixture()
def db(tmp_path):
    # DB-Wrapper, nicht rohes sqlite3: _dedup_refresh ruft db.fetchone(sql),
    # das eine Connection nicht kennt — sonst greift fail-open und der
    # Dedup-Pfad wird stillschweigend uebersprungen.
    conn = DB(tmp_path / "t.db")
    conn._get_persistent()
    pcp.ensure_table(conn)
    return conn


def _zeile(db):
    r = db.fetchone("SELECT amount_usd, pnl_usd_est, close_pct FROM "
                    "partial_close_shadow ORDER BY id DESC LIMIT 1")
    return dict(r) if r else None


# ── Der Vertrag ─────────────────────────────────────────────────────────────

def test_entscheidungswert_wird_protokolliert_nicht_der_restwert(db):
    """G24.DE: $60,00 zum Entscheidungszeitpunkt, nicht $45,00 danach."""
    pcp.record_decision(db, mode="current", path="llm", symbol="G24.DE",
                        position_id="3590440419", instrument_id=1,
                        pnl_pct=2.52, close_pct=25.0, amount_usd=60.0,
                        allowed=True, reason=None)
    assert _zeile(db)["amount_usd"] == 60.0


def test_schaetzung_haengt_am_entscheidungswert(db):
    """60 x 25% x 2.52% = $0,378 — mit dem Restwert waeren es $0,284."""
    pcp.record_decision(db, mode="current", path="llm", symbol="G24.DE",
                        position_id="3590440419", instrument_id=1,
                        pnl_pct=2.52, close_pct=25.0, amount_usd=60.0,
                        allowed=True, reason=None)
    assert _zeile(db)["pnl_usd_est"] == pytest.approx(0.378, abs=0.001)

    pcp.record_decision(db, mode="current", path="llm", symbol="G24.DE",
                        position_id="XX", instrument_id=1,
                        pnl_pct=2.52, close_pct=25.0, amount_usd=45.0,
                        allowed=True, reason=None)
    assert _zeile(db)["pnl_usd_est"] == pytest.approx(0.284, abs=0.001)


def test_eine_spaetere_teilschliessung_aendert_die_zeile_nicht(db):
    """Der Ledger haelt den Zeitpunkt fest — nachtraegliche Ausfuehrung nicht."""
    pcp.record_decision(db, mode="current", path="llm", symbol="G24.DE",
                        position_id="3590440419", instrument_id=1,
                        pnl_pct=2.52, close_pct=25.0, amount_usd=60.0,
                        allowed=True, reason=None)
    vorher = _zeile(db)
    # Welt bewegt sich weiter: Position ist jetzt $45 wert. Kein Re-Record.
    assert _zeile(db) == vorher


# ── Die Dedup-Regel darf den Vertrag nicht aushebeln ────────────────────────

def test_dedup_aktualisiert_auf_den_neuen_entscheidungswert(db):
    """Gleiche Sprosse erneut: der Wert des NEUEN Entscheidungszeitpunkts."""
    for amt in (60.0, 58.0):
        pcp.record_decision(db, mode="current", path="trailing", symbol="X",
                            position_id="P1", instrument_id=1,
                            pnl_pct=5.0, close_pct=25.0, amount_usd=amt,
                            allowed=True, reason=None)
    n = db.fetchone("SELECT COUNT(*) c FROM partial_close_shadow")["c"]
    assert n == 1, "gleiche Sprosse darf keine zweite Zeile erzeugen"
    assert _zeile(db)["amount_usd"] == 58.0


def test_echter_pnl_sprung_legt_eine_neue_stufe_an(db):
    """> dedup_pnl_tolerance_pct: echte neue Entscheidung, eigene Zeile."""
    pcp.record_decision(db, mode="current", path="trailing", symbol="X",
                        position_id="P1", instrument_id=1,
                        pnl_pct=5.0, close_pct=25.0, amount_usd=60.0,
                        allowed=True, reason=None)
    pcp.record_decision(db, mode="current", path="trailing", symbol="X",
                        position_id="P1", instrument_id=1,
                        pnl_pct=12.0, close_pct=25.0, amount_usd=60.0,
                        allowed=True, reason=None)
    n = db.fetchone("SELECT COUNT(*) c FROM partial_close_shadow")["c"]
    assert n == 2


# ── Die Schemazeile sagt jetzt, was gilt ────────────────────────────────────

def test_schema_benennt_den_entscheidungszeitpunkt():
    """Verhindert den Rueckfall auf 'current position value'."""
    assert "AT DECISION TIME" in pcp._TABLE
    assert "current position value" not in pcp._TABLE
