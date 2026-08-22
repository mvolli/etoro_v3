#!/usr/bin/env python3
"""Entry-Quality Shadow-Mode — End-to-End-Verifikation (Phase 1, 2026-08-22).

Prueft OHNE Live-Beruehrung (Temp-Kopie der trading.db):
  1. Config laedt, trading.entry_quality existiert (mode=shadow, 5 Gates)
  2. evaluate() ueber alle 5 Gate-Szenarien (P1-Cluster / Core-Sweep /
     Dip-Buy / Vol-Confirm / ATR-Fenster) + Fail-Open
  3. ensure_table + record + latest_size_mult gegen eine ECHTE Kopie der
     trading.db via dem echten bot.db.connection.DB-Wrapper
  4. Reale Live-Signal-Muster: Pure-Oversold-Cluster vs. MACD_TURN-Begleiteter
Exit 0 = alle Checks bestanden.
"""
import copy
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bot.core import entry_quality as eq  # noqa: E402
from bot.core.entry_quality import GateHit  # noqa: E402
from bot.db.connection import DB  # noqa: E402
import yaml  # noqa: E402

# Workers laden das config.yaml als ROHES dict (yaml.safe_load) und ueben
# cfg.get("trading",{}) — evaluate() sieht also genau das. Testet dieselbe Form.
with open(ROOT / "config" / "config.yaml", encoding="utf-8") as _fh:
    cfg = yaml.safe_load(_fh) or {}

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def gates_of(ev):
    return [h.gate for h in ev.hits]


# ── 1. Config ──────────────────────────────────────────────────────────────
eqcfg = cfg.get("trading", {}).get("entry_quality", {})
check("config.entry_quality exists", bool(eqcfg))
check("config.mode == shadow", eqcfg.get("mode") == "shadow", f"mode={eqcfg.get('mode')}")
check("config.enabled == true", eqcfg.get("enabled") is True)
check("config.min_size_mult == 0.25", eqcfg.get("min_size_mult") == 0.25)
check("config has 5 gates", len(eqcfg.get("gates", {})) == 5,
      f"gates={sorted(eqcfg.get('gates', {}).keys())}")

# ── 2. evaluate() Szenarien ────────────────────────────────────────────────
print("\nevaluate() Szenarien:")
# P1-Cluster: Pure-Oversold OHNE MACD_TURN + fallender Trend + weak volume
# Kombinierung = MIN ueber alle Hits (schwachste Komponente, Codebase-Konvention)
ev_p1 = eq.evaluate(cfg, symbol="TEST", signal_type="BB_LOWER_RSI_OVERSOLD",
                    indicators={"roc_5d_pct": -20.0, "vol_ratio": 0.5,
                                "atr": 1.0, "price": 100.0, "sma20": 100.0, "sma50": 110.0},
                    regime="NORMAL")
check("P1: macd_turn_required feuert", "macd_turn_required" in gates_of(ev_p1))
check("P1: dipbuy_trend feuert", "dipbuy_trend" in gates_of(ev_p1))
check("P1: volume_confirm feuert", "volume_confirm" in gates_of(ev_p1))
check("P1: size_mult == 0.5 (MIN ueber 3x 0.5-Gates)", abs(ev_p1.size_mult - 0.5) < 1e-9,
      f"size_mult={ev_p1.size_mult} hits={gates_of(ev_p1)}")
check("P1: NOT blocked (nur Soft-Gates)", ev_p1.blocked is False)

# Healthy signal: alles clean -> 1.0
ev_ok = eq.evaluate(cfg, symbol="TEST", signal_type="MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING",
                    indicators={"roc_5d_pct": -5.0, "vol_ratio": 2.0,
                                "atr": 1.0, "price": 100.0, "sma20": 100.0, "sma50": 98.0},
                    regime="NORMAL")
check("OK: keine Gate-Hits", not ev_ok.hits, f"hits={gates_of(ev_ok)}")
check("OK: size_mult == 1.0", abs(ev_ok.size_mult - 1.0) < 1e-9)

# Core-Sweep in DEFENSIVE: Regime-Gate blockt (size_mult 0.0)
ev_cs = eq.evaluate(cfg, symbol="SPY", signal_type="CORE_SWEEP",
                    indicators={}, regime="DEFENSIVE", is_core_sweep=True)
