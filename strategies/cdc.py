"""CDC DCA strategy implementation using the generic strategy contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .base import (
    StrategyAction,
    StrategyActionType,
    StrategyConfigSnapshot,
    StrategyContext,
    StrategyDecision,
    StrategyEngine,
    dedupe_key_for,
    make_request_id,
)


@dataclass(frozen=True, slots=True)
class WeeklyDcaDecisionInput:
    """Parameters required to decide a weekly DCA action."""

    now: datetime
    schedule_id: int
    mode: str
    amount: float
    cdc_status: str
    cdc_enabled: bool
    binance_amount: float = 0.0
    okx_amount: float = 0.0


@dataclass(frozen=True, slots=True)
class TransitionDecisionInput:
    """Parameters for CDC transition (flip) decisions."""

    now: datetime
    previous_status: str | None
    current_status: str
    red_epoch_active: bool
    half_sell_policy: str
    sell_percent_binance: int
    sell_percent_okx: int
    sell_percent_global: int
    active_exchange: str
    reserve_usdt: float
    reserve_binance_usdt: float
    reserve_okx_usdt: float


class CdcDcaStrategy(StrategyEngine):
    """CDC DCA strategy translated into the action-based contract."""

    name = "cdc_dca_v1"
    version = "2025.09.26"

    def __init__(self, config_params: Mapping[str, float | int | str] | None = None):
        self._config_params = dict(config_params or {})

    def snapshot_config(self) -> StrategyConfigSnapshot:
        return StrategyConfigSnapshot(
            name=self.name,
            version=self.version,
            params=self._config_params,
        )

    # The base class tick remains unused for now because CDC has multiple entry
    # points (weekly DCA, transition checks, manual triggers).  Each entry point
    # generates StrategyDecision objects so the orchestrator can treat them
    # uniformly.  Additional hooks can be added later when the entire runtime is
    # moved to a pure tick-based loop.

    def decide_weekly_dca(self, data: WeeklyDcaDecisionInput) -> StrategyDecision:
        """Return actions required for a weekly DCA event."""

        now_utc = data.now.astimezone(timezone.utc)
        base_context = StrategyContext(
            now=now_utc,
            request_id=make_request_id("cdc-weekly"),
            dedupe_key=dedupe_key_for("weekly-dca", data.schedule_id, now_utc.date()),
            config=self.snapshot_config(),
            cursor={},
            metadata={
                "schedule_id": data.schedule_id,
                "mode": data.mode,
                "cdc_status": data.cdc_status,
            },
        )

        actions: list[StrategyAction] = []
        notes: dict[str, str | float | int] = {
            "mode": data.mode,
            "cdc_status": data.cdc_status,
        }

        if data.mode == "global":
            action = self._global_dca_action(base_context, data)
            actions.append(action)
        else:
            if data.mode in ("binance", "both") and data.binance_amount > 0:
                actions.append(
                    self._exchange_dca_action(
                        base_context,
                        exchange="binance",
                        amount=data.binance_amount,
                        cdc_status=data.cdc_status,
                        cdc_enabled=data.cdc_enabled,
                    )
                )
            if data.mode in ("okx", "both") and data.okx_amount > 0:
                actions.append(
                    self._exchange_dca_action(
                        base_context,
                        exchange="okx",
                        amount=data.okx_amount,
                        cdc_status=data.cdc_status,
                        cdc_enabled=data.cdc_enabled,
                    )
                )
        return StrategyDecision(issued_at=now_utc, actions=tuple(actions), notes=notes)

    def _global_dca_action(
        self,
        context: StrategyContext,
        data: WeeklyDcaDecisionInput,
    ) -> StrategyAction:
        """Produce a single action for the legacy global CDC mode."""
        if not data.cdc_enabled:
            action_kind = StrategyActionType.DCA_BUY
        elif data.cdc_status == "up":
            action_kind = StrategyActionType.DCA_BUY
        else:
            action_kind = StrategyActionType.RESERVE_MOVE
        dedupe = dedupe_key_for(
            context.dedupe_key,
            "global",
            action_kind.name,
        )
        payload = {
            "amount": data.amount,
            "schedule_id": data.schedule_id,
            "cdc_status": data.cdc_status,
            "mode": data.mode,
        }
        metadata = {"cdc_enabled": data.cdc_enabled}
        if action_kind is StrategyActionType.RESERVE_MOVE:
            metadata["reserve_direction"] = "increase"
            metadata["reserve_channel"] = "global"
        else:
            metadata["exchange"] = "active"
        return StrategyAction(
            action_type=action_kind,
            request_id=context.request_id,
            dedupe_key=dedupe,
            payload=payload,
            metadata=metadata,
        )

    def decide_transition(self, data: TransitionDecisionInput) -> StrategyDecision:
        """Return actions for a CDC color transition (half-sell / reserve buy)."""

        now_utc = data.now.astimezone(timezone.utc)
        if data.previous_status == data.current_status:
            return StrategyDecision(issued_at=now_utc, actions=(), notes={})

        context = StrategyContext(
            now=now_utc,
            request_id=make_request_id("cdc-transition"),
            dedupe_key=dedupe_key_for("cdc-transition", data.previous_status, data.current_status, now_utc.date()),
            config=self.snapshot_config(),
            metadata={
                "prev": data.previous_status,
                "curr": data.current_status,
                "red_epoch_active": data.red_epoch_active,
            },
        )

        actions: list[StrategyAction] = []

        if data.current_status == "down":
            if not data.red_epoch_active:
                actions.extend(self._half_sell_actions(context, data))
        elif data.current_status == "up":
            actions.extend(self._reserve_buy_actions(context, data))

        return StrategyDecision(issued_at=now_utc, actions=tuple(actions), notes={"transition": True})

    def _half_sell_actions(self, context: StrategyContext, data: TransitionDecisionInput) -> Sequence[StrategyAction]:
        policy = (data.half_sell_policy or "auto_proportional").lower()

        def pct_for(exchange: str) -> int:
            ex = exchange.lower()
            if ex == "okx":
                return max(int(data.sell_percent_okx or 0), 0)
            if ex == "binance":
                return max(int(data.sell_percent_binance or 0), 0)
            return max(int(data.sell_percent_global or 0), 0)

        exchanges: list[str] = []
        if policy == "binance_only":
            exchanges = ["binance"]
        elif policy == "okx_only":
            exchanges = ["okx"]
        else:
            for ex in ("binance", "okx"):
                if pct_for(ex) > 0:
                    exchanges.append(ex)
            if not exchanges:
                exchanges.append(str(data.active_exchange or "binance").lower())

        seen: set[str] = set()
        actions: list[StrategyAction] = []
        for ex in exchanges:
            ex_low = ex.lower()
            if ex_low in seen:
                continue
            seen.add(ex_low)
            pct = pct_for(ex_low)
            if pct <= 0:
                continue
            actions.append(
                StrategyAction(
                    action_type=StrategyActionType.HALF_SELL,
                    request_id=context.request_id,
                    dedupe_key=dedupe_key_for(context.dedupe_key, "half_sell", ex_low, pct),
                    payload={
                        "exchange": ex_low,
                        "percent": pct,
                    },
                )
            )
        return actions

    def _reserve_buy_actions(self, context: StrategyContext, data: TransitionDecisionInput) -> Sequence[StrategyAction]:
        actions: list[StrategyAction] = []
        actions.append(
            StrategyAction(
                action_type=StrategyActionType.RESERVE_BUY,
                request_id=context.request_id,
                dedupe_key=dedupe_key_for(context.dedupe_key, "reserve_buy", "global"),
                payload={
                    "mode": "global",
                    "amount": max(float(data.reserve_usdt or 0.0), 0.0),
                },
            )
        )
        for exchange, amount in (
            ("binance", data.reserve_binance_usdt),
            ("okx", data.reserve_okx_usdt),
        ):
            actions.append(
                StrategyAction(
                    action_type=StrategyActionType.RESERVE_BUY,
                    request_id=context.request_id,
                    dedupe_key=dedupe_key_for(context.dedupe_key, "reserve_buy", exchange, round(float(amount or 0), 8)),
                    payload={
                        "mode": "exchange",
                        "exchange": exchange,
                        "amount": max(float(amount or 0), 0.0),
                    },
                )
            )
        return actions

    def _exchange_dca_action(
        self,
        context: StrategyContext,
        exchange: str,
        amount: float,
        cdc_status: str,
        *,
        cdc_enabled: bool,
    ) -> StrategyAction:
        """Produce a per-exchange CDC DCA action."""
        if not cdc_enabled:
            action_kind = StrategyActionType.DCA_BUY
        elif cdc_status == "up":
            action_kind = StrategyActionType.DCA_BUY
        else:
            action_kind = StrategyActionType.RESERVE_MOVE
        dedupe = dedupe_key_for(
            context.dedupe_key,
            exchange,
            action_kind.name,
            round(amount, 8),
        )
        payload = {
            "exchange": exchange,
            "amount": amount,
            "cdc_status": cdc_status,
        }
        metadata = {"reserve_direction": "increase" if action_kind is StrategyActionType.RESERVE_MOVE else "decrease"}
        return StrategyAction(
            action_type=action_kind,
            request_id=context.request_id,
            dedupe_key=dedupe,
            payload=payload,
            metadata=metadata,
        )
