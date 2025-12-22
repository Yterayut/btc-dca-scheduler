#!/usr/bin/env python3
"""Convenience CLI for common BTC DCA maintenance tasks."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
import MySQLdb
import requests

# Ensure repository root is on sys.path so we can import project modules
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.flex_preview import sample_payload  # noqa: E402
from services.balance_service import fetch_balances  # noqa: E402

PID_FILE = REPO_ROOT / "scheduler.pid"
LOG_FILE = REPO_ROOT / "scheduler.out"

def _env_flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _python_executable() -> str:
    venv_python = REPO_ROOT / "venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _load_env(env_path: str | None) -> None:
    if env_path:
        load_dotenv(env_path)
        return
    default_env = REPO_ROOT / ".env"
    load_dotenv(default_env if default_env.exists() else None)


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_db_connection():
    return MySQLdb.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        passwd=os.getenv("DB_PASSWORD"),
        db=os.getenv("DB_NAME"),
    )


def _infer_asset_from_purchase(price: float | None, fee_asset: str | None) -> str | None:
    asset_hint = (fee_asset or "").strip().upper()
    if asset_hint in ("BTC", "XAUT", "PAXG"):
        return "BTC" if asset_hint == "BTC" else "GOLD"
    try:
        price_f = float(price or 0.0)
    except (TypeError, ValueError):
        return None
    if price_f <= 0:
        return None
    threshold = float(os.getenv("S4_PNL_BTC_PRICE_THRESHOLD", "10000") or 10000)
    return "BTC" if price_f >= threshold else "GOLD"


def _infer_asset_from_symbol(symbol: str | None) -> str | None:
    sym = (symbol or "").upper()
    if "XAUT" in sym or "PAXG" in sym:
        return "GOLD"
    if "BTC" in sym:
        return "BTC"
    return None


def _load_fifo_open_lots(cursor, exchange: str, asset: str) -> list[dict]:
    lots: list[dict] = []
    cursor.execute(
        """
        SELECT purchase_time, btc_quantity, usdt_amount, btc_price, fee_buy_asset
        FROM purchase_history
        WHERE exchange = %s
        ORDER BY purchase_time ASC
        """,
        (exchange,),
    )
    purchases = cursor.fetchall()
    cursor.execute(
        """
        SELECT symbol, btc_quantity
        FROM sell_history
        WHERE exchange = %s
        ORDER BY sell_time ASC
        """,
        (exchange,),
    )
    sells = cursor.fetchall()

    for purchase_time, qty, notional, price, fee_asset in purchases:
        inferred = _infer_asset_from_purchase(price, fee_asset)
        if inferred != asset:
            continue
        qty_f = float(qty or 0.0)
        if qty_f <= 0:
            continue
        notional_f = float(notional or 0.0)
        cost_per_unit = notional_f / qty_f if qty_f else 0.0
        lots.append({"qty": qty_f, "cost": cost_per_unit, "timestamp": purchase_time})

    for symbol, sell_qty in sells:
        inferred = _infer_asset_from_symbol(symbol)
        if inferred != asset:
            continue
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


def _sum_lots_cost(lots: list[dict]) -> float:
    return sum(float(lot.get("qty") or 0.0) * float(lot.get("cost") or 0.0) for lot in lots)


def cmd_scheduler_start(args: argparse.Namespace) -> None:
    pid = _read_pid()
    if pid and _process_alive(pid):
        if not args.force:
            print(f"Scheduler already running (pid {pid}). Use --force to restart.")
            return
        print(f"Stopping existing scheduler (pid {pid}) before restart...")
        _stop_process(pid, timeout=args.timeout)

    if not _env_flag("STRATEGY_DRY_RUN", False) and not _env_flag("DRY_RUN", False):
        print("WARNING: DRY_RUN is disabled. Starting LIVE scheduler.")

    python_exec = _python_executable()
    log_file = LOG_FILE.open("ab")
    log_file.write(
        f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting scheduler via dca_tool\n".encode()
    )
    log_file.flush()

    proc = subprocess.Popen(
        [python_exec, "main.py"],
        cwd=str(REPO_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    print(f"Scheduler started (pid {proc.pid}). Logs: {LOG_FILE}")


def _stop_process(pid: int, *, timeout: int = 20) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.5)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return False


def cmd_scheduler_stop(args: argparse.Namespace) -> None:
    pid = _read_pid()
    if not pid:
        print("Scheduler not running (no PID file).")
        if PID_FILE.exists():
            PID_FILE.unlink(missing_ok=True)
        return

    if _stop_process(pid, timeout=args.timeout):
        print(f"Scheduler process {pid} stopped.")
    else:
        print(f"Scheduler process {pid} may still be running. Please check manually.")
    PID_FILE.unlink(missing_ok=True)


def cmd_scheduler_status(args: argparse.Namespace) -> None:
    if getattr(args, "verbose", False):
        return cmd_scheduler_status_verbose(args)
    pid = _read_pid()
    alive = bool(pid and _process_alive(pid))
    if alive:
        print(f"Scheduler running (pid {pid}).")
        return
    if pid:
        print(f"PID file present but process {pid} is not running.")
    else:
        print("Scheduler not running.")


def cmd_scheduler_status_verbose(_: argparse.Namespace) -> None:
    pid = _read_pid()
    alive = bool(pid and _process_alive(pid))
    health_port = int(os.getenv("HEALTH_CHECK_PORT", "8001") or 8001)
    health_status = None
    try:
        r = requests.get(f"http://localhost:{health_port}", timeout=2)
        if r.status_code == 200:
            health_status = r.text
    except Exception:
        health_status = None

    dry_run = _env_flag("STRATEGY_DRY_RUN", False) or _env_flag("DRY_RUN", False)
    use_testnet = _env_flag("USE_BINANCE_TESTNET", False) or _env_flag("BINANCE_TESTNET", False) or _env_flag("OKX_TESTNET", False)

    print("Scheduler status:")
    print(f"  pid: {pid or 'none'}")
    print(f"  alive: {alive}")
    print(f"  health_port: {health_port}")
    print(f"  health_status: {health_status or 'unreachable'}")
    print("Environment:")
    print(f"  dry_run: {dry_run}")
    print(f"  use_testnet: {use_testnet}")


def cmd_flex_preview(args: argparse.Namespace) -> None:
    payload = sample_payload(args.kind)
    if args.output:
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"Wrote Flex payload to {args.output}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _comma_to_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _format_balance(value: float) -> str:
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.8f}"


def cmd_balance(args: argparse.Namespace) -> None:
    exchanges = _comma_to_list(args.exchanges)
    assets = _comma_to_list(args.assets)
    try:
        balances = fetch_balances(
            exchanges,
            assets,
            cache_ttl=args.cache_ttl,
            force_refresh=args.force_refresh,
        )
    except Exception as exc:
        print(f"Failed to fetch balances: {exc}")
        return

    if args.json:
        print(json.dumps(balances, ensure_ascii=False, indent=2))
        return

    meta = balances.get("_meta", {})
    for exchange in balances:
        if exchange == "_meta":
            continue
        print(f"[{exchange.upper()}]")
        for asset, entry in balances[exchange].items():
            free = _format_balance(entry.get("free", 0.0))
            locked = _format_balance(entry.get("locked", 0.0))
            stale = " (stale)" if entry.get("stale") else ""
            print(f"  {asset}: free {free} / locked {locked}{stale}")
            error = entry.get("error")
            if error:
                print(textwrap.indent(f"error: {error}", "    "))
        print()
    errors = meta.get("errors")
    if errors:
        print("Errors:")
        for exchange, message in errors.items():
            print(f"  {exchange}: {message}")


def cmd_s4_status(_: argparse.Namespace) -> None:
    """Show detailed S4 strategy status directly from DB."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM strategy_state WHERE mode='s4_multi_leg' LIMIT 1")
        row = cursor.fetchone()
        if not row:
            print("No S4 strategy state found in DB.")
            return

        cols = [d[0] for d in cursor.description]
        data = dict(zip(cols, row))

        metadata = {}
        raw_meta = data.get("metadata_json")
        if raw_meta:
            try:
                if isinstance(raw_meta, str):
                    metadata = json.loads(raw_meta)
                elif isinstance(raw_meta, dict):
                    metadata = raw_meta
            except Exception as exc:
                print(f"Warning: failed to parse metadata_json: {exc}")

        runtime = metadata.get("runtime") or {}
        config = metadata.get("config") or {}

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
        last_rot = dict(zip([d[0] for d in cursor.description], rot_row)) if rot_row else {}

        print("\n" + "=" * 60)
        print("S4 STRATEGY STATUS (Live from DB)")
        print("=" * 60)

        active_asset = runtime.get("active_asset", "UNKNOWN")
        cdc_status = str(runtime.get("last_cdc_status", "N/A")).upper()
        signal_src = runtime.get("signal_source", "N/A")
        last_sig_at = runtime.get("last_signal_at", "N/A")
        print(f"Active Asset: {active_asset}")
        print(f"CDC Signal:   {cdc_status} (Src: {signal_src})")
        print(f"Signal Time:  {last_sig_at}")

        print("-" * 60)
        exp = runtime.get("exposure")
        if isinstance(exp, dict):
            def _to_float(value) -> float:
                try:
                    return float(value or 0.0)
                except (TypeError, ValueError):
                    return 0.0

            total_usd = _to_float(exp.get("total_usd"))
            btc_alloc = _to_float((exp.get("btc") or {}).get("notional_usd"))
            gold_alloc = _to_float((exp.get("gold") or {}).get("notional_usd"))
            btc_w = _to_float((exp.get("btc") or {}).get("weight")) * 100
            gold_w = _to_float((exp.get("gold") or {}).get("weight")) * 100

            cost_btc = 0.0
            cost_gold = 0.0
            cost_total = 0.0
            try:
                lots_btc = _load_fifo_open_lots(cursor, "okx", "BTC")
                lots_gold = _load_fifo_open_lots(cursor, "okx", "GOLD")
                cost_btc = _sum_lots_cost(lots_btc)
                cost_gold = _sum_lots_cost(lots_gold)
                cost_total = cost_btc + cost_gold

                def _pnl_line(value: float, cost: float) -> str:
                    if cost <= 0:
                        return "$0.00 (0.00%)"
                    pnl = value - cost
                    pct = (pnl / cost) * 100.0
                    sign = "+" if pnl >= 0 else ""
                    return f"{sign}${pnl:,.2f} ({pct:.2f}%)"

                btc_line = f"             Cost: ${cost_btc:,.2f} | PnL: {_pnl_line(btc_alloc, cost_btc)}"
                gold_line = f"             Cost: ${cost_gold:,.2f} | PnL: {_pnl_line(gold_alloc, cost_gold)}"
            except Exception as exc:
                print(f"PnL compute error: {exc}")

            print(f"Portfolio:   ${total_usd:,.2f}")
            print(f"  BTC:       ${btc_alloc:,.2f} ({btc_w:.1f}%)")
            print(f"             Cost: ${cost_btc:,.2f} | PnL: {_pnl_line(btc_alloc, cost_btc)}")
            print(f"  GOLD:      ${gold_alloc:,.2f} ({gold_w:.1f}%)")
            print(f"             Cost: ${cost_gold:,.2f} | PnL: {_pnl_line(gold_alloc, cost_gold)}")
            if cost_total > 0:
                pnl_total = total_usd - cost_total
                pct_total = (pnl_total / cost_total) * 100.0
                sign = "+" if pnl_total >= 0 else ""
                print("-" * 60)
                print(f"Total PnL:   {sign}${pnl_total:,.2f} ({pct_total:.2f}%)")
        else:
            print("Portfolio:   N/A (no exposure data)")

        print("-" * 60)
        print("Safety Gates:")
        hist = runtime.get("signal_history")
        hist_len = len(hist) if isinstance(hist, list) else 0
        print(f"  Signal History: {hist_len} entries")
        print(f"  Last Flip:      {runtime.get('last_flip_at') or 'Never'}")
        flips_30d = runtime.get("flip_count_30d", 0)
        max_flips = config.get("max_flips_30d") or os.getenv("S4_MAX_FLIPS_30D", "2")
        print(f"  Flips (30d):    {flips_30d} / {max_flips}")
        if runtime.get("last_hold_reason"):
            print(f"  Last Hold:      {runtime.get('last_hold_reason')}")

        print("-" * 60)
        last_results = runtime.get("last_action_result")
        if isinstance(last_results, list) and last_results:
            res = last_results[0] if isinstance(last_results[0], dict) else {}
            status = res.get("status", "UNKNOWN")
            reason = res.get("reason", "")
            print(f"Last Status:  {status}")
            if reason:
                print(f"  Reason:     {reason}")
        else:
            print("Last Status:  N/A")

        last_err = runtime.get("last_error")
        if isinstance(last_err, dict):
            err_time = last_err.get("at", "")
            err_reason = last_err.get("reason", "")
            err_detail = last_err.get("detail", "")
            print("\nLast Error:")
            print(f"  At:     {err_time}")
            print(f"  Reason: {err_reason}")
            if err_detail:
                print(f"  Detail: {err_detail}")

        if last_rot:
            print(
                f"\nLast DB Rotation: {last_rot.get('executed_at')} "
                f"({last_rot.get('from_asset')} -> {last_rot.get('to_asset')})"
            )

        print("=" * 60 + "\n")
    except Exception as exc:
        print(f"Error fetching S4 status: {exc}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BTC DCA utility toolkit.")
    parser.add_argument("--env-file", help="Path to .env file (defaults to project .env).")

    subparsers = parser.add_subparsers(dest="command", required=True)

    scheduler = subparsers.add_parser("scheduler", help="Manage the trading scheduler.")
    sched_sub = scheduler.add_subparsers(dest="action", required=True)

    start_cmd = sched_sub.add_parser("start", help="Start main.py in the background.")
    start_cmd.add_argument("--force", action="store_true", help="Stop existing process if running.")
    start_cmd.add_argument("--timeout", type=int, default=20, help="Seconds to wait when stopping.")
    start_cmd.set_defaults(func=cmd_scheduler_start)

    stop_cmd = sched_sub.add_parser("stop", help="Stop the running scheduler.")
    stop_cmd.add_argument("--timeout", type=int, default=20, help="Seconds to wait before SIGKILL.")
    stop_cmd.set_defaults(func=cmd_scheduler_stop)

    status_cmd = sched_sub.add_parser("status", help="Show scheduler status.")
    status_cmd.add_argument("--verbose", action="store_true", help="Show extended diagnostics.")
    status_cmd.set_defaults(func=cmd_scheduler_status)

    flex_cmd = subparsers.add_parser("flex", help="Render Flex sample payloads.")
    flex_cmd.add_argument(
        "kind",
        choices=["weekly_dca", "s4_dca"],
        help="Type of Flex preview to render.",
    )
    flex_cmd.add_argument("--output", type=Path, help="Optional file to write JSON payload.")
    flex_cmd.set_defaults(func=cmd_flex_preview)

    balance_cmd = subparsers.add_parser("balance", help="Fetch account balances via adapters.")
    balance_cmd.add_argument(
        "--exchanges",
        default="binance,okx",
        help="Comma-separated exchanges (default: binance,okx).",
    )
    balance_cmd.add_argument(
        "--assets",
        default="BTC,USDT,XAUT",
        help="Comma-separated assets to query (default: BTC,USDT,XAUT).",
    )
    balance_cmd.add_argument(
        "--force-refresh",
        action="store_true",
        help="Bypass balance cache and force adapter refresh.",
    )
    balance_cmd.add_argument(
        "--cache-ttl",
        type=int,
        default=30,
        help="Cache TTL in seconds when not forcing refresh (default: 30).",
    )
    balance_cmd.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON instead of human-readable output.",
    )
    balance_cmd.set_defaults(func=cmd_balance)

    s4_cmd = subparsers.add_parser("s4", help="S4 strategy utilities.")
    s4_sub = s4_cmd.add_subparsers(dest="action", required=True)
    s4_status = s4_sub.add_parser("status", help="Show S4 strategy status.")
    s4_status.set_defaults(func=cmd_s4_status)

    return parser


def main(argv: Iterable[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    _load_env(args.env_file)

    func = getattr(args, "func", None)
    if not func:
        parser.error("No command provided.")
    func(args)


if __name__ == "__main__":
    main()