check("CS DEFENSIVE: core_sweep_regime feuert", "core_sweep_regime" in gates_of(ev_cs))
check("CS DEFENSIVE: blocked + size_mult 0.0", ev_cs.blocked is True and ev_cs.size_mult == 0.0)

# Core-Sweep in DEFENSIVE aber mit Trend-Override (sma20>sma50) -> entlastet
ev_cs_ovr = eq.evaluate(cfg, symbol="SPY", signal_type="CORE_SWEEP",
                        indicators={"sma20": 102.0, "sma50": 100.0},
                        regime="DEFENSIVE", is_core_sweep=True)
check("CS DEFENSIVE + Trend-Override: entlastet", "core_sweep_regime" not in gates_of(ev_cs_ovr))

# Core-Sweep in NORMAL + fail-open leere Indikatoren: keine Hits
ev_cs_ok = eq.evaluate(cfg, symbol="SPY", signal_type="CORE_SWEEP",
                       indicators={}, regime="NORMAL", is_core_sweep=True)
check("CS NORMAL: keine Hits (fail-open)", not ev_cs_ok.hits)

# ATR-Fenster (ATR in Preiseinheiten, atr_pct = atr/price*100):
hi = eq.evaluate(cfg, symbol="T", signal_type="BUY",
                 indicators={"atr": 9.0, "price": 100.0}, regime="NORMAL")
lo = eq.evaluate(cfg, symbol="T", signal_type="BUY",
                 indicators={"atr": 0.4, "price": 100.0}, regime="NORMAL")
mid = eq.evaluate(cfg, symbol="T", signal_type="BUY",
                  indicators={"atr": 2.0, "price": 100.0}, regime="NORMAL")
check("ATR: 9% feuert", "atr_window" in gates_of(hi))
check("ATR: 0.4% feuert", "atr_window" in gates_of(lo))
check("ATR: 2% ruhig", "atr_window" not in gates_of(mid))

# Fail-open: komplett leere Indikatoren -> nur macd_turn_required (rest braucht Daten)
ev_empty = eq.evaluate(cfg, symbol="T", signal_type="BB_LOWER_RSI_OVERSOLD",
                       indicators={}, regime="NORMAL")
check("Fail-open: leere Indikatoren -> nur macd_turn_required",
      gates_of(ev_empty) == ["macd_turn_required"], f"hits={gates_of(ev_empty)}")

# ── 3. DB round-trip gegen echte (Temp-)Kopie der trading.db ──────────────
print("\nDB round-trip (Temp-Kopie der echten trading.db, echter DB-Wrapper):")
tmpdir = tempfile.mkdtemp(prefix="eq_test_")
tmpdb = Path(tmpdir) / "trading.db"
shutil.copy2(ROOT / "data" / "trading.db", tmpdb)
db = DB(tmpdb)  # bot.db.connection.DB — dieselbe API wie in den Workers

eq.ensure_table(db)
check("Tabelle entry_quality_events angelegt", db.fetchone(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entry_quality_events'"
) is not None)

row_id = eq.record(db, ev_p1, mode="shadow", applied=False,
                   signal_id=1, instrument_id=42)
check("record() liefert rowid", isinstance(row_id, int) and row_id > 0, f"id={row_id}")

sm = eq.latest_size_mult(db, signal_id=1)
check("latest_size_mult(signal_id) liest den Multiplikator",
      abs(sm - ev_p1.size_mult) < 1e-9, f"sm={sm}")

# is_core_sweep-Column-Default bei record ohne Flag (data_worker-Pfad)
row_id2 = eq.record(db, ev_ok, mode="shadow", applied=False, signal_id=2, instrument_id=43)
check("record() ohne is_core_sweep Default=0", db.fetchone(
    f"SELECT is_core_sweep FROM entry_quality_events WHERE id={row_id2}")[0] == 0)
# explicit is_core_sweep=True (signal_worker-Pfad)
row_id3 = eq.record(db, ev_cs, mode="shadow", applied=False,
                    signal_id=None, instrument_id=3000, is_core_sweep=True)
check("record(is_core_sweep=True) persistiert 1", db.fetchone(
    f"SELECT is_core_sweep FROM entry_quality_events WHERE id={row_id3}")[0] == 1)
# latest_size_mult ohne match -> 1.0 (fail-open)
check("latest_size_mult unbekannte signal_id -> 1.0", eq.latest_size_mult(db, signal_id=999999) == 1.0)
# signal_id None -> 1.0 (fail-open, kein SQL)
check("latest_size_mult(None) -> 1.0", eq.latest_size_mult(db, signal_id=None) == 1.0)

