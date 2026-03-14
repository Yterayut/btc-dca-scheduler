S4 Neutral Zone

DCA Direction + Swap Rules

Technical Specification for Codex Implementation

Version: 1.0  |  Date: 2026-03-09  |  Status: Draft

Author: KiRiYaH + Claude Opus 4.6

Data Source: BTC/XAU ratio (OKX daily) + CDC Action Zone + Neutral-State Classifier

1. Executive Summary

1.1 System Identity

S4 is a risk manager that rotates between BTC and XAU (gold) based on the BTC/XAU ratio trend. Phase 0.5 analysis across 5 backtesting windows (299 days GOLD-dominant + 3 BTC-bull windows spanning 2016-2025) established that S4 is not an alpha generator against pure-hold of the correct asset. Instead, S4 provides insurance against being on the wrong side of a regime shift.

1.2 Two Distinct Actions

This spec separates S4 into two actions with fundamentally different risk profiles:

Action

Description

Risk Level

If Signal Wrong

DCA Direction

New money each period buys whichever asset the trend favors

Low

Bought slightly worse asset, small cost

Swap (Full Rotation)

Sell entire holding of one asset, buy the other

High

Sell at bad price + buy at bad price = double loss

1.3 Key Evidence from Phase 0.5

GOLD-only baseline returned +148.3% over 299 days vs Model A (CDC execution) +133.8%

BTC signal median duration: 4 days, win rate: 33% (1/3), avg loss > avg win

Switching net value-add was negative in every backtesting window tested

Model B (CDC + neutral filter) outperformed Model A in 3 of 4 windows

In BTC-bull regimes, both models strongly beat GOLD-only (+81% to +1344%)

2. Architecture Overview

2.1 Signal Sources (Existing)

Module

Purpose

Current Role

CDC Action Zone

EMA12/EMA26 crossover with buy/sell trigger tracking

Execution (production)

Neutral-State Classifier

Gap + slope analysis with 4 states (gold_signal, weak_signal, neutral_zone, btc_signal)

Log-only (Phase 1)

S4_CONFIRM_DAYS=2

Requires CDC status to persist for 2 consecutive days

Execution (production)

2.2 New Decision Layer

This spec adds a decision layer on top of existing signals. No changes to CDC or neutral-state calculation logic.

Signal Sources (unchanged)

CDC Action Zone  ──> cdc_status: up | down

Neutral-State    ──> state: gold_signal | weak_signal | neutral_zone | btc_signal

Decision Layer (new)

┌─────────────────────────────────────────────┐

│  DCA Router                                 │

│  Input: cdc_status                          │

│  Output: dca_target = BTC | XAU             │

│  Speed: Immediate on CDC flip               │

└─────────────────────────────────────────────┘

┌─────────────────────────────────────────────┐

│  Swap Gate                                  │

│  Input: cdc_status + neutral_state + slope  │

│         + gap + confirm_days + holding      │

│  Output: swap_action = HOLD | SWAP_TO_BTC   │

│          | SWAP_TO_XAU | PARTIAL_SWAP       │

│  Speed: Multi-day confirmation required     │

└─────────────────────────────────────────────┘

3. DCA Direction Rules

3.1 Logic

DCA direction follows CDC status directly. This is low-risk because each DCA contribution is a small fraction of total portfolio. Even if the signal is wrong, the cost is marginal.

def get_dca_target(cdc_status: str) -> str:

"""

Determine which asset to buy with new DCA contribution.

Simple and fast - follows CDC signal immediately.

"""

if cdc_status == "up":

return "BTC"

else:

return "XAU"

3.2 Parameters

Parameter

Value

Rationale

Trigger

CDC status flip (up/down)

Simple, already validated in production

Delay

None (immediate)

DCA amounts are small, false signal cost is low

Confirmation

None required

S4_CONFIRM_DAYS=2 already built into CDC pipeline

Frequency

Follows existing DCA schedule

Weekly or monthly per user preference

3.3 Expected Behavior

During GOLD-dominant regime (current): CDC stays "down" for extended periods (28+ consecutive days observed). All DCA contributions buy XAU. During BTC-bull regime: CDC flips to "up" and DCA switches to BTC. May flip back quickly (median BTC signal is 4 days), but DCA frequency (weekly/monthly) naturally filters noise.

Key insight: DCA frequency acts as a natural filter. If CDC flips to "up" for 3 days then back to "down", a weekly DCA schedule might not even execute during that window. This is a feature, not a bug.

4. Swap Rules (Full Rotation)

4.1 Design Principle

