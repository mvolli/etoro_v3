# Online Research — Improving the Dip-Buy Mean-Reversion Bot (Exit-Layer Focus)
Date: 2026-09-29 · Method: headless-Chromium extraction of 30+ live sources (Investopedia, Wikipedia,
QuantifiedStrategies, arXiv). Search engines (Bing/DDG) were geo-broken/rate-limited, so discovery was
curated on canonical domains and content was extracted directly. All claims below are tied to the
**measured** state of the system (see `evidence-entry-quality-2026-09-29-FINAL.md`).

---

## THE SYSTEM (measured ground truth)
- Small eToro account, ~$9,172 equity, copy-traded 1:20 → must stay small. Median deployment ~$90/trade.
- Round-trip cost ~0.18% (spread only). **Costs are NOT the problem.**
- Dip-buy signal has **NO selection alpha** (excess ≈ 0, CI includes 0). It tracks the equal-weight market.
- In down-regimes (CAUTION/DEFENSIVE) the market **rebounds +4.5% to +7.7% over 10 days** — so the edge
  exists *if the position is held long enough*.
- **THE PROBLEM = EXIT LAYER.** Fixed 3.0–3.5% ATR-adaptive hard stop fires on **251/508 trades = −$608**
  (the single biggest loss bucket, −$2.42/trade). Median hold only **4.1 days**, but the edge needs ~10 days.
  The bot buys the dip → price makes a 1–3 day lower low → 3% stop fires → out right before the 5–10 day
  rebound. Negative-symmetric exit: the stop cuts the dip-down, so the rebound never happens on the book.
- Secondary fragmentation: ~52 break-even exits (+$38), ~106 KI exits (−$157), ~28 auto-trims, ~26 stale.
  **Damage is concentrated in the hard stop + KI trend-exits.**
- ATR% of stopped symbols: 78% have ATR% ≥ 3.0 → a 3% fixed stop ≈ **one day of normal noise**.

**Conclusion the research was to validate:** this is a *stop-placement* problem, not an entry or cost
problem. A mean-reversion dip-buy that tracks the market up should use **wide, volatility-scaled stops and
longer holds**, not a tight fixed stop. The literature below confirms exactly this, and names the specific
fixes.

---

## RANKED FINDINGS (what to change, in order of expected value)

### 1. WIDEN THE STOP TO A VOLATILITY-SCALED "BLACK SWAN" STOP (highest leverage)
The single most consistent finding across sources: **mean-reversion strategies must NOT use tight
trend-following stops.** QuantifiedStrategies is explicit:

> "…due to the nature of mean reversion strategies, you would likely only have a **'black swan
> stop-loss' far away from current price**, rather than the usual tight stop-loss you would use when
> using a trend-following strategy."
> — https://quantifiedstrategies.com/mean-reversion/

A 3–3.5% fixed stop is a *trend-following* stop. On a dip-buy it guarantees you exit on the very
adverse excursion the strategy is betting against. 78% of stopped symbols move ≥3% ATR in a day, so the
stop is firing on noise, not a regime change.
**Fix:** widen the hard stop to ~2–3× ATR (≈ 6–9% for typical symbols) as a pure *catastrophe* guard.
Its job becomes "cap a black swan," not "cut the dip." This directly removes the −$608 stop bucket.

### 2. REPLACE THE FIXED STOP WITH A CHANDELIER-STYLE TRAILING (ATR) STOP
The standard exit that lets a mean-reversion/continuation trade "run" while capping drawdown is the
**Chandelier Exit** (Chuck LeBeau): a trailing stop placed under the highest high since entry at
`highest_high − k × ATR` (k typically 2–3). It *only ratchets up* — it never tightens on a pullback.

> "The ATR is commonly used as an exit method… One popular technique is known as the **'chandelier exit'**…
> The chandelier exit places a trailing stop under the highest high the stock has reached since you
> entered the trade. The distance… is defined as some **multiple multiplied by the ATR**."
> — https://www.investopedia.com/terms/a/atr.asp

This is the *mechanical* fix for the negative-symmetric-exit problem: once price rebounds even 1% above
entry, the stop ratchets above the dip; it then trails up and lets the 5–10 day rebound run, instead of
getting stopped at the low. Use k = 2–3 ATR and trail from highest-high, **do not** reset the stop on the
way down.
**Note:** Wikipedia's "Chandelier exit" page does not exist; the canonical references are Investopedia
(ATR page) and LeBeau's original. The mechanics are well-established and simple to implement on daily bars.

