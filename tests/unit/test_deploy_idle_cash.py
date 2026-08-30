"""feat/deploy-idle-cash (2026-08-29) — Idle-Cash-Deployment via Floor-Bump.

VoLLi-Direktive: Position < Floor → auf den Floor ANHEBEN und genehmigen,
bis Free Cash unter deploy_idle_cash_min_free_cash_pct des Equity fällt.
``pct <= 0`` deaktiviert das Feature vollständig (fail-closed).

Testebenen:
  1. Modulebene: deploy_bump_active() + bumped_amount() — pure Decision-Logik.
  2. Helper-Ebene: _deploy_bump_or_reject() mit Stub-Repos (kein Broker,
     keine DB, keine API) — Bump-Pfad, Reject-Pfad, Cap-Klammerung,
     Floor>Cap-Guard, DEPLOY-BUMP-Logzeile, Fail-open.
  3. Struktur: alle 4 Floor-Stellen im Signal-Worker (1× SIGNAL_FLOOR vor
     den Haircuts, 3× DUST_FLOOR: Korrelation / Region / Kettenende) laufen
     durch _deploy_bump_or_reject; das Broker-Minimum-Gate bleibt ein harter
     Reject (Order unter Broker-Minimum scheitert an der Execution, error 720).
"""
import re
from pathlib import Path

import pytest

from bot.core.deploy_idle_cash import bumped_amount, deploy_bump_active

REPO = Path(__file__).resolve().parents[2]
SW_PATH = REPO / "src" / "bot" / "workers" / "signal_worker.py"
CONFIG_PATH = REPO / "config" / "config.yaml"

FLOOR = 300.0
ACTIVE = dict(cash=9000.0, equity=10000.0, pct=50.0)   # 90 % >= 50 % → aktiv
INACTIVE = dict(cash=4000.0, equity=10000.0, pct=50.0)  # 40 % < 50 % → inaktiv


def _sw_source():
    return SW_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Modulebene: deploy_bump_active() + bumped_amount()
# ---------------------------------------------------------------------------

def test_pct_0_deaktiviert_feature():
    assert deploy_bump_active(9000.0, 10000.0, 0.0) is False


def test_pct_negativ_deaktiviert_feature():
    assert deploy_bump_active(9000.0, 10000.0, -1.0) is False


def test_equity_zero_fail_closed():
    assert deploy_bump_active(9000.0, 0.0, 50.0) is False


def test_cash_negativ_fail_closed():
    assert deploy_bump_active(-1.0, 10000.0, 50.0) is False


def test_cash_ueber_freigabe_aktiv():
    assert deploy_bump_active(9000.0, 10000.0, 50.0) is True


def test_freigabe_grenzwert_inklusive():
    # Free Cash EXAKT 50 % → Modus bleibt aktiv (>= Schwellwert)
    assert deploy_bump_active(5000.0, 10000.0, 50.0) is True


def test_cash_unterschreitet_freigabe():
    # Free Cash 49.99 % < 50 % → Deployment stoppt
    assert deploy_bump_active(4999.0, 10000.0, 50.0) is False


def test_bumped_amount_klammerung_auf_floor():
    assert bumped_amount(0.01, FLOOR) == FLOOR
    assert bumped_amount(200.0, FLOOR) == FLOOR


def test_bumped_amount_1cent_guard():
    assert bumped_amount(0.0, FLOOR) == FLOOR


def test_bumped_amount_cap_klammerung():
    # max_trade_pct-Cap unter dem Floor → Cap gewinnt (Klammer + Guard)
    assert bumped_amount(200.0, FLOOR, cap=250.0) == 250.0
    # Cap ueber dem Floor → Floor
    assert bumped_amount(200.0, FLOOR, cap=500.0) == FLOOR
    # Cap None → Floor
    assert bumped_amount(200.0, FLOOR, cap=None) == FLOOR


def test_bumped_amount_never_below_buy_amount_in_range():
    """Invariante: Klammerung verkleinert nie (solange buy_amount <= floor)."""
    for amt in (0.0, 10.0, 100.0, 250.0, FLOOR):
        for cap in (None, FLOOR, 600.0):
            result = bumped_amount(amt, FLOOR, cap=cap)
            assert result >= amt
            assert result <= FLOOR + 1e-9
            if cap is not None:
                assert result <= cap + 1e-9


# ---------------------------------------------------------------------------
# 2. Helper-Ebene: _deploy_bump_or_reject() mit Stub-Repos
# ---------------------------------------------------------------------------

