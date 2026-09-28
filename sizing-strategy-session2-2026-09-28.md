# Sizing & Combo Optimization — Session 2 (Verified Corrections + Plan)
Date: 2026-09-28 · Portfolio: 10.000 USD reset 09-10, equity now 9.199.59 (−8% of reset base)
Scope: 26.07-Zäsur regime (trades created ≥ 2026-07-26). All $ = realized via `trade_pnl.realized_by_trade`.

## Session protocol
Each session reads this file + the DB, does ONE slice, appends, commits. Advisor (Opus 5.5) = second opinion only.
Advisor report: `sizing-advisor-report-2026-09-28.txt`.

---

## 1. What the advisor flagged — verified verdicts

| Advisor claim | Verdict (this session) |
|---|---|
| A: Conviction join double-counted, "HIGH inverted" not proven | **CONFIRMED bug, fixed.** Correct join (`signals.conviction`, one tier/trade): HIGH n=31 real −155.1 (WR 25.8%), MEDIUM n=435 real +234.1 (WR 36.1%). HIGH **IS** the net loser (−6.14%/notional). "HIGH is inverted" now holds. |
| B: Fees ~140 was a guess | **CONFIRMED, measured.** Total fees+slippage = **$402.1** = 0.67% of notional. By class: stock 0.548% ($315), **crypto 4.49% ($78.9)** — crypto fees eat 62% of its gross. ETF 0.65%. |
| C: Gate is LIVE not shadow, 14.748 events, 1.716 applied @×0.5 | **CONFIRMED (config `entry_quality.mode: live`, `min_size_mult: 0.25`).** Applied trades WR 33.3% (n=48) vs control 39.2% (n=125) → gate is **shrinking worse trades**, so it's net-negative on WR. |
| D/E: CORE_SWEEP +281 / crypto +93 are outliers | **CONFIRMED.** CORE_SWEEP w/o top-3 = +86.3 (CI 95% 50.5–528.4, still positive). Crypto w/o top-3 = +39.9 (CI 39.7–151.8, barely positive). |
| F: avg% ≠ real$ (confounding) | **CONFIRMED.** Use size-weighted net%/notional. |
| G: ~30% BE / ~50% winners capped | **CONFIRMED wrong.** BE-Schutz = 8% of exits (40/491). The real winners-capper = **TREND_KIPP_1H 50%-take (≥135, ≈27%) + KI TIGHTEN (≥90, ≈18%)**. |
| H: combo −147 | Trivial, confirmed. |

## 2. The REAL sizing chain (current config, equity $9199.59)

`buy = conviction_pct × equity × risk_scalar × kelly × news`, then EQ-gate (live ×0.5), ATR-parity (down only), dust-floor.

- conviction_pct: **VERY_HIGH 6.0 · HIGH 6.0 · MEDIUM 6.0 · LOW 2.0**  ← all three top tiers are 6.0 now (not 8/7/6/2 the advisor assumed)
- risk_scalar: MEDIUM 1.0, HIGH 0.85, (LOW 0.4, VERY_HIGH 1.1)
- base MEDIUM @ NORMAL: 6.0% × 9199.59 = **$552** → kelly(≈0.75 avg) → **≈$414** → ATR-parity → **≈$300** → if EQ-gated ×0.5 → **≈$150**
- HIGH: 6.0% × 0.85 × kelly ≈ **$300** (before parity/gate)
- **DEFENSIVE max_trade_pct = 3.0% hard cap ($276)** — this is the hidden ceiling in defensive regimes.
- **Conviction does almost nothing to size** (6/6/6/2): risk_scalar is the only differentiator, and it's mild (0.85 vs 1.0). This is why positions look "uniformly small".

## 3. The three structural leaks (why net-negative)

1. **HIGH conviction is the biggest net drag** (n=31, −$155, −6.14%/notional) yet is sized at 6.0%×0.85 ≈ $300. → shrink HIGH.
2. **Entry-Quality gate live @×0.5 shrinks trades that then underperform their control** (33.3% vs 39.2% WR). It's removing size from the worst entries — right instinct, but it also halves the good ones. Needs re-tuning, not removal.
3. **Exit capping**: TREND_KIPP_1H 50%-take + KI TIGHTEN lock in small wins and cut winners early. This is the biggest WR-vs-size lever (advisor P1, counterfactual not yet run).

