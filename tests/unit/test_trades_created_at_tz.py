"""fix/created-at-doppelte-utc (2026-09-12).

Dritter Fund derselben Fehlerklasse: die Live-Tabelle traegt noch
DEFAULT (datetime('now','utc')) aus der Zeit vor c9eabc8. SQLites
`datetime('now')` IST bereits UTC — der 'utc'-Modifier liest den Wert als
Ortszeit und rechnet ein ZWEITES Mal um. Im Sommer landet jede so
geschriebene Zeile zwei Stunden in der Vergangenheit.

`signals.generated_at` (2026-08-28) und `system_log.ts` (2026-09-10) sind
so behoben worden; `trades.created_at` blieb uebrig.

Gemessen am 12.09.2026: approved_at minus created_at ist bei allen 224
Epochen-Trades exakt 120,0 Minuten. Kein Verzug, ein Uhrversatz.

Zwei Verbraucher lesen created_at als "echtes Alter":

  market_closed_too_old (BELEGTE Wirkung): der konfigurierte
  market_closed_max_hours=4 wurde faktisch zu 2 Stunden. 323 Trades
  tragen "Markt >4h geschlossen — Signal veraltet, verworfen", obwohl
  ihr wahres Alter unter 4 Stunden lag. Jeder Verwurf kam zwei Stunden
  zu frueh.

  classify_requeue (LATENTE Wirkung): das Fenster REQUEUE_MAX_AGE_MIN=60
  ist gegen ein Startalter von 120 Minuten unerreichbar. Ein Schaden ist
  bisher NICHT nachweisbar — von den 294 FAILED-Trades trug kein
  einziger einen transienten Grund, der Requeue scheiterte also schon am
  Grundfilter. Die Bedingung bliebe aber auch beim ersten echten
  API-Timeout unerfuellbar.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.db.connection import DB
from bot.db.repo import TradeRepo
from bot.workers.execution_worker import (
    REQUEUE_MAX_AGE_MIN, _age_minutes, classify_requeue, market_closed_too_old,
)


@pytest.fixture()
def db(tmp_path):
    d = DB(db_path=tmp_path / "t.db")
    # BEWUSST der alte, falsche Spalten-Default — genau wie die Live-Tabelle.
    # SQLite kann einen DEFAULT nicht nachtraeglich aendern, deshalb muss der
    # Schreibpfad ihn umgehen, nicht die Migration ihn reparieren.
    d.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, instrument_id INTEGER, symbol TEXT,
            direction TEXT, status TEXT, amount_usd REAL, stop_loss_pct REAL,
            signal_id INTEGER, signal_price REAL, rejection_reason TEXT,
            order_id TEXT, requeue_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','utc')),
            approved_at TEXT
        )
    """)
    return d


def _create(db) -> int:
    return TradeRepo(db).create(
        instrument_id=1001, symbol="AAPL", direction="BUY",
        amount_usd=100.0, stop_loss_pct=5.0, signal_id=1, signal_price=200.0)


def test_der_alte_default_ist_wirklich_zwei_stunden_daneben(db):
    """Beweist die Praemisse — sonst testet der Rest nur sich selbst."""
    row = db.fetchone("SELECT datetime('now') AS ohne, datetime('now','utc') AS mit")
    ohne = datetime.strptime(row["ohne"], "%Y-%m-%d %H:%M:%S")
    mit = datetime.strptime(row["mit"], "%Y-%m-%d %H:%M:%S")
    if ohne == mit:
        pytest.skip("Testmaschine laeuft auf UTC — der Versatz ist dort 0")
    assert mit < ohne


def test_created_at_liegt_auf_echter_utc(db):
    tid = _create(db)
    row = db.fetchone("SELECT created_at FROM trades WHERE id=?", (tid,))
    ts = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc)
    abstand = abs((datetime.now(timezone.utc) - ts).total_seconds())
    assert abstand < 120, f"created_at {abstand:.0f}s von jetzt entfernt"


def test_frischer_trade_ist_null_minuten_alt_nicht_hundertzwanzig(db):
    tid = _create(db)
    row = db.fetchone("SELECT created_at FROM trades WHERE id=?", (tid,))
    alter = _age_minutes(row["created_at"])
    assert alter is not None and alter < 2, f"Alter bei Geburt: {alter:.1f} min"


def test_requeue_ist_fuer_einen_frischen_trade_wieder_erreichbar(db):
    """Latenter Schaden: das Zeitfenster war gegen 120 min nie erreichbar.

    Der Grund muss aus dem APIError-Pfad stammen — is_transient_failure
    akzeptiert nur Reasons, die mit "APIError"/"Unexpected error" beginnen.
    """
    tid = _create(db)
    db.execute("UPDATE trades SET status='FAILED', "
               "rejection_reason='APIError: HTTP 503 temporarily unavailable' "
               "WHERE id=?", (tid,))
    trade = dict(db.fetchone("SELECT * FROM trades WHERE id=?", (tid,)))
    assert classify_requeue(trade) is True


def test_alter_zeitstempel_haette_das_fenster_verfehlt(db):
    """Gegenprobe mit dem Zustand VOR dem Fix — 120 min gegen ein 60-min-Fenster."""
    tid = _create(db)
    db.execute("UPDATE trades SET status='FAILED', "
               "rejection_reason='APIError: HTTP 503 temporarily unavailable', "
               "created_at=datetime('now','-120 minutes') WHERE id=?", (tid,))
    trade = dict(db.fetchone("SELECT * FROM trades WHERE id=?", (tid,)))
    assert REQUEUE_MAX_AGE_MIN == 60
    assert classify_requeue(trade) is False


def test_markt_geschlossen_ttl_haelt_wieder_die_volle_dauer(db):
    """Die belegte Wirkung: market_closed_max_hours=4 wurde faktisch zu 2 h.

    323 Trades tragen "Markt >4h geschlossen — Signal veraltet, verworfen"
    bei einem wahren Alter unter 4 Stunden.
    """
    tid = _create(db)
    trade = dict(db.fetchone("SELECT * FROM trades WHERE id=?", (tid,)))
    assert market_closed_too_old(trade, max_hours=4.0) is False
    # und der Cap greift weiterhin, wenn der Trade wirklich zu alt ist
    db.execute("UPDATE trades SET created_at=datetime('now','-5 hours') WHERE id=?", (tid,))
    alt = dict(db.fetchone("SELECT * FROM trades WHERE id=?", (tid,)))
    assert market_closed_too_old(alt, max_hours=4.0) is True
