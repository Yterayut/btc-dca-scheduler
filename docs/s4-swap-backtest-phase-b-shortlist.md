# S4 Swap Backtest - Phase B Shortlist Analysis
Date: 2026-03-09

## What Was Checked
From `log/s4_swap_backtest_full/`:
- `s4_swap_param_sweep_results.csv`
- `s4_swap_backtest_top_configs.json`
- `s4_swap_backtest_holdout.json`

## 1) Strict Criteria Test (as requested)
Criteria:
1. `return > 0` in at least `3/4` windows
2. `max_drawdown_pct >= -30` in **all** windows
3. `false_swap_per_year < 3` in **all** windows

Result:
- `0` configs passed.

Interpretation:
- Current strict DD threshold (`-30%`) is too tight for this system across mixed + BTC-bull windows.

## 2) Relaxation Check
Tested multiple thresholds:
- `dd >= -35`, false `<3` -> `0` pass
- `dd >= -40`, false `<3` -> `0` pass
- `dd >= -45`, false `<3` -> `18` pass

Implication:
- Practical shortlist emerges around `max_drawdown >= -45%`.

## 3) Minimax Top Family (Full Sweep)
Top minimax configs converged to:
- `swap_btc_confirm_days = 3`
- `swap_xau_confirm_days = 5`
- `swap_btc_slope_min = 2.0`
- `swap_btc_gap_max = 2.0`
- `swap_cooldown_days = 7`
- `swap_require_neutral` both true/false variants

Representative config: `663`.

## 4) Model E vs Best F (cfg 663)
| Window | F (cfg 663) | E (DCA-only) | Delta (F-E) |
|---|---:|---:|---:|
| W1_gold_dominant | +58.07% | +148.35% | -90.28% |
| W2_btc_bull_2016_2018 | +862.22% | -94.66% | +956.88% |
| W3_btc_bull_2020_2021 | +286.35% | -86.48% | +372.83% |
| W4_recent_2023_2025 | +69.39% | -55.21% | +124.59% |

Interpretation:
- Swap lane (F) adds large upside in BTC-led/mixed windows.
- But in GOLD-dominant window (W1), DCA-only defensive behavior (E) still wins strongly.

## 5) Holdout Context (All Windows, 60d Tail)
Using top minimax configs:
- W1 holdout F: positive
- W4 holdout F: positive
- W2 holdout F: negative
- W3 holdout F: negative

Extra context check against BTC-only on holdout tails:
- W2/W3 holdout tails had BTC-only positive while F was negative.

Interpretation:
- W2/W3 negatives are **not** automatically explained by market crash regime shift.
- More likely: selected strict-entry configs under-participated BTC holdout upside.

## 6) Decision for Next Iteration
1. Keep production unchanged.
2. Use Phase B shortlist with relaxed DD gate (`>= -45%`) and minimax ranking.
3. Run targeted Phase C on shortlist only, adding:
   - participation metric vs BTC-only in holdout tails,
   - cap on under-exposure in BTC-positive holdouts.