class _LogRepo:
    def __init__(self, broken=False):
        self.writes = []
        self.broken = broken

    def write(self, *args, **kw):
        if self.broken:
            raise RuntimeError("db down")
        self.writes.append((args, kw))


class _SignalRepo:
    def __init__(self):
        self.updates = []

    def update_signal_status(self, *a, **kw):
        self.updates.append((a, kw))


@pytest.fixture
def bump_helper():
    from bot.workers.signal_worker import _deploy_bump_or_reject
    return _deploy_bump_or_reject


def _call(h, *, amt, floor, cap=None, active, log=None, sig=None,
          blocked=None, kind="DUST_FLOOR", stage="Test"):
    log = log if log is not None else _LogRepo()
    sig = sig if sig is not None else _SignalRepo()
    blocked = blocked if blocked is not None else []
    out, bumped = h(
        log, sig, blocked,
        symbol="AAA", signal_id=1, buy_amount=amt, floor=floor, cap=cap,
        deploy_active=active, cfg_pct=ACTIVE["pct"],
        cash_estimate=ACTIVE["cash"], equity=ACTIVE["equity"],
        kind=kind, stage=stage,
    )
    return out, bumped, log, sig, blocked


def test_helper_rejects_when_deploy_inactive(bump_helper):
    out, bumped, log, sig, blocked = _call(
        bump_helper, amt=200.0, floor=FLOOR, active=False)
    assert out == 200.0 and bumped is False
    assert len(sig.updates) == 1            # Signal persistiert als REJECTED
    assert "DUST_FLOOR" in blocked[0]
    assert len(log.writes) == 1             # Reject-Logzeile
    args, _ = log.writes[0]
    assert "BLOCKED" in args[2]             # Log-Nachricht
    assert args[3]["floor_kind"] == "DUST_FLOOR"


def test_helper_bumps_when_deploy_active(bump_helper):
    out, bumped, log, sig, blocked = _call(
        bump_helper, amt=200.0, floor=FLOOR, active=True)
    assert out == FLOOR and bumped is True
    assert blocked == [] and sig.updates == []
    assert log.writes == []                 # Bump: nur Logger, kein Reject-Log


def test_helper_cap_above_floor_bumps_to_floor(bump_helper):
    """Cap ueber dem Floor (normale DEFENSIVE-Konstellation) -> Bump auf Floor."""
    out, bumped, *_ = _call(
        bump_helper, amt=200.0, floor=FLOOR, cap=500.0, active=True)
    assert out == FLOOR and bumped is True


def test_helper_rejects_when_floor_over_cap(bump_helper):
    """Floor > Cap: der Bump wuerde einen Trade erzeugen, den der
    execution_worker-Guard in DEFENSIVE verwerfen wuerde (verbrannter
    Slot) → REJECT statt durchlassen (Klammer + Guard IMMER gemeinsam)."""
    out, bumped, log, sig, blocked = _call(
        bump_helper, amt=200.0, floor=FLOOR, cap=200.0, active=True)
    assert out == 200.0 and bumped is False
    assert len(sig.updates) == 1 and len(blocked) == 1


def test_helper_failopen_on_broken_log(bump_helper):
    """Repo-Write kaputt → Bump bleibt aktiv, kein gefaktes Reject."""
    out, bumped, log, sig, blocked = _call(
        bump_helper, amt=200.0, floor=FLOOR, active=True,
        log=_LogRepo(broken=True))
    assert out == FLOOR and bumped is True
    assert sig.updates == [] and blocked == []


def test_helper_signal_floor_kind_log(bump_helper):
    """SIGNAL_FLOOR-Stelle (vor den Haircuts) laeuft durch denselben Pfad."""
    out, bumped, log, *_ = _call(
        bump_helper, amt=200.0, floor=FLOOR, active=True,
        kind="SIGNAL_FLOOR", stage="vor Haircuts")
    assert out == FLOOR and bumped is True
    assert log.writes == []                 # Bump: nur Logger, kein Reject-Log


# ---------------------------------------------------------------------------
# 3. Struktur: Signal-Worker-Stellen
# ---------------------------------------------------------------------------

def test_config_key_daempft_nicht_mehr():
    """Der Config-Key ist der Free-Cash-Schwellwert, kein 0.0-Default."""
    config = CONFIG_PATH.read_text(encoding="utf-8")
    m = re.search(r"deploy_idle_cash_min_free_cash_pct:\s*([0-9.]+)", config)
    assert m is not None, "Config-Key fehlt"
    assert float(m.group(1)) > 0.0, "Key muss > 0 sein (Feature an)"


