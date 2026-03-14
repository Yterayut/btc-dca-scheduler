# S4 Phase 0.5 Analysis Plan

## Objective
Validate whether current `CDC execution` should remain primary, and whether `neutral-state` should be promoted from observability to a future execution filter.

## Confirmed Baseline
- Baseline execution: `CDC` (`run_s4_tick` uses `cdc_status` for target allocation).
- `S4_CONFIRM_DAYS=2` confirms CDC daily status continuity.
- `neutral-state` is Phase 1 log-only telemetry (no execution impact yet).

## Locked Definitions
- `premature_exit` is true when either:
  - Forward BTC/XAU return after exit is `>= 3%` within `5` trading days, or
  - Re-entry into BTC occurs within `<= 3` trading days.
- `expectancy`:
  - `expectancy = win_rate * avg_win_pct + (1 - win_rate) * avg_loss_pct`

## Deliverables
1. `s4_phase_0_5_event_report.csv`
2. `s4_gold_event_report.csv`
3. `s4_gold_event_summary.json`
4. `s4_regime_summary.csv`
5. `s4_window_comparison.csv`
6. `s4_window_comparison.json`
7. `s4_system_expectancy_summary.json`
8. `s4_conflict_day_analysis.csv`
9. `s4_cdc_vs_filter_comparison.csv`
10. `s4_phase_0_5_event_summary.json`

## Models To Compare
- `model_a_cdc_execution`: In-BTC when CDC status is `up`.
- `model_b_cdc_plus_neutral_filter`: In-BTC when CDC status is `up` and neutral-state is `btc_signal`.

## Required Metrics
- Event metrics:
  - `event_count`, `avg_duration_days`, `median_duration_days`
  - `win_rate`, `avg_win_pct`, `avg_loss_pct`, `median_return_pct`, `expectancy_pct`
  - `avg_peak_return_pct`, `avg_mae_pct`
- Exit quality:
  - `premature_exit_count`, `premature_exit_rate`
- Conflict metrics:
  - CDC/neutral disagreement days and forward 3/5/7 day outcomes.

## Decision Gate
- Keep CDC primary if baseline expectancy remains positive and filter does not materially improve risk-adjusted event quality.
- Promote neutral filter to Phase 2 simulation only if it reduces false/weak BTC entries without sacrificing upside materially.
