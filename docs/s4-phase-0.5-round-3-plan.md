# S4 Phase 0.5 - Round 3 Plan

Version: 1.0  
Status: Ready  
Objective: Determine whether BTC leg still adds value beyond a simple GOLD-only baseline, and whether Model B improvement comes from better filtering or simply staying in GOLD longer.

---

## 1) Purpose

Round 3 exists to answer the next critical question after Round 2:

> Is S4 actually adding value beyond just holding GOLD, or is the current edge mostly a GOLD-hold effect?

This round remains measurement-only.  
No production logic changes will be made during Round 3.

---

## 2) Current Context

### Confirmed from prior rounds
- CDC = execution layer
- neutral-state = log-only layer
- S4_CONFIRM_DAYS=2 confirms CDC daily history
- Current production system = Model A (CDC-only)

### Round 2 key findings
- BTC leg expectancy is positive but very small
- GOLD leg contributes ~99% of system expectancy in the current sample
- Model B (CDC + neutral filter) outperforms Model A in analysis
- Sample size for BTC events remains small
- Production should remain unchanged for now

### New question
If GOLD leg contributes almost all current edge:
- Is BTC leg still worth keeping?
- Or is current system performance mostly explained by holding GOLD through a favorable regime?

---

## 3) Models to Compare

### Model A - Current Production Baseline
- CDC-only execution
- neutral-state remains log-only

### Model B - Candidate Filter Model
- CDC buy only if neutral_state == btc_signal
- analysis-only, not live

### Model C - GOLD-only Baseline
- Enter GOLD on day 1
- Stay in GOLD throughout the full window
- No CDC
- No neutral-state
- No flips

---

## 4) Deliverable 1 - GOLD-only Baseline

### Goal
Create a simple baseline to test whether active switching adds value.

### Model C definition
- Initial position: GOLD
- Hold GOLD continuously through the selected window
- No switching logic
- No execution filters
- No confirm rules

### Output files
- `s4_gold_only_baseline_summary.json`
- `s4_gold_only_baseline_comparison.csv`

### Required metrics
- `total_return_pct`
- `expectancy_pct`
- `max_drawdown_pct`
- `avg_mae_pct`
- `window_start`
- `window_end`
- `rows`
- `time_in_gold_pct` = 100%
- `event_count` = 1 (or equivalent synthetic hold segment)

### Required comparison
Compare GOLD-only against:
- Model A (CDC-only)
- Model B (CDC + neutral filter)

### Core questions
- Does GOLD-only outperform Model A?
- Does GOLD-only outperform Model B?
- Is switching providing measurable edge beyond simple GOLD hold?

---

## 5) Deliverable 2 - Regime Comparison by Model

### Goal
Determine whether BTC leg has value in some regimes but not others.

### Regime labels
- `gold_dominant`
- `mixed`
- `btc_dominant`

### Output files
- `s4_regime_model_comparison.csv`
- `s4_regime_model_comparison.json`

### Required metrics per model x regime
- `rows`
- `btc_event_count`
- `gold_event_count`
- `win_rate_btc`
- `expectancy_btc_pct`
- `expectancy_gold_pct`
- `total_expectancy_pct`
- `avg_duration_btc_days`
- `avg_duration_gold_days`
- `avg_mae_btc_pct`
- `avg_mae_gold_pct`
- `conflict_days`

### Core questions
- Does BTC leg only work in `btc_dominant` regimes?
- Does Model B outperform Model A in all regimes or mainly in `gold_dominant`?
- In `gold_dominant` windows, is flipping to BTC worth it?

---

## 6) Deliverable 3 - BTC Leg Value-Add Analysis

### Goal
Measure whether BTC leg contributes positively relative to a GOLD-only baseline.

### Output files
- `s4_btc_leg_value_add.csv`
- `s4_btc_leg_value_add_summary.json`

### Required comparisons
- Model A vs GOLD-only
- Model B vs GOLD-only

### Required metrics
- `delta_total_expectancy_pct`
- `delta_total_return_pct`
- `delta_drawdown_pct`
- `delta_mae_pct`
- `number_of_btc_entries`
- `positive_btc_value_add_events`
- `negative_btc_value_add_events`
- `net_btc_leg_value_add_pct`

### Core questions
- Is BTC leg an alpha source or a noise source?
- Does BTC leg improve system return enough to justify switching risk?
- Does Model B preserve BTC upside better than Model A?

---

## 7) Deliverable 4 - Hold-vs-Flip Attribution

### Goal
Separate gains from holding vs gains from switching.

