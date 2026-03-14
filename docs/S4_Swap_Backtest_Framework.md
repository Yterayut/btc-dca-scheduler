S4 Swap Timing Backtest

Framework & Execution Plan for Codex

v1.0 | 2026-03-09 | Companion to S4_DCA_Swap_Rules_Spec.docx

Backtest all 4 windows: GOLD-dominant + 3 BTC-bull periods

1. Objective

Validate the swap timing rules defined in S4_DCA_Swap_Rules_Spec.docx against historical data before any production deployment. The goal is to answer three questions:

Do the 5-gate swap rules (XAU-to-BTC) successfully block false entries that hurt Model A in Phase 0.5?

Do the 3-gate swap rules (BTC-to-XAU) exit BTC positions at reasonable times without excessive lag?

What is the optimal parameter combination across different regime types?

2. Data Windows

Each window tests a different regime and stresses different parts of the swap logic.

ID

Period

Source

Regime

Primary Test

W1

2025-05-13 to 2026-03-07

OKX daily

GOLD-dominant

Swap gate should BLOCK most BTC entries

W2

2016-01-01 to 2018-01-31

FRED/LBMA

BTC-bull (strong)

Swap gate should ALLOW BTC entries with acceptable lag

W3

2020-04-01 to 2021-11-30

FRED/LBMA

BTC-bull (momentum)

Test if 5-day confirm is too conservative

W4

2023-01-01 to 2025-12-31

FRED/LBMA

Mixed/recent

Robustness across mixed regime changes

2.1 Pass/Fail Criteria Per Window

Window

Pass Condition

Fail Condition

W1 GOLD-dom

Swap gate blocks >=80% of BTC entries that Model A took; total return within 3% of GOLD-only

Swap gate allows false entries that lose >5% each; total return >10% worse than GOLD-only

W2 BTC-bull

Captures >=60% of BTC-only return; swap lag <10 days from CDC flip

Captures <30% of BTC-only return; swap lag >20 days

W3 BTC-momentum

Outperforms Model A total return; swap count reasonable (<10 per year)

Underperforms both Model A and Model B; excessive swaps (>15/year)

W4 Mixed

Outperforms GOLD-only when BTC is winning; limits loss to <10% when wrong

Drawdown >30% from swap timing errors

3. Models to Compare

Model

DCA Logic

Swap Logic

Purpose

Model E

CDC-directed

No swap (accumulate only)

DCA-only baseline: pure DCA value without swap risk

Model F

CDC-directed

Full 100% swap with 5-gate/3-gate

Full spec implementation from Spec doc

Model G

CDC-directed

Staged 30/30/40 partial swap

Test if partial swap reduces false-signal damage

Model A (legacy)

N/A

CDC-only execution (current prod)

Comparison: how much do new swap gates improve?

Model B (legacy)

N/A

CDC + neutral filter

Comparison: is Model F better than Model B?

GOLD-only

Always XAU

None

Baseline

BTC-only

Always BTC

None

Baseline

3.1 DCA Simulation Assumptions

DCA amount: 1 unit per period (normalized, not dollar-denominated)

DCA frequency: Weekly (every 7 days on the data timeline)

DCA buys at daily close price of the target asset

Accumulated units track separately for BTC and XAU

Portfolio value = (BTC_units * BTC_price) + (XAU_units * XAU_price) at any point

3.2 Swap Simulation Assumptions

Swap executes at daily close price on the day all gates pass

Swap converts 100% of one asset to the other (Model F) or staged % (Model G)

No slippage or transaction fees in base simulation (add as sensitivity test)

Cooldown period starts from swap execution date

If partial swap is aborted, hold mixed allocation until next clean signal

4. Swap Gate Implementation (Pseudocode)

4.1 Core Swap Evaluator

class SwapConfig:

swap_btc_confirm_days: int = 5

swap_xau_confirm_days: int = 3

swap_btc_slope_min: float = 1.0     # %/day

swap_xau_slope_max: float = -0.5    # %/day

swap_btc_gap_max: float = 3.0       # %

swap_cooldown_days: int = 14

swap_require_neutral: bool = True

partial_enabled: bool = False

partial_stages: list = [0.30, 0.30, 0.40]

partial_delays: list = [0, 3, 5]  # days after swap signal

def evaluate_swap(row, cdc_history, config, current_holding, last_swap_date):

"""

Evaluate whether to swap on this day.

Returns: (action, reason)

action: HOLD | SWAP_TO_BTC | SWAP_TO_XAU | PARTIAL_BTC_N | PARTIAL_XAU_N

"""

days_since_swap = (row.date - last_swap_date).days if last_swap_date else 999

if days_since_swap < config.swap_cooldown_days:

