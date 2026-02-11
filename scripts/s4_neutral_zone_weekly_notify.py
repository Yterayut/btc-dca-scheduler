#!/usr/bin/env python3
"""Weekly LINE Flex summary for S4 Neutral Zone logs."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import MySQLdb
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from notifications.line_flex import build_basic_bubble, make_flex_message  # noqa: E402
from notify import send_line_flex_with_retry, send_line_message_with_retry  # noqa: E402


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


def _fetch_window_summary(cur, days: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT state, COUNT(*) AS cnt,
               ROUND(COUNT(*) * 100.0 / %s, 1) AS pct
        FROM s4_neutral_zone_eod
        WHERE date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
        GROUP BY state
        ORDER BY cnt DESC
        """,
        (days, days),
    )
    dist = cur.fetchall()

    cur.execute(
        """
        SELECT COUNT(*) FROM s4_neutral_zone_state_changes
        WHERE ts >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """,
        (days,),
    )
    changes = int(cur.fetchone()[0] or 0)

    cur.execute(
        """
        SELECT date, state, ema_gap_pct, slope_pct, eod_lag_days
        FROM s4_neutral_zone_eod
        ORDER BY date DESC
        LIMIT 1
        """
    )
    latest = cur.fetchone()
    return {
        "distribution": dist,
        "changes": changes,
        "latest": latest,
    }


def send_report(days: int = 7) -> bool:
    _load_env()
    with _db() as conn:
        cur = conn.cursor()
        summary = _fetch_window_summary(cur, days)
        cur.close()

    updated_bkk = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M")
    sections = [("Updated (BKK)", updated_bkk)]
    if summary["latest"]:
        latest_date, latest_state, latest_gap, latest_slope, latest_lag = summary["latest"]
        lag_days = int(latest_lag or 0)
        if lag_days > 1:
            sections.append(("Alert", "EOD lag > 1 day"))
        sections.append(("Latest", f"{latest_date} | {latest_state}"))
        sections.append(("Gap/Slope", f"{float(latest_gap or 0.0):.4f}% / {float(latest_slope or 0.0):.4f}%"))
        sections.append(("EOD Lag", f"{lag_days} days"))
    sections.append(("State Changes", str(summary["changes"])))

    for row in summary["distribution"]:
        state, cnt, pct = row
        sections.append((str(state), f"{cnt} days ({pct}%)"))

    theme = "warning" if summary["latest"] and int(summary["latest"][4] or 0) > 1 else "info"
    bubble = build_basic_bubble(
        "S4 Neutral Zone Weekly",
        sections,
        subtitle=f"Last {days} days",
        theme=theme,
    )
    flex_message = make_flex_message(f"S4 Neutral Weekly ({days}d)", bubble)
    if send_line_flex_with_retry(flex_message):
        return True

    lines = ["S4 Neutral Zone Weekly", f"Window: {days} days"]
    for label, value in sections:
        lines.append(f"{label}: {value}")
    return send_line_message_with_retry("\n".join(lines))


if __name__ == "__main__":
    try:
        ok = send_report(7)
        raise SystemExit(0 if ok else 2)
    except Exception as exc:
        send_line_message_with_retry(f"S4 Neutral Zone weekly report failed: {exc}")
        raise
