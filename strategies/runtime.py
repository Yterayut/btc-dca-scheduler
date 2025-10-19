"""Strategy execution runtime helpers."""

from __future__ import annotations

from typing import Awaitable, Callable, Dict, Iterable, Mapping

from .base import ActionResult, ActionStatus, StrategyAction, StrategyDecision

ActionHandler = Callable[[StrategyAction], Awaitable[ActionResult]]


class StrategyOrchestrator:
    """Executes strategy decisions with in-memory idempotency guards."""

    def __init__(self) -> None:
        self._dedupe_cache: set[str] = set()

    def reset_cache(self) -> None:
        """Clear dedupe cache (useful in tests)."""
        self._dedupe_cache.clear()

    async def execute(
        self,
        decision: StrategyDecision,
        handlers: Mapping[str, ActionHandler] | Mapping[int, ActionHandler] | Dict,
    ) -> tuple[ActionResult, ...]:
        """Execute actions using provided handlers and record dedupe keys."""
        results: list[ActionResult] = []
        for action in decision.actions:
            dedupe_key = action.dedupe_key
            if dedupe_key in self._dedupe_cache:
                results.append(
                    ActionResult(
                        request_id=action.request_id,
                        dedupe_key=dedupe_key,
                        status=ActionStatus.SKIPPED,
                        detail="duplicate_action",
                    )
                )
                continue

            handler = handlers.get(action.action_type) or handlers.get(action.action_type.name)  # type: ignore[arg-type]
            if handler is None:
                results.append(
                    ActionResult(
                        request_id=action.request_id,
                        dedupe_key=dedupe_key,
                        status=ActionStatus.FAILED,
                        detail="no_handler",
                    )
                )
                continue

            try:
                result = await handler(action)
            except Exception as exc:  # pragma: no cover - surfaced as failure
                result = ActionResult(
                    request_id=action.request_id,
                    dedupe_key=dedupe_key,
                    status=ActionStatus.FAILED,
                    detail=str(exc),
                )

            if result.status is ActionStatus.SUCCESS:
                self._dedupe_cache.add(dedupe_key)
            results.append(result)
        return tuple(results)