### 3. EXIT ON THE MEAN-REVERSION SIGNAL, NOT ON A PRICE STOP
The cleanest mean-reversion exit is **symmetric to the entry**: you entered because RSI/price were
oversold; you exit when they are *no longer* oversold (reverted to the mean). QuantifiedStrategies'
RSI(3) mean-reversion backtest does exactly this:

> "When the RSI indicator has moved all the way from overbought to **oversold** (in a short position)
> or **from oversold to overbought** (in a long position)… on the daily close, **we exit our position on
> the next day's open**."
> — https://quantifiedstrategies.com/mean-reversion/

That produced a 73.96% win rate / 2.34 profit factor over 96 trades. The point: **the stop and the
signal should agree.** Your current bot has a *signal* exit (RSI/Bollinger improving) AND a *tight price*
exit, and the price exit fires first. **Fix:** make the primary exit the "reversion to mean / RSI flips
back above threshold" condition, with the wide volatility stop as the backstop. This also *lengthens*
the average hold naturally (you stay in until reversion completes, ~5–10 days), which is exactly the hold
the edge requires.

### 4. ADD A 200-DAY-MOVING-AVERAGE (TREND) FILTER TO STOP CATCHING FALLING KNIVES
The highest-performing dip-buy backtest in the literature **only buys dips in an overall uptrend**:

> "We use the 200-day moving average as a trend filter… The close must be **higher than the 200-day moving
> average**." → **329 trades, avg gain 0.52%/trade, 76% win rate, CAGR 5.7% (35% risk-adjusted per day
> invested).**
> — https://www.quantifiedstrategies.com/buy-the-dip/

And the warning is explicit for your exact failure mode:

> "…it is also just as likely for the asset price to continue declining… this is **more likely to happen
> with individual stocks** than with a broad market ETF… **be careful when practicing 'buy the dip' on
> individual stocks.**"
> — same source

Your bot keeps dip-buying in CAUTION/DEFENSIVE (falling knives). The measured data already shows dip-buys
have *no alpha* and *underperform the market* when the market is down — but the *market itself* rebounds.
A 200-day-MA / trend filter (or equivalently: only dip-buy when the symbol is above its long-term mean, or
when the *regime* is not CRITICAL) would stop you from buying individual names that are in a secular
downtrend. **Caveat (see §5):** a strict "only buy in uptrend" filter would also block the CAUTION/DEFENSIVE
rebound trades that *are* your edge — so the filter should be on **individual-symbol trend**, not on the
broad regime. Prefer: skip a dip-buy if *that stock* is below its own 200-day MA; still allow buying when
the *broad market* dips in a CAUTION/DEFENSIVE regime.

