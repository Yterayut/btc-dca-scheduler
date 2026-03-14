# S4 Phase 0.5 - Round 4 Plan

Version: 1.0  
Status: Ready  
Objective: Expand analysis into BTC-bull / BTC-outperform regimes to determine whether BTC leg and rotation logic add real value when BTC is dominant.

---

## 1) Purpose

Round 3 answered a regime-biased question:

> In the recent GOLD-dominant sample, GOLD-only outperformed switching models.

Round 4 answers the missing half:

> When BTC truly outperforms GOLD, does S4 switching add value, and does neutral filtering help or hurt?

Round 4 remains measurement-only.  
No production logic changes will be made.

---

## 2) Why Round 4 Is Necessary

Round 3 showed:
- GOLD-only > Model B > Model A in the sampled GOLD-dominant regime
- BTC leg had negative value-add in that regime
- Most edge came from staying in GOLD

This does not prove:
- GOLD-only is always best
- BTC leg is always useless
- switching has no value in BTC-led markets

---

## 3) Core Questions

1. When BTC outperforms GOLD, does switching beat GOLD-only?
2. Does Model A capture BTC trend better than Model B?
3. Does neutral filtering become too conservative in BTC-bull regimes?
4. Is BTC leg true alpha in BTC-dominant windows, or noise?
5. Should future design become regime-aware?

---

## 4) Models to Compare

### Model A - Production Baseline
- CDC-only execution
- neutral-state remains log-only

### Model B - Candidate Filter
- CDC buy only if `neutral_state == btc_signal`
- analysis-only

### Model C - GOLD-only Baseline
- hold GOLD continuously
- no flips

### Model D - BTC-only Baseline
- hold BTC continuously
- no flips

---

## 5) Required Windows

Round 4 must include:

1. BTC-dominant window (sustained BTC outperformance)
2. Mixed window (alternating leadership)
3. GOLD-dominant control window

Each window must report:
- requested start/end
- actual usable rows
- objective selection rationale
- assigned label (`btc_dominant_window`, `mixed_window`, `gold_dominant_window`)

---

## 6) Deliverable 1 - Multi-Window Model Comparison

Output:
- `s4_round_4_window_comparison.csv`
- `s4_round_4_window_comparison.json`

Metrics per model x window:
- `total_return_pct`
- `expectancy_pct`
- `max_drawdown_pct`
- `avg_mae_pct`
- `btc_leg_expectancy_pct`
- `gold_leg_expectancy_pct`
- `time_in_btc_pct`
- `time_in_gold_pct`
- `btc_event_count`
- `gold_event_count`
- `conflict_days`
- `switch_count`

---

## 7) Deliverable 2 - BTC-only Baseline

Output:
- `s4_btc_only_baseline_summary.json`
- `s4_btc_only_baseline_comparison.csv`

Metrics:
- `total_return_pct`
- `expectancy_pct`
- `max_drawdown_pct`
- `avg_mae_pct`
- `rows`
- `time_in_btc_pct = 100%`
- `event_count = 1`

---

## 8) Deliverable 3 - BTC Leg Value Add in BTC-Bull Regimes

Output:
- `s4_btc_bull_value_add.csv`
- `s4_btc_bull_value_add_summary.json`

Comparisons:
- Model A vs BTC-only
- Model B vs BTC-only
- Model A vs GOLD-only
- Model B vs GOLD-only

Metrics:
- `delta_total_return_pct`
- `delta_total_expectancy_pct`
- `delta_drawdown_pct`
- `delta_mae_pct`
- `positive_btc_value_add_events`
- `negative_btc_value_add_events`
- `net_btc_leg_value_add_pct`

---

## 9) Deliverable 4 - Regime Performance Map

Output:
- `s4_regime_performance_map.csv`
- `s4_regime_performance_map.json`

Expected summary shape:

| Regime Type | Model A | Model B | GOLD-only | BTC-only | Winner |
|---|---:|---:|---:|---:|---|
| Gold dominant | ... | ... | ... | ... | ... |
| Mixed | ... | ... | ... | ... | ... |
| BTC dominant | ... | ... | ... | ... | ... |

