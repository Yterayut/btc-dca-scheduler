"""Strategy state and journaling helpers."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from services.db import db_transaction, get_db_connection


def load_strategy_record(mode: str) -> dict | None:
    """Return a raw strategy_state row as dict."""
    return load_strategy_record_with_connection(mode, get_db_connection)


def load_strategy_record_with_connection(mode: str, get_connection) -> dict | None:
    """Return a raw strategy_state row as dict using injected connection factory."""
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM strategy_state WHERE mode=%s LIMIT 1", (mode,))
        row = cursor.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))
    except Exception as exc:
        logging.warning(f"load_strategy_record({mode}) failed: {exc}")
        return None
    finally:
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


def record_fee_totals(
    strategy: str,
    exchange: str,
    fee_type: str,
    fee_usd: float,
    fee_asset: str | None,
    fee_asset_amount: float,
) -> None:
    """Accumulate fee totals per exchange/strategy for reporting."""
    return record_fee_totals_with_transaction(
        strategy,
        exchange,
        fee_type,
        fee_usd,
        fee_asset,
        fee_asset_amount,
        db_transaction,
    )


def record_fee_totals_with_transaction(
    strategy: str,
    exchange: str,
    fee_type: str,
    fee_usd: float,
    fee_asset: str | None,
    fee_asset_amount: float,
    transaction_ctx,
) -> None:
    """Accumulate fee totals per exchange/strategy for reporting."""
    try:
        fee_usd_val = float(fee_usd or 0.0)
    except (TypeError, ValueError):
        fee_usd_val = 0.0
    try:
        fee_asset_val = float(fee_asset_amount or 0.0)
    except (TypeError, ValueError):
        fee_asset_val = 0.0

    if abs(fee_usd_val) < 1e-12 and abs(fee_asset_val) < 1e-12:
        return

    strategy_key = (strategy or "unknown").strip().lower() or "unknown"
    exchange_key = (exchange or "unknown").strip().lower() or "unknown"
    fee_type_key = "sell" if fee_type == "sell" else "buy"
    asset_key = (fee_asset or ("USD" if fee_usd_val else "UNKNOWN")).strip().upper() or "UNKNOWN"

    now = datetime.utcnow()
    try:
        with transaction_ctx() as (cursor, _):
            cursor.execute(
                """
                INSERT INTO strategy_fee_totals
                    (exchange, strategy, fee_type, fee_asset, fee_usd, fee_asset_amount, last_updated)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    fee_usd = fee_usd + VALUES(fee_usd),
                    fee_asset_amount = fee_asset_amount + VALUES(fee_asset_amount),
                    last_updated = VALUES(last_updated)
                """,
                (
                    exchange_key,
                    strategy_key,
                    fee_type_key,
                    asset_key,
                    fee_usd_val,
                    fee_asset_val,
                    now,
                ),
            )
    except Exception as exc:
        logging.debug(f"record_fee_totals failed: {exc}")


def save_strategy_metadata(mode: str, metadata: dict, extra: dict | None = None) -> None:
    """Persist metadata_json alongside optional fields on strategy_state."""
    return save_strategy_metadata_with_transaction(mode, metadata, extra, db_transaction)


def save_strategy_metadata_with_transaction(mode: str, metadata: dict, extra: dict | None, transaction_ctx) -> None:
    """Persist metadata_json alongside optional fields on strategy_state."""
    setters = ["metadata_json=%s"]
    params = [json.dumps(metadata)]
    if extra:
        for key, value in extra.items():
            setters.append(f"{key}=%s")
            params.append(value)
    params.append(mode)
    try:
        with transaction_ctx() as (cursor, _):
            cursor.execute(
                f"UPDATE strategy_state SET {', '.join(setters)}, updated_at=NOW() WHERE mode=%s",
                tuple(params),
            )
    except Exception as exc:
        logging.error(f"save_strategy_metadata({mode}) failed: {exc}")
        raise


def record_rotation_event(
    *,
    executed_at: datetime,
    strategy_mode: str,
    from_asset: str,
    to_asset: str,
    notional_usd: float,
    cdc_status: str | None,
    delta_pct: float | None,
    reason: str | None,
    metadata: dict | None = None,
) -> None:
    """Insert a journal entry in strategy_rotation_log."""
    return record_rotation_event_with_transaction(
        executed_at=executed_at,
        strategy_mode=strategy_mode,
        from_asset=from_asset,
        to_asset=to_asset,
        notional_usd=notional_usd,
        cdc_status=cdc_status,
        delta_pct=delta_pct,
        reason=reason,
        metadata=metadata,
        transaction_ctx=db_transaction,
    )


def record_rotation_event_with_transaction(
    *,
    executed_at: datetime,
    strategy_mode: str,
    from_asset: str,
    to_asset: str,
    notional_usd: float,
    cdc_status: str | None,
    delta_pct: float | None,
    reason: str | None,
    metadata: dict | None = None,
    transaction_ctx,
) -> None:
    """Insert a journal entry in strategy_rotation_log."""
    try:
        with transaction_ctx() as (cursor, _):
            cursor.execute(
                """
                INSERT INTO strategy_rotation_log
                    (executed_at, strategy_mode, from_asset, to_asset, notional_usd,
                     cdc_status, delta_pct, reason, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    executed_at,
                    strategy_mode,
                    from_asset,
                    to_asset,
                    round(float(notional_usd or 0.0), 2),
                    cdc_status,
                    None if delta_pct is None else round(float(delta_pct), 6),
                    reason,
                    json.dumps(metadata or {}),
                ),
            )
    except Exception as exc:
        logging.error(f"record_rotation_event failed: {exc}")


