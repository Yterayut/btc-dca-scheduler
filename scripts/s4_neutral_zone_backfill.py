#!/usr/bin/env python3
"""Backfill S4 neutral zone EOD rows from OKX ratio series."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import MySQLdb
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategies.s4_neutral_zone import DEFAULT_NEUTRAL_CONFIG, calculate_state  # noqa: E402
from strategies.s4_utils import compute_ema_series, fetch_okx_ratio_series  # noqa: E402


def _load_env() -> None:
    load_dotenv(dotenv_path=".env")


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing env var: {name}")
    return value


def _db() -> MySQLdb.connections.Connection:
    return MySQLdb.connect(
        host=_env("DB_HOST"),
        user=_env("DB_USER"),
        passwd=_env("DB_PASSWORD"),
        db=_env("DB_NAME"),
        charset="utf8mb4",
    )


def _date_from_ts(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).date()


def _upsert_eod(
    cur,
    eod_date: date,
    ratio_close: float,
    ema12: float,
    ema26: float,
    gap: float,
    slope: float,
    state: str,
    lag_days: int,
    cdc_status: str | None,
    active_asset: str | None,
) -> None:
    cur.execute(
        """
        INSERT INTO s4_neutral_zone_eod (
            date, ratio_close, ema12, ema26, ema_gap_pct, slope_pct,
            state, cdc_status, active_asset, eod_lag_days
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            ratio_close=VALUES(ratio_close),
            ema12=VALUES(ema12),
            ema26=VALUES(ema26),
            ema_gap_pct=VALUES(ema_gap_pct),
            slope_pct=VALUES(slope_pct),
            state=VALUES(state),
            cdc_status=VALUES(cdc_status),
            active_asset=VALUES(active_asset),
            eod_lag_days=VALUES(eod_lag_days)
        """,
        (eod_date, ratio_close, ema12, ema26, gap, slope, state, cdc_status, active_asset, lag_days),
    )


def backfill(start: date, end: date) -> int:
    series = fetch_okx_ratio_series(use_cache=False)
    filtered = [(ts, ratio) for ts, ratio in series if start <= _date_from_ts(ts) <= end]
    if not filtered:
        raise RuntimeError("No ratio points for backfill window")

    ratios = [ratio for _, ratio in series]
    ema12_series = compute_ema_series(ratios, 12)
    ema26_series = compute_ema_series(ratios, 26)
    if not ema12_series or not ema26_series:
        raise RuntimeError("EMA series empty")

    now_utc_date = datetime.now(timezone.utc).date()
    updated = 0
    with _db() as conn:
        cur = conn.cursor()
        for idx, (ts, ratio_close) in enumerate(series):
            eod_date = _date_from_ts(ts)
            if eod_date < start or eod_date > end:
                continue
            ema12 = ema12_series[idx]
            ema26 = ema26_series[idx]
            ema12_hist = list(reversed(ema12_series[: idx + 1]))
            state, metrics = calculate_state(
                ema12=ema12,
                ema26=ema26,
                ema12_history=ema12_hist,
                config=DEFAULT_NEUTRAL_CONFIG,
            )
            if not state:
                continue
            gap = float(metrics.get("ema_gap_pct") or 0.0)
            slope = float(metrics.get("slope_pct") or 0.0)
            if state.value == "btc_signal":
                cdc_status = "up"
                active_asset = "BTC"
            elif state.value == "gold_signal":
                cdc_status = "down"
                active_asset = "GOLD"
            else:
                cdc_status = None
                active_asset = None
            lag_days = (now_utc_date - eod_date).days
            _upsert_eod(
                cur,
                eod_date,
                ratio_close,
                ema12,
                ema26,
                gap,
                slope,
                state.value,
                lag_days,
                cdc_status,
                active_asset,
            )
            updated += 1
        conn.commit()
        cur.close()
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill S4 neutral zone EOD rows")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    _load_env()
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    updated = backfill(start, end)
    print(f"Backfilled {updated} rows.")


if __name__ == "__main__":
    main()
