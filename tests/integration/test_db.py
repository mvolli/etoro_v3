"""Integration tests for DB layer."""
import sys; sys.path.insert(0, "src")
import pytest
from bot.db.connection import DB
from bot.db.repo import StateRepo, LogRepo

DB_PATH = "data/trading.db"

@pytest.fixture
def db(): return DB(DB_PATH)

def test_state_regime(db):
    sr = StateRepo(db)
    sr.set_regime("NORMAL")
    assert sr.get_regime() == "NORMAL"

def test_state_equity(db):
    sr = StateRepo(db)
    sr.set("CURRENT_EQUITY", "9469.16")
    assert abs(sr.get_equity() - 9469.16) < 0.01

def test_log_write(db):
    lr = LogRepo(db)
    lr.write("INFO", "test", "V3 integration test OK")
    r = lr.get_recent(limit=1, worker="test")
    assert r and r[0]["message"] == "V3 integration test OK"

def test_db_fetchone(db):
    r = db.fetchone("SELECT value FROM system_state WHERE key=?", ("CURRENT_REGIME",))
    assert r is not None
    assert r["value"] in ("NORMAL", "DRAWDOWN", "RECOVERY")