Swaps require a much higher confirmation bar than DCA because the cost of being wrong is severe. Phase 0.5 proved that switching net value-add was negative in every window tested. Therefore, swaps must be treated as rare, high-conviction events.

4.2 Asymmetric Swap Logic

Swap rules are deliberately asymmetric because the two directions have very different characteristics:

Direction

Frequency

Confidence Needed

Reason

XAU to BTC

Rare

Very High

BTC signals are short (median 4d), win rate 33%, false starts common

BTC to XAU

Less Rare

High

GOLD signals are long (median 18d), GOLD trends are more persistent

4.2.1 Swap: XAU to BTC (Defensive to Aggressive)

This is the highest-risk swap. Phase 0.5 showed 2 of 3 BTC entries were losses (one false_start, one inconclusive). The swap gate must be highly selective.

Required conditions (ALL must be true):

CDC status = "up" for at least SWAP_BTC_CONFIRM_DAYS consecutive days (default: 5)

Neutral-state = "btc_signal" (not weak_signal or neutral_zone)

Slope > SWAP_BTC_SLOPE_MIN (default: 1.0%/day) indicating strong momentum

Gap < SWAP_BTC_GAP_MAX (default: 3.0%) indicating EMA convergence

No swap executed in the last SWAP_COOLDOWN_DAYS (default: 14 days)

def evaluate_swap_xau_to_btc(

cdc_history: list[str],        # last N days of cdc_status

neutral_state: str,

slope_pct: float,

gap_pct: float,

days_since_last_swap: int,

config: SwapConfig

) -> SwapDecision:

# Gate 1: CDC persistence

recent_cdc = cdc_history[-config.swap_btc_confirm_days:]

if not all(s == "up" for s in recent_cdc):

return SwapDecision.HOLD

# Gate 2: Neutral-state confirmation

if neutral_state != "btc_signal":

return SwapDecision.HOLD

# Gate 3: Momentum strength

if slope_pct < config.swap_btc_slope_min:

return SwapDecision.HOLD

# Gate 4: EMA proximity

if gap_pct > config.swap_btc_gap_max:

return SwapDecision.HOLD

# Gate 5: Cooldown

if days_since_last_swap < config.swap_cooldown_days:

return SwapDecision.HOLD

# All gates passed

return SwapDecision.SWAP_TO_BTC

4.2.2 Swap: BTC to XAU (Aggressive to Defensive)

This swap is less risky because GOLD trends are more persistent. However, it still requires confirmation to avoid whipsaw during brief BTC corrections within a bull run.

Required conditions (ALL must be true):

CDC status = "down" for at least SWAP_XAU_CONFIRM_DAYS consecutive days (default: 3)

Slope < SWAP_XAU_SLOPE_MAX (default: -0.5%/day) indicating weakening momentum

No swap executed in the last SWAP_COOLDOWN_DAYS (default: 14 days)

def evaluate_swap_btc_to_xau(

cdc_history: list[str],

slope_pct: float,

days_since_last_swap: int,

config: SwapConfig

) -> SwapDecision:

# Gate 1: CDC persistence (shorter than BTC entry)

recent_cdc = cdc_history[-config.swap_xau_confirm_days:]

if not all(s == "down" for s in recent_cdc):

return SwapDecision.HOLD

# Gate 2: Momentum weakness

if slope_pct > config.swap_xau_slope_max:

return SwapDecision.HOLD

# Gate 3: Cooldown

if days_since_last_swap < config.swap_cooldown_days:

return SwapDecision.HOLD

return SwapDecision.SWAP_TO_XAU

Note: BTC-to-XAU swap intentionally has a lower bar (3 days vs 5 days confirm, no neutral-state gate). This is because exiting BTC early is less costly than entering BTC early. Phase 0.5 showed that BTC exits were generally correct or inconclusive, not premature (except 1 of 3 events).

5. Partial Swap Strategy (Risk Mitigation)

5.1 Rationale

Instead of swapping 100% of holdings at once, a staged approach reduces the impact of false signals. If the signal reverses before full swap completes, losses are limited to the swapped portion only.

5.2 Staged Swap Schedule

Stage

Trigger

Action

Cumulative Exposure

Stage 0

Swap signal triggered

Swap 30% of current holding

30% new asset / 70% old

Stage 1

Signal persists +3 days after Stage 0

Swap additional 30%

60% new / 40% old

Stage 2

Signal persists +5 days after Stage 1

Swap remaining 40%

100% new asset

Abort

Signal reverses before Stage 2

Hold current mix, do not swap back immediately

Partial allocation

5.3 Abort Handling

