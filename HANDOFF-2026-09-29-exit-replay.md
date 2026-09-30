# HANDOFF: eToro V3 Exit-Layer Optimization (Session 4 → Session 5)
Written: 2026-09-29 · Repo: `/home/mvolli/.hermes/workspace/etoro_v3` (branch `main`, 2 commits ahead of origin — push when green)

## ✅ UPDATED 2026-09-30: Replay DONE + MA200 gate LIVE (shadow) — new session reads this first
- `scripts/_shadow_exit_replay.py` **BUILT + RUN** (committed `61566c8`, in sync with origin).
  Results: `data/exit_replay_results.json` (16 scenarios × full/train/holdout/monthly, 511 closed trades,
  284 symbols × 246 bars backfilled via `scripts/_fetch_replay_bars.py`).
- **Replay verdict**:
  - Realized: −$513 / −$1.00/tr (holdout −$363 / −$1.12). S0 baseline replay: −$163 / −$0.32.
  - Standalone levers (wide stop, chandelier, relax-BE, time-stop) all FAIL on holdout (CIs < 0).
  - ATR-sizing S6 is a MESS (−$4,472 holdout, CI [−9077, +309]) — do NOT adopt.
  - Only OOS-robust lever = **MA200 filter** (blocks ~56% of trades):
    S12 (MA200+base): full +$22 / +$0.10/tr, holdout −$12 / −$0.09 (≈breakeven, CI [−317,+193]),
    WR 44–43%, vs realized −$1.00/tr. Monthly: Jul +$63, Aug −$19, Sep −$22 (not single-month).
  - Full-stack S8 (+reversion+ATR-size+cap10): full +$1,516 / +$6.74 but holdout +$326 with
    CI [−964,+1541] — NOT significant; overfit risk. Keep S12 as the minimal robust change.
- **MA200 shadow gate LIVE since 2026-09-29** (config default `ma200_trend.mode: shadow`;
  no override in config.yaml). Shadow invariant HOLDS: pure-ma200 rows carry size_mult 1.0;
  co-firing rows (volume_confirm 0.5×) are legitimate gate stacking.
- **Flip-criterion progress (≥50 closed passed + ≥50 closed would-block, $/trade metric)**:
  26 would-block ledger rows so far (1 linked trade still ACTIVE: 06881.HK; rest REJECTED by
  other gates or never executed). Progress is SLOW for structural reasons (see below).
- **✅ COVERAGE GAP CLOSED 2026-09-30** (was: only 16/76 evaluated symbols = 21% had ≥201
  closes; uncovered symbols FAIL-OPEN; block rate 5.5% overall vs 32.3% covered):
  `scripts/_backfill_ma200_crypto.py` seeded 2y yfinance daily history for the evaluated
  universe (watchlist ∪ 90d-closed trades = 772 symbols, skipping the 282 the equity seed
  already had). Result: **490 fetched → 466 OK, 7 short, 17 no-data (delisted/gold/silver
  ETFs with no yfinance mapping — stay fail-open, acceptable). `ma200_daily` = 335,167 rows,
  748 symbols ≥201 closes → coverage 748/772 (96.9%)**. Idempotent (INSERT OR IGNORE),
  closed bars only, fail-open per symbol. KNOWN STALENESS: yfinance back-adjusts the whole
  series — a post-seed split makes stored history stale (advisor-noted, live-appended rows
  correct from then on).
  → NEXT: let the shadow gate accumulate on the now-covered universe. Do NOT flip before
    ≥50 closed passed + ≥50 closed would-block AND $/trade beats realized baseline.
- AGENTS.md truncation fixed via `hermes config set context_file_max_chars 50000` (was 32000
  pin; file 35,806 chars; verified no truncation). Compression drafts (`AGENTS.md.compact`,
  `agentsmd_manifest.json`, `/tmp/agents_v3.md`) proved LOSSY (108 code spans missing) →
  discarded & cleaned; pin-increase is the durable fix, lossless-compress Local Contracts
  bullets if the file passes ~48k.