### Output file
- `s4_return_attribution.json`

### Required attribution fields
- `carry_from_gold_hold`
- `carry_from_btc_hold`
- `gain_from_switching`
- `loss_from_switching`
- `gain_lost_due_to_false_btc_entries`
- `gain_saved_due_to_staying_in_gold`
- `switching_net_value_add`

### Core questions
- Does the system win because it switches well?
- Or because it mostly stays in GOLD?
- Are BTC entries additive or mainly a drag?

---

## 8) Deliverable 5 - Standard Summary Table

Codex should summarize results in one compact table:

| Window | Model | Total Expectancy | GOLD Leg | BTC Leg | vs GOLD-only |
|---|---:|---:|---:|---:|---:|
| 180d | Model A | ... | ... | ... | ... |
| 180d | Model B | ... | ... | ... | ... |
| 180d | GOLD-only | ... | ... | n/a | baseline |
| 365d | Model A | ... | ... | ... | ... |
| 365d | Model B | ... | ... | ... | ... |
| 365d | GOLD-only | ... | ... | n/a | baseline |

This table is mandatory in the final summary.

---

## 9) Deliverable 6 - Round 3 Decision Memo

### Goal
Produce one short memo answering:
1. Is GOLD-only better than Model A?
2. Is Model B better than GOLD-only?
3. Does BTC leg add measurable value?
4. What should happen next?

### Output file
- `s4_phase_0_5_round_3_decision_memo.md`

---

## 10) Windows to Run

Round 3 must run on both windows:
- 180 days
- 365 days

### Important caveat
If usable rows are fewer than calendar days due to feed availability, report both:
- requested window days
- actual usable rows

---

## 11) Required Commands

```bash
./venv/bin/python scripts/s4_phase_0_5_analysis.py --days 180 --output-dir log
./venv/bin/python scripts/s4_phase_0_5_analysis.py --days 365 --output-dir log
```

If additional flags are needed for GOLD-only baseline or regime comparison, document them clearly.

---

## 12) Metrics Definitions (Locked)

### Expectancy formula
`expectancy_pct = win_rate * avg_win_pct + (1 - win_rate) * avg_loss_pct`

### Premature exit
A BTC exit is premature if either condition holds:
- forward BTC/XAU return >= 3% within 5 days
- re-entry into BTC occurs within 3 days

### BTC leg value add
BTC leg value add is positive if:
- `(model_total_expectancy - gold_only_expectancy) > 0`

### GOLD-only baseline
A synthetic no-switch model that holds GOLD continuously through the full window.

---

## 13) Pass / Fail Criteria

### GOLD-only baseline outcome
- If GOLD-only >= Model A: BTC leg is not clearly adding value in current sample
- If Model B > GOLD-only: neutral filter remains a promising candidate
- If both Model A and Model B <= GOLD-only: current switching logic may not be justified in this regime sample

### BTC leg value-add outcome
- PASS: BTC leg improves total expectancy over GOLD-only without large MAE/switching drag
- FAIL: BTC leg reduces total expectancy or mainly adds noise

---

## 14) What NOT to Do During Round 3

- Do not change production logic
- Do not enable Model B live
- Do not modify thresholds
- Do not add deceleration rule
- Do not redesign CDC
- Do not optimize from current sample alone

Round 3 is analysis-only.

---

## 15) Expected Interpretation Guidelines

- If GOLD-only dominates: edge is mostly a GOLD-hold effect in this sample
- If Model B beats GOLD-only and Model A: neutral-state may have real filter value
- If Model A and Model B both beat GOLD-only: switching still adds value
- If 180d and 365d differ sharply: sample instability is high; collect more data

---

## 16) Core Questions Round 3 Must Answer

1. Is S4 better than simply holding GOLD?
2. Is BTC leg an alpha source or noise source?
3. Does Model B outperform because of better timing, or because it stays in GOLD longer?
4. Should BTC leg remain part of architecture in future phases?

---

## 17) Final Instruction to Codex

Please run Round 3 as measurement-only. Add a GOLD-only baseline model and compare it against Model A (CDC-only) and Model B (CDC + neutral filter) over both 180d and 365d windows. Export regime-by-model comparison, BTC leg value-add analysis, hold-vs-flip attribution, and a short decision memo. The goal is to determine whether BTC leg still adds value or whether the current system edge is mostly explained by holding GOLD through the sampled regime.

---

## 18) Bottom Line

Round 3 is not about improving production logic.  
It is about answering this with evidence:

> Does switching into BTC still justify itself, or is the current edge mostly a GOLD-hold effect?

