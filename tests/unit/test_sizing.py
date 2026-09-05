#!/usr/bin/env python3
"""Unit tests — src/bot/core/sizing.py (risk-neutral Kelly position sizing).

fix/kelly-risk-neutral (2026-08-22, post commit-review-2026-08-21-b):

    factor = clamp(kelly_base + kelly_scale * kelly,
                   kelly_min_factor, kelly_max_factor)

Defaults: base=0.49 (calibrated so the trade-weighted mean over the live
90d trade mix is ~0.30 — the tested account risk level), scale=0.45,
min_trades=25 (was 10), floor=0.15 (was 0.5), cap=0.94 (base+scale —
kelly ist auf [-1,1] geklemmt, also die natuerliche Obergrenze).

Negative-edge samples now pull the factor DOWN to the floor (kelly can be
negative); the old scale floored every combo at 0.5 and boosted 10-trade
samples to 1.35 — measured 2.47x the tested risk on trading.db.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import bot.core.sizing as sizing_mod
from bot.core.sizing import (
    kelly_size_factor,
    DEFAULT_BASE,
    DEFAULT_MAX_FACTOR,
    DEFAULT_MIN_FACTOR,
    DEFAULT_MIN_TRADES,
    DEFAULT_SCALE,
)


@pytest.fixture(autouse=True)
def _pin_sizing_defaults(monkeypatch):
    """Tests laufen gegen die DEFAULT_*-Konstanten, NICHT gegen das
    repo's config.yaml — sonst zerbroecht jeder Tuning-Edit des Users
    (z.B. kelly_base -> 0.6) die Sizing-Tests."""
    monkeypatch.setattr(sizing_mod, "_get_sizing_cfg", lambda: {})


def _make_db(rows: list[dict], st: str | None = None) -> MagicMock:
    """Mock DB: fetchall() returns (signal_type, pnl_pct) rows.

    fix/kelly-components (2026-07-26): die Query liefert den signal_type
    ('st') jeder Zeile — Rows ohne eigenes 'st' bekommen den uebergebenen
    Default (simuliert den Exact-Match-Fall).
    """
    db = MagicMock()
    mock_rows = []
    for r in rows:
        _r = {"st": st, **r}
        mr = MagicMock()
        mr.__getitem__ = lambda self, k, _r=_r: _r[k]
        mock_rows.append(mr)
    db.fetchall.return_value = mock_rows
    return db


class TestKellySizeFactor:
    """Risk-neutral Kelly scale edge cases."""

    def test_insufficient_data_returns_base(self):
        """Fewer than min_trades → base (no scaling)."""
        db = _make_db([{"pnl_pct": 1.5}, {"pnl_pct": -0.5}], st="BB_LOWER_RSI_OVERSOLD")
        assert kelly_size_factor("BB_LOWER_RSI_OVERSOLD", db, min_trades=10) == pytest.approx(DEFAULT_BASE)

    def test_default_min_trades_is_25(self):
        """fix/kelly-risk-neutral: thin Combos (10-Trade-Minimum) stay at base."""
        assert DEFAULT_MIN_TRADES == 25
        # 15 rows: enough for the old default of 10, not for 25.
        rows = [{"pnl_pct": 1.0} for _ in range(15)]
        db = _make_db(rows, st="THIN")
        assert kelly_size_factor("THIN", db) == pytest.approx(DEFAULT_BASE)

    def test_all_winners_returns_max(self):
        """All profitable trades → kelly capped at +1.0 → max_factor."""
        rows = [{"pnl_pct": 2.0 + i * 0.1} for i in range(15)]
        db = _make_db(rows, st="GOLDEN_CROSS")
        assert kelly_size_factor("GOLDEN_CROSS", db, min_trades=10) == pytest.approx(DEFAULT_MAX_FACTOR)

    def test_all_losers_returns_min_factor(self):
        """All losing trades → kelly=-1.0 → min_factor floor (0.15).

        fix/kelly-risk-neutral: proven loss-bringers shrink to the floor
        instead of getting a guaranteed 0.5 — at a negative account-level
        expectancy a 0.5 floor is still a 1.67x increase."""
        rows = [{"pnl_pct": -(1.0 + i * 0.1)} for i in range(15)]
        db = _make_db(rows, st="TREND_PULLBACK")
        assert kelly_size_factor("TREND_PULLBACK", db, min_trades=10) == pytest.approx(
            DEFAULT_MIN_FACTOR
        )

    def test_negative_avg_pnl_clamped_to_min_factor(self):
        """Strongly negative Kelly value is clamped to the min_factor floor.

        30% win rate, avg_win=1% vs avg_loss=5% → kelly = 0.3 - 0.7/0.2 = -3.2
        → clamped to -1.0 → base - scale → floor."""
        wins = [{"pnl_pct": 1.0}] * 3
        losses = [{"pnl_pct": -5.0}] * 7
        db = _make_db(wins + losses, st="BAD_SIGNAL")
        factor = kelly_size_factor("BAD_SIGNAL", db, min_trades=5)
        assert factor == pytest.approx(DEFAULT_MIN_FACTOR)
        assert factor >= DEFAULT_MIN_FACTOR

    def test_good_edge_boosts_size_risk_neutral(self):
        """Strong edge (70% win, avg_win=2%, avg_loss=1%).

        kelly = 0.7 - 0.3/2 = 0.55 → 0.49 + 0.45*0.55 = 0.7375.
        Above base (reward) but BELOW the old-scale value (1.55 → capped 1.5):
        the reward mechanism stays, the level does not get raised."""
        wins = [{"pnl_pct": 2.0}] * 7
        losses = [{"pnl_pct": -1.0}] * 3
        db = _make_db(wins + losses, st="BB_EXTREME_RSI_OVERSOLD")
        factor = kelly_size_factor("BB_EXTREME_RSI_OVERSOLD", db, min_trades=5)
        assert factor == pytest.approx(0.49 + 0.45 * 0.55)
        assert factor > DEFAULT_BASE
        assert factor <= DEFAULT_MAX_FACTOR

    def test_excellent_edge_stays_bounded(self):
        """Excellent edge (80% win, avg_win=4%, avg_loss=1%).

        kelly = 0.8 - 0.2/4 = 0.75 → 0.49 + 0.45*0.75 = 0.8275 < cap 0.94.
        Reward without a size increase above the tested risk level."""
        wins = [{"pnl_pct": 4.0}] * 8
        losses = [{"pnl_pct": -1.0}] * 2
        db = _make_db(wins + losses, st="RSI_EXTREME_OVERSOLD")
        factor = kelly_size_factor("RSI_EXTREME_OVERSOLD", db, min_trades=5)
        assert factor == pytest.approx(0.49 + 0.45 * 0.75)
        assert DEFAULT_MIN_FACTOR <= factor <= DEFAULT_MAX_FACTOR

    def test_db_error_returns_base(self):
        """DB exception → graceful fallback to base."""
        db = MagicMock()
        db.fetchall.side_effect = Exception("DB locked")
        assert kelly_size_factor("ANY_SIGNAL", db) == pytest.approx(DEFAULT_BASE)

    def test_min_factor_configurable(self):
        """kelly_cfg.kelly_min_factor overrides the default floor."""
        rows = [{"pnl_pct": -1.0} for _ in range(15)]
        db = _make_db(rows, st="BAD")
        factor = kelly_size_factor(
            "BAD", db, min_trades=10, kelly_cfg={"kelly_min_factor": 0.25}
        )
        assert factor == pytest.approx(0.25)

    def test_base_and_scale_configurable(self):
        """kelly_cfg.kelly_base / kelly_scale override the defaults."""
        rows = [{"pnl_pct": 1.0}] * 8 + [{"pnl_pct": -1.0}] * 2
        # kelly = 0.8 - 0.2/1 = 0.6
        db = _make_db(rows, st="TUNE")
        factor = kelly_size_factor(
            "TUNE", db, min_trades=5,
            kelly_cfg={"kelly_base": 0.3, "kelly_scale": 0.5,
                       "kelly_min_factor": 0.1, "kelly_max_factor": 1.0},
        )
        assert factor == pytest.approx(0.3 + 0.5 * 0.6)

    def test_result_always_in_range(self):
        """Output is always in [min_factor, max_factor] regardless of input."""
        import random
        random.seed(42)
        for _ in range(50):
            rows = [{"pnl_pct": random.gauss(0.5, 3.0)} for _ in range(20)]
            db = _make_db(rows, st="ANY")
            f = kelly_size_factor("ANY", db, min_trades=5)
            assert DEFAULT_MIN_FACTOR <= f <= DEFAULT_MAX_FACTOR, f"factor {f} out of range"

    def test_component_pool_fallback(self):
        """fix/kelly-components: thin exact combo pools shared components.

        Exact combo "A,B" has only 2 trades; component "A" appears in 32
        trades (30 in "A,C" + 2 in "A,B") → the component pool is used.
        All pool PnLs are +2.0 winners → kelly capped at +1.0 → max_factor."""
        pool = [{"pnl_pct": 2.0, "st": "A,C"} for _ in range(30)]
        exact = [{"pnl_pct": 2.0, "st": "A,B"} for _ in range(2)]
        db = _make_db(pool + exact)
        factor = kelly_size_factor("A,B", db, min_trades=25)
        assert factor == pytest.approx(DEFAULT_MAX_FACTOR)

    def test_component_pool_fallback_negative_pool_hits_floor(self):
        """Thin exact combo pools a negative component pool → min_factor."""
        pool = [{"pnl_pct": -1.0, "st": "A,C"} for _ in range(30)]
        exact = [{"pnl_pct": -1.0, "st": "A,B"} for _ in range(2)]
        db = _make_db(pool + exact)
        factor = kelly_size_factor("A,B", db, min_trades=25)
        assert factor == pytest.approx(DEFAULT_MIN_FACTOR)


# ── feat/kelly-asset-class-split (2026-09-05) ────────────────────────────────
# Krypto-Trades duerfen nicht die Groesse fuer Aktien-Trades DESSELBEN
# Signal-Clusters setzen. Gemessen am Dip-Cluster (90d, trading.db):
#   Krypto n= 9  Ø +16.52%  6/9 Wins   <- Fenster XRP +45.2% / BTC +24.3%
#   Aktien n=72  Ø  +0.08% 20/72 Wins
# Der Cluster trug 0.573 — den hoechsten Faktor ueberhaupt — obwohl 89 % der
# Stichprobe bei null liegt. Der Schalter ist per Default AUS (Blue/Green).

class _BucketDB:
    """Fake-DB, die den asset_class-Filter der SQL tatsaechlich beachtet.

    Der echte Loader haengt den Filter als SQL-Fragment an; hier wird das
    Fragment gelesen und auf die kanonischen Zeilen angewandt — sonst
    wuerde der Test den Filter gar nicht pruefen (nur dass er nicht knallt).
    """

    def __init__(self, rows):
        self._rows = rows          # (signal_type, pnl_pct, asset_class)

    def fetchall(self, sql, params=()):
        rows = self._rows
        if "<> 'crypto'" in sql:
            rows = [r for r in rows if r[2] != "crypto"]
        elif "= 'crypto'" in sql:
            rows = [r for r in rows if r[2] == "crypto"]
        return [{"st": st, "pnl_pct": p} for st, p, _ in rows]

    def fetchone(self, sql, params=()):
        return {"asset_class": "crypto"} if params and params[0] == 999 else \
               {"asset_class": "stock"}


def _mixed_pool():
    """40 flache Aktien-Trades + 10 starke Krypto-Trades, EIN Cluster."""
    flat = [("DIP", 0.05, "stock")] * 20 + [("DIP", -0.05, "stock")] * 20
    hot = [("DIP", 18.0, "crypto")] * 10
    return flat + hot


class TestAssetClassSplit:

    def test_bucket_mapping(self):
        assert sizing_mod._asset_bucket("crypto") == "crypto"
        assert sizing_mod._asset_bucket("CRYPTO ") == "crypto"
        assert sizing_mod._asset_bucket("stock") == "other"
        assert sizing_mod._asset_bucket("etf") == "other"
        assert sizing_mod._asset_bucket("") is None
        assert sizing_mod._asset_bucket(None) is None

    def test_default_is_off_behaviour_unchanged(self):
        """Ohne Flag zaehlen Krypto-Trades weiter mit — bisheriges Verhalten."""
        db = _BucketDB(_mixed_pool())
        mit = kelly_size_factor("DIP", db, kelly_cfg={"kelly_min_trades": 25})
        ohne_krypto = kelly_size_factor(
            "DIP", _BucketDB([r for r in _mixed_pool() if r[2] != "crypto"]),
            kelly_cfg={"kelly_min_trades": 25})
        assert mit > ohne_krypto, "Krypto muss den Faktor ohne Flag anheben"

    def test_split_removes_crypto_from_equity_factor(self):
        """Der Kern: mit Flag setzt Krypto die Aktien-Groesse nicht mehr."""
        cfg = {"kelly_min_trades": 25, "kelly_asset_class_split": True}
        db = _BucketDB(_mixed_pool())
        f_stock = kelly_size_factor("DIP", db, kelly_cfg=cfg, asset_class="stock")
        f_ohne = kelly_size_factor(
            "DIP", _BucketDB([r for r in _mixed_pool() if r[2] != "crypto"]),
            kelly_cfg={"kelly_min_trades": 25})
        assert f_stock == pytest.approx(f_ohne), (
            "mit Split muss der Aktien-Faktor dem reinen Aktien-Pool entsprechen"
        )

    def test_split_lowers_the_contaminated_cluster(self):
        db = _BucketDB(_mixed_pool())
        aus = kelly_size_factor("DIP", db, kelly_cfg={"kelly_min_trades": 25})
        an = kelly_size_factor("DIP", db, asset_class="stock",
                               kelly_cfg={"kelly_min_trades": 25,
                                          "kelly_asset_class_split": True})
        assert an < aus

    def test_crypto_candidate_sees_only_crypto(self):
        cfg = {"kelly_min_trades": 5, "kelly_asset_class_split": True}
        db = _BucketDB(_mixed_pool())
        f = kelly_size_factor("DIP", db, kelly_cfg=cfg, asset_class="crypto")
        assert f > DEFAULT_BASE, "reiner Krypto-Pool ist stark positiv"

    def test_instrument_id_resolves_asset_class(self):
        cfg = {"kelly_min_trades": 25, "kelly_asset_class_split": True}
        db = _BucketDB(_mixed_pool())
        via_id = kelly_size_factor("DIP", db, kelly_cfg=cfg, instrument_id=1)
        via_ac = kelly_size_factor("DIP", db, kelly_cfg=cfg, asset_class="stock")
        assert via_id == pytest.approx(via_ac)

    def test_asset_class_wins_over_instrument_id(self):
        cfg = {"kelly_min_trades": 5, "kelly_asset_class_split": True}
        db = _BucketDB(_mixed_pool())
        f = kelly_size_factor("DIP", db, kelly_cfg=cfg,
                              asset_class="crypto", instrument_id=1)
        f_crypto = kelly_size_factor("DIP", db, kelly_cfg=cfg, asset_class="crypto")
        assert f == pytest.approx(f_crypto)

    def test_unknown_asset_class_falls_open(self):
        """Kein Bucket -> ungefilterter Pool, kein Absturz (fail-open)."""
        cfg = {"kelly_min_trades": 25, "kelly_asset_class_split": True}
        db = _BucketDB(_mixed_pool())
        f = kelly_size_factor("DIP", db, kelly_cfg=cfg, asset_class=None)
        aus = kelly_size_factor("DIP", db, kelly_cfg={"kelly_min_trades": 25})
        assert f == pytest.approx(aus)

    def test_instrument_lookup_failure_is_safe(self):
        class _Broken(_BucketDB):
            def fetchone(self, *a, **k):
                raise RuntimeError("db weg")
        cfg = {"kelly_min_trades": 25, "kelly_asset_class_split": True}
        f = kelly_size_factor("DIP", _Broken(_mixed_pool()), kelly_cfg=cfg,
                              instrument_id=1)
        assert DEFAULT_MIN_FACTOR <= f <= DEFAULT_MAX_FACTOR
