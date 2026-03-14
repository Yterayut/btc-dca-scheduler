#!/usr/bin/env python3
"""S4 swap timing backtest (models E/F/G + baselines)."""

from __future__ import annotations

import argparse
import csv
import io
import itertools
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from strategies.s4_neutral_zone import DEFAULT_NEUTRAL_CONFIG, calculate_state
from strategies.s4_utils import cdc_status_from_series, compute_ema_series, fetch_okx_ratio_series

FRED_BTC_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CBBTCUSD"
LBMA_GOLD_PM_URL = "https://prices.lbma.org.uk/json/gold_pm.json"


@dataclass(frozen=True)
class Window:
    id: str
    source: str  # okx_ratio | fred_lbma
    start: date
    end: date


@dataclass
class SwapConfig:
    swap_btc_confirm_days: int = 5
    swap_xau_confirm_days: int = 3
    swap_btc_slope_min: float = 1.0
    swap_xau_slope_max: float = -0.5
    swap_btc_gap_max: float = 3.0
    swap_cooldown_days: int = 14
    swap_require_neutral: bool = True
    partial_enabled: bool = False
    partial_stages: tuple[float, ...] = (0.30, 0.30, 0.40)
    partial_delays: tuple[int, ...] = (0, 3, 5)  # days after previous stage


DEFAULT_WINDOWS = (
    Window("W1_gold_dominant", "okx_ratio", date(2025, 5, 13), date(2026, 3, 7)),
    Window("W2_btc_bull_2016_2018", "fred_lbma", date(2016, 1, 1), date(2018, 1, 31)),
    Window("W3_btc_bull_2020_2021", "fred_lbma", date(2020, 4, 1), date(2021, 11, 30)),
    Window("W4_recent_2023_2025", "fred_lbma", date(2023, 1, 1), date(2025, 12, 31)),
)


def _http_get_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; S4SwapBacktest/1.0)",
            "Accept": "application/json,text/csv,text/plain,*/*",
        },
    )
    with urlopen(req, timeout=30) as resp:  # nosec B310
        return resp.read().decode("utf-8")


def _load_fred_btc() -> dict[date, float]:
    raw = _http_get_text(FRED_BTC_CSV_URL)
    out: dict[date, float] = {}
    for row in csv.DictReader(io.StringIO(raw)):
        d = (row.get("observation_date") or "").strip()
        v = (row.get("CBBTCUSD") or "").strip()
        if not d or not v or v == ".":
            continue
        try:
            dd = date.fromisoformat(d)
            vv = float(v)
        except ValueError:
            continue
        if vv > 0:
            out[dd] = vv
    return out


def _load_lbma_gold() -> dict[date, float]:
    raw = _http_get_text(LBMA_GOLD_PM_URL)
    payload = json.loads(raw)
    out: dict[date, float] = {}
    for item in payload:
        d = str(item.get("d") or "").strip()
        vals = item.get("v") or []
        if not d or not vals:
            continue
        try:
            dd = date.fromisoformat(d)
            usd_pm = float(vals[0])
        except (ValueError, IndexError, TypeError):
            continue
        if usd_pm > 0:
            out[dd] = usd_pm
    return out


def _rows_from_fred_lbma(start: date, end: date) -> list[dict[str, Any]]:
    btc = _load_fred_btc()
    gold = _load_lbma_gold()
    out: list[dict[str, Any]] = []
    for d in sorted(set(btc) & set(gold)):
        if d < start or d > end:
            continue
        b = btc[d]
        g = gold[d]
        if b <= 0 or g <= 0:
            continue
        out.append({"date": d, "btc_price": b, "xau_price": g, "ratio": b / g})
    return out


def _rows_from_okx(start: date, end: date) -> list[dict[str, Any]]:
    series = fetch_okx_ratio_series(use_cache=False, limit=4000, bar="1D")
    out: list[dict[str, Any]] = []
    for ts, ratio in series:
        d = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).date()
        if d < start or d > end:
            continue
        r = float(ratio)
        if r <= 0:
            continue
        out.append({"date": d, "btc_price": r, "xau_price": 1.0, "ratio": r})
    return out


