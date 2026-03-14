"""Helpers for FIFO lot tracking and realized PnL calculation."""

from __future__ import annotations

import logging


def load_fifo_open_lots_with_transaction(exchange: str, transaction_ctx) -> list[dict]:
    lots: list[dict] = []
    try:
        with transaction_ctx() as (cursor, _):
            cursor.execute(
                """
                SELECT purchase_time, btc_quantity, usdt_amount
                FROM purchase_history
                WHERE exchange = %s
                ORDER BY purchase_time ASC
                """,
                (exchange,),
            )
            purchases = cursor.fetchall()
            cursor.execute(
                """
                SELECT btc_quantity
                FROM sell_history
                WHERE exchange = %s
                ORDER BY sell_time ASC
                """,
                (exchange,),
            )
            sells = cursor.fetchall()
    except Exception as exc:
        logging.warning(f"FIFO lot load failed for {exchange}: {exc}")
        purchases = []
        sells = []

    for purchase_time, qty, notional in purchases:
        qty_f = float(qty or 0.0)
        if qty_f <= 0:
            continue
        notional_f = float(notional or 0.0)
        cost_per_unit = notional_f / qty_f if qty_f else 0.0
        lots.append(
            {
                "qty": qty_f,
                "cost": cost_per_unit,
                "timestamp": purchase_time,
            }
        )

    for (sell_qty,) in sells:
        remaining = float(sell_qty or 0.0)
        idx = 0
        while remaining > 0 and idx < len(lots):
            lot = lots[idx]
            available = float(lot.get("qty") or 0.0)
            if available <= 0:
                idx += 1
                continue
            consume = min(available, remaining)
            lot["qty"] = max(0.0, available - consume)
            remaining -= consume
            if lot["qty"] <= 1e-9:
                lot["qty"] = 0.0
            else:
                idx += 1
    return [lot for lot in lots if lot.get("qty", 0.0) > 1e-9]


def compute_realized_pnl_with_transaction(exchange: str, sell_qty: float, proceeds: float, transaction_ctx) -> tuple[float, dict]:
    lots = load_fifo_open_lots_with_transaction(exchange, transaction_ctx)
    remaining = float(sell_qty or 0.0)
    cost = 0.0
    contributions: list[dict] = []
    for lot in lots:
        if remaining <= 0:
            break
        available = float(lot.get("qty") or 0.0)
        if available <= 0:
            continue
        consume = min(available, remaining)
        cost += consume * float(lot.get("cost") or 0.0)
        contributions.append(
            {
                "qty": consume,
                "cost_per_unit": float(lot.get("cost") or 0.0),
                "source_time": str(lot.get("timestamp")) if lot.get("timestamp") else None,
            }
        )
        remaining -= consume
    metadata = {
        "method": "fifo",
        "consumed_qty": float(sell_qty) - remaining,
        "remaining_qty": max(0.0, remaining),
        "lots_used": len(contributions),
        "lots_total": len(lots),
        "contributions": contributions[:5],
    }
    metadata["cost_basis"] = cost
    metadata["proceeds"] = float(proceeds)
    pnl = float(proceeds) - cost
    if remaining > 1e-6:
        metadata["note"] = "Sold more BTC than available FIFO lots; excess treated as zero-cost"
    return pnl, metadata
