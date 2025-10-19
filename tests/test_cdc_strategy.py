import asyncio
from datetime import datetime, timezone
import unittest

from strategies import (
    StrategyAction,
    StrategyActionType,
    StrategyDecision,
    CdcDcaStrategy,
    WeeklyDcaDecisionInput,
    TransitionDecisionInput,
    StrategyOrchestrator,
    ActionStatus,
    ActionResult,
)


class CdcDcaStrategyTest(unittest.TestCase):
    def _input(self, **overrides):
        base = dict(
            now=datetime.now(timezone.utc),
            schedule_id=1,
            mode="global",
            amount=100.0,
            cdc_status="up",
            cdc_enabled=True,
            binance_amount=0.0,
            okx_amount=0.0,
        )
        base.update(overrides)
        return WeeklyDcaDecisionInput(**base)

    def test_decide_weekly_dca_global_buy(self):
        strategy = CdcDcaStrategy(config_params={"exchange": "binance"})
        decision = strategy.decide_weekly_dca(self._input(cdc_status="up"))
        self.assertEqual(len(decision.actions), 1)
        self.assertIs(decision.actions[0].action_type, StrategyActionType.DCA_BUY)

    def test_decide_weekly_dca_global_reserve(self):
        strategy = CdcDcaStrategy({})
        decision = strategy.decide_weekly_dca(self._input(cdc_status="down"))
        self.assertEqual(len(decision.actions), 1)
        self.assertIs(decision.actions[0].action_type, StrategyActionType.RESERVE_MOVE)

    def test_decide_weekly_dca_disabled_still_buys(self):
        strategy = CdcDcaStrategy({})
        decision = strategy.decide_weekly_dca(self._input(cdc_status="disabled", cdc_enabled=False))
        self.assertEqual(len(decision.actions), 1)
        self.assertIs(decision.actions[0].action_type, StrategyActionType.DCA_BUY)
        self.assertEqual(decision.actions[0].payload.get("cdc_status"), "disabled")

    def test_decide_weekly_dca_multi_exchange(self):
        strategy = CdcDcaStrategy({})
        decision = strategy.decide_weekly_dca(
            self._input(
                mode="both",
                binance_amount=25.0,
                okx_amount=30.0,
                cdc_status="down",
            )
        )
        self.assertEqual(len(decision.actions), 2)
        exchanges = {action.payload.get("exchange") for action in decision.actions}
        self.assertSetEqual(exchanges, {"binance", "okx"})
        for action in decision.actions:
            self.assertIs(action.action_type, StrategyActionType.RESERVE_MOVE)

    def test_decide_transition_to_down_creates_half_sell_actions(self):
        strategy = CdcDcaStrategy({})
        decision = strategy.decide_transition(
            TransitionDecisionInput(
                now=datetime.now(timezone.utc),
                previous_status="up",
                current_status="down",
                red_epoch_active=False,
                half_sell_policy="auto_proportional",
                sell_percent_binance=60,
                sell_percent_okx=40,
                sell_percent_global=50,
                active_exchange="binance",
                reserve_usdt=0.0,
                reserve_binance_usdt=0.0,
                reserve_okx_usdt=0.0,
            )
        )
        self.assertGreaterEqual(len(decision.actions), 1)
        for action in decision.actions:
            self.assertIs(action.action_type, StrategyActionType.HALF_SELL)

    def test_decide_transition_to_up_creates_reserve_actions(self):
        strategy = CdcDcaStrategy({})
        decision = strategy.decide_transition(
            TransitionDecisionInput(
                now=datetime.now(timezone.utc),
                previous_status="down",
                current_status="up",
                red_epoch_active=True,
                half_sell_policy="auto_proportional",
                sell_percent_binance=60,
                sell_percent_okx=40,
                sell_percent_global=50,
                active_exchange="binance",
                reserve_usdt=100.0,
                reserve_binance_usdt=50.0,
                reserve_okx_usdt=25.0,
            )
        )
        self.assertEqual(len(decision.actions), 3)
        kinds = [action.action_type for action in decision.actions]
        self.assertTrue(all(kind is StrategyActionType.RESERVE_BUY for kind in kinds))


class StrategyOrchestratorTest(unittest.IsolatedAsyncioTestCase):
    async def test_orchestrator_dedupe(self):
        orchestrator = StrategyOrchestrator()
        now = datetime.now(timezone.utc)
        action = StrategyAction(
            action_type=StrategyActionType.DCA_BUY,
            request_id="req",
            dedupe_key="key",
            payload={"amount": 10},
        )
        decision = StrategyDecision(issued_at=now, actions=(action, action))

        async def handler(act: StrategyAction) -> ActionResult:
            return ActionResult(
                request_id=act.request_id,
                dedupe_key=act.dedupe_key,
                status=ActionStatus.SUCCESS,
            )

        results = await orchestrator.execute(decision, {StrategyActionType.DCA_BUY: handler})
        self.assertEqual(len(results), 2)
        self.assertIs(results[0].status, ActionStatus.SUCCESS)
        self.assertIs(results[1].status, ActionStatus.SKIPPED)


if __name__ == "__main__":
    unittest.main()