If the signal reverses mid-swap (e.g., after Stage 0 or Stage 1), DO NOT automatically swap back. Instead, hold the partial allocation and wait for a new clean signal. This prevents churn from rapid flip-flop signals.

Abort cooldown: After an aborted swap, the cooldown period resets. No new swap can be initiated for SWAP_COOLDOWN_DAYS from the abort date.

5.4 Implementation Note

Partial swap is optional for Phase 1. The system can launch with full 100% swaps and add partial logic later. The main protection comes from the multi-gate confirmation, not from partial sizing. However, partial swap is strongly recommended for production deployment.

6. Configuration Parameters

6.1 DCA Parameters

Parameter

Default

Range

Description

DCA_FOLLOW_CDC

true

true/false

Whether DCA direction follows CDC status

DCA_FREQUENCY

weekly

daily/weekly/monthly

How often DCA executes (external to S4)

6.2 Swap Parameters

Parameter

Default

Range

Description

SWAP_BTC_CONFIRM_DAYS

5

3-10

CDC=up days required to swap XAU->BTC

SWAP_XAU_CONFIRM_DAYS

3

2-7

CDC=down days required to swap BTC->XAU

SWAP_BTC_SLOPE_MIN

1.0

0.5-3.0

Min slope %/day for XAU->BTC swap

SWAP_XAU_SLOPE_MAX

-0.5

-2.0 to 0.0

Max slope %/day for BTC->XAU swap

SWAP_BTC_GAP_MAX

3.0

2.0-6.0

Max EMA gap % for XAU->BTC swap

SWAP_COOLDOWN_DAYS

14

7-30

Min days between any two swaps

SWAP_REQUIRE_NEUTRAL

true

true/false

Require neutral=btc_signal for XAU->BTC

6.3 Partial Swap Parameters

Parameter

Default

Range

Description

PARTIAL_SWAP_ENABLED

false

true/false

Enable staged swap (Phase 2)

PARTIAL_STAGE_0_PCT

30

20-50

% to swap at Stage 0

PARTIAL_STAGE_1_PCT

30

20-40

% to swap at Stage 1

PARTIAL_STAGE_1_DELAY

3

2-5

Days after Stage 0 for Stage 1

PARTIAL_STAGE_2_DELAY

5

3-7

Days after Stage 1 for Stage 2

6.4 Parameter Origin

Important: All default values are derived from Phase 0.5 analysis (median BTC duration=4d, win rate=33%, slope patterns) but are NOT optimized. They should be treated as starting points for backtesting. The SWAP_BTC_CONFIRM_DAYS=5 is set higher than CDC default (2) specifically because Phase 0.5 showed that short BTC signals (2-4 days) were frequently false starts.

7. Combined State Machine

7.1 States

State

DCA Target

Swap Status

Description

HOLD_XAU

XAU

No swap pending

Default state. Holding XAU, DCA into XAU

DCA_FLIP_TO_BTC

BTC

Evaluating swap

CDC flipped up. DCA now buys BTC. Swap gate evaluating.

SWAP_PENDING_BTC

BTC

Confirming...

Swap gates partially passed. Waiting for full confirmation.

HOLD_BTC

BTC

No swap pending

Fully rotated to BTC. DCA into BTC.

DCA_FLIP_TO_XAU

XAU

Evaluating swap

CDC flipped down. DCA now buys XAU. Swap gate evaluating.

SWAP_PENDING_XAU

XAU

Confirming...

Swap gates partially passed. Waiting for full confirmation.

7.2 Transitions

HOLD_XAU ──[CDC flips up]──> DCA_FLIP_TO_BTC

DCA immediately switches to BTC

Swap gate starts evaluating

DCA_FLIP_TO_BTC ──[all 5 swap gates pass]──> SWAP_PENDING_BTC

Execute swap (or Stage 0 if partial)

SWAP_PENDING_BTC ──[swap complete]──> HOLD_BTC

Fully rotated to BTC

DCA_FLIP_TO_BTC ──[CDC flips back down]──> HOLD_XAU

False signal. DCA reverts to XAU.

No swap occurred. No damage beyond DCA timing.

HOLD_BTC ──[CDC flips down]──> DCA_FLIP_TO_XAU

DCA immediately switches to XAU

Swap gate starts evaluating

DCA_FLIP_TO_XAU ──[3 swap gates pass]──> SWAP_PENDING_XAU

Execute swap (or Stage 0 if partial)

SWAP_PENDING_XAU ──[swap complete]──> HOLD_XAU

Fully rotated to XAU

DCA_FLIP_TO_XAU ──[CDC flips back up]──> HOLD_BTC

Brief correction, not trend change.

DCA reverts to BTC. No swap occurred.

