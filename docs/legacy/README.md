# Legacy Reference Docs (from the pre-V3 `etoro` project)

The old `/home/mvolli/.hermes/workspace/etoro` directory (monolithic
pipeline, superseded by this repo) has been archived. Before archiving it,
these documents were pulled forward because they contain design rationale
and API contracts that aren't written down anywhere else in this repo.

## What's here

- **`TradingV3.md`** — the audit (Gemini 2.5 Pro + live system data,
  2026-06-24) that diagnosed the old monolithic pipeline's failures
  (timeouts, a `'WARNING'` vs `'WARN'` DB constraint crash, regime-gate
  bypasses, unenforced sector limits) and proposed the worker-based
  redesign. This is the direct ancestor of this repo's architecture.
- **`TradingV3_Architecture.md`** — the actual blueprint this repo (`etoro_v3`)
  was built from: the `data_worker` / `risk_worker` / `reconciler` /
  `signal_worker` / `execution_worker` / `monitor_worker` split, the
  staggered cron schedule, the trade state machine
  (`PENDING_APPROVAL → APPROVED → SUBMITTING → ACTIVE → CLOSING → CLOSED`),
  and the DB schema this repo's `schema.sql` descends from.
- **`trading_bible_constants_v5.py.reference`** — the canonical "Trading
  Bible v5.0–5.3" constants from the old project. Comments throughout this
  repo (`sell_exits.py`, `regime.py`, `kill_switch.py`, `correlation.py`,
  `trailing_stop.py`, etc.) reference "Trading Bible V5" without ever
  defining it in this repo — this file is that definition. **Not imported
  by anything; reference only.** See "Known divergences" below.
- **`bible_v4_gemini.md`** — the incident postmortem (2026-06-11, Gemini
  2.5 Flash) that drove the v4→v5 rule changes: SL breaches on NVDA/META,
  circuit-breaker buys that shouldn't have fired, positions that grew to
  400–841% of their concentration limit before manual intervention. Explains
  *why* several of this repo's guards exist (e.g. regime-gated buys,
  pre-trade concentration checks).
- **`api-execution-reference.md`** / **`close-api-reference.md`** — verified
  eToro API contracts: exact request bodies for open/close orders, the
  "always flat body, never nested" and "use InstrumentID, never Symbol"
  rules, and the close-order cache-refresh delay (~10-15s before a
  close/partial-close is reflected in the portfolio). This repo's
  `trailing_stop.py` independently rediscovered the cache-delay behavior
  (see `_verify_partial_close`'s polling/backoff) — this doc would have
  saved that rediscovery.
- **`superseded-v2-architecture-v7.md`** — the OLD monolithic pipeline's
  architecture doc (v7.0, 2026-06-22). Superseded by `TradingV3_Architecture.md`.
  Kept for historical contrast only — don't treat anything in it as current.

## Known divergences from `trading_bible_constants_v5.py.reference`

Spot-checked against this repo's `config/config.yaml` and
`src/bot/core/*.py` on 2026-07-03. Most core numbers carried over 1:1
(MAX_OPEN_POSITIONS=21, cash targets 15/30/10%, SL 3%/4%/2%). Three did not:

1. **`BREAK_EVEN_TRIGGER_PCT`**: Bible = 5.0%, this repo = **3.0%**
   (lowered 2026-07-03 — intentional, see git log on `trailing_stop.py`).
2. **`PROFIT_TAKE_LEVELS`**: Bible had 7 graduated stages (+10/20/30/40/50/75/100%,
   15–50% close-per-stage). This repo simplified to 3 stages (+15/25/50%,
   20/20/30% close), now ATR-adaptive per instrument instead of fixed
   (`trailing_stop.py`, 2026-07-03). Intentional simplification, not a
   regression — but worth knowing the original had finer granularity if the
   3-stage ladder ever looks too coarse.
3. **`MDD_DAILY_PCT` / `MDD_WEEKLY_PCT` / `MDD_MONTHLY_PCT`** (2%/5%/10%
   max drawdown, Bible v5.3): ~~gap~~ **RESOLVED 2026-07-04** — this repo now
   implements all three horizons (`config.yaml: risk.daily/weekly/monthly_
   loss_limit_pct` = 5/8/12%, looser than the Bible's figures by deliberate
   user decision). Weekly/monthly measure max drawdown from the 7d/30d
   equity high (`risk.check_trailing_loss_breach`); breaches trip the
   kill-switch. Daily trips auto-clear on the next UTC day
   (`kill_switch.auto_clear_if_new_day`, 2026-07-05); weekly/monthly stay
   until manual review.

Everything else in the old project (incident reports, optimization-plan
docs, historical `.db` files, `post_*.py` scripts) is archived at
`~/.hermes/archive/etoro_legacy_2026-07-03/` rather than copied here — it's
point-in-time debugging material for a pipeline that no longer exists, not
ongoing reference.
