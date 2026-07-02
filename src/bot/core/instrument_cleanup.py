#!/usr/bin/env python3
"""Instrument cleanup logic — fix/instrument-db-cleanup.

Pure classification logic for cleaning up the `instruments` table:
delisted corpses, placeholder IDs, empty yfinance mappings. No DB, no
API — fully unit-testable. scripts/cleanup_instruments.py is the CLI
glue around this.

Design principles (hard rules):
  - NEVER delete a row that is referenced by trades / signals /
    portfolio_snapshot — history must stay resolvable. Referenced
    corpses are DEACTIVATED instead.
  - NEVER delete a row that eToro still knows (when live verification
    data is provided) — a 'delisted' yahoo_status often just means the
    local yfinance_symbol was wrong (VALT.L incident), not that the
    instrument is dead.
  - Rows whose eToro live symbol DIFFERS from the local symbol are
    flagged MISMATCH and deactivated (never auto-renamed here — the
    symbol audit owns renames; auto-renaming risks UNIQUE collisions).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# The two fabricated-ID generations known in this codebase:
#   discovery hash placeholders: 100_000–999_999 (removed in
#   fix/discovery-identity-verification)
PLACEHOLDER_ID_MIN = 100_000
PLACEHOLDER_ID_MAX = 999_999

# Classification actions
ACTION_KEEP = "KEEP"                  # healthy / rescued by eToro verification
ACTION_DELETE = "DELETE"              # corpse, unreferenced → hard delete
ACTION_DEACTIVATE = "DEACTIVATE"      # corpse/mismatch but referenced → is_active=0
ACTION_REVIEW = "REVIEW"              # eToro symbol mismatch → audit should decide


@dataclass
class CleanupDecision:
    instrument_id: int
    action: str
    reason: str
    symbol: str = ""
    etoro_symbol: str = ""


@dataclass
class CleanupPlan:
    keep: list[CleanupDecision] = field(default_factory=list)
    delete: list[CleanupDecision] = field(default_factory=list)
    deactivate: list[CleanupDecision] = field(default_factory=list)
    review: list[CleanupDecision] = field(default_factory=list)

    def add(self, d: CleanupDecision) -> None:
        {
            ACTION_KEEP: self.keep,
            ACTION_DELETE: self.delete,
            ACTION_DEACTIVATE: self.deactivate,
            ACTION_REVIEW: self.review,
        }[d.action].append(d)

    @property
    def total(self) -> int:
        return len(self.keep) + len(self.delete) + len(self.deactivate) + len(self.review)


def is_placeholder_id(instrument_id: int) -> bool:
    """True for IDs in the known fabricated-placeholder range."""
    return PLACEHOLDER_ID_MIN <= instrument_id <= PLACEHOLDER_ID_MAX


def is_corpse_candidate(row: dict) -> tuple[bool, str]:
    """Pure check: does this instruments row LOOK like a corpse?

    row keys (missing keys treated as None): instrument_id, symbol,
    yfinance_symbol, yahoo_status, is_active.

    A candidate is not a verdict — verification against eToro and the
    reference check decide the final action.
    """
    iid = int(row.get("instrument_id") or 0)
    status = (row.get("yahoo_status") or "").strip().lower()
    yf_sym = (row.get("yfinance_symbol") or "").strip()

    if is_placeholder_id(iid):
        return True, f"Placeholder-ID {iid} (fabrizierter Bereich {PLACEHOLDER_ID_MIN}–{PLACEHOLDER_ID_MAX})"
    if status == "delisted":
        return True, "yahoo_status=delisted"
    if not yf_sym:
        return True, "yfinance_symbol leer — Instrument kann nie Daten/Signale liefern"
    return False, ""


def _norm(sym: str) -> str:
    """Loose symbol normalization for eToro↔local comparison (keeps
    exchange suffixes, strips USD quote suffixes — mirrors
    bot.core.instrument_verification.normalize_symbol)."""
    if not sym:
        return ""
    s = sym.upper().strip()
    for suffix in ("-USD", "/USD", "USD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s


def classify_instrument(
    row: dict,
    referenced_ids: set[int],
    etoro_symbols: dict[int, str] | None,
) -> CleanupDecision:
    """Decide the cleanup action for one instruments row.

    Parameters
    ----------
    row : dict
        instruments row (instrument_id, symbol, yfinance_symbol,
        yahoo_status, is_active — missing keys tolerated).
    referenced_ids : set[int]
        instrument_ids referenced by trades/signals/portfolio_snapshot.
    etoro_symbols : dict[int, str] | None
        Live verification result: {instrument_id: symbolFull} for every
        ID eToro could resolve. None = verification NOT performed
        (conservative mode: nothing gets deleted, corpses only
        deactivated). An ID absent from the dict = eToro does not know
        it.
    """
    iid = int(row.get("instrument_id") or 0)
    symbol = (row.get("symbol") or "").strip()

    candidate, why = is_corpse_candidate(row)
    if not candidate:
        return CleanupDecision(iid, ACTION_KEEP, "gesund", symbol=symbol)

    referenced = iid in referenced_ids

    # ── With live eToro verification ─────────────────────────────────────
    if etoro_symbols is not None:
        live_symbol = etoro_symbols.get(iid, "")
        if live_symbol:
            # eToro knows this ID → NOT a corpse, whatever yahoo says.
            if _norm(live_symbol) == _norm(symbol):
                return CleanupDecision(
                    iid, ACTION_KEEP,
                    f"eToro bestätigt ID↔Symbol ({live_symbol}) — '{why}' ist ein "
                    f"Datenqualitätsproblem (yfinance_symbol prüfen), keine Leiche",
                    symbol=symbol, etoro_symbol=live_symbol,
                )
            # ID exists but resolves to a DIFFERENT ticker → mapping broken.
            action = ACTION_REVIEW if not referenced else ACTION_DEACTIVATE
            return CleanupDecision(
                iid, action,
                f"eToro-Symbol '{live_symbol}' ≠ lokal '{symbol}' — Zuordnung "
                f"kaputt, gehört in den Symbol-Audit (kein Auto-Rename)",
                symbol=symbol, etoro_symbol=live_symbol,
            )
        # eToro does NOT know the ID → confirmed corpse.
        if referenced:
            return CleanupDecision(
                iid, ACTION_DEACTIVATE,
                f"{why}; eToro kennt ID nicht; referenziert durch "
                f"trades/signals/portfolio → deaktivieren statt löschen",
                symbol=symbol,
            )
        return CleanupDecision(
            iid, ACTION_DELETE,
            f"{why}; eToro kennt ID nicht; keine Referenzen → sicher löschbar",
            symbol=symbol,
        )

    # ── Without verification: conservative — never delete ────────────────
    return CleanupDecision(
        iid, ACTION_DEACTIVATE,
        f"{why}; ohne --verify-etoro wird nichts gelöscht → deaktivieren",
        symbol=symbol,
    )


def build_plan(
    rows: list[dict],
    referenced_ids: set[int],
    etoro_symbols: dict[int, str] | None,
) -> CleanupPlan:
    """Classify all rows into a CleanupPlan."""
    plan = CleanupPlan()
    for row in rows:
        plan.add(classify_instrument(row, referenced_ids, etoro_symbols))
    return plan


def load_audit_corrections(raw: object) -> dict[int, dict]:
    """Tolerant loader for audit-scan result files.

    Accepts any of:
      {"corrections": {"1456": {...}}}   — audit v5 style wrapper
      {"1456": {...}}                    — flat id→fields mapping
      [{"instrument_id": 1456, ...}]     — list of row dicts

    Recognised correction fields: symbol, name, yfinance_symbol,
    yahoo_status. Everything else is ignored. Returns {} for anything
    unparseable — the cleanup then simply runs without corrections.
    """
    allowed = {"symbol", "name", "yfinance_symbol", "yahoo_status"}
    out: dict[int, dict] = {}

    def _put(key: object, fields: object) -> None:
        try:
            iid = int(key)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if not isinstance(fields, dict):
            return
        clean = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
        if clean:
            out[iid] = clean

    if isinstance(raw, dict):
        inner = raw.get("corrections", raw)
        if isinstance(inner, dict):
            for k, v in inner.items():
                _put(k, v)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "instrument_id" in item:
                _put(item.get("instrument_id"), item)

    return out