7.3 Key Design Properties

DCA flips immediately on CDC change (fast, low-cost)

Swap requires multi-day confirmation (slow, high-cost)

CDC flip-back before swap confirmation = no damage (only DCA timing affected)

Cooldown prevents rapid swap-swap sequences

State machine is fully deterministic given inputs

8. Backtesting Requirements

8.1 Test Windows

Window

Period

Regime

Purpose

W1

2025-05-13 to 2026-03-07

GOLD-dominant

Current regime validation

W2

2016-01-01 to 2018-01-31

BTC-bull

Strong BTC uptrend

W3

2020-04-01 to 2021-11-30

BTC-bull

BTC institutional adoption

W4

2023-01-01 to 2025-12-31

Mixed/BTC-bull

Recent market conditions

W5

2018-01-01 to 2020-03-31

BTC-bear/GOLD

BTC crash + recovery

8.2 Models to Compare

Model

DCA Logic

Swap Logic

Model E: DCA-only

CDC-directed

No swap ever (accumulate only)

Model F: DCA + Full Swap

CDC-directed

Full 100% swap with 5-gate confirmation

Model G: DCA + Partial Swap

CDC-directed

Staged 30/30/40 swap with 5-gate confirmation

Baseline: GOLD-only

Always XAU

No swap

Baseline: BTC-only

Always BTC

No swap

8.3 Metrics

Total return (including DCA contributions)

Max drawdown

Number of swaps executed

Swap win rate (did swap improve or worsen return?)

Swap timing: days from optimal swap point

False swap rate (swaps that were reversed within COOLDOWN period)

DCA allocation accuracy (% of DCA that went to correct asset)

8.4 Threshold Sensitivity

Run parameter sweep for each window:

SWAP_BTC_CONFIRM_DAYS: [3, 5, 7, 10]

SWAP_BTC_SLOPE_MIN: [0.5, 1.0, 1.5, 2.0]

SWAP_BTC_GAP_MAX: [2.0, 3.0, 4.0, 5.0]

SWAP_COOLDOWN_DAYS: [7, 14, 21, 30]

9. Expected Outcomes by Scenario

9.1 Scenario A: Current Regime Continues (GOLD dominant)

DCA: Continuously buys XAU (correct)

Swap: Rarely triggered because CDC stays "down" and swap gates block BTC entry

Expected: Similar to GOLD-only performance with minimal swap friction

9.2 Scenario B: BTC Trend Reversal (Bull Run Starts)

DCA: Switches to BTC within 1 DCA cycle after CDC flips (fast response)

Swap: Triggered after 5+ days of sustained CDC="up" + neutral=btc_signal (delayed but confirmed)

Expected: Captures most of BTC uptrend but misses first 5-7 days (acceptable trade-off)

9.3 Scenario C: False BTC Signal (Brief Bounce in Downtrend)

DCA: May buy 1 BTC contribution during the bounce (small cost)

Swap: NOT triggered because CDC flips back before 5-day confirmation

Expected: Minimal damage. Only 1 DCA contribution to BTC instead of XAU.

9.4 Scenario D: Choppy Market (Frequent Regime Changes)

DCA: Alternates between BTC and XAU (neutral outcome on average)

Swap: Cooldown period (14 days) prevents rapid swaps. System holds current allocation.

Expected: Higher friction than pure-hold but protected from large one-sided losses.

10. Implementation Roadmap

10.1 Phase 1a: DCA Direction (Immediate)

Add get_dca_target() function to strategies/s4_utils.py

Wire into existing DCA execution flow

Log DCA direction decisions to s4_neutral_zone_eod table

Deploy to production (low risk, reversible)

10.2 Phase 1b: Swap Gate (After Backtest)

Implement SwapConfig dataclass with all parameters from Section 6

Implement evaluate_swap_xau_to_btc() and evaluate_swap_btc_to_xau()

Add swap state machine to run_s4_tick()

Deploy as LOG-ONLY first (shadow mode) for 30 days

Compare shadow decisions vs actual outcomes

Promote to execution after validation

10.3 Phase 2: Partial Swap (Optional)

Implement staged swap logic

Add abort handling

Backtest staged vs full swap across all windows

10.4 Success Criteria

Metric

Target

Measured Over

Swap win rate

> 50%

All windows combined

vs GOLD-only (GOLD regime)

Within -5% return

W1

vs BTC-only (BTC regime)

Within -30% return

W2, W3, W4

Max drawdown improvement

> 5% reduction vs pure-hold

All windows

False swap rate

< 20%

All windows combined

DCA allocation accuracy

> 70%

All windows combined

End of Specification
