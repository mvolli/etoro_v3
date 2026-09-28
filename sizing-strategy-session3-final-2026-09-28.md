# Sizing Decision — FINAL (Session 3, corrected)
Date: 2026-09-28 · Tests: 1591/1591 pass

## Verified Data (live DB, post-26.07, CLOSED trades)

| Band | n | PnL raw | WR | ~Fees | Net ~ |
|------|----|---------|----|-------|-------|
| $50–75 | 147 | −50 | 36.7% | −60 | −111 |
| $75–100 | 82 | −81 | 29.3% | −46 | −127 |
| $100–150 | 89 | −121 | 28.1% | −74 | −195 |
| $150–300 | 148 | −178 | 36.5% | −171 | −349 |
| $300+ | 36 | −68 | **50.0%** | −85 | −153 |
| **TOTAL** | **502** | **−498** | **35.2%** | **−436** | **−935** |

| Conviction | n | PnL | WR |
|-----------|-----|------|------|
| HIGH | 31 | −32 | 35.5% |
| MEDIUM | 471 | −467 | 34.8% |

## Final Decision: NO BEHAVIORAL CHANGES

### Why my Session-2 plan was wrong
1. **"MEDIUM 6→8%"**: Violates 3 invariants (monotonicity, $500 basis anchor, regime cap). MEDIUM is already at the $500 basis at 6.0%.
2. **"HIGH 6→4%"**: Breaks monotonicity (HIGH must ≥ MEDIUM). And HIGH is only −32 USD (n=31) — not the dominant drag.
3. **"De-weight TREND_PULLBACK+GOLDEN_CROSS 1.0→0.5"**: The file was ALREADY correctly differentiated (base 1.0, HIGH/LOW/VH → 0.25) per VoLLi's 2026-09-11 decision. My edit over-damped MEDIUM (the only non-negative tier) and broke the live-state test.
4. **"Exit-capping is the WR lever"**: Counterfactual REFUTED this. Holding was WORSE at +3d for both KIPP_1H (−1.32pp) and KI TIGHTEN (−3.51pp). Exits are correct.

### What the data actually says
- **Every size band is negative.** The best WR ($300+, 50%) is still −68 raw. No size redistribution can fix a system that loses at every level.
- **The root cause is entry quality**: 35.2% WR across 502 closed trades. Breakeven for a long-only system is ~51-52% (fees + slippage). The system is 16pp below breakeven.
- **"High-risk small, low-risk big" is ALREADY implemented**: conviction ladder (6/6/6/2), combo weights (0.25 for bad tiers), Kelly asset-class split, entry-quality gate, regime scaling. All working as designed.
- **$500 target is already reachable**: MEDIUM basis = 6.0% × 8,356 = $501. Kelly/EQG/regime correctly shrink to ~$75 based on risk assessment.

### The real lever (for future work)
**Entry quality / signal generation** — not sizing, not exits, not combo weights.
- 35% WR means the signals have negative expected value before fees.
- No sizing change can fix this. Only better entries (higher WR) or fewer trades (reduce fee drag on losing signals) will turn the system positive.
- Next step: analyze WHY the 35% WR persists (signal timing, regime mismatch, overtrading) and design entry-quality improvements.

## Files
- config/config.yaml: comment-only updates (documenting the analysis, values unchanged 6/6/6/2)
- data/llm_signal_weights.json: restored to pre-analysis state (base 1.0, by_conv HIGH/LOW/VH 0.25)
- No code changes

## Guardrails for future sizing work
1. Monotonicity: VERY_HIGH ≥ HIGH ≥ MEDIUM ≥ LOW (test-enforced, User-Entscheid 2026-07-14)
2. Basis anchor: MEDIUM must stay ~$500 at 6.0% (test-enforced)
3. Regime cap: basis ≤ regime.max_trade_pct (test-enforced)
4. Live-state test: TREND_PULLBACK+GOLDEN_CROSS must have MEDIUM free, others damped
5. 1 money-impacting change per 50 closed trades (guardrail from Session 2)
