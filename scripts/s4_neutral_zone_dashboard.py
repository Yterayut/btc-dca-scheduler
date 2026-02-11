#!/usr/bin/env python3
"""Generate a quick HTML dashboard + CSV exports for S4 Neutral Zone logs."""
from __future__ import annotations

import csv
import os
from pathlib import Path
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


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _fetch_rows(cur, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_dashboard(days: int, output_html: Path, output_dir: Path) -> None:
    _load_env()
    with _db() as conn:
        cur = conn.cursor()
        eod_rows = _fetch_rows(
            cur,
            """
            SELECT date, state, ema_gap_pct, slope_pct, cdc_status, active_asset, eod_lag_days
            FROM s4_neutral_zone_eod
            WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            ORDER BY date
            """,
            (days,),
        )
        change_rows = _fetch_rows(
            cur,
            """
            SELECT ts, old_state, new_state, ema_gap_pct, slope_pct
            FROM s4_neutral_zone_state_changes
            WHERE ts >= DATE_SUB(NOW(), INTERVAL %s DAY)
            ORDER BY ts
            """,
            (days,),
        )
        dist_rows = _fetch_rows(
            cur,
            """
            SELECT state, COUNT(*) AS days,
                   ROUND(COUNT(*) * 100.0 / %s, 1) AS pct
            FROM s4_neutral_zone_eod
            WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            GROUP BY state
            ORDER BY days DESC
            """,
            (days, days),
        )
        cur.close()

    _write_csv(output_dir / "s4_neutral_zone_eod.csv", eod_rows)
    _write_csv(output_dir / "s4_neutral_zone_state_changes.csv", change_rows)

    output_html.parent.mkdir(parents=True, exist_ok=True)
    dist_lines = "\n".join(
        f"<li><strong>{row['state']}</strong>: {row['days']} days ({row['pct']}%)</li>"
        for row in dist_rows
    )
    eod_lines = "\n".join(
        f"<tr><td>{row['date']}</td><td>{row['state']}</td><td>{row['ema_gap_pct']}</td>"
        f"<td>{row['slope_pct']}</td><td>{row['cdc_status']}</td><td>{row['active_asset']}</td>"
        f"<td>{row.get('eod_lag_days', '')}</td></tr>"
        for row in eod_rows[-14:]
    )
    change_lines = "\n".join(
        f"<tr><td>{row['ts']}</td><td>{row['old_state']}</td><td>{row['new_state']}</td>"
        f"<td>{row['ema_gap_pct']}</td><td>{row['slope_pct']}</td></tr>"
        for row in change_rows[-20:]
    )
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>S4 Neutral Zone Dashboard</title>
  <style>
    body {{ font-family: Arial, sans-serif; background: #0e1116; color: #e5e7eb; padding: 24px; }}
    h1, h2 {{ color: #60a5fa; }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
    th, td {{ border-bottom: 1px solid #1f2937; padding: 8px; text-align: left; }}
    .muted {{ color: #9ca3af; }}
    a {{ color: #93c5fd; }}
  </style>
</head>
<body>
  <h1>S4 Neutral Zone Dashboard</h1>
  <p class="muted">Window: last {days} days</p>
  <h2>State Distribution</h2>
  <ul>
    {dist_lines or '<li>No data</li>'}
  </ul>
  <h2>Recent EOD (last 14)</h2>
    <table>
    <thead><tr><th>Date</th><th>State</th><th>EMA Gap %</th><th>Slope %</th><th>CDC</th><th>Asset</th><th>EOD Lag</th></tr></thead>
    <tbody>{eod_lines or ''}</tbody>
  </table>
  <h2>Recent State Changes (last 20)</h2>
  <table>
    <thead><tr><th>TS</th><th>Old</th><th>New</th><th>EMA Gap %</th><th>Slope %</th></tr></thead>
    <tbody>{change_lines or ''}</tbody>
  </table>
  <p class="muted">CSV exports: <code>{(output_dir / 's4_neutral_zone_eod.csv')}</code> and <code>{(output_dir / 's4_neutral_zone_state_changes.csv')}</code></p>
</body>
</html>
"""
    output_html.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate quick S4 Neutral Zone dashboard")
    parser.add_argument("--days", type=int, default=30, help="Lookback window (days)")
    parser.add_argument("--output", type=Path, default=Path("static/s4_neutral_zone_dashboard.html"))
    parser.add_argument("--csv-dir", type=Path, default=Path("log"))
    args = parser.parse_args()

    build_dashboard(args.days, args.output, args.csv_dir)
    print(f"Dashboard written to {args.output}")
