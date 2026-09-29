# HANDOFF 2026-09-29 — MA200 Shadow/Ledger Live Wiring (COMPLETE, awaiting advisor sign-off)

## Goal
Wire the **frozen MA200 entry filter** (feat/ma200-filter, advisor-frozen 2026-09-29)
into the LIVE entry path in **SHADOW + LEDGER** mode only — NO live sizing change.
The gate records every would-be block to `entry_quality_events` (with entry price)
so it can be evaluated after enough trades. Live execution must stay byte-identical.

## Status: COMPLETE + ADVISOR SIGNED OFF (2026-09-29, exit=0). GATE IS LIVE IN SHADOW MODE (events already accumulating in trading.db).
Milestone commit done: `feat/ma200-filter: MA200 shadow gate + ledger, ma200_daily seed + live wiring`.
NEXT: START NEW SESSION (VoLLi: context-overflow prevention). First action of next session:
`read_file HANDOFF-2026-09-29-ma200-shadow-wiring.md` — flip criterion, split-guard backlog, and
evaluation protocol are in it. No further code work until ≥50 live + ≥50 shadow-blocked trades.

## Frozen rule (must stay exact)
- Gate #7 `ma200_trend`: block if `close[D-1] < SMA200(close[D-200..D-1])`.
- Decision INTRADAY on signal day D using ONLY closed bars (last closed = D-1).
- `lag: 0` (config + code default) = decide on last provided close → live-faithful S12b_L2.
- SHADOW: shadow hit = GateHit(size_mult 0.0) → `ev.blocked=True` (ledger marker)
  but `live_effective_mult == 1.0` (execution UNTOUCHED — the core invariant).
- LIVE mode (later flip): soft `size_mult 0.25` (no hard block, anti-brake).

## Files (ALL compile clean, verified 2026-09-29)
- `src/bot/core/entry_quality.py` ✅
  - `ma200_decision(closes, window=200, lag=1-default) -> (blocked, detail)` pure, fail-open.
  - `DEFAULT_CONFIG.gates.ma200_trend = {enabled, mode shadow, window 200, lag 0, size_mult 0.25}`.
  - `evaluate(..., daily_closes=None)` Gate #7 (lines 402-435).
  - `EntryQualityEval.live_effective_mult` — excludes shadow ma200 hit.
  - `record(entry_price=...)`; `ensure_table` includes `entry_price` + idempotent ALTER TABLE
    (live DB already had the table w/o the column — migration verified needed).
  - `record()` stores `size_mult = ev.live_effective_mult` (execution-effective); the
    would-block 0.0 stays in the `hits` JSON; `blocked=1` = shadow would-block marker.
- `src/bot/core/ma200_history.py` ✅ NEW
  - `ma200_daily(symbol, date, close)` PK(symbol,date) + index.
  - `ensure_table`, `seed_from_csvs(db, dir=data/exit_replay_bars)` (idempotent),
    `get_daily_closes(db, symbol, limit)` oldest→newest, `upsert_closes` (one executemany + 1 commit),
    `df_to_closed_pairs(df, today_str)` drops today's in-progress bar.
- `src/bot/workers/data_worker.py` ✅
  - In `run()` (~line 665): guarded one-time seed — `ma200_history.ensure_table(db)` +
    `seed_from_csvs(db)` only if `COUNT(*) < 1000` (idempotent, no 284-CSV re-scan per 5-min run).
  - In scan loop: `ma200_history.upsert_closes(db, yf_sym, ma200_history.df_to_closed_pairs(df))`
    then `daily_closes=ma200_history.get_daily_closes(db, yf_sym, limit=201)` into `evaluate()`,
    `entry_price=result.price`, `record(mode= overall entry_quality.mode)` — all fail-open.

## Verification (2026-09-29, all green)
- `scripts/_verify_ma200_shadow.py` (end-to-end, temp DB): 282 symbols / 72,527 closed closes
  seeded; spot-checks match manual SMA200; decision bar = last provided close (no look-ahead);
  SHADOW invariant `live_effective_mult == 1.0` both would-block AND pass; LIVE mode 0.25 soft-gate;
  fail-open (50 closes / None); upsert idempotent.
- `tests/unit/test_ma200_shadow.py` — 14 new unit tests, ALL PASS (decision tuple API,
  shadow invariant incl. `record()` row: size_mult=1.0/blocked=1/entry_price + hits JSON,
  live mode 0.25, fail-open, disabled, ma200_history idempotency/ordering, df_to_closed_pairs).
- `tests/unit/` full: **1580 passed**, 9 FAILED = PRE-EXISTING matplotlib-PNG tests
  (matplotlib not installed in env; unrelated to this change — they fail on clean tree too).
- LIVE DB seeded: `data/trading.db` → 301 symbols / 73,800 rows (after today-bar purge),
  282 symbols ≥201 closes. AAPL last closed bar 2026-09-28 (D-1). ✅

## Look-Ahead Fix (found during final verification, 2026-09-29)
- Seed CSVs contained 236 same-day (2026-09-29) in-progress bars → would have
  violated "closed bars only". `seed_from_csvs` now drops rows dated >= today
  (same rule as `df_to_closed_pairs`). Live DB purged: 74,036 → 73,800 rows;
  every series now ends at D-1 (AAPL/CATE.ST last = 2026-09-28). Re-verified:
  CATE.ST block decision now on 348.20 (2026-09-28 close) vs SMA200 426.57.
