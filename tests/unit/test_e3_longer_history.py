"""Unit-Tests für Phase 1e E3-Test (reine Funktionen: RSI, Trim, t-Stat)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from e3_longer_history_test import rsi_series, trimmed_mean, t_stat  # noqa: E402


class TestRsiSeries:
    def test_warmup_none(self):
        r = rsi_series([100, 101, 102], n=14)
        assert all(v is None for v in r)

    def test_all_down_low_rsi(self):
        # Stetiger Fall → RSI nahe 0
        closes = [100 - i * 0.5 for i in range(40)]
        r = rsi_series(closes)
        assert r[-1] is not None
        assert r[-1] < 20

    def test_all_up_high_rsi(self):
        closes = [100 + i * 0.5 for i in range(40)]
        r = rsi_series(closes)
        assert r[-1] is not None
        assert r[-1] > 80

    def test_range_0_100(self):
        closes = [100 + (i % 7) - 3 for i in range(60)]
        r = rsi_series(closes)
        for v in r:
            if v is not None:
                assert 0.0 <= v <= 100.0


class TestTrimmedMean:
    def test_empty(self):
        assert trimmed_mean([]) is None

    def test_no_trim_small(self):
        assert trimmed_mean([1.0, 2.0, 3.0, 4.0, 5.0], frac=0.05) == 3.0

    def test_outlier_removed(self):
        # +500 Ausreißer wird bei 5 % Trim weggenommen
        vals = [1.0] * 18 + [2.0, 500.0]
        m = trimmed_mean(vals)
        assert m is not None and m < 10.0

    def test_symmetric(self):
        vals = [-500.0] + [1.0] * 18 + [500.0]
        m = trimmed_mean(vals)
        assert m is not None and abs(m - 1.0) < 0.5


class TestTStat:
    def test_single(self):
        assert t_stat([1.0]) == 0.0

    def test_zero_variance(self):
        # Alle gleich → Varianz 0 → t=0
        assert t_stat([2.0, 2.0, 2.0]) == 0.0

    def test_positive_mean_positive_t(self):
        assert t_stat([1.0, 2.0, 3.0, 4.0]) > 0

    def test_negative_mean_negative_t(self):
        assert t_stat([-1.0, -2.0, -3.0, -4.0]) < 0
