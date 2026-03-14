# S4 DCA + Swap Spec (Execution v1)
Date: 2026-03-09  
Status: Draft for offline validation first

## 1) Objective
Split S4 decisions into 2 lanes with different risk budgets:
- `DCA lane` (new money): fast reaction, lower confirmation.
- `Swap lane` (existing holdings): strict confirmation, optional staged execution.

Core signal basis: `BTC/XAU ratio trend`.

## 2) Core Principle
- If BTC/XAU trend is `down`: prefer `XAU`.
- If BTC/XAU trend is `up`: prefer `BTC`.

But:
- DCA can flip quickly.
- Swap must be conservative to avoid heavy opportunity loss.

## 3) Decision Lanes
### 3.1 DCA Lane (Fast)
- `CDC = up` -> route new DCA to `BTC`.
- `CDC = down` -> route new DCA to `XAU`.
- No portfolio liquidation in this lane.

### 3.2 Swap Lane (Slow)
Use asymmetric confirmation:
- `XAU -> BTC` (harder): require strong multi-gate confirmation.
- `BTC -> XAU` (easier): allow faster risk-off response.

## 4) Swap Gates
## 4.1 XAU -> BTC (5 gates, must pass all)
1. `cdc_status == up`
2. `cdc_up_confirm_days >= 5` (tunable: 3-10)
3. `neutral_state == btc_signal`
4. `slope_pct >= +1.00`
5. `ema_gap_pct <= +3.00`

If all pass -> trigger swap plan (prefer staged).

## 4.2 BTC -> XAU (3 gates, must pass all)
1. `cdc_status == down`
2. `cdc_down_confirm_days >= 3` (tunable: 2-7)
3. `slope_pct <= -0.50`

If all pass -> trigger swap plan (can be faster than XAU->BTC).

## 5) Partial Swap Policy (Default)
Avoid 100% one-shot flip by default:
- Stage 1: swap `30%` (day 0)
- Stage 2: swap additional `30%` if gates still hold after `+3` days
- Stage 3: swap remaining `40%` if gates still hold after `+5` days from stage 2

Fallback:
- If confirmation fails before next stage, stop further stages.
- Do not auto-swap back immediately; hold mixed allocation until next clean signal.

## 6) Risk Controls
- Max one swap stage per day.
- Cooldown after any swap execution or aborted sequence: `>= 14 days`.
- Log every gate value at decision time.
- Keep DCA lane active even during swap cooldown.

## 7) Backtest Scope (must pass before live swap)
Windows:
- `2016-01-01..2018-01-31` (classic BTC bull)
- `2020-04-01..2021-11-30` (institutional BTC bull)
- `2023-01-01..2025-12-31` (recent probe)
- GOLD-dominant control window (latest available, separate run)

Models:
- `A`: CDC-only
- `B`: CDC + neutral filter
- `C`: GOLD-only
- `D`: BTC-only
- `E`: DCA lane + strict swap lane (this spec)

## 8) Acceptance Criteria (for promoting swap lane)
`E` may be promoted only if all are true:
1. Beats `A` on total return in at least 2/3 BTC-bull windows.
2. Beats `A` on max drawdown in at least 2/3 windows.
3. Outperforms `GOLD-only` in BTC-bull windows.
4. No severe whipsaw cluster: <= 2 failed swap sequences per 90 trading days.

If not satisfied:
- keep only DCA lane logic live,
- keep swap lane offline.

## 9) Metrics to Report
- Total return %
- Max drawdown %
- Expectancy %
- Time in BTC / XAU
- Swap count and stage count
- Failed swap sequence count
- Avg delay to correct asset after regime shift

## 10) Operational Rollout
Phase 1:
- Live: DCA lane only
- Offline: swap lane simulation

Phase 2:
- Live: partial swap (`30/30/40`) with conservative gates
- Monitor weekly drift and whipsaw metrics

Phase 3:
- Retune thresholds only with multi-window evidence
