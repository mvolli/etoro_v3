"""fix/pc-shadow-dedup + fix/pc-shadow-null (2026-10-06).

Unit tests for the two partial_close_shadow ledger fixes:
  FIX 1 (ZM-Dedup): a same-rung re-fire whose PnL is within tolerance
    refreshes ONE existing row (sliding reference) instead of appending a
    duplicate; a genuine PnL jump still appends a new row.
  FIX 2 (NULL-Fix): a NULL amount_usd is resolved from the live
    portfolio_snapshot first, then the last ledger value, so the row carries
    a real value and a real pnl_usd_est.

Uses a temp-file sqlite DB (NEVER the production data/trading.db — conftest
installs a write-barrier on data/) and monkeypatches load_config for
hermetic behavior.
"""
import pytest

from bot.core import partial_close_policy as pcp
from bot.db.connection import DB


# ── Fixture: fresh temp DB + hermetic config ─────────────────────────────────
@pytest.fixture
def db(tmp_path):
    dbp = tmp_path / "test.db"
    conn = DB(dbp)
    conn._get_persistent()  # open now so table-creation below is on the same conn
    # Minimal schema: the shadow ledger + the snapshot table the NULL-fixer reads.
    pcp.ensure_table(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS portfolio_snapshot ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " api_position_id TEXT,"
        " instrument_id INTEGER,"
        " amount_usd REAL,"
        " last_synced TEXT)"
    )
    yield conn
    conn.close()


@pytest.fixture
def cfg(monkeypatch):
    """Hermetic config: ledger on, 2.0pp dedup tolerance, mode=current."""
    monkeypatch.setattr(
        pcp, "load_config",
        lambda force=False: {
            "mode": "current",
            "ledger": True,
            "partial_threshold_pct": 99.5,
            "dedup_pnl_tolerance_pct": 2.0,
        },
    )


def _count(db):
    r = db.fetchone("SELECT COUNT(*) AS n FROM partial_close_shadow")
    return r["n"] if r else 0


# ── FIX 1: ZM-Dedup ──────────────────────────────────────────────────────────
def test_near_identical_refreshes_same_row(db, cfg):
    """Two same-rung decisions within 2pp collapse to ONE row."""
    a = pcp.record_decision(
        db, mode="current", path="trailing", symbol="ZM.DE",
        position_id="3590737653", instrument_id=1,
        pnl_pct=5.0, close_pct=35.0, amount_usd=50.54, allowed=True, reason=None,
    )
    assert a["inserted"] is True and a["updated"] is False
    assert _count(db) == 1

    b = pcp.record_decision(
        db, mode="current", path="trailing", symbol="ZM.DE",
        position_id="3590737653", instrument_id=1,
        pnl_pct=6.2, close_pct=35.0, amount_usd=51.0, allowed=True, reason=None,
    )
    # Within 2pp (|6.2-5.0| = 1.2) -> in-place refresh, no new row.
    assert b["inserted"] is False and b["updated"] is True
    assert _count(db) == 1
    # The single row reflects the LATEST state (sliding reference).
    row = db.fetchone(
        "SELECT pnl_pct, amount_usd FROM partial_close_shadow WHERE id=?",
        (b["decision_id"],),
    )
    assert row["pnl_pct"] == 6.2
    assert row["amount_usd"] == 51.0


def test_pnl_jump_beyond_tolerance_appends(db, cfg):
    """A genuine PnL jump (>2pp) is a new decision stage -> new row."""
    a = pcp.record_decision(
        db, mode="current", path="trailing", symbol="ZM.DE",
        position_id="3590737653", instrument_id=1,
        pnl_pct=5.0, close_pct=35.0, amount_usd=50.54, allowed=True, reason=None,
    )
    assert a["inserted"] is True
    assert _count(db) == 1

    b = pcp.record_decision(
        db, mode="current", path="trailing", symbol="ZM.DE",
        position_id="3590737653", instrument_id=1,
        pnl_pct=8.4, close_pct=35.0, amount_usd=52.0, allowed=True, reason=None,
    )
    # |8.4-5.0| = 3.4 > 2.0 -> new row.
    assert b["inserted"] is True and b["updated"] is False
    assert _count(db) == 2
    # The earlier row is NOT clobbered.
    rows = db.fetchall("SELECT pnl_pct FROM partial_close_shadow ORDER BY id")
    assert [r["pnl_pct"] for r in rows] == [5.0, 8.4]


def test_different_path_not_collapsed(db, cfg):
    """Same position+close_pct but a DIFFERENT path is a distinct decision."""
    a = pcp.record_decision(
        db, mode="current", path="trailing", symbol="ZM.DE",
        position_id="3590737653", instrument_id=1,
        pnl_pct=5.0, close_pct=35.0, amount_usd=50.54, allowed=True, reason=None,
    )
    b = pcp.record_decision(
        db, mode="current", path="llm", symbol="ZM.DE",
        position_id="3590737653", instrument_id=1,
        pnl_pct=5.4, close_pct=35.0, amount_usd=50.6, allowed=True, reason=None,
    )
    assert b["inserted"] is True and b["updated"] is False
    assert _count(db) == 2


