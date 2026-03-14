"""Scheduler/runtime infrastructure helpers."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from pytz import timezone

_LAST_HEARTBEAT_DAY_SENT: str | None = None


def ensure_action_dedupe_table_with_transaction(*, enabled: bool, transaction_ctx) -> None:
    """Create action_dedupe table when DB dedupe is enabled."""
    if not enabled:
        return
    try:
        with transaction_ctx() as (cursor, _):
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS action_dedupe (
                    dedupe_key VARCHAR(128) PRIMARY KEY,
                    request_id VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    KEY idx_action_dedupe_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """
            )
        logging.info("DB dedupe enabled: ensured action_dedupe table exists.")
    except Exception as exc:
        logging.warning("ensure_action_dedupe_table failed: %s", exc)


def claim_dedupe_key_with_transaction(
    dedupe_key: str,
    request_id: str,
    *,
    enabled: bool,
    transaction_ctx,
) -> bool:
    """Try to claim a dedupe key in DB. Returns True if new, False if duplicate."""
    if not enabled or not dedupe_key:
        return True
    try:
        with transaction_ctx() as (cursor, _):
            cursor.execute(
                "INSERT IGNORE INTO action_dedupe (dedupe_key, request_id) VALUES (%s, %s)",
                (dedupe_key, request_id),
            )
            claimed = cursor.rowcount > 0
        if not claimed:
            logging.warning("DB dedupe hit: skipping action dedupe_key=%s request_id=%s", dedupe_key, request_id)
        return claimed
    except Exception as exc:
        logging.warning("claim_dedupe_key failed (allowing action): %s", exc)
        return True


def cleanup_action_dedupe_with_transaction(
    *,
    dedupe_enabled: bool,
    cleanup_enabled: bool,
    cleanup_days: int,
    transaction_ctx,
) -> int:
    """Delete old action_dedupe rows older than configured retention days."""
    if not (dedupe_enabled and cleanup_enabled):
        return 0
    days = max(cleanup_days, 1)
    try:
        with transaction_ctx() as (cursor, _):
            cursor.execute(
                "DELETE FROM action_dedupe WHERE created_at < (NOW() - INTERVAL %s DAY)",
                (days,),
            )
            deleted = cursor.rowcount or 0
        if deleted:
            logging.info("DB dedupe cleanup: deleted %s rows older than %s days.", deleted, days)
        return int(deleted)
    except Exception as exc:
        logging.warning("DB dedupe cleanup failed: %s", exc)
        return 0


def maybe_send_daily_heartbeat_with_dependencies(
    now: datetime,
    *,
    deps: dict,
) -> None:
    """Send a daily heartbeat LINE message once per day (08:00-08:15 Asia/Bangkok)."""
    global _LAST_HEARTBEAT_DAY_SENT

    if now.tzinfo is None:
        now = timezone("Asia/Bangkok").localize(now)
    if now.hour != 8 or now.minute > 15:
        return

    day_key = now.strftime("%Y-%m-%d")
    dedupe_key = f"heartbeat:{day_key}"
    request_id = f"heartbeat-{day_key.replace('-', '')}-{os.getpid()}"
    if not deps["DB_DEDUPE_ENABLED"]:
        if _LAST_HEARTBEAT_DAY_SENT == day_key:
            return
        _LAST_HEARTBEAT_DAY_SENT = day_key
    else:
        if not deps["claim_dedupe_key"](dedupe_key, request_id):
            return

    cdc_status = None
    try:
        cdc_status = deps["load_strategy_state"]().get("last_cdc_status")
    except Exception:
        cdc_status = None

    s4_asset = None
    s4_cdc = None
    s4_signal_source = None
    cooldown_text = None
    confirm_text = None
    last_flip_text = None
    portfolio_text = None
    try:
        record, _, _, runtime = deps["get_s4_state"]()
        if record and isinstance(runtime, dict):
            s4_cdc = str(runtime.get("last_cdc_status") or "").lower() or None
            s4_signal_source = runtime.get("signal_source")
            s4_asset = deps["_s4_runtime_holding_asset"](runtime)
            if not s4_asset:
                s4_asset = "BTC" if (s4_cdc or "up") == "up" else "GOLD"
            cooldown_text, confirm_text = deps["_compute_s4_gates_summary"](now, runtime)
            last_flip_text = deps["_format_dt_local_from_iso"](runtime.get("last_flip_at"))
            exposure = runtime.get("exposure") if isinstance(runtime, dict) else None
            if isinstance(exposure, dict):
                total_usd = exposure.get("total_usd")
                if isinstance(total_usd, (int, float)) and total_usd > 0:
                    portfolio_text = f"{total_usd:,.2f} USDT"
    except Exception as exc:
        logging.debug("Heartbeat S4 state read failed: %s", exc)

    effective_cdc = s4_cdc or cdc_status or "unknown"
    signal_source = s4_signal_source or ("binance_cdc" if cdc_status else None)
    asset_text = s4_asset or "unknown"

    gates_bits = []
    if cooldown_text:
        gates_bits.append(f"cooldown={cooldown_text}")
    if confirm_text:
        gates_bits.append(f"confirm_pending={confirm_text}")

    payload = {
        "status": "RUNNING",
        "time": deps["_format_dt_local"](now) + " (Asia/Bangkok)",
        "pid": os.getpid(),
        "asset": asset_text,
        "cdc": effective_cdc,
        "signal_source": signal_source,
        "gates": " | ".join(gates_bits) if gates_bits else "",
        "last_flip": last_flip_text or "",
        "portfolio": portfolio_text or "",
    }
    try:
        deps["notify_daily_heartbeat"](payload)
        logging.info("Daily heartbeat sent dedupe_key=%s", dedupe_key)
    except Exception as exc:
        logging.warning("Daily heartbeat notify failed: %s", exc)


def acquire_scheduler_lock_with_connection(
    *,
    enabled: bool,
    lock_name: str,
    lock_timeout: int,
    get_connection,
) -> object | None:
    """Acquire a DB-level lock to ensure a single scheduler instance."""
    if not enabled:
        return None
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT GET_LOCK(%s, %s)", (lock_name, lock_timeout))
        row = cursor.fetchone()
        got = bool(row and row[0] == 1)
        if not got:
            logging.error("Failed to acquire scheduler lock '%s'. Another instance may be running.", lock_name)
            cursor.close()
            conn.close()
            return None
        logging.info("Acquired scheduler lock '%s'.", lock_name)
        cursor.close()
        return conn
    except Exception as exc:
        logging.error("Scheduler lock acquisition error: %s", exc)
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass
        return None


def release_scheduler_lock_connection(
    conn: object | None,
    *,
    enabled: bool,
    lock_name: str,
) -> None:
    """Release a DB-level scheduler lock."""
    if not conn or not enabled:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        conn.commit()
        cursor.close()
        logging.info("Released scheduler lock '%s'.", lock_name)
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
