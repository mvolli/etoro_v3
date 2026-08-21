"""fix/sl-close-unverified-dedupe (2026-08-21): Regressionstest fuer die
per-Position Alert-Dedupe im risk_worker.

Root cause (Live-DB 2026-08-21): a verified=False SL-close fired 1 ERROR +
1 CRITICAL Discord alert on EVERY 5-minute risk cycle per stuck position
(2600.HK: 105 alerts in ~11h ≈ 144 alerts/day). The state is already
persisted (trades.verification_status='PENDING', Reconciler finalizes it),
so this is a notification problem. Fix: per-position cap (3) + cooldown
(6h), CRITICAL→WARNING downgrade after the first hit, both the provisional
CLOSE embed and the CRITICAL alert share one gate.

This test drives the pure helper `_unverified_close_alert_due` against a
real SQLite-backed StateRepo (the exact production code path), plus
`_clear_unverified_close_state` for the verified-close reset.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.db.repo import DB, StateRepo
from bot.workers.risk_worker import (
    SL_UNVERIFIED_COOLDOWN_HOURS,
    SL_UNVERIFIED_MAX_ALERTS,
    _clear_unverified_close_state,
    _sweep_unverified_close_state,
    _unverified_close_alert_due,
)


@pytest.fixture()
def repo(tmp_path):
    """Real StateRepo on a temp DB — exact production persistence path."""
    d = DB(db_path=tmp_path / "state.db")
    d.execute("""
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)
    yield StateRepo(d)
    d.close()


def _stored_count(repo, position_id) -> int:
    raw = repo.get(f"SL_UNVERIFIED_{position_id}")
    assert raw is not None, "state must be persisted"
    return int(json.loads(raw)["count"])


# ── First hit fires ────────────────────────────────────────────────────────────

def test_first_hit_fires(repo):
    due, count = _unverified_close_alert_due(repo, "12345", "2600.HK")
    assert due is True, "first unverified-close must always alert"
    assert count == 1
    assert _stored_count(repo, "12345") == 1


# ── Cooldown suppresses repeats ───────────────────────────────────────────────

def test_repeated_within_cooldown_suppressed(repo):
    """Unterdrueckte Treffer duerfen den Alert-Zaehler NICHT hochtreiben.

    Vorher stand hier `assert count == i` und `_stored_count == 8` mit dem
    Kommentar "cap tracking still runs even while suppressed" — das hat das
    Fehlverhalten festgeschrieben: der Zaehler zaehlte Aufrufe statt Alerts
    und war nach 3 Zyklen (15 Minuten) erschoepft.
    """
    _unverified_close_alert_due(repo, "12345", "2600.HK")  # feuert (count 1)
    for i in range(2, 9):  # gleiches Cooldown-Fenster: 7 weitere Treffer
        due, count = _unverified_close_alert_due(repo, "12345", "2600.HK")
        assert due is False, f"hit {i} inside cooldown must be suppressed"
        assert count == 1, "unterdrueckter Treffer darf den Alert-Zaehler nicht erhoehen"
    assert _stored_count(repo, "12345") == 1, "erst 1 Alert wirklich gesendet"
    # hits zaehlt die Zyklen weiter — Diagnose bleibt erhalten
    assert json.loads(repo.get("SL_UNVERIFIED_12345"))["hits"] == 8


# ── Cap after MAX_ALERTS ──────────────────────────────────────────────────────

def test_cap_never_fires_after_max(repo):
    # Backdate last_at past cooldown so each hit is "fresh", force count past cap
    for i in range(1, SL_UNVERIFIED_MAX_ALERTS + 3):
        due, count = _unverified_close_alert_due(repo, "42", "AAPL")
        if i <= SL_UNVERIFIED_MAX_ALERTS:
            assert due is True, f"hit {i} (count {count}) must fire"
        else:
            assert due is False, f"hit {i} (count {count}) over cap must not fire"
        # push last_at back so the next hit isn't cooldown-suppressed
        key = "SL_UNVERIFIED_42"
        info = json.loads(repo.get(key))
        past = (datetime.now(timezone.utc) - timedelta(hours=SL_UNVERIFIED_COOLDOWN_HOURS + 1)).isoformat()
        repo.set(key, json.dumps({"count": count, "last_at": past, "symbol": "AAPL"}))


