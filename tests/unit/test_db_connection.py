#!/usr/bin/env python3
"""Unit tests — fix/db-connection-reuse.

The DB wrapper reuses one connection per instance instead of opening a
fresh connection (plus 4 PRAGMAs) for every query. Semantics must stay:
per-statement commits, error rollback with reusable connection, cross-
instance visibility (no stale WAL snapshots), working context manager.
"""
from __future__ import annotations

import pytest

from bot.db.connection import DB


@pytest.fixture
def db(tmp_path):
    d = DB(db_path=tmp_path / "t.db")
    d.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
    return d


def test_connection_is_reused(db, monkeypatch):
    opens = []
    original = DB.connect

    def counting_connect(self):
        opens.append(1)
        return original(self)

    monkeypatch.setattr(DB, "connect", counting_connect)
    fresh = DB(db_path=db.db_path)
    fresh.execute("INSERT INTO kv VALUES ('a', '1')")
    fresh.fetchone("SELECT v FROM kv WHERE k='a'")
    fresh.fetchall("SELECT * FROM kv")
    fresh.execute("UPDATE kv SET v='2' WHERE k='a'")
    assert len(opens) == 1  # eine Connection für alle vier Queries


def test_writes_visible_across_instances(db, tmp_path):
    # Simuliert zwei Worker-Prozesse: Instanz B muss Writes von A sehen
    other = DB(db_path=db.db_path)
    db.execute("INSERT INTO kv VALUES ('x', 'from-a')")
    row = other.fetchone("SELECT v FROM kv WHERE k='x'")
    assert row[0] == "from-a"

    # ... auch NACH früheren Reads auf B (kein festgehaltener WAL-Snapshot)
    other.fetchall("SELECT * FROM kv")
    db.execute("INSERT INTO kv VALUES ('y', 'later')")
    row = other.fetchone("SELECT v FROM kv WHERE k='y'")
    assert row is not None and row[0] == "later"


def test_error_rolls_back_and_connection_stays_usable(db):
    with pytest.raises(Exception):
        db.execute("INSERT INTO kv VALUES ('a', '1'), ('a', '2')")  # PK-Konflikt
    # Connection weiter benutzbar, fehlgeschlagener Insert nicht persistiert
    db.execute("INSERT INTO kv VALUES ('b', 'ok')")
    assert db.fetchone("SELECT v FROM kv WHERE k='b'")[0] == "ok"
    assert db.fetchone("SELECT COUNT(*) FROM kv WHERE k='a'")[0] == 0


def test_close_reopens_lazily(db):
    db.execute("INSERT INTO kv VALUES ('c', '3')")
    db.close()
    assert db._conn is None
    assert db.fetchone("SELECT v FROM kv WHERE k='c'")[0] == "3"


def test_context_manager_commits(tmp_path):
    path = tmp_path / "cm.db"
    with DB(db_path=path) as d:
        d.execute("CREATE TABLE t (x INTEGER)")
        d.execute("INSERT INTO t VALUES (42)")
    # nach __exit__: neue Instanz sieht die Daten, alte Connection zu
    assert DB(db_path=path).fetchone("SELECT x FROM t")[0] == 42


def test_lastrowid_still_works(db):
    db.execute("CREATE TABLE seq (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    cur = db.execute("INSERT INTO seq (v) VALUES ('first')")
    assert cur.lastrowid == 1
