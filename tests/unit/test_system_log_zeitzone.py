#!/usr/bin/env python3
"""fix/system-log-doppelte-zeitzone (2026-09-10).

`system_log.ts` traegt DEFAULT (datetime('now','utc')). SQLites `now` IST
bereits UTC — der 'utc'-Modifier liest den Wert als Ortszeit und rechnet
ein ZWEITES Mal um. Gemessen auf der Live-DB:

    datetime('now')        2026-09-10 21:45:28
    datetime('now','utc')  2026-09-10 19:45:28   <- zwei Stunden zurueck

`LogRepo.write()` liess den Default greifen, `discord_embeds.
insert_system_log()` setzte ts mit datetime('now') selbst. Zwei Uhren in
derselben Tabelle: Zeilen desselben Vorgangs standen zwei Stunden
auseinander, und jede Zeitfensterabfrage auf system_log war falsch.
Aufgefallen bei der Suche nach dem CAR.AX-Close-Embed-Spam, wo genau
diese Abfragen die Beweisfuehrung tragen sollten.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from bot.db.connection import DB
from bot.db.repo import LogRepo

_SCHEMA = """
CREATE TABLE system_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL DEFAULT (datetime('now','utc')),
    level   TEXT NOT NULL,
    worker  TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);
"""


def _abstand_s(a: str, b: str) -> float:
    """Abstand zweier SQLite-Zeitstempel in Sekunden.

    fix/zeitzone-test-sekundenrace (2026-09-26): Die Tests verglichen die
    Zeitstempel exakt. Faellt zwischen Schreiben und Nachlesen (bzw. zwischen
    den beiden Schreibwegen) ein Sekundenwechsel, wurden sie sporadisch rot,
    ohne dass der Code falsch war. Geprueft wird der eigentliche Fehler —
    eine Verschiebung um die Zeitzone, also Stunden — mit 2 s Toleranz.
    """
    fmt = "%Y-%m-%d %H:%M:%S"
    return abs((datetime.strptime(a, fmt) - datetime.strptime(b, fmt)).total_seconds())


_TOLERANZ_S = 2.0


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    return DB(p)


def test_write_setzt_ts_selbst_und_nicht_den_default(db):
    """Der Kern: geschrieben wird datetime('now'), nicht der Spalten-Default."""
    LogRepo(db).write("INFO", "test", "hallo")
    row = db.fetchone(
        "SELECT ts, datetime('now') AS jetzt,"
        " datetime('now','utc') AS default_wert FROM system_log")
    assert _abstand_s(row["ts"], row["jetzt"]) <= _TOLERANZ_S
    # Nur dort aussagekraeftig, wo die Testmaschine nicht auf UTC steht —
    # auf einer UTC-Maschine fallen beide Werte zusammen.
    if _abstand_s(row["jetzt"], row["default_wert"]) > _TOLERANZ_S:
        assert _abstand_s(row["ts"], row["default_wert"]) > _TOLERANZ_S


def test_beide_schreibwege_stimmen_ueberein(db):
    """LogRepo und discord_embeds duerfen nicht auseinanderlaufen."""
    LogRepo(db).write("INFO", "repo", "a")
    db.execute(
        "INSERT INTO system_log (ts, level, worker, message, details)"
        " VALUES (datetime('now'), ?, ?, ?, ?)",
        ("INFO", "discord_embeds", "b", None))
    rows = db.fetchall("SELECT worker, ts FROM system_log ORDER BY id")
    assert _abstand_s(rows[0]["ts"], rows[1]["ts"]) <= _TOLERANZ_S


def test_details_werden_weiterhin_als_json_abgelegt(db):
    LogRepo(db).write("WARN", "test", "m", {"k": 1})
    row = db.fetchone("SELECT level, details FROM system_log")
    assert row["level"] == "WARN"
    assert '"k": 1' in row["details"]
