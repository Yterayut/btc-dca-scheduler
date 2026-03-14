# S4 Phase 0.5 - Round 5 Plan: True BTC Bull Regime Validation

Version: 1.0  
Status: Ready  
Objective: Validate whether S4 switching logic has real value in true BTC bull regimes, and determine whether Model A, Model B, BTC-only, or GOLD-only is the correct benchmark when BTC is dominant.

---

## 1) Purpose

Rounds 3 and 4 established that in the recent GOLD-dominant sample:
- GOLD-only outperformed switching models
- BTC leg had negative value-add
- Model B improved over Model A mainly by staying in GOLD more consistently
- BTC-only performed very poorly

But that does not answer:

> When BTC truly leads in a multi-month / multi-quarter bull regime, does S4 switching add value, or does it lag simpler BTC-heavy baselines?

Round 5 exists to answer that question.

This round remains measurement-only.  
No production logic changes will be made during Round 5.

---

## 2) Why Round 5 Is Necessary

Current evidence is one-sided (mostly GOLD-led). We still do not know:
- whether BTC leg becomes valuable in true BTC bull markets
- whether Model A captures BTC convex upside better than Model B
- whether Model B becomes too conservative when BTC is the correct asset
- whether regime-aware architecture is justified

---

## 3) Core Questions

1. In true BTC bull regimes, does switching beat GOLD-only?
2. In true BTC bull regimes, how close can S4 get to BTC-only?
3. Does Model A capture BTC upside better than Model B?
4. Does Model B become too conservative and leave too much BTC upside?
5. Does BTC leg become a real alpha source in BTC-led markets?
6. Is regime-aware logic justified from evidence across both sides of the market?

---

## 4) Models to Compare

### Model A - Current Production Baseline
- CDC-only execution
- neutral-state remains log-only

### Model B - Candidate Filter Model
- CDC buy only if `neutral_state == btc_signal`
- analysis-only

### Model C - GOLD-only Baseline
- hold GOLD continuously through the full window

### Model D - BTC-only Baseline
- hold BTC continuously through the full window

---

## 5) Recommended True BTC Bull Windows

Round 5 should use explicit historical windows.

### Window A - Classic BTC Bull Cycle
`2016-01-01` to `2018-01-31`

Why:
- clean macro BTC uptrend
- strong cycle expansion
- good test of whether switching captures prolonged BTC strength

### Window B - Post-COVID / Institutional BTC Bull
`2020-04-01` to `2021-11-30`

Why:
- strong modern BTC bull run
- more relevant to current market structure than 2016 cycle
- strong trend + momentum + transition phases

### Window C - Recent Bull / ETF-era Probe
`2023-01-01` to latest available date  
or at least `2023-01-01` to `2025-12-31` if feed coverage supports it.

Why:
- closest to current structure
- highest relevance for current decisioning

---

## 6) Optional Control Windows

- Mixed regime window (alternating BTC/GOLD leadership)
- GOLD-dominant control window (reuse Round 3/4 style sample)

Purpose:
- compare model ranking changes by regime
- reduce single-environment bias

---

## 7) Window Selection Rules

For every selected window, document:
- requested_start_date
- requested_end_date
- actual_usable_rows
- data source availability
- reason for selection
- label:
  - `true_btc_bull`
  - `mixed`
  - `gold_dominant_control`

True BTC bull qualification:
- BTC-only clearly outperforms GOLD-only over full window
- BTC trend is sustained positive (weekly/monthly structure)
- BTC/XAU ratio trend is broadly positive, not just local bounce

---

## 8) Deliverable 1 - Multi-Window Summary Table

Output files:
- `s4_round_5_window_comparison.csv`
- `s4_round_5_window_comparison.json`

Required metrics per model x window:
- total_return_pct
- expectancy_pct
- max_drawdown_pct
- avg_mae_pct
- btc_leg_expectancy_pct
- gold_leg_expectancy_pct
- time_in_btc_pct
- time_in_gold_pct
- btc_event_count
- gold_event_count
- conflict_days
- switch_count
- vs_gold_only_return_pct
- vs_btc_only_return_pct
- vs_gold_only_expectancy_pct
- vs_btc_only_expectancy_pct

Mandatory summary table:

| Window | Model A | Model B | GOLD-only | BTC-only | Winner |
|---|---:|---:|---:|---:|---|
| 2016-2018 BTC bull | ... | ... | ... | ... | ... |
| 2020-2021 BTC bull | ... | ... | ... | ... | ... |
| 2023+ BTC probe | ... | ... | ... | ... | ... |
| Mixed control | ... | ... | ... | ... | ... |
| GOLD control | ... | ... | ... | ... | ... |

---

## 9) Deliverable 2 - BTC Bull Value-Add Analysis

Output files:
- `s4_round_5_btc_bull_value_add.csv`
- `s4_round_5_btc_bull_value_add_summary.json`

Comparisons:
- Model A vs BTC-only
- Model B vs BTC-only
- Model A vs GOLD-only
- Model B vs GOLD-only

Required metrics:
- delta_total_return_vs_btc_pct
- delta_total_return_vs_gold_pct
- delta_total_expectancy_vs_btc_pct
- delta_total_expectancy_vs_gold_pct
- delta_drawdown_vs_btc_pct
- delta_drawdown_vs_gold_pct
- number_of_btc_entries
- positive_btc_value_add_events
- negative_btc_value_add_events
- net_btc_leg_value_add_pct

---

