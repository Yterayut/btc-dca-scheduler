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


def execute_half_sell_for_exchange_with_dependencies(
    now: datetime,
    exchange: str,
    pct: int,
    state: dict | None = None,
    context: dict | None = None,
    *,
    deps: dict,
) -> dict:
    """Execute exchange half-sell using injected dependencies."""
    ex = exchange.lower()
    pct = int(pct or 0)
    try:
        adapter = deps["get_adapter"](ex, testnet=deps["USE_TESTNET"], dry_run=deps["is_dry_run"]())
        if ex == "okx":
            try:
                maxu = float((state or {}).get("okx_max_usdt", 0) or 0)
                adapter = deps["OkxAdapter"](
                    testnet=deps["USE_TESTNET"],
                    dry_run=deps["is_dry_run"](),
                    max_usdt=maxu if maxu > 0 else None,
                )
            except Exception:
                pass

        if pct <= 0:
            payload = {
                "reason": "sell_percent_zero",
                "btc_free": 0,
                "step": "-",
                "min_notional": "-",
                "pct": pct,
                "exchange": ex,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_half_sell_skipped"](payload)
            return {"skipped": True, "reason": "sell_percent_zero", "exchange": ex, "pct": pct}

        balance = adapter.get_balance(asset="BTC")
        btc_free = float(balance.get("free") or 0)
        if btc_free <= 0:
            payload = {
                "reason": "no_balance",
                "btc_free": btc_free,
                "step": "-",
                "min_notional": "-",
                "pct": pct,
                "exchange": ex,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_half_sell_skipped"](payload)
            return {"skipped": True, "reason": "no_balance", "exchange": ex, "pct": pct}

        filters = deps["get_symbol_filters"]("BTCUSDT", exchange=ex)
        step = float(filters["stepSize"])
        min_qty = float(filters["minQty"])
        min_notional = float(filters["minNotional"])

        sell_target = btc_free * (pct / 100.0)
        qty = deps["adjust_qty_to_step"](sell_target, step)
        if qty < min_qty:
            payload = {
                "reason": "below_minQty",
                "btc_free": btc_free,
                "step": step,
                "min_notional": min_notional,
                "pct": pct,
                "exchange": ex,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_half_sell_skipped"](payload)
            return {"skipped": True, "reason": "below_minQty", "exchange": ex, "pct": pct}

        price = float(adapter.get_price())
        depth_ok, depth_info = deps["evaluate_depth_guard"](adapter, ex, price)
        if not depth_ok:
            payload = {
                "exchange": ex,
                "reason": depth_info.get("reason", "depth_guard"),
                "depth": depth_info,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("half_sell", payload)
            depth_info["skipped"] = True
            return {"skipped": True, "reason": depth_info.get("reason", "depth_guard"), "exchange": ex, "pct": pct, "detail": depth_info}

        twap_ok, twap_info = deps["evaluate_twap_guard"](adapter, ex, price)
        if not twap_ok:
            payload = {
                "exchange": ex,
                "reason": twap_info.get("reason", "twap_guard"),
                "twap": twap_info,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("half_sell", payload)
            twap_info["skipped"] = True
            return {"skipped": True, "reason": twap_info.get("reason", "twap_guard"), "exchange": ex, "pct": pct, "detail": twap_info}

        notional = qty * price
        cap_ok, cap_info = deps["evaluate_notional_cap"](ex, notional, state)
        if not cap_ok:
            payload = {
                "exchange": ex,
                "reason": "notional_cap",
                "cap": cap_info.get("cap"),
                "attempt": cap_info.get("attempt"),
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("half_sell", payload)
            return {"skipped": True, "reason": "notional_cap", "exchange": ex, "pct": pct, "detail": cap_info}

        ok, liquidity = deps["assess_liquidity"](adapter, ex, context=context)
        if not ok:
            payload = {
                "exchange": ex,
                "reason": liquidity.get("reason"),
                "spread_pct": liquidity.get("spread_pct"),
                "threshold_pct": liquidity.get("threshold_pct"),
                "expected_notional": notional,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("half_sell", payload)
            payload["skipped"] = True
            return {"skipped": True, "reason": liquidity.get("reason", "liquidity_guard"), "exchange": ex, "pct": pct}

        if notional < min_notional:
            payload = {
                "reason": "below_minNotional",
                "btc_free": btc_free,
                "step": step,
                "min_notional": min_notional,
                "pct": pct,
                "exchange": ex,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key", "cdc_status"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_half_sell_skipped"](payload)
            return {"skipped": True, "reason": "below_minNotional", "exchange": ex, "pct": pct}

        res = adapter.place_market_sell_qty(qty)
        order_id = res.order_id
        executed_qty = float(res.executed_qty)
        cummulative_quote_qty = float(res.cummulative_quote_qty)
        if executed_qty <= 0 or cummulative_quote_qty <= 0:
            raise ValueError("Sell order not filled or zero quantities")
        avg_price = cummulative_quote_qty / executed_qty if executed_qty else 0.0
        pnl_value, pnl_meta = deps["compute_realized_pnl"](ex, executed_qty, cummulative_quote_qty)
        fee_sell_usdt = float(getattr(res, "fee_usd", 0.0) or 0.0)
        fee_sell_asset = getattr(res, "fee_asset", None)
        fee_sell_asset_amount = float(getattr(res, "fee_asset_amount", 0.0) or 0.0)

        with deps["db_transaction"]() as (cursor, _):
            cursor.execute(
                """
                INSERT INTO sell_history (sell_time, symbol, btc_quantity, usdt_received, price, order_id, sell_percent, note, exchange, fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "BTCUSDT",
                    executed_qty,
                    cummulative_quote_qty,
                    avg_price,
                    order_id,
                    pct,
                    "sell via CDC",
                    ex,
                    fee_sell_usdt if fee_sell_usdt is not None else None,
                    fee_sell_asset,
                    fee_sell_asset_amount if fee_sell_asset_amount is not None else None,
                ),
            )

        notify_payload = {
            "btc_qty": executed_qty,
            "price": avg_price,
            "usdt": cummulative_quote_qty,
            "order_id": order_id,
            "pct": pct,
            "exchange": ex,
            "timestamp": now,
        }
        if context:
            for key in ("request_id", "dedupe_key", "cdc_status"):
                val = context.get(key)
                if val:
                    notify_payload[key] = val
        deps["notify_half_sell_executed"](notify_payload)
        deps["record_fee_totals"]("cdc_half_sell", ex, "sell", fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount)

        try:
            meta = dict(pnl_meta)
            meta.update(
                {
                    "order_id": order_id,
                    "pct": pct,
                    "cdc_status": context.get("cdc_status") if context else None,
                    "request_id": context.get("request_id") if context else None,
                    "dedupe_key": context.get("dedupe_key") if context else None,
                }
            )
            deps["log_compliance_event"](now, "sell", ex, cummulative_quote_qty, executed_qty, avg_price, pnl_value, metadata=meta)
            if abs(pnl_value) >= deps["ANOMALY_PNL_THRESHOLD_USDT"]:
                deps["notify_security_alert"](
                    "Realized PnL exceeded threshold",
                    {
                        "exchange": ex.upper(),
                        "pnl_usdt": f"{pnl_value:,.2f}",
                        "threshold": f"{deps['ANOMALY_PNL_THRESHOLD_USDT']:,.2f}",
                        "order_id": order_id,
                    },
                )
        except Exception:
            logging.debug("Compliance log skipped for half-sell", exc_info=True)
        return {"executed": True, "exchange": ex, "qty": executed_qty, "usdt": cummulative_quote_qty, "price": avg_price, "order_id": order_id, "pct": pct}
    except Exception as exc:
        logging.error("Half-sell %s error: %s", ex, exc)
        deps["send_line_message"](f"❌ Half-sell {ex.upper()} error: {exc}")
        return {"error": str(exc), "exchange": ex, "pct": pct}


def execute_reserve_buy_with_dependencies(
    now: datetime,
    context: dict | None = None,
    *,
    deps: dict,
) -> dict:
    """Use global reserve balance to buy BTC using injected dependencies."""
    try:
        state = deps["load_strategy_state"]()
        reserve = float(state.get("reserve_usdt", 0) or 0)
        if reserve <= 0:
            return {"skipped": True, "reason": "no_reserve"}

        current_state = deps["load_strategy_state"]()
        exchange = current_state.get("exchange", "binance")
        adapter = deps["get_adapter"](exchange, testnet=deps["USE_TESTNET"], dry_run=deps["is_dry_run"]())
        balance = adapter.get_balance(asset="USDT")
        available_usdt = float(balance.get("free") or 0)
        spend = min(available_usdt, reserve)
        filters = deps["get_symbol_filters"]("BTCUSDT", exchange=exchange)
        min_notional = float(filters["minNotional"])
        if spend < min_notional:
            payload = {"spend": spend, "min_notional": min_notional, "reserve": reserve, "timestamp": now}
            if context:
                for key in ("request_id", "dedupe_key"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_reserve_buy_skipped_min_notional"](payload)
            return {"skipped": True, "reason": "below_minNotional", "spend": spend}

        price = float(adapter.get_price())
        depth_ok, depth_info = deps["evaluate_depth_guard"](adapter, exchange, price)
        if not depth_ok:
            payload = {
                "exchange": exchange,
                "reason": depth_info.get("reason", "depth_guard"),
                "depth": depth_info,
                "expected_notional": spend,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("reserve_buy", payload)
            return {"skipped": True, "reason": depth_info.get("reason", "depth_guard"), "exchange": exchange, "detail": depth_info}

        twap_ok, twap_info = deps["evaluate_twap_guard"](adapter, exchange, price)
        if not twap_ok:
            payload = {
                "exchange": exchange,
                "reason": twap_info.get("reason", "twap_guard"),
                "twap": twap_info,
                "expected_notional": spend,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("reserve_buy", payload)
            return {"skipped": True, "reason": twap_info.get("reason", "twap_guard"), "exchange": exchange, "detail": twap_info}

        cap_ok, cap_info = deps["evaluate_notional_cap"](exchange, spend, state)
        if not cap_ok:
            payload = {
                "exchange": exchange,
                "reason": "notional_cap",
                "cap": cap_info.get("cap"),
                "attempt": cap_info.get("attempt"),
                "timestamp": now,
            }
            deps["notify_liquidity_blocked"]("reserve_buy", payload)
            return {"skipped": True, "reason": "notional_cap", "exchange": exchange, "detail": cap_info}

        ok, liquidity = deps["assess_liquidity"](adapter, exchange, context=context)
        if not ok:
            payload = {
                "exchange": exchange,
                "reason": liquidity.get("reason"),
                "spread_pct": liquidity.get("spread_pct"),
                "threshold_pct": liquidity.get("threshold_pct"),
                "expected_notional": spend,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("reserve_buy", payload)
            return {"skipped": True, "reason": liquidity.get("reason", "liquidity_guard"), "exchange": exchange}

        res = adapter.place_market_buy_quote(spend)
        order_id = res.order_id
        executed_qty = float(res.executed_qty)
        cummulative_quote_qty = float(res.cummulative_quote_qty)
        fee_buy_usdt = float(getattr(res, "fee_usd", 0.0) or 0.0)
        fee_buy_asset = getattr(res, "fee_asset", None)
        fee_buy_asset_amount = float(getattr(res, "fee_asset_amount", 0.0) or 0.0)
        if executed_qty <= 0 or cummulative_quote_qty <= 0:
            raise ValueError("Reserve buy not filled or zero quantities")
        avg_price = cummulative_quote_qty / executed_qty

        with deps["db_transaction"]() as (cursor, _):
            cursor.execute(
                """
                INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cummulative_quote_qty,
                    executed_qty,
                    avg_price,
                    order_id,
                    None,
                    exchange,
                    fee_buy_usdt if fee_buy_usdt is not None else None,
                    fee_buy_asset,
                    fee_buy_asset_amount if fee_buy_asset_amount is not None else None,
                ),
            )
            cursor.execute("UPDATE strategy_state SET reserve_usdt = GREATEST(reserve_usdt - %s, 0) WHERE mode='cdc_dca_v1'", (cummulative_quote_qty,))
            cursor.execute("SELECT reserve_usdt FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
            new_reserve = float(cursor.fetchone()[0] or 0)
            try:
                cursor.execute(
                    """
                    INSERT INTO reserve_log (event_time, change_usdt, reserve_after, reason, note)
                    VALUES (NOW(), %s, %s, %s, %s)
                    """,
                    (-cummulative_quote_qty, new_reserve, "reserve_buy", "Auto reserve buy on CDC GREEN"),
                )
            except Exception:
                pass

        notify_payload = {
            "spend": cummulative_quote_qty,
            "btc_qty": executed_qty,
            "price": avg_price,
            "reserve_left": new_reserve,
            "order_id": order_id,
            "exchange": exchange,
            "timestamp": now,
        }
        if context:
            for key in ("request_id", "dedupe_key"):
                val = context.get(key)
                if val:
                    notify_payload[key] = val
        if context and context.get("cdc_status"):
            notify_payload["cdc_status"] = context.get("cdc_status")
        deps["notify_reserve_buy_executed"](notify_payload)
        deps["record_fee_totals"]("cdc_reserve_buy", exchange, "buy", fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)

        try:
            meta = {
                "reserve_after": new_reserve,
                "cdc_status": context.get("cdc_status") if context else None,
                "request_id": context.get("request_id") if context else None,
                "dedupe_key": context.get("dedupe_key") if context else None,
                "mode": "global",
            }
            deps["log_compliance_event"](now, "reserve_buy", exchange, cummulative_quote_qty, executed_qty, avg_price, 0.0, metadata=meta)
            if cummulative_quote_qty >= deps["ANOMALY_NOTIONAL_THRESHOLD_USDT"]:
                deps["notify_security_alert"](
                    "High notional reserve deployment",
                    {
                        "exchange": exchange.upper(),
                        "notional": f"{cummulative_quote_qty:,.2f} USDT",
                        "threshold": f"{deps['ANOMALY_NOTIONAL_THRESHOLD_USDT']:,.2f} USDT",
                        "mode": "global",
                    },
                )
        except Exception:
            logging.debug("Compliance log skipped for reserve buy", exc_info=True)
        return {"executed": True, "spend": cummulative_quote_qty, "qty": executed_qty, "price": avg_price, "order_id": order_id}
    except Exception as exc:
        logging.error("Reserve buy error: %s", exc)
        deps["send_line_message"](f"❌ Reserve buy error: {exc}")
        return {"error": str(exc)}


def execute_reserve_buy_exchange_with_dependencies(
    now: datetime,
    exchange: str,
    context: dict | None = None,
    *,
    deps: dict,
) -> dict:
    """Use per-exchange reserve to buy BTC on a specific exchange."""
    try:
        state = deps["load_strategy_state"]()
        reserve = float(state.get(f"reserve_{exchange}_usdt", 0) or 0)
        if reserve <= 0:
            return {"skipped": True, "reason": "no_reserve", "exchange": exchange}

        adapter = deps["get_adapter"](exchange, testnet=deps["USE_TESTNET"], dry_run=deps["is_dry_run"]())
        if exchange == "okx":
            maxu = float(state.get("okx_max_usdt", 0) or 0)
            adapter = deps["OkxAdapter"](testnet=deps["USE_TESTNET"], dry_run=deps["is_dry_run"](), max_usdt=maxu if maxu > 0 else None)
        balance = adapter.get_balance("USDT")
        available_usdt = float(balance.get("free") or 0)
        spend = min(available_usdt, reserve)
        filters = deps["get_symbol_filters"]("BTCUSDT", exchange=exchange)
        min_notional = float(filters.get("minNotional") or 10.0)
        if spend < min_notional:
            payload = {"spend": spend, "min_notional": min_notional, "reserve": reserve, "exchange": exchange, "timestamp": now}
            if context:
                for key in ("request_id", "dedupe_key"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_reserve_buy_skipped_min_notional"](payload)
            return {"skipped": True, "reason": "below_minNotional", "exchange": exchange, "spend": spend}

        price = float(adapter.get_price())
        depth_ok, depth_info = deps["evaluate_depth_guard"](adapter, exchange, price)
        if not depth_ok:
            payload = {
                "exchange": exchange,
                "reason": depth_info.get("reason", "depth_guard"),
                "depth": depth_info,
                "expected_notional": spend,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("reserve_buy", payload)
            return {"skipped": True, "reason": depth_info.get("reason", "depth_guard"), "exchange": exchange, "detail": depth_info}

        twap_ok, twap_info = deps["evaluate_twap_guard"](adapter, exchange, price)
        if not twap_ok:
            payload = {
                "exchange": exchange,
                "reason": twap_info.get("reason", "twap_guard"),
                "twap": twap_info,
                "expected_notional": spend,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("reserve_buy", payload)
            return {"skipped": True, "reason": twap_info.get("reason", "twap_guard"), "exchange": exchange, "detail": twap_info}

        cap_ok, cap_info = deps["evaluate_notional_cap"](exchange, spend, state)
        if not cap_ok:
            payload = {
                "exchange": exchange,
                "reason": "notional_cap",
                "cap": cap_info.get("cap"),
                "attempt": cap_info.get("attempt"),
                "timestamp": now,
            }
            deps["notify_liquidity_blocked"]("reserve_buy", payload)
            return {"skipped": True, "reason": "notional_cap", "exchange": exchange, "detail": cap_info}

        ok, liquidity = deps["assess_liquidity"](adapter, exchange, context=context)
        if not ok:
            payload = {
                "exchange": exchange,
                "reason": liquidity.get("reason"),
                "spread_pct": liquidity.get("spread_pct"),
                "threshold_pct": liquidity.get("threshold_pct"),
                "expected_notional": spend,
                "timestamp": now,
            }
            if context:
                for key in ("request_id", "dedupe_key"):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            deps["notify_liquidity_blocked"]("reserve_buy", payload)
            return {"skipped": True, "reason": liquidity.get("reason", "liquidity_guard"), "exchange": exchange}

        res = adapter.place_market_buy_quote(spend)
        executed_qty = float(res.executed_qty)
        cummulative_quote_qty = float(res.cummulative_quote_qty)
        avg_price = float(res.avg_price)
        fee_buy_usdt = float(getattr(res, "fee_usd", 0.0) or 0.0)
        fee_buy_asset = getattr(res, "fee_asset", None)
        fee_buy_asset_amount = float(getattr(res, "fee_asset_amount", 0.0) or 0.0)
        if executed_qty <= 0 or cummulative_quote_qty <= 0:
            raise ValueError("not filled")

        with deps["db_transaction"]() as (cursor, _):
            cursor.execute(
                """
                INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cummulative_quote_qty,
                    executed_qty,
                    avg_price,
                    res.order_id,
                    None,
                    exchange,
                    fee_buy_usdt if fee_buy_usdt is not None else None,
                    fee_buy_asset,
                    fee_buy_asset_amount if fee_buy_asset_amount is not None else None,
                ),
            )
            if exchange == "binance":
                cursor.execute("UPDATE strategy_state SET reserve_binance_usdt = GREATEST(reserve_binance_usdt - %s, 0) WHERE mode='cdc_dca_v1'", (cummulative_quote_qty,))
            else:
                cursor.execute("UPDATE strategy_state SET reserve_okx_usdt = GREATEST(reserve_okx_usdt - %s, 0) WHERE mode='cdc_dca_v1'", (cummulative_quote_qty,))

        reserve_left = max(0.0, reserve - cummulative_quote_qty)
        notify_payload = {
            "spend": cummulative_quote_qty,
            "btc_qty": executed_qty,
            "price": avg_price,
            "reserve_left": reserve_left,
            "order_id": res.order_id,
            "exchange": exchange,
            "timestamp": now,
        }
        if context:
            for key in ("request_id", "dedupe_key"):
                val = context.get(key)
                if val:
                    notify_payload[key] = val
        if context and context.get("cdc_status"):
            notify_payload["cdc_status"] = context.get("cdc_status")
        deps["notify_reserve_buy_executed"](notify_payload)
        deps["record_fee_totals"]("cdc_reserve_buy", exchange, "buy", fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)

        try:
            meta = {
                "reserve_before": reserve,
                "reserve_after": reserve_left,
                "cdc_status": context.get("cdc_status") if context else None,
                "request_id": context.get("request_id") if context else None,
                "dedupe_key": context.get("dedupe_key") if context else None,
                "mode": "per_exchange",
            }
            deps["log_compliance_event"](now, "reserve_buy", exchange, cummulative_quote_qty, executed_qty, avg_price, 0.0, metadata=meta)
            if cummulative_quote_qty >= deps["ANOMALY_NOTIONAL_THRESHOLD_USDT"]:
                deps["notify_security_alert"](
                    "High notional reserve deployment",
                    {
                        "exchange": exchange.upper(),
                        "notional": f"{cummulative_quote_qty:,.2f} USDT",
                        "threshold": f"{deps['ANOMALY_NOTIONAL_THRESHOLD_USDT']:,.2f} USDT",
                        "mode": "per_exchange",
                    },
                )
        except Exception:
            logging.debug("Compliance log skipped for reserve buy exchange", exc_info=True)
        return {"executed": True, "exchange": exchange, "spend": cummulative_quote_qty, "qty": executed_qty, "price": avg_price}
    except Exception as exc:
        logging.error("Reserve buy %s error: %s", exchange, exc)
        deps["send_line_message"](f"❌ Reserve buy {exchange.upper()} error: {exc}")
        return {"error": str(exc), "exchange": exchange}