- NOTE: live DB rows dated 2026-09-29 from the LIVE upsert path are CORRECT
  (they are the real D-1 bar on 09-30); only same-day rows relative to fetch
  time must be excluded — the guard is time-relative, not a fixed date.

## Advisor sign-off (2026-09-29, /tmp/advisor_ma200_wiring_reply.txt, exit=0): WIRING CORRECT, LAUFEN LASSEN.
Advisor verified independently: shadow-hit → 1.0 through `latest_size_mult()` → signal_worker
buy-amount untouched; no reader of the `blocked` column outside entry_quality.py; config.yaml
does NOT override `ma200_trend` (default shadow applies); entry_price migration applied in prod.
Notes carried forward:
- SPLITS are the real risk (not dividends): a yfinance split back-adjusts the whole series;
  `INSERT OR IGNORE` never corrects stored rows → silent SMA200 discontinuity, distorts the
  ledger the flip decision rests on. Suggested guard: on upsert, compare incoming close vs
  stored close for same date; >5% deviation → re-seed that symbol. (No fix now; shadow-only.)
- `signal_ttl_minutes: 60` → no day-crossing drift; day-bootstrap fine.
- Keep the FROZEN flip criterion ≥50 live + ≥50 shadow-blocked, metric = $/TRADE (not WR —
  VoLLi's 30er/WR idea contradicts the frozen evidence basis, −364.6 $/trade CIs); add a time
  window only as "regime independence" (ratchet drifts weights monthly).
- Known (unreachable) nit: `live_effective_mult` floors at 0.0 vs normal-path clamp at
  `min_size_mult` (0.25). Unreachable with current gates (all ≥0.25); would matter only if a
  gate <0.25 is added while a shadow hit co-occurs.
- advisor.sh nvm bug fixed 2026-09-29 (skip nvm if node on PATH, else `nvm use --delete-prefix
  --silent`; stale `prefix=/home/mvolli/.npm-global` in ~/.npmrc previously broke it → exit=1).

## What live behavior changes (intentional, minimal)
- Nothing at execution: EQ overall mode stays `live` (unchanged), `ma200_trend.mode: shadow`.
- Ledger will now accumulate would-block rows with entry_price (previously: gate couldn't
  evaluate at all — 3mo df < 201 closes → always insufficient).
- Startup: one extra one-time DB seed (~1-2 s) per data_worker run when table < 1000 rows.
- Per scan cycle: +1 SELECT (get_daily_closes) + upsert per candidate symbol.

## Next session (after advisor sign-off)
1. Commit (above) + push if configured.
2. Evaluation protocol (frozen): flip `ma200_trend.mode: live` (soft 0.25) only when
   ≥50 closed live trades AND ≥50 closed would-block shadow trades exist in the ledger AND
   passed−blocked $/trade > 0 (day-bootstrap CI). Query:
   `SELECT ... FROM entry_quality_events WHERE hits LIKE '%ma200_trend%'` + join trades on signal_id/entry_price.
3. Watch: shadow-block rate (expected ≈54% of trades per replay: 274/506), any gate misfire,
   `ma200_daily` growth (should add ~1 row/symbol/day).

## Invariants (do NOT break)
- SHADOW mode MUST NOT change any live size (`live_effective_mult` the only execution mult).
- Fail-open on missing/short history. No look-ahead (closed bars only).
- `ohlcv_daily` untouched (separate table). `ma200_daily` = dedicated history.
- Replay bars: 284 CSVs × 246 rows, 2025-09-29→2026-09-29, keyed by yfinance symbol.

## Live context (do not change without advisor)
- $9,172 equity, 1:20 copy-traded, LIVE. Small & cautious.
- Evidenz (frozen, see entry_quality.py header 152-178): realized holdout blocked −1.39 $/tr
  vs passed −0.80 $/tr; day-bootstrap CI [+0.51, +1.76]; filter is purely reducing (adds no trades).

## Advisor note
- `advisor.sh` nvm bug fixed 2026-09-29: skips nvm if node on PATH; else
  `nvm use --delete-prefix --silent` (stale `prefix=/home/mvolli/.npm-global` in ~/.npmrc
  previously broke it → exit=1). Verified: nvm use rc=0, node v24.14.0, claude resolves.

## Key identifiers
- entry_quality: `~/.hermes/workspace/etoro_v3/src/bot/core/entry_quality.py` (gate #7 lines 402-435, ma200_decision 180-202)
- data_worker:   `~/.hermes/workspace/etoro_v3/src/bot/workers/data_worker.py` (seed in run() ~665, wiring in scan loop ~958)
- ma200_history: `~/.hermes/workspace/etoro_v3/src/bot/core/ma200_history.py`
- verify script: `~/.hermes/workspace/etoro_v3/scripts/_verify_ma200_shadow.py`
- unit tests:    `~/.hermes/workspace/etoro_v3/tests/unit/test_ma200_shadow.py`
- replay seed:   `~/.hermes/workspace/etoro_v3/data/exit_replay_bars/*.csv`
- advisor Q:     `~/.hermes/workspace/etoro_v3/advisor-q-ma200-wiring-checkpoint.txt`
- DB:            `~/.hermes/workspace/etoro_v3/data/trading.db`
