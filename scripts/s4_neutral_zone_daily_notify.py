#!/usr/bin/env python3
"""Daily LINE Flex alert for S4 Neutral Zone logs."""
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


def _fetch_latest_eod(cur) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT date, ratio_close, ema12, ema26, ema_gap_pct, slope_pct,
               state, cdc_status, active_asset, eod_lag_days
        FROM s4_neutral_zone_eod
        ORDER BY date DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _fetch_state_changes(cur, date_value: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT old_state, new_state, COUNT(*) AS cnt
        FROM s4_neutral_zone_state_changes
        WHERE DATE(ts) = %s
        GROUP BY old_state, new_state
        ORDER BY cnt DESC
        """,
        (date_value,),
    )
    rows = cur.fetchall()
    return [
        {"old_state": row[0], "new_state": row[1], "cnt": int(row[2] or 0)}
        for row in rows
    ]


def _format_state_changes(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No state changes"
    parts = []
    for row in rows:
        old_state = row.get("old_state") or "-"
        new_state = row.get("new_state") or "-"
        cnt = row.get("cnt") or 0
        parts.append(f"{old_state}→{new_state} ({cnt})")
    return "; ".join(parts)


def build_flex_payload() -> dict:
    _load_env()
    with _db() as conn:
        cur = conn.cursor()
        latest = _fetch_latest_eod(cur)
        if not latest:
            bubble = build_basic_bubble(
                "S4 Neutral Zone Daily Log",
                [("Status", "No EOD data yet")],
                subtitle="Waiting for daily close",
                theme="warning",
            )
            return make_flex_message("S4 Neutral Zone: No data", bubble)

        date_value = str(latest["date"])
        state_changes = _fetch_state_changes(cur, date_value)

    lag_days = int(latest.get('eod_lag_days') or 0)
    updated_bkk = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M")
    sections = [
        ("Updated (BKK)", updated_bkk),
        ("Date", date_value),
        ("State", str(latest.get("state") or "-")),
        ("CDC", str(latest.get("cdc_status") or "-")),
        ("Asset", str(latest.get("active_asset") or "-")),
        ("EMA Gap", f"{float(latest.get('ema_gap_pct') or 0.0):.4f}%"),
        ("Slope", f"{float(latest.get('slope_pct') or 0.0):.4f}%"),
        ("EOD Lag", f"{lag_days} days"),
        ("State Changes", _format_state_changes(state_changes)),
    ]
    theme = "warning" if lag_days > 1 else "info"
    if lag_days > 1:
        sections.insert(1, ("Alert", "EOD lag > 1 day"))
    bubble = build_basic_bubble(
        "S4 Neutral Zone Daily Log",
        sections,
        subtitle="EOD Summary",
        theme=theme,
    )
    return make_flex_message(f"S4 Neutral Zone {date_value}", bubble)


def send_report() -> bool:
    flex_message = build_flex_payload()
    if send_line_flex_with_retry(flex_message):
        return True
    # fallback to plain text if Flex fails
    payload = flex_message.get("contents") or {}
    body = payload.get("body") or {}
    contents = body.get("contents") or []
    text_lines = []
    for item in contents:
        if isinstance(item, dict) and item.get("type") == "text":
            text_lines.append(str(item.get("text") or ""))
    if text_lines:
        return send_line_message_with_retry("\n".join(text_lines))
    return send_line_message_with_retry("S4 Neutral Zone Daily Log")


if __name__ == "__main__":
    try:
        ok = send_report()
        raise SystemExit(0 if ok else 2)
    except Exception as exc:
        send_line_message_with_retry(f"S4 Neutral Zone daily report failed: {exc}")
        raise
