#!/usr/bin/env python3
"""Run common SQL queries for S4 Neutral Zone checks."""
from __future__ import annotations

import argparse
import os
from typing import Any

import MySQLdb
from dotenv import load_dotenv


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


def _print_rows(cursor) -> None:
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    print(" | ".join(cols))
    print("-" * max(10, len(" | ".join(cols))))
    for row in rows:
        print(" | ".join(str(val) for val in row))


def query_latest_eod(limit: int) -> None:
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT date, state, ema_gap_pct, slope_pct, eod_lag_days
            FROM s4_neutral_zone_eod
            ORDER BY date DESC
            LIMIT %s
            """,
            (limit,),
        )
        _print_rows(cur)
        cur.close()


def query_latest_state_changes(limit: int) -> None:
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts, old_state, new_state, ema_gap_pct
            FROM s4_neutral_zone_state_changes
            ORDER BY ts DESC
            LIMIT %s
            """,
            (limit,),
        )
        _print_rows(cur)
        cur.close()


def query_state_distribution(days: int) -> None:
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT state,
                   COUNT(*) AS days,
                   ROUND(COUNT(*) * 100.0 / %s, 1) AS pct
            FROM s4_neutral_zone_eod
            WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            GROUP BY state
            ORDER BY days DESC
            """,
            (days, days),
        )
        _print_rows(cur)
        cur.close()


def query_spot_check(days: int) -> None:
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT date, state, cdc_status, active_asset, ema_gap_pct, slope_pct, eod_lag_days
            FROM s4_neutral_zone_eod
            WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            ORDER BY date DESC
            """,
            (days,),
        )
        _print_rows(cur)
        cur.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="S4 Neutral Zone SQL quick checks")
    parser.add_argument("--latest-eod", type=int, default=7, help="Latest EOD rows")
    parser.add_argument("--latest-changes", type=int, default=10, help="Latest state changes")
    parser.add_argument("--distribution-days", type=int, default=30, help="State distribution days")
    parser.add_argument("--spot-check-days", type=int, default=10, help="Spot check window")
    args = parser.parse_args()

    _load_env()
    print("\n=== Latest EOD ===")
    query_latest_eod(args.latest_eod)
    print("\n=== Latest State Changes ===")
    query_latest_state_changes(args.latest_changes)
    print("\n=== State Distribution ===")
    query_state_distribution(args.distribution_days)
    print("\n=== Spot Check ===")
    query_spot_check(args.spot_check_days)


if __name__ == "__main__":
    main()
