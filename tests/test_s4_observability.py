import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategies.s4_observability import (
    derive_shadow_decision,
    mismatch_severity,
    next_unlock_from_gate_reason,
    normalize_reason_filter,
    parse_bool,
)


@pytest.mark.parametrize(
    "value,default,expected",
    [
        (None, False, False),
        (None, True, True),
        (True, False, True),
        (False, True, False),
        ("1", False, True),
        ("true", False, True),
        ("yes", False, True),
        ("on", False, True),
        ("0", True, False),
        ("false", True, False),
        ("no", True, False),
        ("off", True, False),
        ("unknown", True, True),
        ("unknown", False, False),
    ],
)
def test_parse_bool(value, default, expected):
    assert parse_bool(value, default=default) is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("all", {"shadow_swap_plan", "shadow_swap_heartbeat"}),
        ("heartbeat", {"shadow_swap_heartbeat"}),
        ("plan", {"shadow_swap_plan"}),
        ("unknown", {"shadow_swap_plan", "shadow_swap_heartbeat"}),
        (None, {"shadow_swap_plan", "shadow_swap_heartbeat"}),
    ],
)
def test_normalize_reason_filter(raw, expected):
    assert normalize_reason_filter(raw) == expected


def test_derive_shadow_decision_from_gate():
    entry = {
        "reason": "shadow_swap_heartbeat",
        "metadata_json": {"gate": {"decision": "SWAP_TO_BTC"}},
    }
    assert derive_shadow_decision(entry) == "SWAP_TO_BTC"


def test_derive_shadow_decision_from_plan_route():
    entry = {
        "reason": "shadow_swap_plan",
        "from_asset": "GOLD",
        "to_asset": "BTC",
    }
    assert derive_shadow_decision(entry) == "SWAP_TO_BTC"


def test_derive_shadow_decision_defaults_hold():
    assert derive_shadow_decision({"reason": "shadow_swap_heartbeat"}) == "HOLD"


@pytest.mark.parametrize(
    "reason,btc_days,xau_days,condition,min_days",
    [
        ("gate_cdc_up_required", 3, 5, "cdc_status must be up for 3 consecutive days", 3),
        ("gate_cdc_down_required", 3, 5, "cdc_status must be down for 5 consecutive days", 5),
        ("gate_cdc_confirm", 3, 5, "cdc confirmation window must complete", 1),
        ("gate_neutral", 3, 5, "neutral_state must be btc_signal", 1),
        ("gate_slope", 3, 5, "slope threshold must be satisfied", 1),
        ("gate_gap", 3, 5, "ema gap threshold must be satisfied", 1),
        ("gate_cooldown", 3, 5, "cooldown period must expire", 1),
        ("all_gates_passed", 3, 5, "all gates passed", 0),
        ("unknown", 3, 5, "waiting for next valid signal snapshot", 1),
    ],
)
def test_next_unlock_from_gate_reason(reason, btc_days, xau_days, condition, min_days):
    actual_condition, actual_days = next_unlock_from_gate_reason(
        reason,
        btc_confirm_days=btc_days,
        xau_confirm_days=xau_days,
    )
    assert actual_condition == condition
    assert actual_days == min_days


@pytest.mark.parametrize(
    "mismatch,eod_lag_days,streak,expected",
    [
        (False, 0, 0, "match"),
        (True, 2, 1, "info"),
        (True, 0, 1, "warn"),
        (True, 2, 2, "warn"),
        (True, 2, 5, "warn"),
        (True, 0, 5, "critical"),
    ],
)
def test_mismatch_severity(mismatch, eod_lag_days, streak, expected):
    assert mismatch_severity(mismatch=mismatch, eod_lag_days=eod_lag_days, streak=streak) == expected
