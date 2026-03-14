"""
Balance fetching service with lightweight in-memory caching.

The service centralises balance lookups across exchanges so the rest of the
application can request holdings in a single call without worrying about rate
limits or credential handling. It keeps a short-lived cache per (exchange,
asset) pair and marks results as stale when the cache is reused beyond the
configured TTL or when the latest refresh attempt failed.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Callable, Dict, Tuple

from exchanges.factory import get_adapters

BalanceMap = Dict[str, Dict[str, dict]]
CacheKey = Tuple[str, str]

# (exchange, asset) -> {"free": float, "locked": float, "updated_at": float | None}
_CACHE: dict[CacheKey, dict[str, float | None]] = {}

DEFAULT_CACHE_TTL_SECONDS = 30


def clear_cache() -> None:
    """Clear the in-memory balance cache (useful for tests)."""
    _CACHE.clear()


def fetch_balances(
    exchanges: Sequence[str],
    assets: Sequence[str],
    cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
    *,
    force_refresh: bool = False,
    adapter_factory: Callable[[Sequence[str]], dict[str, object]] = get_adapters,
    adapter_kwargs: dict | None = None,
) -> BalanceMap:
    """
    Fetch balances for the requested exchanges/assets with TTL caching.

    Args:
        exchanges: Iterable of exchange identifiers (e.g. ["binance", "okx"]).
        assets: Iterable of asset symbols (e.g. ["BTC", "XAUT"]).
        cache_ttl: Seconds to keep cached balances before refetching.
        force_refresh: Bypass cache when True.
        adapter_factory: Factory returning {exchange: adapter}.
        adapter_kwargs: Extra kwargs forwarded to the adapter factory.

    Returns:
        Dict of exchanges -> assets -> balance dict containing:
            {
                "free": float,
                "locked": float,
                "updated_at": float | None,  # epoch seconds
                "stale": bool,               # True if using cached/old data
                "error": str | None          # Present when the refresh failed
            }
        A top-level "_meta" key contains aggregated errors if any.

    Raises:
        ValueError: When no exchanges or assets are provided.
    """
    normalized_exchanges = _normalize_exchanges(exchanges)
    normalized_assets = _normalize_assets(assets)

    ttl = max(int(cache_ttl or 0), 0)
    adapter_kwargs = adapter_kwargs or {}
    adapters = adapter_factory(normalized_exchanges, **adapter_kwargs)

    now = time.time()
    results: BalanceMap = {}
    aggregated_errors: dict[str, str] = {}

    for exchange in normalized_exchanges:
        adapter = adapters.get(exchange)
        if adapter is None:
            error_msg = f"adapter_not_available: {exchange}"
            aggregated_errors[exchange] = error_msg
            results[exchange] = _placeholder_exchange_balances(
                normalized_assets, error_msg=error_msg
            )
            continue

        exchange_balances: dict[str, dict] = {}
        last_error: Exception | None = None

        for asset in normalized_assets:
            key = _cache_key(exchange, asset)
            cached_entry = _CACHE.get(key)
            entry_data = dict(cached_entry) if cached_entry else {
                "free": 0.0,
                "locked": 0.0,
                "updated_at": None,
            }

            needs_refresh = force_refresh or _needs_refresh(entry_data, now, ttl)
            if last_error is None and needs_refresh:
                try:
                    raw_balance = getattr(adapter, "get_balance")(asset)
                    entry_data = {
                        "free": float(raw_balance.get("free") or 0.0),
                        "locked": float(raw_balance.get("locked") or 0.0),
                        "updated_at": now,
                    }
                    _CACHE[key] = entry_data
                except Exception as exc:  # pragma: no cover - defensive; log upstream
                    last_error = exc
                    # fall back to existing cache if available; otherwise zeroed stub
                    entry_data = dict(cached_entry) if cached_entry else {
                        "free": 0.0,
                        "locked": 0.0,
                        "updated_at": None,
                    }

            updated_at = entry_data.get("updated_at")
            age = (now - updated_at) if updated_at is not None else None
            stale = (
                updated_at is None
                or (ttl > 0 and age is not None and age > ttl)
                or last_error is not None
            )

            result_entry = {
                "free": float(entry_data.get("free") or 0.0),
                "locked": float(entry_data.get("locked") or 0.0),
                "updated_at": updated_at,
                "stale": stale,
            }
            if last_error is not None:
                result_entry["error"] = str(last_error)

            exchange_balances[asset] = result_entry

        if last_error is not None:
            aggregated_errors[exchange] = str(last_error)

        results[exchange] = exchange_balances

    if aggregated_errors:
        results["_meta"] = {"errors": aggregated_errors}

    return results


def _normalize_exchanges(exchanges: Sequence[str]) -> list[str]:
    unique = OrderedDict()
    for raw in exchanges or []:
        if raw is None:
            continue
        slug = str(raw).strip().lower()
        if not slug:
            continue
        if slug.startswith("binance"):
            key = "binance"
        elif slug.startswith("okx"):
            key = "okx"
        elif slug.startswith("bitkub"):
            key = "bitkub"
        else:
            raise ValueError(f"Unsupported exchange '{raw}'")
        unique.setdefault(key, None)

    if not unique:
        raise ValueError("At least one exchange is required")
    return list(unique.keys())


def _normalize_assets(assets: Sequence[str]) -> list[str]:
    unique = OrderedDict()
    for raw in assets or []:
        if raw is None:
            continue
        symbol = str(raw).strip().upper()
        if not symbol:
            continue
        unique.setdefault(symbol, None)

    if not unique:
        raise ValueError("At least one asset is required")
    return list(unique.keys())


def _cache_key(exchange: str, asset: str) -> CacheKey:
    return exchange.lower(), asset.upper()


def _needs_refresh(entry: dict[str, float | None], now: float, ttl: int) -> bool:
    if ttl <= 0:
        return True
    updated_at = entry.get("updated_at")
    if updated_at is None:
        return True
    return (now - updated_at) > ttl


def _placeholder_exchange_balances(assets: Sequence[str], *, error_msg: str) -> dict[str, dict]:
    now = time.time()
    return {
        asset: {
            "free": 0.0,
            "locked": 0.0,
            "updated_at": None,
            "stale": True,
            "error": error_msg,
            "generated_at": now,
        }
        for asset in assets
    }
