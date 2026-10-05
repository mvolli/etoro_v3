"""End-to-End-Tests fuer signal_worker.main() (refactor-2b, 2026-09-26).

WARUM: Bis hierher lief main() in keinem Test komplett durch. Der Kaufpfad
war nur ueber Quelltext-Tests abgesichert (Reihenfolge von Floors, Bumps,
Order im Quelltext) — die frieren die Struktur ein, pruefen aber nicht, was
der Worker TUT. Diese Tests lassen main() gegen eine synthetische DB laufen
und pruefen das Ergebnis in trades/signals/system_log.

Hermetik (der Bot laeuft auf derselben Maschine LIVE weiter):
  * worker_lock -> No-op: sonst nimmt der Test den Lock des Live-Crons.
  * Kill-Switch-Pruefung -> False: data/kill_switch.flag ist Live-Zustand.
  * Discord: _DE=None, _post gesperrt, conftest nullt zusaetzlich den Token.
  * yfinance.download/Ticker -> Fehler (curl_cffi umgeht jede Socket-Sperre;
    alle Aufrufer sind fail-open).
  * News-Pull -> deterministischer Fake, Markt immer offen, keine API-Keys.
  * DB nur unter tmp_path; conftest sperrt Schreibzugriffe auf data/.
"""
from __future__ import annotations

import contextlib
import re
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))


# ── Schema ───────────────────────────────────────────────────────────────────

_EXTRA_COLUMNS = {
    "instruments": ["yfinance_symbol TEXT", "is_tradable INTEGER", "atr_pct REAL",
                    "min_position_amount REAL", "min_position_amount_learned_at TEXT",
                    "market_region TEXT", "market_cap REAL", "adv_usd REAL"],
    "signals": ["price REAL", "status TEXT DEFAULT 'FRESH'"],
    "trades": ["closed_at TEXT", "pnl_usd REAL", "approved_at TEXT",
               "signal_price REAL", "stop_loss_pct REAL", "signal_id INTEGER",
               "requeue_count INTEGER DEFAULT 0",
               "verification_status TEXT DEFAULT ''",
               "verify_attempts INTEGER DEFAULT 0"],
    "portfolio_snapshot": ["yfinance_symbol TEXT"],
}


def _build_schema(path: Path) -> None:
    from init_db import init_db
    init_db(path)
    con = sqlite3.connect(path)
    for table, cols in _EXTRA_COLUMNS.items():
        for col in cols:
            with contextlib.suppress(sqlite3.OperationalError):
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    # Tabellen, die Module zur Laufzeit selbst anlegen, einmal vorab.
    pat = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;?\s*(?:\"\"\"|''')", re.S)
    for f in (REPO / "src").rglob("*.py"):
        for m in pat.finditer(f.read_text(encoding="utf-8")):
            with contextlib.suppress(sqlite3.Error):
                con.execute(f"CREATE TABLE IF NOT EXISTS {m.group(1)} ({m.group(2)})")
    con.execute("CREATE TABLE IF NOT EXISTS instrument_failures (instrument_id INTEGER PRIMARY KEY,"
                " consecutive_failures INTEGER DEFAULT 0, last_failure_at TEXT, blacklisted_until TEXT)")
    con.commit()
    con.close()


_INSTRUMENTS = [
    # id, symbol, asset_class, yf, sector, atr_pct, region
    (1, "BTC", "crypto", "BTC-USD", "CRYPTO", 2.0, "GLOBAL"),
    (2, "ETH", "crypto", "ETH-USD", "CRYPTO", 3.0, "GLOBAL"),
    (3, "ADA", "crypto", "ADA-USD", "CRYPTO", 5.0, "GLOBAL"),
    (4, "SOL", "crypto", "SOL-USD", "CRYPTO", 8.0, "GLOBAL"),
    (7, "GOLD", "commodity", "GC=F", "COMMODITY", 1.0, "GLOBAL"),
    (8, "LTC", "crypto", "LTC-USD", "CRYPTO", 3.0, "GLOBAL"),
    (9, "DOGE", "crypto", "DOGE-USD", "CRYPTO", 6.0, "GLOBAL"),
    (10, "LINK", "crypto", "LINK-USD", "CRYPTO", 4.0, "GLOBAL"),
    (1001, "AAPL", "stock", "AAPL", "Technology", 1.5, "US"),
    (1002, "MSFT", "stock", "MSFT", "Technology", 1.4, "US"),
    (1003, "NVDA", "stock", "NVDA", "Technology", 2.0, "US"),
]

