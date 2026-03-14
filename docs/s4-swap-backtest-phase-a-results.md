# S4 Swap Backtest - Phase A Sweep Results
Date: 2026-03-09

## What Was Implemented
- Added sweep and holdout support to:
  - `scripts/s4_swap_backtest.py`
- Added CLI options:
  - `--sweep-mode {none,phase_a,full}`
  - `--sweep-limit`
  - `--top-k`
  - `--holdout-days`
- Added outputs:
  - `s4_swap_param_sweep_results.csv`
  - `s4_swap_backtest_top_configs.json` (top-k by avg return, Model F)
  - `s4_swap_backtest_holdout.json` (holdout evaluation)

## Run Command
```bash
./venv/bin/python scripts/s4_swap_backtest.py \
  --output-dir log/s4_swap_backtest_phase_a \
  --sweep-mode phase_a \
  --top-k 5 \
  --holdout-days 60
```

## Default Config Baseline (A/B/C/D/E/F/G)
From `s4_swap_backtest_summary_all_windows.csv`:

| Window | Best Model by Return |
|---|---|
| W1_gold_dominant | F (+173.00%) |
| W2_btc_bull_2016_2018 | D (+1773.14%) |
| W3_btc_bull_2020_2021 | D (+639.61%) |
| W4_recent_2023_2025 | D (+123.24%) |

## Sweep Scope (Phase A)
- Swept BTC-entry parameters only (384 configs total):
  - `swap_btc_confirm_days`: 3,5,7,10
  - `swap_btc_slope_min`: 0.5,1.0,1.5,2.0
  - `swap_btc_gap_max`: 2,3,4,5
  - `swap_cooldown_days`: 7,14,21
  - `swap_require_neutral`: true,false
- Fixed exit params:
  - `swap_xau_confirm_days=3`
  - `swap_xau_slope_max=-0.5`

## Top Config Pattern (Model F)
Top-ranked configs converged to:
- `swap_btc_confirm_days`: mostly `7` (some `3/5`)
- `swap_btc_slope_min`: `0.5`
- `swap_btc_gap_max`: `4.0` or `5.0`
- `swap_cooldown_days`: `7`
- `swap_require_neutral`: mixed (both true/false appeared)

## Holdout (W1 last 60 days)
From `s4_swap_backtest_holdout.json`:
- Top-5 configs produced very similar holdout returns for Model F (`~55.79%` in holdout slice)
- Model G holdout converged to same value in this slice due limited staged events

## Interpretation (Current Round)
1. Sweep infrastructure is now working end-to-end with real data.
2. For this phase-a space, permissive BTC entry (`slope_min=0.5`) with moderate confirmation (`~7 days`) dominated.
3. `cooldown=7` is consistently favored in top configs.
4. More work remains before promotion:
   - run `full` sweep (3,456 configs)
   - perform cross-window decision using both return and drawdown constraints
   - inspect event-level whipsaw behavior on selected configs