def test_dedup_never_raises_on_empty(db, cfg):
    """First-ever decision (no prior row) always inserts; fail-open on None."""
    a = pcp.record_decision(
        db, mode="current", path="sell_exit", symbol="ZM.DE",
        position_id="3590737653", instrument_id=1,
        pnl_pct=None, close_pct=35.0, amount_usd=50.54, allowed=True, reason=None,
    )
    # pnl_pct is None -> dedup skipped (requires pnl), so a fresh insert.
    assert a is not None and a["inserted"] is True
    assert _count(db) == 1


# ── FIX 2: NULL-Fix ──────────────────────────────────────────────────────────
def test_null_amount_resolved_from_snapshot(db, cfg):
    """A NULL amount_usd is backfilled from the live portfolio_snapshot."""
    db.execute(
        "INSERT INTO portfolio_snapshot "
        "(api_position_id, instrument_id, amount_usd, last_synced) "
        "VALUES (?,?,?,?)",
        ("3590737653", 1, 60.0, "2026-10-06T00:00:00"),
    )
    pcp.record_decision(
        db, mode="current", path="llm", symbol="G24.DE",
        position_id="3590737653", instrument_id=1,
        pnl_pct=10.0, close_pct=50.0, amount_usd=None, allowed=True, reason=None,
    )
    row = db.fetchone(
        "SELECT amount_usd, pnl_usd_est FROM partial_close_shadow "
        "ORDER BY id DESC LIMIT 1"
    )
    # amount_usd must NOT be NULL now.
    assert row["amount_usd"] is not None and row["amount_usd"] == 60.0
    # pnl_usd_est is derived: 60.0 * 0.50 * 0.10 = 3.0
    assert row["pnl_usd_est"] is not None
    assert abs(row["pnl_usd_est"] - 3.0) < 1e-9


def test_null_amount_falls_back_to_ledger(db, cfg):
    """No snapshot -> fall back to the last non-NULL ledger amount_usd.

    Same path+close_pct, PnL within tolerance -> the row is refreshed in
    place AND its NULL amount is resolved from the prior ledger row.
    """
    pcp.record_decision(
        db, mode="current", path="llm", symbol="ALAT.PA",
        position_id="3593777677", instrument_id=2,
        pnl_pct=4.0, close_pct=25.0, amount_usd=100.0, allowed=True, reason=None,
    )
    assert _count(db) == 1
    pcp.record_decision(
        db, mode="current", path="llm", symbol="ALAT.PA",
        position_id="3593777677", instrument_id=2,
        pnl_pct=4.2, close_pct=25.0, amount_usd=None, allowed=True, reason=None,
    )
    # |4.2-4.0| within tolerance -> refresh in place; amount resolved to 100.0.
    row = db.fetchone(
        "SELECT amount_usd, pnl_pct, pnl_usd_est FROM partial_close_shadow "
        "ORDER BY id DESC LIMIT 1"
    )
    assert row["amount_usd"] == 100.0
    assert row["pnl_pct"] == 4.2
    assert _count(db) == 1  # collapsed, not duplicated


def test_null_amount_no_source_stays_none(db, cfg):
    """No snapshot, no ledger history -> amount stays None (fail-open, no 0.0)."""
    pcp.record_decision(
        db, mode="current", path="llm", symbol="NEW",
        position_id="0000000000", instrument_id=9,
        pnl_pct=5.0, close_pct=50.0, amount_usd=None, allowed=True, reason=None,
    )
    row = db.fetchone("SELECT amount_usd, pnl_usd_est FROM partial_close_shadow")
    # Still None (never force 0.0); derived $ also None.
    assert row["amount_usd"] is None
    assert row["pnl_usd_est"] is None


def test_explicit_amount_not_overridden(db, cfg):
    """A provided amount_usd is used as-is (no resolution attempt)."""
    db.execute(
        "INSERT INTO portfolio_snapshot "
        "(api_position_id, instrument_id, amount_usd, last_synced) "
        "VALUES (?,?,?,?)",
        ("3590737653", 1, 60.0, "2026-10-06T00:00:00"),
    )
    pcp.record_decision(
        db, mode="current", path="llm", symbol="G24.DE",
        position_id="3590737653", instrument_id=1,
        pnl_pct=10.0, close_pct=50.0, amount_usd=75.0, allowed=True, reason=None,
    )
    row = db.fetchone("SELECT amount_usd FROM partial_close_shadow")
    assert row["amount_usd"] == 75.0  # caller value wins, not the 60.0 snapshot
