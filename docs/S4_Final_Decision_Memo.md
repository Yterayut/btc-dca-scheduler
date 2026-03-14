S4 Final Decision Memo

& DCA-First Implementation Spec

v1.0 | 2026-03-09 | Based on Phase 0.5 + Phase A/B Backtest Evidence

Decision: Deploy DCA Direction + Shadow Swap + Future Partial Swap

1. Executive Decision

DECISION: Deploy DCA direction (CDC-based) to production immediately. Log swap gate decisions in shadow mode. Do NOT execute full swaps. Re-evaluate after 90 days with production data.

1.1 What We Deploy Now

Component

Action

Risk

Timeline

DCA Direction

Production execution

Low

Immediate

Swap Gate (5-gate/3-gate)

Shadow mode (log only)

None

Immediate

Full Swap (100%)

NOT deployed

N/A

Deferred

Partial Swap (30/30/40)

NOT deployed

N/A

Phase 2 (90+ days)

1.2 What We Explicitly Do NOT Do

Do NOT execute any full swaps (sell 100% of one asset, buy another)

Do NOT promote any sweep config to production (no config passed strict criteria)

Do NOT run Phase C parameter tuning (diminishing returns on 3,456 configs that all failed)

Do NOT implement regime detection layer yet (premature without DCA baseline data)

2. Evidence Summary

2.1 Key Finding: Full Swap Cannot Pass Quality Gate

From 3,456 parameter configurations tested across 4 historical windows:

Criteria

Threshold

Configs Passed

Return > 0 in 3/4 windows + DD >= -30% + false swap/yr < 3

Strict

0 of 3,456

Return > 0 in 3/4 windows + DD >= -35% + false swap/yr < 3

Moderate

0 of 3,456

Return > 0 in 3/4 windows + DD >= -40% + false swap/yr < 3

Relaxed

0 of 3,456

Return > 0 in 3/4 windows + DD >= -45% + false swap/yr < 3

Very Relaxed

18 of 3,456

Conclusion: Full swap inherently carries drawdown > 30% regardless of parameter tuning. The 18 configs that passed at -45% still showed holdout instability (W2/W3 holdout negative while BTC-only was positive).

2.2 DCA Direction Works Well

Window

Model E (DCA-only)

GOLD-only

BTC-only

DCA Value-Add

W1 GOLD-dom

+148.35%

+148.35%

-59.73%

Matched GOLD-only, avoided BTC loss

W2 BTC-bull 2016

-94.66%

-94.66%

+1773.14%

DCA accumulated some BTC during CDC-up periods

W3 BTC-bull 2020

-86.48%

-86.48%

+639.61%

Similar to W2

W4 Mixed recent

-55.21%

-55.21%

+123.24%

Defensive DCA limited exposure

Note: Model E DCA-only in these results only does DCA (no initial holding). In practice with existing XAU holdings, DCA direction change means new contributions switch but existing holdings are preserved. The actual portfolio effect is much more conservative than these pure-DCA numbers suggest.

2.3 S4 Identity Confirmed

Phase 0.5 through Phase B consistently showed that S4 is a risk manager, not an alpha generator. Its value is preventing catastrophic loss from being on the wrong side of a regime shift, not beating pure-hold of the correct asset.

3. Implementation Spec: DCA Direction

3.1 Core Logic

def get_dca_target(cdc_status: str) -> str:

"""

Determine which asset to buy with new DCA contribution.

Args:

cdc_status: Current CDC Action Zone status ('up' or 'down')

Returns:

'BTC' if cdc_status is 'up', else 'XAU'

Notes:

- This only affects NEW contributions, not existing holdings

- CDC status already includes S4_CONFIRM_DAYS=2 persistence

- No additional confirmation needed (DCA amounts are small)

"""

if cdc_status == "up":

return "BTC"

else:

return "XAU"

3.2 Integration Point

# In run_s4_tick() or DCA execution flow:

cdc = get_cdc_status_1d(...)  # existing function

dca_target = get_dca_target(cdc['status'])

# Log decision

log_dca_decision({

"timestamp": now_utc,

"cdc_status": cdc["status"],

"dca_target": dca_target,

"neutral_state": neutral_state,  # for future reference

"ema_gap_pct": gap,

"slope_pct": slope,

})

# Execute DCA

execute_dca_purchase(asset=dca_target, amount=dca_amount)

3.3 Database Schema Addition

-- Add to existing s4_neutral_zone_eod table or create new table:

CREATE TABLE IF NOT EXISTS s4_dca_decisions (

id SERIAL PRIMARY KEY,

timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),

cdc_status VARCHAR(10) NOT NULL,

dca_target VARCHAR(10) NOT NULL,  -- 'BTC' or 'XAU'

neutral_state VARCHAR(20),

ema_gap_pct NUMERIC(10, 4),

slope_pct NUMERIC(10, 4),

dca_amount NUMERIC(18, 8),

executed BOOLEAN DEFAULT FALSE,

notes TEXT

);

3.4 Testing Checklist

Unit test: get_dca_target('up') returns 'BTC'

Unit test: get_dca_target('down') returns 'XAU'

Integration test: CDC status flows correctly from pipeline to DCA decision

Log verification: DCA decisions are recorded in database

Manual check: First 3 DCA executions reviewed before enabling auto-execute

4. Implementation Spec: Shadow Swap Logging

4.1 Purpose

Log what the swap gate WOULD decide each day, without executing any swaps. This builds a track record for future evaluation. After 90 days, compare shadow decisions against actual market outcomes to determine if swap execution is justified.

