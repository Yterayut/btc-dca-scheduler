from datetime import datetime, timedelta
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from strategies.base import StrategyActionType
from strategies.s4 import S4Strategy, S4DecisionInput


def test_s4_no_action_when_within_threshold():
    strategy = S4Strategy(
        {
            'target_btc_pct_up': 0.60,
            'target_btc_pct_down': 0.30,
            'rebalance_threshold_pct': 5.0,
            'min_flip_usd': 500.0,
        }
    )
    now = datetime.utcnow()
    data = S4DecisionInput(
        now=now,
        cdc_status='up',
        btc_price=60000.0,
        gold_price=1900.0,
        exposure_btc_usd=6000.0,
        exposure_gold_usd=4000.0,
    )
    decision = strategy.decide(data)
    assert decision.actions == ()


def test_s4_flip_to_gold_when_overweight_btc():
    strategy = S4Strategy(
        {
            'target_btc_pct_up': 0.60,
            'target_btc_pct_down': 0.30,
            'rebalance_threshold_pct': 5.0,
            'min_flip_usd': 500.0,
        }
    )
    now = datetime.utcnow()
    data = S4DecisionInput(
        now=now,
        cdc_status='down',
        btc_price=58000.0,
        gold_price=1900.0,
        exposure_btc_usd=8000.0,
        exposure_gold_usd=2000.0,
    )
    decision = strategy.decide(data)
    assert len(decision.actions) == 1
    action = decision.actions[0]
    assert action.action_type is StrategyActionType.ROTATION_FLIP
    assert action.payload.get('from_asset') == 'BTC'
    assert action.payload.get('to_asset') == 'GOLD'
    assert action.payload.get('amount_usd') > 0


def test_s4_respects_cooldown():
    strategy = S4Strategy(
        {
            'target_btc_pct_up': 0.60,
            'target_btc_pct_down': 0.30,
            'rebalance_threshold_pct': 3.0,
            'min_flip_usd': 100.0,
            'cooldown_minutes': 120,
        }
    )
    now = datetime.utcnow()
    data = S4DecisionInput(
        now=now,
        cdc_status='down',
        btc_price=58000.0,
        gold_price=1900.0,
        exposure_btc_usd=7500.0,
        exposure_gold_usd=2500.0,
        last_flip_at=now - timedelta(minutes=30),
    )
    decision = strategy.decide(data)
    assert decision.actions == ()