def load_strategy_state(*, fail_on_error: bool = False):
    """Load CDC strategy state with graceful handling for legacy schemas."""
    return load_strategy_state_with_connection(get_db_connection, fail_on_error=fail_on_error)


def load_strategy_state_with_connection(get_connection, *, fail_on_error: bool = False):
    """Load CDC strategy state with graceful handling for legacy schemas."""
    defaults = {
        "last_cdc_status": None,
        "reserve_usdt": 0.0,
        "red_epoch_active": 0,
        "cdc_enabled": 1,
        "sell_percent": 50,
        "exchange": "binance",
        "sell_percent_binance": 50,
        "sell_percent_okx": 50,
        "okx_max_usdt": 0.0,
        "binance_max_usdt": 0.0,
        "half_sell_policy": "auto_proportional",
        "reserve_binance_usdt": 0.0,
        "reserve_okx_usdt": 0.0,
        "last_half_sell_at": None,
    }

    db = None
    cursor = None
    try:
        db = get_connection()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
        row = cursor.fetchone()
        if not row:
            return defaults
        columns = [desc[0] for desc in cursor.description]
        record = dict(zip(columns, row))
    except Exception as exc:
        if fail_on_error:
            raise
        logging.warning(f"load_strategy_state fallback: {exc}")
        return defaults
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if db:
                db.close()
        except Exception:
            pass

    def _to_int(val, default):
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _to_float(val, default):
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    sell_percent = _to_int(record.get("sell_percent"), defaults["sell_percent"])
    return {
        "last_cdc_status": record.get("last_cdc_status", defaults["last_cdc_status"]),
        "reserve_usdt": _to_float(record.get("reserve_usdt"), defaults["reserve_usdt"]),
        "red_epoch_active": _to_int(record.get("red_epoch_active"), defaults["red_epoch_active"]),
        "cdc_enabled": _to_int(record.get("cdc_enabled"), defaults["cdc_enabled"]),
        "sell_percent": sell_percent,
        "exchange": (record.get("exchange") or defaults["exchange"]),
        "sell_percent_binance": _to_int(record.get("sell_percent_binance"), sell_percent),
        "sell_percent_okx": _to_int(record.get("sell_percent_okx"), sell_percent),
        "okx_max_usdt": _to_float(record.get("okx_max_usdt"), defaults["okx_max_usdt"]),
        "binance_max_usdt": _to_float(record.get("binance_max_usdt"), defaults["binance_max_usdt"]),
        "half_sell_policy": str(record.get("half_sell_policy") or defaults["half_sell_policy"]),
        "reserve_binance_usdt": _to_float(record.get("reserve_binance_usdt"), defaults["reserve_binance_usdt"]),
        "reserve_okx_usdt": _to_float(record.get("reserve_okx_usdt"), defaults["reserve_okx_usdt"]),
        "last_half_sell_at": record.get("last_half_sell_at", defaults["last_half_sell_at"]),
    }


def save_strategy_state(patch: dict) -> None:
    """Upsert selected fields in strategy_state for mode='cdc_dca_v1'."""
    return save_strategy_state_with_connection(patch, get_db_connection)


def save_strategy_state_with_connection(patch: dict, get_connection) -> None:
    """Upsert selected fields in strategy_state for mode='cdc_dca_v1'."""
    allowed = ["last_cdc_status", "last_transition_at", "reserve_usdt", "red_epoch_active", "last_half_sell_at"]
    cols = ["mode"]
    values = ["cdc_dca_v1"]
    updates = []
    for key in allowed:
        if key in patch:
            cols.append(key)
            values.append(patch[key])
            updates.append(f"{key}=VALUES({key})")

    if len(cols) == 1:
        return

    db = None
    cursor = None
    try:
        db = get_connection()
        cursor = db.cursor()
        placeholders = ", ".join(["%s"] * len(cols))
        update_clause = ", ".join(updates)
        sql = (
            f"INSERT INTO strategy_state ({', '.join(cols)}) "
            f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_clause}"
        )
        cursor.execute(sql, tuple(values))
        db.commit()
    except Exception as exc:
        logging.warning(f"save_strategy_state failed: {exc}")
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if db:
                db.close()
        except Exception:
            pass
