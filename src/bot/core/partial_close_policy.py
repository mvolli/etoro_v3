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


def record_decision(db: Any, *, mode: str, path: str, symbol: str,
           position_id: Optional[str], instrument_id: Optional[int],
           pnl_pct: Optional[float], close_pct: Optional[float],
           amount_usd: Optional[float], allowed: bool,
           reason: Optional[str]) -> None:
    """Record one decision (allowed or suppressed). Never raises."""
    if db is None:
        return
    if not load_config().get("ledger", True):
        return
    ensure_table(db)  # idempotent self-heal (CREATE TABLE IF NOT EXISTS)
    try:
        pnl_usd_est = None
        if amount_usd and pnl_pct is not None and close_pct:
            pnl_usd_est = amount_usd * (close_pct / 100.0) * (pnl_pct / 100.0)
        db.execute(
            "INSERT INTO partial_close_shadow "
            "(ts, mode, path, symbol, position_id, instrument_id, pnl_pct, "
            " close_pct, amount_usd, allowed, reason, pnl_usd_est) "
            "VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, path, symbol,
             str(position_id) if position_id else None,
             instrument_id, pnl_pct, close_pct, amount_usd,
             1 if allowed else 0, reason, pnl_usd_est),
        )
    except Exception as e:
        logger.debug("[partial_close_policy] record failed: %s", e)
