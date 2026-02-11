from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable

import MySQLdb

from security_utils import decrypt_metadata, encrypt_metadata

logger = logging.getLogger(__name__)


def _conn_kwargs() -> dict[str, Any]:
    return {
        "host": os.getenv("DB_HOST"),
        "user": os.getenv("DB_USER"),
        "passwd": os.getenv("DB_PASSWORD"),
        "db": os.getenv("DB_NAME"),
        "charset": "utf8",
    }


@contextmanager
def get_connection():
    conn = None
    try:
        conn = MySQLdb.connect(**_conn_kwargs())
        yield conn
    finally:
        if conn:
            conn.close()


def record_event(
    event_time: datetime,
    event_type: str,
    exchange: str,
    notional_usdt: float,
    btc_quantity: float,
    price_usdt: float,
    realized_pnl_usdt: float,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload = metadata or {}
    token, encrypted = encrypt_metadata(payload)
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO compliance_audit_log
                    (event_time, event_type, exchange, notional_usdt, btc_quantity, price_usdt,
                     realized_pnl_usdt, metadata_blob, metadata_encrypted)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event_time.strftime("%Y-%m-%d %H:%M:%S"),
                    event_type,
                    exchange,
                    float(notional_usdt),
                    float(btc_quantity),
                    float(price_usdt),
                    float(realized_pnl_usdt),
                    token,
                    1 if encrypted else 0,
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.error("Failed to record compliance event: %s", exc, exc_info=True)


def fetch_events(limit: int = 500, start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    qs: list[str] = []
    params: list[Any] = []
    if start:
        qs.append("event_time >= %s")
        params.append(start.strftime("%Y-%m-%d %H:%M:%S"))
    if end:
        qs.append("event_time <= %s")
        params.append(end.strftime("%Y-%m-%d %H:%M:%S"))
    where_clause = f"WHERE {' AND '.join(qs)}" if qs else ""
    sql = f"""
        SELECT event_time, event_type, exchange, notional_usdt, btc_quantity, price_usdt,
               realized_pnl_usdt, metadata_blob, metadata_encrypted
        FROM compliance_audit_log
        {where_clause}
        ORDER BY event_time DESC
        LIMIT %s
    """
    params.append(int(limit))
    events: list[dict[str, Any]] = []
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    except Exception as exc:
        logger.error("Failed to fetch compliance events: %s", exc, exc_info=True)
        return []
    for row in rows:
        ts, event_type, exchange, notional, qty, price, pnl, metadata_blob, encrypted_flag = row
        encrypted = bool(encrypted_flag)
        metadata: dict[str, Any] = {}
        if metadata_blob:
            try:
                metadata = decrypt_metadata(metadata_blob, encrypted=encrypted)
            except Exception as exc:
                metadata = {"error": str(exc)}
        events.append(
            {
                "event_time": str(ts),
                "event_type": event_type,
                "exchange": exchange,
                "notional_usdt": float(notional or 0.0),
                "btc_quantity": float(qty or 0.0),
                "price_usdt": float(price or 0.0),
                "realized_pnl_usdt": float(pnl or 0.0),
                "metadata": metadata,
                "encrypted": encrypted,
            }
        )
    return events