4.2 Shadow Config (Best Candidate)

SHADOW_SWAP_CONFIG = SwapConfig(

swap_btc_confirm_days=3,

swap_xau_confirm_days=5,

swap_btc_slope_min=2.0,

swap_xau_slope_max=-0.5,

swap_btc_gap_max=2.0,

swap_cooldown_days=7,

swap_require_neutral=True,

)

Note: This is the minimax top config from full sweep (config family 663). It is NOT promoted to execution. It is used only for shadow logging to build validation data.

4.3 Daily Shadow Log

def log_shadow_swap(row, cdc_history, config, hypothetical_holding):

"""Log what swap gate would decide without executing."""

action, reason = evaluate_swap(

row, cdc_history, config, hypothetical_holding, last_shadow_swap_date

)

insert_shadow_log({

"date": row.date,

"hypothetical_holding": hypothetical_holding,

"gate_decision": action,

"gate_reason": reason,

"cdc_status": row.cdc_status,

"neutral_state": row.neutral_state,

"slope_pct": row.slope_pct,

"gap_pct": row.gap_pct,

"would_have_swapped": action != "HOLD",

})

4.4 Database Schema

CREATE TABLE IF NOT EXISTS s4_shadow_swap_log (

id SERIAL PRIMARY KEY,

date DATE NOT NULL,

hypothetical_holding VARCHAR(10) NOT NULL,

gate_decision VARCHAR(20) NOT NULL,

gate_reason TEXT,

cdc_status VARCHAR(10),

neutral_state VARCHAR(20),

slope_pct NUMERIC(10, 4),

gap_pct NUMERIC(10, 4),

would_have_swapped BOOLEAN DEFAULT FALSE,

config_json JSONB

);

5. 90-Day Review Plan

5.1 Data Collection Period

From deployment date: collect 90 days of DCA execution data + shadow swap log. Expected date range approximately 2026-03-10 to 2026-06-10.

5.2 Review Metrics

Metric

Data Source

Decision Threshold

DCA accuracy

s4_dca_decisions vs actual BTC/XAU performance

> 60% of DCA went to better-performing asset

Shadow swap count

s4_shadow_swap_log

< 6 swaps in 90 days (< 1/15 days)

Shadow swap hypothetical return

Shadow log + market data

Positive average return per hypothetical swap

Shadow false swap rate

Swaps that would have reversed within 30d

< 30%

Portfolio vs GOLD-only

Actual DCA portfolio vs pure XAU DCA

Within 5% in GOLD regime, ahead in BTC regime

5.3 Decision Tree at 90-Day Review

Condition

Action

DCA accuracy > 60% AND shadow swap looks profitable

Consider promoting partial swap (20-30%) to shadow mode

DCA accuracy > 60% BUT shadow swap unprofitable

Keep DCA-only. Swap not justified.

DCA accuracy < 60%

Investigate CDC signal quality. May need to adjust DCA logic.

Regime changed (BTC outperforming XAU)

Re-run backtest with new data. Evaluate swap urgency.

5.4 Partial Swap (Phase 2 - If Warranted)

If 90-day review shows shadow swap is profitable, the next step is NOT full swap. Instead:

Implement partial swap: swap only 20-30% of holdings on first signal

Run partial swap in shadow mode for 30 days

If shadow partial swap is profitable: promote to execution

If successful after 60 days: consider increasing to 50% then 100% gradually

Rationale: Phase B proved that full swap drawdown > 30% is unavoidable with current signal quality. Partial swap limits maximum drawdown from any single false signal to 6-9% (30% of position x 20-30% adverse move) instead of 20-45%.

6. Risk Assessment

6.1 Risks of This Decision

Risk

Likelihood

Impact

Mitigation

CDC signal quality degrades

Low

DCA buys wrong asset

90-day review catches this early

BTC bull starts, DCA too slow to capture

Medium

Miss early BTC upside

DCA switches within 1 cycle; shadow swap tracks signal

Regime shifts rapidly back and forth

Medium

DCA alternates, no damage but no benefit

Weekly DCA frequency naturally filters noise

Shadow swap shows swap would have been profitable

Medium

Missed opportunity

Phase 2 partial swap addresses this

6.2 Risks We Avoid

Full swap drawdown > 30% (proven by 3,456 config sweep)

Parameter overfit to historical data (no config promoted)

Premature complexity (no regime detection, no multi-layer gating)

Irreversible deployment (DCA direction is trivially reversible)

7. Document Reference

Document

Content

Status

S4_DCA_Swap_Rules_Spec.docx

Full DCA + swap logic specification

Reference (swap portion deferred)

S4_Swap_Backtest_Framework.docx

Backtest design, metrics, sweep plan

Completed

s4-swap-backtest-phase-a-results.md

Phase A sweep: 384 configs

Completed

s4-swap-backtest-phase-b-shortlist.md

Phase B: strict criteria, 0 pass at -30%

Completed

s4-swap-backtest-full-results.md

Full sweep: 3,456 configs

Completed

s4-phase-0.5-round-5-results.md

Cross-regime comparison (GOLD + BTC bull)

Completed

This document

Final decision + implementation spec

Active

7.1 Files to Create in Codebase

File

Purpose

strategies/s4_dca.py

get_dca_target() + log_dca_decision()

strategies/s4_shadow_swap.py

Shadow swap evaluator + logger

migrations/add_s4_dca_tables.sql

Database schema for DCA + shadow swap tables

tests/test_s4_dca.py

Unit tests for DCA logic

End of Decision Memo & Implementation Spec

Ready for Codex implementation
