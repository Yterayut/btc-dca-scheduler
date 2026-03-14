"""Utility helpers for S4 BTC↔GOLD allocation logic."""

from __future__ import annotations

from typing import Any, Mapping, Sequence, Tuple

import math
import time
from datetime import datetime, timezone

import requests

OKX_BASE_URL = "https://www.okx.com"
_OKX_RATIO_CACHE: dict[str, Any] = {"data": None, "expires": 0}
_OKX_RATIO_SERIES_CACHE: dict[str, Any] = {"data": None, "expires": 0}


def get_s4_dca_target_asset(cdc_status: str | None) -> str:
    """Return DCA target asset for S4 DCA-first lane.

    Rule:
    - CDC up   -> BTC
    - CDC down -> GOLD
    """
    return "BTC" if str(cdc_status or "").lower() == "up" else "GOLD"


def _clamp_ratio(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def resolve_s4_target_allocations(config: Mapping[str, Any] | None, cdc_status: str) -> Tuple[float, float]:
    """Resolve target BTC/GOLD weights (0-1) based on config and CDC state."""

    def _pct(value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        return _clamp_ratio(parsed)

    cfg = dict(config or {})
    status = str(cdc_status or "").lower()
    if status == "up":
        btc_pct = _pct(cfg.get("target_btc_pct_up", 1.0), 1.0)
        gold_key = "target_gold_pct_up"
    else:
        btc_pct = _pct(cfg.get("target_btc_pct_down", 0.0), 0.0)
        gold_key = "target_gold_pct_down"

    gold_pct_raw = cfg.get(gold_key)
    gold_pct = _pct(gold_pct_raw, 0.0) if gold_pct_raw is not None else 1.0 - btc_pct

    total = btc_pct + gold_pct
    if total <= 0:
        return (1.0, 0.0) if status == "up" else (0.0, 1.0)

    btc_weight = _clamp_ratio(btc_pct / total)
    gold_weight = _clamp_ratio(1.0 - btc_weight)
    return btc_weight, gold_weight


def plan_s4_rotation(
    current_btc_usd: float,
    current_gold_usd: float,
    target_btc_pct: float,
    *,
    min_usd: float = 0.0,
) -> dict[str, float | str] | None:
    """Compute rotation plan required to reach the target BTC weight."""
    total = max(current_btc_usd, 0.0) + max(current_gold_usd, 0.0)
    if total <= 0:
        return None

    target_btc_pct = _clamp_ratio(target_btc_pct)
    target_gold_pct = _clamp_ratio(1.0 - target_btc_pct)

    target_btc_usd = target_btc_pct * total
    target_gold_usd = target_gold_pct * total
    delta_btc_usd = target_btc_usd - current_btc_usd
    usd_gap = abs(delta_btc_usd)

    if usd_gap <= 0:
        return None

    from_asset = "GOLD" if delta_btc_usd > 0 else "BTC"
    to_asset = "BTC" if from_asset == "GOLD" else "GOLD"
    available_usd = current_gold_usd if from_asset == "GOLD" else current_btc_usd
    rotate_usd = min(usd_gap, max(available_usd, 0.0))

    if rotate_usd <= 0:
        return None
    if min_usd > 0 and rotate_usd < min_usd:
        return None

    delta_pct = delta_btc_usd / total if total > 0 else 0.0
    return {
        "from_asset": from_asset,
        "to_asset": to_asset,
        "rotate_usd": rotate_usd,
        "delta_btc_pct": delta_pct,
        "target_btc_pct": target_btc_pct,
        "target_gold_pct": target_gold_pct,
        "target_btc_usd": target_btc_usd,
        "target_gold_usd": target_gold_usd,
    }


# --- CDC ratio helpers -------------------------------------------------------

def _ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    if period <= 1:
        return list(values)
    k = 2 / (period + 1)
    out: list[float] = []
    prev = float(values[0])
    out.append(prev)
    for value in values[1:]:
        prev = (float(value) * k) + (prev * (1 - k))
        out.append(prev)
    return out


def compute_ema_series(values: Sequence[float], period: int) -> list[float]:
    """Public EMA helper for S4 neutral zone calculations."""
    return _ema(values, period)


def _last_true_index(flags: Sequence[bool]) -> int | None:
    for idx in range(len(flags) - 1, -1, -1):
        if flags[idx]:
            return idx
    return None


def cdc_status_from_series(values: Sequence[float]) -> dict[str, Any]:
    """Compute CDC Action Zone status from a numeric sequence."""
    cleansed = [float(v) for v in values if v is not None and not math.isnan(v)]
    if len(cleansed) < 50:
        return {"status": "down", "fast": None, "slow": None}

    xprice = _ema(cleansed, 1)
    fast = _ema(xprice, 12)
    slow = _ema(xprice, 26)

    n = len(cleansed)
    bull = [fast[i] > slow[i] for i in range(n)]
    bear = [fast[i] < slow[i] for i in range(n)]
    green = [bull[i] and (xprice[i] > fast[i]) for i in range(n)]
    red = [bear[i] and (xprice[i] < fast[i]) for i in range(n)]

    buycond = [False] * n
    sellcond = [False] * n
    for i in range(1, n):
        buycond[i] = green[i] and (not green[i - 1])
        sellcond[i] = red[i] and (not red[i - 1])

    last_buy = _last_true_index(buycond)
    last_sell = _last_true_index(sellcond)
    current_idx = n - 1
    infinity = float("inf")
    bars_since_buy = (current_idx - last_buy) if last_buy is not None else infinity
    bars_since_sell = (current_idx - last_sell) if last_sell is not None else infinity
    if bars_since_buy == infinity and bars_since_sell == infinity:
        bullish = bull[-1]
    else:
        bullish = bars_since_buy < bars_since_sell

    status = "up" if bullish else "down"
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "status": status,
        "fast": fast[-1],
        "slow": slow[-1],
        "updated_at": now_iso,
    }


def _fetch_okx_candles(inst_id: str, *, limit: int = 200, bar: str = "1D") -> list[tuple[int, float]]:
    params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    resp = requests.get(
        f"{OKX_BASE_URL}/api/v5/market/candles",
        params=params,
        timeout=(5, 5),
    )
    resp.raise_for_status()
    data = resp.json().get("data") or []
    candles: list[tuple[int, float]] = []
    for entry in reversed(data):
        try:
            ts = int(float(entry[0]))
            close = float(entry[4])
        except (ValueError, TypeError):
            continue
        candles.append((ts, close))
    return candles


def _drop_unclosed_candle(candles: Sequence[tuple[int, float]], *, timeframe_seconds: int) -> list[tuple[int, float]]:
    if not candles:
        return []
    now_ms = int(time.time() * 1000)
    frame_ms = timeframe_seconds * 1000
    trimmed = list(candles)
    if trimmed and (now_ms - trimmed[-1][0]) < frame_ms:
        trimmed = trimmed[:-1]
    return trimmed


def build_ratio_series(
    btc_candles: Sequence[tuple[int, float]],
    gold_candles: Sequence[tuple[int, float]],
) -> list[tuple[int, float, float, float]]:
    """Align BTC and GOLD candles by timestamp and compute ratio."""
    if not btc_candles or not gold_candles:
        return []
    btc_map = {ts: close for ts, close in btc_candles}
    gold_map = {ts: close for ts, close in gold_candles}
    timestamps = sorted(set(btc_map.keys()) & set(gold_map.keys()))
    series: list[tuple[int, float, float, float]] = []
    for ts in timestamps:
        btc_close = btc_map.get(ts, 0.0)
        gold_close = gold_map.get(ts, 0.0)
        if btc_close <= 0 or gold_close <= 0:
            continue
        ratio = btc_close / gold_close
        series.append((ts, ratio, btc_close, gold_close))
    return series


def fetch_okx_ratio_signal(
    *,
    use_cache: bool = True,
    limit: int = 200,
    bar: str = "1D",
) -> dict[str, Any]:
    """Fetch BTC/XAUT ratio from OKX and compute CDC status."""
    now = time.time()
    cache_ttl = 300  # 5 minutes
    if use_cache and _OKX_RATIO_CACHE["data"] and now < _OKX_RATIO_CACHE["expires"]:
        return _OKX_RATIO_CACHE["data"]

    timeframe_seconds = 60 * 60 * 24 if bar.upper() in ("1D", "1d") else 60 * 60
    btc = _fetch_okx_candles("BTC-USDT", limit=limit, bar=bar)
    gold = _fetch_okx_candles("XAUT-USDT", limit=limit, bar=bar)
    btc = _drop_unclosed_candle(btc, timeframe_seconds=timeframe_seconds)
    gold = _drop_unclosed_candle(gold, timeframe_seconds=timeframe_seconds)
    ratio_series = build_ratio_series(btc, gold)
    if len(ratio_series) < 50:
        raise ValueError("insufficient ratio data from OKX")

    timestamps = [ts for ts, _, _, _ in ratio_series]
    ratios = [ratio for _, ratio, _, _ in ratio_series]
    btc_closes = [btc_close for _, _, btc_close, _ in ratio_series]
    gold_closes = [gold_close for _, _, _, gold_close in ratio_series]
    cdc = cdc_status_from_series(ratios)
    latest_ts = timestamps[-1]
    cdc.update(
        {
            "ratio": ratios[-1],
            "btc_close": btc_closes[-1],
            "gold_close": gold_closes[-1],
            "timestamp": latest_ts,
            "source": "okx_ratio",
        }
    )
    _OKX_RATIO_CACHE["data"] = cdc
    _OKX_RATIO_CACHE["expires"] = now + cache_ttl
    return cdc


def fetch_okx_ratio_series(
    *,
    use_cache: bool = True,
    limit: int = 200,
    bar: str = "1D",
) -> list[tuple[int, float]]:
    """Fetch BTC/XAUT ratio series (timestamp, ratio) from OKX."""
    now = time.time()
    cache_ttl = 300  # 5 minutes
    if use_cache and _OKX_RATIO_SERIES_CACHE["data"] and now < _OKX_RATIO_SERIES_CACHE["expires"]:
        return _OKX_RATIO_SERIES_CACHE["data"]

    timeframe_seconds = 60 * 60 * 24 if bar.upper() in ("1D", "1d") else 60 * 60
    btc = _fetch_okx_candles("BTC-USDT", limit=limit, bar=bar)
    gold = _fetch_okx_candles("XAUT-USDT", limit=limit, bar=bar)
    btc = _drop_unclosed_candle(btc, timeframe_seconds=timeframe_seconds)
    gold = _drop_unclosed_candle(gold, timeframe_seconds=timeframe_seconds)
    ratio_series = build_ratio_series(btc, gold)
    if len(ratio_series) < 50:
        raise ValueError("insufficient ratio data from OKX")

    series = [(ts, ratio) for ts, ratio, _, _ in ratio_series]
    _OKX_RATIO_SERIES_CACHE["data"] = series
    _OKX_RATIO_SERIES_CACHE["expires"] = now + cache_ttl
    return series