- Untracked leftovers (not mine to commit): `advisor-q-*.txt` (3 advisor reports),
  `data/backup/`, `data/backtest_cache/`, `data/backtest_symfiles/`,
  `data/.watchdog_llm_last_alert` — plus tracked-but-live `data/llm_*.json` churn from the
  running bot (commit only deliberately, per Self-Improvement-Lauf convention).

## Mission (user-verbatim)
"Arbeite autonom weiter an Optimierungen. Der advisor ist dein Freund. Führe auch tiefgreifende Recherchen online durch was noch improvement-fähig ist."
Follow-up: "Autonom weiter mit den Optimierungen. Neue Session starten bevor der Kontext über läuft" → this handoff.

## State: what is DONE and VERIFIED (do not re-derive)
All numbers below were measured against `data/trading.db` (read-only!) this session:

1. **Cost model CORRECTED (final)**: `amount_usd` overstates deployed capital ~5.5×; true deployment ≈ **$90/trade** (base = `pnl_usd/pnl_pct`). Real round-trip cost ≈ **0.18%** of deployed (spread; OPEN leg books 0.5×spread, close leg NOT booked). The earlier "80% fee-driven / 0.66% round-trip" claim was a MATH ERROR and is dead.
2. **GROSS PnL ≈ −$476** across 508/509 CLOSED trades since 2026-07-26 (26.07-Zaesur!): genuine negative edge, NOT fee-driven.
3. **Beta/alpha decomposition** (684 records, entry=next open, +10d fwd, excess vs equal-weight market, moving-block bootstrap CIs):
   - dipbuy: **−1.11% CI [−1.96, +0.13]** → NO selection alpha
   - trend: **−5.45% CI [−7.28, −4.69]** → significantly worse than market (keep de-weighted)
   - sweep: **−4.73% CI [−6.70, −4.11]** → significantly worse than market (keep de-weighted)
   - Monthly folds: dipbuy Aug −0.70% (wide CI), Sep −1.69% CI [−2.36,−1.16]
   - **Market in down-regimes REBOUNDS**: CAUTION +7.69%, DEFENSIVE +4.52% fwd-10d → edge exists IF held ~10d
