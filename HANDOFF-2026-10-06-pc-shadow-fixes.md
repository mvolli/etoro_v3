# HANDOFF — ZM-Dedup-Fix + NULL-Fix (partial_close_shadow ledger)

Stand: 2026-10-06 · Repo: `~/.hermes/workspace/etoro_v3` (LIVE bot, echtes Geld) · Branch: `main`

VoLLi approved implementing TWO data-quality fixes to the shadow ledger
`partial_close_shadow` (written by `src/bot/core/partial_close_policy.py`).
Implement via the **safe-change workflow** (load skill `etoro-v3` →
`references/safe-change-workflow.md`, or `~/.hermes/skills/finance/etoro-v3/
references/safe-change-workflow.md`): pure logic first, idempotent migration,
full test suite green, commit+push on `main`, DOX-pass, live-verification.

## WHY (the bug)

The shadow ledger records EVERY partial-close decision (allowed or suppressed)
so the forward balance impact of `no_partials`/`loss_only` is measurable.
Two data-quality defects make the count statistically worthless:

1. **ZM-Ledger-Spam (dedup):** one *ongoing* ladder decision re-fires a ledger
   row every 5-min cycle. Root cause: the profit-ladder rung fires from
   `trailing_stop` every cycle; `position_state.levels_taken` is only written
   after eToro ACCEPTS the order, which lags / may not happen — so the SAME
   open rung (ZM pos `3590737653`, 35% close, `amount_usd` frozen 50.54,
   PnL drifting 5.0→8.4%) wrote **191 rows** (all close_pct=35, allowed=1,
   191 distinct ts). n=191 is not independent; the real count is ~1 decision.
2. **NULL-Fix:** 4 rows have `amount_usd = NULL` (all `path='llm'`), so
   `pnl_usd_est` (the derived $ of the would-be partial) is silently missing.
   The llm call site (`llm_execution.py`) currently passes `amount_usd=None`.

Live DB facts (read-only, verified 2026-10-06, `data/trading.db`):
- `partial_close_shadow`: 210 rows total, 4 NULL `amount_usd`, ZM=191 rows
  (one position, close_pct 35, amount 50.54, allowed 1, pnl 5.00–8.41).
- Config `partial_close`: `{mode: current, ledger: true, partial_threshold_pct: 99.5}`.
- Backfill sources for the 4 historical NULL rows (from `trades.amount_usd`
  by `api_position_id`): `3587406277`→6690.HK=**72.79**, `3590440419`→G24.DE=
  **60.0**, `3593777677`→ALAT.PA=**100.0** (appears twice, rows id 94 & 123).
  (G24.DE also has a live `portfolio_snapshot.amount_usd`; the other two are
  CLOSED positions → snapshot gone → use `trades`.)

## THE DESIGN (implement exactly this)

All changes are **self-contained in `partial_close_policy.py`** (the one
module all three call sites share: trailing / sell_exit / llm). Fail-open
everywhere: any lookup/compare error → fall back to appending (NEVER drop or
suppress a decision). No new tables; idempotent migration pattern.

### A. Sliding-reference DEDUP (the ZM fix) — in `record_decision()`
Before the INSERT:
- Build a dedup key: `(position_id, path, close_pct)` (all three must be
  known; if any is None → skip dedup, just append).
- Look up the MOST RECENT existing row with the same key:
  `SELECT id, pnl_pct FROM partial_close_shadow WHERE position_id=? AND
  path=? AND ROUND(close_pct,2)=ROUND(?,2) ORDER BY ts DESC, id DESC LIMIT 1`.
- If a row exists AND both its `pnl_pct` and the incoming `pnl_pct` are
  non-NULL AND `abs(incoming − prev) <= DEDUP_PNL_TOLERANCE_PCT` (new config
  key, default **2.0**, see C) → it is the SAME ongoing decision:
  **UPDATE that row in place** instead of INSERT:
  - `ts = datetime('now')` (keeps advancing → shows the decision is still live)
  - keep `pnl_pct`, `close_pct`, `allowed`, `reason` as the ORIGINAL decision
  - **refresh `amount_usd` and `pnl_usd_est` to the incoming (latest) values,
    but ONLY when the incoming value is non-NULL** (this also heals an early
    NULL amount from a later cycle that has it).
  - then `return`.
- A PnL jump beyond the tolerance = a genuinely new decision stage → new row.
- On ANY exception in the dedup path → log at debug and fall through to
  the normal INSERT (append). Never raise.

This collapses ZM's 191 same-rung rows to ~1 (PnL drift 5.0→8.4 stays within
the sliding 2.0pp reference each 5-min step, so it keeps refreshing one row).
Truly distinct PnL stages still get distinct rows.

### B. amount_usd NULL fallback — in `record_decision()` (forward fix)
At the top, when `amount_usd is None` and `position_id` is known:
- First try the latest `portfolio_snapshot.amount_usd` for that
  `api_position_id`.
- If not found, try the latest non-NULL `amount_usd` in the ledger for the
  same `position_id`.