# ── Cooldown expiry re-fires ───────────────────────────────────────────────────

def test_cooldown_expiry_refires(repo):
    _unverified_close_alert_due(repo, "12345", "2600.HK")  # count=1, fires
    # Backdate last_at 7h ago (past 6h cooldown)
    key = "SL_UNVERIFIED_12345"
    info = json.loads(repo.get(key))
    past = (datetime.now(timezone.utc) - timedelta(hours=SL_UNVERIFIED_COOLDOWN_HOURS + 1)).isoformat()
    repo.set(key, json.dumps({"count": info["count"], "last_at": past, "symbol": "2600.HK"}))
    due, count = _unverified_close_alert_due(repo, "12345", "2600.HK")
    assert due is True, "cooldown expired → may fire again"
    assert count == 2


# ── Independent positions ──────────────────────────────────────────────────────

def test_positions_are_independent(repo):
    d1, c1 = _unverified_close_alert_due(repo, "111", "AAA")
    d2, c2 = _unverified_close_alert_due(repo, "222", "BBB")
    assert d1 is True and c1 == 1
    assert d2 is True and c2 == 1, "each position tracks its own counter"
    # Position 111 again is suppressed, but 222 is unaffected
    d1b, c1b = _unverified_close_alert_due(repo, "111", "AAA")
    assert d1b is False and c1b == 1, "unterdrueckt -> Alert-Zaehler bleibt 1"


# ── Corrupt state → safe reset, only first fires ─────────────────────────────

def test_corrupt_state_is_safe(repo):
    repo.set("SL_UNVERIFIED_999", "{not valid json")
    due, count = _unverified_close_alert_due(repo, "999", "XXX")
    assert due is True, "unparsable state → treat as first hit"
    assert count == 1


# ── _clear_unverified_close_state (verified close resets) ─────────────────────

def test_clear_resets_state(repo):
    _unverified_close_alert_due(repo, "12345", "2600.HK")
    _unverified_close_alert_due(repo, "12345", "2600.HK")
    assert repo.get("SL_UNVERIFIED_12345") is not None
    _clear_unverified_close_state(repo, "12345")
    assert repo.get("SL_UNVERIFIED_12345") is None, "state must be cleared"
    # After a verified close + reset, the next unverified-close fires again
    due, count = _unverified_close_alert_due(repo, "12345", "2600.HK")
    assert due is True and count == 1


# ── Clear on unknown position is a no-op (never raises) ────────────────────────

def test_clear_unknown_position_noop(repo):
    _clear_unverified_close_state(repo, "does-not-exist")  # must not raise


# ── Der Test, der gefehlt hat: REALE Zyklen ohne Handanlegen ──────────────────

def test_real_cycle_sequence_delivers_full_cap(repo, monkeypatch):
    """24 h echter risk_worker-Takt (*/5) — ohne last_at von Hand zu aendern.

    Diese Luecke liess den Bug durch: alle anderen Tests datieren `last_at`
    selbst zurueck, um den Cooldown zu ueberspringen. Die Produktion tut das
    nie — dort wurde `last_at` bei JEDEM Zyklus auf jetzt gesetzt, war also
    immer ~5 Minuten alt, und der 6h-Cooldown konnte nie ablaufen. Gemessen
    lieferte die Funktion 1 statt 3 Alerts.

    Der Test faehrt die Uhr vorwaerts, statt den Zustand zu manipulieren.
    """
    import bot.workers.risk_worker as rw

    start = datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc)
    jetzt = {"t": start}

    class _Uhr(datetime):
        @classmethod
        def now(cls, tz=None):
            return jetzt["t"]

    monkeypatch.setattr(rw, "datetime", _Uhr)

    alerts = []
    for zyklus in range(288):  # 24 h bei */5
        jetzt["t"] = start + timedelta(minutes=5 * zyklus)
        due, count = _unverified_close_alert_due(repo, "2600", "2600.HK")
        if due:
            alerts.append((jetzt["t"], count))

    assert len(alerts) == SL_UNVERIFIED_MAX_ALERTS, (
        f"erwartet {SL_UNVERIFIED_MAX_ALERTS} Alerts ueber 24h, bekommen {len(alerts)}"
    )
    assert [c for _, c in alerts] == [1, 2, 3]
    # Abstaende entsprechen dem Cooldown, nicht dem Worker-Takt
    for (t_prev, _), (t_next, _) in zip(alerts, alerts[1:]):
        abstand_h = (t_next - t_prev).total_seconds() / 3600
        assert abstand_h >= SL_UNVERIFIED_COOLDOWN_HOURS, (
            f"Alerts nur {abstand_h:.2f}h auseinander — Cooldown greift nicht"
        )
    # Diagnose bleibt: alle 288 Zyklen sind gezaehlt
    assert json.loads(repo.get("SL_UNVERIFIED_2600"))["hits"] == 288


