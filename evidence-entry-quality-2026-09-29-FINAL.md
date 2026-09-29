# Entry-Quality Diagnosis — FINAL (advisor r2-corroborated)

Date: 2026-09-29. This supersedes earlier "80% fee-driven" and "0.66% round-trip cost" claims,
which were math errors. Advisor round-2 (Opus-5.5) validated the corrected method.

## 1. What was WRONG before (corrected)
- Shadow tests had **wrong polarity**: sell-signals were computed as long forward-returns.
- **"80% fee-driven" was a math error.** Recorded `cost_usd` = 0.5 × spread% × amount booked on
  OPEN only (close leg not booked). Round-trip spread ≈ `spread_pct% × amount`.
- Real deployed capital ≈ **$90/trade** (realized base = pnl_usd / pnl_pct), NOT $500.
  `amount_usd` overstates deployed capital ~5.5×.
- Real round-trip cost ≈ **0.18%** of deployed (median spread 0.09% one-way). NOT 0.66%.
- **GROSS PnL ≈ −$476** across 227 costable trades → a *genuine negative edge*, not fee-driven.

## 2. Advisor r2 decisive experiment — Beta vs Alpha (scripts/_alpha_beta.py)
Buy-signal forward-10d returns (entry=next open) minus equal-weight market, moving-block
bootstrap, monthly folds, 684 deduped records.

| family  | days | excess | CI95            | reading                         |
|---------|------|--------|-----------------|---------------------------------|
| dipbuy  | 32   | −1.11% | [−1.96, +0.13]  | NO selection alpha (CI ∋ 0)     |
| trend   | 25   | −5.45% | [−7.28, −4.69]  | SIGNIFICANTLY WORSE than market |
| sweep   | 14   | −4.73% | [−6.70, −4.11]  | SIGNIFICANTLY WORSE than market |

Monthly: dipbuy Aug excess −0.70% (CI wide), Sep excess −1.69% (CI [−2.36,−1.16]).

**Regime → market 10d forward:**
- CAUTION market fwd10 = **+7.69%** (days=21)
- DEFENSIVE market fwd10 = **+4.52%** (days=13)
- NORMAL (days=4, too few)

## 3. The paradox and its resolution (the real loss driver)
- dipbuy excess ≈ 0 over 10d ⇒ it *tracks* the market, which **rebounds +7.7%/+4.5%** in down
  regimes. So a dip-buy held to the 10d horizon should be roughly market-positive.
- But realized PnL is **negative** ⇒ the loss comes from **EXIT timing, not entry selection**.

### Exit-layer decomposition (trade_events, 508 closed)
| exit reason              | trades | net PnL      |
|--------------------------|--------|--------------|
| HARD-SL                  | 251    | **−$608**    |
| KI EXIT                  | 106    | −$157        |
| Break-even               | ~52    | +$38         |
| Profit / SELL / others   | rest   | +$110 + …    |

- **57% of positions carry a fixed 3.0–3.5% hard stop** (275 trades, realized median −0.82%).
- Median hold = **4.1 days** — half the 10d window where the signal excess≈0 and market rebounds.

### Proof the stop is the killer (recovery test)
Stopped-out (HARD/EMERGENCY SL) trades, forward-10d from exit price, n=155 (of 185):
- fwd-10d-from-stop: mean **+1.07%**, median **+1.69%**
- **81% recovered > 0**, 19% fell further
- day-agg mean −0.54% CI95 [−4.80, +3.72] (∋ 0)
- counterfactual (held instead of stopped at −3%): mean **−1.97%** vs realized ≈ −3.0%
  ⇒ **holding adds +1.03 pp per stopped trade on average**

### Why the stop is structurally too tight
ATR% of the 179 HARD-SL-stopped symbols:
- median **3.67%**, mean 3.57%
- **78% have ATR% ≥ 3.0** ⇒ a 3.0–3.5% fixed stop ≈ **one day of noise**
- 96% have ATR% ≥ 2.0 ⇒ stop ≈ 1.5 days of noise

A fixed 3% stop on a stock that moves 3.7% daily (1-sigma) will be shaken out by ~normal
intraday/volatility noise almost immediately, capturing the dip-down leg and missing the
rebound. This is a **negative-symmetric exit**: on zero-alpha dip-buys it locks in the
loss and forfeits the mean-reversion payoff.

## 4. Verdict
- **No gross selection alpha** in the core dip-buy signal (excess ≈ 0, tracks market).
- **Trend and sweep signals have NEGATIVE alpha** (significantly worse than the market) —
  keep the de-weighting (already done in sizing-decision commits).
- **The market rebounds in down regimes** (CAUTION +7.7%, DEFENSIVE +4.5%) — dip-buying is
  the right *direction*, but the bot never holds long enough to collect it.
- **Primary loss driver = the exit layer** (fixed 3% stop + break-even exits + median 4.1d
  hold), NOT fees and NOT entry selection per se.

## 5. Fix candidates (ranked by expected impact, all shadow-testable)
1. **ATR-scaled / widened stop** — replace fixed 3.0–3.5% with ≥1.5×ATR (or 2×ATR) or a
   trailing stop. Directly addresses the 78% "stop = 1 day of noise" problem. Highest leverage.
2. **Extend hold horizon / relax break-even exit** — let dip-buys run toward the 10d rebound;
   remove the aggressive break-even stop that books +$38 but truncates recovery.
3. **Regime-conditional sizing** — dip-buy value concentrates in CAUTION/DEFENSIVE (where
   the +7.7%/+4.5% rebound lives). Size up there, down in NORMAL.
4. **Hard-de-weight trend/sweep** (already de-weighted) — their negative alpha confirms it.
5. **Cost-awareness is NOT the master gate** (advisor) — real round-trip cost ≈ 0.18%; the
   edge estimate is the missing piece, and the exit fix provides it.

## 6. What to do next (shadow-first, no live change yet)
- Build a shadow re-run of the exit layer: replay the 508 closed trades with
  (a) ATR-scaled stop, (b) 2× hold horizon, and measure net PnL vs current.
- Gate candidate = ATR-scaled stop; measure in shadow before touching config.yaml.
- Deep online research on exit-design (trailing vs fixed, optimal holding period for
  mean-reversion, regime filtering) running in parallel → research-online-improvements file.
