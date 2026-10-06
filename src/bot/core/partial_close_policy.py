"""Partial-Close Policy (Teilschliessungen) — config-gated mode selector.

feat/partial-close-policy (2026-10-01)

VoLLi asked for two selectable behaviors, implemented TOGETHER (both
variants, selectable via config — nothing live until the mode is flipped):

    current      — no change (profit-side partials allowed, as today)
    no_partials  — Variant A ("Wegfall"): suppress ALL partial closes.
                   Positions hold to their final exit (SL / BE / FULL_EXIT /
                   STALE_EXIT / 50%-rule full close).
    loss_only    — Variant B: suppress PROFIT-side partials, allow LOSS-side
                   partials ("Teilschliessungen only in the loss case").

Why both map to "hold to final exit" today: the bot's partial closes are
inherently profit-side (the Profit-Ladder, Momentum-Fade min_lock +2%, and
the LLM TIGHTEN/EXIT all fire on gains — measured: POST 2026-09-05,
loss-side partials = 0). A loss-side partial would only ever fire via the
50%-rule UPGRADE, which produces a FULL close (loss protection) — so
`loss_only` keeps every loss-exit path intact and removes only the
profit-taking trims.

Design invariants
-----------------
* Fail-open: any missing PnL, bad close_pct, unknown mode, or config error
  -> ALLOW (never suppress on a data gap). Mirrors the AGENTS.md "read-only
  safety + fail-open" discipline.
* Full closes are NEVER touched: close_pct >= PARTIAL_THRESHOLD_PCT (99.5)
  passes through unchanged. BE_CLOSE / SL / FULL_EXIT / STALE_EXIT and the
  50%-rule full-close upgrade therefore always run (loss protection).
* Self-contained: module DEFAULT_CONFIG dict shallow-merged with the
  config.yaml `partial_close` section (same pattern as
  trailing_stop.apply_config / entry_quality / ma200_history). No external
  state, no new tables except the shadow ledger.
* Shadow ledger: every decision (allowed or suppressed) is written to
  `partial_close_shadow` in ALL modes, so the forward balance impact of
  `no_partials` / `loss_only` is measurable without touching live behavior.
  Ledger write failures never propagate (never raise).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Config (module default, shallow-merged with config.yaml `partial_close`)
DEFAULT_CONFIG: dict = {
    "mode": "current",   # current | no_partials | loss_only
    "ledger": True,      # record every decision (shadow evidence)
    "partial_threshold_pct": 99.5,  # close_pct strictly below this => "partial"
    # fix/pc-shadow-dedup (2026-10-06): same-rung re-fires whose PnL moved by
    # <= this many percentage points refresh ONE ledger row (sliding reference)
    # instead of appending a duplicate; a jump beyond it is a genuinely new
    # decision stage (new row). Code default is authoritative; config.yaml may
    # override it but does not have to.
    "dedup_pnl_tolerance_pct": 2.0,
}

VALID_MODES = ("current", "no_partials", "loss_only")

_config_cache: Optional[dict] = None


def load_config(force: bool = False) -> dict:
    """Merge DEFAULT_CONFIG with the raw config.yaml `partial_close` section.

    Reads the RAW yaml (like the workers' _load_config / trailing_stop.
    apply_config) — the bot.config.Config dataclass drops unknown sections,
    so we cannot go through it. Fail-open: on any error, return
    DEFAULT_CONFIG (mode=current). Cached after first load (the mode is a
    strategy-level setting, not a per-cycle knob); force=True bypasses the
    cache (used by tests).
    """
    global _config_cache
    if _config_cache is not None and not force:
        return _config_cache
    cfg = dict(DEFAULT_CONFIG)
    try:
        import yaml
        from bot.config import _PROJECT_ROOT
        cfg_path = _PROJECT_ROOT / "config" / "config.yaml"
        raw = {}
        if cfg_path.exists():
            raw = yaml.safe_load(cfg_path.read_text()) or {}
        pc = raw.get("partial_close") or {}
        for k in DEFAULT_CONFIG:
            if k in pc and pc[k] is not None:
                cfg[k] = pc[k]
    except Exception as e:
        logger.debug("[partial_close_policy] config not loaded (defaults): %s", e)
    _config_cache = cfg
    return cfg


def mode() -> str:
    """Current policy mode (from config)."""
    return str(load_config().get("mode", "current"))


@dataclass
class PolicyDecision:
    allowed: bool                            # should the partial execute?
    mode: str                                # the mode this was decided under
    suppressed_reason: Optional[str] = None  # set when not allowed (else None)


def evaluate(pnl_pct: Optional[float], close_pct: Optional[float],
             mode: Optional[str] = None) -> PolicyDecision:
    """Pure decision. Fail-open: bad input -> allowed=True (never suppress
    on a data gap). Only acts on a genuine partial (close_pct < threshold).
    Full closes (close_pct >= threshold) always pass through.

    mode: None -> read from config; pass an explicit mode in tests.
    """
    m = mode if mode is not None else load_config().get("mode", "current")
    thr = float(load_config().get("partial_threshold_pct", 99.5))
    if m not in VALID_MODES:
        return PolicyDecision(True, m, "invalid-mode-fail-open")
    if close_pct is None:
        return PolicyDecision(True, m, "close-pct-unknown-fail-open")
    if close_pct >= thr:
        # Full close (or effectively full) — never touched by this gate.
        return PolicyDecision(True, m, None)
    if pnl_pct is None:
        # Data gap — fail-open, allow the partial.
        return PolicyDecision(True, m, "pnl-unknown-fail-open")
    in_loss = pnl_pct < 0
    if m == "current":
        return PolicyDecision(True, m, None)
    if m == "no_partials":
        return PolicyDecision(False, m, "no_partials:suppress-all")
    # loss_only
    if in_loss:
        return PolicyDecision(True, m, "loss_only:allow-loss")
    return PolicyDecision(False, m, "loss_only:suppress-profit")


def check(db: Any, *, path: str, symbol: str, position_id: Optional[str],
          instrument_id: Optional[int], pnl_pct: Optional[float],
          close_pct: float, amount_usd: Optional[float],
          record: bool = True) -> PolicyDecision:
    """Evaluate AND (optionally) record the decision (shadow ledger).
    Returns the decision; NEVER raises (ledger write is best-effort).

    path: 'trailing' | 'sell_exit' | 'llm'
    record: False skips the ledger (dry-run / simulation — do not pollute
        the shadow data with non-live decisions).
    """
    m = mode()
    d = evaluate(pnl_pct, close_pct, mode=m)
    if record:
        try:
            record_decision(db, mode=m, path=path, symbol=symbol,
                            position_id=position_id, instrument_id=instrument_id,
                            pnl_pct=pnl_pct, close_pct=close_pct, amount_usd=amount_usd,
                            allowed=d.allowed, reason=d.suppressed_reason)
        except Exception as e:
            logger.debug("[partial_close_policy] check/record failed (allow): %s", e)
            # If the ledger blew up, fail-open: allow (never let a ledger bug
            # suppress a partial).
            return PolicyDecision(True, m, "ledger-error-fail-open")
    return d


# ── Shadow ledger (partial_close_shadow) ─────────────────────────────────────
_TABLE = """
CREATE TABLE IF NOT EXISTS partial_close_shadow (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    mode          TEXT NOT NULL,
    path          TEXT,             -- trailing | sell_exit | llm
    symbol        TEXT,
    position_id   TEXT,
    instrument_id INTEGER,
    pnl_pct       REAL,             -- live PnL% at decision time
    close_pct     REAL,             -- recommended partial size (%)
    amount_usd    REAL,             -- current position value (USD)
    allowed       INTEGER NOT NULL, -- 1 = executed/allowed, 0 = suppressed
    reason        TEXT,             -- suppressed_reason (None when allowed)
    pnl_usd_est   REAL              -- derived $ of the (would-be) partial
)
"""
_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_pcs_mode_ts ON partial_close_shadow(mode, ts)",
    "CREATE INDEX IF NOT EXISTS idx_pcs_pos ON partial_close_shadow(position_id)",
)


def ensure_table(db: Any) -> None:
    """Idempotent migration (AGENTS.md): CREATE TABLE IF NOT EXISTS.
    Never raises."""
    if db is None:
        return
    try:
        db.execute(_TABLE)
        for idx in _IDX:
            db.execute(idx)
    except Exception as e:
        logger.debug("[partial_close_policy] ensure_table failed: %s", e)


def _pnl_usd_est(amount_usd, pnl_pct, close_pct) -> Optional[float]:
    """Derived $ of the would-be partial. Returns None when inputs are
    missing (fail-open — never force 0.0)."""
    if amount_usd and pnl_pct is not None and close_pct:
        return amount_usd * (close_pct / 100.0) * (pnl_pct / 100.0)
    return None


def _resolve_amount_usd(db: Any, position_id: Optional[str],
                        amount_usd: Optional[float]) -> Optional[float]:
    """fix/pc-shadow-null (2026-10-06): resolve a NULL amount_usd so the
    shadow row carries a real value. Priority: (a) live portfolio_snapshot
    for the position, (b) last non-NULL amount_usd already in the ledger.
    Fail-open: on any error or no source, return the original (possibly
    None). Never raises."""
    if amount_usd is not None or not position_id:
        return amount_usd
    pid = str(position_id)
    try:
        row = db.fetchone(
            "SELECT amount_usd FROM portfolio_snapshot "
            "WHERE api_position_id = ? AND amount_usd IS NOT NULL "
            "ORDER BY last_synced DESC, instrument_id DESC LIMIT 1", (pid,),
        )
        if row is not None and row["amount_usd"] is not None:
            return float(row["amount_usd"])
    except Exception as e:
        logger.debug("[partial_close_policy] snapshot amount resolve: %s", e)
    try:
        row = db.fetchone(
            "SELECT amount_usd FROM partial_close_shadow "
            "WHERE position_id = ? AND amount_usd IS NOT NULL "
            "ORDER BY ts DESC, id DESC LIMIT 1", (pid,),
        )
        if row is not None and row["amount_usd"] is not None:
            return float(row["amount_usd"])
    except Exception as e:
        logger.debug("[partial_close_policy] ledger amount resolve: %s", e)
    return amount_usd


def _dedup_refresh(db: Any, *, mode: str, path: str, position_id: Optional[str],
                   pnl_pct: Optional[float], close_pct: Optional[float],
                   amount_usd: Optional[float], pnl_usd_est: Optional[float],
                   allowed: bool, reason: Optional[str],
                   tolerance_pct: float) -> Optional[dict]:
    """fix/pc-shadow-dedup (2026-10-06): if the latest ledger row for the
    same (position_id, path, close_pct) has a pnl_pct within tolerance of the
    incoming value, refresh THAT row in place (sliding reference) instead of
    appending a duplicate — an ongoing same-rung decision collapses to ONE
    row. Returns a decision dict (updated=True, decision_id=row id) on a
    match, else None (caller falls through to the normal INSERT).

    Fail-open: on ANY error, return None (append). Never raises."""
    if not position_id or close_pct is None or pnl_pct is None:
        return None
    try:
        row = db.fetchone(
            "SELECT id, pnl_pct FROM partial_close_shadow "
            "WHERE position_id = ? AND path = ? "
            "AND ROUND(close_pct, 2) = ROUND(?, 2) "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (str(position_id), path, close_pct),
        )
        if row is None or row["pnl_pct"] is None:
            return None
        if abs(row["pnl_pct"] - pnl_pct) > tolerance_pct:
            return None  # genuine PnL jump -> new decision stage (append)
        cur = db.execute(
            "UPDATE partial_close_shadow SET ts = datetime('now'), "
            "pnl_pct = ?, amount_usd = ?, pnl_usd_est = ?, allowed = ? "
            "WHERE id = ?",
            (pnl_pct, amount_usd, pnl_usd_est, 1 if allowed else 0, row["id"]),
        )
        return {
            "inserted": False, "updated": True, "decision_id": row["id"],
            "rowcount": int(cur.rowcount) if cur is not None else 0,
        }
    except Exception as e:
        logger.debug("[partial_close_policy] dedup refresh (append): %s", e)
        return None


def record_decision(db: Any, *, mode: str, path: str, symbol: str,
           position_id: Optional[str], instrument_id: Optional[int],
           pnl_pct: Optional[float], close_pct: Optional[float],
           amount_usd: Optional[float], allowed: bool,
           reason: Optional[str]) -> Optional[dict]:
    """Record one decision (allowed or suppressed). Never raises.

    Returns a decision dict on a successful write:
      {inserted, updated, decision_id, rowcount}
    ``updated=True`` (fix/pc-shadow-dedup) marks a same-rung in-place refresh
    of an existing row; ``inserted=True`` marks a new row. Returns None when
    db is None, the ledger is disabled, or the write failed (fail-open)."""
    if db is None:
        return None
    cfg = load_config()
    if not cfg.get("ledger", True):
        return None
    ensure_table(db)  # idempotent self-heal (CREATE TABLE IF NOT EXISTS)
    try:
        # fix/pc-shadow-null: resolve a NULL amount (live snapshot, then the
        # last ledger value) BEFORE computing the derived $ and writing.
        amount_usd = _resolve_amount_usd(db, position_id, amount_usd)
        if amount_usd is None and position_id:
            logger.debug(
                "[partial_close_policy] amount_usd stayed None (no snapshot/"
                "ledger source) pos=%s path=%s", position_id, path,
            )
        pnl_usd_est = _pnl_usd_est(amount_usd, pnl_pct, close_pct)

        # fix/pc-shadow-dedup: an ongoing same-rung re-fire (same
        # position_id/path/close_pct, PnL within tolerance) refreshes the one
        # existing row instead of appending a duplicate. A genuine PnL jump
        # falls through to the INSERT below.
        dedup = _dedup_refresh(
            db, mode=mode, path=path, position_id=position_id,
            pnl_pct=pnl_pct, close_pct=close_pct, amount_usd=amount_usd,
            pnl_usd_est=pnl_usd_est, allowed=allowed, reason=reason,
            tolerance_pct=float(cfg.get("dedup_pnl_tolerance_pct", 2.0)),
        )
        if dedup is not None:
            return dedup

        cur = db.execute(
            "INSERT INTO partial_close_shadow "
            "(ts, mode, path, symbol, position_id, instrument_id, pnl_pct, "
            " close_pct, amount_usd, allowed, reason, pnl_usd_est) "
            "VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, path, symbol,
             str(position_id) if position_id else None,
             instrument_id, pnl_pct, close_pct, amount_usd,
             1 if allowed else 0, reason, pnl_usd_est),
        )
        return {
            "inserted": True, "updated": False,
            "decision_id": cur.lastrowid if cur is not None else None,
            "rowcount": int(cur.rowcount) if cur is not None else 0,
        }
    except Exception as e:
        logger.debug("[partial_close_policy] record failed: %s", e)
        return None
