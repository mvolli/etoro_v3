"""fix/regime-alert-dedupe + fix/regime-alert-set-after-post (2026-08-21).

Die Regime-Eintrittsmeldung hatte zwei Fehler hintereinander:

1. Sie postete in JEDEM 30-min-Heartbeat, solange das Regime anhielt —
   gemessen 20-48 Meldungen taeglich ueber acht Tage. Der Alert ist ein
   WECHSEL-Ereignis, kein Zustand.

2. Nach dem Dedupe-Fix stand `state_repo.set("REGIME_ALERTED", regime)` VOR
   dem Discord-Post. Schlug der Post fehl, galt das Regime als gemeldet und
   die Eintrittsmeldung ins DEFENSIVE/CRITICAL-Regime ging DAUERHAFT
   verloren — ein stiller Ausfall bei genau der Meldung, die zaehlt.

Diese Tests fahren `maybe_post_regime_alert` mit injiziertem Discord-Stub;
es wird nie gepostet, nur der Payload und das Flag geprueft.
"""
from __future__ import annotations

import pytest

from bot.db.repo import DB, StateRepo
from bot.workers.reconciler import maybe_post_regime_alert


@pytest.fixture()
def repo(tmp_path):
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


class _Discord:
    """Stub: zaehlt Posts, Erfolg steuerbar."""

    def __init__(self, erfolg=True):
        self.erfolg, self.calls = erfolg, []

    def __call__(self, fn_name, **kw):
        self.calls.append((fn_name, kw))
        return "msg-123" if self.erfolg else False


def _call(repo, regime, discord, dd=9.1, eq=8000.0, peak=9400.0):
    return maybe_post_regime_alert(repo, regime, dd, eq, peak, discord=discord)


# ── Wechsel-Semantik ──────────────────────────────────────────────────────────

def test_eintritt_meldet_einmal(repo):
    d = _Discord()
    assert _call(repo, "DEFENSIVE", d) is True
    assert len(d.calls) == 1
    assert repo.get("REGIME_ALERTED") == "DEFENSIVE"


def test_persistierendes_regime_meldet_nicht_erneut(repo):
    """Der urspruengliche Spam: 48 Zyklen, 1 Meldung."""
    d = _Discord()
    for _ in range(48):
        _call(repo, "DEFENSIVE", d)
    assert len(d.calls) == 1, f"{len(d.calls)} Meldungen statt 1"


def test_eskalation_meldet_erneut(repo):
    d = _Discord()
    _call(repo, "DEFENSIVE", d)
    assert _call(repo, "CRITICAL", d) is True, "DEFENSIVE→CRITICAL ist ein Wechsel"
    assert len(d.calls) == 2
    assert repo.get("REGIME_ALERTED") == "CRITICAL"


@pytest.mark.parametrize("ruhig", ["NORMAL", "CAUTION"])
def test_rueckkehr_gibt_das_flag_frei(repo, ruhig):
    d = _Discord()
    _call(repo, "DEFENSIVE", d)
    assert _call(repo, ruhig, d) is False, "in Ruhe wird nicht gemeldet"
    assert repo.get("REGIME_ALERTED") == "NORMAL"
    # und der naechste Eintritt meldet wieder
    assert _call(repo, "DEFENSIVE", d) is True
    assert len(d.calls) == 2


def test_ruhiges_regime_postet_nie(repo):
    d = _Discord()
    for r in ("NORMAL", "CAUTION", "NORMAL"):
        assert _call(repo, r, d) is False
    assert d.calls == []


# ── Der eigentliche Fix: Flag erst nach erfolgreichem Post ────────────────────

def test_fehlgeschlagener_post_setzt_das_flag_nicht(repo):
    """Kern des Fixes — vorher ging die Meldung dauerhaft verloren."""
    d = _Discord(erfolg=False)
    assert _call(repo, "DEFENSIVE", d) is False
    assert repo.get("REGIME_ALERTED") in (None, ""), (
        "Flag darf nach fehlgeschlagenem Post NICHT gesetzt sein"
    )


def test_nach_fehlschlag_wird_erneut_versucht(repo):
    """Discord kommt zurueck — die Meldung muss noch rausgehen."""
    kaputt = _Discord(erfolg=False)
    for _ in range(3):
        _call(repo, "DEFENSIVE", kaputt)
    assert len(kaputt.calls) == 3, "jeder Zyklus versucht es erneut"
    assert repo.get("REGIME_ALERTED") in (None, "")

    heil = _Discord(erfolg=True)
    assert _call(repo, "DEFENSIVE", heil) is True
    assert repo.get("REGIME_ALERTED") == "DEFENSIVE"


def test_ausnahme_im_discord_wird_wie_fehlschlag_behandelt(repo):
    def boom(fn_name, **kw):
        return None  # _discord kapselt Ausnahmen zu None
    assert maybe_post_regime_alert(repo, "CRITICAL", 16.0, 8000.0, 9400.0,
                                   discord=boom) is False
    assert repo.get("REGIME_ALERTED") in (None, "")


# ── Payload ───────────────────────────────────────────────────────────────────

def test_schweregrad_und_zahlen_im_payload(repo):
    repo.set("RISK_SCALAR", "0.25")
    d = _Discord()
    _call(repo, "CRITICAL", d, dd=16.4, eq=7654.32, peak=9400.0)
    fn, kw = d.calls[0]
    assert fn == "post_alert_embed"
    assert kw["severity"] == "CRITICAL"
    assert "16.40%" in kw["description"]
    assert "0.25" in kw["description"]
    assert "$7654.32" in kw["description"]
    assert "VERY_HIGH" in kw["description"]


def test_defensive_ist_nur_warning(repo):
    d = _Discord()
    _call(repo, "DEFENSIVE", d)
    assert d.calls[0][1]["severity"] == "WARNING"


def test_fehlender_risk_scalar_faellt_auf_default(repo):
    d = _Discord()
    _call(repo, "DEFENSIVE", d)          # RISK_SCALAR nie gesetzt
    assert "0.50" in d.calls[0][1]["description"]
