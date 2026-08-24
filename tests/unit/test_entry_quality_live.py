"""feat/entry-quality Phase 2 (2026-08-24): Live-Pfad — Sizing statt Shadow-Log.

Deckt ab, was ab jetzt echtes Geld bewegt: die Kombination der Gates, die
Klemme auf ``min_size_mult``, das Fail-Open-Verhalten und — als
Regressionsschutz fuer die Anti-Brake-Vorgabe — dass die ausgelieferte
config.yaml keine Hard-Blocks enthaelt.
"""
from pathlib import Path

import yaml

from bot.core import entry_quality as EQ
from bot.db.connection import DB

REPO = Path(__file__).resolve().parents[2]


def _cfg(mode="live", min_size_mult=0.25, **gate_over):
    gates = {
        "macd_turn_required": {
            "enabled": True, "size_mult": 0.5,
            "oversold_types": ["RSI_EXTREME"],
            "macd_turn_types": ["MACD_TURN_BELOW_SMA20"],
        },
        "core_sweep_regime": {
            "enabled": True, "size_mult": 0.25,
            "allowed_regimes": ["NORMAL", "CAUTION"], "trend_override": False,
        },
        "dipbuy_trend": {
            "enabled": True, "size_mult": 0.5,
            "applies_to": ["RSI_EXTREME"], "min_roc_5d_pct": -15.0,
        },
        "volume_confirm": {"enabled": True, "size_mult": 0.5, "min_vol_ratio": 1.2},
        "atr_window": {
            "enabled": True, "size_mult": 0.5, "min_atr_pct": 0.8, "max_atr_pct": 7.0,
        },
    }
    for name, over in gate_over.items():
        gates[name].update(over)
    return {"trading": {"entry_quality": {
        "enabled": True, "mode": mode,
        "min_size_mult": min_size_mult, "gates": gates,
    }}}


# gesunde Werte: kein Gate greift
_CLEAN = {"vol_ratio": 1.5, "atr": 2.0, "price": 100.0, "roc_5d_pct": 1.0}


# ─── evaluate(): Gate-Logik ──────────────────────────────────────────────────

def test_clean_entry_has_no_hits():
    ev = EQ.evaluate(_cfg(), symbol="AAPL", signal_type="MACD_TURN_BELOW_SMA20",
                     indicators=_CLEAN, regime="NORMAL")
    assert ev.hits == []
    assert ev.size_mult == 1.0
    assert ev.blocked is False


def test_pure_oversold_without_macd_turn_halves_size():
    ev = EQ.evaluate(_cfg(), symbol="AAPL", signal_type="RSI_EXTREME",
                     indicators=_CLEAN, regime="NORMAL")
    assert [h.gate for h in ev.hits] == ["macd_turn_required"]
    assert ev.size_mult == 0.5


def test_macd_turn_present_clears_the_gate():
    ev = EQ.evaluate(_cfg(), symbol="AAPL",
                     signal_type="RSI_EXTREME,MACD_TURN_BELOW_SMA20",
                     indicators=_CLEAN, regime="NORMAL")
    assert ev.size_mult == 1.0


def test_combination_is_min_not_product():
    """Weakest-Link: vier gleichzeitige Treffer a 0.5 ergeben 0.5 — NICHT 0.5**4.

    Wichtige Eigenschaft fuer die Live-Wirkung: Gate-Treffer stapeln sich
    nicht. Ein Entry mit vier Maengeln wird genauso stark verkleinert wie
    einer mit einem einzigen. Das begrenzt die Bremswirkung bewusst.
    """
    ind = {"vol_ratio": 0.3, "atr": 20.0, "price": 100.0, "roc_5d_pct": -30.0}
    ev = EQ.evaluate(_cfg(), symbol="AAPL",
                     signal_type="RSI_EXTREME", indicators=ind, regime="NORMAL")
    gates_hit = {h.gate for h in ev.hits}
    assert {"macd_turn_required", "dipbuy_trend", "volume_confirm", "atr_window"} <= gates_hit
    assert ev.size_mult == 0.5
    assert ev.size_mult != 0.5 ** len(ev.hits), "kombiniert wird per MIN, nicht multiplikativ"
    assert ev.blocked is False