def _enrich_signals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    rows = sorted(rows, key=lambda x: x["date"])
    ratios = [float(r["ratio"]) for r in rows]
    ema12 = compute_ema_series(ratios, 12)
    ema26 = compute_ema_series(ratios, 26)

    cdc_values: list[float] = []
    for i, r in enumerate(rows):
        cdc_values.append(float(r["ratio"]))
        cdc = cdc_status_from_series(cdc_values)
        state, metrics = calculate_state(
            ema12=float(ema12[i]),
            ema26=float(ema26[i]),
            ema12_history=list(reversed(ema12[: i + 1])),
            config=DEFAULT_NEUTRAL_CONFIG,
        )
        r["cdc_status"] = str(cdc.get("status") or "").lower() or None
        r["neutral_state"] = state.value if state else None
        r["slope_pct"] = None if not metrics else float(metrics.get("slope_pct") or 0.0)
        r["gap_pct"] = None if not metrics else float(metrics.get("ema_gap_pct") or 0.0)
    return rows


def _pct(a: float, b: float) -> float:
    if a <= 0:
        return 0.0
    return (b / a - 1.0) * 100.0


def _daily_return_from_alloc(prev_ratio: float, cur_ratio: float, alloc_btc: float) -> float:
    btc_ret = cur_ratio / prev_ratio - 1.0
    xau_ret = prev_ratio / cur_ratio - 1.0
    return alloc_btc * btc_ret + (1.0 - alloc_btc) * xau_ret


