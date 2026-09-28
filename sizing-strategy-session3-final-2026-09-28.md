# Sizing Decision — Final (Session 3)
Date: 2026-09-28 · Commit: f4915fb · 1591 tests pass
Portfolio: 9,199.59 USD · Regime: NORMAL

## Decision (VoLLi + Advisor, final)

### 1. TREND_PULLBACK+GOLDEN_CROSS: 1.0 → 0.5 (implemented)
- n=71, realized −153 USD, the only combo with volume AND consistently negative results
- Manual override, ratchet unlock for this entry only
- Pre-registered revert: back to 1.0 if 50 closed trades confirm >50% winrate
- HIGH by_conviction stays 0.25 (already more aggressive than base 0.5)

### 2. Conviction ladder: stays 6/6/6/2 (no change)
- Monotonic invariant (User-Entscheid 2026-07-14, test_max_trade_pct_klammer.py)
- Risk-based sizing (high risk small, low risk big, up to $500) is realized
  via the **combo/asset axis**, NOT by inverting the conviction ladder
- MEDIUM stays 6.0 = the $500 basis anchor (6.0% × 8,356.85 × 1.0 = 501 USD)

### 3. No exit-mechanism change
- Counterfactual (measured): holding was WORSE at +3d for both
  KIPP_1H (median −1.32pp) and KI TIGHTEN (−3.51pp)
- The exits correctly avoid further decline — not a leak

### 4. No new combos live
- In-sample mining risk; shadow-only with pre-registered hypothesis, n≥100 OOS

### 5. Crypto: no size-up
- n=27, 95% CI spans 35–75% WR, fee-drag 4.49% — not enough evidence

## What was NOT done (and why)
- HIGH 6→4: would break monotonicity (HIGH must stay ≥ MEDIUM=6.0)
- MEDIUM 6→8: would break basis invariant (must stay 480–520 USD) and
  overshoot NORMAL max_trade_pct (6.0%)
- Kelly kelly_base 0.3681→lower: would shrink ALL trades, including MEDIUM
  (the proven engine), to reduce the 50–75 USD dust band

## Next steps
1. Wait for 50 closed trades post-deploy to measure TREND_PULLBACK+GOLDEN_CROSS
   at 0.5 vs 1.0
2. LLM Review Worker daily cycle will re-evaluate all weights (ratchet applies
   to all entries EXCEPT TREND_PULLBACK+GOLDEN_CROSS which has _manual_override)
3. Session 4: measure floor-rule violation (146 trades at 50–75 USD, under the
   100 USD Floor-Regel) — decide if dust_floor/signal_floor should be raised