return "HOLD", f"cooldown: {days_since_swap}/{config.swap_cooldown_days}"

if current_holding == "XAU":

return _eval_xau_to_btc(row, cdc_history, config)

elif current_holding == "BTC":

return _eval_btc_to_xau(row, cdc_history, config)

return "HOLD", "unknown_state"

4.2 XAU-to-BTC Gate (5 conditions)

def _eval_xau_to_btc(row, cdc_history, config):

# Gate 1: CDC persistence

window = cdc_history[-config.swap_btc_confirm_days:]

if len(window) < config.swap_btc_confirm_days:

return "HOLD", "insufficient_cdc_history"

if not all(s == "up" for s in window):

return "HOLD", f"cdc_not_persistent: need {config.swap_btc_confirm_days}d"

# Gate 2: Neutral-state (optional)

if config.swap_require_neutral and row.neutral_state != "btc_signal":

return "HOLD", f"neutral={row.neutral_state}, need btc_signal"

# Gate 3: Slope minimum

if row.slope_pct < config.swap_btc_slope_min:

return "HOLD", f"slope={row.slope_pct:.2f} < {config.swap_btc_slope_min}"

# Gate 4: Gap maximum

if row.gap_pct > config.swap_btc_gap_max:

return "HOLD", f"gap={row.gap_pct:.2f} > {config.swap_btc_gap_max}"

# All gates passed

return "SWAP_TO_BTC", "all_5_gates_passed"

4.3 BTC-to-XAU Gate (3 conditions)

def _eval_btc_to_xau(row, cdc_history, config):

# Gate 1: CDC persistence

window = cdc_history[-config.swap_xau_confirm_days:]

if len(window) < config.swap_xau_confirm_days:

return "HOLD", "insufficient_cdc_history"

if not all(s == "down" for s in window):

return "HOLD", f"cdc_not_persistent: need {config.swap_xau_confirm_days}d"

# Gate 2: Slope weakness

if row.slope_pct > config.swap_xau_slope_max:

return "HOLD", f"slope={row.slope_pct:.2f} > {config.swap_xau_slope_max}"

# All gates passed

return "SWAP_TO_XAU", "all_3_gates_passed"

5. Parameter Sweep Design

5.1 Parameters to Sweep

Parameter

Values to Test

Total

SWAP_BTC_CONFIRM_DAYS

3, 5, 7, 10

4

SWAP_BTC_SLOPE_MIN

0.5, 1.0, 1.5, 2.0

4

SWAP_BTC_GAP_MAX

2.0, 3.0, 4.0, 5.0

4

SWAP_COOLDOWN_DAYS

7, 14, 21

3

SWAP_XAU_CONFIRM_DAYS

2, 3, 5

3

SWAP_XAU_SLOPE_MAX

-1.0, -0.5, 0.0

3

SWAP_REQUIRE_NEUTRAL

True, False

2

Total combinations: 4 x 4 x 4 x 3 x 3 x 3 x 2 = 3,456 configurations per window. With 4 windows = 13,824 total runs.

5.2 Sweep Reduction Strategy

Full sweep is feasible but can be phased:

Phase A: Fix XAU exit params at default (3d confirm, -0.5 slope). Sweep only BTC entry params (4x4x4x3x2 = 384 per window). Identify top-10 configs per window.

Phase B: Take top-10 BTC entry configs. Sweep XAU exit params (3x3 = 9 each = 90 per window). Find global top-5.

Phase C: Run top-5 configs through all 4 windows. Pick config that performs best across all regimes (not just one).

5.3 Anti-Overfit Rules

No config is valid unless it passes in at least 3 of 4 windows

If best config differs by window, use the one that minimizes worst-case loss (minimax)

Final config must be tested on a 60-day hold-out period (most recent data) not used in sweep

Report both in-sample and hold-out performance for transparency

6. Metrics & Output Schema

6.1 Per-Config Metrics

Metric

Formula / Description

Target

total_return_pct

Final portfolio value / initial - 1

Maximize

max_drawdown_pct

Worst peak-to-trough decline

Minimize (< -20%)

swap_count

Number of completed swaps

< 10/year

swap_win_rate

Swaps where return improved vs no-swap / total swaps

> 50%

swap_avg_lag_days

Days from CDC flip to swap execution

< 10 for BTC entry

false_swap_count

Swaps reversed within 30 days

< 2/year

btc_capture_pct

Model return / BTC-only return (in BTC-bull windows)

> 60%

gold_proximity_pct

1 - abs(model return - GOLD-only return)/GOLD-only return (in GOLD window)

> 95%

dca_accuracy_pct

DCA periods where target matched best asset / total periods

