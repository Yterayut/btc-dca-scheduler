import math
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from strategies import s4_utils


def test_resolve_targets_defaults():
    btc_up, gold_up = s4_utils.resolve_s4_target_allocations({}, "up")
    btc_down, gold_down = s4_utils.resolve_s4_target_allocations({}, "down")

    assert math.isclose(btc_up, 1.0)
    assert math.isclose(gold_up, 0.0)
    assert math.isclose(btc_down, 0.0)
    assert math.isclose(gold_down, 1.0)


def test_resolve_targets_custom_and_normalized():
    config = {
        "target_btc_pct_up": 0.6,
        "target_gold_pct_up": 0.5,  # should normalize
        "target_btc_pct_down": 0.25,
    }
    btc_up, gold_up = s4_utils.resolve_s4_target_allocations(config, "up")
    btc_down, gold_down = s4_utils.resolve_s4_target_allocations(config, "down")

    assert pytest.approx(btc_up, rel=1e-9) == 0.5454545454  # 0.6 / (0.6 + 0.5)
    assert pytest.approx(gold_up, rel=1e-9) == 0.4545454545
    assert pytest.approx(btc_down, rel=1e-9) == 0.25
    assert pytest.approx(gold_down, rel=1e-9) == 0.75


def test_plan_rotation_towards_btc():
    plan = s4_utils.plan_s4_rotation(
        current_btc_usd=20000,
        current_gold_usd=80000,
        target_btc_pct=0.7,
    )
    assert plan is not None
    assert plan["from_asset"] == "GOLD"
    assert plan["to_asset"] == "BTC"
    assert pytest.approx(plan["rotate_usd"], rel=1e-9) == 50000
    assert pytest.approx(plan["delta_btc_pct"], rel=1e-9) == 0.5


def test_plan_rotation_towards_gold():
    plan = s4_utils.plan_s4_rotation(
        current_btc_usd=80000,
        current_gold_usd=20000,
        target_btc_pct=0.3,
    )
    assert plan is not None
    assert plan["from_asset"] == "BTC"
    assert plan["to_asset"] == "GOLD"
    assert pytest.approx(plan["rotate_usd"], rel=1e-9) == 50000
    assert pytest.approx(plan["delta_btc_pct"], rel=1e-9) == -0.5


def test_plan_rotation_honours_min_usd():
    plan = s4_utils.plan_s4_rotation(
        current_btc_usd=10000,
        current_gold_usd=90000,
        target_btc_pct=0.11,
        min_usd=2000,
    )
    # delta only 1000, below threshold
    assert plan is None


def test_cdc_status_from_series_detects_uptrend():
    series = [float(x) for x in range(1, 120)]
    status = s4_utils.cdc_status_from_series(series)
    assert status["status"] == "up"


def test_cdc_status_from_series_detects_downtrend():
    series = [float(200 - x) for x in range(1, 180)]
    status = s4_utils.cdc_status_from_series(series)
    assert status["status"] == "down"


def test_build_ratio_series_alignment():
    btc = [
        (1000, 20000.0),
        (2000, 21000.0),
        (3000, 22000.0),
    ]
    gold = [
        (1000, 2000.0),
        (2000, 2050.0),
        (3000, 2100.0),
    ]
    series = s4_utils.build_ratio_series(btc, gold)
    assert len(series) == 3
    assert series[0] == (1000, 10.0, 20000.0, 2000.0)
    assert pytest.approx(series[-1][1], rel=1e-9) == pytest.approx(22000.0 / 2100.0, rel=1e-9)