_SIGNALS = [
    # instrument_id, signal_type, conviction, score, price
    (1, "MACD_BULLISH_CROSS", "HIGH", 0.90, 50000),
    (2, "TREND_PULLBACK", "VERY_HIGH", 0.95, 3000),
    (4, "MACD_BULLISH_CROSS+RSI_OVERSOLD", "HIGH", 0.85, 150),
    (7, "TREND_PULLBACK", "HIGH", 0.75, 2000),
    (10, "MACD_BULLISH_CROSS", "HIGH", 0.90, 15),          # is_tradable=0
    (8, "BB_UPPER_RSI_OVERBOUGHT", "HIGH", 0.90, 80),      # SELL -> ignoriert
]


class World:
    """Synthetische DB + Stellschrauben pro Szenario."""

    def __init__(self, path: Path):
        self.path = path
        _build_schema(path)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        for iid, sym, ac, yf, sec, atr, reg in _INSTRUMENTS:
            self.con.execute(
                "INSERT INTO instruments(instrument_id,symbol,asset_class,yfinance_symbol,"
                "sector,atr_pct,market_region,is_tradable) VALUES (?,?,?,?,?,?,?,?)",
                (iid, sym, ac, yf, sec, atr, reg, 0 if sym == "LINK" else 1))
        for iid, st, conv, sc, px in _SIGNALS:
            self.con.execute(
                "INSERT INTO signals(instrument_id,signal_type,conviction,score,price,status,"
                "expires_at,rsi) VALUES (?,?,?,?,?,'FRESH',datetime('now','+6 hours'),40)",
                (iid, st, conv, sc, px))
        self.state(CURRENT_EQUITY="10000", AVAILABLE_CASH="6000", CURRENT_REGIME="NORMAL")
        self.con.commit()

    def state(self, **kv):
        for k, v in kv.items():
            self.con.execute("INSERT OR REPLACE INTO system_state(key,value) VALUES (?,?)", (k, v))
        self.con.commit()

    def hold(self, iid: int, symbol: str, usd: float):
        self.con.execute("INSERT INTO portfolio_snapshot(api_position_id,instrument_id,symbol,amount_usd)"
                         " VALUES (?,?,?,?)", (f"p{iid}", iid, symbol, usd))
        self.con.execute("INSERT INTO trades(instrument_id,symbol,direction,amount_usd,status,signal_id)"
                         " VALUES (?,?,'BUY',?,'ACTIVE',1)", (iid, symbol, usd))
        self.con.commit()

    def approved(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for r in self.con.execute("SELECT symbol, amount_usd FROM trades WHERE status='APPROVED' ORDER BY id"):
            out.setdefault(r["symbol"], []).append(round(r["amount_usd"], 2))
        return out

    def signal_status(self, iid: int, signal_type: str | None = None) -> list[str]:
        q = "SELECT status FROM signals WHERE instrument_id=?"
        args: list = [iid]
        if signal_type:
            q += " AND signal_type=?"
            args.append(signal_type)
        return [r["status"] for r in self.con.execute(q + " ORDER BY id", args)]

    def log_messages(self) -> list[str]:
        return [r["message"] for r in self.con.execute("SELECT message FROM system_log ORDER BY id")]


def _snapshot_globals(*mods):
    """main() ruft risk/regime/diversity/deployment.apply_config — das
    schreibt Modul-Globals und sickerte ohne Rueckbau in andere Tests
    (test_regime, test_risk_v5). Container werden IN-PLACE restauriert,
    weil andere Module sie per `from ... import` referenzieren."""
    import copy
    snap = []
    for m in mods:
        for k, v in list(vars(m).items()):
            if k.startswith("__") or callable(v) or isinstance(v, type(sys)):
                continue
            if isinstance(v, (dict, list, set, int, float, str, bool, tuple, frozenset)) or v is None:
                snap.append((m, k, v, copy.deepcopy(v)))

    def restore():
        for m, k, obj, saved in snap:
            cur = getattr(m, k, None)
            if isinstance(obj, dict) and cur is obj:
                obj.clear(); obj.update(saved)
            elif isinstance(obj, list) and cur is obj:
                obj[:] = saved
            elif isinstance(obj, set) and cur is obj:
                obj.clear(); obj.update(saved)
            else:
                setattr(m, k, saved)
    return restore


@pytest.fixture
def world(tmp_path, monkeypatch):
    import yfinance

    from bot.core import market_hours
    import bot.core.kill_switch as ks
    import bot.core.worker_lock as wl
    import bot.discord_embeds as de
    import bot.workers.news_flags_worker as nfw
    from bot.workers import signal_worker as sw

    def _no_net(*a, **k):
        raise RuntimeError("Netz im Test gesperrt")

    monkeypatch.setattr(yfinance, "download", _no_net)
    monkeypatch.setattr(yfinance, "Ticker", _no_net)
    monkeypatch.setattr(de, "_request_discord", _no_net)
    monkeypatch.setattr(sw, "_DE", None)
    posts: list = []
    monkeypatch.setattr(sw, "_post", lambda fn, **kw: posts.append(fn))

    @contextlib.contextmanager
    def _lock(name):
        yield True
    monkeypatch.setattr(wl, "worker_lock", _lock)
    monkeypatch.setattr(ks, "is_kill_switch_active", lambda: False)
    monkeypatch.setattr(market_hours, "is_market_open", lambda *a, **k: True)

    def _fake_pull(entries, budget_s=45.0):
        flags = {}
        for e in entries:
            f = World.news_flags.get(e["symbol"])
            if f:
                flags[e["symbol"]] = {"flag": f, "reason": "test"}
        return flags, False
    monkeypatch.setattr(nfw, "pull_regel_flags", _fake_pull)
    World.news_flags = {}

    for k in ("ETORO_BOT_API_KEY", "ETORO_BOT_USER_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("bot.config.HERMES_ENV_PATH", tmp_path / "keine.env")
    monkeypatch.setattr(sw, "_LLM_NEWS_FLAGS_PATH", tmp_path / "keine_flags.json")
    monkeypatch.setattr(sw, "_LLM_GHOST_BLACKLIST_PATH", tmp_path / "keine_blacklist.json")
    monkeypatch.setattr(sw, "_LLM_SIGNAL_WEIGHTS_PATH", tmp_path / "keine_weights.json")

    import bot.core.regime as regime_mod
    import bot.core.risk as risk_mod
    restore = _snapshot_globals(risk_mod, regime_mod, sw)

    w = World(tmp_path / "trading.db")
    cfg = yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8"))
    cfg["db"]["path"] = str(w.path)
    t = cfg.setdefault("trading", {})
    t["signal_news_pull"] = True
    t.setdefault("commodity", {})["enabled"] = True
    cs = t.setdefault("core_sweep", {})
    cs["enabled"] = True
    cs["market_open_only"] = False
    cs["whitelist"] = {"AAPL": 1001, "MSFT": 1002}
    w.cfg = cfg
    w.posts = posts
    monkeypatch.setattr(sw, "_load_config", lambda: w.cfg)
    w.run = sw.main
    yield w
    w.con.close()
    restore()


# ── Szenarien ────────────────────────────────────────────────────────────────

def test_normallauf_genehmigt_erwartete_kaeufe(world):
    world.run()
    got = world.approved()
    # Signal-Pfad: BTC, ETH, SOL, GOLD; Core-Sweep: AAPL, MSFT
    assert set(got) == {"BTC", "ETH", "SOL", "GOLD", "AAPL", "MSFT"}
    assert all(len(v) == 1 for v in got.values()), f"Instrument doppelt: {got}"
    # Rohstoff: feste Groesse aus trading.commodity.position_usd
    assert got["GOLD"] == [float(world.cfg["trading"]["commodity"].get("position_usd", 500.0))]
    # is_tradable=0 und SELL-Signale erzeugen keinen Kauf
    assert "LINK" not in got and "LTC" not in got
    assert world.signal_status(10) == ["FRESH"], "LINK darf nicht angefasst werden"


def test_jeder_kauf_liegt_ueber_dem_dust_floor(world):
    world.run()
    floor = float(world.cfg["trading"].get("min_buy_usd", 50.0))
    for sym, amounts in world.approved().items():
        assert min(amounts) >= floor, f"{sym} unter Dust-Floor: {amounts}"


def test_defensive_klammert_auf_max_trade_pct(world):
    from bot.core.regime import get_regime_params
    world.state(CURRENT_REGIME="DEFENSIVE")
    world.run()
    cap = 10000 * float(get_regime_params("DEFENSIVE").get("max_trade_pct", 100.0)) / 100.0
    signal_trades = {s: a for s, a in world.approved().items() if s not in ("AAPL", "MSFT")}
    for sym, amounts in signal_trades.items():
        if sym == "GOLD":
            continue  # feste Rohstoffgroesse, bewusst ausserhalb der Kette
        assert max(amounts) <= cap + 0.01, f"{sym} ueber max_trade_pct: {amounts} > {cap}"


def test_news_avoid_blockt_signal_und_core_sweep(world, monkeypatch, tmp_path):
    # Signal-Kandidat: Flag kommt aus dem synchronen News-Pull.
    World.news_flags = {"BTC": "AVOID"}
    # Core-Sweep-Titel: Flag kommt aus der stuendlichen llm_news_flags.json
    # (der Pull laeuft nur fuer Signal-Kandidaten).
    import json
    from datetime import datetime, timedelta, timezone
    from bot.workers import signal_worker as sw
    f = tmp_path / "flags.json"
    f.write_text(json.dumps({
        "auto_expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
        "flags": {"AAPL": {"flag": "AVOID", "reason": "test"}},
    }))
    monkeypatch.setattr(sw, "_LLM_NEWS_FLAGS_PATH", f)
    world.run()
    got = world.approved()
    assert "BTC" not in got
    assert "AAPL" not in got, "Core-Sweep ignoriert News-AVOID"
    assert "MSFT" in got


def test_news_caution_halbiert(world):
    world.run()
    ohne = world.approved()["ETH"][0]

    world.con.execute("DELETE FROM trades WHERE status='APPROVED'")
    world.con.execute("UPDATE signals SET status='FRESH' WHERE signal_type != 'CORE_SWEEP'")
    world.con.execute("DELETE FROM signals WHERE signal_type='CORE_SWEEP'")
    world.con.commit()
    world.cfg["trading"]["signal_type_cooldown_minutes"] = 0
    World.news_flags = {"ETH": "CAUTION"}
    world.run()
    mit = world.approved()["ETH"][0]
    assert mit < ohne, f"CAUTION ohne Wirkung: {mit} vs {ohne}"


def test_db_sektor_blockt_signalkauf(world):
    world.con.execute("UPDATE instruments SET sector='ALT' WHERE symbol IN ('SOL','LTC')")
    world.con.commit()
    world.hold(8, "LTC", 1950.0)          # ALT-Topf bei 19.5 %
    world.run()
    assert "SOL" not in world.approved()
    assert world.signal_status(4) == ["REJECTED"]


def test_core_sweep_blockt_an_anlageklassen_grenze(world):
    world.hold(1003, "NVDA", 3950.0)      # US_TECH bei 39.5 %
    world.run()
    got = world.approved()
    assert "AAPL" not in got and "MSFT" not in got
    assert world.signal_status(1001, "CORE_SWEEP") == ["REJECTED"]
    assert any("an Portfolio-Grenzen geblockt" in m for m in world.log_messages())


def test_ohne_equity_kein_kauf(world):
    world.state(CURRENT_EQUITY="0")
    world.run()
    assert world.approved() == {}
    assert "post_alert_embed" in world.posts


def test_ohne_frische_kaufsignale_kein_kauf(world):
    world.con.execute("UPDATE signals SET status='CONSUMED'")
    world.con.commit()
    world.run()
    assert world.approved() == {}


def test_tageslimit_stoppt_approvals(world):
    world.cfg["trading"]["max_trades_per_day"] = 1
    world.con.execute("INSERT INTO trades(instrument_id,symbol,direction,amount_usd,status)"
                      " VALUES (2,'ETH','BUY',100,'ACTIVE')")
    world.con.commit()
    world.run()
    assert world.approved() == {}


def test_extra_slots_doppeln_keinen_kandidaten(world):
    # 7 HIGH-Kaufkandidaten + Cash 60 % > 30 % -> Basis-Slots + Extra-Slots.
    for iid, px in ((3, 0.5), (9, 0.2), (8, 80)):
        world.con.execute(
            "INSERT INTO signals(instrument_id,signal_type,conviction,score,price,status,"
            "expires_at,rsi) VALUES (?,'MACD_BULLISH_CROSS','HIGH',0.8,?,'FRESH',"
            "datetime('now','+6 hours'),40)", (iid, px))
    world.con.commit()
    world.cfg["trading"]["core_sweep"]["enabled"] = False
    world.run()
    got = world.approved()
    assert all(len(v) == 1 for v in got.values()), f"Instrument doppelt: {got}"