## 4. DECISION (per user: high-risk smaller, low-risk larger up to $500)

### SHRINK (higher-risk → smaller)
- **HIGH conviction → 4.0%** (from 6.0): −$155 drag, lowest WR tier. Base becomes ~$200. *Biggest single win.*
- **Crypto asset-class → ×0.5** in sizing: 4.49% fee drag, only n=27, barely-positive after top-3. Don't scale it up.
- **EQ-gated entries**: keep gate but **raise min_size_mult 0.25 → 0.4** so it stops producing dust trades that breach the $100 floor.

### GROW (lower-risk → larger, up to $500)
- **MEDIUM conviction → 8.0%** (from 6.0): the +$234, 36.1% WR engine. Base becomes 8.0%×9199.59 ≈ **$736** → after kelly/parity ≈ **$500**. This is the $500 target, and it's the proven-positive tier.
- **VERY_HIGH → 8.0%** (from 6.0): restore differentiation (currently = MEDIUM). Only re-arm full size once it shows n≥10 live (post-Zäsur it's been suppressed).
- **CORE_SWEEP**: keep, don't shrink (still +$86 w/o top-3). Optional: let it bypass EQ-gate floor.

### NET effect
- MEDIUM trades grow ~$300→$500 (the profit engine gets real size).
- HIGH trades shrink ~$300→$200 (the drag gets smaller).
- Net risk per trade roughly flat, but **risk-weighted toward the +$234 tier, away from the −$155 tier.**

## 5. Signal combos

- **WEIGHT DOWN ×0.5: `TREND_PULLBACK + GOLDEN_CROSS`** — n=71, −$153, the only combo with volume AND consistent negative. (Confirm survives P0.3 net% re-cut.)
- **LEAVE: `RSI_EXTREME_OVERSOLD + MACD_TURN_BELOW_SMA20`** — n=8, too small to judge.
- **NEW combos: do NOT go live.** In-sample mining. If pursuing, run as **shadow-log with pre-registered hypothesis, n≥100 out-of-sample, fees included.** Candidate: `RSI_EXTREME_OVERSOLD + BB_LOW_MACD_IMPROVING + TREND_PULLBACK`. Verify component names exist in `signals.py` first.

## 6. Exit mechanics (the real WR lever — advisor P1)
Counterfactual still to run: for each TREND_KIPP_1H-50%-take + KI TIGHTEN exit, reconstruct price +1/+3/+5d from bars; measure % where "holding" beat the exit. If ≥100 exits show a clear sign, raise the take-profit threshold / delay KI TIGHTEN. This is where "net negative → net positive" most likely lives, more than sizing alone.

## 7. Guardrails (advisor, adopted)
- **One money-affecting change per 50 closed trades** (deploy discipline) — else every pre/post is unattributable.
- **No size-up on any class until P0.4 (outlier CI) shows positive without top-3.**
- **Floor rule**: 116+30 trades sit in $50–75, under the own $100 Bible floor → check why dust_floor/signal_floor = 75, not 100.

## 8. OPEN (next session)
- [ ] Exit counterfactual (P1) — the WR lever.
- [ ] Re-cut all combo tables with size-weighted net%/notional (P0.3).
- [ ] Verify EQ-gate per signal_type gated-vs-control (blocked by a NOT-IN subquery; redo with plain JOIN).
- [ ] Confirm `very_high_pct` re-arm needs n≥10 gate before going live.
- [ ] Decision: does MEDIUM→8.0% respect the 75% exposure cap & consolidation rule (Kap.10: consolidate before BUY > MAX)?

## 9. Files
- This report: `etoro_v3/sizing-strategy-session2-2026-09-28.md`
- Advisor report: `etoro_v3/sizing-advisor-report-2026-09-28.txt`
- Config: `etoro_v3/config/config.yaml` (conviction %, entry_quality, max_trade_pct)
- Sizing chain: `etoro_v3/src/bot/workers/signal_worker.py` (~char 107000)
- DB: `etoro_v3/data/trading.db` (read-only)