- If still None → leave None (fail-open, as today).
- After a successful backfill, recompute `pnl_usd_est`.
(Keep it best-effort/try-except; a missing snapshot is normal for closed
positions — then the backfill simply doesn't apply.)

### C. Config knob
Add `"dedup_pnl_tolerance_pct": 2.0` to `DEFAULT_CONFIG` (so
`load_config()` picks it up and it is shallow-merged from config.yaml's
`partial_close` section, matching the existing pattern). Do NOT require it in
config.yaml; the code default governs. (Optionally add a comment line in
`config/config.yaml` under `partial_close` documenting it — but code default
is authoritative.)

### D. Historical data migration (one-off, on `data/trading.db`)
A small script (or inline Python) that, on the LIVE db, applies the SAME
logic to existing rows:
1. **Collapse dup rows:** within each `(position_id, path, ROUND(close_pct,2))`
   group, walk rows in `ts` order; when a row's `pnl_pct` is within the
   tolerance (2.0) of the row currently "open" for that key, DELETE the
   newer row (the older stays as the canonical one). When PnL jumps beyond
   tolerance, start a new open row. (ZM 191 → ~1.)
2. **Backfill NULLs:** for rows with `amount_usd IS NULL`, set
   `amount_usd` from `trades.amount_usd` (by `api_position_id`/position_id)
   or `portfolio_snapshot.amount_usd`, then recompute
   `pnl_usd_est = amount_usd * (close_pct/100) * (pnl_pct/100)`.
Run it **idempotently** and print before/after counts (total, NULLs, ZM rows).
Follow the workflow: `sqlite3 -readonly` SELECT to count/verify first, then
the UPDATE/DELETE, then verify `changes()`. Keep a `.db` backup BEFORE
mutating (e.g. `cp data/trading.db data/backups/...` or a timestamped copy).

### E. Call site (llm) — OPTIONAL, small
`llm_execution.py` currently passes `amount_usd=None`. It already reads
`portfolio_snapshot` for `unrealized_pnl_pct` in the same block — extend that
fetch to also grab `amount_usd` and pass it into `_pcp.check(...)`. This makes
future llm rows carry the real value. (The record_decision fallback in B makes
this non-mandatory, but it's the cleaner fix. Do it if trivially safe.)

## TESTS (new file `tests/unit/test_partial_close_policy.py`)
Cover with an in-memory/temp SQLite db:
- Dedup: 3 calls, same position_id/path/close_pct, pnl 5.0→5.1→5.2 (all
  within 2.0) → exactly **1 row**, `ts` advanced, original pnl kept, amount
  refreshed. Then a call at pnl 8.5 (jump >2.0 from 5.2) → **2 rows**.
- Dedup skipped when position_id/close_pct/pnl unknown → appends (row count
  grows).
- NULL fallback: amount_usd None + a matching `portfolio_snapshot` row →
  backfilled, `pnl_usd_est` populated. amount None + no snapshot + a prior
  ledger value → backfilled from ledger. Nothing available → stays None,
  no error.
- Fail-open: force the dedup SELECT to raise (e.g. malformed db handle) →
  still appends, never raises.
- Tolerance reads from config (`dedup_pnl_tolerance_pct`).
Secure/restore any module global config cache the tests touch.

## VERIFICATION (mandatory, report exact output)
1. Full suite green:
   `cd ~/.hermes/workspace/etoro_v3 && PYTHONPATH=src python3 -m pytest`
   (report the `passed` count; ALL green, not just new tests).
2. DB migration verified: print before/after (total rows, NULL count, ZM rows)
   and confirm ZM collapsed to ~1 and 0 NULLs.
3. Commit on `main` + push (Conventional Commits, body explains the WHY).
   Report the commit hash.
4. Live-verification per the workflow:
   `sqlite3 -readonly data/trading.db "SELECT key,value FROM system_state WHERE key LIKE 'LAST_RUN_%'"`
   `bash scripts/etoro_kill_switch_watchdog.sh`   # empty = healthy
   Confirm the bot is still healthy (not broken by the change).
5. DOX-pass: add a short note to `etoro_v3/AGENTS.md` (the shadow-ledger
   entry) that the ledger now dedups same-rung re-fires (sliding reference,
   `dedup_pnl_tolerance_pct`) and backfills NULL `amount_usd` — so the row
   count reflects independent decisions. Keep it to a few lines.

## CONSTRAINTS (never violate)
- Mode stays `current`; do NOT change any trading behavior — this is
  ledger/data-quality only. `current` mode still always allows.
- No live orders/closes/buys. No position closed. No `enabled:false` cron
  reactivated. No file deleted outside tmp/cache.
- Fail-open discipline preserved: a ledger bug must NEVER suppress/alter a
  partial decision.
- Use only `data/trading.db` (the single real DB).
- Weights-drift check: `git diff HEAD -- data/llm_signal_weights.json` must
  stay empty (this change must not touch weights).

## DELIVER
A final report containing: pytest pass count, migration before/after numbers
(total / NULL / ZM), commit hash + push status, watchdog/heartbeat status, and
any deviations from this design.