> 70%

expectancy_pct

win_rate * avg_win + (1-win_rate) * avg_loss per swap event

> 0

6.2 Per-Swap Event Log

Each swap event should be logged with the following fields:

{

"swap_id": 1,

"date": "2025-07-05",

"direction": "XAU_TO_BTC",

"trigger_reason": "all_5_gates_passed",

"gates": {

"cdc_confirm_days": 5,

"neutral_state": "btc_signal",

"slope_pct": 1.23,

"gap_pct": 2.45,

"cooldown_days_elapsed": 22

},

"entry_ratio": 32.5,

"cdc_flip_date": "2025-06-30",

"swap_lag_days": 5,

"holding_pct_before": {"XAU": 100, "BTC": 0},

"holding_pct_after": {"XAU": 0, "BTC": 100},

"dca_units_at_swap": {"XAU": 12.5, "BTC": 0.003},

"exit_date": "2025-08-06",

"exit_reason": "swap_to_xau",

"duration_days": 32,

"return_pct": 5.8,

"peak_return_pct": 10.1,

"mae_pct": -0.87,

"quality_label": "premature_exit",

"was_reversed": false

}

6.3 Output Files

File

Content

Format

s4_swap_backtest_events_{window}.csv

Per-swap event log for each window

CSV

s4_swap_backtest_summary_{window}.csv

Per-config summary metrics

CSV

s4_swap_param_sweep_results.csv

All configs x all windows flattened

CSV

s4_swap_backtest_top_configs.json

Top-5 configs with cross-window metrics

JSON

s4_swap_backtest_holdout.json

Hold-out validation of top configs

JSON

s4_swap_backtest_decision_memo.md

Human-readable decision summary

Markdown

7. DCA Backtest Logic

7.1 DCA Portfolio Simulation

class DCAPortfolio:

def __init__(self):

self.btc_units = 0.0

self.xau_units = 0.0

self.dca_history = []  # [{date, target, amount, price}]

self.swap_history = []

def dca_buy(self, date, target, btc_price, xau_price, amount=1.0):

if target == "BTC":

self.btc_units += amount / btc_price

else:

self.xau_units += amount / xau_price

self.dca_history.append({

"date": date, "target": target,

"amount": amount, "price": btc_price if target == "BTC" else xau_price

})

def swap(self, date, direction, btc_price, xau_price, pct=1.0):

if direction == "XAU_TO_BTC":

xau_to_sell = self.xau_units * pct

value = xau_to_sell * xau_price

self.xau_units -= xau_to_sell

self.btc_units += value / btc_price

elif direction == "BTC_TO_XAU":

btc_to_sell = self.btc_units * pct

value = btc_to_sell * btc_price

self.btc_units -= btc_to_sell

self.xau_units += value / xau_price

self.swap_history.append({

"date": date, "direction": direction, "pct": pct

})

def value(self, btc_price, xau_price):

return self.btc_units * btc_price + self.xau_units * xau_price

7.2 Combined DCA + Swap Simulation Loop

def run_backtest(rows, config, dca_freq_days=7):

portfolio = DCAPortfolio()

holding = "XAU"         # start in XAU

cdc_history = []

last_swap_date = None

last_dca_date = None

swap_events = []

daily_values = []

for i, row in enumerate(rows):

cdc_history.append(row.cdc_status)

# --- DCA ---

if last_dca_date is None or (row.date - last_dca_date).days >= dca_freq_days:

dca_target = get_dca_target(row.cdc_status)  # from spec

portfolio.dca_buy(row.date, dca_target, row.btc_price, row.xau_price)

last_dca_date = row.date

# --- Swap evaluation ---

action, reason = evaluate_swap(

row, cdc_history, config, holding, last_swap_date

)

if action == "SWAP_TO_BTC" and holding == "XAU":

portfolio.swap(row.date, "XAU_TO_BTC", row.btc_price, row.xau_price)

holding = "BTC"

last_swap_date = row.date

swap_events.append(create_swap_event(row, action, reason))

elif action == "SWAP_TO_XAU" and holding == "BTC":

portfolio.swap(row.date, "BTC_TO_XAU", row.btc_price, row.xau_price)

holding = "XAU"

last_swap_date = row.date

swap_events.append(create_swap_event(row, action, reason))

daily_values.append({

"date": row.date,

"value": portfolio.value(row.btc_price, row.xau_price),

"holding": holding

})

return daily_values, swap_events, portfolio

8. Execution Plan for Codex

8.1 Step-by-Step Commands

Step

Action

Command / Deliverable

1

Implement swap gate + DCA portfolio in analysis script

Update scripts/s4_phase_0_5_analysis.py or create scripts/s4_swap_backtest.py