def _simulate_abcd(rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    alloc_hist: list[float] = []
    for i, r in enumerate(rows):
        cdc = str(r.get("cdc_status") or "")
        neutral = str(r.get("neutral_state") or "")
        if model == "A":
            alloc = 1.0 if cdc == "up" else 0.0
        elif model == "B":
            alloc = 1.0 if (cdc == "up" and neutral == "btc_signal") else 0.0
        elif model == "C":
            alloc = 0.0
        elif model == "D":
            alloc = 1.0
        else:
            raise ValueError(model)
        alloc_hist.append(alloc)
        if i == 0:
            continue
        dret = _daily_return_from_alloc(float(rows[i - 1]["ratio"]), float(r["ratio"]), alloc_hist[i - 1])
        equity *= 1.0 + dret
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    switches = sum(1 for i in range(1, len(alloc_hist)) if (alloc_hist[i] >= 0.5) != (alloc_hist[i - 1] >= 0.5))
    return {
        "model": model,
        "total_return_pct": (equity - 1.0) * 100.0,
        "max_drawdown_pct": max_dd * 100.0,
        "switch_count": switches,
        "final_alloc_btc": alloc_hist[-1] if alloc_hist else 0.0,
    }


def _consecutive_cdc(rows: list[dict[str, Any]], idx: int, status: str, days: int) -> bool:
    if idx - days + 1 < 0:
        return False
    for j in range(idx - days + 1, idx + 1):
        if str(rows[j].get("cdc_status") or "") != status:
            return False
    return True


def _evaluate_swap_signal(rows: list[dict[str, Any]], idx: int, holding: str, cfg: SwapConfig, days_since_swap: int) -> tuple[str, str]:
    row = rows[idx]
    slope = float(row.get("slope_pct") or 0.0)
    gap = float(row.get("gap_pct") or 0.0)
    neutral = str(row.get("neutral_state") or "")

    if days_since_swap < cfg.swap_cooldown_days:
        return "HOLD", "cooldown"

    if holding == "XAU":
        if not _consecutive_cdc(rows, idx, "up", cfg.swap_btc_confirm_days):
            return "HOLD", "cdc_confirm"
        if cfg.swap_require_neutral and neutral != "btc_signal":
            return "HOLD", "neutral"
        if slope < cfg.swap_btc_slope_min:
            return "HOLD", "slope"
        if gap > cfg.swap_btc_gap_max:
            return "HOLD", "gap"
        return "SWAP_TO_BTC", "all_5_gates_passed"

    if holding == "BTC":
        if not _consecutive_cdc(rows, idx, "down", cfg.swap_xau_confirm_days):
            return "HOLD", "cdc_confirm"
        if slope > cfg.swap_xau_slope_max:
            return "HOLD", "slope"
        return "SWAP_TO_XAU", "all_3_gates_passed"

    return "HOLD", "unknown_holding"


def _best_asset_next_7d(rows: list[dict[str, Any]], idx: int) -> str | None:
    end = min(len(rows) - 1, idx + 7)
    if end <= idx:
        return None
    r0 = float(rows[idx]["ratio"])
    r1 = float(rows[end]["ratio"])
    return "BTC" if r1 > r0 else "XAU"


def _simulate_efg(rows: list[dict[str, Any]], model: str, cfg: SwapConfig) -> dict[str, Any]:
    assert model in {"E", "F", "G"}

    alloc_btc = 0.0  # start defensive in XAU
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    last_swap_idx = -9999

    swaps: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None

    dca_checks = 0
    dca_hits = 0

    def dominant_holding(a_btc: float) -> str:
        return "BTC" if a_btc >= 0.5 else "XAU"

    cdc_flip_idx: int | None = None
    prev_cdc = None

    for i, r in enumerate(rows):
        cur_cdc = str(r.get("cdc_status") or "")
        if prev_cdc is not None and cur_cdc != prev_cdc:
            cdc_flip_idx = i
        prev_cdc = cur_cdc

        # DCA-direction accuracy check (weekly cadence)
        if i % 7 == 0:
            dca_checks += 1
            target = "BTC" if cur_cdc == "up" else "XAU"
            best = _best_asset_next_7d(rows, i)
            if best is not None and target == best:
                dca_hits += 1

        # swap decisions
        if model == "E":
            pass  # no swap
        else:
            holding = dominant_holding(alloc_btc)
            days_since_swap = i - last_swap_idx

            if model == "F":
                action, reason = _evaluate_swap_signal(rows, i, holding, cfg, days_since_swap)
                if action == "SWAP_TO_BTC" and alloc_btc < 1.0:
                    before = alloc_btc
                    alloc_btc = 1.0
                    swaps.append({
                        "date": r["date"].isoformat(),
                        "model": model,
                        "direction": "XAU_TO_BTC",
                        "stage_pct": 1.0,
                        "alloc_btc_before": before,
                        "alloc_btc_after": alloc_btc,
                        "reason": reason,
                        "swap_lag_days": None if cdc_flip_idx is None else max(0, i - cdc_flip_idx),
                    })
                    last_swap_idx = i
                elif action == "SWAP_TO_XAU" and alloc_btc > 0.0:
                    before = alloc_btc
                    alloc_btc = 0.0
                    swaps.append({
                        "date": r["date"].isoformat(),
                        "model": model,
                        "direction": "BTC_TO_XAU",
                        "stage_pct": 1.0,
                        "alloc_btc_before": before,
                        "alloc_btc_after": alloc_btc,
                        "reason": reason,
                        "swap_lag_days": None if cdc_flip_idx is None else max(0, i - cdc_flip_idx),
                    })
                    last_swap_idx = i

            if model == "G":
                if pending is not None:
                    if i >= pending["next_idx"]:
                        action, reason = _evaluate_swap_signal(rows, i, pending["holding_start"], cfg, i - last_swap_idx)
                        if action == "HOLD":
                            pending = None
                            last_swap_idx = i
                        else:
                            stage_pct = cfg.partial_stages[pending["stage"]]
                            before = alloc_btc
                            if pending["direction"] == "XAU_TO_BTC":
                                alloc_btc = alloc_btc + stage_pct * (1.0 - alloc_btc)
                            else:
                                alloc_btc = alloc_btc - stage_pct * alloc_btc
                            swaps.append({
                                "date": r["date"].isoformat(),
                                "model": model,
                                "direction": pending["direction"],
                                "stage_pct": stage_pct,
                                "alloc_btc_before": before,
                                "alloc_btc_after": alloc_btc,
                                "reason": "partial_stage",
                                "swap_lag_days": None if cdc_flip_idx is None else max(0, i - cdc_flip_idx),
                            })
                            last_swap_idx = i
                            pending["stage"] += 1
                            if pending["stage"] >= len(cfg.partial_stages):
                                pending = None
                            else:
                                delay = cfg.partial_delays[min(pending["stage"], len(cfg.partial_delays) - 1)]
                                pending["next_idx"] = i + delay
                else:
                    action, reason = _evaluate_swap_signal(rows, i, holding, cfg, days_since_swap)
                    if action in {"SWAP_TO_BTC", "SWAP_TO_XAU"}:
                        pending = {
                            "direction": "XAU_TO_BTC" if action == "SWAP_TO_BTC" else "BTC_TO_XAU",
                            "holding_start": holding,
                            "stage": 0,
                            "next_idx": i,
                        }

        if i == 0:
            continue
        prev_ratio = float(rows[i - 1]["ratio"])
        cur_ratio = float(r["ratio"])
        dret = _daily_return_from_alloc(prev_ratio, cur_ratio, alloc_btc)
        equity *= 1.0 + dret
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)

    # failed swaps: opposite direction within 30 days after first stage
    failed = 0
    for j in range(1, len(swaps)):
        d0 = date.fromisoformat(swaps[j - 1]["date"])
        d1 = date.fromisoformat(swaps[j]["date"])
        if (d1 - d0).days <= 30 and swaps[j - 1]["direction"] != swaps[j]["direction"]:
            failed += 1

    lags = [s["swap_lag_days"] for s in swaps if s.get("swap_lag_days") is not None]
    swap_count = sum(1 for s in swaps if abs(float(s["stage_pct"]) - 1.0) < 1e-9 or float(s["stage_pct"]) == cfg.partial_stages[0])

    return {
        "model": model,
        "total_return_pct": (equity - 1.0) * 100.0,
        "max_drawdown_pct": max_dd * 100.0,
        "swap_count": swap_count,
        "swap_stage_count": len(swaps),
        "swap_avg_lag_days": (sum(lags) / len(lags)) if lags else None,
        "false_swap_count": failed,
        "dca_accuracy_pct": (dca_hits / dca_checks * 100.0) if dca_checks else None,
        "final_alloc_btc": alloc_btc,
        "events": swaps,
    }


