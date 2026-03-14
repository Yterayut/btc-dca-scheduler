#!/usr/bin/env python3
"""Generate S4 Phase 0.5 analysis deliverables (Round 5)."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any
from urllib.request import Request, urlopen

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from strategies.s4_neutral_zone import DEFAULT_NEUTRAL_CONFIG, calculate_state
from strategies.s4_utils import cdc_status_from_series, compute_ema_series, fetch_okx_ratio_series

PREMATURE_EXIT_RETURN_PCT = 3.0
PREMATURE_EXIT_LOOKAHEAD_DAYS = 5
PREMATURE_EXIT_REENTRY_DAYS = 3
REGIME_LOOKBACK_DAYS = 20
REGIME_BTC_DOM_PCT = 2.0
REGIME_GOLD_DOM_PCT = -2.0
FRED_BTC_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CBBTCUSD"
LBMA_GOLD_PM_URL = "https://prices.lbma.org.uk/json/gold_pm.json"


@dataclass(frozen=True)
class ModelDef:
    name: str
    description: str


MODELS = (
    ModelDef("model_a_cdc_execution", "in_btc when cdc_status == up"),
    ModelDef("model_b_cdc_plus_neutral_filter", "in_btc when cdc_status == up and neutral_state == btc_signal"),
    ModelDef("model_c_gold_only", "always in gold (no flips)"),
    ModelDef("model_d_btc_only", "always in btc (no flips)"),
)


def _safe_mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _safe_median(values: list[float]) -> float | None:
    return median(values) if values else None


def _pct_return(base: float, value: float) -> float | None:
    if base <= 0:
        return None
    return (value / base - 1.0) * 100.0


def _event_return_pct(leg: str, entry_ratio: float, value_ratio: float) -> float | None:
    if entry_ratio <= 0 or value_ratio <= 0:
        return None
    if leg == "btc":
        return (value_ratio / entry_ratio - 1.0) * 100.0
    if leg == "gold":
        return (entry_ratio / value_ratio - 1.0) * 100.0
    raise ValueError(f"unknown leg: {leg}")


def _parse_windows(raw: str) -> list[int]:
    out: list[int] = []
    for token in str(raw).split(","):
        token = token.strip()
        if not token:
            continue
        days = int(token)
        if days > 0 and days not in out:
            out.append(days)
    return sorted(out)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _http_get_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; S4Phase05Analysis/1.0; +https://example.local)",
            "Accept": "application/json,text/csv,text/plain,*/*",
        },
    )
    with urlopen(req, timeout=30) as resp:  # nosec B310
        return resp.read().decode("utf-8")


def _fetch_fred_btc_close() -> dict[date, float]:
    raw = _http_get_text(FRED_BTC_CSV_URL)
    reader = csv.DictReader(io.StringIO(raw))
    out: dict[date, float] = {}
    for row in reader:
        raw_date = str(row.get("observation_date") or "").strip()
        raw_val = str(row.get("CBBTCUSD") or "").strip()
        if not raw_date or not raw_val or raw_val == ".":
            continue
        try:
            d = date.fromisoformat(raw_date)
            v = float(raw_val)
        except (TypeError, ValueError):
            continue
        if v > 0:
            out[d] = v
    return out


def _fetch_lbma_gold_pm_usd() -> dict[date, float]:
    raw = _http_get_text(LBMA_GOLD_PM_URL)
    payload = json.loads(raw)
    out: dict[date, float] = {}
    for item in payload:
        raw_date = str(item.get("d") or "").strip()
        vals = item.get("v") or []
        if not raw_date or not vals:
            continue
        try:
            d = date.fromisoformat(raw_date)
            usd_pm = float(vals[0])
        except (TypeError, ValueError, IndexError):
            continue
        if usd_pm > 0:
            out[d] = usd_pm
    return out


def _build_rows_from_fred_lbma(start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
    btc_by_date = _fetch_fred_btc_close()
    gold_by_date = _fetch_lbma_gold_pm_usd()
    common_dates = sorted(set(btc_by_date.keys()) & set(gold_by_date.keys()))
    out: list[dict[str, Any]] = []
    for d in common_dates:
        if start and d < start:
            continue
        if end and d > end:
            continue
        btc = btc_by_date[d]
        gold = gold_by_date[d]
        if btc <= 0 or gold <= 0:
            continue
        out.append({"date": d, "ratio": btc / gold})
    return out


def _forward_ratio_return(rows: list[dict[str, Any]], idx: int, days: int) -> tuple[float | None, int]:
    base = float(rows[idx]["ratio"])
    start_date: date = rows[idx]["date"]
    target = start_date + timedelta(days=days)
    candidates = [r for r in rows if start_date < r["date"] <= target]
    if not candidates:
        return None, 0
    end_ratio = float(candidates[-1]["ratio"])
    ret = _pct_return(base, end_ratio)
    observed_days = int((candidates[-1]["date"] - start_date).days)
    return ret, observed_days


def _in_btc(model_name: str, row: dict[str, Any]) -> bool:
    if model_name == "model_c_gold_only":
        return False
    if model_name == "model_d_btc_only":
        return True
    cdc = str(row.get("cdc_status") or "").lower()
    neutral = str(row.get("neutral_state") or "").lower()
    if model_name == "model_a_cdc_execution":
        return cdc == "up"
    if model_name == "model_b_cdc_plus_neutral_filter":
        return cdc == "up" and neutral == "btc_signal"
    raise ValueError(f"unknown model: {model_name}")


def _in_leg(model_name: str, row: dict[str, Any], leg: str) -> bool:
    in_btc = _in_btc(model_name, row)
    if leg == "btc":
        return in_btc
    if leg == "gold":
        return not in_btc
    raise ValueError(f"unknown leg: {leg}")


def _build_rows(
    max_days: int,
    data_source: str,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    source_rows: list[dict[str, Any]] = []
    if data_source == "okx":
        limit = max(max_days + 240, 1200)
        series = fetch_okx_ratio_series(use_cache=False, limit=limit, bar="1D")
        for ts, ratio in series:
            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            d = dt.date()
            if start and d < start:
                continue
            if end and d > end:
                continue
            source_rows.append({"date": d, "ratio": float(ratio)})
    elif data_source == "fred_lbma":
        source_rows = _build_rows_from_fred_lbma(start=start, end=end)
    else:
        raise SystemExit(f"Unsupported --data-source: {data_source}")

    values: list[float] = []
    rows: list[dict[str, Any]] = []
    for row in sorted(source_rows, key=lambda x: x["date"]):
        d = row["date"]
        ratio = row["ratio"]
        values.append(float(ratio))
        cdc = cdc_status_from_series(values)
        row_out = {
            "date": d,
            "ratio": float(ratio),
            "cdc_status": str(cdc.get("status") or "").lower() or None,
            "cdc_fast": None if cdc.get("fast") is None else float(cdc["fast"]),
            "cdc_slow": None if cdc.get("slow") is None else float(cdc["slow"]),
        }
        rows.append(row_out)

    if not rows:
        return []

    ema12 = compute_ema_series([r["ratio"] for r in rows], 12)
    ema26 = compute_ema_series([r["ratio"] for r in rows], 26)
    for idx, row in enumerate(rows):
        row["ema12"] = float(ema12[idx])
        row["ema26"] = float(ema26[idx])
        state, metrics = calculate_state(
            ema12=row["ema12"],
            ema26=row["ema26"],
            ema12_history=list(reversed(ema12[: idx + 1])),
            config=DEFAULT_NEUTRAL_CONFIG,
        )
        row["neutral_state"] = state.value if state else None
        row["ema_gap_pct"] = None if not metrics else float(metrics.get("ema_gap_pct") or 0.0)
        row["slope_pct"] = None if not metrics else float(metrics.get("slope_pct") or 0.0)

    for idx, row in enumerate(rows):
        if idx >= REGIME_LOOKBACK_DAYS:
            base = float(rows[idx - REGIME_LOOKBACK_DAYS]["ratio"])
            now = float(row["ratio"])
            ret20 = _pct_return(base, now)
        else:
            ret20 = None
        row["ret_20d_pct"] = ret20
        if ret20 is None:
            row["regime_label"] = "mixed"
        elif ret20 >= REGIME_BTC_DOM_PCT:
            row["regime_label"] = "btc_dominant"
        elif ret20 <= REGIME_GOLD_DOM_PCT:
            row["regime_label"] = "gold_dominant"
        else:
            row["regime_label"] = "mixed"

    return rows


def _slice_window_days(rows: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    if not rows:
        return []
    end_date = rows[-1]["date"]
    start_date = end_date - timedelta(days=days - 1)
    return [r for r in rows if r["date"] >= start_date]


def _slice_window_dates(rows: list[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    return [r for r in rows if start <= r["date"] <= end]


def _build_events(rows: list[dict[str, Any]], model: ModelDef, leg: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    i = 0
    while i < len(rows):
        if not _in_leg(model.name, rows[i], leg):
            i += 1
            continue
        start_idx = i
        while i + 1 < len(rows) and _in_leg(model.name, rows[i + 1], leg):
            i += 1
        end_idx = i

        event_rows = rows[start_idx : end_idx + 1]
        entry_ratio = float(event_rows[0]["ratio"])
        returns = [_event_return_pct(leg, entry_ratio, float(r["ratio"])) for r in event_rows]
        clean_returns = [r for r in returns if r is not None]

        exit_idx = end_idx + 1 if end_idx + 1 < len(rows) else None
        exit_day = rows[exit_idx]["date"] if exit_idx is not None else None
        exit_reason = "end_of_data"
        if exit_idx is not None:
            next_row = rows[exit_idx]
            exit_reason = (
                f"in_{leg}->false "
                f"(cdc={next_row.get('cdc_status')}, neutral={next_row.get('neutral_state')}, "
                f"gap={next_row.get('ema_gap_pct')}, slope={next_row.get('slope_pct')})"
            )

        reentry_3d = False
        max_return_5d = None
        premature = False
        f3 = f5 = f7 = None
        obs3 = obs5 = obs7 = 0
        if exit_idx is not None:
            f3, obs3 = _forward_ratio_return(rows, exit_idx, 3)
            f5, obs5 = _forward_ratio_return(rows, exit_idx, 5)
            f7, obs7 = _forward_ratio_return(rows, exit_idx, 7)

            if leg == "btc":
                lookahead = []
                for j in range(exit_idx + 1, len(rows)):
                    d = (rows[j]["date"] - rows[exit_idx]["date"]).days
                    if d > PREMATURE_EXIT_LOOKAHEAD_DAYS:
                        break
                    lookahead.append(_pct_return(float(rows[exit_idx]["ratio"]), float(rows[j]["ratio"])))
                lookahead = [x for x in lookahead if x is not None]
                max_return_5d = max(lookahead) if lookahead else None
                for j in range(exit_idx + 1, len(rows)):
                    d = (rows[j]["date"] - rows[exit_idx]["date"]).days
                    if d > PREMATURE_EXIT_REENTRY_DAYS:
                        break
                    if _in_leg(model.name, rows[j], "btc"):
                        reentry_3d = True
                        break
                premature = bool(
                    reentry_3d or (max_return_5d is not None and max_return_5d >= PREMATURE_EXIT_RETURN_PCT)
                )

        quality = "inconclusive"
        end_return = clean_returns[-1] if clean_returns else None
        peak_return = max(clean_returns) if clean_returns else None
        if leg == "btc" and end_return is not None and peak_return is not None:
            if bool(premature):
                quality = "premature_exit"
            elif end_return <= 0 and peak_return <= 1.0:
                quality = "false_start"
            elif end_return > 0 and peak_return > 1.0:
                quality = "trend_follow_through"
            elif end_return > 0:
                quality = "clean_win"

        event_regimes = [str(r.get("regime_label") or "mixed") for r in event_rows]
        regime_counts = {
            "gold_dominant": event_regimes.count("gold_dominant"),
            "mixed": event_regimes.count("mixed"),
            "btc_dominant": event_regimes.count("btc_dominant"),
        }
        regime_label = max(regime_counts, key=lambda k: regime_counts[k])

        events.append(
            {
                "model": model.name,
                "leg": leg,
                "start_date": event_rows[0]["date"].isoformat(),
                "end_date": event_rows[-1]["date"].isoformat(),
                "duration_days": len(event_rows),
                "entry_ratio": round(entry_ratio, 6),
                "exit_ratio": round(float(event_rows[-1]["ratio"]), 6),
                "return_pct": round(end_return, 4) if end_return is not None else None,
                "peak_return_pct": round(max(clean_returns), 4) if clean_returns else None,
                "max_adverse_excursion_pct": round(min(clean_returns), 4) if clean_returns else None,
                "exit_reason": exit_reason,
                "cdc_at_entry": event_rows[0].get("cdc_status"),
                "cdc_at_exit": event_rows[-1].get("cdc_status"),
                "state_at_entry": event_rows[0].get("neutral_state"),
                "state_at_exit": event_rows[-1].get("neutral_state"),
                "state_before_entry": rows[start_idx - 1].get("neutral_state") if start_idx > 0 else None,
                "state_after_exit": rows[exit_idx].get("neutral_state") if exit_idx is not None else None,
                "exit_day": exit_day.isoformat() if exit_day else None,
                "forward_return_3d_pct": round(f3, 4) if f3 is not None else None,
                "forward_return_5d_pct": round(f5, 4) if f5 is not None else None,
                "forward_return_7d_pct": round(f7, 4) if f7 is not None else None,
                "forward_obs_3d": obs3,
                "forward_obs_5d": obs5,
                "forward_obs_7d": obs7,
                "max_return_5d_after_exit_pct": round(max_return_5d, 4) if max_return_5d is not None else None,
                "reentry_within_3d": reentry_3d,
                "premature_exit": premature,
                "quality_label": quality,
                "regime_label": regime_label,
            }
        )
        i += 1
    return events


def _build_conflicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        cdc = str(row.get("cdc_status") or "").lower()
        neutral = str(row.get("neutral_state") or "").lower()
        if not cdc or not neutral:
            continue
        conflict_type = None
        if cdc == "up" and neutral != "btc_signal":
            conflict_type = "cdc_up_vs_non_btc_signal"
        elif cdc == "down" and neutral == "btc_signal":
            conflict_type = "cdc_down_vs_btc_signal"
        if not conflict_type:
            continue
        f3, obs3 = _forward_ratio_return(rows, idx, 3)
        f5, obs5 = _forward_ratio_return(rows, idx, 5)
        f7, obs7 = _forward_ratio_return(rows, idx, 7)
        out.append(
            {
                "date": row["date"].isoformat(),
                "conflict_type": conflict_type,
                "ratio": round(float(row["ratio"]), 6),
                "cdc_status": cdc,
                "neutral_state": neutral,
                "regime_label": row.get("regime_label"),
                "ema_gap_pct": round(float(row.get("ema_gap_pct") or 0.0), 4),
                "slope_pct": round(float(row.get("slope_pct") or 0.0), 4),
                "forward_3d_pct": round(f3, 4) if f3 is not None else None,
                "forward_5d_pct": round(f5, 4) if f5 is not None else None,
                "forward_7d_pct": round(f7, 4) if f7 is not None else None,
                "forward_obs_3d": obs3,
                "forward_obs_5d": obs5,
                "forward_obs_7d": obs7,
            }
        )
    return out


def _event_summary(model: ModelDef, leg: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(e["return_pct"]) for e in events if e.get("return_pct") is not None]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    durations = [int(e["duration_days"]) for e in events]
    peaks = [float(e["peak_return_pct"]) for e in events if e.get("peak_return_pct") is not None]
    maes = [float(e["max_adverse_excursion_pct"]) for e in events if e.get("max_adverse_excursion_pct") is not None]
    premature_count = sum(1 for e in events if e.get("premature_exit"))

    win_rate = (len(wins) / len(returns)) if returns else None
    avg_win = _safe_mean(wins)
    avg_loss = _safe_mean(losses)
    expectancy = None
    if returns:
        if win_rate is not None and avg_win is not None and avg_loss is not None:
            expectancy = win_rate * avg_win + (1.0 - win_rate) * avg_loss
        else:
            expectancy = _safe_mean(returns)

    return {
        "model": model.name,
        "description": model.description,
        "leg": leg,
        "event_count": len(events),
        "avg_duration_days": _safe_mean([float(x) for x in durations]),
        "median_duration_days": _safe_median([float(x) for x in durations]),
        "win_rate": win_rate,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "median_return_pct": _safe_median(returns),
        "expectancy_pct": expectancy,
        "avg_peak_return_pct": _safe_mean(peaks),
        "avg_mae_pct": _safe_mean(maes),
        "premature_exit_count": premature_count,
        "premature_exit_rate": (premature_count / len(events)) if events else None,
    }


def _daily_metrics(rows: list[dict[str, Any]], model_name: str) -> dict[str, Any]:
    if len(rows) < 2:
        return {"total_return_pct": None, "max_drawdown_pct": None, "switch_count": 0}

    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    switch_count = 0

    prev_in_btc = _in_btc(model_name, rows[0])
    for i in range(1, len(rows)):
        r_prev = float(rows[i - 1]["ratio"])
        r_cur = float(rows[i]["ratio"])
        curr_in_btc = _in_btc(model_name, rows[i - 1])
        if curr_in_btc != prev_in_btc:
            switch_count += 1
        prev_in_btc = curr_in_btc

        ret = (r_cur / r_prev - 1.0) if curr_in_btc else (r_prev / r_cur - 1.0)
        equity *= (1.0 + ret)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)

    return {
        "total_return_pct": (equity - 1.0) * 100.0,
        "max_drawdown_pct": max_dd * 100.0,
        "switch_count": switch_count,
    }


def _build_attribution(rows: list[dict[str, Any]], model_name: str, btc_events: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) < 2:
        return {
            "carry_from_gold_hold": 0.0,
            "carry_from_btc_hold": 0.0,
            "gain_from_switching": 0.0,
            "loss_from_switching": 0.0,
            "gain_saved_by_staying_in_btc": 0.0,
            "gain_saved_by_staying_in_gold": 0.0,
            "switching_net_value_add": 0.0,
            "gain_lost_due_to_false_btc_entries": 0.0,
        }

    carry_gold = carry_btc = gain_switch = loss_switch = 0.0
    for i in range(1, len(rows)):
        prev_row = rows[i - 1]
        cur_row = rows[i]
        rp = float(prev_row["ratio"])
        rc = float(cur_row["ratio"])
        prev_leg_btc = _in_btc(model_name, prev_row)

        ret_if_btc = rc / rp - 1.0
        ret_if_gold = rp / rc - 1.0
        actual = ret_if_btc if prev_leg_btc else ret_if_gold
        if prev_leg_btc:
            carry_btc += actual
        else:
            carry_gold += actual

        if _in_btc(model_name, cur_row) != prev_leg_btc:
            counter = ret_if_gold if prev_leg_btc else ret_if_btc
            delta = actual - counter
            if delta >= 0:
                gain_switch += delta
            else:
                loss_switch += delta

    false_entry_loss = 0.0
    for e in btc_events:
        if e.get("quality_label") == "false_start":
            false_entry_loss += abs(float(e.get("return_pct") or 0.0)) / 100.0

    saved_gold = saved_btc = 0.0
    for c in conflicts:
        f5 = c.get("forward_5d_pct")
        if f5 is None:
            continue
        v = float(f5)
        if c.get("conflict_type") == "cdc_down_vs_btc_signal" and v < 0:
            saved_gold += abs(v) / 100.0
        if c.get("conflict_type") == "cdc_up_vs_non_btc_signal" and v > 0:
            saved_btc += abs(v) / 100.0

    return {
        "carry_from_gold_hold": carry_gold * 100.0,
        "carry_from_btc_hold": carry_btc * 100.0,
        "gain_from_switching": gain_switch * 100.0,
        "loss_from_switching": loss_switch * 100.0,
        "gain_saved_by_staying_in_btc": saved_btc * 100.0,
        "gain_saved_by_staying_in_gold": saved_gold * 100.0,
        "switching_net_value_add": (gain_switch + loss_switch) * 100.0,
        "gain_lost_due_to_false_btc_entries": false_entry_loss * 100.0,
    }


def _window_metrics(window_rows: list[dict[str, Any]], model: ModelDef) -> dict[str, Any]:
    btc_events = _build_events(window_rows, model, "btc")
    gold_events = _build_events(window_rows, model, "gold")
    conflicts = _build_conflicts(window_rows)

    btc_summary = _event_summary(model, "btc", btc_events)
    gold_summary = _event_summary(model, "gold", gold_events)

    btc_days = sum(1 for r in window_rows if _in_btc(model.name, r))
    total_days = len(window_rows)
    gold_days = total_days - btc_days
    time_in_btc = (btc_days / total_days) if total_days else None
    time_in_gold = (gold_days / total_days) if total_days else None
    time_in_conflict = (len(conflicts) / total_days) if total_days else None

    btc_exp = btc_summary.get("expectancy_pct")
    gold_exp = gold_summary.get("expectancy_pct")
    total_exp = None
    if time_in_btc is not None and time_in_gold is not None:
        if time_in_btc == 0 and gold_exp is not None:
            total_exp = float(gold_exp)
        elif time_in_gold == 0 and btc_exp is not None:
            total_exp = float(btc_exp)
        elif btc_exp is not None and gold_exp is not None:
            total_exp = time_in_btc * float(btc_exp) + time_in_gold * float(gold_exp)

    daily = _daily_metrics(window_rows, model.name)

    abs_btc = abs(time_in_btc * float(btc_exp)) if (time_in_btc is not None and btc_exp is not None) else 0.0
    abs_gold = abs(time_in_gold * float(gold_exp)) if (time_in_gold is not None and gold_exp is not None) else 0.0
    contrib_btc = contrib_gold = None
    if abs_btc + abs_gold > 0:
        denom = abs_btc + abs_gold
        contrib_btc = abs_btc / denom
        contrib_gold = abs_gold / denom

    attribution = _build_attribution(window_rows, model.name, btc_events, conflicts)

    return {
        "btc_events": btc_events,
        "gold_events": gold_events,
        "conflicts": conflicts,
        "btc_summary": btc_summary,
        "gold_summary": gold_summary,
        "attribution": attribution,
        "system": {
            "total_expectancy_pct": total_exp,
            "btc_leg_expectancy_pct": btc_exp,
            "gold_leg_expectancy_pct": gold_exp,
            "contribution_pct_btc_leg": contrib_btc,
            "contribution_pct_gold_leg": contrib_gold,
            "time_in_btc_pct": time_in_btc,
            "time_in_gold_pct": time_in_gold,
            "time_in_conflict_pct": time_in_conflict,
            "btc_event_count": len(btc_events),
            "gold_event_count": len(gold_events),
            "conflict_days": len(conflicts),
            "rows": total_days,
            "total_return_pct": daily["total_return_pct"],
            "max_drawdown_pct": daily["max_drawdown_pct"],
            "switch_count": daily["switch_count"],
            "avg_mae_pct": _safe_mean([x for x in [btc_summary.get("avg_mae_pct"), gold_summary.get("avg_mae_pct")] if x is not None]),
        },
    }


def _regime_model_comparison(rows: list[dict[str, Any]], model: ModelDef) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for regime in ("gold_dominant", "mixed", "btc_dominant"):
        subset = [r for r in rows if r.get("regime_label") == regime]
        metrics = _window_metrics(subset, model) if subset else None
        out.append(
            {
                "model": model.name,
                "regime_label": regime,
                "rows": len(subset),
                "btc_event_count": metrics["btc_summary"]["event_count"] if metrics else 0,
                "gold_event_count": metrics["gold_summary"]["event_count"] if metrics else 0,
                "win_rate_btc": metrics["btc_summary"]["win_rate"] if metrics else None,
                "expectancy_btc_pct": metrics["btc_summary"]["expectancy_pct"] if metrics else None,
                "expectancy_gold_pct": metrics["gold_summary"]["expectancy_pct"] if metrics else None,
                "total_expectancy_pct": metrics["system"]["total_expectancy_pct"] if metrics else None,
                "avg_duration_btc_days": metrics["btc_summary"]["avg_duration_days"] if metrics else None,
                "avg_duration_gold_days": metrics["gold_summary"]["avg_duration_days"] if metrics else None,
                "avg_mae_btc_pct": metrics["btc_summary"]["avg_mae_pct"] if metrics else None,
                "avg_mae_gold_pct": metrics["gold_summary"]["avg_mae_pct"] if metrics else None,
                "conflict_days": len(metrics["conflicts"]) if metrics else 0,
            }
        )
    return out


def _neutral_filter_cost(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    model_a = ModelDef("model_a_cdc_execution", "")
    model_b = ModelDef("model_b_cdc_plus_neutral_filter", "")
    for i, row in enumerate(rows):
        a = _in_btc(model_a.name, row)
        b = _in_btc(model_b.name, row)
        if not (a and not b):
            continue
        f5, _ = _forward_ratio_return(rows, i, 5)
        delayed_days = None
        for j in range(i + 1, len(rows)):
            if _in_btc(model_b.name, rows[j]):
                delayed_days = (rows[j]["date"] - row["date"]).days
                break
        out.append(
            {
                "date": row["date"].isoformat(),
                "missed_btc_entries": 1,
                "missed_return_pct": f5,
                "delayed_entry_days": delayed_days,
                "delayed_entry_cost_pct": f5 if delayed_days is not None else f5,
                "false_entry_avoidance_gain_pct": (-f5 if f5 is not None and f5 < 0 else 0.0),
                "regime_label": row.get("regime_label"),
            }
        )
    return out


def _regime_transition_study(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    def desired_pos(reg: str) -> str | None:
        if reg == "btc_dominant":
            return "BTC"
        if reg == "gold_dominant":
            return "GOLD"
        return None

    for i in range(1, len(rows)):
        prev_reg = rows[i - 1].get("regime_label")
        curr_reg = rows[i].get("regime_label")
        if prev_reg == curr_reg:
            continue
        target = desired_pos(str(curr_reg))
        for model in MODELS:
            before = "BTC" if _in_btc(model.name, rows[i - 1]) else "GOLD"
            after = "BTC" if _in_btc(model.name, rows[i]) else "GOLD"
            days_to_correct = None
            if target is not None:
                for j in range(i, len(rows)):
                    pos = "BTC" if _in_btc(model.name, rows[j]) else "GOLD"
                    if pos == target:
                        days_to_correct = (rows[j]["date"] - rows[i]["date"]).days
                        break
            out.append(
                {
                    "transition_date": rows[i]["date"].isoformat(),
                    "regime_before": prev_reg,
                    "regime_after": curr_reg,
                    "model": model.name,
                    "model_position_before": before,
                    "model_position_after": after,
                    "days_to_correct_position": days_to_correct,
                    "drawdown_during_transition": None,
                    "missed_return_during_transition": None,
                }
            )
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    headers = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _decision_memo(table_rows: list[dict[str, Any]]) -> str:
    lines = [
        "# S4 Phase 0.5 Round 5 Decision Memo",
        "",
        "## Summary Table",
        "| Window Type | Model A | Model B | GOLD-only | BTC-only | Winner |",
        "|---|---:|---:|---:|---:|---|",
    ]
    # table_rows keyed by window label
    for row in table_rows:
        lines.append(
            f"| {row['window_type']} | {row.get('model_a')} | {row.get('model_b')} | {row.get('gold_only')} | {row.get('btc_only')} | {row.get('winner')} |"
        )
    lines.extend([
        "",
        "## Recommendation",
        "- Keep production unchanged until BTC-bull regime evidence is sufficient.",
        "- Use model ranking per regime to decide whether regime-aware gating is justified.",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate S4 Phase 0.5 analysis deliverables (Round 5)")
    parser.add_argument(
        "--data-source",
        type=str,
        default="okx",
        choices=("okx", "fred_lbma"),
        help="Input ratio source: okx (existing) or fred_lbma (BTC from FRED CBBTCUSD + GOLD from LBMA PM)",
    )
    parser.add_argument("--days", type=int, default=365, help="Primary output window in days")
    parser.add_argument("--windows", type=str, default="", help="Comma-separated control windows (e.g. 180,365)")
    parser.add_argument("--start", type=str, default=None, help="Explicit start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="Explicit end date YYYY-MM-DD")
    parser.add_argument("--window-label", type=str, default="custom_window", help="Label for explicit date window")
    parser.add_argument("--output-dir", type=Path, default=Path("log"), help="Output directory")
    args = parser.parse_args()

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    if (start is None) ^ (end is None):
        raise SystemExit("--start and --end must be provided together")

    windows = _parse_windows(args.windows)
    if (start is None and end is None) and args.days not in windows:
        windows.append(args.days)
    windows = sorted(set(windows))

    max_days_for_fetch = max(windows) if windows else max(args.days, 365)
    all_rows = _build_rows(
        max(max_days_for_fetch, 30),
        data_source=args.data_source,
        start=start if (start and end) else None,
        end=end if (start and end) else None,
    )
    if not all_rows:
        raise SystemExit("No ratio rows available")

    named_windows: list[tuple[str, list[dict[str, Any]]]] = []
    if start and end:
        custom_rows = _slice_window_dates(all_rows, start, end)
        named_windows.append((args.window_label, custom_rows))
    for w in windows:
        named_windows.append((f"{w}d", _slice_window_days(all_rows, w)))

    window_metrics: dict[str, dict[str, Any]] = {}
    for label, rows in named_windows:
        model_data = {}
        for m in MODELS:
            model_data[m.name] = _window_metrics(rows, m)
        window_metrics[label] = {"rows": rows, "models": model_data}

    primary_label = args.window_label if (start and end) else f"{args.days}d"
    primary_rows = window_metrics[primary_label]["rows"]

    # collect primary detailed outputs
    primary_btc_events: list[dict[str, Any]] = []
    primary_gold_events: list[dict[str, Any]] = []
    primary_conflicts = _build_conflicts(primary_rows)
    primary_compare: list[dict[str, Any]] = []
    primary_regime_rows: list[dict[str, Any]] = []
    for m in MODELS:
        md = window_metrics[primary_label]["models"][m.name]
        primary_btc_events.extend(md["btc_events"])
        primary_gold_events.extend(md["gold_events"])
        primary_compare.append(md["btc_summary"])
        primary_compare.append(md["gold_summary"])
        primary_regime_rows.extend(_regime_model_comparison(primary_rows, m))

    # window comparison
    round4_window_rows: list[dict[str, Any]] = []
    btc_bull_value_add_rows: list[dict[str, Any]] = []
    for label, wd in window_metrics.items():
        gold_sys = wd["models"]["model_c_gold_only"]["system"]
        btc_sys = wd["models"]["model_d_btc_only"]["system"]
        for m in MODELS:
            sysm = wd["models"][m.name]["system"]
            row = {
                "window_type": label,
                "model": m.name,
                "rows": len(wd["rows"]),
                "total_return_pct": sysm.get("total_return_pct"),
                "expectancy_pct": sysm.get("total_expectancy_pct"),
                "max_drawdown_pct": sysm.get("max_drawdown_pct"),
                "avg_mae_pct": sysm.get("avg_mae_pct"),
                "btc_leg_expectancy_pct": sysm.get("btc_leg_expectancy_pct"),
                "gold_leg_expectancy_pct": sysm.get("gold_leg_expectancy_pct"),
                "time_in_btc_pct": sysm.get("time_in_btc_pct"),
                "time_in_gold_pct": sysm.get("time_in_gold_pct"),
                "btc_event_count": sysm.get("btc_event_count"),
                "gold_event_count": sysm.get("gold_event_count"),
                "conflict_days": sysm.get("conflict_days"),
                "switch_count": sysm.get("switch_count"),
                "vs_gold_only_return_pct": None if sysm.get("total_return_pct") is None or gold_sys.get("total_return_pct") is None else float(sysm["total_return_pct"]) - float(gold_sys["total_return_pct"]),
                "vs_btc_only_return_pct": None if sysm.get("total_return_pct") is None or btc_sys.get("total_return_pct") is None else float(sysm["total_return_pct"]) - float(btc_sys["total_return_pct"]),
                "vs_gold_only_expectancy_pct": None if sysm.get("total_expectancy_pct") is None or gold_sys.get("total_expectancy_pct") is None else float(sysm["total_expectancy_pct"]) - float(gold_sys["total_expectancy_pct"]),
                "vs_btc_only_expectancy_pct": None if sysm.get("total_expectancy_pct") is None or btc_sys.get("total_expectancy_pct") is None else float(sysm["total_expectancy_pct"]) - float(btc_sys["total_expectancy_pct"]),
            }
            round4_window_rows.append(row)
            if m.name in ("model_a_cdc_execution", "model_b_cdc_plus_neutral_filter"):
                btc_events = wd["models"][m.name]["btc_events"]
                pos = sum(1 for e in btc_events if e.get("return_pct") is not None and float(e["return_pct"]) > 0)
                neg = sum(1 for e in btc_events if e.get("return_pct") is not None and float(e["return_pct"]) <= 0)
                btc_bull_value_add_rows.append(
                    {
                        "window_type": label,
                        "model": m.name,
                        "delta_total_return_vs_gold_pct": row["vs_gold_only_return_pct"],
                        "delta_total_return_vs_btc_pct": row["vs_btc_only_return_pct"],
                        "delta_total_expectancy_vs_gold_pct": row["vs_gold_only_expectancy_pct"],
                        "delta_total_expectancy_vs_btc_pct": row["vs_btc_only_expectancy_pct"],
                        "delta_drawdown_vs_gold_pct": None if row["max_drawdown_pct"] is None or gold_sys.get("max_drawdown_pct") is None else float(row["max_drawdown_pct"]) - float(gold_sys["max_drawdown_pct"]),
                        "delta_drawdown_vs_btc_pct": None if row["max_drawdown_pct"] is None or btc_sys.get("max_drawdown_pct") is None else float(row["max_drawdown_pct"]) - float(btc_sys["max_drawdown_pct"]),
                        "number_of_btc_entries": len(btc_events),
                        "positive_btc_value_add_events": pos,
                        "negative_btc_value_add_events": neg,
                        "net_btc_leg_value_add_pct": row["vs_gold_only_expectancy_pct"],
                    }
                )

    # neutral filter cost + transitions on primary
    neutral_cost_rows = _neutral_filter_cost(primary_rows)
    transition_rows = _regime_transition_study(primary_rows)

    # performance map from primary regime comparison
    perf_map: list[dict[str, Any]] = []
    for regime in ("gold_dominant", "mixed", "btc_dominant"):
        reg_rows = [r for r in primary_regime_rows if r["regime_label"] == regime]
        score = {r["model"]: r.get("total_expectancy_pct") for r in reg_rows}
        winner = None
        if score:
            valid = [(k, v) for k, v in score.items() if v is not None]
            if valid:
                winner = max(valid, key=lambda x: x[1])[0]
        perf_map.append(
            {
                "regime_type": regime,
                "model_a": score.get("model_a_cdc_execution"),
                "model_b": score.get("model_b_cdc_plus_neutral_filter"),
                "gold_only": score.get("model_c_gold_only"),
                "btc_only": score.get("model_d_btc_only"),
                "winner": winner,
            }
        )

    # decision memo table by window labels
    memo_rows = []
    for label, wd in window_metrics.items():
        by_model = {r["model"]: r for r in round4_window_rows if r["window_type"] == label}
        candidates = [(m, by_model[m]["expectancy_pct"]) for m in by_model if by_model[m]["expectancy_pct"] is not None]
        winner = max(candidates, key=lambda x: x[1])[0] if candidates else None
        memo_rows.append(
            {
                "window_type": label,
                "model_a": by_model.get("model_a_cdc_execution", {}).get("expectancy_pct"),
                "model_b": by_model.get("model_b_cdc_plus_neutral_filter", {}).get("expectancy_pct"),
                "gold_only": by_model.get("model_c_gold_only", {}).get("expectancy_pct"),
                "btc_only": by_model.get("model_d_btc_only", {}).get("expectancy_pct"),
                "winner": winner,
            }
        )

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    files_csv = {
        "s4_phase_0_5_event_report.csv": primary_btc_events,
        "s4_gold_event_report.csv": primary_gold_events,
        "s4_conflict_day_analysis.csv": primary_conflicts,
        "s4_cdc_vs_filter_comparison.csv": primary_compare,
        "s4_regime_summary.csv": primary_regime_rows,
        "s4_round_5_window_comparison.csv": round4_window_rows,
        "s4_round_5_btc_bull_value_add.csv": btc_bull_value_add_rows,
        "s4_round_5_neutral_filter_cost.csv": neutral_cost_rows,
        "s4_regime_transition_study.csv": transition_rows,
        "s4_round_5_regime_performance_map.csv": perf_map,
        "s4_btc_only_baseline_comparison.csv": [r for r in round4_window_rows if r["model"] == "model_d_btc_only"],
        "s4_gold_only_baseline_comparison.csv": [r for r in round4_window_rows if r["model"] == "model_c_gold_only"],
    }

    for fn, rows in files_csv.items():
        _write_csv(output_dir / fn, rows)

    files_json = {
        "s4_phase_0_5_event_summary.json": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_source": args.data_source,
            "primary_window": primary_label,
            "primary_rows": len(primary_rows),
            "windows": {k: len(v["rows"]) for k, v in window_metrics.items()},
        },
        "s4_round_5_window_comparison.json": round4_window_rows,
        "s4_round_5_btc_bull_value_add_summary.json": btc_bull_value_add_rows,
        "s4_round_5_neutral_filter_cost_summary.json": {
            "rows": len(neutral_cost_rows),
            "missed_btc_entries": sum(int(r.get("missed_btc_entries") or 0) for r in neutral_cost_rows),
            "avg_missed_return_pct": _safe_mean([float(r["missed_return_pct"]) for r in neutral_cost_rows if r.get("missed_return_pct") is not None]),
            "avg_delayed_entry_days": _safe_mean([float(r["delayed_entry_days"]) for r in neutral_cost_rows if r.get("delayed_entry_days") is not None]),
        },
        "s4_round_5_return_attribution.json": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "windows": {
                label: {m.name: wd["models"][m.name]["attribution"] for m in MODELS}
                for label, wd in window_metrics.items()
            },
        },
        "s4_regime_model_comparison.json": primary_regime_rows,
        "s4_round_5_regime_performance_map.json": perf_map,
        "s4_regime_transition_study.json": transition_rows,
        "s4_btc_only_baseline_summary.json": [r for r in round4_window_rows if r["model"] == "model_d_btc_only"],
        "s4_gold_only_baseline_summary.json": [r for r in round4_window_rows if r["model"] == "model_c_gold_only"],
        "s4_system_expectancy_summary.json": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_source": args.data_source,
            "windows": {
                label: {
                    "requested_window": label,
                    "actual_rows": len(wd["rows"]),
                    "start": wd["rows"][0]["date"].isoformat() if wd["rows"] else None,
                    "end": wd["rows"][-1]["date"].isoformat() if wd["rows"] else None,
                    "models": {m.name: wd["models"][m.name]["system"] for m in MODELS},
                }
                for label, wd in window_metrics.items()
            },
            "regime_rule": f"20d_ratio_return >= {REGIME_BTC_DOM_PCT}% => btc_dominant; <= {REGIME_GOLD_DOM_PCT}% => gold_dominant; else mixed",
        },
    }

    for fn, payload in files_json.items():
        (output_dir / fn).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    memo_path = output_dir / "s4_phase_0_5_round_5_decision_memo.md"
    memo_path.write_text(_decision_memo(memo_rows), encoding="utf-8")

    for fn in sorted(list(files_csv.keys()) + list(files_json.keys()) + [memo_path.name]):
        print(output_dir / fn)


if __name__ == "__main__":
    main()
