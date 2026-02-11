#!/usr/bin/env python3
"""Report helper for S4 Neutral Zone EOD + state change logs."""
from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict
from typing import Any

import MySQLdb
from dotenv import load_dotenv


def _load_env() -> None:
    load_dotenv(dotenv_path=".env")


def _db_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing env var: {name}")
    return val


def get_db_connection():
    return MySQLdb.connect(
        host=_db_env('DB_HOST'),
        user=_db_env('DB_USER'),
        passwd=_db_env('DB_PASSWORD'),
        db=_db_env('DB_NAME'),
        charset='utf8mb4',
    )


def fetch_eod(days: int | None) -> list[dict[str, Any]]:
    sql = (
        "SELECT date, ratio_close, ema12, ema26, ema_gap_pct, slope_pct, state, cdc_status, active_asset, eod_lag_days "
        "FROM s4_neutral_zone_eod"
    )
    params: tuple[Any, ...] = ()
    if days and days > 0:
        sql += " WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"
        params = (days,)
    sql += " ORDER BY date"
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        cur.close()
    return rows


def fetch_state_changes(days: int | None) -> list[dict[str, Any]]:
    sql = (
        "SELECT ts, old_state, new_state, ema_gap_pct, slope_pct "
        "FROM s4_neutral_zone_state_changes"
    )
    params: tuple[Any, ...] = ()
    if days and days > 0:
        sql += " WHERE ts >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        params = (days,)
    sql += " ORDER BY ts"
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        cur.close()
    return rows


def _write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def summarize_eod(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No EOD rows found.")
        return
    state_counts = defaultdict(int)
    gap_sum = defaultdict(float)
    slope_sum = defaultdict(float)
    lag_sum = defaultdict(float)
    lag_count = defaultdict(int)
    for row in rows:
        state = row.get("state") or "unknown"
        state_counts[state] += 1
        gap_sum[state] += float(row.get("ema_gap_pct") or 0.0)
        slope_sum[state] += float(row.get("slope_pct") or 0.0)
        lag = row.get("eod_lag_days")
        if lag is not None:
            lag_sum[state] += float(lag or 0.0)
            lag_count[state] += 1

    total = len(rows)
    print("=== S4 Neutral Zone EOD Summary ===")
    print(f"Rows: {total}")
    for state, count in sorted(state_counts.items()):
        pct = (count / total) * 100.0 if total else 0.0
        avg_gap = gap_sum[state] / count if count else 0.0
        avg_slope = slope_sum[state] / count if count else 0.0
        avg_lag = (lag_sum[state] / lag_count[state]) if lag_count[state] else 0.0
        print(f"- {state}: {count} ({pct:.1f}%) | avg_gap={avg_gap:.4f}% | avg_slope={avg_slope:.4f}% | avg_lag={avg_lag:.2f}d")


def summarize_state_changes(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No state change rows found.")
        return
    counts = defaultdict(int)
    for row in rows:
        key = f"{row.get('old_state')}→{row.get('new_state')}"
        counts[key] += 1
    print("=== S4 Neutral Zone State Changes ===")
    print(f"Events: {len(rows)}")
    for key, count in sorted(counts.items()):
        print(f"- {key}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="S4 Neutral Zone log report")
    parser.add_argument("--days", type=int, default=90, help="Lookback window (days)")
    parser.add_argument("--csv-eod", type=str, default="", help="Export EOD rows to CSV path")
    parser.add_argument("--csv-state-changes", type=str, default="", help="Export state change rows to CSV path")
    args = parser.parse_args()

    _load_env()
    try:
        eod_rows = fetch_eod(args.days)
        state_rows = fetch_state_changes(args.days)
    except Exception as exc:
        print(f"Failed to load logs: {exc}")
        return

    summarize_eod(eod_rows)
    summarize_state_changes(state_rows)

    if args.csv_eod:
        _write_csv(args.csv_eod, eod_rows)
        print(f"Wrote EOD CSV: {args.csv_eod}")
    if args.csv_state_changes:
        _write_csv(args.csv_state_changes, state_rows)
        print(f"Wrote state change CSV: {args.csv_state_changes}")


if __name__ == "__main__":
    main()
