# S4 Swap Backtest - Full Sweep Results
Date: 2026-03-09

## Scope Completed
1. Investigated `W4_recent_2023_2025` event behavior for default Model F.
2. Ran full sweep (`3,456` configs) with minimax ranking:
   - maximize `worst_window_return_pct_F`
   - tie-break with `avg_total_return_pct_F`
3. Ran holdout on **all windows** (last 60 days each).

## Commands
```bash
./venv/bin/python scripts/s4_swap_backtest.py \
  --output-dir log/s4_swap_backtest_full \
  --sweep-mode full \
  --top-k 5 \
  --holdout-days 60
```

## Output Files
- `log/s4_swap_backtest_full/s4_swap_backtest_summary_all_windows.csv`
- `log/s4_swap_backtest_full/s4_swap_backtest_events_all_windows.csv`
- `log/s4_swap_backtest_full/s4_swap_param_sweep_results.csv`
- `log/s4_swap_backtest_full/s4_swap_backtest_top_configs.json`
- `log/s4_swap_backtest_full/s4_swap_backtest_holdout.json`

## W4 Investigation (Default Model F)
- Event count in W4 (default F): `12` full swaps
- Pattern: frequent alternating `XAU_TO_BTC` and `BTC_TO_XAU` (mixed regime churn)
- This explains weak W4 return under default config (`+3.64%`) and poor DD profile.

## Full Sweep - Best F by Window (from sweep table)
- W1: `+77.48%` (cfg `873`)
- W2: `+1102.97%` (cfg `895`)
- W3: `+494.31%` (cfg `67`)
- W4: `+69.39%` (cfg `663`)

## Minimax Top Config Family
Top-ranked configs (by worst-window return then avg return) converged to:
- `swap_btc_confirm_days=3`
- `swap_xau_confirm_days=5`
- `swap_btc_slope_min=2.0`
- `swap_btc_gap_max=2.0`
- `swap_cooldown_days=7`
- `swap_require_neutral`: both `true/false` appeared

Representative top row (`config_id=663`):
- `worst_window_return_pct_F = +58.07%`
- `avg_total_return_pct_F = +319.01%`
- `avg_false_swaps_F = 0.75`

## Holdout (All Windows, 60-day tail)
For top minimax configs:
- W1 holdout F: `+55.79%`
- W4 holdout F: `+56.09%`
- W2 holdout F: `-26.71%`
- W3 holdout F: `-18.25%`

Interpretation:
- Top configs remain robust in W1/W4 holdout slices.
- W2/W3 holdout slices are negative, indicating instability across late-cycle tails.

## Decision Impact
1. The framework now supports full sweep + minimax + per-window holdout as requested.
2. W4 red flag is confirmed to be churn-driven under default F.
3. Full sweep can find much better W4/W2/W3 performance than default, but holdout is mixed.
4. Promotion to live swap is still premature; keep swap lane offline and continue with stricter validation.

## Recommended Next Step
- Phase B evaluation (targeted shortlist) with explicit acceptance gates:
  - pass >=3/4 windows,
  - constrain worst-window drawdown,
  - cap false swaps per 90d,
  - compare against Model A/B and GOLD-only in each window.