def _run_window(w: Window, cfg: SwapConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if w.source == "okx_ratio":
        rows = _rows_from_okx(w.start, w.end)
    elif w.source == "fred_lbma":
        rows = _rows_from_fred_lbma(w.start, w.end)
    else:
        raise ValueError(w.source)
    rows = _enrich_signals(rows)
    if len(rows) < 30:
        return [], []

    base = {m: _simulate_abcd(rows, m) for m in ("A", "B", "C", "D")}
    efg = {m: _simulate_efg(rows, m, cfg) for m in ("E", "F", "G")}

    btc_only_ret = base["D"]["total_return_pct"]
    gold_only_ret = base["C"]["total_return_pct"]

    summary_rows: list[dict[str, Any]] = []
    for key, obj in {**base, **efg}.items():
        ret = obj["total_return_pct"]
        btc_capture = (ret / btc_only_ret * 100.0) if btc_only_ret not in (0, None) else None
        gold_prox = None
        if gold_only_ret not in (0, None):
            gold_prox = 100.0 * (1.0 - abs(ret - gold_only_ret) / abs(gold_only_ret))
        summary_rows.append(
            {
                "window_id": w.id,
                "source": w.source,
                "start": rows[0]["date"].isoformat(),
                "end": rows[-1]["date"].isoformat(),
                "rows": len(rows),
                "model": key,
                "total_return_pct": round(ret, 4),
                "max_drawdown_pct": round(obj.get("max_drawdown_pct", 0.0), 4),
                "swap_count": int(obj.get("swap_count", obj.get("switch_count", 0))),
                "swap_stage_count": int(obj.get("swap_stage_count", obj.get("swap_count", obj.get("switch_count", 0)))),
                "swap_avg_lag_days": None if obj.get("swap_avg_lag_days") is None else round(float(obj["swap_avg_lag_days"]), 4),
                "false_swap_count": int(obj.get("false_swap_count", 0)),
                "dca_accuracy_pct": None if obj.get("dca_accuracy_pct") is None else round(float(obj["dca_accuracy_pct"]), 4),
                "btc_capture_pct": None if btc_capture is None else round(float(btc_capture), 4),
                "gold_proximity_pct": None if gold_prox is None else round(float(gold_prox), 4),
            }
        )

    event_rows: list[dict[str, Any]] = []
    for m in ("E", "F", "G"):
        for i, ev in enumerate(efg[m]["events"], start=1):
            row = dict(ev)
            row["window_id"] = w.id
            row["event_id"] = i
            event_rows.append(row)

    return summary_rows, event_rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    headers = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)


