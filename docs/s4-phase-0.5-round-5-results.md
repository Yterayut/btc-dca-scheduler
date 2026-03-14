# S4 Phase 0.5 - Round 5 Results Summary
Date: 2026-03-09  
Data source: `fred_lbma` (`BTC=CBBTCUSD` from FRED, `GOLD=gold_pm` from LBMA)

## Scope
- `btc_bull_2016_2018`: 2016-01-01 to 2018-01-31 (usable rows: 523)
- `btc_bull_2020_2021`: 2020-04-01 to 2021-11-30 (usable rows: 419)
- `btc_bull_recent_probe`: 2023-01-01 to 2025-12-31 (usable rows: 752)

## Window Winners (Total Return)
| Window | Model A (CDC) | Model B (CDC+Filter) | GOLD-only | BTC-only | Winner |
|---|---:|---:|---:|---:|---|
| btc_bull_2016_2018 | +957.98% | +1249.41% | -94.66% | +1773.14% | BTC-only |
| btc_bull_2020_2021 | +249.06% | +219.06% | -86.48% | +639.61% | BTC-only |
| btc_bull_recent_probe | +26.10% | +57.32% | -55.21% | +123.24% | BTC-only |

## Key Findings
1. In all 3 BTC-bull windows, `BTC-only` is the top performer.
2. Both switching models (`A`, `B`) strongly beat `GOLD-only` in all windows.
3. `Model B` beats `Model A` in 2 windows (`2016-2018`, `recent`), but loses in `2020-2021`.
4. Both `Model A` and `Model B` still lag `BTC-only` materially in every window.

## BTC Leg Value-Add (vs Baselines)
- `Model A` vs `GOLD-only` return delta: `+1052.64%`, `+335.54%`, `+81.31%`
- `Model B` vs `GOLD-only` return delta: `+1344.08%`, `+305.54%`, `+112.52%`
- `Model A` vs `BTC-only` return delta: `-815.17%`, `-390.55%`, `-97.14%`
- `Model B` vs `BTC-only` return delta: `-523.73%`, `-420.55%`, `-65.92%`

Interpretation:
- Switching adds major value relative to GOLD-only during BTC-bull windows.
- Switching still misses substantial BTC convex upside versus holding BTC throughout.

## Neutral Filter Cost (Model B vs Model A)
- `2016-2018`: avg missed return over 5d = `-0.49%` (filter likely avoided weak entries on average)
- `2020-2021`: avg missed return over 5d = `+1.59%` (filter likely blocked profitable BTC entries)
- `recent`: avg missed return over 5d = `-0.27%` (filter again skewed defensive)

Interpretation:
- Filter behavior is regime/phase dependent.
- In stronger BTC acceleration phases (e.g., 2020-2021), Model B looks too conservative.

## Hold-vs-Flip Attribution
Switching net value add from attribution is negative for both models in all three windows:
- `Model A`: `-130.57%`, `-36.84%`, `-109.04%`
- `Model B`: `-187.58%`, `-120.84%`, `-201.35%`

Interpretation:
- Current attribution method indicates switching friction/lag dominates gains from the flip timing itself.
- Most captured gains appear to come from being in the correct leg for stretches, not from frequent switching.

## Round 5 Decision
1. `BTC-only` is the best benchmark winner across true BTC-bull windows.
2. `Model A/B` are useful versus `GOLD-only`, so rotation still has value when compared to staying in GOLD.
3. `Model B` is not universally better than `Model A`; it can underperform when BTC momentum is strong.
4. Evidence supports a regime-aware direction for future rounds:
   - allow more aggressive BTC capture in strong BTC regimes,
   - keep defensive filtering for non-BTC-dominant phases.

## Referenced Outputs
- `log/round5_fred_lbma_2016_2018/`
- `log/round5_fred_lbma_2020_2021/`
- `log/round5_fred_lbma_recent/`