## 10) Deliverable 3 - Neutral Filter Cost in BTC Bull Windows

Output files:
- `s4_round_5_neutral_filter_cost.csv`
- `s4_round_5_neutral_filter_cost_summary.json`

Required metrics:
- missed_btc_entries
- missed_return_pct
- delayed_entry_days
- delayed_entry_cost_pct
- false_entry_avoidance_gain_pct
- regime_label
- window_label

---

## 11) Deliverable 4 - BTC Convexity Capture Score

Output files:
- `s4_btc_convexity_capture.csv`
- `s4_btc_convexity_capture_summary.json`

Suggested metrics:
- pct_of_btc_only_return_captured
- pct_of_btc_only_up_months_participated
- months_in_btc_during_major_btc_breakouts
- missed_major_btc_breakout_count
- average_delay_to_btc_entry_after_breakout

---

## 12) Deliverable 5 - Hold-vs-Flip Attribution in BTC Bull Windows

Output file:
- `s4_round_5_return_attribution.json`

Required fields:
- carry_from_btc_hold
- carry_from_gold_hold
- gain_from_switching
- loss_from_switching
- gain_saved_by_staying_in_btc
- gain_saved_by_staying_in_gold
- switching_net_value_add
- missed_btc_convexity_cost

---

## 13) Deliverable 6 - Regime Transition Study (BTC-focused)

Output files:
- `s4_round_5_regime_transition_study.csv`
- `s4_round_5_regime_transition_study.json`

Required metrics:
- transition_date
- regime_before
- regime_after
- model_position_before
- model_position_after
- days_to_correct_position
- missed_return_during_transition
- drawdown_during_transition

---

## 14) Deliverable 7 - Winner-by-Regime Map

Output files:
- `s4_round_5_regime_performance_map.csv`
- `s4_round_5_regime_performance_map.json`

Suggested table:

| Regime Type | Model A | Model B | GOLD-only | BTC-only | Winner |
|---|---:|---:|---:|---:|---|
| Gold dominant | ... | ... | ... | ... | ... |
| Mixed | ... | ... | ... | ... | ... |
| True BTC bull | ... | ... | ... | ... | ... |

---

## 15) Deliverable 8 - Round 5 Decision Memo

Output file:
- `s4_phase_0_5_round_5_decision_memo.md`

Memo must answer:
1. In true BTC bull windows, which model wins?
2. Does switching still matter when BTC leads?
3. Does Model B become too conservative?
4. Is Model A preferable in BTC-led markets?
5. Does evidence justify future regime-aware architecture?
6. What should Round 6 test?

---

## 16) Required Commands

Run with explicit date windows:

```bash
./venv/bin/python scripts/s4_phase_0_5_analysis.py --data-source fred_lbma --start 2016-01-01 --end 2018-01-31 --window-label btc_bull_2016_2018 --output-dir log
./venv/bin/python scripts/s4_phase_0_5_analysis.py --data-source fred_lbma --start 2020-04-01 --end 2021-11-30 --window-label btc_bull_2020_2021 --output-dir log
./venv/bin/python scripts/s4_phase_0_5_analysis.py --data-source fred_lbma --start 2023-01-01 --end 2025-12-31 --window-label btc_bull_recent_probe --output-dir log
```

Recommended source for long-history validation:
- `BTC`: FRED `CBBTCUSD`
- `GOLD`: LBMA `gold_pm`

If end date/rows are constrained by feed coverage, always report:
- requested dates
- actual rows
- actual final covered date

---

## 17) Locked Definitions

### Expectancy
`expectancy_pct = win_rate * avg_win_pct + (1 - win_rate) * avg_loss_pct`

### True BTC Bull Window
Must satisfy:
- BTC-only clearly outperforms GOLD-only
- BTC trend macro-positive
- BTC/XAU strength sustained, not local-only

### Neutral Filter Cost
Count missed BTC opportunity when:
- Model A enters BTC
- Model B blocks/delays entry
- forward BTC/XAU return is materially positive

### BTC Convexity Capture
A model captures BTC convexity when it participates in major asymmetric BTC upswings without excessive lag.

---

## 18) Pass / Fail Criteria

Model A passes BTC-bull validation if:
- materially beats GOLD-only
- remains reasonably competitive with BTC-only
- captures BTC upside without excessive switching drag

Model B passes BTC-bull validation if:
- avoids false entries
- without missing too much BTC upside
- and stays competitive with Model A in BTC-led windows

BTC leg passes if:
- adds value vs GOLD-only in BTC-bull windows
- and is not merely drag vs BTC-only

Regime-aware design is justified if:
- winners differ consistently by regime type

---

## 19) What NOT To Do During Round 5

- Do not change production logic
- Do not promote Model B live yet
- Do not optimize thresholds from selected BTC windows
- Do not cherry-pick favorable dates only
- Do not conclude from one BTC-bull window alone

Round 5 remains analysis-only.

---

## 20) Interpretation Guidelines

- If BTC-only dominates every BTC-bull window:
  - switching may be too slow for BTC convexity
- If Model A beats GOLD-only and stays close to BTC-only:
  - CDC has real BTC-led value
- If Model B lags Model A materially in BTC-bull windows:
  - neutral filter may be too conservative
- If winners differ by regime:
  - regime-aware architecture becomes a serious candidate

---

## 21) Bottom Line

Round 5 must answer with evidence:

> When BTC truly leads, does S4 switching still matter, and which S4 variant captures that upside best?
