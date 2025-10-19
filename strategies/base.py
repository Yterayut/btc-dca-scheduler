"""Core strategy contracts for the DCA engine.

Defines common data classes for idempotent actions, configuration snapshots,
and orchestration interfaces that any strategy implementation must follow.
The intent is to support exactly-once execution semantics even when the
scheduler retries work because of transient failures.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Mapping, Sequence


def make_request_id(prefix: str | None = None) -> str:
    """Generate a globally unique request identifier."""
    core = uuid.uuid4().hex
    return f"{prefix}-{core}" if prefix else core


def dedupe_key_for(*parts: Any) -> str:
    """Derive a deterministic dedupe key from ordered components."""
    normalized = []
    for part in parts:
        if part is None:
            normalized.append("null")
        elif isinstance(part, (str, int, float, bool)):
            normalized.append(str(part))
        elif isinstance(part, datetime):
            normalized.append(part.astimezone(timezone.utc).isoformat())
        else:
            normalized.append(repr(part))
    return "|".join(normalized)


class StrategyError(Exception):
    """Raised when a strategy cannot produce a decision safely."""


class StrategyActionType(Enum):
    """Enumeration of action kinds emitted by a strategy tick."""

    DCA_BUY = auto()
    RESERVE_MOVE = auto()
    ROTATION_FLIP = auto()
    HEALTH_UPDATE = auto()
    RESERVE_BUY = auto()
    HALF_SELL = auto()
    SYNC = auto()
    OTHER = auto()


class ActionStatus(Enum):
    """Result status after executing an action."""

    SUCCESS = auto()
    SKIPPED = auto()
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class StrategyConfigSnapshot:
    """Immutable snapshot of user-facing configuration at tick time."""

    name: str
    version: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Runtime context information supplied on every strategy tick."""

    now: datetime
    request_id: str
    dedupe_key: str
    config: StrategyConfigSnapshot
    cursor: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StrategyAction:
    """Idempotent action produced by a strategy decision."""

    action_type: StrategyActionType
    request_id: str
    dedupe_key: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def with_metadata(self, **extra: Any) -> "StrategyAction":
        merged = dict(self.metadata)
        merged.update(extra)
        return StrategyAction(
            action_type=self.action_type,
            request_id=self.request_id,
            dedupe_key=self.dedupe_key,
            payload=self.payload,
            metadata=merged,
        )


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """Output from a strategy tick call."""

    issued_at: datetime
    actions: Sequence[StrategyAction]
    notes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Outcome returned by the orchestrator after attempting an action."""

    request_id: str
    dedupe_key: str
    status: ActionStatus
    detail: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


class StrategyEngine:
    """High-level interface any trading strategy must satisfy."""

    name: str = "base"
    version: str = "0.0"

    def snapshot_config(self) -> StrategyConfigSnapshot:
        """Return configuration snapshot used for decision traceability."""
        return StrategyConfigSnapshot(name=self.name, version=self.version, params={})

    def tick(self, context: StrategyContext) -> StrategyDecision:
        """Compute actions for the current tick."""
        raise NotImplementedError
