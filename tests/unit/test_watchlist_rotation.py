"""Watchlist-Rotation nach Nuetzlichkeit (fix/rotation-usefulness).

Vorher hielt JEDES Signal einen Platz am Leben — auch BB_UPPER_RSI_OVERBOUGHT.
Ein Titel, der wochenlang nur Verkaufssignale lieferte, blockierte damit
dauerhaft einen Discovery-Platz, obwohl der signal_worker (der Kauf-Pfad) ihn
nie verwenden kann. Gemessen am 2026-08-28: bei 30 Tagen waren 9 von 230
Plaetzen raeumbar, die Rotation stand faktisch still.
"""
import sqlite3

import pytest

from bot.workers.discovery_worker import _evict_stale_discovered


class _DB:
    """Minimaler Ersatz fuer bot.db.connection.DB."""

    def __init__(self, con):
        self.con = con
        con.row_factory = sqlite3.Row

    def fetchall(self, sql, params=()):
        return self.con.execute(sql, params).fetchall()

    def execute(self, sql, params=()):
        cur = self.con.execute(sql, params)
        self.con.commit()
        return cur


@pytest.fixture
def db(tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    con.executescript("""
        CREATE TABLE watchlist (
            id INTEGER PRIMARY KEY, symbol TEXT, instrument_id INTEGER,
            category TEXT, added_at TEXT, last_score REAL, last_signal_at TEXT);
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY, instrument_id INTEGER,
            signal_type TEXT, generated_at TEXT);
    """)
    con.commit()
    return _DB(con)


def _platz(db, iid, symbol, kategorie="us.discovered", score=50.0):
    db.execute(
        "INSERT INTO watchlist (symbol, instrument_id, category, last_score, "
        "last_signal_at) VALUES (?, ?, ?, ?, datetime('now','-60 days'))",
        (symbol, iid, kategorie, score))


def _signal(db, iid, typ, tage_alt):
    db.execute(
        "INSERT INTO signals (instrument_id, signal_type, generated_at) "
        "VALUES (?, ?, datetime('now', ?))", (iid, typ, f"-{tage_alt} days"))


def _uebrig(db):
    return {r["symbol"] for r in db.fetchall("SELECT symbol FROM watchlist")}


def test_nur_verkaufssignale_schuetzen_einen_platz_nicht(db):
    """Der Kern der Aenderung."""
    _platz(db, 1, "NUR_VERKAUF")
    _signal(db, 1, "BB_UPPER_RSI_OVERBOUGHT", 1)      # gestern, aber wertlos
    _signal(db, 1, "TREND_KIPP_1H,SELL", 2)
    assert _evict_stale_discovered(db) == 1
    assert "NUR_VERKAUF" not in _uebrig(db)


def test_kaufsignal_im_fenster_haelt_den_platz(db):
    _platz(db, 2, "MIT_KAUF")
    _signal(db, 2, "MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING", 5)
    assert _evict_stale_discovered(db) == 0
    assert "MIT_KAUF" in _uebrig(db)


def test_altes_kaufsignal_schuetzt_nicht_mehr(db):
    """21-Tage-Fenster: aelter zaehlt nicht."""
    _platz(db, 3, "ALTER_KAUF")
    _signal(db, 3, "TREND_PULLBACK,GOLDEN_CROSS", 40)
    assert _evict_stale_discovered(db) == 1
    assert "ALTER_KAUF" not in _uebrig(db)


def test_statische_plaetze_werden_nie_geraeumt(db):
    """Nur *.discovered rotiert — feste Listen bleiben unangetastet."""
    _platz(db, 4, "FEST", kategorie="stocks")
    _platz(db, 5, "FOREX_FEST", kategorie="forex")
    assert _evict_stale_discovered(db) == 0
    assert {"FEST", "FOREX_FEST"} <= _uebrig(db)


def test_schwaechste_zuerst_und_deckel_greift(db):
    """Sortierung nach last_score aufsteigend, LIMIT = EVICTION_MAX."""
    from bot.workers.discovery_worker import STALE_DISCOVERED_EVICTION_MAX
    for i in range(STALE_DISCOVERED_EVICTION_MAX + 5):
        _platz(db, 100 + i, f"S{i:02d}", score=float(i))
    n = _evict_stale_discovered(db)
    assert n == STALE_DISCOVERED_EVICTION_MAX
    uebrig = _uebrig(db)
    # Die 5 mit dem hoechsten Score muessen ueberleben.
    assert uebrig == {f"S{i:02d}" for i in
                      range(STALE_DISCOVERED_EVICTION_MAX,
                            STALE_DISCOVERED_EVICTION_MAX + 5)}


def test_fenster_ist_21_tage():
    from bot.workers.discovery_worker import STALE_DISCOVERED_DAYS
    assert STALE_DISCOVERED_DAYS == 21
