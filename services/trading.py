"""Trading and reserve helper functions extracted from main runtime."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime


def increment_reserve_with_transaction(
    amount: float,
    *,
    reason: str | None = None,
    note: str | None = None,
    transaction_ctx,
) -> float:
    """Increase global reserve_usdt by amount and return new value."""
    try:
        amt = float(amount or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if amt <= 0:
        return 0.0
    try:
        with transaction_ctx() as (cursor, _):
            cursor.execute("UPDATE strategy_state SET reserve_usdt = reserve_usdt + %s WHERE mode='cdc_dca_v1'", (amt,))
            cursor.execute("SELECT reserve_usdt FROM strategy_state WHERE mode='cdc_dca_v1'")
            val = float(cursor.fetchone()[0] or 0.0)
            log_reason = reason or "weekly_skip"
            log_note = note or "Skipped weekly DCA due to CDC RED"
            try:
                cursor.execute(
                    """
                    INSERT INTO reserve_log (event_time, change_usdt, reserve_after, reason, note)
                    VALUES (NOW(), %s, %s, %s, %s)
                    """,
                    (amt, val, log_reason, log_note),
                )
            except Exception:
                pass
        return val
    except Exception:
        return 0.0


def increment_reserve_exchange_with_transaction(
    exchange: str,
    amount: float,
    *,
    reason: str | None = None,
    note: str | None = None,
    transaction_ctx,
) -> float:
    """Increase per-exchange reserve and return new value."""
    try:
        amt = float(amount or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if amt <= 0:
        return 0.0
    try:
        with transaction_ctx() as (cursor, _):
            if exchange == "binance":
                cursor.execute("UPDATE strategy_state SET reserve_binance_usdt = reserve_binance_usdt + %s WHERE mode='cdc_dca_v1'", (amt,))
                cursor.execute("SELECT reserve_binance_usdt FROM strategy_state WHERE mode='cdc_dca_v1'")
            else:
                cursor.execute("UPDATE strategy_state SET reserve_okx_usdt = reserve_okx_usdt + %s WHERE mode='cdc_dca_v1'", (amt,))
                cursor.execute("SELECT reserve_okx_usdt FROM strategy_state WHERE mode='cdc_dca_v1'")
            val = float(cursor.fetchone()[0] or 0.0)
            log_reason = reason or f"weekly_skip_{exchange}"
            log_note = note or f"Skipped weekly DCA on {exchange.upper()} due to CDC RED"
            try:
                cursor.execute(
                    """
                    INSERT INTO reserve_log (event_time, change_usdt, reserve_after, reason, note)
                    VALUES (NOW(), %s, %s, %s, %s)
                    """,
                    (amt, val, log_reason, log_note),
                )
            except Exception:
                pass
        return val
    except Exception:
        return 0.0


def purchase_on_exchange_with_dependencies(
    now: datetime,
    exchange: str,
    amount: float,
    schedule_id: int | None,
    context: dict | None,
    *,
    deps: dict,
) -> dict:
    """Place market buy on a specific exchange using injected dependencies."""
    try:
        state = deps["load_strategy_state"]()
        adapter = deps["get_adapter"](exchange, testnet=deps["USE_TESTNET"], dry_run=deps["is_dry_run"]())
        if exchange == "okx":
            maxu = float(state.get("okx_max_usdt", 0) or 0)
            adapter = deps["OkxAdapter"](testnet=deps["USE_TESTNET"], dry_run=deps["is_dry_run"](), max_usdt=maxu if maxu > 0 else None)
        skip_liquidity_guards = str((context or {}).get("cdc_status") or "").lower() == "okx_pure_dca"
        price = float(adapter.get_price())
        depth_ok, depth_info = deps["evaluate_depth_guard"](adapter, exchange, price)
        if not depth_ok and not skip_liquidity_guards:
            payload = {
                "exchange": exchange,
                "reason": depth_info.get("reason", "depth_guard"),
                "depth": depth_info,
                "expected_notional": amount,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            logging.warning(
                "DCA buy liquidity block (depth) exchange=%s schedule_id=%s amount=%.2f reason=%s detail=%s",
                exchange, schedule_id, float(amount or 0.0), depth_info.get("reason", "depth_guard"), depth_info,
            )
            deps["notify_liquidity_blocked"]("dca_buy", payload)
            return {"skipped": True, "reason": depth_info.get("reason", "depth_guard"), "exchange": exchange, "detail": depth_info}
        if not depth_ok and skip_liquidity_guards:
            logging.warning(
                "Bypassing depth guard for okx_pure_dca exchange=%s schedule_id=%s amount=%.2f detail=%s",
                exchange, schedule_id, float(amount or 0.0), depth_info,
            )
        twap_ok, twap_info = deps["evaluate_twap_guard"](adapter, exchange, price)
        if not twap_ok and not skip_liquidity_guards:
            payload = {
                "exchange": exchange,
                "reason": twap_info.get("reason", "twap_guard"),
                "twap": twap_info,
                "expected_notional": amount,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            logging.warning(
                "DCA buy liquidity block (twap) exchange=%s schedule_id=%s amount=%.2f reason=%s detail=%s",
                exchange, schedule_id, float(amount or 0.0), twap_info.get("reason", "twap_guard"), twap_info,
            )
            deps["notify_liquidity_blocked"]("dca_buy", payload)
            return {"skipped": True, "reason": twap_info.get("reason", "twap_guard"), "exchange": exchange, "detail": twap_info}
        if not twap_ok and skip_liquidity_guards:
            logging.warning(
                "Bypassing twap guard for okx_pure_dca exchange=%s schedule_id=%s amount=%.2f detail=%s",
                exchange, schedule_id, float(amount or 0.0), twap_info,
            )
        cap_ok, cap_info = deps["evaluate_notional_cap"](exchange, amount, state)
        if not cap_ok and not skip_liquidity_guards:
            payload = {
                "exchange": exchange,
                "reason": "notional_cap",
                "cap": cap_info.get("cap"),
                "attempt": cap_info.get("attempt"),
                "timestamp": now,
            }
            logging.warning(
                "DCA buy liquidity block (notional_cap) exchange=%s schedule_id=%s amount=%.2f cap=%s attempt=%s",
                exchange, schedule_id, float(amount or 0.0), cap_info.get("cap"), cap_info.get("attempt"),
            )
            deps["notify_liquidity_blocked"]("dca_buy", payload)
            return {"skipped": True, "reason": "notional_cap", "exchange": exchange, "detail": cap_info}
        if not cap_ok and skip_liquidity_guards:
            logging.warning(
                "Bypassing notional cap for okx_pure_dca exchange=%s schedule_id=%s amount=%.2f detail=%s",
                exchange, schedule_id, float(amount or 0.0), cap_info,
            )

        pre_btc = None
        pre_quote = None
        quote_asset = "THB" if exchange == "bitkub" else "USDT"
        if exchange == "bitkub":
            try:
                pre_btc = float((adapter.get_balance("BTC") or {}).get("free") or 0.0)
                pre_quote = float((adapter.get_balance(quote_asset) or {}).get("free") or 0.0)
            except Exception as bal_exc:
                logging.warning("Bitkub pre-balance snapshot failed: %s", bal_exc)

        res = adapter.place_market_buy_quote(amount)
        ex_qty = float(res.executed_qty)
        cqq = float(res.cummulative_quote_qty)
        avg = float(res.avg_price)
        order_id_raw = res.order_id
        order_id_db = None
        try:
            if order_id_raw is not None and str(order_id_raw).strip() != "":
                order_id_db = int(str(order_id_raw))
        except Exception:
            order_id_db = None
            logging.warning("Non-numeric order_id from %s adapter: %s (store NULL in purchase_history)", exchange, order_id_raw)
        fee_buy_usdt = float(getattr(res, "fee_usd", 0.0) or 0.0)
        fee_buy_asset = getattr(res, "fee_asset", None)
        fee_buy_asset_amount = float(getattr(res, "fee_asset_amount", 0.0) or 0.0)

        if exchange == "bitkub":
            def _apply_exec_info(info: dict | None, source: str) -> bool:
                nonlocal ex_qty, cqq, avg, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount
                if not isinstance(info, dict):
                    return False
                exec_qty = float(info.get("qty") or 0.0)
                exec_avg = float(info.get("avg_price") or 0.0)
                exec_spent = float(info.get("quote_spent") or 0.0)
                exec_fee = float(info.get("fee_quote") or 0.0)
                if exec_qty <= 0 or exec_avg <= 0:
                    return False
                ex_qty = exec_qty
                avg = exec_avg
                if exec_spent > 0:
                    cqq = exec_spent
                if exec_fee > 0:
                    fee_buy_usdt = exec_fee
                    fee_buy_asset = quote_asset
                    fee_buy_asset_amount = exec_fee
                logging.info(
                    "Bitkub fill confirmed source=%s qty=%.8f %s=%.8f avg=%.2f schedule=%s order=%s",
                    source, ex_qty, quote_asset, cqq, avg, schedule_id, order_id_raw,
                )
                return True

            settle_timeout = max(float(os.getenv("BITKUB_SETTLE_TIMEOUT_SEC", "10")), 1.0)
            settle_sleep = max(float(os.getenv("BITKUB_SETTLE_POLL_SEC", "0.8")), 0.2)
            settle_deadline = time.time() + settle_timeout
            attempts = 0

            while (ex_qty <= 0 or cqq <= 0) and time.time() <= settle_deadline:
                attempts += 1
                if order_id_raw not in (None, ""):
                    try:
                        info = adapter.get_order_execution_symbol(adapter.symbol(), order_id_raw, side="buy", retries=1, retry_sleep_sec=0.2)
                        if _apply_exec_info(info, "order_info"):
                            break
                    except Exception as order_info_exc:
                        logging.warning("Bitkub order-info lookup failed (attempt=%s): %s", attempts, order_info_exc)
                    try:
                        info = adapter.get_order_execution_from_history_symbol(adapter.symbol(), order_id_raw, limit=50)
                        if _apply_exec_info(info, "order_history"):
                            break
                    except Exception as hist_exc:
                        logging.warning("Bitkub order-history lookup failed (attempt=%s): %s", attempts, hist_exc)
                try:
                    post_btc = float((adapter.get_balance("BTC") or {}).get("free") or 0.0)
                    post_quote = float((adapter.get_balance(quote_asset) or {}).get("free") or 0.0)
                    if pre_btc is not None and pre_quote is not None:
                        delta_btc = max(post_btc - pre_btc, 0.0)
                        delta_quote = max(pre_quote - post_quote, 0.0)
                        if delta_btc > 0 and delta_quote > 0:
                            ex_qty = delta_btc
                            cqq = delta_quote
                            avg = (cqq / ex_qty) if ex_qty > 0 else avg
                            logging.warning(
                                "Bitkub fill inferred from balance delta: qty=%.8f %s=%.8f schedule=%s attempts=%s",
                                ex_qty, quote_asset, cqq, schedule_id, attempts,
                            )
                            break
                except Exception as infer_exc:
                    logging.warning("Bitkub post-balance infer failed (attempt=%s): %s", attempts, infer_exc)
                if ex_qty <= 0 or cqq <= 0:
                    time.sleep(settle_sleep)

        if ex_qty <= 0 or cqq <= 0:
            raise ValueError("not filled")
        with deps["db_transaction"]() as (cursor, _):
            cursor.execute(
                """
                INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cqq, ex_qty, avg, order_id_db, schedule_id, exchange,
                    fee_buy_usdt if fee_buy_usdt is not None else None,
                    fee_buy_asset,
                    fee_buy_asset_amount if fee_buy_asset_amount is not None else None,
                ),
            )
        try:
            notify_payload = {
                "usdt": cqq,
                "quote_amount": cqq,
                "quote_asset": quote_asset,
                "btc_qty": ex_qty,
                "price": avg,
                "schedule_id": schedule_id,
                "order_id": order_id_raw,
                "exchange": exchange,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        notify_payload[key] = val
            deps["_attach_holdings_snapshot"](notify_payload, exchange, assets=("BTC", quote_asset), force_refresh=True)
            sent = deps["notify_weekly_dca_buy"](notify_payload)
            if sent:
                logging.info("Weekly DCA notify sent (%s) schedule=%s order=%s amount=%.2f %s", exchange, schedule_id, order_id_raw, cqq, quote_asset)
            else:
                logging.error("Weekly DCA notify failed (%s) schedule=%s order=%s amount=%.2f %s", exchange, schedule_id, order_id_raw, cqq, quote_asset)
        except Exception as notify_exc:
            logging.exception("Weekly DCA notify exception (%s) schedule=%s order=%s: %s", exchange, schedule_id, order_id_raw, notify_exc)

        deps["record_fee_totals"]("cdc_weekly_dca", exchange, "buy", fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
        try:
            meta = {"schedule_id": schedule_id, "order_id": order_id_raw}
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        meta[key] = val
            deps["log_compliance_event"](now, "buy", exchange, cqq, ex_qty, avg, 0.0, metadata=meta)
            if exchange in ("binance", "okx") and cqq >= deps["ANOMALY_NOTIONAL_THRESHOLD_USDT"]:
                deps["notify_security_alert"](
                    "High notional DCA buy",
                    {
                        "exchange": exchange.upper(),
                        "notional": f"{cqq:,.2f} USDT",
                        "threshold": f"{deps['ANOMALY_NOTIONAL_THRESHOLD_USDT']:,.2f} USDT",
                        "order_id": order_id_raw,
                    },
                )
        except Exception:
            logging.debug("Compliance log skipped for buy", exc_info=True)
        result_payload = {
            "executed": True,
            "exchange": exchange,
            "qty": ex_qty,
            "usdt": cqq,
            "quote_amount": cqq,
            "quote_asset": quote_asset,
            "price": avg,
            "order_id": order_id_raw,
        }
        if context:
            for key in ("request_id", "dedupe_key", "cdc_status"):
                val = context.get(key)
                if val:
                    result_payload[key] = val
        return result_payload
    except Exception as exc:
        logging.exception(
            "purchase_on_exchange failed exchange=%s schedule_id=%s amount=%.8f: %s",
            exchange, schedule_id, float(amount or 0.0), exc,
        )
        deps["send_line_message"](f"❌ Weekly DCA {exchange.upper()} error: {exc}")
        return {"error": str(exc), "exchange": exchange}