def _load_window_rows(w: Window) -> list[dict[str, Any]]:
    if w.source == "okx_ratio":
        rows = _rows_from_okx(w.start, w.end)
    elif w.source == "fred_lbma":
        rows = _rows_from_fred_lbma(w.start, w.end)
    else:
        raise ValueError(w.source)
    return _enrich_signals(rows)


def _model_summary_from_rows(window_id: str, source: str, rows: list[dict[str, Any]], model: str, obj: dict[str, Any], btc_only_ret: float, gold_only_ret: float) -> dict[str, Any]:
    ret = float(obj["total_return_pct"])
    btc_capture = (ret / btc_only_ret * 100.0) if btc_only_ret not in (0, None) else None
    gold_prox = None
    if gold_only_ret not in (0, None):
        gold_prox = 100.0 * (1.0 - abs(ret - gold_only_ret) / abs(gold_only_ret))
    return {
        "window_id": window_id,
        "source": source,
        "start": rows[0]["date"].isoformat(),
        "end": rows[-1]["date"].isoformat(),
        "rows": len(rows),
        "model": model,
        "total_return_pct": round(ret, 4),
        "max_drawdown_pct": round(float(obj.get("max_drawdown_pct", 0.0)), 4),
        "swap_count": int(obj.get("swap_count", obj.get("switch_count", 0))),
        "swap_stage_count": int(obj.get("swap_stage_count", obj.get("swap_count", obj.get("switch_count", 0)))),
        "swap_avg_lag_days": None if obj.get("swap_avg_lag_days") is None else round(float(obj["swap_avg_lag_days"]), 4),
        "false_swap_count": int(obj.get("false_swap_count", 0)),
        "dca_accuracy_pct": None if obj.get("dca_accuracy_pct") is None else round(float(obj["dca_accuracy_pct"]), 4),
        "btc_capture_pct": None if btc_capture is None else round(float(btc_capture), 4),
        "gold_proximity_pct": None if gold_prox is None else round(float(gold_prox), 4),
    }