# ── 4. Reale Live-Signal-Muster ────────────────────────────────────────────
print("\nEchtes Signal-Scenario (P1 Pure-Oversold vs. MACD_TURN-Begleitet):")
# Reales Muster aus der Live-DB: BB_LOWER_RSI_OVERSOLD ohne MACD_TURN,
# ROC5d <-15%, vol_ratio <1.2 -> Sizing wird gemauselt
real = eq.evaluate(cfg, symbol="KTA.DE", signal_type="BB_LOWER_RSI_OVERSOLD",
                   indicators={"roc_5d_pct": -18.0, "vol_ratio": 0.8,
                               "atr": 3.0, "price": 50.0, "sma20": 55.0, "sma50": 60.0},
                   regime="CAUTION")
check("REAL: Pure-Oversold-Cluster mauselt (size_mult<1)", 0.0 < real.size_mult < 1.0,
      f"size_mult={real.size_mult} hits={gates_of(real)}")
# Gleiche Indikatoren ABER mit dem echten MACD-Turn-Signaltyp (DB-Name!)
real2 = eq.evaluate(cfg, symbol="KTA.DE", signal_type="BB_LOWER_RSI_OVERSOLD,MACD_TURN_BELOW_SMA20",
                    indicators={"roc_5d_pct": -18.0, "vol_ratio": 0.8,
                                "atr": 3.0, "price": 50.0, "sma20": 55.0, "sma50": 60.0},
                    regime="CAUTION")
check("REAL: MACD_TURN entlastet (macd_turn_required nicht mehr da)",
      "macd_turn_required" not in gates_of(real2) and "macd_turn_required" in gates_of(real))

# ── 5. Core-sweep-Pfad (signal_worker): leere Indikatoren + Regime ─────────
print("\nCore-sweep Execution-Pfad:")
ev_live_cs = eq.evaluate(cfg, symbol="DIA", signal_type="CORE_SWEEP",
                         indicators={}, regime="NORMAL", is_core_sweep=True)
eq.record(db, ev_live_cs, mode="shadow", applied=False,
          signal_id=None, instrument_id=3000, is_core_sweep=True)
n_cs = db.fetchone(
    "SELECT COUNT(*) FROM entry_quality_events WHERE is_core_sweep=1"
)[0]
check("core-sweep-Events werden geloggt (is_core_sweep=1)", n_cs >= 1, f"n={n_cs}")

# ── 6. Zero-Live-Change-Verifikation: config-Kopie ohne entry_quality ─────
print("\nZero-Live-Change (fail-open ohne Config-Bereich):")
cfg_no_eq = copy.deepcopy(cfg)
cfg_no_eq["trading"].pop("entry_quality", None)
ev_no_cfg = eq.evaluate(cfg_no_eq, symbol="T", signal_type="BB_LOWER_RSI_OVERSOLD",
                        indicators={"roc_5d_pct": -20.0, "vol_ratio": 0.5,
                                    "atr": 1.0, "price": 100.0, "sma20": 100.0, "sma50": 110.0},
                        regime="NORMAL")
check("ohne Config: Defaults greifen (Gates enabled, shadow)",
      len(ev_no_cfg.hits) >= 1 and not ev_no_cfg.blocked, f"hits={gates_of(ev_no_cfg)}")

# ── 7. Zero-Live-Change: signal_worker-Codepfad waehlt buy_amount NICHT ──
print("\nZero-Live-Change (Code-Pfad-Inspektion):")
sw = (ROOT / "src" / "bot" / "workers" / "signal_worker.py").read_text(encoding="utf-8")
i_buy = sw.find("_eq_sm = _eq.latest_size_mult(db, signal_id)")
i_min = sw.find("# Enforce minimum from regime params", i_buy)
seg = sw[i_buy:i_min]
check("BUY-Hook: keine buy_amount-Mutation zwischen read und min_enforce",
      "buy_amount =" not in seg and "buy_amount*=" not in seg,
      "read-only shadow-log")
check("BUY-Hook: fail-open wrapped (except)", "except Exception" in seg)

shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n{'='*60}\nERGEBNIS: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print("FEHLER:", FAIL)
    sys.exit(1)
print("ALLE CHECKS BESTANDEN")
