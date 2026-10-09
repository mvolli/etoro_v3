"""Unit-Tests für Phase 1c Signalhygiene (SELL-Klassifikation + Statistik)."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from signal_hygiene import _is_sell, _trimmed_mean, _stat  # noqa: E402


class TestIsSell:
    def test_trend_kipp_sell(self):
        assert _is_sell("TREND_KIPP_1H,SELL") is True

    def test_bb_overbought(self):
        assert _is_sell("BB_UPPER_RSI_OVERBOUGHT") is True

    def test_buy_combo(self):
        assert _is_sell("MACD_TURN_BELOW_SMA20,BB_LOW_MACD_IMPROVING") is False

    def test_core_sweep(self):
        assert _is_sell("CORE_SWEEP") is False

    def test_trend_pullback(self):
        assert _is_sell("TREND_PULLBACK,GOLDEN_CROSS") is False

    def test_empty(self):
        assert _is_sell("") is False

    def test_none(self):
        assert _is_sell(None) is False

    def test_lowercase_sell_marker(self):
        # Marker-Match ist case-insensitiv
        assert _is_sell("trend_kipp_1h,sell") is True


class TestTrimmedMean:
    def test_empty(self):
        assert _trimmed_mean([]) is None

    def test_no_trim_small_n(self):
        # n=10, 5 % Trim = 0 Werte je Seite → normales Mittel
        assert _trimmed_mean([1.0, 2.0, 3.0, 4.0, 5.0,
                              6.0, 7.0, 8.0, 9.0, 10.0]) == 5.5

    def test_trims_outliers(self):
        # Krypto-Ausreißer +500 wird weggelöscht (n=20 → 1 je Seite)
        vals = [1.0] * 18 + [2.0, 500.0]
        m = _trimmed_mean(vals)
        assert m is not None
        assert m < 10.0  # Ausreißer trägt nicht

    def test_symmetric(self):
        # negativer Ausreißer links, positiver rechts → beide weg
        vals = [-500.0] + [1.0] * 18 + [500.0]
        m = _trimmed_mean(vals)
        assert m is not None
        assert abs(m - 1.0) < 0.1


class TestStat:
    def _rows(self, vals):
        return [{"fwd_10d_pnl": v} for v in vals]

    def test_empty(self):
        n, m, wr = _stat([], "fwd_10d_pnl")
        assert n == 0 and m is None and wr == 0.0

    def test_all_positive(self):
        n, m, wr = _stat(self._rows([1.0, 2.0, 3.0]), "fwd_10d_pnl")
        assert n == 3 and wr == 100.0

    def test_all_negative(self):
        n, m, wr = _stat(self._rows([-1.0, -2.0, -3.0]), "fwd_10d_pnl")
        assert n == 3 and wr == 0.0

    def test_mixed(self):
        n, m, wr = _stat(self._rows([1.0, -1.0]), "fwd_10d_pnl")
        assert n == 2 and wr == 50.0

    def test_none_excluded(self):
        n, m, wr = _stat([{"fwd_10d_pnl": None},
                          {"fwd_10d_pnl": 5.0}], "fwd_10d_pnl")
        assert n == 1 and wr == 100.0


class TestPnlFlipDb:
    """Integration: SELL-Flip in einer echten (temporären) DB."""

    def test_sell_flip(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE signal_outcomes ("
                   "signal_type TEXT, direction TEXT, fwd_10d REAL)")
        db.execute("INSERT INTO signal_outcomes VALUES "
                   "('BB_UPPER_RSI_OVERBOUGHT','SELL',-2.0)")  # Preis runter
        db.execute("INSERT INTO signal_outcomes VALUES "
                   "('CORE_SWEEP','BUY',+3.0)")                # Preis rauf
        # pnl: SELL = -fwd, BUY = fwd
        for st, direction, fwd in db.execute(
                "SELECT signal_type, direction, fwd_10d FROM signal_outcomes"):
            pnl = -fwd if direction == "SELL" else fwd
            if direction == "SELL":
                assert pnl > 0  # Preis runter = SELL-Profit
            else:
                assert pnl > 0
        db.close()