2

Run W1 (GOLD-dom) with default config

--start 2025-05-13 --end 2026-03-07 --data-source okx

3

Run W2 (BTC-bull 2016-18)

--start 2016-01-01 --end 2018-01-31 --data-source fred_lbma

4

Run W3 (BTC-bull 2020-21)

--start 2020-04-01 --end 2021-11-30 --data-source fred_lbma

5

Run W4 (Mixed recent)

--start 2023-01-01 --end 2025-12-31 --data-source fred_lbma

6

Run Phase A param sweep (384 configs x 4 windows)

Output: s4_swap_param_sweep_results.csv

7

Identify top-10 configs, run Phase B sweep

Output: s4_swap_backtest_top_configs.json

8

Hold-out validation on 60d most recent

Output: s4_swap_backtest_holdout.json

9

Generate decision memo

Output: s4_swap_backtest_decision_memo.md

8.2 Expected Runtime

Phase A: 384 configs x 4 windows x ~300-750 rows each. Estimated <5 minutes total on a single core. Phase B: 90 configs x 4 windows. Estimated <1 minute. Full sweep (3,456 x 4 = 13,824): ~15 minutes worst case.

8.3 Data Requirements

Each row in the data feed must include:

Field

Source

Required For

date

OKX or FRED

Timeline

btc_price

OKX or FRED (CBBTCUSD)

Portfolio valuation + DCA

xau_price

OKX or LBMA (gold_pm)

Portfolio valuation + DCA

ratio

btc_price / xau_price

EMA + CDC + neutral-state calculation

cdc_status

cdc_status_from_series()

DCA direction + swap gate 1

neutral_state

calculate_state()

Swap gate 2 (BTC entry)

ema_gap_pct

calculate_state() metrics

Swap gate 4

slope_pct

calculate_state() metrics

Swap gates 3 & 5

9. Decision Criteria After Backtest

9.1 Scenario Matrix

Result

Action

Model F beats Model A in all 4 windows AND passes all window criteria

Promote swap rules to shadow mode (log-only) in production for 30 days

Model F beats Model A in 3/4 windows but fails 1 window badly

Investigate failing window. May need regime-specific params.

Model F beats Model A only in GOLD window (blocks false entries) but lags in BTC windows

Consider DCA-only (Model E) as simpler alternative with less risk

Model E (DCA-only) performs within 5% of Model F across all windows

Adopt DCA-only. Swap complexity not justified.

No config passes in 3+ windows (overfit risk)

Go back to design. Current spec may need fundamental changes.

9.2 Promotion Path

Backtest complete with chosen config

Shadow mode: log swap decisions for 30 days without executing

Compare shadow decisions to actual market outcomes

If shadow performance matches backtest expectations: promote to execution

DCA direction can be promoted immediately (low risk)

9.3 Rollback Plan

If swap rules underperform in shadow mode:

Revert to current CDC-only execution (Model A)

Keep DCA direction active (low risk, validated separately)

Collect 60 more days of data and re-evaluate

10. Appendix: Quick Reference for Codex

10.1 Default Config (Starting Point)

DEFAULT_SWAP_CONFIG = SwapConfig(

swap_btc_confirm_days=5,

swap_xau_confirm_days=3,

swap_btc_slope_min=1.0,

swap_xau_slope_max=-0.5,

swap_btc_gap_max=3.0,

swap_cooldown_days=14,

swap_require_neutral=True,

partial_enabled=False,

)

10.2 Sweep Grid (Phase A)

SWEEP_PHASE_A = {

"swap_btc_confirm_days": [3, 5, 7, 10],

"swap_btc_slope_min": [0.5, 1.0, 1.5, 2.0],

"swap_btc_gap_max": [2.0, 3.0, 4.0, 5.0],

"swap_cooldown_days": [7, 14, 21],

"swap_require_neutral": [True, False],

# Fix XAU exit at defaults:

"swap_xau_confirm_days": [3],

"swap_xau_slope_max": [-0.5],

}

# Total: 4 * 4 * 4 * 3 * 2 = 384 configs

10.3 File References

File

Purpose

S4_DCA_Swap_Rules_Spec.docx

Logic specification (companion document)

scripts/s4_phase_0_5_analysis.py

Existing analysis script (extend or create new)

strategies/s4_utils.py

CDC implementation (cdc_status_from_series)

strategies/s4_neutral_zone.py

Neutral-state classifier (calculate_state)

main.py

Production execution (run_s4_tick)

docs/s4-neutral-zone-spec.md

Original S4 spec (thresholds marked Tentative)

docs/s4-phase-0.5-round-5-results.md

Phase 0.5 final results

End of Framework Document