def test_floor_clamps_a_gate_below_min_size_mult():
    """Ein Gate unter der Klemme wird auf min_size_mult angehoben."""
    ev = EQ.evaluate(_cfg(min_size_mult=0.25, volume_confirm={"size_mult": 0.1}),
                     symbol="AAPL", signal_type="MACD_TURN_BELOW_SMA20",
                     indicators={"vol_ratio": 0.3, "atr": 2.0, "price": 100.0},
                     regime="NORMAL")
    assert [h.gate for h in ev.hits] == ["volume_confirm"]
    assert ev.size_mult == 0.25, "Klemme min_size_mult muss greifen"
    assert ev.blocked is False


def test_missing_indicators_fail_open():
    """Fehlende Daten duerfen nicht bestrafen (kein Hit aus datenabhaengigen Gates)."""
    ev = EQ.evaluate(_cfg(), symbol="AAPL", signal_type="MACD_TURN_BELOW_SMA20",
                     indicators={}, regime="NORMAL")
    assert ev.hits == []
    assert ev.size_mult == 1.0


def test_core_sweep_outside_allowed_regime_is_soft_not_block():
    """Phase 2: CORE_SWEEP im falschen Regime wird verkleinert, NICHT geblockt."""
    ev = EQ.evaluate(_cfg(), symbol="AAPL", signal_type="CORE_SWEEP",
                     indicators={}, regime="DEFENSIVE", is_core_sweep=True)
    assert [h.gate for h in ev.hits] == ["core_sweep_regime"]
    assert ev.blocked is False, "0.25 darf kein Block sein"
    assert ev.size_mult == 0.25


def test_hard_block_config_still_blocks_and_bypasses_floor():
    """Gegenprobe: mit size_mult 0.0 bleibt es ein Block (Klemme greift nicht)."""
    ev = EQ.evaluate(_cfg(core_sweep_regime={"size_mult": 0.0}), symbol="AAPL",
                     signal_type="CORE_SWEEP", indicators={},
                     regime="DEFENSIVE", is_core_sweep=True)
    assert ev.blocked is True
    assert ev.size_mult == 0.0


# ─── DB-Schicht: mark_applied / latest_size_mult ─────────────────────────────

def _db(tmp_path):
    d = DB(tmp_path / "t.db")
    EQ.ensure_table(d)
    return d


def test_latest_size_mult_defaults_to_one(tmp_path):
    d = _db(tmp_path)
    assert EQ.latest_size_mult(d, None) == 1.0
    assert EQ.latest_size_mult(d, 999) == 1.0


def test_latest_size_mult_reads_most_recent_row(tmp_path):
    d = _db(tmp_path)
    for mult, sig in ((0.5, "RSI_EXTREME"), (1.0, "MACD_TURN_BELOW_SMA20")):
        ev = EQ.evaluate(_cfg(), symbol="AAPL", signal_type=sig,
                         indicators=_CLEAN, regime="NORMAL")
        assert ev.size_mult == mult
        EQ.record(d, ev, mode="live", applied=False, signal_id=42, instrument_id=7)
    assert EQ.latest_size_mult(d, 42) == 1.0, "juengste Zeile gewinnt"


def test_mark_applied_flags_only_latest_row(tmp_path):
    d = _db(tmp_path)
    ev = EQ.evaluate(_cfg(), symbol="AAPL", signal_type="RSI_EXTREME",
                     indicators=_CLEAN, regime="NORMAL")
    for _ in range(2):
        EQ.record(d, ev, mode="live", applied=False, signal_id=11, instrument_id=1)
    EQ.mark_applied(d, 11)
    rows = d.fetchall(
        "SELECT applied FROM entry_quality_events WHERE signal_id = ? ORDER BY id", (11,))
    assert [r[0] for r in rows] == [0, 1]


def test_mark_applied_is_failopen_on_unknown_signal(tmp_path):
    d = _db(tmp_path)
    EQ.mark_applied(d, 12345)   # darf nicht werfen
    EQ.mark_applied(d, None)


# ─── Regressionsschutz fuer die ausgelieferte Konfiguration ─────────────────

def test_shipped_config_has_no_hard_blocks():
    """Anti-Brake-Vorgabe (Plan-Phase 2): keine Hard-Blocks in config.yaml.

    Ein Gate mit size_mult <= 0 umgeht die min_size_mult-Klemme, verwirft den
    Trade komplett und beendet damit die Datensammlung fuer diesen Cluster.
    """
    cfg = yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8"))
    eq = cfg["trading"]["entry_quality"]
    blocking = {
        name: g.get("size_mult")
        for name, g in eq["gates"].items()
        if g.get("enabled") and float(g.get("size_mult", 1.0)) <= 0.0
    }
    assert not blocking, f"Hard-Blocks in config.yaml: {blocking}"
    assert float(eq["min_size_mult"]) > 0.0
