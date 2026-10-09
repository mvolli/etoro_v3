"""Unit-Tests für Phase 1b Confluence-Grading (Score + Grade-Logik)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from confluence_grading import confluence_score, grade  # noqa: E402


def _row(co, ma200, rsi):
    return {"co_signals": co, "ma200_pos": ma200, "rsi_depth": rsi}


class TestConfluenceScore:
    def test_all_features(self):
        assert confluence_score(_row(2, 0.05, 50)) == 3

    def test_no_features(self):
        assert confluence_score(_row(0, -0.05, 70)) == 0

    def test_co_signals_only(self):
        assert confluence_score(_row(1, -0.05, 70)) == 1

    def test_ma200_only(self):
        assert confluence_score(_row(0, 0.05, 70)) == 1

    def test_rsi_only(self):
        assert confluence_score(_row(0, -0.05, 50)) == 1

    def test_none_ma200_counts_nothing(self):
        assert confluence_score(_row(0, None, 50)) == 1  # nur rsi

    def test_none_rsi_counts_nothing(self):
        assert confluence_score(_row(1, 0.05, None)) == 2  # co+ma200

    def test_co_zero_is_no_feature(self):
        assert confluence_score(_row(0, 0.05, 50)) == 2  # ma200+rsi, co=0

    def test_rsi_boundary_60_excluded(self):
        # rsi < 60 → rsi=60 zählt NICHT
        assert confluence_score(_row(0, -0.05, 60)) == 0

    def test_rsi_59_included(self):
        assert confluence_score(_row(0, -0.05, 59)) == 1


class TestGrade:
    def test_a_score_3(self):
        assert grade(3) == "A"

    def test_a_score_2(self):
        assert grade(2) == "A"

    def test_b_score_1(self):
        assert grade(1) == "B"

    def test_c_score_0(self):
        assert grade(0) == "C"


class TestGradeMapping:
    def test_high_confluence_is_a(self):
        assert grade(confluence_score(_row(2, 0.1, 45))) == "A"

    def test_single_feature_is_b(self):
        assert grade(confluence_score(_row(0, 0.1, 70))) == "B"

    def test_no_features_is_c(self):
        assert grade(confluence_score(_row(0, -0.1, 75))) == "C"
