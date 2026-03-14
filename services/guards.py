"""Liquidity and market guard helpers for execution paths."""

from __future__ import annotations

import os


def assess_liquidity_with_threshold(
    adapter,
    *,
    max_spread_pct: float,
) -> tuple[bool, dict]:
    """Check top-of-book spread against a configured threshold."""
    try:
        top_of_book = adapter.get_top_of_book()
        bid = float(top_of_book.get("bid") or 0.0)
        ask = float(top_of_book.get("ask") or 0.0)
        if bid <= 0 or ask <= 0:
            return False, {"reason": "invalid_top_of_book"}
        mid = (bid + ask) / 2
        spread_pct = ((ask - bid) / mid) * 100 if mid > 0 else 999.0
        metrics = {
            "spread_pct": spread_pct,
            "threshold_pct": max_spread_pct,
            "bid": bid,
            "ask": ask,
        }
        if spread_pct > max_spread_pct:
            metrics["reason"] = "spread_high"
            return False, metrics
        return True, metrics
    except NotImplementedError:
        return True, {"reason": "not_supported"}
    except Exception as exc:
        return False, {"reason": "liquidity_error", "error": str(exc)}


def depth_band_limits(price: float, *, band_pct: float) -> tuple[float, float]:
    band = band_pct / 100.0
    lower = price * (1.0 - band)
    upper = price * (1.0 + band)
    return lower, upper


def evaluate_depth_guard_with_config(
    adapter,
    exchange: str,
    price: float,
    *,
    enabled: bool,
    depth_level: int,
    band_pct: float,
    min_notional_threshold: float,
    is_dry_run,
) -> tuple[bool, dict]:
    if not enabled or price <= 0:
        return True, {}
    try:
        snapshot = adapter.get_depth_snapshot(limit=depth_level)
    except NotImplementedError:
        return True, {"reason": "depth_not_supported"}
    except Exception as exc:
        return False, {"reason": "depth_error", "error": str(exc)}
    bids = snapshot.get("bids") or []
    asks = snapshot.get("asks") or []
    lower, upper = depth_band_limits(price, band_pct=band_pct)
    bid_notional = sum(p * q for p, q in bids if p >= lower)
    ask_notional = sum(p * q for p, q in asks if p <= upper)
    min_notional = min(bid_notional, ask_notional)
    metrics = {
        "bid_notional": bid_notional,
        "ask_notional": ask_notional,
        "threshold": min_notional_threshold,
        "band_pct": band_pct,
        "dry_run": is_dry_run(),
    }
    if min_notional < min_notional_threshold:
        metrics["reason"] = "depth_insufficient"
        metrics["min_notional"] = min_notional
        return False, metrics
    return True, metrics


def evaluate_twap_guard_with_config(
    adapter,
    exchange: str,
    price: float,
    *,
    enabled: bool,
    window_minutes: int,
    max_deviation_pct: float,
    is_dry_run,
) -> tuple[bool, dict]:
    if not enabled or price <= 0 or window_minutes <= 0:
        return True, {}
    try:
        candles = adapter.get_recent_candles(interval="1m", limit=window_minutes)
    except NotImplementedError:
        return True, {"reason": "twap_not_supported"}
    except Exception as exc:
        return False, {"reason": "twap_error", "error": str(exc)}
    closes = [float(candle.get("close") or 0.0) for candle in candles if candle.get("close")]
    if not closes:
        return True, {"reason": "twap_no_data"}
    twap = sum(closes) / len(closes)
    if twap <= 0:
        return True, {"reason": "twap_invalid"}
    deviation_pct = abs(price - twap) / twap * 100.0
    metrics = {
        "twap": twap,
        "window_minutes": len(closes),
        "deviation_pct": deviation_pct,
        "threshold_pct": max_deviation_pct,
        "dry_run": is_dry_run(),
    }
    if deviation_pct > max_deviation_pct:
        metrics["reason"] = "twap_deviation"
        return False, metrics
    return True, metrics


def evaluate_notional_cap_with_state(
    exchange: str,
    notional: float,
    state: dict | None = None,
    *,
    is_dry_run,
) -> tuple[bool, dict]:
    current_state = state or {}
    cap = 0.0
    exchange_code = exchange.lower()
    if exchange_code == "okx":
        cap_val = current_state.get("okx_max_usdt")
        if cap_val is None:
            env_val = os.getenv("OKX_MAX_USDT")
            try:
                cap = float(env_val) if env_val not in (None, "") else 0.0
            except (TypeError, ValueError):
                cap = 0.0
        else:
            try:
                cap = float(cap_val)
            except (TypeError, ValueError):
                cap = 0.0
    elif exchange_code == "binance":
        cap_val = current_state.get("binance_max_usdt")
        if cap_val is None:
            env_val = os.getenv("BINANCE_MAX_USDT")
            try:
                cap = float(env_val) if env_val not in (None, "") else 0.0
            except (TypeError, ValueError):
                cap = 0.0
        else:
            try:
                cap = float(cap_val)
            except (TypeError, ValueError):
                cap = 0.0
    if is_dry_run():
        return True, {"reason": "dry_run", "cap": cap, "attempt": notional}
    if cap and cap > 0 and notional > cap:
        return False, {"reason": "notional_cap", "cap": cap, "attempt": notional}
    return True, {"cap": cap, "attempt": notional}
