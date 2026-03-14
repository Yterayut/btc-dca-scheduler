"""S4 status data assembly helpers."""

from __future__ import annotations

import json
import os


def build_s4_status_data_with_dependencies(*, deps: dict) -> dict:
    """Build the S4 status payload for web rendering."""
    data: dict = {
        "active_asset": "UNKNOWN",
        "holding_asset": "UNKNOWN",
        "cdc_status": "N/A",
        "signal_source": "N/A",
        "signal_time": "N/A",
        "exchange": "okx",
        "portfolio": {},
        "gates": {},
        "last_status": {},
        "last_error": {},
        "last_rotation": {},
        "shadow_swap": {"count_90d": 0, "last": {}, "recent": []},
        "signal_layers": {
            "eod": {},
            "runtime": {},
            "mismatch": False,
            "mismatch_streak_days": 0,
            "mismatch_severity": "match",
        },
        "why_not_flip": {},
    }
    with deps["get_db_cursor"]() as (cursor, _):
        cursor.execute("SELECT * FROM strategy_state WHERE mode='s4_multi_leg' LIMIT 1")
        row = cursor.fetchone()
        if not row:
            data["error"] = "No S4 strategy state found."
            return data
        cols = [d[0] for d in cursor.description]
        record = dict(zip(cols, row))

        metadata_raw = record.get("metadata_json")
        metadata = {}
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw
            except Exception:
                metadata = {}

        runtime = deps["_normalize_s4_runtime_aliases"](metadata.get("runtime") or {})
        config = metadata.get("config") or {}
        confirm_days = int(os.getenv("S4_CONFIRM_DAYS", "2") or 2)
        holding_asset = runtime.get("holding_asset") or runtime.get("active_asset") or "UNKNOWN"
        data["exchange"] = str(config.get("exchange") or "okx").lower()
        data["active_asset"] = holding_asset
        data["holding_asset"] = holding_asset
        data["signal_target_asset"] = runtime.get("signal_target_asset") or holding_asset
        data["cdc_status"] = str(runtime.get("last_cdc_status") or "N/A").upper()
        data["signal_source"] = runtime.get("signal_source") or "N/A"
        data["signal_time"] = runtime.get("last_signal_at") or "N/A"
        data["signal_layers"]["runtime"] = {
            "layer": "runtime_production",
            "runtime_ts_utc": runtime.get("last_signal_at") or "",
            "cdc_status_runtime": str(runtime.get("last_cdc_status") or "").lower(),
            "active_asset_runtime": holding_asset,
            "holding_asset_runtime": holding_asset,
            "signal_target_asset_runtime": runtime.get("signal_target_asset") or holding_asset,
            "signal_source_runtime": runtime.get("signal_source") or "",
            "mismatch_streak_event": str(runtime.get("mismatch_streak_event") or ""),
            "last_confirmed_status": str(runtime.get("last_confirmed_status") or "").lower(),
            "confirm_progress": {
                "streak": min(
                    deps["_s4_confirm_streak"](runtime.get("signal_history") or [], runtime.get("last_cdc_status")),
                    max(confirm_days, 0),
                ),
                "required_days": confirm_days,
            },
        }

        exposure = runtime.get("exposure") if isinstance(runtime, dict) else {}
        total_usd = deps["_safe_float"](exposure.get("total_usd"), 0.0) if isinstance(exposure, dict) else 0.0
        btc_value = deps["_safe_float"]((exposure.get("btc") or {}).get("notional_usd"), 0.0) if isinstance(exposure, dict) else 0.0
        gold_value = deps["_safe_float"]((exposure.get("gold") or {}).get("notional_usd"), 0.0) if isinstance(exposure, dict) else 0.0
        btc_weight = deps["_safe_float"]((exposure.get("btc") or {}).get("weight"), 0.0) * 100 if isinstance(exposure, dict) else 0.0
        gold_weight = deps["_safe_float"]((exposure.get("gold") or {}).get("weight"), 0.0) * 100 if isinstance(exposure, dict) else 0.0

        exchange = data["exchange"]
        lots_btc = deps["_load_s4_fifo_open_lots"](cursor, exchange, "BTC")
        lots_gold = deps["_load_s4_fifo_open_lots"](cursor, exchange, "GOLD")
        cost_btc = deps["_sum_lots_cost"](lots_btc)
        cost_gold = deps["_sum_lots_cost"](lots_gold)
        cost_total = cost_btc + cost_gold
        pnl_total = total_usd - cost_total if cost_total > 0 else 0.0
        pnl_total_pct = (pnl_total / cost_total) * 100.0 if cost_total > 0 else 0.0

        def _pnl(value: float, cost: float) -> tuple[float, float]:
            if cost <= 0:
                return 0.0, 0.0
            pnl = value - cost
            pct = (pnl / cost) * 100.0
            return pnl, pct

        btc_pnl, btc_pnl_pct = _pnl(btc_value, cost_btc)
        gold_pnl, gold_pnl_pct = _pnl(gold_value, cost_gold)

        data["portfolio"] = {
            "total_usd": total_usd,
            "cost_total": cost_total,
            "pnl_total": pnl_total,
            "pnl_total_pct": pnl_total_pct,
            "btc": {
                "notional_usd": btc_value,
                "weight_pct": btc_weight,
                "cost": cost_btc,
                "pnl": btc_pnl,
                "pnl_pct": btc_pnl_pct,
            },
            "gold": {
                "notional_usd": gold_value,
                "weight_pct": gold_weight,
                "cost": cost_gold,
                "pnl": gold_pnl,
                "pnl_pct": gold_pnl_pct,
            },
        }

        data["gates"] = {
            "signal_history_len": len(runtime.get("signal_history") or []) if isinstance(runtime.get("signal_history"), list) else 0,
            "last_flip_at": runtime.get("last_flip_at") or "N/A",
            "flips_30d": runtime.get("flip_count_30d") or 0,
            "max_flips_30d": config.get("max_flips_30d") or os.getenv("S4_MAX_FLIPS_30D", "2"),
            "last_hold_reason": runtime.get("last_hold_reason") or "",
            "confirm_days": confirm_days,
            "confirm_streak": min(
                deps["_s4_confirm_streak"](runtime.get("signal_history") or [], runtime.get("last_cdc_status")),
                max(confirm_days, 0),
            ),
        }

        cursor.execute(
            """
            SELECT date, cdc_status, state, slope_pct, ema_gap_pct, eod_lag_days
            FROM s4_neutral_zone_eod
            ORDER BY date DESC
            LIMIT 1
            """
        )
        eod_row = cursor.fetchone()
        eod = {}
        if eod_row:
            eod_cols = [d[0] for d in cursor.description]
            eod = dict(zip(eod_cols, eod_row))
        eod_cdc = str((eod or {}).get("cdc_status") or "").lower()
        runtime_cdc = str(runtime.get("last_cdc_status") or "").lower()
        eod_lag_days = int(deps["_safe_float"]((eod or {}).get("eod_lag_days"), 0.0))
        mismatch = bool(eod_cdc and runtime_cdc and eod_cdc != runtime_cdc)
        streak = int(deps["_safe_float"](runtime.get("mismatch_streak_days"), 0.0))
        severity = deps["mismatch_severity"](mismatch=mismatch, eod_lag_days=eod_lag_days, streak=streak)
        data["signal_layers"]["eod"] = {
            "layer": "eod_analytics",
            "asof_date": (eod or {}).get("date") or "",
            "snapshot_ts_utc": "",
            "cdc_status_eod": eod_cdc,
            "neutral_state_eod": str((eod or {}).get("state") or ""),
            "slope_pct_eod": deps["_safe_float"]((eod or {}).get("slope_pct"), 0.0),
            "gap_pct_eod": deps["_safe_float"]((eod or {}).get("ema_gap_pct"), 0.0),
            "eod_lag_days": eod_lag_days,
        }
        data["signal_layers"]["mismatch"] = mismatch
        data["signal_layers"]["mismatch_streak_days"] = streak
        data["signal_layers"]["mismatch_severity"] = severity
        fallback_event = "mismatch_detected" if mismatch else "match_state"
        data["signal_layers"]["mismatch_streak_event"] = str(runtime.get("mismatch_streak_event") or fallback_event)

        last_results = runtime.get("last_action_result")
        if isinstance(last_results, list) and last_results:
            res = last_results[0] if isinstance(last_results[0], dict) else {}
            data["last_status"] = {
                "status": res.get("status") or "N/A",
                "reason": res.get("reason") or "",
            }

        last_err = runtime.get("last_error")
        if isinstance(last_err, dict):
            data["last_error"] = {
                "at": last_err.get("at") or "",
                "reason": last_err.get("reason") or "",
                "detail": last_err.get("detail") or "",
            }

        cursor.execute(
            """
            SELECT executed_at, from_asset, to_asset, reason
            FROM strategy_rotation_log
            WHERE strategy_mode='s4_multi_leg'
              AND metadata_json LIKE '%"executed_ok": true%'
            ORDER BY executed_at DESC
            LIMIT 1
            """
        )
        rot_row = cursor.fetchone()
        if rot_row:
            rot_cols = [d[0] for d in cursor.description]
            data["last_rotation"] = dict(zip(rot_cols, rot_row))

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM strategy_rotation_log
            WHERE strategy_mode='s4_multi_leg'
              AND reason IN ('shadow_swap_plan', 'shadow_swap_heartbeat')
              AND executed_at >= (UTC_TIMESTAMP() - INTERVAL 90 DAY)
            """
        )
        cnt_row = cursor.fetchone()
        data["shadow_swap"]["count_90d"] = int((cnt_row or [0])[0] or 0)

        cursor.execute(
            """
            SELECT executed_at, from_asset, to_asset, notional_usd, cdc_status, reason, metadata_json
            FROM strategy_rotation_log
            WHERE strategy_mode='s4_multi_leg'
              AND reason IN ('shadow_swap_plan', 'shadow_swap_heartbeat')
            ORDER BY executed_at DESC
            LIMIT 10
            """
        )
        recent_rows = cursor.fetchall() or []
        cols = [d[0] for d in cursor.description]
        recent: list[dict] = []
        for row in recent_rows:
            entry = dict(zip(cols, row))
            meta_raw = entry.get("metadata_json")
            if isinstance(meta_raw, str):
                try:
                    entry["metadata_json"] = json.loads(meta_raw)
                except Exception:
                    pass
            meta_obj = entry.get("metadata_json") if isinstance(entry.get("metadata_json"), dict) else {}
            gate_obj = meta_obj.get("gate") if isinstance(meta_obj.get("gate"), dict) else {}
            if gate_obj:
                gate_reason = str(gate_obj.get("reason") or entry.get("reason") or "")
                if not gate_obj.get("next_unlock_condition") or gate_obj.get("next_unlock_min_days") is None:
                    cond, min_days = deps["next_unlock_from_gate_reason"](
                        gate_reason,
                        btc_confirm_days=max(int(os.getenv("S4_SHADOW_BTC_CONFIRM_DAYS", "3") or 3), 0),
                        xau_confirm_days=max(int(os.getenv("S4_SHADOW_XAU_CONFIRM_DAYS", "5") or 5), 0),
                    )
                    gate_obj["next_unlock_condition"] = cond
                    gate_obj["next_unlock_min_days"] = min_days
                meta_obj["gate"] = gate_obj
                entry["metadata_json"] = meta_obj
            recent.append(entry)
        if recent:
            data["shadow_swap"]["last"] = recent[0]
            data["shadow_swap"]["recent"] = recent
            latest_heartbeat = next((r for r in recent if str(r.get("reason") or "") == "shadow_swap_heartbeat"), recent[0])
            hb_meta = latest_heartbeat.get("metadata_json") if isinstance(latest_heartbeat.get("metadata_json"), dict) else {}
            hb_gate = hb_meta.get("gate") if isinstance(hb_meta, dict) and isinstance(hb_meta.get("gate"), dict) else {}
            hb_reason = str(hb_gate.get("reason") or latest_heartbeat.get("reason") or "")
            unlock_cond = hb_gate.get("next_unlock_condition")
            unlock_days_raw = hb_gate.get("next_unlock_min_days")
            if not unlock_cond or unlock_days_raw is None:
                unlock_cond, unlock_days = deps["next_unlock_from_gate_reason"](
                    hb_reason,
                    btc_confirm_days=max(int(os.getenv("S4_SHADOW_BTC_CONFIRM_DAYS", "3") or 3), 0),
                    xau_confirm_days=max(int(os.getenv("S4_SHADOW_XAU_CONFIRM_DAYS", "5") or 5), 0),
                )
            else:
                unlock_days = int(deps["_safe_float"](unlock_days_raw, 0.0))
            data["why_not_flip"] = {
                "decision": hb_gate.get("decision") or "HOLD",
                "reason": hb_reason,
                "next_unlock_condition": unlock_cond,
                "next_unlock_min_days": unlock_days,
                "days_since_last_swap": int(deps["_safe_float"](hb_gate.get("days_since_last_swap"), 0.0)),
                "holding": hb_gate.get("holding") or latest_heartbeat.get("from_asset"),
                "target_asset": hb_gate.get("target_asset") or latest_heartbeat.get("to_asset"),
            }

    return data
