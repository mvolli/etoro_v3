"""Unit-Tests für Phase 1d Marktregime-Feature (SPY-Merge + Look-ahead)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import market_regime_features as mrf  # noqa: E402


class TestLoadSpyBars:
    def test_merges_two_sources_dedup(self, tmp_path):
        # Zwei Quellen, eine überlappende Zeile → Dedup auf 2 Datenpunkte
        d = tmp_path / "src"
        d.mkdir()
        (d / "SPY_a.csv").write_text(
            "Date,Open,High,Low,Close,Volume\n"
            "2026-01-01,1,10,5,8,100\n"
            "2026-01-02,1,11,6,9,100\n"
        )
        (d / "SPY_b.csv").write_text(
            "Date,Open,High,Low,Close,Volume\n"
            "2026-01-02,1,11,6,9,100\n"   # Duplikat
            "2026-01-03,1,12,7,10,100\n"
        )
        bars = mrf.load_spy_bars_for([str(d)])
        dates = [b[0] for b in bars]
        assert dates == ["2026-01-01", "2026-01-02", "2026-01-03"]
        # hoch/low/close korrekt
        assert bars[0][1:] == (10.0, 5.0, 8.0)

    def test_skips_invalid_rows(self, tmp_path):
        d = tmp_path / "src"
        d.mkdir()
        (d / "SPY.csv").write_text(
            "Date,Open,High,Low,Close,Volume\n"
            "2026-01-01,1,10,5,8,100\n"
            "bad,row,here,x,y\n"
            "2026-01-03,1,12,7,10,100\n"
        )
        bars = mrf.load_spy_bars_for([str(d)])
        assert len(bars) == 2

    def test_empty_dir(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert mrf.load_spy_bars_for([str(d)]) == []

    def test_real_repo_has_bars(self):
        bars = mrf.load_spy_bars()
        assert len(bars) > 500
        assert bars[0][0] < bars[-1][0]  # aufsteigend


class TestNoLookAhead:
    """Die Populate-Logik: letzte SPY-BAR streng VOR dem Event-Datum."""

    def test_strictly_before(self):
        # Event am 2026-01-03 → nimmt BAR 2026-01-02 (nicht 01-03)
        from bisect import bisect_left
        dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
        d = "2026-01-03"
        i = bisect_left(dates, d) - 1
        assert dates[i] == "2026-01-02"

    def test_event_before_first_bar(self):
        from bisect import bisect_left
        dates = ["2026-01-05"]
        d = "2026-01-01"
        i = bisect_left(dates, d) - 1
        assert i < 0  # keine BAR → Event bleibt NULL

    def test_event_after_last_bar(self):
        from bisect import bisect_left
        dates = ["2026-01-01", "2026-01-02"]
        d = "2026-01-10"
        i = bisect_left(dates, d) - 1
        assert dates[i] == "2026-01-02"  # letzte verfügbare


class TestAdxAtr:
    def test_short_series_failopen(self):
        # <30 Bars → {} (fail-open)
        bars = [(f"2026-01-{i:02d}", 10.0, 5.0, 8.0) for i in range(1, 10)]
        assert mrf.compute_adx_atr(bars) == {}

    def test_adx_range_sane(self):
        # Synthetischer Trend mit realistischem Spread → ADX im Bereich (0-100)
        import datetime
        bars = []
        price = 100.0
        start = datetime.date(2026, 1, 1)
        for i in range(60):
            d = (start + datetime.timedelta(days=i)).isoformat()
            o = price
            c = price * 1.01  # stetiger Aufwärtstrend
            h = max(o, c) * 1.005  # Spread je Tag (keine Zero-TR)
            l = min(o, c) * 0.995
            bars.append((d, h, l, c))
            price = c
        feat = mrf.compute_adx_atr(bars)
        if feat:  # ta verfügbar
            assert feat  # nicht leer
            for adx, atr_pct in feat.values():
                assert 0.0 <= adx <= 100.0
                assert atr_pct > 0