def test_deploy_setup_once_per_cycle():
    """Free-Cash-Freigabe wird EINMAL pro Zyklus ausgewertet (nicht je
    Stelle) — sonst driften die 4 Stellen bei einem Cash-Wechsel."""
    src = _sw_source()
    assert "deploy_idle_cash_min_free_cash_pct" in src
    assert "_deploy_bump_on" in src
    assert "deploy_active=_deploy_bump_on" in src
    assert "cfg_pct=_deploy_cfg_pct" in src


def test_signal_floor_site_wired_to_bump():
    src = _sw_source()
    m = re.search(
        r"if buy_amount < signal_floor:\s*\n"
        r"\s*#\s*feat/deploy-idle-cash.*?\n"
        r"\s*_dep_amt, _dep_bumped = _deploy_bump_or_reject\(.*?kind=\"SIGNAL_FLOOR\"",
        src, re.DOTALL)
    assert m is not None, "Signal-Floor-Stelle laeuft nicht durch _deploy_bump_or_reject"


def test_all_dust_floor_sites_wired_to_bump():
    src = _sw_source()
    sites = re.findall(r"if buy_amount < dust_floor:", src)
    assert len(sites) == 3, f"erwartet 3 DUST-Floor-Stellen, gefunden {len(sites)}"
    for m in re.finditer(r"if buy_amount < dust_floor:", src):
        window = src[m.start(): m.start() + 400]
        assert "_deploy_bump_or_reject(" in window, (
            f"DUST-Floor-Stelle @ {m.start()} nicht durch Bump-Helper "
            "verdrahtet (Korrelation/Region/Kettenende muss bumpen)")
    # Jede Stelle schreibt den gebumperten Betrag zurueck
    assert src.count("buy_amount = _dep_amt") == 4


def test_broker_min_gate_stays_hard_reject():
    """Broker-Minimum bleibt ein harter Reject — unter Broker-Minimum
    scheitert die Order an der Execution (error 720)."""
    src = _sw_source()
    m = re.search(
        r"if _broker_min and buy_amount < _broker_min:\s*\n"
        r".*?_reject_below_floor_impl\(.*?kind=\"BROKER_MIN\".*?continue",
        src, re.DOTALL)
    assert m is not None, (
        "Broker-Minimum-Gate muss ein harter _reject_below_floor-Call "
        "(kind=BROKER_MIN) bleiben (kein Bump)")


def test_reject_impl_single_source():
    """Alle Floor-Rejects laufen durch _reject_below_floor_impl —
    eine Quelle fuer Log + Status + blocked_reasons."""
    src = _sw_source()
    assert "def _reject_below_floor(" in src
    assert "return _reject_below_floor_impl(" in src
    # 4 Bump-Stellen + 1 Broker-Min = 5 Reject-Faehige Stellen
    bump_sites = len(re.findall(r"= _deploy_bump_or_reject\(", src))
    assert bump_sites == 4, f"erwartet 4 Bump-Stellen, gefunden {bump_sites}"
    # _reject_below_floor_impl-Call-Sites (def-Zeile ausgenommen):
    #   1x return _reject_below_floor_impl( in _reject_below_floor
    #   3x in _deploy_bump_or_reject (floor>cap Guard + cap<amt Guard + final)
    #   1x Broker-Minimum
    total = len(re.findall(r"_reject_below_floor_impl\(", src))
    def_line = len(re.findall(r"def _reject_below_floor_impl\(", src))
    assert total - def_line == 5, (
        f"erwartet 5 _reject_below_floor_impl-Call-Sites (delegation 1 + "
        f"helper 3 + broker-min 1), gefunden {total - def_line}")


def test_dust_floor_kettenende_still_last_check():
    """Am Kettenende (vor DB-Insertion) muss die DUST-Floor-Pruefung die
    letzte Groessenentscheidung sein — kein Multiplikator/Clamp nach ihr."""
    src = _sw_source()
    last_dust = src.rindex("if buy_amount < dust_floor:")
    create = src.index("trade_id = trade_repo.create(")
    assert last_dust < create, "Dust-Floor-Pruefung liegt nicht mehr vor der Order"
    dazwischen = src[last_dust:create]
    for forbidden in ("buy_amount *= ", "buy_amount = round", "buy_amount = min"):
        assert forbidden not in dazwischen, (
            f"Multiplikator/Clamp NACH der DUST-Floor-Pruefung: {forbidden!r} "
            "— sie muss am Ende der Sizing-Kette bleiben")