### 5. REGIME / VOLATILITY REGIME FILTER (VIX-CLASS) — SIZE DOWN, DON'T JUST BLOCK
VIX is "the square root of the risk-neutral expectation of the S&P 500 variance over the next 30 calendar
days" — a forward-looking market-volatility gauge (https://en.wikipedia.org/wiki/Volatility_(finance),
https://www.investopedia.com/terms/v/vix.asp). Two valid uses:
- **As a sizing control:** raise the volatility regime (CAUTION/DEFENSIVE/high-VIX) → *smaller* per-trade
  size and *wider* stops. QuantifiedStrategies on position sizing: "higher market volatility may require
  smaller positions… In volatile markets, reducing position sizes helps manage risk effectively"
  (https://www.quantifiedstrategies.com/position-sizing/).
- **As an entry gate (careful):** high VIX historically *precedes* larger rebounds, so blocking entries in
  high-vol regimes would **remove your edge**, not protect it. Use the regime to scale *size/stop-width*,
  not to zero out entries.

### 6. VOLATILITY-BASED POSITION SIZING (ATR) — matches the "stay small" constraint
Rather than a fixed $, size each position by ATR so dollar-risk-per-trade is constant regardless of
volatility:

> "Volatility-Based Position Sizing uses tools like the **Average True Range (ATR)** to adapt position
> sizes… position size = maximum risk / risk per unit" (https://www.quantifiedstrategies.com/position-sizing/);
> ATR position-sizing is also standard per Investopedia ATR page ("indication of what size trade to use").

Because your stops are getting wider (§1–2), ATR-based sizing keeps risk/trade flat instead of letting a
wider stop on a high-ATR name blow past your per-trade budget. This is also what protects the 1:20
copy-trading constraint — risk is defined by *stop width × size*, so both must be set together.

### 7. BREAK-EVEN / TOO-EARLY EXITS — RELAX OR DEFER
A break-even stop that triggers on a 1–2% bounce locks in ~0 and, more importantly, **kicks you out of a
position that was mid-reversion**, forcing a higher-cost re-entry. The literature's consistent stance is
that profit/stop levels "should be set **around the mean**" (Investopedia mean-reversion) — i.e., let the
reversion complete rather than taking a break-even scalp. **Fix:** remove the aggressive break-even
ratchet, or only move-to-break-even *after* a meaningful reversion (e.g., price back to entry + 1 ATR),
never on a 0.5% tick.

---

## CONCRETE, IMPLEMENTABLE EXIT-LAYER SPEC (for shadow replay next)
Replace the current exit stack with this, then **shadow-replay all 508 closed trades** (per Trading-Bible
OOS rules) before touching `config.yaml`:

| Lever | Current | Proposed | Source |
|---|---|---|---|
| Hard stop | 3.0–3.5% fixed | **2.5–3× ATR** wide catastrophe stop (no ratchet down) | QS mean-reversion; QS position-sizing |
| Primary exit | Tight price stop + signal | **Mean-reversion exit**: RSI/Bollinger reverts to mean → exit next open; signal exit primary, price stop backstop | QS mean-reversion (RSI3) |
| Trailing | none / fixed | **Chandelier**: highest_high − 2.5×ATR, ratchets up only | Investopedia ATR |
| Hold horizon | median 4.1d | let it run to reversion (~5–10d); time-stop only at ~15–20d | QS mean-reversion |
| Break-even | aggressive | remove, or gate at entry+1 ATR | Investopedia mean-reversion |
| Entry filter | none (buys knives) | skip if **symbol < own 200-day MA** (per-symbol trend), *allow* broad-market CAUTION/DEFENSIVE rebounds | QS buy-the-dip |
| Sizing | ~$90 fixed | **ATR-based**: size = risk$/stop_width, keep risk/trade flat | QS position-sizing; Investopedia ATR |
| Regime | CAUTION/DEFENSIVE | scale **size down + stop wider** in high-vol; do **not** zero entries | Wikipedia VIX; QS position-sizing |

**Expected effect (hypothesis to test, not claim):** the wide stop + reversion exit + longer hold should
convert the −$608 stop bucket into held-to-reversion winners that capture the +4.5–7.7% down-regime
rebound. The 200-day-MA per-symbol filter removes the secular-downtrend knife-catches. Net should lift the
signal from market-tracking (excess ≈ 0) to a small positive, because the *exit* finally lets the tracked
rebound complete.

**What could go wrong (be honest in the shadow test):**
- Wider stops mean *larger* single-trade drawdowns before the stop — need ATR sizing (§6) so risk/trade
  stays ~constant, else a few big losers dominate.
- Mean-reversion exits can *hold through a real breakdown* (the black-swan case); the 2.5–3×ATR stop is
  the backstop that must actually be wide enough to survive a 2–3 day lower-low.
- Sample: 508 trades, but the CAUTION/DEFENSIVE rebound window is recent (Aug–Sep 2026); a longer hold
  concentrates more exposure in one regime → check the CI is not entirely from one month.

---

## SOURCE LIST (extracted live, cited inline above)
- https://quantifiedstrategies.com/mean-reversion/  (black-swan stop; RSI3 exit-on-reversion; 73.96% WR backtest)
- https://quantifiedstrategies.com/buy-the-dip/       (200-day-MA filter; 76% WR / 0.52% per trade; individual-stock knife warning)
- https://quantifiedstrategies.com/trailing-stop/     (trailing amount; volatility-adjusted trailing)
- https://quantifiedstrategies.com/position-sizing/   (fixed-$/fixed-%/ATR/Kelly; volatility → smaller size; stop-out risk)
- https://www.investopedia.com/terms/a/atr.asp        (chandelier exit mechanics; ATR position sizing; ATR as exit method)
- https://www.investopedia.com/terms/m/meanreversion.asp (Z-score/mean; stops set around the mean)
- https://www.investopedia.com/terms/r/rsi.asp        (RSI overbought/oversold thresholds)
- https://www.investopedia.com/terms/v/vix.asp        (VIX = 30-day forward vol)
- https://en.wikipedia.org/wiki/Volatility_(finance)  (VIX = sqrt of 30-day expected variance; forward-looking)
- https://en.wikipedia.org/wiki/Bollinger_Bands       (BB mean/deviation basis for the entry signal)
- https://en.wikipedia.org/wiki/Relative_strength_index (RSI definition)
- https://en.wikipedia.org/wiki/Drawdown_(economics)  (drawdown framing for the equity decline)

Raw extracted text: `/tmp/research_pages/*.txt` (26+ pages, ~250 KB of source).
