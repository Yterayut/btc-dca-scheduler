"""Helpers for S4 observability and shadow swap diagnostics."""

from __future__ import annotations

from typing import Any


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def normalize_reason_filter(value: str | None) -> set[str]:
    token = str(value or "all").strip().lower()
    if token == "all":
        return {"shadow_swap_plan", "shadow_swap_heartbeat"}
    if token == "heartbeat":
        return {"shadow_swap_heartbeat"}
    if token == "plan":
        return {"shadow_swap_plan"}
    return {"shadow_swap_plan", "shadow_swap_heartbeat"}


def derive_shadow_decision(entry: dict[str, Any]) -> str:
    meta = entry.get("metadata_json") if isinstance(entry.get("metadata_json"), dict) else {}
    gate = meta.get("gate") if isinstance(meta, dict) else {}
    decision = gate.get("decision") if isinstance(gate, dict) else None
    if decision:
        return str(decision)
    if str(entry.get("reason") or "") == "shadow_swap_plan":
        from_asset = str(entry.get("from_asset") or "").upper()
        to_asset = str(entry.get("to_asset") or "").upper()
        if from_asset == "GOLD" and to_asset == "BTC":
            return "SWAP_TO_BTC"
        if from_asset == "BTC" and to_asset == "GOLD":
            return "SWAP_TO_XAU"
    return "HOLD"


def next_unlock_from_gate_reason(reason: str, *, btc_confirm_days: int, xau_confirm_days: int) -> tuple[str, int]:
    key = str(reason or "").strip().lower()
    if key == "gate_cdc_up_required":
        return (f"cdc_status must be up for {btc_confirm_days} consecutive days", max(btc_confirm_days, 0))
    if key == "gate_cdc_down_required":
        return (f"cdc_status must be down for {xau_confirm_days} consecutive days", max(xau_confirm_days, 0))
    if key == "gate_cdc_confirm":
        return ("cdc confirmation window must complete", 1)
    if key == "gate_neutral":
        return ("neutral_state must be btc_signal", 1)
    if key == "gate_slope":
        return ("slope threshold must be satisfied", 1)
    if key == "gate_gap":
        return ("ema gap threshold must be satisfied", 1)
    if key == "gate_cooldown":
        return ("cooldown period must expire", 1)
    if key == "all_gates_passed":
        return ("all gates passed", 0)
    return ("waiting for next valid signal snapshot", 1)


def mismatch_severity(*, mismatch: bool, eod_lag_days: int, streak: int) -> str:
    if not mismatch:
        return "match"
    if eod_lag_days > 0:
        # When analytics is lagged, mismatch is expected; cap at WARN.
        return "warn" if streak >= 2 else "info"
    if streak >= 5:
        return "critical"
    if streak >= 2:
        return "warn"
    return "warn"
