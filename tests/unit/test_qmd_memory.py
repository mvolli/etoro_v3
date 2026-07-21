"""feat/qmd-memory: Recall/Reindex fail-open + Parsing."""
import json
from unittest import mock

from bot.core import qmd_memory


def _fake_proc(stdout="", returncode=0):
    m = mock.Mock()
    m.stdout = stdout
    m.returncode = returncode
    return m


def test_recall_parses_and_filters_by_score():
    rows = [
        {"score": 0.5, "file": "a/b.md", "title": "T1", "snippet": "hit one"},
        {"score": 0.01, "file": "c.md", "title": "T2", "snippet": "too weak"},
    ]
    with mock.patch("subprocess.run", return_value=_fake_proc(json.dumps(rows))):
        out = qmd_memory.qmd_recall("stop loss", min_score=0.03)
    assert len(out) == 1 and out[0]["title"] == "T1"


def test_recall_failopen_on_error():
    with mock.patch("subprocess.run", side_effect=OSError("no binary")):
        assert qmd_memory.qmd_recall("x") == []
    with mock.patch("subprocess.run", return_value=_fake_proc("not json", 0)):
        assert qmd_memory.qmd_recall("x") == []
    with mock.patch("subprocess.run", return_value=_fake_proc("", 1)):
        assert qmd_memory.qmd_recall("x") == []


def test_recall_empty_query():
    assert qmd_memory.qmd_recall("   ") == []


def test_recall_block_format():
    rows = [{"score": 0.4, "file": "docs/llm_insights.md", "title": "Insights", "snippet": "MACD-Split WR 32%"}]
    with mock.patch("subprocess.run", return_value=_fake_proc(json.dumps(rows))):
        block = qmd_memory.qmd_recall_block("macd")
    assert "QMD-Recall" in block and "MACD-Split" in block
    with mock.patch("subprocess.run", return_value=_fake_proc("[]")):
        assert qmd_memory.qmd_recall_block("nix") == ""


def test_reindex_failopen():
    with mock.patch("subprocess.run", return_value=_fake_proc("", 0)):
        assert qmd_memory.qmd_reindex() is True
    with mock.patch("subprocess.run", side_effect=OSError):
        assert qmd_memory.qmd_reindex() is False