---

## 10) Deliverable 5 - Neutral Filter Cost (BTC-Bull)

Output:
- `s4_neutral_filter_cost.csv`
- `s4_neutral_filter_cost_summary.json`

Metrics:
- `missed_btc_entries`
- `missed_return_pct`
- `delayed_entry_days`
- `delayed_entry_cost_pct`
- `false_entry_avoidance_gain_pct`

---

## 11) Deliverable 6 - Hold-vs-Flip Attribution (Extended)

Output:
- `s4_round_4_return_attribution.json`

Fields:
- `carry_from_gold_hold`
- `carry_from_btc_hold`
- `gain_from_switching`
- `loss_from_switching`
- `gain_saved_by_staying_in_btc`
- `gain_saved_by_staying_in_gold`
- `switching_net_value_add`

---

## 12) Deliverable 7 - Regime Transition Study

Output:
- `s4_regime_transition_study.csv`
- `s4_regime_transition_study.json`

Metrics:
- `transition_date`
- `regime_before`
- `regime_after`
- `model_position_before`
- `model_position_after`
- `days_to_correct_position`
- `drawdown_during_transition`
- `missed_return_during_transition`

---

## 13) Deliverable 8 - Round 4 Decision Memo

Output:
- `s4_phase_0_5_round_4_decision_memo.md`

Memo must answer:
1. In BTC-bull regimes, which model wins?
2. Does Model A justify BTC switching better than Model B?
3. Does Model B become too conservative in BTC-led markets?
4. Is regime-aware architecture justified?
5. What should Round 5 test?

---

## 14) Required Commands

If exporter supports date ranges:

```bash
./venv/bin/python scripts/s4_phase_0_5_analysis.py --start YYYY-MM-DD --end YYYY-MM-DD --output-dir log
```

If not, extend exporter to support:
- `--start`
- `--end`
- window labels
- baseline selection flags

---

## 15) Locked Definitions

### Expectancy
`expectancy_pct = win_rate * avg_win_pct + (1 - win_rate) * avg_loss_pct`

### BTC-bull window
Window qualifies if:
- ratio trend is positive over sustained period
- BTC-only materially outperforms GOLD-only
- regime labeling confirms BTC leadership for most rows

### Neutral filter cost
Count missed BTC opportunity when:
- Model A enters BTC
- Model B blocks entry
- subsequent BTC/XAU forward return is materially positive

---

## 16) Pass / Fail Criteria

### Model A passes BTC-bull test if
- beats GOLD-only materially
- captures enough BTC upside relative to BTC-only
- without excessive switching drag

### Model B passes BTC-bull test if
- avoids false BTC entries
- without missing too much BTC upside
- and remains competitive with Model A

### BTC leg passes if
- adds value vs GOLD-only in BTC-dominant windows
- and does not lag BTC-only excessively

### Regime-aware design justified if
- winners differ consistently by regime type

---

## 17) What NOT To Do

- Do not change production logic
- Do not promote Model B live yet
- Do not optimize thresholds from selected windows
- Do not overfit one BTC-bull episode
- Do not conclude BTC-only is always best from one sample

Round 4 remains analysis-only.

---

## 18) Interpretation Guidelines

- If BTC-only dominates BTC-bull windows:
  - switching may be too slow for BTC convexity
- If Model A beats GOLD-only and is near BTC-only:
  - CDC has value in BTC-led markets
- If Model B underperforms Model A in BTC-bull windows:
  - neutral filter may be too conservative
- If winners differ by regime:
  - regime-aware architecture is a valid next-step candidate

---

## 19) Mandatory Final Table

| Window Type | Model A | Model B | GOLD-only | BTC-only | Winner |
|---|---:|---:|---:|---:|---|
| Gold dominant | ... | ... | ... | ... | ... |
| Mixed | ... | ... | ... | ... | ... |
| BTC dominant | ... | ... | ... | ... | ... |

---

## 20) Bottom Line

Round 4 must answer, with evidence:

> When BTC truly leads, does S4 switching still matter, and which version captures that upside best?