def _evaluate_models_on_rows(window: Window, rows: list[dict[str, Any]], cfg: SwapConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if len(rows) < 30:
        return [], [], {}
    base = {m: _simulate_abcd(rows, m) for m in ("A", "B", "C", "D")}
    efg = {m: _simulate_efg(rows, m, cfg) for m in ("E", "F", "G")}
    metrics = {**base, **efg}
    btc_only_ret = float(base["D"]["total_return_pct"])
    gold_only_ret = float(base["C"]["total_return_pct"])

    summary_rows = [
        _model_summary_from_rows(window.id, window.source, rows, model, metrics[model], btc_only_ret, gold_only_ret)
        for model in ("A", "B", "C", "D", "E", "F", "G")
    ]

    event_rows: list[dict[str, Any]] = []
    for m in ("E", "F", "G"):
        for i, ev in enumerate(efg[m]["events"], start=1):
            row = dict(ev)
            row["window_id"] = window.id
            row["event_id"] = i
            event_rows.append(row)
    return summary_rows, event_rows, metrics


def _config_to_dict(cfg: SwapConfig) -> dict[str, Any]:
    return {
        "swap_btc_confirm_days": cfg.swap_btc_confirm_days,
        "swap_xau_confirm_days": cfg.swap_xau_confirm_days,
        "swap_btc_slope_min": cfg.swap_btc_slope_min,
        "swap_xau_slope_max": cfg.swap_xau_slope_max,
        "swap_btc_gap_max": cfg.swap_btc_gap_max,
        "swap_cooldown_days": cfg.swap_cooldown_days,
        "swap_require_neutral": cfg.swap_require_neutral,
        "partial_enabled": cfg.partial_enabled,
        "partial_stages": list(cfg.partial_stages),
        "partial_delays": list(cfg.partial_delays),
    }


def _iter_configs(mode: str) -> list[SwapConfig]:
    if mode == "phase_a":
        cfgs: list[SwapConfig] = []
        for btc_confirm, btc_slope, btc_gap, cooldown, need_neutral in itertools.product(
            (3, 5, 7, 10),
            (0.5, 1.0, 1.5, 2.0),
            (2.0, 3.0, 4.0, 5.0),
            (7, 14, 21),
            (True, False),
        ):
            cfgs.append(
                SwapConfig(
                    swap_btc_confirm_days=btc_confirm,
                    swap_btc_slope_min=btc_slope,
                    swap_btc_gap_max=btc_gap,
                    swap_cooldown_days=cooldown,
                    swap_require_neutral=need_neutral,
                    swap_xau_confirm_days=3,
                    swap_xau_slope_max=-0.5,
                )
            )
        return cfgs

    if mode == "full":
        cfgs = []
        for btc_confirm, btc_slope, btc_gap, cooldown, xau_confirm, xau_slope, need_neutral in itertools.product(
            (3, 5, 7, 10),
            (0.5, 1.0, 1.5, 2.0),
            (2.0, 3.0, 4.0, 5.0),
            (7, 14, 21),
            (2, 3, 5),
            (-1.0, -0.5, 0.0),
            (True, False),
        ):
            cfgs.append(
                SwapConfig(
                    swap_btc_confirm_days=btc_confirm,
                    swap_btc_slope_min=btc_slope,
                    swap_btc_gap_max=btc_gap,
                    swap_cooldown_days=cooldown,
                    swap_xau_confirm_days=xau_confirm,
                    swap_xau_slope_max=xau_slope,
                    swap_require_neutral=need_neutral,
                )
            )
        return cfgs

    raise ValueError(mode)


def _slice_holdout(rows: list[dict[str, Any]], days: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(rows) <= days + 30:
        return rows, []
    return rows[:-days], rows[-days:]


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y"}


def main() -> None:
    parser = argparse.ArgumentParser(description="S4 swap timing backtest")
    parser.add_argument("--output-dir", type=Path, default=Path("log/s4_swap_backtest"))
    parser.add_argument("--sweep-mode", choices=("none", "phase_a", "full"), default="phase_a")
    parser.add_argument("--sweep-limit", type=int, default=0, help="0 = no limit")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--holdout-days", type=int, default=60)
    args = parser.parse_args()

    all_summary: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    window_rows: dict[str, list[dict[str, Any]]] = {}

    # Load rows once per window for reuse in default + sweep/holdout.
    for w in DEFAULT_WINDOWS:
        rows = _load_window_rows(w)
        window_rows[w.id] = rows
        s, e, _ = _evaluate_models_on_rows(w, rows, SwapConfig())
        all_summary.extend(s)
        all_events.extend(e)

    out = args.output_dir
    _write_csv(out / "s4_swap_backtest_summary_all_windows.csv", all_summary)
    _write_csv(out / "s4_swap_backtest_events_all_windows.csv", all_events)

    (out / "s4_swap_backtest_config.json").write_text(
        json.dumps({"default_config": _config_to_dict(SwapConfig()), "windows": [w.__dict__ for w in DEFAULT_WINDOWS]}, indent=2, default=str),
        encoding="utf-8",
    )

    # top model by return per window (default config run)
    top: dict[str, Any] = {}
    for w in {r["window_id"] for r in all_summary}:
        rows = [r for r in all_summary if r["window_id"] == w]
        best = max(rows, key=lambda x: float(x["total_return_pct"]))
        top[w] = best
    (out / "s4_swap_backtest_top_configs.json").write_text(json.dumps(top, indent=2), encoding="utf-8")

    sweep_rows: list[dict[str, Any]] = []
    holdout_payload: dict[str, Any] = {"generated": False}
    if args.sweep_mode != "none":
        cfgs = _iter_configs(args.sweep_mode)
        if args.sweep_limit and args.sweep_limit > 0:
            cfgs = cfgs[: args.sweep_limit]

        for idx, cfg in enumerate(cfgs, start=1):
            for w in DEFAULT_WINDOWS:
                tr_rows = window_rows[w.id]
                if not tr_rows:
                    continue
                tr_rows, _ = _slice_holdout(tr_rows, args.holdout_days)
                if len(tr_rows) < 30:
                    continue
                _, _, metrics = _evaluate_models_on_rows(w, tr_rows, cfg)
                for model in ("F", "G"):
                    m = metrics[model]
                    sweep_rows.append(
                        {
                            "config_id": idx,
                            "window_id": w.id,
                            "model": model,
                            "total_return_pct": round(float(m["total_return_pct"]), 4),
                            "max_drawdown_pct": round(float(m["max_drawdown_pct"]), 4),
                            "swap_count": int(m.get("swap_count", 0)),
                            "swap_stage_count": int(m.get("swap_stage_count", 0)),
                            "swap_avg_lag_days": None if m.get("swap_avg_lag_days") is None else round(float(m["swap_avg_lag_days"]), 4),
                            "false_swap_count": int(m.get("false_swap_count", 0)),
                            "dca_accuracy_pct": None if m.get("dca_accuracy_pct") is None else round(float(m["dca_accuracy_pct"]), 4),
                            **_config_to_dict(cfg),
                        }
                    )

        _write_csv(out / "s4_swap_param_sweep_results.csv", sweep_rows)

        # Rank configs for model F by minimax (maximize worst-window return), tie-break by avg return.
        by_cfg: dict[int, list[dict[str, Any]]] = {}
        for r in sweep_rows:
            if r["model"] != "F":
                continue
            by_cfg.setdefault(int(r["config_id"]), []).append(r)
        ranking: list[dict[str, Any]] = []
        for cid, rows in by_cfg.items():
            if not rows:
                continue
            returns = [float(r["total_return_pct"]) for r in rows]
            dds = [float(r["max_drawdown_pct"]) for r in rows]
            avg_ret = sum(float(r["total_return_pct"]) for r in rows) / len(rows)
            avg_dd = sum(float(r["max_drawdown_pct"]) for r in rows) / len(rows)
            avg_false = sum(int(r["false_swap_count"]) for r in rows) / len(rows)
            worst_ret = min(returns)
            worst_dd = min(dds)
            cfg_fields = _config_to_dict(
                SwapConfig(
                    swap_btc_confirm_days=int(rows[0]["swap_btc_confirm_days"]),
                    swap_xau_confirm_days=int(rows[0]["swap_xau_confirm_days"]),
                    swap_btc_slope_min=float(rows[0]["swap_btc_slope_min"]),
                    swap_xau_slope_max=float(rows[0]["swap_xau_slope_max"]),
                    swap_btc_gap_max=float(rows[0]["swap_btc_gap_max"]),
                    swap_cooldown_days=int(rows[0]["swap_cooldown_days"]),
                    swap_require_neutral=_to_bool(rows[0]["swap_require_neutral"]),
                )
            )
            ranking.append(
                {
                    "config_id": cid,
                    "train_windows": len(rows),
                    "worst_window_return_pct_F": round(worst_ret, 4),
                    "worst_window_drawdown_pct_F": round(worst_dd, 4),
                    "avg_total_return_pct_F": round(avg_ret, 4),
                    "avg_max_drawdown_pct_F": round(avg_dd, 4),
                    "avg_false_swaps_F": round(avg_false, 4),
                    **cfg_fields,
                }
            )
        ranking.sort(key=lambda x: (x["worst_window_return_pct_F"], x["avg_total_return_pct_F"]), reverse=True)
        top_cfgs = ranking[: max(1, args.top_k)]
        (out / "s4_swap_backtest_top_configs.json").write_text(json.dumps(top_cfgs, indent=2), encoding="utf-8")

        # Holdout evaluation on every window last N days using top-k configs.
        holdout_payload = {
            "generated": True,
            "windows": [w.id for w in DEFAULT_WINDOWS],
            "holdout_days": args.holdout_days,
            "results": [],
        }
        for cfg_row in top_cfgs:
            cfg = SwapConfig(
                swap_btc_confirm_days=int(cfg_row["swap_btc_confirm_days"]),
                swap_xau_confirm_days=int(cfg_row["swap_xau_confirm_days"]),
                swap_btc_slope_min=float(cfg_row["swap_btc_slope_min"]),
                swap_xau_slope_max=float(cfg_row["swap_xau_slope_max"]),
                swap_btc_gap_max=float(cfg_row["swap_btc_gap_max"]),
                swap_cooldown_days=int(cfg_row["swap_cooldown_days"]),
                swap_require_neutral=_to_bool(cfg_row["swap_require_neutral"]),
            )
            cfg_result: dict[str, Any] = {"config_id": int(cfg_row["config_id"]), "per_window": []}
            for w in DEFAULT_WINDOWS:
                train_rows, hold_rows = _slice_holdout(window_rows[w.id], args.holdout_days)
                if len(train_rows) < 30 or len(hold_rows) < 30:
                    continue
                _, _, mtrain = _evaluate_models_on_rows(w, train_rows, cfg)
                _, _, mhold = _evaluate_models_on_rows(w, hold_rows, cfg)
                cfg_result["per_window"].append(
                    {
                        "window_id": w.id,
                        "train_F_return_pct": round(float(mtrain["F"]["total_return_pct"]), 4) if mtrain else None,
                        "holdout_F_return_pct": round(float(mhold["F"]["total_return_pct"]), 4) if mhold else None,
                        "train_G_return_pct": round(float(mtrain["G"]["total_return_pct"]), 4) if mtrain else None,
                        "holdout_G_return_pct": round(float(mhold["G"]["total_return_pct"]), 4) if mhold else None,
                    }
                )
            holdout_payload["results"].append(cfg_result)
        (out / "s4_swap_backtest_holdout.json").write_text(json.dumps(holdout_payload, indent=2), encoding="utf-8")

    print(out / "s4_swap_backtest_summary_all_windows.csv")
    print(out / "s4_swap_backtest_events_all_windows.csv")
    print(out / "s4_swap_backtest_config.json")
    print(out / "s4_swap_backtest_top_configs.json")
    if args.sweep_mode != "none":
        print(out / "s4_swap_param_sweep_results.csv")
        print(out / "s4_swap_backtest_holdout.json")


if __name__ == "__main__":
    main()
