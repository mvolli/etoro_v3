"""feat/close-history-observability: insert_system_log persistiert in system_log."""
import sqlite3
import bot.discord_embeds as DE


def _mkdb(tmp_path):
    p = tmp_path / "t.db"
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE system_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL DEFAULT (datetime('now','utc')),
        level TEXT NOT NULL, worker TEXT NOT NULL, message TEXT NOT NULL, details TEXT)""")
    c.commit(); c.close()
    return p


def test_insert_writes_row(tmp_path, monkeypatch):
    db = _mkdb(tmp_path)
    monkeypatch.setattr(DE, "_TRADING_DB_PATH", db)
    DE.insert_system_log("INFO", "discord_embeds", "P14 Position Closed: LUS1.DE ✂️25%", "detail")
    c = sqlite3.connect(db); r = c.execute("SELECT level,worker,message,details FROM system_log").fetchone(); c.close()
    assert r == ("INFO", "discord_embeds", "P14 Position Closed: LUS1.DE ✂️25%", "detail")


def test_insert_failopen_on_bad_path(monkeypatch, tmp_path):
    monkeypatch.setattr(DE, "_TRADING_DB_PATH", tmp_path / "does_not_exist" / "x.db")
    # darf nicht werfen
    DE.insert_system_log("WARN", "discord_embeds", "msg")


def test_empty_details_becomes_null(tmp_path, monkeypatch):
    db = _mkdb(tmp_path)
    monkeypatch.setattr(DE, "_TRADING_DB_PATH", db)
    DE.insert_system_log("INFO", "discord_embeds", "msg")
    c = sqlite3.connect(db); r = c.execute("SELECT details FROM system_log").fetchone(); c.close()
    assert r[0] is None
