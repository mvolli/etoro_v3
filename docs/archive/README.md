# docs/archive/

Retired one-shot artifacts, kept as **Vorlagen** (reusable templates), not
live code. Nothing here is wired to a cron or referenced from `src/`/`tests/`.

## leftover-close batch (task `leftover-cleanup-2026-08-28`)

One-shot, market-timed cleanup of 15 residual eToro positions — one close per
position, timed to that position's own market session (ASX/TOKYO/HK/EU/US).
**Status: complete.** All 15 positions are `CLOSED` + `VERIFIED` in
`trades`, and the close events are in `trade_events` (final CLOSE rows
`source=reconciler_9d`, `pnl_source=api_history`). The reconciler's recovery
path verified them independently from API history — no manual ledger entry was
needed.

Files (archived 2026-09-11; originally untracked one-shots in `scripts/`):
- `close_leftover.py` — core: close one position by id, verify, record to state.
- `close_leftover_slot.py` — close the next pending position in a given market slot.
- `record_asx_close.py` — one-off ASX close recorder.
- `leftover_close_state.json` — the 15 positions + outcomes (audit trail).

The scripts co-import each other (`import close_leftover as cl`), so this
directory stays internally runnable if reused. The matching per-market wrappers
(`close_slot_{TOKYO,HK,EU,US}.sh`) were retired alongside and live in
`~/.hermes/scripts/archive/`.

Pattern reference (the "how", reusable for a future batch):
`~/.hermes/skills/finance/etoro-manual-cleanup/references/market-timed-batch-close.md`.
