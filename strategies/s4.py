"""S4 swing overlay strategy: rotates between BTC and GOLD legs based on CDC.

The strategy is intentionally dry-run friendly – it only suggests rotation
actions (StrategyActionType.ROTATION_FLIP) and leaves actual execution to the
orchestrator/handler.  This keeps automated tests and staging runs safe while
allowing human operators to inspect the decision notes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

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
class S4DecisionInput:
    """Inputs required to decide whether to rotate between BTC and GOLD."""

    now: datetime
    cdc_status: str
    btc_price: float
    gold_price: float
    exposure_btc_usd: float
    exposure_gold_usd: float
    last_flip_at: datetime | None = None

    @property
    def total_usd(self) -> float:
        return max((self.exposure_btc_usd or 0.0) + (self.exposure_gold_usd or 0.0), 0.0)

    @property
    def btc_pct(self) -> float:
        total = self.total_usd
        if total <= 0:
            return 0.0
        return (self.exposure_btc_usd or 0.0) / total

    @property
    def gold_pct(self) -> float:
        total = self.total_usd
        if total <= 0:
            return 0.0
        return (self.exposure_gold_usd or 0.0) / total


class S4Strategy(StrategyEngine):
    """Rotation strategy that tilts between BTC and GOLD legs."""

    name = "s4_multi_leg"
    version = "2025.10.01"

    def __init__(self, config_params: Mapping[str, float | int | str] | None = None):
        self._config_params = dict(config_params or {})

    # Helper getters -----------------------------------------------------
    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._config_params.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(float(self._config_params.get(key, default)))
        except (TypeError, ValueError):
            return default

    def snapshot_config(self) -> StrategyConfigSnapshot:
        return StrategyConfigSnapshot(
            name=self.name,
            version=self.version,
            params=self._config_params,
        )

    # Decision logic -----------------------------------------------------
    def decide(self, data: S4DecisionInput) -> StrategyDecision:
        """Decide whether we should rotate between BTC and GOLD legs."""
        now_utc = data.now.astimezone(timezone.utc)
        request_id = make_request_id("s4-rotation")
        dedupe = dedupe_key_for("s4-rotation", data.cdc_status, now_utc.date())

        notes: dict[str, float | str] = {
            "cdc_status": data.cdc_status,
            "btc_price": data.btc_price,
            "gold_price": data.gold_price,
            "exposure_btc_pct": round(data.btc_pct * 100.0, 2),
            "exposure_gold_pct": round(data.gold_pct * 100.0, 2),
            "total_usd": round(data.total_usd, 2),
        }

        total_usd = data.total_usd
        if total_usd <= 0:
            return StrategyDecision(issued_at=now_utc, actions=(), notes=notes)

        cooldown_minutes = self._cfg_int("cooldown_minutes", 90)
        if (
            data.last_flip_at
            and (data.now - data.last_flip_at).total_seconds() < cooldown_minutes * 60
        ):
            notes["cooldown_active"] = True
            notes["cooldown_minutes"] = cooldown_minutes
            return StrategyDecision(issued_at=now_utc, actions=(), notes=notes)

        target_btc_pct_up = self._cfg_float("target_btc_pct_up", 0.65)
        target_btc_pct_down = self._cfg_float("target_btc_pct_down", 0.35)
        threshold_pct = self._cfg_float("rebalance_threshold_pct", 5.0) / 100.0
        min_flip_usd = self._cfg_float("min_flip_usd", 500.0)
        max_flip_pct = self._cfg_float("max_flip_pct", 35.0) / 100.0

        target_btc_pct = (
            target_btc_pct_up if str(data.cdc_status).lower() == "up" else target_btc_pct_down
        )
        delta_pct = data.btc_pct - target_btc_pct

        notes["target_btc_pct"] = round(target_btc_pct * 100.0, 2)
        notes["delta_pct"] = round(delta_pct * 100.0, 2)

        if abs(delta_pct) < threshold_pct:
            notes["below_threshold"] = True
            notes["threshold_pct"] = threshold_pct * 100.0
            return StrategyDecision(issued_at=now_utc, actions=(), notes=notes)

        amount_usd = abs(delta_pct) * total_usd
        amount_usd = min(amount_usd, total_usd * max_flip_pct)
        amount_usd = max(amount_usd, min_flip_usd)

        # Clamp to available notionals
        if delta_pct > 0:
            # Overweight BTC -> rotate from BTC into GOLD
            amount_usd = min(amount_usd, data.exposure_btc_usd)
            from_leg = "BTC"
            to_leg = "GOLD"
        else:
            # Overweight GOLD -> rotate from GOLD back to BTC
            amount_usd = min(amount_usd, data.exposure_gold_usd)
            from_leg = "GOLD"
            to_leg = "BTC"

        if amount_usd <= 0:
            notes["insufficient_notional"] = True
            return StrategyDecision(issued_at=now_utc, actions=(), notes=notes)

        payload = {
            "from_asset": from_leg,
            "to_asset": to_leg,
            "amount_usd": round(amount_usd, 2),
            "target_btc_pct": target_btc_pct,
            "current_btc_pct": data.btc_pct,
            "cdc_status": data.cdc_status,
            "btc_price": data.btc_price,
            "gold_price": data.gold_price,
        }

        metadata = {
            "delta_pct": delta_pct,
            "threshold_pct": threshold_pct,
            "max_flip_pct": max_flip_pct,
            "min_flip_usd": min_flip_usd,
        }

        action = StrategyAction(
            action_type=StrategyActionType.ROTATION_FLIP,
            request_id=request_id,
            dedupe_key=dedupe,
            payload=payload,
            metadata=metadata,
        )

        return StrategyDecision(issued_at=now_utc, actions=(action,), notes=notes)
