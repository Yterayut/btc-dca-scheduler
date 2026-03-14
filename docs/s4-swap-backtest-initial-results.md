# S4 Swap Backtest - Initial Results (Default Config)
Date: 2026-03-09

## Run
```bash
./venv/bin/python scripts/s4_swap_backtest.py --output-dir log/s4_swap_backtest_default
```

## Output Files
- `log/s4_swap_backtest_default/s4_swap_backtest_summary_all_windows.csv`
- `log/s4_swap_backtest_default/s4_swap_backtest_events_all_windows.csv`
- `log/s4_swap_backtest_default/s4_swap_backtest_config.json`
- `log/s4_swap_backtest_default/s4_swap_backtest_top_configs.json`

## Windows
- `W1_gold_dominant` (OKX ratio, 2025-05-13..2026-03-07)
- `W2_btc_bull_2016_2018` (FRED/LBMA)
- `W3_btc_bull_2020_2021` (FRED/LBMA)
- `W4_recent_2023_2025` (FRED/LBMA)

## Snapshot (Total Return %)
| Window | A | B | C | D | E | F | G |
|---|---:|---:|---:|---:|---:|---:|---:|
| W1_gold_dominant | 133.79 | 142.38 | 148.35 | -59.73 | 148.35 | 173.00 | 49.28 |
| W2_btc_bull_2016_2018 | 957.98 | 1249.41 | -94.66 | 1773.14 | -94.66 | 293.48 | 41.96 |
| W3_btc_bull_2020_2021 | 249.06 | 219.06 | -86.48 | 639.61 | -86.48 | 308.62 | 16.15 |
| W4_recent_2023_2025 | 26.10 | 57.32 | -55.21 | 123.24 | -55.21 | 3.64 | -24.96 |

## Notes
1. This is a first implementation pass for Model `E/F/G` framework and event logging.
2. `E` is DCA-direction baseline with no swaps and therefore tracks the defensive hold behavior.
3. `F` uses full swap with 5-gate/3-gate logic and `G` uses staged partial swap.
4. Results here are directional only; parameter sweep and holdout validation are still pending.