# ── fix/sl-unverified-state-leak (2026-08-21) ─────────────────────────────────
# `_clear_unverified_close_state` laeuft nur beim VERIFIZIERTEN Close im
# risk_worker — genau diese Faelle finalisiert aber der Reconciler. Der Key
# blieb danach fuer immer in `system_state`. Der Sweep raeumt einmal pro Lauf
# selbst auf, statt an jeder Finalisierungsstelle des Reconcilers einzuhaken.

@pytest.fixture()
def repo_mit_trades(tmp_path):
    d = DB(db_path=tmp_path / "sweep.db")
    d.execute("""
        CREATE TABLE system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','utc'))
        )
    """)
    d.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            api_position_id TEXT,
            verification_status TEXT
        )
    """)
    yield StateRepo(d)
    d.close()


def _trade(repo, pos_id, status):
    repo.db.execute(
        "INSERT INTO trades (api_position_id, verification_status) VALUES (?,?)",
        (pos_id, status),
    )


def _key(repo, pos_id):
    repo.set(f"SL_UNVERIFIED_{pos_id}",
             json.dumps({"count": 1, "hits": 9, "last_at": "", "symbol": pos_id}))


def test_sweep_entfernt_finalisierte_und_verwaiste(repo_mit_trades):
    r = repo_mit_trades
    _trade(r, "P1", "PENDING")
    _trade(r, "P2", "PENDING")
    _trade(r, "F1", "VERIFIED")
    _trade(r, "F2", "UNRESOLVED")
    for pid in ("P1", "P2", "F1", "F2", "GEIST"):
        _key(r, pid)

    entfernt = _sweep_unverified_close_state(r)

    assert entfernt == 3, "VERIFIED + UNRESOLVED + verwaist muessen weg"
    assert r.get("SL_UNVERIFIED_P1") is not None, "PENDING bleibt"
    assert r.get("SL_UNVERIFIED_P2") is not None, "PENDING bleibt"
    for pid in ("F1", "F2", "GEIST"):
        assert r.get(f"SL_UNVERIFIED_{pid}") is None


def test_sweep_fasst_fremde_keys_nicht_an(repo_mit_trades):
    r = repo_mit_trades
    r.set("CURRENT_REGIME", "DEFENSIVE")
    r.set("RISK_SCALAR", "0.5")
    _key(r, "WEG")
    _sweep_unverified_close_state(r)
    assert r.get("CURRENT_REGIME") == "DEFENSIVE"
    assert r.get("RISK_SCALAR") == "0.5"
    assert r.get("SL_UNVERIFIED_WEG") is None


def test_sweep_ist_idempotent(repo_mit_trades):
    r = repo_mit_trades
    _trade(r, "P1", "PENDING")
    _key(r, "P1")
    _key(r, "WEG")
    assert _sweep_unverified_close_state(r) == 1
    assert _sweep_unverified_close_state(r) == 0
    assert r.get("SL_UNVERIFIED_P1") is not None


def test_sweep_ohne_keys_ist_geraeuschlos(repo_mit_trades):
    assert _sweep_unverified_close_state(repo_mit_trades) == 0


def test_sweep_faellt_nicht_um_wenn_trades_fehlt(repo):
    """`repo`-Fixture hat KEINE trades-Tabelle — Sweep muss still scheitern."""
    _key(repo, "X")
    assert _sweep_unverified_close_state(repo) == 0
    assert repo.get("SL_UNVERIFIED_X") is not None, "nichts geloescht bei Fehler"
