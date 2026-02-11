"""Pure helpers for S4 Neutral Zone state calculation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class S4NeutralState(str, Enum):
    NEUTRAL_ZONE = "neutral_zone"
    WEAK_SIGNAL = "weak_signal"
    BTC_SIGNAL = "btc_signal"
    GOLD_SIGNAL = "gold_signal"


@dataclass(frozen=True)
class NeutralZoneConfig:
    name: str
    ema_gap_low: float
    ema_gap_high: float
    slope_lookback_days: int
    slope_deadband: float


DEFAULT_NEUTRAL_CONFIG = NeutralZoneConfig(
    name="balanced",
    ema_gap_low=0.25,
    ema_gap_high=0.40,
    slope_lookback_days=3,
    slope_deadband=0.03,
)


def calculate_state(
    *,
    ema12: float,
    ema26: float,
    ema12_history: list[float],
    config: NeutralZoneConfig,
) -> tuple[S4NeutralState | None, dict[str, Any]]:
    """Return neutral zone state and metrics using EMA inputs.

    ema12_history should be ordered with the most recent value first.
    Returns (None, metrics) when inputs are insufficient.
    """
    metrics: dict[str, Any] = {}
    if ema26 <= 0 or not ema12_history:
        return None, metrics
    lookback = max(int(config.slope_lookback_days or 0), 1)
    if len(ema12_history) <= lookback:
        return None, metrics

    ema_gap_pct = abs(float(ema12) - float(ema26)) / float(ema26) * 100.0
    prior = float(ema12_history[lookback])
    if prior <= 0:
        return None, metrics

    slope_pct = (float(ema12) - prior) / prior * 100.0
    metrics["ema_gap_pct"] = ema_gap_pct
    metrics["slope_pct"] = slope_pct

    if ema_gap_pct < config.ema_gap_low and abs(slope_pct) <= config.slope_deadband:
        state = S4NeutralState.NEUTRAL_ZONE
    elif ema_gap_pct > config.ema_gap_high and slope_pct > config.slope_deadband:
        state = S4NeutralState.BTC_SIGNAL
    elif ema_gap_pct > config.ema_gap_high and slope_pct < -config.slope_deadband:
        state = S4NeutralState.GOLD_SIGNAL
    else:
        state = S4NeutralState.WEAK_SIGNAL

    return state, metrics