4. **Exit layer is the PRIMARY loss driver** (the diagnosis):
   - HARD_SL: **251 trades → −$608** (#1 loss bucket, −$2.42/trade); KI exits: 106 → −$157; BE +$38, profit/SELL +$110
   - **57% of positions (275 trades) carry fixed 3.0–3.5% hard stop**, realized median −0.82%
   - Median hold **4.1 days**; edge needs ~10 days
   - **78% of stopped-out symbols have ATR% ≥ 3.0** → 3% stop ≈ 1 day of noise
   - **Recovery proof**: 155 stopped trades → 81% recovered >0 in next 10d; mean +1.07%, median +1.69%; holding ≈ **+1.03pp/trade** vs stopping
5. **Online research DONE**: `research-online-improvements-2026-09-29.md` (committed `fc41d5d`) — 25+ live sources via Playwright headless Chromium (Hermes web tools + ddgs are DOWN/bot-walled; Playwright is the working channel, see §Tooling). Findings confirm: wide "black swan" ATR stop, Chandelier trailing, reversion-based primary exit, 200-day-MA per-symbol filter (QS backtest: 76% WR / 0.52%/trade), ATR-based sizing, regime = scale size NOT zero entries.
6. **Advisor rounds 1+2 consumed**: `advisor-report-entry-quality-2026-09-29.txt` + `-r2.txt` (in repo, untracked — commit or leave; content consumed). Round 2 demanded the beta/alpha experiment (done). Exit-code-1 on the advisor process = npm/nvm warning AFTER report write; report complete.

## Committed artifacts (git)
- `evidence-entry-quality-2026-09-29-FINAL.md` — 93-line FINAL corrected diagnosis
- `research-online-improvements-2026-09-29.md` — online research, 12 cited sources
- `scripts/_alpha_beta.py` — the beta/alpha decomposition (stable; output `/tmp/alpha_beta_out.txt` may be gone — rerun if needed)
- `scripts/_shadow_round2.py`, `_shadow_forward_test.py`, `_entry_quality_deep.py` — earlier analysis scaffolds

## NEXT STEP (the single best experiment) — build `scripts/_shadow_exit_replay.py`
Replay all 508 closed trades with the NEW exit stack and compare net PnL vs current **−$508** (or GROSS −$476), per-scenario, day-level CIs. Spec (from research file, ranked):

| Lever | Current | Proposed (scenario) |
|---|---|---|
| Hard stop | 3.0–3.5% fixed | **2.5×ATR and 3×ATR** wide catastrophe stop, ratchets up only (Chandelier: highest_high − k×ATR) |
| Primary exit | tight price stop first | mean-reversion exit (RSI/Bollinger reverts → exit next open); price stop = backstop |
| Hold | median 4.1d | let it run to reversion; time-stop ~15–20d |
| Break-even | aggressive | removed / gated at entry+1 ATR |
| Entry filter | none | per-symbol 200-day-MA: skip if symbol < own 200d MA; ALLOW broad-market CAUTION/DEFENSIVE rebounds |
| Sizing | ~$90 fixed | ATR-based: size = risk$/stop_width (keep risk/trade flat) |

Run as ablation (each lever on/off) + full-stack. Judge OOS: chronological 60/40 holdout (Sep 2026 = holdout) + monthly folds. **Do NOT touch `config.yaml` until the full stack is OOS-positive** (Trading-Bible shadow-first rule; live = real money). Then: implement in `src/bot/core/` exit path + `config.yaml`, per safe-change workflow (pure function → tests → full suite → commit/push → live verify).

Risks to check in the replay: wider stops ⇒ bigger single-trade drawdowns (ATR sizing must compensate); reversion exit can hold through a real breakdown (2.5–3×ATR stop is the backstop); the CAUTION/DEFENSIVE rebound window is recent (Aug–Sep 2026) → CI must not be one-month-concentrated.

## Tooling that WORKS / FAILS right now (measured)
- ✅ `sqlite3`/`execute_code` on `data/trading.db` (read-only URI mode)
- ✅ **Playwright headless Chromium** (node): `export NODE_PATH=/home/mvolli/.hermes/hermes-agent/node_modules; node <script>` — cached browsers in `~/.cache/ms-playwright/`. Works for web content + search; Bing geo-junk (German results) and rate-limits — prefer direct URL extraction of known domains over search.
- ✅ `ddgs` CLI: `ddgs text -b lite -r us-en -m 8 -k "query"` — flaky, often empty; use with retries as secondary
- ✅ `curl` (with UA) for known URLs; ❌ Hermes `web_search`/`web_extract` (Nous gateway unreachable); ❌ `browser_exec`/CDP (down); ❌ DDG html endpoint (captcha)
- Advisor: `~/.hermes/scripts/advisor.sh "Frage" [files…] > report.txt 2>&1` — run `background:true`, takes 1–5 min, 5h shared Opus quota
- `ohlcv_daily` is the price source for replays (3-month coverage, 73-day Jul/Aug gap pre-2026-09-12 → check gaps); 28k rows, 403 instruments

## Hard rules for the new session (from AGENTS.md / memory — read the two AGENTS.md files fresh)
- DB: ONLY `etoro_v3/data/trading.db`. Join: `signals/ohlcv_daily.instrument_id` (INTEGER) → `instruments.instrument_id` → `instruments.symbol` (eToro ns) / `yfinance_symbol` (Yahoo ns). Never mix namespaces.
- 26.07-Zaesur: trade analyses filter `trades.created_at >= '2026-07-26'` or split phases explicitly.
- Shadow-before-live; OOS/holdout-based evidence; BIBLE_HARD_LIMITS in code are inviolable; weights-drift check (`git diff HEAD -- data/llm_signal_weights.json`) is mandatory on diagnostics.
- Copy-trading 1:20 → account must stay small; risk/trade must stay flat when stops widen.
- Regime now DEFENSIVE (since 2026-09-28); equity ~$9,172. Bot runs live during any change.
- Discord: answer "Du" + German, data-rich, no thinking blocks, thread channel `1554066398819651625`.

## Cleanup done
- Stale `claude` CLI (PID 1328, idle 14d on pts/2) killed; no other advisor/research/node procs running; no live delegate subagents.
