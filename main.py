import os
import json
import logging
import time
from logging.handlers import RotatingFileHandler
import asyncio
import threading
import socket
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from binance.exceptions import BinanceAPIException
from datetime import datetime, timedelta
from pytz import timezone, utc
import requests
from collections.abc import Sequence
from notify import (
    send_line_message,
    send_line_message_with_retry,
    notify_cdc_transition,
    notify_half_sell_executed,
    notify_half_sell_skipped,
    notify_weekly_dca_buy,
    notify_weekly_dca_skipped,
    notify_weekly_dca_skipped_exchange,
    notify_reserve_buy_executed,
    notify_reserve_buy_skipped_min_notional,
    notify_liquidity_blocked,
    notify_security_alert,
    notify_s4_rotation,
    notify_s4_dca_buy,
    notify_daily_heartbeat,
)
from exchanges.factory import get_adapter
from services.balance_service import fetch_balances
from strategies.base import StrategyActionType, ActionStatus, ActionResult, StrategyAction
from strategies.cdc import CdcDcaStrategy, WeeklyDcaDecisionInput, TransitionDecisionInput
from strategies.runtime import StrategyOrchestrator
from strategies.s4_utils import (
    get_s4_dca_target_asset as _s4_dca_target_asset,
    plan_s4_rotation as _plan_s4_rotation,
    resolve_s4_target_allocations as _resolve_s4_target_allocations,
    fetch_okx_ratio_signal as _fetch_okx_ratio_signal,
    fetch_okx_ratio_series as _fetch_okx_ratio_series,
    compute_ema_series as _compute_ema_series,
)
from strategies.s4_neutral_zone import calculate_state as _s4_neutral_state, DEFAULT_NEUTRAL_CONFIG
from strategies.s4_observability import (
    mismatch_severity as _s4_mismatch_severity,
    next_unlock_from_gate_reason as _s4_next_unlock_from_gate_reason,
)
from compliance import record_event as log_compliance_event
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from services.bootstrap import create_binance_client, env_flag, load_required_env_vars
from services.db import db_transaction as _db_transaction, get_db_connection as _get_db_connection
from services.state import (
    load_strategy_record_with_connection as _load_strategy_record,
    load_strategy_state_with_connection as _load_strategy_state,
    record_fee_totals_with_transaction as _record_fee_totals,
    record_rotation_event_with_transaction as _record_rotation_event,
    save_strategy_metadata_with_transaction as _save_strategy_metadata,
    save_strategy_state_with_connection as _save_strategy_state,
)

try:
    from utils import get_btc_price, get_gold_price
except Exception:
    get_btc_price = None
    get_gold_price = None

required_env_vars = load_required_env_vars()

def _env_flag(name: str, default: bool = False) -> bool:
    return env_flag(name, default)

USE_TESTNET = _env_flag('USE_BINANCE_TESTNET', False) or _env_flag('BINANCE_TESTNET', False) or _env_flag('OKX_TESTNET', False)
DRY_RUN = _env_flag('STRATEGY_DRY_RUN', False) or _env_flag('DRY_RUN', False)

client = create_binance_client(required_env_vars, testnet=USE_TESTNET)

def is_dry_run() -> bool:
    return DRY_RUN

strategy_orchestrator = StrategyOrchestrator()

LIQUIDITY_MAX_SPREAD_PCT = float(os.getenv('LIQUIDITY_MAX_SPREAD_PCT', '0.60'))
ENABLE_DEPTH_GUARD = _env_flag('ENABLE_DEPTH_GUARD', True)
ENABLE_TWAP_GUARD = _env_flag('ENABLE_TWAP_GUARD', True)
DEPTH_GUARD_MIN_NOTIONAL_USDT = float(os.getenv('DEPTH_GUARD_MIN_NOTIONAL_USDT', '1000000'))
DEPTH_GUARD_BAND_PCT = float(os.getenv('DEPTH_GUARD_BAND_PCT', '1.0'))
DEPTH_GUARD_DEPTH_LEVEL = int(os.getenv('DEPTH_GUARD_DEPTH_LEVEL', '40'))
TWAP_GUARD_WINDOW_MINUTES = int(os.getenv('TWAP_GUARD_WINDOW_MINUTES', '15'))
TWAP_GUARD_MAX_DEVIATION_PCT = float(os.getenv('TWAP_GUARD_MAX_DEVIATION_PCT', '1.5'))
ANOMALY_PNL_THRESHOLD_USDT = float(os.getenv('ANOMALY_PNL_THRESHOLD_USDT', '50000'))
ANOMALY_NOTIONAL_THRESHOLD_USDT = float(os.getenv('ANOMALY_NOTIONAL_THRESHOLD_USDT', '250000'))

DB_DEDUPE_ENABLED = _env_flag('DB_DEDUPE_ENABLED', False)
SCHEDULER_DB_LOCK_ENABLED = _env_flag('SCHEDULER_DB_LOCK_ENABLED', False)
SCHEDULER_DB_LOCK_NAME = os.getenv('SCHEDULER_DB_LOCK_NAME', 'dca_scheduler')
SCHEDULER_DB_LOCK_TIMEOUT = int(os.getenv('SCHEDULER_DB_LOCK_TIMEOUT', '1') or 1)

DEDUPE_CLEANUP_ENABLED = _env_flag('DEDUPE_CLEANUP_ENABLED', False)
DEDUPE_CLEANUP_DAYS = int(os.getenv('DEDUPE_CLEANUP_DAYS', '30') or 30)
DEDUPE_CLEANUP_INTERVAL_HOURS = float(os.getenv('DEDUPE_CLEANUP_INTERVAL_HOURS', '6') or 6)

# --- S4 Hardening (gates) ---
S4_HARDENING_ENABLED = _env_flag('S4_HARDENING_ENABLED', False)
S4_RATIO_TTL_MINUTES = int(os.getenv('S4_RATIO_TTL_MINUTES', '30') or 30)
S4_CONFIRM_DAYS = int(os.getenv('S4_CONFIRM_DAYS', '2') or 2)
S4_COOLDOWN_DAYS = int(os.getenv('S4_COOLDOWN_DAYS', '3') or 3)
S4_MAX_FLIPS_30D = int(os.getenv('S4_MAX_FLIPS_30D', '2') or 2)

# --- S4 Execution Hardening (OKX only) ---
S4_EXEC_HARDENING_ENABLED = _env_flag('S4_EXEC_HARDENING_ENABLED', False)
S4_LIMIT_FIRST_SECONDS = int(os.getenv('S4_LIMIT_FIRST_SECONDS', '45') or 45)
S4_IOC_FALLBACK_ENABLED = _env_flag('S4_IOC_FALLBACK_ENABLED', False)
S4_MAX_SPREAD_PCT_BTC = float(os.getenv('S4_MAX_SPREAD_PCT_BTC', '0.60') or 0.60)
S4_MAX_SPREAD_PCT_XAUT = float(os.getenv('S4_MAX_SPREAD_PCT_XAUT', '0.50') or 0.50)
# --- S4 DCA-first mode ---
S4_DCA_FOLLOW_CDC_ONLY = _env_flag('S4_DCA_FOLLOW_CDC_ONLY', True)
S4_SWAP_EXEC_ENABLED = _env_flag('S4_SWAP_EXEC_ENABLED', False)
S4_SHADOW_SWAP_LOG_ENABLED = _env_flag('S4_SHADOW_SWAP_LOG_ENABLED', True)
S4_SHADOW_BTC_CONFIRM_DAYS = int(os.getenv('S4_SHADOW_BTC_CONFIRM_DAYS', '3') or 3)
S4_SHADOW_XAU_CONFIRM_DAYS = int(os.getenv('S4_SHADOW_XAU_CONFIRM_DAYS', '5') or 5)
S4_SHADOW_BTC_SLOPE_MIN = float(os.getenv('S4_SHADOW_BTC_SLOPE_MIN', '2.0') or 2.0)
S4_SHADOW_XAU_SLOPE_MAX = float(os.getenv('S4_SHADOW_XAU_SLOPE_MAX', '-0.5') or -0.5)
S4_SHADOW_BTC_GAP_MAX = float(os.getenv('S4_SHADOW_BTC_GAP_MAX', '2.0') or 2.0)
S4_SHADOW_COOLDOWN_DAYS = int(os.getenv('S4_SHADOW_COOLDOWN_DAYS', '7') or 7)
S4_SHADOW_REQUIRE_NEUTRAL = _env_flag('S4_SHADOW_REQUIRE_NEUTRAL', True)


def ensure_action_dedupe_table() -> None:
    """Create action_dedupe table when DB dedupe is enabled."""
    if not DB_DEDUPE_ENABLED:
        return
    try:
        with db_transaction() as (cursor, _):
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
        logging.warning(f"ensure_action_dedupe_table failed: {exc}")


def claim_dedupe_key(dedupe_key: str, request_id: str) -> bool:
    """Try to claim a dedupe key in DB. Returns True if new, False if duplicate."""
    if not DB_DEDUPE_ENABLED or not dedupe_key:
        return True
    try:
        with db_transaction() as (cursor, _):
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


def cleanup_action_dedupe() -> int:
    """Delete old action_dedupe rows older than configured retention days."""
    if not (DB_DEDUPE_ENABLED and DEDUPE_CLEANUP_ENABLED):
        return 0
    days = max(DEDUPE_CLEANUP_DAYS, 1)
    try:
        with db_transaction() as (cursor, _):
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


def _format_dt_local(dt: datetime) -> str:
    try:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def _format_dt_local_from_iso(value: str | None, tz_name: str = "Asia/Bangkok") -> str | None:
    if not value:
        return None
    dt = parse_iso_dt(value)
    if not dt:
        return None
    try:
        tz = timezone(tz_name)
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt.isoformat()


def _compute_s4_gates_summary(now: datetime, runtime: dict) -> tuple[str | None, str | None]:
    cooldown_text = None
    confirm_text = None

    try:
        last_flip_dt = parse_iso_dt(runtime.get('last_flip_at')) if isinstance(runtime.get('last_flip_at'), str) else None
        cooldown_days = max(int(S4_COOLDOWN_DAYS or 0), 0)
        if last_flip_dt and cooldown_days > 0:
            cooldown_until = last_flip_dt + timedelta(days=cooldown_days)
            if now.astimezone(utc) < cooldown_until:
                remaining = cooldown_until - now.astimezone(utc)
                hours_left = max(int(remaining.total_seconds() // 3600), 0)
                cooldown_text = f"ON ({hours_left}h left)"
            else:
                cooldown_text = "OFF"
        else:
            cooldown_text = "OFF"
    except Exception:
        cooldown_text = None

    try:
        confirm_days = max(int(S4_CONFIRM_DAYS or 0), 1)
        history = runtime.get('signal_history')
        if confirm_days > 1 and isinstance(history, list):
            confirm_text = "OFF" if _s4_confirmed(history, days=confirm_days) else f"ON (need {confirm_days}D)"
        else:
            confirm_text = "OFF"
    except Exception:
        confirm_text = None

    return cooldown_text, confirm_text


_LAST_HEARTBEAT_DAY_SENT: str | None = None


def maybe_send_daily_heartbeat(now: datetime) -> None:
    """Send a daily heartbeat LINE message once per day (08:00–08:15 Asia/Bangkok)."""
    if now.tzinfo is None:
        now = timezone('Asia/Bangkok').localize(now)
    if now.hour != 8 or now.minute > 15:
        return

    day_key = now.strftime("%Y-%m-%d")
    dedupe_key = f"heartbeat:{day_key}"
    request_id = f"heartbeat-{day_key.replace('-', '')}-{os.getpid()}"
    global _LAST_HEARTBEAT_DAY_SENT
    if not DB_DEDUPE_ENABLED:
        if _LAST_HEARTBEAT_DAY_SENT == day_key:
            return
        _LAST_HEARTBEAT_DAY_SENT = day_key
    else:
        if not claim_dedupe_key(dedupe_key, request_id):
            return

    cdc_status = None
    try:
        cdc_status = load_strategy_state().get('last_cdc_status')
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
        record, _, _, runtime = get_s4_state()
        if record and isinstance(runtime, dict):
            s4_cdc = str(runtime.get('last_cdc_status') or '').lower() or None
            s4_signal_source = runtime.get('signal_source')
            s4_asset = _s4_runtime_holding_asset(runtime)
            if not s4_asset:
                s4_asset = 'BTC' if (s4_cdc or 'up') == 'up' else 'GOLD'
            cooldown_text, confirm_text = _compute_s4_gates_summary(now, runtime)
            last_flip_text = _format_dt_local_from_iso(runtime.get('last_flip_at'))
            exposure = runtime.get('exposure') if isinstance(runtime, dict) else None
            if isinstance(exposure, dict):
                total_usd = exposure.get('total_usd')
                if isinstance(total_usd, (int, float)) and total_usd > 0:
                    portfolio_text = f"{total_usd:,.2f} USDT"
    except Exception as exc:
        logging.debug("Heartbeat S4 state read failed: %s", exc)

    effective_cdc = (s4_cdc or cdc_status or 'unknown')
    signal_source = s4_signal_source or ('binance_cdc' if cdc_status else None)
    asset_text = s4_asset or 'unknown'

    lines = [
        "Daily Heartbeat",
        "Status: RUNNING",
        f"Time: {_format_dt_local(now)} (Asia/Bangkok) | PID: {os.getpid()}",
        f"S4: Asset={asset_text} | CDC={effective_cdc}",
    ]
    gates_bits = []
    if cooldown_text:
        gates_bits.append(f"cooldown={cooldown_text}")
    if confirm_text:
        gates_bits.append(f"confirm_pending={confirm_text}")
    if gates_bits:
        lines.append("Gates: " + " | ".join(gates_bits))
    if last_flip_text:
        lines.append(f"Last Flip: {last_flip_text} (Asia/Bangkok)")
    if portfolio_text:
        lines.append(f"Portfolio: {portfolio_text}")

    payload = {
        "status": "RUNNING",
        "time": _format_dt_local(now) + " (Asia/Bangkok)",
        "pid": os.getpid(),
        "asset": asset_text,
        "cdc": effective_cdc,
        "signal_source": signal_source,
        "gates": " | ".join(gates_bits) if gates_bits else "",
        "last_flip": last_flip_text or "",
        "portfolio": portfolio_text or "",
    }
    try:
        notify_daily_heartbeat(payload)
        logging.info("Daily heartbeat sent dedupe_key=%s", dedupe_key)
    except Exception as exc:
        logging.warning("Daily heartbeat notify failed: %s", exc)


def acquire_scheduler_lock() -> object | None:
    """Acquire a DB-level lock to ensure single scheduler instance."""
    if not SCHEDULER_DB_LOCK_ENABLED:
        return None
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT GET_LOCK(%s, %s)", (SCHEDULER_DB_LOCK_NAME, SCHEDULER_DB_LOCK_TIMEOUT))
        row = cursor.fetchone()
        got = bool(row and row[0] == 1)
        if not got:
            logging.error("Failed to acquire scheduler lock '%s'. Another instance may be running.", SCHEDULER_DB_LOCK_NAME)
            cursor.close()
            conn.close()
            return None
        logging.info("Acquired scheduler lock '%s'.", SCHEDULER_DB_LOCK_NAME)
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


def release_scheduler_lock(conn: object | None) -> None:
    if not conn or not SCHEDULER_DB_LOCK_ENABLED:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT RELEASE_LOCK(%s)", (SCHEDULER_DB_LOCK_NAME,))
        conn.commit()
        cursor.close()
        logging.info("Released scheduler lock '%s'.", SCHEDULER_DB_LOCK_NAME)
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def assess_liquidity(adapter, exchange: str, *, context: dict | None = None) -> tuple[bool, dict]:
    """Check top-of-book spread vs threshold."""
    try:
        tob = adapter.get_top_of_book()
        bid = float(tob.get('bid') or 0.0)
        ask = float(tob.get('ask') or 0.0)
        if bid <= 0 or ask <= 0:
            return False, {'reason': 'invalid_top_of_book'}
        mid = (bid + ask) / 2
        spread_pct = ((ask - bid) / mid) * 100 if mid > 0 else 999.0
        metrics = {
            'spread_pct': spread_pct,
            'threshold_pct': LIQUIDITY_MAX_SPREAD_PCT,
            'bid': bid,
            'ask': ask,
        }
        if spread_pct > LIQUIDITY_MAX_SPREAD_PCT:
            metrics['reason'] = 'spread_high'
            return False, metrics
        return True, metrics
    except NotImplementedError:
        return True, {'reason': 'not_supported'}
    except Exception as exc:
        return False, {'reason': 'liquidity_error', 'error': str(exc)}

def _depth_band_limits(price: float) -> tuple[float, float]:
    band = DEPTH_GUARD_BAND_PCT / 100.0
    lower = price * (1.0 - band)
    upper = price * (1.0 + band)
    return lower, upper

def evaluate_depth_guard(adapter, exchange: str, price: float) -> tuple[bool, dict]:
    if not ENABLE_DEPTH_GUARD or price <= 0:
        return True, {}
    try:
        snapshot = adapter.get_depth_snapshot(limit=DEPTH_GUARD_DEPTH_LEVEL)
    except NotImplementedError:
        return True, {'reason': 'depth_not_supported'}
    except Exception as exc:
        return False, {'reason': 'depth_error', 'error': str(exc)}
    bids = snapshot.get('bids') or []
    asks = snapshot.get('asks') or []
    lower, upper = _depth_band_limits(price)
    bid_notional = sum(p * q for p, q in bids if p >= lower)
    ask_notional = sum(p * q for p, q in asks if p <= upper)
    min_notional = min(bid_notional, ask_notional)
    metrics = {
        'bid_notional': bid_notional,
        'ask_notional': ask_notional,
        'threshold': DEPTH_GUARD_MIN_NOTIONAL_USDT,
        'band_pct': DEPTH_GUARD_BAND_PCT,
        'dry_run': is_dry_run(),
    }
    if min_notional < DEPTH_GUARD_MIN_NOTIONAL_USDT:
        metrics['reason'] = 'depth_insufficient'
        metrics['min_notional'] = min_notional
        return False, metrics
    return True, metrics

def evaluate_twap_guard(adapter, exchange: str, price: float) -> tuple[bool, dict]:
    if not ENABLE_TWAP_GUARD or price <= 0 or TWAP_GUARD_WINDOW_MINUTES <= 0:
        return True, {}
    try:
        candles = adapter.get_recent_candles(interval="1m", limit=TWAP_GUARD_WINDOW_MINUTES)
    except NotImplementedError:
        return True, {'reason': 'twap_not_supported'}
    except Exception as exc:
        return False, {'reason': 'twap_error', 'error': str(exc)}
    closes = [float(c.get('close') or 0.0) for c in candles if c.get('close')]
    if not closes:
        return True, {'reason': 'twap_no_data'}
    twap = sum(closes) / len(closes)
    if twap <= 0:
        return True, {'reason': 'twap_invalid'}
    deviation_pct = abs(price - twap) / twap * 100.0
    metrics = {
        'twap': twap,
        'window_minutes': len(closes),
        'deviation_pct': deviation_pct,
        'threshold_pct': TWAP_GUARD_MAX_DEVIATION_PCT,
        'dry_run': is_dry_run(),
    }
    if deviation_pct > TWAP_GUARD_MAX_DEVIATION_PCT:
        metrics['reason'] = 'twap_deviation'
        return False, metrics
    return True, metrics

def evaluate_notional_cap(exchange: str, notional: float, state: dict | None = None) -> tuple[bool, dict]:
    st = state or {}
    cap = 0.0
    ex = exchange.lower()
    if ex == 'okx':
        cap_val = st.get('okx_max_usdt')
        if cap_val is None:
            env_val = os.getenv('OKX_MAX_USDT')
            try:
                cap = float(env_val) if env_val not in (None, '') else 0.0
            except (TypeError, ValueError):
                cap = 0.0
        else:
            try:
                cap = float(cap_val)
            except (TypeError, ValueError):
                cap = 0.0
    elif ex == 'binance':
        cap_val = st.get('binance_max_usdt')
        if cap_val is None:
            env_val = os.getenv('BINANCE_MAX_USDT')
            try:
                cap = float(env_val) if env_val not in (None, '') else 0.0
            except (TypeError, ValueError):
                cap = 0.0
        else:
            try:
                cap = float(cap_val)
            except (TypeError, ValueError):
                cap = 0.0
    if is_dry_run():
        return True, {'reason': 'dry_run', 'cap': cap, 'attempt': notional}
    if cap and cap > 0 and notional > cap:
        return False, {'reason': 'notional_cap', 'cap': cap, 'attempt': notional}
    return True, {'cap': cap, 'attempt': notional}

async def handle_half_sell_action(now: datetime, action: StrategyAction, *, state: dict | None = None) -> ActionResult:
    """Execute a HALF_SELL strategy action and return result metadata."""
    exchange = str(action.payload.get('exchange') or '').lower()
    pct = int(action.payload.get('percent') or 0)
    ctx_state = state or load_strategy_state()
    if not claim_dedupe_key(action.dedupe_key, action.request_id):
        return ActionResult(
            request_id=action.request_id,
            dedupe_key=action.dedupe_key,
            status=ActionStatus.SKIPPED,
            detail="duplicate_action_db",
        )
    meta = {
        'request_id': action.request_id,
        'dedupe_key': action.dedupe_key,
        'cdc_status': action.metadata.get('cdc_status') if action.metadata else None,
        'timestamp': now,
    }
    result = await asyncio.to_thread(_execute_half_sell_for_exchange, now, exchange, pct, ctx_state, meta)
    status = ActionStatus.SUCCESS if result.get('executed') else ActionStatus.FAILED
    return ActionResult(
        request_id=action.request_id,
        dedupe_key=action.dedupe_key,
        status=status,
        data={'exchange': exchange, 'payload': result, 'request_id': action.request_id, 'dedupe_key': action.dedupe_key},
    )


async def handle_reserve_buy_action(now: datetime, action: StrategyAction) -> ActionResult:
    """Execute a RESERVE_BUY action (global or per exchange)."""
    mode = str(action.payload.get('mode') or 'global').lower()
    exchange = str(action.payload.get('exchange') or '').lower()
    if not claim_dedupe_key(action.dedupe_key, action.request_id):
        return ActionResult(
            request_id=action.request_id,
            dedupe_key=action.dedupe_key,
            status=ActionStatus.SKIPPED,
            detail="duplicate_action_db",
        )
    context = {
        'request_id': action.request_id,
        'dedupe_key': action.dedupe_key,
        'cdc_status': action.metadata.get('cdc_status') if action.metadata else None,
        'timestamp': now,
    }
    if mode == 'exchange' and exchange:
        result = await asyncio.to_thread(execute_reserve_buy_exchange, now, exchange, context)
    else:
        result = await asyncio.to_thread(execute_reserve_buy, now, context)
    status = ActionStatus.SUCCESS if (result.get('executed') or result.get('skipped')) else ActionStatus.FAILED
    return ActionResult(
        request_id=action.request_id,
        dedupe_key=action.dedupe_key,
        status=status,
        data={'mode': mode, 'exchange': exchange or None, 'payload': result, 'request_id': action.request_id, 'dedupe_key': action.dedupe_key},
    )

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('btc_purchase_log.log', maxBytes=5 * 1024 * 1024, backupCount=5),
        logging.StreamHandler()
    ]
)

# Health check server with port conflict handling
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Scheduler is running")
    
    def log_message(self, format, *args):
        # Suppress HTTP server logs
        return

def is_port_in_use(port):
    """Check if a port is already in use"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0

def find_available_port(start_port, max_attempts=10):
    """Find an available port starting from start_port"""
    for i in range(max_attempts):
        port = start_port + i
        if not is_port_in_use(port):
            return port
    return None

def start_health_check():
    """Start a simple HTTP server for health check with port conflict handling."""
    base_port = int(os.getenv('HEALTH_CHECK_PORT', 8001))
    
    # Check if base port is in use
    if is_port_in_use(base_port):
        logging.warning(f"Port {base_port} is already in use, finding alternative...")
        available_port = find_available_port(base_port + 1)
        
        if available_port:
            port = available_port
            logging.info(f"Using alternative port {port} for health check")
            # Update environment variable for other processes
            os.environ['HEALTH_CHECK_PORT'] = str(port)
        else:
            logging.error("No available ports found for health check server")
            return None
    else:
        port = base_port
    
    try:
        server = HTTPServer(('localhost', port), HealthCheckHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logging.info(f"Health check server started on port {port}")
        return server
    except Exception as e:
        logging.error(f"Failed to start health check server on port {port}: {e}")
        return None

def get_db_connection():
    return _get_db_connection()

def db_transaction():
    return _db_transaction()


def fetch_schedule_context(schedule_id: int) -> dict:
    """Load schedule metadata (time/label) for notifications."""
    if not schedule_id:
        return {}

    conn = None
    cursor = None
    context: dict[str, str] = {}

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM schedules WHERE id = %s LIMIT 1", (schedule_id,))
        row = cursor.fetchone()
        if not row:
            return {}

        columns = [desc[0] for desc in cursor.description]
        row_dict = dict(zip(columns, row))

        time_value = row_dict.get('schedule_time')
        if hasattr(time_value, 'strftime'):
            context['time'] = time_value.strftime('%H:%M')
        elif isinstance(time_value, str):
            cleaned = time_value.strip()
            if len(cleaned) >= 5 and cleaned[2] == ':':
                context['time'] = cleaned[:5]
            else:
                context['time'] = cleaned or None
        elif time_value is not None:
            context['time'] = str(time_value)

        label = None
        for key in (
            'slot_label',
            'label',
            'name',
            'title',
            'line_channel',
            'line_label',
            'line_topic',
            'channel_label',
            'display_name',
        ):
            value = row_dict.get(key)
            if value:
                label = str(value)
                break

        if not label:
            meta_value = row_dict.get('metadata') or row_dict.get('meta') or row_dict.get('extra') or row_dict.get('config_json')
            if meta_value:
                try:
                    if isinstance(meta_value, (bytes, bytearray)):
                        meta_value = meta_value.decode('utf-8')
                    meta_obj = json.loads(meta_value) if isinstance(meta_value, str) else meta_value
                    if isinstance(meta_obj, dict):
                        for key in (
                            'slot_label',
                            'label',
                            'name',
                            'title',
                            'line_channel',
                            'line_label',
                            'line_topic',
                            'channel_label',
                            'display_name',
                        ):
                            if meta_obj.get(key):
                                label = str(meta_obj[key])
                                break
                except Exception:
                    pass

        if label:
            context['label'] = label

    except Exception as exc:
        logging.debug(f"Schedule context lookup failed for id={schedule_id}: {exc}")
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

    return context

def load_strategy_record(mode: str) -> dict | None:
    return _load_strategy_record(mode, get_db_connection)


def record_fee_totals(
    strategy: str,
    exchange: str,
    fee_type: str,
    fee_usd: float,
    fee_asset: str | None,
    fee_asset_amount: float,
) -> None:
    return _record_fee_totals(strategy, exchange, fee_type, fee_usd, fee_asset, fee_asset_amount, db_transaction)

def save_strategy_metadata(mode: str, metadata: dict, extra: dict | None = None) -> None:
    return _save_strategy_metadata(mode, metadata, extra, db_transaction)

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
    return _record_rotation_event(
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

def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = utc.localize(dt)
        else:
            dt = dt.astimezone(utc)
        return dt
    except Exception:
        return None


def _s4_should_alert(runtime: dict, key: str, now: datetime, *, min_interval_minutes: int = 360) -> bool:
    ts_key = f"last_alert_{key}"
    last_raw = runtime.get(ts_key)
    last_dt = parse_iso_dt(last_raw) if isinstance(last_raw, str) else None
    if not last_dt:
        runtime[ts_key] = now.astimezone(utc).isoformat()
        return True
    if (now.astimezone(utc) - last_dt).total_seconds() >= min_interval_minutes * 60:
        runtime[ts_key] = now.astimezone(utc).isoformat()
        return True
    return False


def _s4_hold(
    now: datetime,
    metadata: dict,
    runtime: dict,
    *,
    reason: str,
    detail: str | None = None,
    alert_key: str | None = None,
    alert_message: str | None = None,
    alert_interval_minutes: int = 360,
) -> None:
    runtime['last_action_result'] = [{'status': 'HOLD', 'reason': reason}]
    runtime.pop('last_error', None)
    runtime['last_hold_detail'] = {
        'at': now.isoformat(),
        'reason': reason,
        'detail': detail,
    }
    # Forensic-friendly logs with minimal spam: log immediately on reason change,
    # otherwise throttle at the same interval as alerts by default.
    last_reason = runtime.get('last_hold_reason')
    runtime['last_hold_reason'] = reason
    if last_reason != reason or _s4_should_alert(runtime, f"log_{reason}", now, min_interval_minutes=alert_interval_minutes):
        logging.info("S4 HOLD | reason=%s | detail=%s", reason, detail or "")
    if alert_key and alert_message and _s4_should_alert(runtime, alert_key, now, min_interval_minutes=alert_interval_minutes):
        try:
            send_line_message_with_retry(alert_message)
        except Exception:
            logging.debug("S4 hold alert failed", exc_info=True)
    save_strategy_metadata('s4_multi_leg', metadata, {'last_run_at': now})


def _s4_runtime_holding_asset(runtime: dict | None, default: str | None = None) -> str | None:
    if not isinstance(runtime, dict):
        return default
    asset = runtime.get('holding_asset') or runtime.get('active_asset') or default
    if asset is None:
        return None
    text = str(asset).upper()
    return text or default


def _s4_set_runtime_holding_asset(runtime: dict, asset: str | None) -> str | None:
    normalized = str(asset).upper() if asset else None
    runtime['holding_asset'] = normalized
    runtime['active_asset'] = normalized
    return normalized


def _s4_asof_date(value: str | None) -> str | None:
    dt = parse_iso_dt(value)
    if not dt:
        return None
    return dt.date().isoformat()


def _s4_update_signal_history(runtime: dict, entry: dict, *, keep: int = 14) -> list[dict]:
    history = runtime.get('signal_history')
    if not isinstance(history, list):
        history = []
    date_key = entry.get('date')
    if date_key:
        replaced = False
        for idx, item in enumerate(history):
            if isinstance(item, dict) and item.get('date') == date_key:
                history[idx] = entry
                replaced = True
                break
        if not replaced:
            history.append(entry)
    # Keep only the latest N entries sorted by date (string ISO date).
    history = [item for item in history if isinstance(item, dict) and item.get('date')]
    history.sort(key=lambda x: x.get('date') or '')
    if len(history) > keep:
        history = history[-keep:]
    runtime['signal_history'] = history
    return history


def _s4_log_neutral_state(
    now: datetime,
    runtime: dict,
    *,
    state: str,
    metrics: dict,
    ratio_close: float,
    ema12: float,
    ema26: float,
    asof_date: str | None,
    preset_name: str,
) -> None:
    gap = float(metrics.get('ema_gap_pct') or 0.0)
    slope = float(metrics.get('slope_pct') or 0.0)
    prev_state = runtime.get('neutral_state')

    if prev_state and prev_state != state:
        logging.info(
            "S4 NEUTRAL STATE_CHANGE | ts=%s | old=%s | new=%s | gap=%.4f%% | slope=%.4f%%",
            now.isoformat(),
            prev_state,
            state,
            gap,
            slope,
        )
        try:
            with db_transaction() as (cursor, _):
                cursor.execute(
                    """
                    INSERT INTO s4_neutral_zone_state_changes (
                        ts, old_state, new_state, ema_gap_pct, slope_pct
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (now, prev_state, state, gap, slope),
                )
        except Exception:
            logging.debug("S4 neutral state change DB log skipped", exc_info=True)

    runtime['neutral_state'] = state
    runtime['neutral_preset'] = preset_name
    runtime['neutral_ratio_close'] = ratio_close
    runtime['neutral_ema12'] = ema12
    runtime['neutral_ema26'] = ema26
    runtime['neutral_ema_gap_pct'] = gap
    runtime['neutral_slope_pct'] = slope

    if asof_date and runtime.get('neutral_eod_date') != asof_date:
        try:
            eod_date = datetime.fromisoformat(asof_date).date()
        except Exception:
            eod_date = None
        now_utc_date = now.astimezone(utc).date()
        lag_days = (now_utc_date - eod_date).days if eod_date else 0
        logging.info(
            "S4 NEUTRAL EOD_SUMMARY | date=%s | state=%s | ratio=%.6f | ema12=%.6f | ema26=%.6f | gap=%.4f%% | slope=%.4f%% | lag_days=%s",
            asof_date,
            state,
            ratio_close,
            ema12,
            ema26,
            gap,
            slope,
            lag_days,
        )
        runtime['neutral_eod_lag_days'] = lag_days
        try:
            with db_transaction() as (cursor, _):
                cursor.execute(
                    """
                    INSERT INTO s4_neutral_zone_eod (
                        date, ratio_close, ema12, ema26, ema_gap_pct, slope_pct,
                        state, cdc_status, active_asset, eod_lag_days
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        ratio_close=VALUES(ratio_close),
                        ema12=VALUES(ema12),
                        ema26=VALUES(ema26),
                        ema_gap_pct=VALUES(ema_gap_pct),
                        slope_pct=VALUES(slope_pct),
                        state=VALUES(state),
                        cdc_status=VALUES(cdc_status),
                        active_asset=VALUES(active_asset),
                        eod_lag_days=VALUES(eod_lag_days)
                    """,
                    (
                        asof_date,
                        ratio_close,
                        ema12,
                        ema26,
                        gap,
                        slope,
                        state,
                        runtime.get('last_cdc_status'),
                        _s4_runtime_holding_asset(runtime),
                        lag_days,
                    ),
                )
        except Exception:
            logging.debug("S4 neutral EOD DB log skipped", exc_info=True)
        runtime['neutral_eod_date'] = asof_date


def _s4_confirmed(history: list[dict], *, days: int) -> bool:
    if days <= 1:
        return True
    if not history or len(history) < days:
        return False
    tail = history[-days:]
    statuses = [str(item.get('status') or '').lower() for item in tail]
    dates = [item.get('date') for item in tail]
    if any(not s for s in statuses) or any(not d for d in dates):
        return False
    if len(set(statuses)) != 1:
        return False
    try:
        parsed_dates = [datetime.fromisoformat(d).date() for d in dates]  # type: ignore[arg-type]
    except Exception:
        return False
    # Require consecutive daily closes (date increments by 1 day).
    for prev, curr in zip(parsed_dates, parsed_dates[1:]):
        if (curr - prev).days != 1:
            return False
    return True


def _s4_shadow_swap_gate_decision(
    *,
    runtime: dict,
    cdc_status: str,
    now: datetime,
) -> dict:
    """Evaluate shadow swap gate status (log-only, no execution)."""
    holding = str(_s4_runtime_holding_asset(runtime, _s4_dca_target_asset(cdc_status)) or _s4_dca_target_asset(cdc_status)).upper()
    if holding not in ('BTC', 'GOLD'):
        holding = _s4_dca_target_asset(cdc_status)

    history = runtime.get('signal_history') if isinstance(runtime.get('signal_history'), list) else []
    neutral_state = str(runtime.get('neutral_state') or '')
    slope_pct = _safe_float(runtime.get('neutral_slope_pct'), 0.0)
    gap_pct = _safe_float(runtime.get('neutral_ema_gap_pct'), 0.0)
    last_flip_dt = parse_iso_dt(runtime.get('last_flip_at')) if isinstance(runtime.get('last_flip_at'), str) else None
    days_since_last_swap = 9999
    if last_flip_dt:
        days_since_last_swap = int((now.astimezone(utc) - last_flip_dt).total_seconds() // 86400)

    decision = 'HOLD'
    reason = 'no_gate'
    target_asset = holding

    if holding == 'GOLD':
        target_asset = 'BTC'
        if cdc_status != 'up':
            reason = 'gate_cdc_up_required'
        elif history and not _s4_confirmed(history, days=max(S4_SHADOW_BTC_CONFIRM_DAYS, 1)):
            reason = 'gate_cdc_confirm'
        elif S4_SHADOW_REQUIRE_NEUTRAL and neutral_state != 'btc_signal':
            reason = 'gate_neutral'
        elif slope_pct < S4_SHADOW_BTC_SLOPE_MIN:
            reason = 'gate_slope'
        elif gap_pct > S4_SHADOW_BTC_GAP_MAX:
            reason = 'gate_gap'
        elif days_since_last_swap < max(S4_SHADOW_COOLDOWN_DAYS, 0):
            reason = 'gate_cooldown'
        else:
            decision = 'SWAP_TO_BTC'
            reason = 'all_gates_passed'
    else:
        target_asset = 'GOLD'
        if cdc_status != 'down':
            reason = 'gate_cdc_down_required'
        elif history and not _s4_confirmed(history, days=max(S4_SHADOW_XAU_CONFIRM_DAYS, 1)):
            reason = 'gate_cdc_confirm'
        elif slope_pct > S4_SHADOW_XAU_SLOPE_MAX:
            reason = 'gate_slope'
        elif days_since_last_swap < max(S4_SHADOW_COOLDOWN_DAYS, 0):
            reason = 'gate_cooldown'
        else:
            decision = 'SWAP_TO_XAU'
            reason = 'all_gates_passed'

    next_unlock_condition, next_unlock_min_days = _s4_next_unlock_from_gate_reason(
        reason,
        btc_confirm_days=max(S4_SHADOW_BTC_CONFIRM_DAYS, 0),
        xau_confirm_days=max(S4_SHADOW_XAU_CONFIRM_DAYS, 0),
    )

    return {
        'holding': holding,
        'target_asset': target_asset,
        'decision': decision,
        'reason': reason,
        'cdc_status': cdc_status,
        'neutral_state': neutral_state,
        'slope_pct': slope_pct,
        'gap_pct': gap_pct,
        'days_since_last_swap': days_since_last_swap,
        'next_unlock_condition': next_unlock_condition,
        'next_unlock_min_days': next_unlock_min_days,
        'config': {
            'btc_confirm_days': S4_SHADOW_BTC_CONFIRM_DAYS,
            'xau_confirm_days': S4_SHADOW_XAU_CONFIRM_DAYS,
            'btc_slope_min': S4_SHADOW_BTC_SLOPE_MIN,
            'xau_slope_max': S4_SHADOW_XAU_SLOPE_MAX,
            'btc_gap_max': S4_SHADOW_BTC_GAP_MAX,
            'cooldown_days': S4_SHADOW_COOLDOWN_DAYS,
            'require_neutral': S4_SHADOW_REQUIRE_NEUTRAL,
        },
    }


def _s4_latest_eod_snapshot() -> dict | None:
    """Fetch latest EOD analytics snapshot for observability alignment."""
    try:
        with db_transaction() as (cursor, _):
            cursor.execute(
                """
                SELECT date, cdc_status, state, slope_pct, ema_gap_pct, eod_lag_days
                FROM s4_neutral_zone_eod
                ORDER BY date DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    except Exception:
        logging.debug("S4 latest EOD snapshot unavailable", exc_info=True)
        return None


def _s4_count_successful_flips_30d() -> int:
    """Count executed S4 flips in the last 30 days (both directions)."""
    days = 30
    try:
        with db_transaction() as (cursor, _):
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM strategy_rotation_log
                WHERE strategy_mode=%s
                  AND reason=%s
                  AND executed_at >= (NOW() - INTERVAL %s DAY)
                  AND (
                        metadata_json LIKE %s
                     OR metadata_json LIKE %s
                     OR (
                            (metadata_json LIKE %s OR metadata_json LIKE %s)
                        AND metadata_json LIKE %s
                        )
                  )
                """,
                (
                    's4_multi_leg',
                    'cdc_flip',
                    days,
                    '%"executed_ok": true%',
                    '%"executed_ok":true%',
                    '%"dry_run": false%',
                    '%"dry_run":false%',
                    '%"executed": {%',
                ),
            )
            row = cursor.fetchone()
            return int(row[0] or 0) if row else 0
    except Exception as exc:
        logging.warning("S4 flip count query failed (allowing flips): %s", exc)
        return 0


def _s4_spread_threshold_pct(symbol: str) -> float:
    sym = str(symbol or "").upper()
    if "XAUT" in sym:
        return float(S4_MAX_SPREAD_PCT_XAUT)
    return float(S4_MAX_SPREAD_PCT_BTC)


def _s4_check_spread_okx(adapter, symbol: str) -> tuple[bool, dict]:
    """Return (ok, metrics) for symbol spread check using OKX top-of-book."""
    try:
        tob = adapter.get_top_of_book(symbol)  # OKX adapter supports symbol
        bid = float(tob.get("bid") or 0.0)
        ask = float(tob.get("ask") or 0.0)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
        threshold = _s4_spread_threshold_pct(symbol)
        metrics = {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "threshold_pct": threshold,
            "ts": tob.get("ts"),
        }
        if bid <= 0 or ask <= 0:
            metrics["reason"] = "invalid_top_of_book"
            return False, metrics
        if spread_pct > threshold:
            metrics["reason"] = "spread_high"
            return False, metrics
        return True, metrics
    except Exception as exc:
        return False, {"symbol": symbol, "reason": "top_of_book_error", "error": str(exc)}

def compute_s4_exposure_from_units(
    btc_units: float,
    gold_units: float,
    btc_price: float,
    gold_price: float,
    stamp: datetime,
) -> tuple[dict, dict[str, float]]:
    btc_value = max(btc_units, 0.0) * max(btc_price, 0.0)
    gold_value = max(gold_units, 0.0) * max(gold_price, 0.0)
    total = btc_value + gold_value

    def _weight(value: float) -> float:
        if total <= 0:
            return 0.0
        try:
            return round(value / total, 6)
        except ZeroDivisionError:
            return 0.0

    exposure = {
        'btc': {
            'notional_usd': round(btc_value, 2),
            'weight': _weight(btc_value),
        },
        'gold': {
            'notional_usd': round(gold_value, 2),
            'weight': _weight(gold_value),
        },
        'total_usd': round(total, 2),
        'valuation_at': stamp.astimezone(timezone('UTC')).isoformat()
    }
    usd_map = {
        'BTC': exposure['btc']['notional_usd'],
        'GOLD': exposure['gold']['notional_usd'],
    }
    return exposure, usd_map


def _s4_exchange_artifacts(exchange_code: str) -> tuple[str, str, str, str, str]:
    code = (exchange_code or 'okx').strip().lower()
    if code == 'okx':
        return 'okx', 'OKX', 'BTC-USDT', 'XAUT-USDT', 'XAUT'
    return 'binance', 'BINANCE', 'BTCUSDT', 'PAXGUSDT', 'PAXG'


def get_s4_state():
    record = load_strategy_record('s4_multi_leg')
    if not record:
        return None, None, None, None
    metadata_raw = record.get('metadata_json')
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except json.JSONDecodeError:
        metadata = {}
    config = metadata.get('config') or {}
    runtime = metadata.setdefault('runtime', {})
    return record, metadata, config, runtime


def execute_s4_dca(now: datetime, amount: float, schedule_id: int) -> dict | None:
    try:
        amount = float(amount or 0.0)
    except Exception:
        amount = 0.0
    if amount <= 0:
        return None

    record, metadata, config, runtime = get_s4_state()
    if not record:
        return None

    exchange_code = str(config.get('exchange') or 'okx').lower()
    adapter_name, exchange_label, btc_symbol, gold_symbol, gold_asset = _s4_exchange_artifacts(exchange_code)

    last_status = str(runtime.get('last_cdc_status') or 'up').lower()
    dca_target_asset = _s4_dca_target_asset(last_status)
    active_asset = _s4_runtime_holding_asset(runtime)
    if S4_DCA_FOLLOW_CDC_ONLY:
        active_asset = dca_target_asset
    elif not active_asset:
        active_asset = dca_target_asset

    symbol = btc_symbol if active_asset == 'BTC' else gold_symbol
    asset_label = 'BTC' if active_asset == 'BTC' else gold_asset

    dry_run = is_dry_run()
    adapter = None
    try:
        adapter = get_adapter(adapter_name, testnet=USE_TESTNET, dry_run=dry_run)
    except Exception as exc:
        logging.debug(f"S4 DCA adapter load failed ({adapter_name}): {exc}")
        adapter = None

    executed_qty = 0.0
    avg_price = 0.0
    filled_usd = amount
    order_id = -1
    fee_buy_usdt = 0.0
    fee_buy_asset = None
    fee_buy_asset_amount = 0.0

    price_hint = fetch_symbol_price_fallback(symbol, exchange_code)

    if adapter is not None and not dry_run:
        try:
            # Basic pre-check: ensure balance is sufficient before claiming dedupe.
            balance = adapter.get_balance('USDT') or {}
            available_usdt = _safe_float(balance.get('free'), 0.0)
            if available_usdt < amount:
                logging.warning(
                    "S4 DCA skipped: insufficient USDT (need=%.2f, available=%.2f)",
                    amount,
                    available_usdt,
                )
                return None
            day_key = now.astimezone(timezone('Asia/Bangkok')).date().isoformat()
            dedupe_key = f"s4_dca:{day_key}:{schedule_id or 0}"
            request_id = f"{dedupe_key}:{int(now.timestamp())}:{os.getpid()}"
            if not claim_dedupe_key(dedupe_key, request_id):
                logging.warning("S4 DCA dedupe hit: skip buy (dedupe_key=%s schedule_id=%s)", dedupe_key, schedule_id)
                if _s4_should_alert(runtime, f"s4_dca_dedupe_{schedule_id}", now, min_interval_minutes=360):
                    try:
                        send_line_message_with_retry(
                            "⚠️ S4 DCA skipped (dedupe)\n"
                            f"Schedule: #{schedule_id}\n"
                            f"Date: {day_key}\n"
                            f"Key: {dedupe_key}"
                        )
                    except Exception:
                        logging.debug("S4 DCA dedupe alert failed", exc_info=True)
                return None
            order = adapter.place_market_buy_quote_symbol(symbol, amount)
            executed_qty = float(order.executed_qty or 0.0)
            avg_price = float(order.avg_price or 0.0)
            filled_usd = float(order.cummulative_quote_qty or amount)
            order_id = order.order_id
            fee_buy_usdt = float(getattr(order, 'fee_usd', 0.0) or 0.0)
            fee_buy_asset = getattr(order, 'fee_asset', None)
            fee_buy_asset_amount = float(getattr(order, 'fee_asset_amount', 0.0) or 0.0)
        except Exception as exc:
            logging.error(f"S4 DCA execution error: {exc}")
            return None
    else:
        price = price_hint
        if price <= 0:
            logging.warning("S4 DCA price unavailable; skipping dry-run buy")
            executed_qty = 0.0
            avg_price = 0.0
        else:
            day_key = now.astimezone(timezone('Asia/Bangkok')).date().isoformat()
            dedupe_key = f"s4_dca:{day_key}:{schedule_id or 0}"
            request_id = f"{dedupe_key}:{int(now.timestamp())}:{os.getpid()}"
            if not claim_dedupe_key(dedupe_key, request_id):
                logging.warning("S4 DCA dedupe hit: skip buy (dedupe_key=%s schedule_id=%s)", dedupe_key, schedule_id)
                if _s4_should_alert(runtime, f"s4_dca_dedupe_{schedule_id}", now, min_interval_minutes=360):
                    try:
                        send_line_message_with_retry(
                            "⚠️ S4 DCA skipped (dedupe)\n"
                            f"Schedule: #{schedule_id}\n"
                            f"Date: {day_key}\n"
                            f"Key: {dedupe_key}"
                        )
                    except Exception:
                        logging.debug("S4 DCA dedupe alert failed", exc_info=True)
                return None
            executed_qty = amount / price
            avg_price = price
        fee_buy_usdt = 0.0
        fee_buy_asset = None
        fee_buy_asset_amount = 0.0

    record_fee_totals('s4_dca', adapter_name, 'buy', fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)

    runtime['last_dca'] = {
        'at': now.isoformat(),
        'asset': asset_label,
        'amount_usd': round(filled_usd, 2),
        'qty': executed_qty,
        'exchange': exchange_label,
        'dry_run': dry_run or adapter is None,
    }

    holdings_payload = None
    holdings_meta = None
    try:
        refreshed_holdings = fetch_balances(
            [adapter_name],
            ['USDT', 'BTC', gold_asset],
            force_refresh=True,
        )
        runtime['holdings'] = refreshed_holdings
        holdings_payload = refreshed_holdings.get(adapter_name)
        holdings_meta = refreshed_holdings.get('_meta')
    except Exception as exc:
        logging.debug(f"S4 DCA holdings refresh failed ({adapter_name}): {exc}")

    if adapter is not None and not dry_run:
        try:
            with db_transaction() as (cursor, _):
                cursor.execute(
                    """
                    INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        now,
                        filled_usd,
                        executed_qty,
                        avg_price,
                        order_id,
                        schedule_id,
                        exchange_label.lower(),
                        fee_buy_usdt if fee_buy_usdt is not None else None,
                        fee_buy_asset,
                        fee_buy_asset_amount if fee_buy_asset_amount is not None else None,
                    ),
                )
        except Exception as exc:
            logging.error(f"S4 DCA logging error: {exc}")

    schedule_context = {}
    try:
        schedule_context = fetch_schedule_context(schedule_id)
    except Exception as exc:
        logging.debug(f"S4 DCA schedule context unavailable ({schedule_id}): {exc}")

    def _order_id_payload(value):
        """Return an order identifier suitable for notifications/logs."""
        if value in (None, '', 0):
            return None
        if isinstance(value, (int, float)):
            return value if value > 0 else None
        text = str(value).strip()
        return text or None

    order_id_payload = _order_id_payload(order_id)

    signal_source = runtime.get('signal_source')
    try:
        notify_s4_dca_buy({
            'asset': asset_label,
            'qty': executed_qty,
            'price': avg_price,
            'usdt': filled_usd,
            'exchange': exchange_label,
            'schedule_id': schedule_id,
            'schedule_time': schedule_context.get('time') if schedule_context else None,
            'schedule_label': schedule_context.get('label') if schedule_context else None,
            'dry_run': dry_run or adapter is None,
            'order_id': order_id_payload,
            'fee_usdt': fee_buy_usdt,
            'fee_asset': fee_buy_asset,
            'fee_asset_amount': fee_buy_asset_amount,
            'cdc_status': runtime.get('last_cdc_status') or last_status,
            'signal_source': signal_source,
            'holdings': holdings_payload,
            'holdings_meta': holdings_meta,
        })
    except Exception as exc:
        logging.error("S4 DCA notification failed; falling back to text message: %s", exc, exc_info=True)
        fallback_lines = [
            "S4 DCA Buy (fallback)",
            f"Asset: {asset_label} | Exchange: {exchange_label}",
            f"Amount: {filled_usd:,.2f} USDT",
        ]
        if executed_qty and avg_price:
            fallback_lines.append(f"Qty: {executed_qty:.6f} {asset_label} @ {avg_price:,.2f}")
        elif executed_qty:
            fallback_lines.append(f"Qty: {executed_qty:.6f} {asset_label}")
        elif avg_price:
            fallback_lines.append(f"Avg: {avg_price:,.2f}")

        status_bits: list[str] = []
        if schedule_id:
            status_bits.append(f"Schedule: #{schedule_id}")
        elif schedule_context.get('label'):
            status_bits.append(f"Schedule: {schedule_context.get('label')}")
        cdc_state = runtime.get('last_cdc_status') or last_status
        if cdc_state:
            source_label = signal_source or 'binance_cdc'
            status_bits.append(f"CDC Signal: {str(cdc_state).upper()} ({source_label})")
        if dry_run or adapter is None:
            status_bits.append("Mode: DRY RUN")
        else:
            status_bits.append("Mode: LIVE")
        if order_id_payload:
            status_bits.append(f"Order: {order_id_payload}")
        if status_bits:
            fallback_lines.append(" | ".join(status_bits))

        fee_bits: list[str] = []
        if fee_buy_usdt:
            fee_bits.append(f"{fee_buy_usdt:,.6f} USDT")
        if fee_buy_asset and fee_buy_asset_amount:
            fee_bits.append(f"{fee_buy_asset_amount:,.6f} {str(fee_buy_asset).upper()}")
        if fee_bits:
            fallback_lines.append("Fee: " + " + ".join(fee_bits))

        try:
            send_line_message_with_retry("\n".join(fallback_lines))
        except Exception:
            logging.error("S4 DCA fallback notification failed", exc_info=True)

    save_strategy_metadata('s4_multi_leg', metadata, {'last_run_at': now})
    return {
        'asset': asset_label,
        'exchange': exchange_label,
        'dry_run': dry_run or adapter is None,
        'amount_usd': filled_usd,
        'qty': executed_qty,
    }

def ensure_s4_exposure(metadata: dict, cdc_status: str, now: datetime) -> tuple[dict, bool]:
    """Guarantee runtime.exposure structure exists and return it."""
    runtime = metadata.setdefault("runtime", {})
    exposure = runtime.get("exposure") or {}
    btc_info = exposure.get("btc") or {}
    gold_info = exposure.get("gold") or {}
    btc_usd = _safe_float(btc_info.get("notional_usd"), 0.0)
    gold_usd = _safe_float(gold_info.get("notional_usd"), 0.0)
    total = btc_usd + gold_usd

    config = metadata.get("config") or {}
    changed = False

    if total <= 0:
        capital = _safe_float(config.get("capital_usdt"), 10000.0)
        if capital <= 0:
            capital = 10000.0
        target_up = _safe_float(config.get("target_btc_pct_up"), 0.65)
        target_down = _safe_float(config.get("target_btc_pct_down"), 0.35)
        target_pct = target_up if str(cdc_status).lower() == "up" else target_down
        btc_usd = round(capital * target_pct, 2)
        gold_usd = max(capital - btc_usd, 0.0)
        total = btc_usd + gold_usd
        changed = True

    if total <= 0:
        total = 0.0

    def _weights(notional: float, denom: float) -> float:
        if denom <= 0:
            return 0.0
        return max(min(notional / denom, 1.0), 0.0)

    new_exposure = {
        "btc": {"notional_usd": round(btc_usd, 2), "weight": round(_weights(btc_usd, total), 6)},
        "gold": {"notional_usd": round(gold_usd, 2), "weight": round(_weights(gold_usd, total), 6)},
        "total_usd": round(total, 2),
        "valuation_at": now.astimezone(timezone('UTC')).isoformat()
    }

    if new_exposure != exposure:
        runtime["exposure"] = new_exposure
        changed = True
    else:
        runtime["exposure"] = exposure
    return runtime["exposure"], changed

def fetch_btc_price_fallback(adapter_exchange: str = "binance") -> float:
    """Fetch BTC price using utils or exchange adapter fallback."""
    price = None
    if callable(get_btc_price):
        try:
            price = float(get_btc_price())
        except Exception:
            price = None
    if price and price > 0:
        return price
    try:
        adapter = get_adapter(adapter_exchange, testnet=USE_TESTNET, dry_run=True)
        return float(adapter.get_price())
    except Exception as exc:
        logging.warning(f"fetch_btc_price_fallback error: {exc}")
    return 0.0

def fetch_gold_price_fallback() -> float:
    """Fetch GOLD (PAXG) price using utils override or Binance client."""
    price = None
    if callable(get_gold_price):
        try:
            price = float(get_gold_price())
        except Exception:
            price = None
    if price and price > 0:
        return price
    try:
        ticker = client.get_symbol_ticker(symbol="PAXGUSDT")
        return float(ticker.get("price") or 0.0)
    except Exception as exc:
        logging.warning(f"fetch_gold_price_fallback error: {exc}")
    return 0.0

def fetch_symbol_price_fallback(symbol: str, exchange: str) -> float:
    """Fetch symbol price from exchange REST as fallback."""
    exchange = (exchange or 'binance').lower()
    try:
        if exchange == 'okx':
            resp = requests.get(
                "https://www.okx.com/api/v5/market/ticker",
                params={"instId": symbol},
                timeout=(5, 5),
            )
            resp.raise_for_status()
            data = (resp.json().get("data") or [{}])[0]
            return float(data.get("last") or 0.0)
        else:
            resp = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": symbol.replace('-', '')},
                timeout=(5, 5),
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("price") or 0.0)
    except Exception as exc:
        logging.warning(f"fetch_symbol_price_fallback error ({exchange}, {symbol}): {exc}")
        return 0.0


def _attach_holdings_snapshot(
    target: dict,
    exchange: str,
    *,
    assets: Sequence[str] | None = None,
    force_refresh: bool = False,
) -> None:
    """Populate `target` with holdings data for the given exchange if available."""
    slug = str(exchange or '').strip().lower()
    if not slug:
        return
    asset_list = tuple(assets or ("BTC", "USDT"))
    try:
        snapshot = fetch_balances([slug], asset_list, force_refresh=force_refresh)
    except Exception as exc:
        logging.debug(f"Holdings fetch failed ({slug}): {exc}")
        return
    if not isinstance(snapshot, dict):
        return
    holdings = snapshot.get(slug)
    meta = snapshot.get('_meta')
    if holdings:
        target['holdings'] = holdings
    if meta:
        target['holdings_meta'] = meta

def validate_schedule(schedule_time_str: str, schedule_days: list) -> None:
    """Validate schedule time and days.

    Args:
        schedule_time_str (str): Time in HH:MM format.
        schedule_days (list): List of days.

    Raises:
        ValueError: If time or days are invalid.
    """
    try:
        datetime.strptime(schedule_time_str, "%H:%M")
    except ValueError:
        raise ValueError(f"Invalid schedule_time format: {schedule_time_str}")
    
    valid_days = {'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'}
    invalid_days = set(schedule_days) - valid_days
    if invalid_days:
        raise ValueError(f"Invalid schedule_day: {invalid_days}")

# Purchase BTC
async def purchase_btc(now: datetime, purchase_amount: float, schedule_id: int, context: dict | None = None) -> None:
    """Purchase BTC on Binance and save to database.

    Args:
        now (datetime): Current timestamp.
        purchase_amount (float): Amount of USDT to purchase.
        schedule_id (int): ID of the schedule for tracking.
    """
    db = None
    cursor = None
    try:
        db = get_db_connection()
        cursor = db.cursor()
        logging.info(f"Purchase amount for schedule {schedule_id}: {purchase_amount} USDT")
        st = load_strategy_state(); ex = st.get('exchange', 'binance')
        adapter = get_adapter(ex, testnet=USE_TESTNET, dry_run=is_dry_run())

        if not is_dry_run():
            bal = adapter.get_balance('USDT')
            available_usdt = float(bal.get('free') or 0)
            logging.info(f"[{ex}] Available USDT balance: {available_usdt}")
            if available_usdt < purchase_amount:
                raise ValueError(f"Insufficient USDT balance: {available_usdt} < {purchase_amount}")
        else:
            logging.info(f"[{ex}] DRY_RUN enabled: skipping USDT balance check")

        res = adapter.place_market_buy_quote(purchase_amount)
        order_id_from_details = res.order_id
        filled_quantity = float(res.executed_qty)
        cummulative_quote_qty = float(res.cummulative_quote_qty)
        filled_price = float(res.avg_price)
        fee_buy_usdt = float(getattr(res, 'fee_usd', 0.0) or 0.0)
        fee_buy_asset = getattr(res, 'fee_asset', None)
        fee_buy_asset_amount = float(getattr(res, 'fee_asset_amount', 0.0) or 0.0)
        logging.info(f"Calculated from client.get_order() details (orderId {order_id_from_details}): filled_price={filled_price}, filled_quantity={filled_quantity}")


        notify_payload = {
            'usdt': cummulative_quote_qty,
            'btc_qty': filled_quantity,
            'price': filled_price,
            'schedule_id': schedule_id,
            'order_id': order_id_from_details,
            'exchange': ex,
            'timestamp': now,
        }
        if context:
            for key in ('request_id', 'dedupe_key', 'cdc_status'):
                val = context.get(key)
                if val:
                    notify_payload[key] = val
        _attach_holdings_snapshot(
            notify_payload,
            ex,
            assets=("BTC", "USDT"),
            force_refresh=True,
        )
        notify_weekly_dca_buy(notify_payload)

        record_fee_totals('cdc_weekly_dca', ex, 'buy', fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)

        cursor.execute(
            """
            INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                purchase_amount,
                filled_quantity,
                filled_price,
                order_id_from_details,
                schedule_id,
                ex,
                fee_buy_usdt if fee_buy_usdt is not None else None,
                fee_buy_asset,
                fee_buy_asset_amount if fee_buy_asset_amount is not None else None,
            ),
        )
        db.commit()
        logging.info(f"Purchase record for order ID {order_id_from_details} saved to database.")

    except BinanceAPIException as e: # Legacy path safety
        # The specific logging for BinanceAPIException already happened in the inner try-except blocks
        current_order_id = 'N/A'
        if 'order_id_from_details' in locals() and order_id_from_details:
            current_order_id = order_id_from_details
        error_message = f"Binance API Error in purchase_btc (orderId: {current_order_id}): Code={e.code}, Message='{e.message}'"
        logging.error(error_message) # Log again with order context if available
        print(error_message)
        send_line_message(error_message)
        raise # Important to re-raise to inform the scheduler loop
    except ValueError as e: # Catch ValueErrors, including from qty checks and float conversions
        # Specific logging for ValueError would have happened at the point of failure
        current_order_id = 'N/A'
        if 'order_id_from_details' in locals() and order_id_from_details:
            current_order_id = order_id_from_details
        error_message = f"ValueError in purchase_btc (orderId: {current_order_id}): {e}"
        logging.error(error_message) # Log again with order context
        print(error_message)
        send_line_message(error_message)
        raise # Important to re-raise
    except Exception as e: # General exception handler for any other unexpected errors
        # Determine orderId for logging, if available
        current_order_id = 'N/A'
        if 'order_id_from_details' in locals() and order_id_from_details:
            current_order_id = order_id_from_details
        
        error_message = f"Unexpected error in purchase_btc (orderId: {current_order_id}): {type(e).__name__} - {e}"
        logging.error(error_message, exc_info=True) # exc_info=True will log the stack trace
        print(error_message)
        send_line_message(error_message)
        raise # Important to re-raise
    finally:
        if cursor:
            cursor.close()
        if db:
            db.close()

# ====== CDC DCA Strategy (enabled) ======
_CDC_CACHE = {'data': None, 'expires': 0}

def _ema_list(values, period: int):
    if not values:
        return []
    if period <= 1:
        return list(values)
    k = 2 / (period + 1)
    out = []
    prev = values[0]
    out.append(prev)
    for x in values[1:]:
        prev = (x * k) + (prev * (1 - k))
        out.append(prev)
    return out

def _last_true_idx(flags):
    for i in range(len(flags) - 1, -1, -1):
        if flags[i]:
            return i
    return None

def get_cdc_status_1d(client_override=None, use_cache: bool = True):
    """Compute CDC Action Zone on 1D BTCUSDT and return {'status','updated_at'}.
    Uses last closed candle to avoid repaint. Caches ~60s.
    """
    import time as _time
    from datetime import datetime as _dt
    now = _time.time()
    if use_cache and _CDC_CACHE['data'] is not None and now < _CDC_CACHE['expires']:
        return _CDC_CACHE['data']

    c = client_override or client
    klines = c.get_klines(symbol='BTCUSDT', interval=Client.KLINE_INTERVAL_1DAY, limit=300)
    if not klines or len(klines) < 50:
        data = {'status': 'down', 'updated_at': _dt.utcnow().isoformat() + 'Z'}
        _CDC_CACHE.update({'data': data, 'expires': now + 60})
        return data

    # Use last closed bar only
    import time
    current_ms = int(time.time() * 1000)
    if int(klines[-1][6]) > current_ms:
        klines = klines[:-1]

    closes = [float(k[4]) for k in klines]
    xprice = _ema_list(closes, 1)
    fast = _ema_list(xprice, 12)
    slow = _ema_list(xprice, 26)

    n = len(closes)
    bull = [fast[i] > slow[i] for i in range(n)]
    bear = [fast[i] < slow[i] for i in range(n)]
    green = [bull[i] and (xprice[i] > fast[i]) for i in range(n)]
    red = [bear[i] and (xprice[i] < fast[i]) for i in range(n)]

    buycond = [False] * n
    sellcond = [False] * n
    for i in range(1, n):
        buycond[i] = green[i] and (not green[i-1])
        sellcond[i] = red[i] and (not red[i-1])

    last_buy = _last_true_idx(buycond)
    last_sell = _last_true_idx(sellcond)
    cur = n - 1
    inf = float('inf')
    bars_since_buy = (cur - last_buy) if last_buy is not None else inf
    bars_since_sell = (cur - last_sell) if last_sell is not None else inf
    if bars_since_buy == inf and bars_since_sell == inf:
        is_bullish = bull[-1]
    else:
        is_bullish = bars_since_buy < bars_since_sell

    status = 'up' if is_bullish else 'down'
    data = {'status': status, 'updated_at': _dt.utcnow().isoformat() + 'Z'}
    _CDC_CACHE.update({'data': data, 'expires': now + 60})
    return data

def load_strategy_state(*, fail_on_error: bool = False):
    return _load_strategy_state(get_db_connection, fail_on_error=fail_on_error)

def save_strategy_state(patch: dict) -> None:
    return _save_strategy_state(patch, get_db_connection)

def _load_fifo_open_lots(exchange: str) -> list[dict]:
    lots: list[dict] = []
    try:
        with db_transaction() as (cursor, _):
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
                'qty': qty_f,
                'cost': cost_per_unit,
                'timestamp': purchase_time,
            }
        )

    for (sell_qty,) in sells:
        remaining = float(sell_qty or 0.0)
        idx = 0
        while remaining > 0 and idx < len(lots):
            lot = lots[idx]
            available = float(lot.get('qty') or 0.0)
            if available <= 0:
                idx += 1
                continue
            consume = min(available, remaining)
            lot['qty'] = max(0.0, available - consume)
            remaining -= consume
            if lot['qty'] <= 1e-9:
                lot['qty'] = 0.0
            else:
                idx += 1
    return [lot for lot in lots if lot.get('qty', 0.0) > 1e-9]

def compute_realized_pnl(exchange: str, sell_qty: float, proceeds: float) -> tuple[float, dict]:
    lots = _load_fifo_open_lots(exchange)
    remaining = float(sell_qty or 0.0)
    cost = 0.0
    contributions: list[dict] = []
    for lot in lots:
        if remaining <= 0:
            break
        available = float(lot.get('qty') or 0.0)
        if available <= 0:
            continue
        consume = min(available, remaining)
        cost += consume * float(lot.get('cost') or 0.0)
        contributions.append(
            {
                'qty': consume,
                'cost_per_unit': float(lot.get('cost') or 0.0),
                'source_time': str(lot.get('timestamp')) if lot.get('timestamp') else None,
            }
        )
        remaining -= consume
    metadata = {
        'method': 'fifo',
        'consumed_qty': float(sell_qty) - remaining,
        'remaining_qty': max(0.0, remaining),
        'lots_used': len(contributions),
        'lots_total': len(lots),
        'contributions': contributions[:5],
    }
    metadata['cost_basis'] = cost
    metadata['proceeds'] = float(proceeds)
    pnl = float(proceeds) - cost
    if remaining > 1e-6:
        metadata['note'] = 'Sold more BTC than available FIFO lots; excess treated as zero-cost'
    return pnl, metadata

def increment_reserve(amount: float, *, reason: str | None = None, note: str | None = None) -> float:
    """Increase global reserve_usdt by amount and return new value."""
    try:
        amt = float(amount or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if amt <= 0:
        return 0.0
    try:
        with db_transaction() as (cursor, _):
            cursor.execute("UPDATE strategy_state SET reserve_usdt = reserve_usdt + %s WHERE mode='cdc_dca_v1'", (amt,))
            cursor.execute("SELECT reserve_usdt FROM strategy_state WHERE mode='cdc_dca_v1'")
            val = float(cursor.fetchone()[0] or 0.0)
            log_reason = reason or 'weekly_skip'
            log_note = note or 'Skipped weekly DCA due to CDC RED'
            try:
                cursor.execute(
                    """
                    INSERT INTO reserve_log (event_time, change_usdt, reserve_after, reason, note)
                    VALUES (NOW(), %s, %s, %s, %s)
                    """,
                    (amt, val, log_reason, log_note)
                )
            except Exception:
                pass
        return val
    except Exception:
        return 0.0

def increment_reserve_exchange(exchange: str, amount: float, *, reason: str | None = None, note: str | None = None) -> float:
    """Increase per-exchange reserve and return new value."""
    try:
        amt = float(amount or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if amt <= 0:
        return 0.0
    try:
        with db_transaction() as (cursor, _):
            if exchange == 'binance':
                cursor.execute("UPDATE strategy_state SET reserve_binance_usdt = reserve_binance_usdt + %s WHERE mode='cdc_dca_v1'", (amt,))
                cursor.execute("SELECT reserve_binance_usdt FROM strategy_state WHERE mode='cdc_dca_v1'")
            else:
                cursor.execute("UPDATE strategy_state SET reserve_okx_usdt = reserve_okx_usdt + %s WHERE mode='cdc_dca_v1'", (amt,))
                cursor.execute("SELECT reserve_okx_usdt FROM strategy_state WHERE mode='cdc_dca_v1'")
            val = float(cursor.fetchone()[0] or 0.0)
            log_reason = reason or f'weekly_skip_{exchange}'
            log_note = note or f'Skipped weekly DCA on {exchange.upper()} due to CDC RED'
            try:
                cursor.execute(
                    """
                    INSERT INTO reserve_log (event_time, change_usdt, reserve_after, reason, note)
                    VALUES (NOW(), %s, %s, %s, %s)
                    """,
                    (amt, val, log_reason, log_note)
                )
            except Exception:
                pass
        return val
    except Exception:
        return 0.0

def purchase_on_exchange(now: datetime, exchange: str, amount: float, schedule_id: int | None, context: dict | None = None) -> dict:
    """Place market buy on a specific exchange using adapter; record history; notify."""
    try:
        state = load_strategy_state()
        adapter = get_adapter(exchange, testnet=USE_TESTNET, dry_run=is_dry_run())
        if exchange == 'okx':
            from exchanges.okx import OkxAdapter
            maxu = float(state.get('okx_max_usdt', 0) or 0)
            adapter = OkxAdapter(testnet=USE_TESTNET, dry_run=is_dry_run(), max_usdt=maxu if maxu > 0 else None)
        skip_liquidity_guards = str((context or {}).get('cdc_status') or '').lower() == 'okx_pure_dca'
        price = float(adapter.get_price())
        depth_ok, depth_info = evaluate_depth_guard(adapter, exchange, price)
        if not depth_ok and not skip_liquidity_guards:
            payload = {
                'exchange': exchange,
                'reason': depth_info.get('reason', 'depth_guard'),
                'depth': depth_info,
                'expected_notional': amount,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            logging.warning(
                "DCA buy liquidity block (depth) exchange=%s schedule_id=%s amount=%.2f reason=%s detail=%s",
                exchange, schedule_id, float(amount or 0.0), depth_info.get('reason', 'depth_guard'), depth_info
            )
            notify_liquidity_blocked('dca_buy', payload)
            return {'skipped': True, 'reason': depth_info.get('reason', 'depth_guard'), 'exchange': exchange, 'detail': depth_info}
        if not depth_ok and skip_liquidity_guards:
            logging.warning(
                "Bypassing depth guard for okx_pure_dca exchange=%s schedule_id=%s amount=%.2f detail=%s",
                exchange, schedule_id, float(amount or 0.0), depth_info
            )
        twap_ok, twap_info = evaluate_twap_guard(adapter, exchange, price)
        if not twap_ok and not skip_liquidity_guards:
            payload = {
                'exchange': exchange,
                'reason': twap_info.get('reason', 'twap_guard'),
                'twap': twap_info,
                'expected_notional': amount,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            logging.warning(
                "DCA buy liquidity block (twap) exchange=%s schedule_id=%s amount=%.2f reason=%s detail=%s",
                exchange, schedule_id, float(amount or 0.0), twap_info.get('reason', 'twap_guard'), twap_info
            )
            notify_liquidity_blocked('dca_buy', payload)
            return {'skipped': True, 'reason': twap_info.get('reason', 'twap_guard'), 'exchange': exchange, 'detail': twap_info}
        if not twap_ok and skip_liquidity_guards:
            logging.warning(
                "Bypassing twap guard for okx_pure_dca exchange=%s schedule_id=%s amount=%.2f detail=%s",
                exchange, schedule_id, float(amount or 0.0), twap_info
            )
        cap_ok, cap_info = evaluate_notional_cap(exchange, amount, state)
        if not cap_ok and not skip_liquidity_guards:
            payload = {
                'exchange': exchange,
                'reason': 'notional_cap',
                'cap': cap_info.get('cap'),
                'attempt': cap_info.get('attempt'),
                'timestamp': now,
            }
            logging.warning(
                "DCA buy liquidity block (notional_cap) exchange=%s schedule_id=%s amount=%.2f cap=%s attempt=%s",
                exchange, schedule_id, float(amount or 0.0), cap_info.get('cap'), cap_info.get('attempt')
            )
            notify_liquidity_blocked('dca_buy', payload)
            return {'skipped': True, 'reason': 'notional_cap', 'exchange': exchange, 'detail': cap_info}
        if not cap_ok and skip_liquidity_guards:
            logging.warning(
                "Bypassing notional cap for okx_pure_dca exchange=%s schedule_id=%s amount=%.2f detail=%s",
                exchange, schedule_id, float(amount or 0.0), cap_info
            )

        pre_btc = None
        pre_quote = None
        quote_asset = 'THB' if exchange == 'bitkub' else 'USDT'
        if exchange == 'bitkub':
            # Bitkub may return acceptance before fill fields are hydrated.
            # Capture balances so we can infer executed fill from delta.
            try:
                pre_btc = float((adapter.get_balance('BTC') or {}).get('free') or 0.0)
                pre_quote = float((adapter.get_balance(quote_asset) or {}).get('free') or 0.0)
            except Exception as bal_exc:
                logging.warning("Bitkub pre-balance snapshot failed: %s", bal_exc)

        res = adapter.place_market_buy_quote(amount)
        ex_qty = float(res.executed_qty);
        cqq = float(res.cummulative_quote_qty);
        avg = float(res.avg_price)
        order_id_raw = res.order_id
        order_id_db = None
        try:
            if order_id_raw is not None and str(order_id_raw).strip() != '':
                order_id_db = int(str(order_id_raw))
        except Exception:
            # Bitkub may return non-numeric identifiers (e.g., hash). Keep DB insert resilient.
            order_id_db = None
            logging.warning(
                "Non-numeric order_id from %s adapter: %s (store NULL in purchase_history)",
                exchange,
                order_id_raw,
            )
        fee_buy_usdt = float(getattr(res, 'fee_usd', 0.0) or 0.0)
        fee_buy_asset = getattr(res, 'fee_asset', None)
        fee_buy_asset_amount = float(getattr(res, 'fee_asset_amount', 0.0) or 0.0)

        if exchange == 'bitkub':
            def _apply_exec_info(info: dict | None, source: str) -> bool:
                nonlocal ex_qty, cqq, avg, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount
                if not isinstance(info, dict):
                    return False
                exec_qty = float(info.get('qty') or 0.0)
                exec_avg = float(info.get('avg_price') or 0.0)
                exec_spent = float(info.get('quote_spent') or 0.0)
                exec_fee = float(info.get('fee_quote') or 0.0)
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
                    source,
                    ex_qty,
                    quote_asset,
                    cqq,
                    avg,
                    schedule_id,
                    order_id_raw,
                )
                return True

            # Settlement window: wait a few seconds for exchange fill metadata to propagate.
            settle_timeout = max(float(os.getenv('BITKUB_SETTLE_TIMEOUT_SEC', '10')), 1.0)
            settle_sleep = max(float(os.getenv('BITKUB_SETTLE_POLL_SEC', '0.8')), 0.2)
            settle_deadline = time.time() + settle_timeout
            attempts = 0

            while (ex_qty <= 0 or cqq <= 0) and time.time() <= settle_deadline:
                attempts += 1
                if order_id_raw not in (None, ''):
                    try:
                        info = adapter.get_order_execution_symbol(
                            adapter.symbol(),
                            order_id_raw,
                            side='buy',
                            retries=1,
                            retry_sleep_sec=0.2,
                        )
                        if _apply_exec_info(info, 'order_info'):
                            break
                    except Exception as order_info_exc:
                        logging.warning("Bitkub order-info lookup failed (attempt=%s): %s", attempts, order_info_exc)

                    try:
                        info = adapter.get_order_execution_from_history_symbol(
                            adapter.symbol(),
                            order_id_raw,
                            limit=50,
                        )
                        if _apply_exec_info(info, 'order_history'):
                            break
                    except Exception as hist_exc:
                        logging.warning("Bitkub order-history lookup failed (attempt=%s): %s", attempts, hist_exc)

                # Final fallback: infer from wallet delta if exchange has not surfaced fill fields yet.
                try:
                    post_btc = float((adapter.get_balance('BTC') or {}).get('free') or 0.0)
                    post_quote = float((adapter.get_balance(quote_asset) or {}).get('free') or 0.0)
                    if pre_btc is not None and pre_quote is not None:
                        delta_btc = max(post_btc - pre_btc, 0.0)
                        delta_quote = max(pre_quote - post_quote, 0.0)
                        if delta_btc > 0 and delta_quote > 0:
                            ex_qty = delta_btc
                            cqq = delta_quote
                            avg = (cqq / ex_qty) if ex_qty > 0 else avg
                            logging.warning(
                                "Bitkub fill inferred from balance delta: qty=%.8f %s=%.8f schedule=%s attempts=%s",
                                ex_qty,
                                quote_asset,
                                cqq,
                                schedule_id,
                                attempts,
                            )
                            break
                except Exception as infer_exc:
                    logging.warning("Bitkub post-balance infer failed (attempt=%s): %s", attempts, infer_exc)

                if ex_qty <= 0 or cqq <= 0:
                    time.sleep(settle_sleep)

        if ex_qty <= 0 or cqq <= 0:
            raise ValueError('not filled')
        with db_transaction() as (cursor, _):
            cursor.execute(
                """
                INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cqq,
                    ex_qty,
                    avg,
                    order_id_db,
                    schedule_id,
                    exchange,
                    fee_buy_usdt if fee_buy_usdt is not None else None,
                    fee_buy_asset,
                    fee_buy_asset_amount if fee_buy_asset_amount is not None else None,
                ),
            )
        try:
            notify_payload = {
                'usdt': cqq,
                'quote_amount': cqq,
                'quote_asset': quote_asset,
                'btc_qty': ex_qty,
                'price': avg,
                'schedule_id': schedule_id,
                'order_id': order_id_raw,
                'exchange': exchange,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        notify_payload[key] = val
            _attach_holdings_snapshot(
                notify_payload,
                exchange,
                assets=("BTC", quote_asset),
                force_refresh=True,
            )
            sent = notify_weekly_dca_buy(notify_payload)
            if sent:
                logging.info(
                    "Weekly DCA notify sent (%s) schedule=%s order=%s amount=%.2f %s",
                    exchange,
                    schedule_id,
                    order_id_raw,
                    cqq,
                    quote_asset,
                )
            else:
                logging.error(
                    "Weekly DCA notify failed (%s) schedule=%s order=%s amount=%.2f %s",
                    exchange,
                    schedule_id,
                    order_id_raw,
                    cqq,
                    quote_asset,
                )
        except Exception as notify_exc:
            logging.exception(
                "Weekly DCA notify exception (%s) schedule=%s order=%s: %s",
                exchange,
                schedule_id,
                order_id_raw,
                notify_exc,
            )

        record_fee_totals('cdc_weekly_dca', exchange, 'buy', fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)

        try:
            meta = {
                'schedule_id': schedule_id,
                'order_id': order_id_raw,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        meta[key] = val
            log_compliance_event(now, 'buy', exchange, cqq, ex_qty, avg, 0.0, metadata=meta)
            if exchange in ('binance', 'okx') and cqq >= ANOMALY_NOTIONAL_THRESHOLD_USDT:
                notify_security_alert(
                    "High notional DCA buy",
                    {
                        'exchange': exchange.upper(),
                        'notional': f"{cqq:,.2f} USDT",
                        'threshold': f"{ANOMALY_NOTIONAL_THRESHOLD_USDT:,.2f} USDT",
                        'order_id': order_id_raw,
                    },
                )
        except Exception:
            logging.debug("Compliance log skipped for buy", exc_info=True)
        result_payload = {
            'executed': True,
            'exchange': exchange,
            'qty': ex_qty,
            'usdt': cqq,
            'quote_amount': cqq,
            'quote_asset': quote_asset,
            'price': avg,
            'order_id': order_id_raw,
        }
        if context:
            for key in ('request_id', 'dedupe_key', 'cdc_status'):
                val = context.get(key)
                if val:
                    result_payload[key] = val
        return result_payload
    except Exception as e:
        logging.exception(
            "purchase_on_exchange failed exchange=%s schedule_id=%s amount=%.8f: %s",
            exchange,
            schedule_id,
            float(amount or 0.0),
            e,
        )
        send_line_message(f"❌ Weekly DCA {exchange.upper()} error: {e}")
        return {'error': str(e), 'exchange': exchange}
def get_symbol_filters(symbol: str = 'BTCUSDT', exchange: str | None = None) -> dict:
    """Return unified filters across exchanges as {'stepSize','minQty','minNotional'}."""
    st = None
    if exchange:
        ex = exchange.lower()
    else:
        st = load_strategy_state()
        ex = (st.get('exchange', 'binance') if st else 'binance').lower()

    adapter = get_adapter(ex, testnet=USE_TESTNET, dry_run=is_dry_run())
    try:
        if ex == 'okx':
            from exchanges.okx import OkxAdapter
            if st is None:
                st = load_strategy_state()
            maxu = float((st or {}).get('okx_max_usdt', 0) or 0)
            adapter = OkxAdapter(testnet=USE_TESTNET, dry_run=is_dry_run(), max_usdt=maxu if maxu > 0 else None)
    except Exception:
        pass

    f = adapter.get_filters()
    if ex == 'okx':
        step = float(f.get('lotSz') or 0.000001)
        min_qty = float(f.get('minSz') or step)
        min_notional = 10.0
        return {'stepSize': step, 'minQty': min_qty, 'minNotional': min_notional}
    return {'stepSize': float(f.get('stepSize') or 0.000001), 'minQty': float(f.get('minQty') or 0.000001), 'minNotional': float(f.get('minNotional') or 10.0)}

def adjust_qty_to_step(qty: float, step: float) -> float:
    try:
        if step <= 0:
            return float(qty)
        qty_dec = Decimal(str(qty))
        step_dec = Decimal(str(step))
        units = (qty_dec / step_dec).to_integral_value(rounding=ROUND_DOWN)
        aligned = units * step_dec
        return float(aligned)
    except (InvalidOperation, ValueError):
        return float(qty)

def _execute_half_sell_for_exchange(
    now: datetime,
    exchange: str,
    pct: int,
    state: dict | None = None,
    context: dict | None = None,
) -> dict:
    ex = exchange.lower()
    pct = int(pct or 0)
    try:
        adapter = get_adapter(ex, testnet=USE_TESTNET, dry_run=is_dry_run())
        if ex == 'okx':
            try:
                from exchanges.okx import OkxAdapter
                maxu = float((state or {}).get('okx_max_usdt', 0) or 0)
                adapter = OkxAdapter(testnet=USE_TESTNET, dry_run=is_dry_run(), max_usdt=maxu if maxu > 0 else None)
            except Exception:
                pass

        if pct <= 0:
            payload = {
                'reason': 'sell_percent_zero',
                'btc_free': 0,
                'step': '-',
                'min_notional': '-',
                'pct': pct,
                'exchange': ex,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_half_sell_skipped(payload)
            return {'skipped': True, 'reason': 'sell_percent_zero', 'exchange': ex, 'pct': pct}

        balance = adapter.get_balance(asset='BTC')
        btc_free = float(balance.get('free') or 0)
        if btc_free <= 0:
            payload = {
                'reason': 'no_balance',
                'btc_free': btc_free,
                'step': '-',
                'min_notional': '-',
                'pct': pct,
                'exchange': ex,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_half_sell_skipped(payload)
            return {'skipped': True, 'reason': 'no_balance', 'exchange': ex, 'pct': pct}

        filters = get_symbol_filters('BTCUSDT', exchange=ex)
        step = float(filters['stepSize'])
        min_qty = float(filters['minQty'])
        min_notional = float(filters['minNotional'])

        sell_target = btc_free * (pct / 100.0)
        qty = adjust_qty_to_step(sell_target, step)
        if qty < min_qty:
            payload = {
                'reason': 'below_minQty',
                'btc_free': btc_free,
                'step': step,
                'min_notional': min_notional,
                'pct': pct,
                'exchange': ex,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_half_sell_skipped(payload)
            return {'skipped': True, 'reason': 'below_minQty', 'exchange': ex, 'pct': pct}

        price = float(adapter.get_price())
        depth_ok, depth_info = evaluate_depth_guard(adapter, ex, price)
        if not depth_ok:
            payload = {
                'exchange': ex,
                'reason': depth_info.get('reason', 'depth_guard'),
                'depth': depth_info,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('half_sell', payload)
            depth_info['skipped'] = True
            return {'skipped': True, 'reason': depth_info.get('reason', 'depth_guard'), 'exchange': ex, 'pct': pct, 'detail': depth_info}

        twap_ok, twap_info = evaluate_twap_guard(adapter, ex, price)
        if not twap_ok:
            payload = {
                'exchange': ex,
                'reason': twap_info.get('reason', 'twap_guard'),
                'twap': twap_info,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('half_sell', payload)
            twap_info['skipped'] = True
            return {'skipped': True, 'reason': twap_info.get('reason', 'twap_guard'), 'exchange': ex, 'pct': pct, 'detail': twap_info}

        notional = qty * price
        cap_ok, cap_info = evaluate_notional_cap(ex, notional, state)
        if not cap_ok:
            payload = {
                'exchange': ex,
                'reason': 'notional_cap',
                'cap': cap_info.get('cap'),
                'attempt': cap_info.get('attempt'),
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('half_sell', payload)
            return {'skipped': True, 'reason': 'notional_cap', 'exchange': ex, 'pct': pct, 'detail': cap_info}

        ok, liquidity = assess_liquidity(adapter, ex, context=context)
        if not ok:
            payload = {
                'exchange': ex,
                'reason': liquidity.get('reason'),
                'spread_pct': liquidity.get('spread_pct'),
                'threshold_pct': liquidity.get('threshold_pct'),
                'expected_notional': notional,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('half_sell', payload)
            payload.update({'skipped': True})
            return {'skipped': True, 'reason': liquidity.get('reason', 'liquidity_guard'), 'exchange': ex, 'pct': pct}
        if notional < min_notional:
            payload = {
                'reason': 'below_minNotional',
                'btc_free': btc_free,
                'step': step,
                'min_notional': min_notional,
                'pct': pct,
                'exchange': ex,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_half_sell_skipped(payload)
            return {'skipped': True, 'reason': 'below_minNotional', 'exchange': ex, 'pct': pct}

        res = adapter.place_market_sell_qty(qty)
        order_id = res.order_id
        executed_qty = float(res.executed_qty)
        cummulative_quote_qty = float(res.cummulative_quote_qty)
        if executed_qty <= 0 or cummulative_quote_qty <= 0:
            raise ValueError('Sell order not filled or zero quantities')
        avg_price = cummulative_quote_qty / executed_qty if executed_qty else 0.0
        pnl_value, pnl_meta = compute_realized_pnl(ex, executed_qty, cummulative_quote_qty)
        fee_sell_usdt = float(getattr(res, 'fee_usd', 0.0) or 0.0)
        fee_sell_asset = getattr(res, 'fee_asset', None)
        fee_sell_asset_amount = float(getattr(res, 'fee_asset_amount', 0.0) or 0.0)

        with db_transaction() as (cursor, _):
            cursor.execute(
                """
                INSERT INTO sell_history (sell_time, symbol, btc_quantity, usdt_received, price, order_id, sell_percent, note, exchange, fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    'BTCUSDT',
                    executed_qty,
                    cummulative_quote_qty,
                    avg_price,
                    order_id,
                    pct,
                    'sell via CDC',
                    ex,
                    fee_sell_usdt if fee_sell_usdt is not None else None,
                    fee_sell_asset,
                    fee_sell_asset_amount if fee_sell_asset_amount is not None else None,
                )
            )

        notify_payload = {
            'btc_qty': executed_qty,
            'price': avg_price,
            'usdt': cummulative_quote_qty,
            'order_id': order_id,
            'pct': pct,
            'exchange': ex,
            'timestamp': now,
        }
        if context:
            for key in ('request_id', 'dedupe_key', 'cdc_status'):
                val = context.get(key)
                if val:
                    notify_payload[key] = val
        notify_half_sell_executed(notify_payload)

        record_fee_totals('cdc_half_sell', ex, 'sell', fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount)

        try:
            meta = dict(pnl_meta)
            meta.update({
                'order_id': order_id,
                'pct': pct,
                'cdc_status': context.get('cdc_status') if context else None,
                'request_id': context.get('request_id') if context else None,
                'dedupe_key': context.get('dedupe_key') if context else None,
            })
            log_compliance_event(now, 'sell', ex, cummulative_quote_qty, executed_qty, avg_price, pnl_value, metadata=meta)
            if abs(pnl_value) >= ANOMALY_PNL_THRESHOLD_USDT:
                notify_security_alert(
                    "Realized PnL exceeded threshold",
                    {
                        'exchange': ex.upper(),
                        'pnl_usdt': f"{pnl_value:,.2f}",
                        'threshold': f"{ANOMALY_PNL_THRESHOLD_USDT:,.2f}",
                        'order_id': order_id,
                    },
                )
        except Exception:
            logging.debug("Compliance log skipped for half-sell", exc_info=True)
        return {'executed': True, 'exchange': ex, 'qty': executed_qty, 'usdt': cummulative_quote_qty, 'price': avg_price, 'order_id': order_id, 'pct': pct}
    except Exception as e:
        logging.error(f"Half-sell {ex} error: {e}")
        send_line_message(f"❌ Half-sell {ex.upper()} error: {e}")
        return {'error': str(e), 'exchange': ex, 'pct': pct}

def execute_half_sell(now: datetime) -> dict:
    """Sell configurable percent of BTC on supported exchanges."""
    state = load_strategy_state()
    policy = str(state.get('half_sell_policy') or 'auto_proportional').lower()

    def _percent_for(ex: str) -> int:
        ex_low = ex.lower()
        val = state.get('sell_percent_okx') if ex_low == 'okx' else state.get('sell_percent_binance')
        if val is None:
            val = state.get('sell_percent')
        try:
            return int(val or 0)
        except Exception:
            return 0

    exchanges: list[str] = []
    if policy == 'binance_only':
        exchanges = ['binance']
    elif policy == 'okx_only':
        exchanges = ['okx']
    else:
        for ex in ('binance', 'okx'):
            pct = _percent_for(ex)
            if pct > 0:
                exchanges.append(ex)
        if not exchanges:
            exchanges = [str(state.get('exchange', 'binance')).lower()]

    seen = set()
    ordered_exchanges = []
    for ex in exchanges:
        ex_low = ex.lower()
        if ex_low not in seen:
            ordered_exchanges.append(ex_low)
            seen.add(ex_low)

    results = []
    for ex in ordered_exchanges:
        pct = _percent_for(ex)
        res = _execute_half_sell_for_exchange(now, ex, pct, state)
        results.append(res)

    executed_any = any(r.get('executed') for r in results if isinstance(r, dict))
    return {'executed': executed_any, 'results': results, 'policy': policy}

def execute_reserve_buy(now: datetime, context: dict | None = None) -> dict:
    """Use reserve_usdt (up to available USDT) to buy BTC; record purchase_history; notify."""
    try:
        # Load state and balances
        state = load_strategy_state()
        reserve = float(state.get('reserve_usdt', 0) or 0)
        if reserve <= 0:
            return {'skipped': True, 'reason': 'no_reserve'}

        st2 = load_strategy_state(); ex = st2.get('exchange', 'binance')
        adapter = get_adapter(ex, testnet=USE_TESTNET, dry_run=is_dry_run())
        balance = adapter.get_balance(asset='USDT')
        available_usdt = float(balance.get('free') or 0)
        spend = min(available_usdt, reserve)
        filters = get_symbol_filters('BTCUSDT', exchange=ex)
        min_notional = float(filters['minNotional'])
        if spend < min_notional:
            payload = {
                'spend': spend,
                'min_notional': min_notional,
                'reserve': reserve,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_reserve_buy_skipped_min_notional(payload)
            return {'skipped': True, 'reason': 'below_minNotional', 'spend': spend}

        price = float(adapter.get_price())
        depth_ok, depth_info = evaluate_depth_guard(adapter, ex, price)
        if not depth_ok:
            payload = {
                'exchange': ex,
                'reason': depth_info.get('reason', 'depth_guard'),
                'depth': depth_info,
                'expected_notional': spend,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('reserve_buy', payload)
            return {'skipped': True, 'reason': depth_info.get('reason', 'depth_guard'), 'exchange': ex, 'detail': depth_info}

        twap_ok, twap_info = evaluate_twap_guard(adapter, ex, price)
        if not twap_ok:
            payload = {
                'exchange': ex,
                'reason': twap_info.get('reason', 'twap_guard'),
                'twap': twap_info,
                'expected_notional': spend,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('reserve_buy', payload)
            return {'skipped': True, 'reason': twap_info.get('reason', 'twap_guard'), 'exchange': ex, 'detail': twap_info}

        cap_ok, cap_info = evaluate_notional_cap(ex, spend, state)
        if not cap_ok:
            payload = {
                'exchange': ex,
                'reason': 'notional_cap',
                'cap': cap_info.get('cap'),
                'attempt': cap_info.get('attempt'),
                'timestamp': now,
            }
            notify_liquidity_blocked('reserve_buy', payload)
            return {'skipped': True, 'reason': 'notional_cap', 'exchange': ex, 'detail': cap_info}

        # Execute via adapter (handles dry_run)
        ok, liquidity = assess_liquidity(adapter, ex, context=context)
        if not ok:
            payload = {
                'exchange': ex,
                'reason': liquidity.get('reason'),
                'spread_pct': liquidity.get('spread_pct'),
                'threshold_pct': liquidity.get('threshold_pct'),
                'expected_notional': spend,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('reserve_buy', payload)
            return {'skipped': True, 'reason': liquidity.get('reason', 'liquidity_guard'), 'exchange': ex}
        res = adapter.place_market_buy_quote(spend)
        order_id = res.order_id
        executed_qty = float(res.executed_qty)
        cummulative_quote_qty = float(res.cummulative_quote_qty)
        fee_buy_usdt = float(getattr(res, 'fee_usd', 0.0) or 0.0)
        fee_buy_asset = getattr(res, 'fee_asset', None)
        fee_buy_asset_amount = float(getattr(res, 'fee_asset_amount', 0.0) or 0.0)
        if executed_qty <= 0 or cummulative_quote_qty <= 0:
            raise ValueError('Reserve buy not filled or zero quantities')
        avg_price = cummulative_quote_qty / executed_qty

        with db_transaction() as (cursor, _):
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
                    ex,
                    fee_buy_usdt if fee_buy_usdt is not None else None,
                    fee_buy_asset,
                    fee_buy_asset_amount if fee_buy_asset_amount is not None else None,
                )
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
                    (-cummulative_quote_qty, new_reserve, 'reserve_buy', 'Auto reserve buy on CDC GREEN')
                )
            except Exception:
                pass

        notify_payload = {
            'spend': cummulative_quote_qty,
            'btc_qty': executed_qty,
            'price': avg_price,
            'reserve_left': new_reserve,
            'order_id': order_id,
            'exchange': ex,
            'timestamp': now,
        }
        if context:
            for key in ('request_id', 'dedupe_key'):
                val = context.get(key)
                if val:
                    notify_payload[key] = val
        if context and context.get('cdc_status'):
            notify_payload['cdc_status'] = context.get('cdc_status')
        notify_reserve_buy_executed(notify_payload)

        record_fee_totals('cdc_reserve_buy', ex, 'buy', fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)

        try:
            meta = {
                'reserve_after': new_reserve,
                'cdc_status': context.get('cdc_status') if context else None,
                'request_id': context.get('request_id') if context else None,
                'dedupe_key': context.get('dedupe_key') if context else None,
                'mode': 'global',
            }
            log_compliance_event(now, 'reserve_buy', ex, cummulative_quote_qty, executed_qty, avg_price, 0.0, metadata=meta)
            if cummulative_quote_qty >= ANOMALY_NOTIONAL_THRESHOLD_USDT:
                notify_security_alert(
                    "High notional reserve deployment",
                    {
                        'exchange': ex.upper(),
                        'notional': f"{cummulative_quote_qty:,.2f} USDT",
                        'threshold': f"{ANOMALY_NOTIONAL_THRESHOLD_USDT:,.2f} USDT",
                        'mode': 'global',
                    },
                )
        except Exception:
            logging.debug("Compliance log skipped for reserve buy", exc_info=True)
        return {'executed': True, 'spend': cummulative_quote_qty, 'qty': executed_qty, 'price': avg_price, 'order_id': order_id}
    except Exception as e:
        logging.error(f"Reserve buy error: {e}")
        send_line_message(f"❌ Reserve buy error: {e}")
        return {'error': str(e)}

def execute_reserve_buy_exchange(now: datetime, exchange: str, context: dict | None = None) -> dict:
    """Use per-exchange reserve to buy BTC on specific exchange."""
    try:
        st = load_strategy_state()
        reserve = float(st.get(f'reserve_{exchange}_usdt', 0) or 0)
        if reserve <= 0:
            return {'skipped': True, 'reason': 'no_reserve', 'exchange': exchange}
        adapter = get_adapter(exchange, testnet=USE_TESTNET, dry_run=is_dry_run())
        if exchange == 'okx':
            from exchanges.okx import OkxAdapter
            maxu = float(st.get('okx_max_usdt', 0) or 0)
            adapter = OkxAdapter(testnet=USE_TESTNET, dry_run=is_dry_run(), max_usdt=maxu if maxu > 0 else None)
        bal = adapter.get_balance('USDT')
        avail = float(bal.get('free') or 0)
        spend = min(avail, reserve)
        f = get_symbol_filters('BTCUSDT', exchange=exchange)
        min_notional = float(f.get('minNotional') or 10.0)
        if spend < min_notional:
            payload = {
                'spend': spend,
                'min_notional': min_notional,
                'reserve': reserve,
                'exchange': exchange,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_reserve_buy_skipped_min_notional(payload)
            return {'skipped': True, 'reason': 'below_minNotional', 'exchange': exchange, 'spend': spend}
        price = float(adapter.get_price())
        depth_ok, depth_info = evaluate_depth_guard(adapter, exchange, price)
        if not depth_ok:
            payload = {
                'exchange': exchange,
                'reason': depth_info.get('reason', 'depth_guard'),
                'depth': depth_info,
                'expected_notional': spend,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('reserve_buy', payload)
            return {'skipped': True, 'reason': depth_info.get('reason', 'depth_guard'), 'exchange': exchange, 'detail': depth_info}
        twap_ok, twap_info = evaluate_twap_guard(adapter, exchange, price)
        if not twap_ok:
            payload = {
                'exchange': exchange,
                'reason': twap_info.get('reason', 'twap_guard'),
                'twap': twap_info,
                'expected_notional': spend,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('reserve_buy', payload)
            return {'skipped': True, 'reason': twap_info.get('reason', 'twap_guard'), 'exchange': exchange, 'detail': twap_info}
        cap_ok, cap_info = evaluate_notional_cap(exchange, spend, st)
        if not cap_ok:
            payload = {
                'exchange': exchange,
                'reason': 'notional_cap',
                'cap': cap_info.get('cap'),
                'attempt': cap_info.get('attempt'),
                'timestamp': now,
            }
            notify_liquidity_blocked('reserve_buy', payload)
            return {'skipped': True, 'reason': 'notional_cap', 'exchange': exchange, 'detail': cap_info}
        ok, liquidity = assess_liquidity(adapter, exchange, context=context)
        if not ok:
            payload = {
                'exchange': exchange,
                'reason': liquidity.get('reason'),
                'spread_pct': liquidity.get('spread_pct'),
                'threshold_pct': liquidity.get('threshold_pct'),
                'expected_notional': spend,
                'timestamp': now,
            }
            if context:
                for key in ('request_id', 'dedupe_key'):
                    val = context.get(key)
                    if val:
                        payload[key] = val
            notify_liquidity_blocked('reserve_buy', payload)
            return {'skipped': True, 'reason': liquidity.get('reason', 'liquidity_guard'), 'exchange': exchange}
        res = adapter.place_market_buy_quote(spend)
        ex_qty = float(res.executed_qty)
        cqq = float(res.cummulative_quote_qty)
        avg = float(res.avg_price)
        fee_buy_usdt = float(getattr(res, 'fee_usd', 0.0) or 0.0)
        fee_buy_asset = getattr(res, 'fee_asset', None)
        fee_buy_asset_amount = float(getattr(res, 'fee_asset_amount', 0.0) or 0.0)
        if ex_qty <= 0 or cqq <= 0:
            raise ValueError('not filled')
        with db_transaction() as (cursor, _):
            cursor.execute(
                """
                INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cqq,
                    ex_qty,
                    avg,
                    res.order_id,
                    None,
                    exchange,
                    fee_buy_usdt if fee_buy_usdt is not None else None,
                    fee_buy_asset,
                    fee_buy_asset_amount if fee_buy_asset_amount is not None else None,
                )
            )
            if exchange == 'binance':
                cursor.execute("UPDATE strategy_state SET reserve_binance_usdt = GREATEST(reserve_binance_usdt - %s, 0) WHERE mode='cdc_dca_v1'", (cqq,))
            else:
                cursor.execute("UPDATE strategy_state SET reserve_okx_usdt = GREATEST(reserve_okx_usdt - %s, 0) WHERE mode='cdc_dca_v1'", (cqq,))
        notify_payload = {
            'spend': cqq,
            'btc_qty': ex_qty,
            'price': avg,
            'reserve_left': max(0.0, reserve - cqq),
            'order_id': res.order_id,
            'exchange': exchange,
            'timestamp': now,
        }
        if context:
            for key in ('request_id', 'dedupe_key'):
                val = context.get(key)
                if val:
                    notify_payload[key] = val
        if context and context.get('cdc_status'):
            notify_payload['cdc_status'] = context.get('cdc_status')
        notify_reserve_buy_executed(notify_payload)

        record_fee_totals('cdc_reserve_buy', exchange, 'buy', fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)

        try:
            meta = {
                'reserve_before': reserve,
                'reserve_after': max(0.0, reserve - cqq),
                'cdc_status': context.get('cdc_status') if context else None,
                'request_id': context.get('request_id') if context else None,
                'dedupe_key': context.get('dedupe_key') if context else None,
                'mode': 'per_exchange',
            }
            log_compliance_event(now, 'reserve_buy', exchange, cqq, ex_qty, avg, 0.0, metadata=meta)
            if cqq >= ANOMALY_NOTIONAL_THRESHOLD_USDT:
                notify_security_alert(
                    "High notional reserve deployment",
                    {
                        'exchange': exchange.upper(),
                        'notional': f"{cqq:,.2f} USDT",
                        'threshold': f"{ANOMALY_NOTIONAL_THRESHOLD_USDT:,.2f} USDT",
                        'mode': 'per_exchange',
                    },
                )
        except Exception:
            logging.debug("Compliance log skipped for reserve buy exchange", exc_info=True)
        return {'executed': True, 'exchange': exchange, 'spend': cqq, 'qty': ex_qty, 'price': avg}
    except Exception as e:
        logging.error(f"Reserve buy {exchange} error: {e}")
        send_line_message(f"❌ Reserve buy {exchange.upper()} error: {e}")
        return {'error': str(e), 'exchange': exchange}

async def run_s4_tick(now: datetime) -> None:
    """Evaluate S4 rotation strategy and emit dry-run rotation actions."""
    if not _env_flag('FEATURE_S4_ENABLED', False):
        return

    if now.tzinfo is None:
        now = utc.localize(now)
    record, metadata, config, runtime = get_s4_state()
    if not record:
        return

    try:
        enabled = int(record.get('cdc_enabled') or 0)
    except (TypeError, ValueError):
        enabled = 0
    if enabled == 0:
        return

    exchange_code = str(config.get('exchange') or 'okx').lower()
    adapter_name, exchange_label, btc_symbol, gold_symbol, gold_asset = _s4_exchange_artifacts(exchange_code)

    dry_run_mode = is_dry_run()
    adapter = None
    btc_units = 0.0
    gold_units = 0.0
    btc_price = 0.0
    gold_price = 0.0
    holdings_result = None

    try:
        adapter = get_adapter(adapter_name, testnet=USE_TESTNET, dry_run=dry_run_mode)
    except Exception as exc:
        logging.debug(f"S4 adapter init failed ({adapter_name}): {exc}")
        adapter = None

    try:
        holdings_result = fetch_balances([adapter_name], ['USDT', 'BTC', gold_asset])
        runtime['holdings'] = holdings_result
    except Exception as exc:
        logging.debug(f"S4 holdings fetch failed ({adapter_name}): {exc}")
        runtime['holdings'] = runtime.get('holdings') or {}
        holdings_result = None

    if holdings_result:
        asset_map = holdings_result.get(adapter_name) or {}
        btc_entry = asset_map.get('BTC') or {}
        gold_entry = asset_map.get(gold_asset) or {}
        btc_units = _safe_float(btc_entry.get('free'), 0.0)
        gold_units = _safe_float(gold_entry.get('free'), 0.0)
    elif adapter is not None:
        try:
            btc_balance = adapter.get_balance('BTC')
            gold_balance = adapter.get_balance(gold_asset)
            btc_units = _safe_float((btc_balance or {}).get('free'), 0.0)
            gold_units = _safe_float((gold_balance or {}).get('free'), 0.0)
        except Exception as exc:
            logging.debug(f"S4 holdings fallback failed ({adapter_name}): {exc}")

    if adapter is not None:
        try:
            btc_price = float(adapter.get_price_symbol(btc_symbol))
        except Exception:
            btc_price = 0.0
        try:
            gold_price = float(adapter.get_price_symbol(gold_symbol))
        except Exception:
            gold_price = 0.0

    if btc_price <= 0:
        btc_price = fetch_symbol_price_fallback(btc_symbol, exchange_code)
    if gold_price <= 0:
        gold_price = fetch_symbol_price_fallback(gold_symbol, exchange_code)

    exposure, usd_map = compute_s4_exposure_from_units(
        btc_units,
        gold_units,
        btc_price if btc_price > 0 else 1.0,
        gold_price if gold_price > 0 else 1.0,
        now,
    )
    metadata.setdefault('runtime', {})['exposure'] = exposure
    runtime['last_signal_at'] = now.isoformat()

    cdc_snapshot = None
    signal_source = 'binance_cdc'
    ratio_snapshot = None
    if exchange_code == 'okx':
        try:
            ratio_snapshot = _fetch_okx_ratio_signal()
        except Exception as exc:
            logging.warning(f"S4 ratio signal fetch failed: {exc}")

    if S4_HARDENING_ENABLED and exchange_code == 'okx':
        # Enforce okx_ratio as PRIMARY for flip decisions.
        if not (ratio_snapshot and ratio_snapshot.get('status')):
            _s4_hold(
                now,
                metadata,
                runtime,
                reason='ratio_missing',
                detail='okx_ratio snapshot missing or invalid; holding allocation (no flip)',
                alert_key='ratio_missing',
                alert_message='⚠️ S4 HOLD: okx_ratio missing/invalid (no flip).',
            )
            return
        updated_at = ratio_snapshot.get('updated_at')
        upd_dt = parse_iso_dt(str(updated_at)) if updated_at else None
        if not upd_dt:
            _s4_hold(
                now,
                metadata,
                runtime,
                reason='ratio_timestamp_invalid',
                detail=f'okx_ratio updated_at invalid: {updated_at}',
                alert_key='ratio_stale',
                alert_message='⚠️ S4 HOLD: okx_ratio timestamp invalid/stale (no flip).',
            )
            return
        ttl_minutes = max(int(S4_RATIO_TTL_MINUTES or 0), 1)
        age_seconds = (now.astimezone(utc) - upd_dt).total_seconds()
        if age_seconds > ttl_minutes * 60:
            _s4_hold(
                now,
                metadata,
                runtime,
                reason='ratio_stale',
                detail=f'okx_ratio age {int(age_seconds)}s > ttl {ttl_minutes}m',
                alert_key='ratio_stale',
                alert_message=f'⚠️ S4 HOLD: okx_ratio stale (> {ttl_minutes}m) (no flip).',
            )
            return
        cdc_snapshot = ratio_snapshot
        signal_source = str(ratio_snapshot.get('source') or 'okx_ratio')
    else:
        # Legacy behaviour: allow fallback to binance_cdc.
        if ratio_snapshot and ratio_snapshot.get('status'):
            cdc_snapshot = ratio_snapshot
            signal_source = str(ratio_snapshot.get('source') or 'okx_ratio')
        else:
            cdc_snapshot = get_cdc_status_1d()
            signal_source = 'binance_cdc'

    cdc_status = str(cdc_snapshot.get('status') or 'down').lower()
    target_asset = _s4_dca_target_asset(cdc_status)
    previous_confirmed = str(runtime.get('last_confirmed_status') or runtime.get('last_cdc_status') or '').lower() or None
    runtime['last_cdc_status'] = cdc_status
    runtime['signal_source'] = signal_source
    runtime['signal_target_asset'] = target_asset
    current_holding = _s4_runtime_holding_asset(runtime)
    if current_holding in ('BTC', 'GOLD'):
        _s4_set_runtime_holding_asset(runtime, current_holding)
    runtime['last_signal_snapshot'] = {
        'status': cdc_status,
        'updated_at': cdc_snapshot.get('updated_at'),
        'ratio': cdc_snapshot.get('ratio'),
        'btc_close': cdc_snapshot.get('btc_close'),
        'gold_close': cdc_snapshot.get('gold_close'),
    }

    # Phase 1: Neutral Zone log-only hooks (no behavior change).
    try:
        ratio_series = _fetch_okx_ratio_series()
        ratios = [ratio for _, ratio in ratio_series]
        ema12_series = _compute_ema_series(ratios, 12)
        ema26_series = _compute_ema_series(ratios, 26)
        if ema12_series and ema26_series:
            ema12 = float(ema12_series[-1])
            ema26 = float(ema26_series[-1])
            ema12_history = list(reversed(ema12_series))
            neutral_state, metrics = _s4_neutral_state(
                ema12=ema12,
                ema26=ema26,
                ema12_history=ema12_history,
                config=DEFAULT_NEUTRAL_CONFIG,
            )
            if neutral_state:
                last_ts = ratio_series[-1][0]
                ratio_close = float(ratio_series[-1][1])
                asof_date = datetime.fromtimestamp(last_ts / 1000.0, tz=utc).date().isoformat()
                _s4_log_neutral_state(
                    now,
                    runtime,
                    state=str(neutral_state.value),
                    metrics=metrics,
                    ratio_close=ratio_close,
                    ema12=ema12,
                    ema26=ema26,
                    asof_date=asof_date,
                    preset_name=DEFAULT_NEUTRAL_CONFIG.name,
                )
    except Exception as exc:
        logging.debug(f"S4 neutral zone log skipped: {exc}")

    # S4 hardening: confirmation/cooldown/max flip circuit breaker (NO-GO gates).
    if S4_HARDENING_ENABLED:
        # Record 1D signal history (de-dupe per as-of date).
        asof_date = None
        if cdc_snapshot.get('asof_date'):
            asof_date = str(cdc_snapshot.get('asof_date'))
        if not asof_date:
            asof_date = _s4_asof_date(str(cdc_snapshot.get('updated_at')) if cdc_snapshot.get('updated_at') else None)
        if asof_date:
            _s4_update_signal_history(
                runtime,
                {
                    'date': asof_date,
                    'status': cdc_status,
                    'source': signal_source,
                    'updated_at': cdc_snapshot.get('updated_at'),
                },
            )

        # Cooldown hard-lock
        last_flip_dt = parse_iso_dt(runtime.get('last_flip_at')) if isinstance(runtime.get('last_flip_at'), str) else None
        cooldown_days = max(int(S4_COOLDOWN_DAYS or 0), 0)
        if last_flip_dt and cooldown_days > 0:
            if (now.astimezone(utc) - last_flip_dt).total_seconds() < cooldown_days * 86400:
                _s4_hold(
                    now,
                    metadata,
                    runtime,
                    reason='cooldown_active',
                    detail=f'cooldown_days={cooldown_days}',
                    alert_key='cooldown',
                    alert_message=f'ℹ️ S4 HOLD: cooldown active ({cooldown_days}d).',
                    alert_interval_minutes=720,
                )
                return

        # Max flips / 30d circuit breaker (count successful executed flips, both directions)
        max_flips = max(int(S4_MAX_FLIPS_30D or 0), 0)
        if max_flips > 0:
            flips = _s4_count_successful_flips_30d()
            runtime['flip_count_30d'] = flips
            if flips >= max_flips:
                _s4_hold(
                    now,
                    metadata,
                    runtime,
                    reason='max_flips_reached',
                    detail=f'flips_30d={flips} >= max={max_flips}',
                    alert_key='max_flips',
                    alert_message=f'⚠️ S4 SAFE MODE: max flips reached ({flips}/{max_flips} in 30d). HOLD.',
                    alert_interval_minutes=1440,
                )
                return

        # 2-day confirmation (requires consecutive daily closes)
        confirm_days = max(int(S4_CONFIRM_DAYS or 0), 1)
        history = runtime.get('signal_history')
        confirmed = True
        if isinstance(history, list) and confirm_days > 1:
            confirmed = _s4_confirmed(history, days=confirm_days)
            if not confirmed:
                _s4_hold(
                    now,
                    metadata,
                    runtime,
                    reason='confirm_pending',
                    detail=f'confirm_days={confirm_days}',
                    alert_key='confirm_pending',
                    alert_message=f'ℹ️ S4 HOLD: waiting {confirm_days}-day confirmation.',
                    alert_interval_minutes=720,
                )
                return
        if confirmed:
            runtime['last_confirmed_status'] = cdc_status
            runtime['last_confirmed_at'] = now.isoformat()
        # No HOLD gates triggered in this tick; clear stale HOLD markers for UI clarity.
        runtime.pop('last_hold_reason', None)
        runtime.pop('last_hold_detail', None)

    target_btc_pct, target_gold_pct = _resolve_s4_target_allocations(config, cdc_status)
    target_alloc = runtime.setdefault('target_allocations', {})
    target_alloc['btc_pct'] = target_btc_pct
    target_alloc['gold_pct'] = target_gold_pct

    min_flip_usd = _safe_float((config or {}).get('min_flip_usd'), 500.0)
    rotation_executed = False
    executed_meta = None
    rotation_amount_usd = 0.0
    rotation_plan = None

    if previous_confirmed not in ('up', 'down'):
        logging.warning(
            "S4 transition with unknown previous CDC state (%s); skipping rotation and persisting current state",
            previous_confirmed,
        )
        _s4_set_runtime_holding_asset(runtime, target_asset)
        runtime['last_action'] = {
            'result': 'noop_unknown_prev',
            'dry_run': dry_run_mode or adapter is None,
            'target_btc_pct': target_btc_pct,
            'target_gold_pct': target_gold_pct,
            'total_usd': exposure['total_usd'],
        }
        runtime['last_action_result'] = [{'status': 'NOOP_UNKNOWN_PREV'}]
        runtime.pop('last_error', None)
        save_strategy_metadata('s4_multi_leg', metadata, {'last_run_at': now})
        return

    if previous_confirmed != cdc_status:
        rotation_plan = _plan_s4_rotation(
            current_btc_usd=usd_map.get('BTC', 0.0),
            current_gold_usd=usd_map.get('GOLD', 0.0),
            target_btc_pct=target_btc_pct,
            min_usd=max(min_flip_usd, 0.0),
        )

    # DCA-first mode: no live full-swap execution. Keep DCA target in sync with CDC
    # and optionally log shadow swap decisions for later review.
    if not S4_SWAP_EXEC_ENABLED:
        active_asset = str(_s4_runtime_holding_asset(runtime) or '').upper()
        if active_asset not in ('BTC', 'GOLD'):
            _s4_set_runtime_holding_asset(runtime, target_asset)
        eod_snapshot = _s4_latest_eod_snapshot() or {}
        eod_cdc_status = str(eod_snapshot.get('cdc_status') or '').lower()
        eod_asof_date = str(eod_snapshot.get('date') or '')
        eod_lag_days = int(_safe_float(eod_snapshot.get('eod_lag_days'), 0.0))
        mismatch = bool(eod_cdc_status and eod_cdc_status != cdc_status)
        # Count mismatch streak once per EOD snapshot date (not every 5-minute tick).
        streak_event = 'unchanged'
        if runtime.get('mismatch_counter_mode') != 'daily_eod':
            runtime['mismatch_counter_mode'] = 'daily_eod'
            runtime['mismatch_streak_days'] = 0
            runtime.pop('mismatch_last_counted_date', None)
            streak_event = 'counter_mode_reset'
        count_key = eod_asof_date or now.astimezone(utc).date().isoformat()
        already_counted = str(runtime.get('mismatch_last_counted_date') or '') == str(count_key)
        prior_streak = int(_safe_float(runtime.get('mismatch_streak_days'), 0.0))
        if already_counted:
            current_streak = prior_streak
            streak_event = 'same_eod_date_no_recount'
        else:
            current_streak = (prior_streak + 1) if mismatch else 0
            runtime['mismatch_last_counted_date'] = count_key
            if mismatch:
                streak_event = 'new_eod_mismatch_counted'
            elif prior_streak > 0:
                streak_event = 'match_recovered_reset'
            else:
                streak_event = 'new_eod_match_still_zero'
        if mismatch:
            runtime['mismatch_last_seen_at'] = now.isoformat()
        else:
            runtime.pop('mismatch_last_seen_at', None)
        severity = _s4_mismatch_severity(
            mismatch=mismatch,
            eod_lag_days=eod_lag_days,
            streak=current_streak,
        )
        runtime['mismatch_streak_days'] = current_streak
        runtime['analytics_runtime_mismatch'] = mismatch
        runtime['mismatch_severity'] = severity
        runtime['mismatch_eod_status'] = eod_cdc_status or None
        runtime['mismatch_eod_date'] = eod_asof_date or None
        runtime['mismatch_eod_lag_days'] = eod_lag_days
        runtime['mismatch_streak_event'] = streak_event
        if mismatch and eod_lag_days == 0 and severity in ('warn', 'critical'):
            alert_interval_seconds = 43200 if severity == 'warn' else 10800
            last_alert = parse_iso_dt(runtime.get('mismatch_last_alert_at')) if isinstance(runtime.get('mismatch_last_alert_at'), str) else None
            should_alert = True
            if last_alert:
                should_alert = (now.astimezone(utc) - last_alert).total_seconds() >= alert_interval_seconds
            if should_alert:
                notify_security_alert(
                    "S4 fresh-EOD analytics/runtime mismatch",
                    {
                        "severity": severity.upper(),
                        "runtime_cdc": cdc_status,
                        "eod_cdc": eod_cdc_status or "n/a",
                        "eod_asof_date": eod_asof_date or "n/a",
                        "eod_lag_days": eod_lag_days,
                        "streak_days": current_streak,
                        "signal_source": signal_source,
                        "note": "Fresh EOD snapshot disagrees with runtime signal.",
                    },
                )
                runtime['mismatch_last_alert_at'] = now.isoformat()
        asof_date = _s4_asof_date(str(cdc_snapshot.get('updated_at')) if cdc_snapshot.get('updated_at') else None)
        should_log_heartbeat = bool(asof_date) and runtime.get('last_shadow_heartbeat_date') != asof_date
        if should_log_heartbeat:
            gate = _s4_shadow_swap_gate_decision(
                runtime=runtime,
                cdc_status=cdc_status,
                now=now,
            )
            heartbeat_entry = {
                'at': now.isoformat(),
                'asof_date': asof_date,
                'holding': gate['holding'],
                'holding_asset': gate['holding'],
                'target_asset': gate['target_asset'],
                'decision': gate['decision'],
                'reason': gate['reason'],
                'cdc_status': cdc_status,
                'neutral_state': gate['neutral_state'],
                'slope_pct': gate['slope_pct'],
                'gap_pct': gate['gap_pct'],
                'days_since_last_swap': gate['days_since_last_swap'],
                'next_unlock_condition': gate.get('next_unlock_condition'),
                'next_unlock_min_days': gate.get('next_unlock_min_days'),
                'analytics_runtime_mismatch': mismatch,
                'mismatch_severity': severity,
                'mismatch_streak_days': current_streak,
                'mismatch_streak_event': streak_event,
                'eod_asof_date': eod_asof_date,
                'runtime_signal_ts': now.isoformat(),
            }
            shadow_log = runtime.setdefault('shadow_swap_log', [])
            shadow_log.append(heartbeat_entry)
            runtime['shadow_swap_log'] = shadow_log[-120:]
            runtime['last_shadow_heartbeat_date'] = asof_date
            runtime['last_shadow_heartbeat'] = heartbeat_entry
            record_rotation_event(
                executed_at=now,
                strategy_mode='s4_multi_leg',
                from_asset=str(gate['holding']),
                to_asset=str(gate['target_asset']),
                notional_usd=float(rotation_plan.get('rotate_usd') or 0.0) if rotation_plan else 0.0,
                cdc_status=cdc_status,
                delta_pct=rotation_plan.get('delta_btc_pct') if rotation_plan else None,
                reason='shadow_swap_heartbeat',
                metadata={
                    'shadow': True,
                    'heartbeat': True,
                    'holding_asset': gate['holding'],
                    'target_asset': gate['target_asset'],
                    'signal_source': signal_source,
                    'target_btc_pct': target_btc_pct,
                    'target_gold_pct': target_gold_pct,
                    'swap_exec_enabled': False,
                    'analytics_runtime_mismatch': mismatch,
                    'mismatch_severity': severity,
                    'mismatch_streak_days': current_streak,
                    'mismatch_streak_event': streak_event,
                    'eod_asof_date': eod_asof_date,
                    'runtime_signal_ts': now.isoformat(),
                    'gate': gate,
                },
            )
        if rotation_plan and S4_SHADOW_SWAP_LOG_ENABLED:
            shadow_entry = {
                'at': now.isoformat(),
                'holding_asset': rotation_plan.get('from_asset'),
                'from': rotation_plan.get('from_asset'),
                'target_asset': rotation_plan.get('to_asset'),
                'to': rotation_plan.get('to_asset'),
                'planned_usd': round(float(rotation_plan.get('rotate_usd') or 0.0), 2),
                'delta_btc_pct': rotation_plan.get('delta_btc_pct'),
                'cdc_status': cdc_status,
                'signal_source': signal_source,
                'target_btc_pct': target_btc_pct,
                'target_gold_pct': target_gold_pct,
                'swap_exec_enabled': False,
            }
            shadow_log = runtime.setdefault('shadow_swap_log', [])
            shadow_log.append(shadow_entry)
            runtime['shadow_swap_log'] = shadow_log[-120:]
            runtime['last_shadow_swap'] = shadow_entry
            record_rotation_event(
                executed_at=now,
                strategy_mode='s4_multi_leg',
                from_asset=str(rotation_plan.get('from_asset') or ''),
                to_asset=str(rotation_plan.get('to_asset') or ''),
                notional_usd=float(rotation_plan.get('rotate_usd') or 0.0),
                cdc_status=cdc_status,
                delta_pct=rotation_plan.get('delta_btc_pct'),
                reason='shadow_swap_plan',
                metadata={
                    'shadow': True,
                    'holding_asset': rotation_plan.get('from_asset'),
                    'target_asset': rotation_plan.get('to_asset'),
                    'signal_source': signal_source,
                    'target_btc_pct': target_btc_pct,
                    'target_gold_pct': target_gold_pct,
                    'swap_exec_enabled': False,
                },
            )

        runtime['last_action'] = {
            'result': 'shadow_swap_plan' if rotation_plan else 'dca_target_only',
            'dry_run': True,
            'holding_asset': _s4_runtime_holding_asset(runtime, target_asset),
            'target_asset': target_asset,
            'target_btc_pct': target_btc_pct,
            'target_gold_pct': target_gold_pct,
            'signal_source': signal_source,
        }
        runtime['last_action_result'] = [{'status': 'SHADOW' if rotation_plan else 'NOOP'}]
        runtime.pop('last_error', None)
        save_strategy_metadata('s4_multi_leg', metadata, {'last_run_at': now})
        return

    if rotation_plan:
        from_asset = str(rotation_plan['from_asset'])
        to_asset = str(rotation_plan['to_asset'])
        plan_usd = float(rotation_plan['rotate_usd'])

        price_from = gold_price if from_asset == 'GOLD' else btc_price
        price_to = btc_price if to_asset == 'BTC' else gold_price
        symbol_from = gold_symbol if from_asset == 'GOLD' else btc_symbol
        symbol_to = btc_symbol if to_asset == 'BTC' else gold_symbol
        available_units = gold_units if from_asset == 'GOLD' else btc_units

        executed_ok = False
        if adapter is not None and not dry_run_mode and price_from > 0 and price_to > 0 and available_units > 0:
            sell_units_target = min(available_units, plan_usd / price_from if price_from > 0 else 0.0)
            if sell_units_target <= 0:
                rotation_plan = None
            else:
                try:
                    if exchange_code == 'okx' and adapter_name == 'okx' and S4_EXEC_HARDENING_ENABLED:
                        ok_from, spread_from = _s4_check_spread_okx(adapter, symbol_from)
                        ok_to, spread_to = _s4_check_spread_okx(adapter, symbol_to)
                        try:
                            logging.info(
                                "S4 EXEC CHECK | from=%s spread=%.4f%% thr=%.4f%% bid=%.6f ask=%.6f | to=%s spread=%.4f%% thr=%.4f%% bid=%.6f ask=%.6f",
                                symbol_from,
                                float(spread_from.get("spread_pct") or 0.0),
                                float(spread_from.get("threshold_pct") or 0.0),
                                float(spread_from.get("bid") or 0.0),
                                float(spread_from.get("ask") or 0.0),
                                symbol_to,
                                float(spread_to.get("spread_pct") or 0.0),
                                float(spread_to.get("threshold_pct") or 0.0),
                                float(spread_to.get("bid") or 0.0),
                                float(spread_to.get("ask") or 0.0),
                            )
                        except Exception:
                            pass
                        if not ok_from or not ok_to:
                            _s4_hold(
                                now,
                                metadata,
                                runtime,
                                reason='s4_spread_guard',
                                detail=json.dumps({"from": spread_from, "to": spread_to}, ensure_ascii=False),
                                alert_key='s4_spread_guard',
                                alert_message='⚠️ S4 HOLD: spread guard blocked rotation (OKX).',
                                alert_interval_minutes=180,
                            )
                            return

                        # Quantize sell quantity to lot size
                        filters_from = adapter.get_symbol_filters(symbol_from)
                        sell_qty, sell_qty_text = adapter.quantize_step(sell_units_target, float(filters_from.get("lotSz") or 0.0))
                        if sell_qty < float(filters_from.get("minSz") or 0.0):
                            _s4_hold(
                                now,
                                metadata,
                                runtime,
                                reason='s4_sell_below_min',
                                detail=f"symbol={symbol_from} qty={sell_qty} min={filters_from.get('minSz')}",
                                alert_key='s4_sell_below_min',
                                alert_message='⚠️ S4 HOLD: sell qty below min size (OKX).',
                            )
                            return

                        # Stage A: limit-first (maker-ish): sell at ask, buy at bid.
                        tob_from = adapter.get_top_of_book(symbol_from)
                        tob_to = adapter.get_top_of_book(symbol_to)
                        ask_from = float(tob_from.get("ask") or price_from)
                        bid_to = float(tob_to.get("bid") or price_to)

                        tick_from = float(filters_from.get("tickSz") or 0.01)
                        px_sell = adapter.round_to_tick(ask_from, tick_from)

                        sell_orders = []
                        sell_res = adapter.place_limit_sell_qty_symbol(
                            symbol_from,
                            sell_qty,
                            px_sell,
                            timeout_seconds=max(int(S4_LIMIT_FIRST_SECONDS), 1),
                            ord_type="limit",
                        )
                        sell_orders.append(sell_res)

                        # Stage B: optional IOC fallback for remaining sell (only if spread still OK)
                        remaining_sell_qty = max(sell_qty - float(sell_res.executed_qty or 0.0), 0.0)
                        if remaining_sell_qty > 0 and S4_IOC_FALLBACK_ENABLED:
                            logging.warning("S4 IOC fallback (sell) enabled | symbol=%s remaining_qty=%.8f", symbol_from, remaining_sell_qty)
                            ok_leg, _ = _s4_check_spread_okx(adapter, symbol_from)
                            if ok_leg:
                                bid_from = float(adapter.get_top_of_book(symbol_from).get("bid") or 0.0)
                                px_sell_ioc = adapter.round_to_tick(bid_from if bid_from > 0 else px_sell, tick_from)
                                sell_res2 = adapter.place_limit_sell_qty_symbol(
                                    symbol_from,
                                    remaining_sell_qty,
                                    px_sell_ioc,
                                    timeout_seconds=max(int(S4_LIMIT_FIRST_SECONDS), 1),
                                    ord_type="ioc",
                                )
                                sell_orders.append(sell_res2)

                        sell_total_quote = sum(float(o.cummulative_quote_qty or 0.0) for o in sell_orders)
                        sell_total_qty = sum(float(o.executed_qty or 0.0) for o in sell_orders)
                        if sell_total_quote <= 0 or sell_total_qty <= 0:
                            _s4_hold(
                                now,
                                metadata,
                                runtime,
                                reason='s4_sell_unfilled',
                                detail=f"symbol={symbol_from} qty={sell_qty_text} timeout={S4_LIMIT_FIRST_SECONDS}s",
                                alert_key='s4_sell_unfilled',
                                alert_message='⚠️ S4 HOLD: limit-first sell unfilled (OKX).',
                                alert_interval_minutes=180,
                            )
                            return

                        rotation_amount_usd = sell_total_quote

                        # Buy stage: compute qty from realized quote
                        filters_to = adapter.get_symbol_filters(symbol_to)
                        tick_to = float(filters_to.get("tickSz") or 0.01)
                        tob_to = adapter.get_top_of_book(symbol_to)
                        bid_to = float(tob_to.get("bid") or price_to)
                        ask_to = float(tob_to.get("ask") or price_to)
                        px_buy = adapter.round_to_tick(bid_to, tick_to)

                        buy_qty_raw = rotation_amount_usd / max(px_buy, 1e-9)
                        buy_qty, buy_qty_text = adapter.quantize_step(buy_qty_raw, float(filters_to.get("lotSz") or 0.0))
                        if buy_qty < float(filters_to.get("minSz") or 0.0):
                            _s4_hold(
                                now,
                                metadata,
                                runtime,
                                reason='s4_buy_below_min',
                                detail=f"symbol={symbol_to} qty={buy_qty_text} min={filters_to.get('minSz')}",
                                alert_key='s4_buy_below_min',
                                alert_message='⚠️ S4 HOLD: buy qty below min size (OKX).',
                            )
                            return

                        buy_orders = []
                        buy_res = adapter.place_limit_buy_qty_symbol(
                            symbol_to,
                            buy_qty,
                            px_buy,
                            timeout_seconds=max(int(S4_LIMIT_FIRST_SECONDS), 1),
                            ord_type="limit",
                        )
                        buy_orders.append(buy_res)

                        remaining_buy_quote = max(rotation_amount_usd - float(buy_res.cummulative_quote_qty or 0.0), 0.0)
                        if remaining_buy_quote > 0 and S4_IOC_FALLBACK_ENABLED:
                            logging.warning("S4 IOC fallback (buy) enabled | symbol=%s remaining_quote=%.2f", symbol_to, remaining_buy_quote)
                            ok_leg, _ = _s4_check_spread_okx(adapter, symbol_to)
                            if ok_leg:
                                px_buy_ioc = adapter.round_to_tick(ask_to if ask_to > 0 else px_buy, tick_to)
                                rem_qty_raw = remaining_buy_quote / max(px_buy_ioc, 1e-9)
                                rem_qty, _ = adapter.quantize_step(rem_qty_raw, float(filters_to.get("lotSz") or 0.0))
                                if rem_qty >= float(filters_to.get("minSz") or 0.0):
                                    buy_res2 = adapter.place_limit_buy_qty_symbol(
                                        symbol_to,
                                        rem_qty,
                                        px_buy_ioc,
                                        timeout_seconds=max(int(S4_LIMIT_FIRST_SECONDS), 1),
                                        ord_type="ioc",
                                    )
                                    buy_orders.append(buy_res2)

                        buy_total_quote = sum(float(o.cummulative_quote_qty or 0.0) for o in buy_orders)
                        buy_total_qty = sum(float(o.executed_qty or 0.0) for o in buy_orders)
                        if buy_total_qty <= 0:
                            _s4_hold(
                                now,
                                metadata,
                                runtime,
                                reason='s4_buy_unfilled',
                                detail=f"symbol={symbol_to} quote={rotation_amount_usd:.2f} timeout={S4_LIMIT_FIRST_SECONDS}s",
                                alert_key='s4_buy_unfilled',
                                alert_message='⚠️ S4 HOLD: limit-first buy unfilled (OKX).',
                                alert_interval_minutes=180,
                            )
                            return

                        executed_meta = {
                            'mode': 'limit_first',
                            'timeout_seconds': int(S4_LIMIT_FIRST_SECONDS),
                            'ioc_fallback': bool(S4_IOC_FALLBACK_ENABLED),
                            'sell_orders': [
                                {
                                    'order_id': o.order_id,
                                    'executed_qty': float(o.executed_qty or 0.0),
                                    'quote_usd': float(o.cummulative_quote_qty or 0.0),
                                    'avg_price': float(o.avg_price or 0.0),
                                    'fee_usd': float(getattr(o, 'fee_usd', 0.0) or 0.0),
                                    'fee_asset': getattr(o, 'fee_asset', None),
                                    'fee_asset_amount': float(getattr(o, 'fee_asset_amount', 0.0) or 0.0),
                                    'symbol': symbol_from,
                                }
                                for o in sell_orders
                            ],
                            'buy_orders': [
                                {
                                    'order_id': o.order_id,
                                    'executed_qty': float(o.executed_qty or 0.0),
                                    'quote_usd': float(o.cummulative_quote_qty or 0.0),
                                    'avg_price': float(o.avg_price or 0.0),
                                    'fee_usd': float(getattr(o, 'fee_usd', 0.0) or 0.0),
                                    'fee_asset': getattr(o, 'fee_asset', None),
                                    'fee_asset_amount': float(getattr(o, 'fee_asset_amount', 0.0) or 0.0),
                                    'symbol': symbol_to,
                                }
                                for o in buy_orders
                            ],
                            'realized_usd': float(sell_total_quote),
                            'spent_usd': float(buy_total_quote),
                            'executed_ok': executed_ok,
                        }
                        executed_ok = True
                        executed_meta['executed_ok'] = True
                    else:
                        sell_res = adapter.place_market_sell_qty_symbol(symbol_from, sell_units_target)
                        rotation_amount_usd = float(sell_res.cummulative_quote_qty or 0.0)
                        buy_res = adapter.place_market_buy_quote_symbol(symbol_to, rotation_amount_usd)
                        executed_ok = float(sell_res.cummulative_quote_qty or 0.0) > 0 and float(buy_res.executed_qty or 0.0) > 0
                        executed_meta = {
                            'sell_order': {
                                'order_id': sell_res.order_id,
                                'executed_qty': sell_res.executed_qty,
                                'quote_usd': sell_res.cummulative_quote_qty,
                                'avg_price': sell_res.avg_price,
                                'symbol': symbol_from,
                            },
                            'buy_order': {
                                'order_id': buy_res.order_id,
                                'executed_qty': buy_res.executed_qty,
                                'quote_usd': buy_res.cummulative_quote_qty,
                                'avg_price': buy_res.avg_price,
                                'symbol': symbol_to,
                            },
                            'realized_usd': rotation_amount_usd,
                            'executed_ok': executed_ok,
                        }
                    if adapter is not None and not dry_run_mode:
                        sell_symbol = symbol_from.replace('-', '')
                        buy_symbol = symbol_to.replace('-', '')
                        try:
                            with db_transaction() as (cursor, _):
                                if executed_meta and executed_meta.get('sell_orders'):
                                    for order_entry in executed_meta['sell_orders']:
                                        cursor.execute(
                                            """
                                            INSERT INTO sell_history (sell_time, symbol, btc_quantity, usdt_received, price, order_id, sell_percent, note, exchange, fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount)
                                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                            """,
                                            (
                                                now,
                                                sell_symbol,
                                                float(order_entry.get('executed_qty') or 0.0),
                                                float(order_entry.get('quote_usd') or 0.0),
                                                float(order_entry.get('avg_price') or 0.0),
                                                order_entry.get('order_id'),
                                                None,
                                                's4 rotation sell (limit-first)',
                                                exchange_label.lower(),
                                                float(order_entry.get('fee_usd') or 0.0) or None,
                                                order_entry.get('fee_asset'),
                                                float(order_entry.get('fee_asset_amount') or 0.0) or None,
                                            ),
                                        )
                                else:
                                    sell_fee_usdt = float(getattr(sell_res, 'fee_usd', 0.0) or 0.0)
                                    sell_fee_asset = getattr(sell_res, 'fee_asset', None)
                                    sell_fee_asset_amount = float(getattr(sell_res, 'fee_asset_amount', 0.0) or 0.0)
                                    cursor.execute(
                                        """
                                        INSERT INTO sell_history (sell_time, symbol, btc_quantity, usdt_received, price, order_id, sell_percent, note, exchange, fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount)
                                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                        """,
                                        (now, sell_symbol, float(sell_res.executed_qty or 0.0), float(sell_res.cummulative_quote_qty or 0.0), float(sell_res.avg_price or 0.0), sell_res.order_id, None, 's4 rotation sell', exchange_label.lower(), sell_fee_usdt or None, sell_fee_asset, sell_fee_asset_amount or None)
                                    )

                                if executed_meta and executed_meta.get('buy_orders'):
                                    for order_entry in executed_meta['buy_orders']:
                                        cursor.execute(
                                            """
                                            INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
                                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                            """,
                                            (
                                                now,
                                                float(order_entry.get('quote_usd') or 0.0),
                                                float(order_entry.get('executed_qty') or 0.0),
                                                float(order_entry.get('avg_price') or 0.0),
                                                order_entry.get('order_id'),
                                                None,
                                                exchange_label.lower(),
                                                float(order_entry.get('fee_usd') or 0.0) or None,
                                                order_entry.get('fee_asset'),
                                                float(order_entry.get('fee_asset_amount') or 0.0) or None,
                                            ),
                                        )
                                else:
                                    buy_fee_usdt = float(getattr(buy_res, 'fee_usd', 0.0) or 0.0)
                                    buy_fee_asset = getattr(buy_res, 'fee_asset', None)
                                    buy_fee_asset_amount = float(getattr(buy_res, 'fee_asset_amount', 0.0) or 0.0)
                                    cursor.execute(
                                        """
                                        INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
                                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                        """,
                                        (now, float(buy_res.cummulative_quote_qty or 0.0), float(buy_res.executed_qty or 0.0), float(buy_res.avg_price or 0.0), buy_res.order_id, None, exchange_label.lower(), buy_fee_usdt or None, buy_fee_asset, buy_fee_asset_amount or None)
                                    )
                        except Exception as exc:
                            logging.warning(f"S4 rotation logging error: {exc}")

                        if executed_meta and executed_meta.get('sell_orders'):
                            for order_entry in executed_meta.get('sell_orders') or []:
                                record_fee_totals(
                                    's4_rotation_sell',
                                    adapter_name,
                                    'sell',
                                    float(order_entry.get('fee_usd') or 0.0),
                                    order_entry.get('fee_asset'),
                                    float(order_entry.get('fee_asset_amount') or 0.0),
                                )
                            for order_entry in executed_meta.get('buy_orders') or []:
                                record_fee_totals(
                                    's4_rotation_buy',
                                    adapter_name,
                                    'buy',
                                    float(order_entry.get('fee_usd') or 0.0),
                                    order_entry.get('fee_asset'),
                                    float(order_entry.get('fee_asset_amount') or 0.0),
                                )
                        else:
                            record_fee_totals('s4_rotation_sell', adapter_name, 'sell', sell_fee_usdt, sell_fee_asset, sell_fee_asset_amount)
                            record_fee_totals('s4_rotation_buy', adapter_name, 'buy', buy_fee_usdt, buy_fee_asset, buy_fee_asset_amount)
                    btc_balance = adapter.get_balance('BTC')
                    gold_balance = adapter.get_balance(gold_asset)
                    btc_units = _safe_float((btc_balance or {}).get('free'))
                    gold_units = _safe_float((gold_balance or {}).get('free'))
                except Exception as exc:
                    logging.error(f"S4 rotation execution error: {exc}")
                    runtime['last_error'] = {
                        'at': now.isoformat(),
                        'reason': 'execution_failed',
                        'detail': str(exc),
                    }
                    save_strategy_metadata('s4_multi_leg', metadata, {'last_run_at': now})
                    return
        else:
            if price_from > 0 and price_to > 0 and available_units > 0:
                sell_units = min(available_units, plan_usd / price_from)
                rotation_amount_usd = sell_units * price_from
                buy_units = rotation_amount_usd / price_to if price_to > 0 else 0.0
                if from_asset == 'GOLD':
                    gold_units = max(gold_units - sell_units, 0.0)
                    btc_units += buy_units
                else:
                    btc_units = max(btc_units - sell_units, 0.0)
                    gold_units += buy_units
            else:
                rotation_plan = None

        if rotation_plan:
            exposure, usd_map = compute_s4_exposure_from_units(
                btc_units,
                gold_units,
                btc_price if btc_price > 0 else 1.0,
                gold_price if gold_price > 0 else 1.0,
                now,
            )
            metadata.setdefault('runtime', {})['exposure'] = exposure
            # Only lock in a flip timestamp after successful execution (prevents cooldown on unfilled/aborted attempts).
            if (adapter is not None) and (not dry_run_mode) and bool(executed_ok):
                runtime['last_flip_at'] = now.astimezone(utc).isoformat()
                _s4_set_runtime_holding_asset(runtime, target_asset)
            runtime['last_action'] = {
                'result': 'rotation',
                'holding_asset': from_asset,
                'from': from_asset,
                'target_asset': to_asset,
                'to': to_asset,
                'amount_usd': round(rotation_amount_usd or plan_usd, 2),
                'dry_run': dry_run_mode or adapter is None,
                'executed': executed_meta,
                'exchange': exchange_label,
                'target_btc_pct': target_btc_pct,
                'target_gold_pct': target_gold_pct,
            }
            rotation_meta = {
                'dry_run': dry_run_mode or adapter is None,
                'exchange': exchange_label,
                'holding_asset': from_asset,
                'target_asset': to_asset,
                'executed': executed_meta,
                'executed_ok': bool(executed_meta.get('executed_ok')) if isinstance(executed_meta, dict) and 'executed_ok' in executed_meta else bool(executed_meta),
                'target_btc_pct': target_btc_pct,
                'target_gold_pct': target_gold_pct,
                'planned_usd': plan_usd,
                'signal_source': signal_source,
            }
            record_rotation_event(
                executed_at=now,
                strategy_mode='s4_multi_leg',
                from_asset=from_asset,
                to_asset=to_asset,
                notional_usd=round(rotation_amount_usd or plan_usd, 2),
                cdc_status=cdc_status,
                delta_pct=rotation_plan.get('delta_btc_pct'),
                reason='cdc_flip',
                metadata=rotation_meta,
            )
            try:
                notes_payload = {
                    'exposure_btc_pct': exposure['btc']['weight'] * 100,
                    'target_btc_pct': target_btc_pct * 100,
                    'target_gold_pct': target_gold_pct * 100,
                    'delta_pct': (rotation_plan.get('delta_btc_pct') or 0.0) * 100,
                    'signal_source': signal_source,
                }
                if cdc_snapshot.get('ratio') is not None:
                    notes_payload['btc_gold_ratio'] = round(float(cdc_snapshot['ratio']), 6)
                if cdc_snapshot.get('btc_close') is not None:
                    notes_payload['btc_close_ratio_feed'] = float(cdc_snapshot['btc_close'])
                if cdc_snapshot.get('gold_close') is not None:
                    notes_payload['gold_close_ratio_feed'] = float(cdc_snapshot['gold_close'])
                notify_s4_rotation({
                    'from': from_asset,
                    'to': to_asset,
                    'holding_asset': from_asset,
                    'target_asset': to_asset,
                    'amount_usd': round(rotation_amount_usd or plan_usd, 2),
                    'cdc_status': cdc_status,
                    'signal_source': signal_source,
                    'btc_price': btc_price,
                    'gold_price': gold_price,
                    'notes': notes_payload,
                    'exchange': exchange_label,
                    'executed': executed_meta,
                })
            except Exception as exc:
                logging.warning(f"S4 rotation notify failed: {exc}")
            rotation_executed = True

    if not rotation_executed:
        _s4_set_runtime_holding_asset(runtime, target_asset)
        runtime['last_action'] = {
            'result': 'noop',
            'holding_asset': _s4_runtime_holding_asset(runtime, target_asset),
            'target_asset': target_asset,
            'total_usd': exposure['total_usd'],
            'dry_run': dry_run_mode or adapter is None,
            'target_btc_pct': target_btc_pct,
            'target_gold_pct': target_gold_pct,
        }

    runtime['exposure'] = exposure
    runtime['last_action_result'] = [{'status': 'EXECUTED' if rotation_executed else 'NOOP'}]
    runtime.pop('last_error', None)

    save_strategy_metadata('s4_multi_leg', metadata, {'last_run_at': now})

async def gate_weekly_dca(now: datetime, schedule_id: int, amount: float, extra: dict | None = None) -> dict:
    """Gate weekly DCA by CDC status per schedule. Supports per-exchange and both modes."""
    state = load_strategy_state()
    mode = (extra or {}).get('exchange_mode') or 'global'
    bz_amt = float((extra or {}).get('binance_amount') or 0)
    okx_amt = float((extra or {}).get('okx_amount') or 0)
    cdc_enabled = int(state.get('cdc_enabled', 1)) == 1
    active_exchange = str(state.get('exchange') or 'binance').lower()

    # Determine CDC status once according to legacy behaviour
    if not cdc_enabled:
        cdc_status = 'up'
    else:
        cdc_status = get_cdc_status_1d().get('status')

    if mode == 'pure_dca':
        day_key = now.astimezone(timezone('Asia/Bangkok')).date().isoformat()
        dedupe_key = f"pure_dca:{day_key}:{schedule_id or 0}"
        request_id = f"{dedupe_key}:{int(now.timestamp())}:{os.getpid()}"
        if not claim_dedupe_key(dedupe_key, request_id):
            return {
                'decision': 'noop',
                'mode': mode,
                'reason': 'duplicate_action_db',
                'request_id': request_id,
                'dedupe_key': dedupe_key,
            }
        await purchase_btc(now, float(amount or 0), schedule_id, context={
            'request_id': request_id,
            'dedupe_key': dedupe_key,
            'cdc_status': 'pure_dca',
            'timestamp': now,
        })
        return {
            'decision': 'buy',
            'amount': float(amount or 0),
            'mode': mode,
            'cdc': 'ignored',
            'request_id': request_id,
            'dedupe_key': dedupe_key,
        }

    if mode == 'okx_pure_dca':
        day_key = now.astimezone(timezone('Asia/Bangkok')).date().isoformat()
        dedupe_key = f"okx_pure_dca:{day_key}:{schedule_id or 0}"
        request_id = f"{dedupe_key}:{int(now.timestamp())}:{os.getpid()}"
        if not claim_dedupe_key(dedupe_key, request_id):
            return {
                'decision': 'noop',
                'mode': mode,
                'reason': 'duplicate_action_db',
                'request_id': request_id,
                'dedupe_key': dedupe_key,
            }
        result = purchase_on_exchange(
            now,
            'okx',
            float(amount or 0),
            schedule_id,
            context={
                'request_id': request_id,
                'dedupe_key': dedupe_key,
                'cdc_status': 'okx_pure_dca',
                'timestamp': now,
            },
        )
        if result.get('executed'):
            return {
                'decision': 'buy',
                'mode': mode,
                'amount': float(amount or 0),
                'cdc': 'ignored',
                'request_id': request_id,
                'dedupe_key': dedupe_key,
                'result': result,
            }
        return {
            'decision': 'noop',
            'mode': mode,
            'request_id': request_id,
            'dedupe_key': dedupe_key,
            'result': result,
        }

    if mode == 'bitkub':
        day_key = now.astimezone(timezone('Asia/Bangkok')).date().isoformat()
        dedupe_key = f"bitkub_pure_dca:{day_key}:{schedule_id or 0}"
        request_id = f"{dedupe_key}:{int(now.timestamp())}:{os.getpid()}"
        if not claim_dedupe_key(dedupe_key, request_id):
            return {
                'decision': 'noop',
                'mode': mode,
                'reason': 'duplicate_action_db',
                'request_id': request_id,
                'dedupe_key': dedupe_key,
            }
        result = purchase_on_exchange(
            now,
            'bitkub',
            float(amount or 0),
            schedule_id,
            context={
                'request_id': request_id,
                'dedupe_key': dedupe_key,
                'cdc_status': 'bitkub_pure_dca',
                'timestamp': now,
            },
        )
        if result.get('executed'):
            return {
                'decision': 'buy',
                'mode': mode,
                'amount': float(amount or 0),
                'request_id': request_id,
                'dedupe_key': dedupe_key,
                'result': result,
            }
        return {
            'decision': 'noop',
            'mode': mode,
            'request_id': request_id,
            'dedupe_key': dedupe_key,
            'result': result,
        }

    strategy = CdcDcaStrategy(
        config_params={
            'exchange': active_exchange,
            'sell_percent': state.get('sell_percent'),
            'sell_percent_binance': state.get('sell_percent_binance'),
            'sell_percent_okx': state.get('sell_percent_okx'),
        }
    )

    decision = strategy.decide_weekly_dca(
        WeeklyDcaDecisionInput(
            now=now,
            schedule_id=schedule_id,
            mode=mode,
            amount=amount,
            cdc_status='disabled' if not cdc_enabled else cdc_status,
            cdc_enabled=cdc_enabled,
            binance_amount=bz_amt,
            okx_amount=okx_amt,
        )
    )

    if mode == 's4':
        result = execute_s4_dca(now, amount, schedule_id)
        return {'mode': 's4', 'result': result, 'cdc': 's4'}

    if mode == 'global':
        # Expect exactly one action in legacy global mode
        if not decision.actions:
            return {'decision': 'noop', 'mode': mode, 'cdc': cdc_status}
        async def handle_global_buy(action):
            if not claim_dedupe_key(action.dedupe_key, action.request_id):
                return ActionResult(
                    request_id=action.request_id,
                    dedupe_key=action.dedupe_key,
                    status=ActionStatus.SKIPPED,
                    detail="duplicate_action_db",
                )
            amt = float(action.payload.get('amount') or amount)
            await purchase_btc(now, amt, schedule_id, context={
                'request_id': action.request_id,
                'dedupe_key': action.dedupe_key,
                'cdc_status': action.payload.get('cdc_status', cdc_status),
                'timestamp': now,
            })
            return ActionResult(
                request_id=action.request_id,
                dedupe_key=action.dedupe_key,
                status=ActionStatus.SUCCESS,
                data={
                    'decision': 'buy',
                    'amount': amt,
                    'cdc': action.payload.get('cdc_status', cdc_status),
                    'mode': mode,
                    'request_id': action.request_id,
                    'dedupe_key': action.dedupe_key,
                },
            )

        async def handle_global_reserve(action):
            if not claim_dedupe_key(action.dedupe_key, action.request_id):
                return ActionResult(
                    request_id=action.request_id,
                    dedupe_key=action.dedupe_key,
                    status=ActionStatus.SKIPPED,
                    detail="duplicate_action_db",
                )
            amt = float(action.payload.get('amount') or amount)
            new_reserve_val = increment_reserve(amt)
            notify_context = {
                'request_id': action.request_id,
                'dedupe_key': action.dedupe_key,
                'cdc_status': action.payload.get('cdc_status', cdc_status),
                'timestamp': now,
            }
            _attach_holdings_snapshot(
                notify_context,
                active_exchange,
                assets=("BTC", "USDT"),
            )
            try:
                notify_weekly_dca_skipped(amt, new_reserve_val, context=notify_context)
            except Exception:
                pass
            return ActionResult(
                request_id=action.request_id,
                dedupe_key=action.dedupe_key,
                status=ActionStatus.SUCCESS,
                data={
                    'decision': 'reserve',
                    'reserve_usdt': new_reserve_val,
                    'cdc': action.payload.get('cdc_status', cdc_status),
                    'mode': mode,
                    'request_id': action.request_id,
                    'dedupe_key': action.dedupe_key,
                },
            )

        handlers = {
            StrategyActionType.DCA_BUY: handle_global_buy,
            StrategyActionType.RESERVE_MOVE: handle_global_reserve,
        }
        results = await strategy_orchestrator.execute(decision, handlers)
        if not results:
            return {'decision': 'noop', 'mode': mode, 'cdc': cdc_status}
        first = results[0]
        if first.status is ActionStatus.SUCCESS and first.data:
            return first.data  # type: ignore[return-value]
        return {'decision': 'noop', 'mode': mode, 'cdc': cdc_status}

    # non-global modes
    async def handle_exchange_buy(action):
        if not claim_dedupe_key(action.dedupe_key, action.request_id):
            return ActionResult(
                request_id=action.request_id,
                dedupe_key=action.dedupe_key,
                status=ActionStatus.SKIPPED,
                detail="duplicate_action_db",
            )
        exchange = str(action.payload.get('exchange') or '').lower()
        amt = float(action.payload.get('amount') or 0)
        context = {
            'request_id': action.request_id,
            'dedupe_key': action.dedupe_key,
            'cdc_status': action.payload.get('cdc_status', cdc_status),
            'timestamp': now,
        }
        result = purchase_on_exchange(now, exchange, amt, schedule_id, context=context)
        if result.get('executed'):
            status = ActionStatus.SUCCESS
        elif result.get('skipped'):
            status = ActionStatus.SKIPPED
        else:
            status = ActionStatus.FAILED
        return ActionResult(
            request_id=action.request_id,
            dedupe_key=action.dedupe_key,
            status=status,
            data={'exchange': exchange, 'payload': result, 'request_id': action.request_id, 'dedupe_key': action.dedupe_key},
        )


    async def handle_exchange_reserve(action):
        if not claim_dedupe_key(action.dedupe_key, action.request_id):
            return ActionResult(
                request_id=action.request_id,
                dedupe_key=action.dedupe_key,
                status=ActionStatus.SKIPPED,
                detail="duplicate_action_db",
            )
        exchange = str(action.payload.get('exchange') or '').lower()
        amt = float(action.payload.get('amount') or 0)
        new_val = increment_reserve_exchange(exchange, amt)
        notify_context = {
            'request_id': action.request_id,
            'dedupe_key': action.dedupe_key,
            'cdc_status': action.payload.get('cdc_status', cdc_status),
            'timestamp': now,
        }
        _attach_holdings_snapshot(
            notify_context,
            exchange,
            assets=("BTC", "USDT"),
        )
        try:
            notify_weekly_dca_skipped_exchange(exchange, amt, new_val, context=notify_context)
        except Exception:
            pass
        return ActionResult(
            request_id=action.request_id,
            dedupe_key=action.dedupe_key,
            status=ActionStatus.SUCCESS,
            data={
                'exchange': exchange,
                'payload': {
                    'decision': 'reserve',
                    'exchange': exchange,
                    'reserve': new_val,
                },
                'request_id': action.request_id,
                'dedupe_key': action.dedupe_key,
            },
        )

    handlers = {
        StrategyActionType.DCA_BUY: handle_exchange_buy,
        StrategyActionType.RESERVE_MOVE: handle_exchange_reserve,
    }
    results = await strategy_orchestrator.execute(decision, handlers)
    payloads = []
    for item in results:
        data = item.data or {}
        payload = data.get('payload')
        if isinstance(payload, dict):
            payloads.append(payload)
    return {'mode': mode, 'cdc': cdc_status if cdc_enabled else 'disabled', 'results': payloads}

async def check_cdc_transition_and_act(now: datetime) -> None:
    """Detect CDC transitions and execute actions (half-sell or reserve-buy)."""
    try:
        state = load_strategy_state(fail_on_error=True)
    except Exception as exc:
        logging.error("CDC transition check skipped: strategy_state unavailable (%s)", exc)
        return
    # Respect global toggle
    if int(state.get('cdc_enabled', 1)) == 0:
        return
    curr = get_cdc_status_1d().get('status')
    prev = state.get('last_cdc_status')
    if prev == curr:
        return

    if prev not in ('up', 'down'):
        logging.warning("CDC transition detected with unknown previous state (%s); updating state without actions", prev)
        save_strategy_state({'last_cdc_status': curr, 'last_transition_at': now.strftime('%Y-%m-%d %H:%M:%S')})
        return

    try:
        notify_cdc_transition(prev, curr, timestamp=now)
    except Exception:
        pass

    strategy = CdcDcaStrategy(
        config_params={
            'exchange': state.get('exchange'),
            'sell_percent': state.get('sell_percent'),
            'sell_percent_binance': state.get('sell_percent_binance'),
            'sell_percent_okx': state.get('sell_percent_okx'),
            'half_sell_policy': state.get('half_sell_policy'),
        }
    )

    decision = strategy.decide_transition(
        TransitionDecisionInput(
            now=now,
            previous_status=prev,
            current_status=curr,
            red_epoch_active=bool(int(state.get('red_epoch_active') or 0)),
            half_sell_policy=str(state.get('half_sell_policy') or 'auto_proportional'),
            sell_percent_binance=int(state.get('sell_percent_binance') or state.get('sell_percent') or 0),
            sell_percent_okx=int(state.get('sell_percent_okx') or state.get('sell_percent') or 0),
            sell_percent_global=int(state.get('sell_percent') or 0),
            active_exchange=str(state.get('exchange') or 'binance'),
            reserve_usdt=float(state.get('reserve_usdt') or 0.0),
            reserve_binance_usdt=float(state.get('reserve_binance_usdt') or 0.0),
            reserve_okx_usdt=float(state.get('reserve_okx_usdt') or 0.0),
        )
    )

    if not decision.actions:
        save_strategy_state({'last_cdc_status': curr, 'last_transition_at': now.strftime('%Y-%m-%d %H:%M:%S')})
        if curr == 'up':
            save_strategy_state({'red_epoch_active': 0})
        return

    handlers = {
        StrategyActionType.HALF_SELL: lambda action: handle_half_sell_action(now, action, state=state),
        StrategyActionType.RESERVE_BUY: lambda action: handle_reserve_buy_action(now, action),
    }
    results = await strategy_orchestrator.execute(decision, handlers)

    half_sell_executed = False
    for action, result in zip(decision.actions, results):
        if (
            action.action_type is StrategyActionType.HALF_SELL
            and result.status is ActionStatus.SUCCESS
            and (result.data or {}).get('payload', {}).get('executed')
        ):
            half_sell_executed = True
            break

    updates = {
        'last_cdc_status': curr,
        'last_transition_at': now.strftime('%Y-%m-%d %H:%M:%S'),
    }
    if curr == 'down':
        updates['red_epoch_active'] = 1
        if half_sell_executed:
            updates['last_half_sell_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
    else:
        updates['red_epoch_active'] = 0

    save_strategy_state(updates)
# Main scheduler loop
async def run_loop_scheduler():
    """Run the DCA scheduler to purchase BTC based on multiple schedules."""
    print("⏳ Real-time BTC DCA scheduler started...")
    config_cache = []
    cache_expiry = datetime.now(timezone('Asia/Bangkok'))
    last_run_times = {}  # Track last run time for each schedule_id
    last_transition_check = datetime.now(timezone('Asia/Bangkok')) - timedelta(seconds=60)
    last_s4_tick = datetime.now(timezone('Asia/Bangkok')) - timedelta(minutes=5)
    last_dedupe_cleanup = datetime.now(timezone('Asia/Bangkok')) - timedelta(hours=DEDUPE_CLEANUP_INTERVAL_HOURS)

    while True:
        try:
            now = datetime.now(timezone('Asia/Bangkok'))
            current_day = now.strftime("%A").lower()
            current_time_str = now.strftime("%H:%M")
            current_datetime = now.strftime("%Y-%m-%d %H:%M")

            # Refresh config cache every 5 minutes
            if now >= cache_expiry or not config_cache:
                db = get_db_connection()
                cursor = db.cursor()
                try:
                    cursor.execute("SELECT id, schedule_time, schedule_day, purchase_amount, exchange_mode, binance_amount, okx_amount FROM schedules WHERE is_active = 1")
                except Exception:
                    cursor.execute("SELECT id, schedule_time, schedule_day, purchase_amount FROM schedules WHERE is_active = 1")
                config_cache = cursor.fetchall()
                cursor.close()
                db.close()
                cache_expiry = now + timedelta(minutes=5)
                logging.info(f"Config cache refreshed - Found {len(config_cache)} active schedules")

            if not config_cache:
                logging.warning("No active schedules found.")
                await asyncio.sleep(10)
                continue

            # Check CDC transitions periodically (~60s)
            try:
                if (now - last_transition_check).total_seconds() >= 60:
                    await check_cdc_transition_and_act(now)
                    last_transition_check = now
            except Exception as e:
                logging.error(f"CDC transition check error: {e}")

            try:
                if (now - last_s4_tick).total_seconds() >= 300:
                    await run_s4_tick(now)
                    last_s4_tick = now
            except Exception as e:
                logging.error(f"S4 tick error: {e}")

            # Periodic cleanup of DB dedupe table (safe, best-effort)
            try:
                if DEDUPE_CLEANUP_ENABLED and (now - last_dedupe_cleanup).total_seconds() >= DEDUPE_CLEANUP_INTERVAL_HOURS * 3600:
                    cleanup_action_dedupe()
                    last_dedupe_cleanup = now
            except Exception as e:
                logging.warning(f"Dedupe cleanup error: {e}")

            # Daily heartbeat (08:00–08:15 Asia/Bangkok) - deduped via action_dedupe
            try:
                maybe_send_daily_heartbeat(now)
            except Exception as e:
                logging.warning("Heartbeat error: %s", e)

            for schedule in config_cache:
                # Support both schema shapes
                if len(schedule) >= 7:
                    schedule_id, schedule_time_str, schedule_day, purchase_amount, exchange_mode, binance_amount, okx_amount = schedule
                else:
                    schedule_id, schedule_time_str, schedule_day, purchase_amount = schedule
                    exchange_mode = 'global'; binance_amount = None; okx_amount = None

                # Validate schedule
                schedule_days = [d.strip().lower() for d in schedule_day.split(",")]
                validate_schedule(schedule_time_str, schedule_days)

                logging.debug(f"[CHECK] Schedule ID: {schedule_id} | Now: {current_day} {current_time_str} | Config: {schedule_days} {schedule_time_str}")
                time_diff = abs((datetime.strptime(current_time_str, "%H:%M") - 
                                 datetime.strptime(schedule_time_str, "%H:%M")).total_seconds())
                logging.debug(f"Time diff for Schedule ID {schedule_id}: {time_diff} seconds")

                # Check if this schedule should run
                schedule_match_window_sec = max(int(float(os.getenv('SCHEDULE_MATCH_WINDOW_SEC', '59'))), 15)
                if current_day in schedule_days and time_diff <= schedule_match_window_sec:
                    last_run = last_run_times.get(schedule_id)
                    current_schedule_time = f"{now.strftime('%Y-%m-%d')} {schedule_time_str}"
                    if last_run != current_schedule_time:
                        logging.info(f"⏰ Matched schedule ID {schedule_id} at {current_time_str}. Evaluating schedule mode...")
                        if exchange_mode in ('global', None):
                            await gate_weekly_dca(now, schedule_id, float(purchase_amount))
                        else:
                            await gate_weekly_dca(now, schedule_id, float(purchase_amount), {
                                'exchange_mode': exchange_mode,
                                'binance_amount': float(binance_amount or 0),
                                'okx_amount': float(okx_amount or 0),
                            })
                        last_run_times[schedule_id] = current_schedule_time
                        await asyncio.sleep(60 - (now.second % 60))  # Wait until next minute
                    else:
                        logging.debug(f"⏳ Schedule ID {schedule_id} already executed at {schedule_time_str} today.")
                else:
                    logging.debug(f"Schedule ID {schedule_id} not matched: day={current_day} in {schedule_days}, time_diff={time_diff}")

            await asyncio.sleep(10)

        except Exception as e:
            logging.error(f"Error in scheduler loop: {e}")
            send_line_message(f"Scheduler error: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    health_server = None
    scheduler_lock_conn = None
    try:
        # Ensure strategy tables exist (best-effort)
        try:
            db = get_db_connection(); cursor = db.cursor()
            cursor.execute("CREATE TABLE IF NOT EXISTS strategy_state (id INT PRIMARY KEY AUTO_INCREMENT, mode VARCHAR(32) NOT NULL, last_cdc_status ENUM('up','down') NULL, last_transition_at DATETIME NULL, reserve_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00, red_epoch_active TINYINT(1) NOT NULL DEFAULT 0, last_half_sell_at DATETIME NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, UNIQUE KEY uq_strategy_mode (mode))")
            cursor.execute("INSERT IGNORE INTO strategy_state (mode, last_cdc_status, reserve_usdt, red_epoch_active) VALUES ('cdc_dca_v1', NULL, 0.00, 0)")
            cursor.execute("CREATE TABLE IF NOT EXISTS sell_history (id INT PRIMARY KEY AUTO_INCREMENT, sell_time DATETIME NOT NULL, symbol VARCHAR(16) NOT NULL DEFAULT 'BTCUSDT', btc_quantity DECIMAL(18,8) NOT NULL, usdt_received DECIMAL(18,2) NOT NULL, price DECIMAL(18,2) NOT NULL, order_id BIGINT, schedule_id INT NULL, note VARCHAR(255) NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, INDEX idx_sell_time (sell_time), UNIQUE KEY uq_sell_order (order_id))")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_rotation_log (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    executed_at DATETIME NOT NULL,
                    strategy_mode VARCHAR(32) NOT NULL,
                    from_asset VARCHAR(16) NOT NULL,
                    to_asset VARCHAR(16) NOT NULL,
                    notional_usd DECIMAL(18,2) NOT NULL,
                    cdc_status VARCHAR(16) NULL,
                    delta_pct DECIMAL(9,4) NULL,
                    reason VARCHAR(64) NULL,
                    metadata_json TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_mode_time (strategy_mode, executed_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8;
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS s4_neutral_zone_eod (
                    date DATE PRIMARY KEY,
                    ratio_close DECIMAL(12,6) NULL,
                    ema12 DECIMAL(12,6) NULL,
                    ema26 DECIMAL(12,6) NULL,
                    ema_gap_pct DECIMAL(8,4) NULL,
                    slope_pct DECIMAL(8,4) NULL,
                    state VARCHAR(20) NULL,
                    cdc_status VARCHAR(10) NULL,
                    active_asset VARCHAR(10) NULL,
                    eod_lag_days INT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8;
            """)
            try:
                cursor.execute("ALTER TABLE s4_neutral_zone_eod ADD COLUMN eod_lag_days INT NULL")
            except Exception:
                pass
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS s4_neutral_zone_state_changes (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    ts DATETIME NOT NULL,
                    old_state VARCHAR(20) NULL,
                    new_state VARCHAR(20) NULL,
                    ema_gap_pct DECIMAL(8,4) NULL,
                    slope_pct DECIMAL(8,4) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_neutral_state_change_ts (ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8;
            """)
            db.commit(); cursor.close(); db.close()
        except Exception as _e:
            logging.warning(f"Strategy tables ensure failed (will rely on app migration): {_e}")

        # Optional: ensure action dedupe infra
        ensure_action_dedupe_table()
        # Start health check server
        health_server = start_health_check()
        
        if health_server:
            # Optional: single-instance scheduler lock
            scheduler_lock_conn = acquire_scheduler_lock()
            if SCHEDULER_DB_LOCK_ENABLED and scheduler_lock_conn is None:
                logging.error("Exiting without starting scheduler due to lock contention.")
                sys.exit(0)
            logging.info("🚀 Starting BTC DCA scheduler...")
            send_line_message("🚀 BTC DCA Scheduler Started")
            asyncio.run(run_loop_scheduler())
        else:
            logging.error("Failed to start health check server, exiting...")
            exit(1)
            
    except KeyboardInterrupt:
        logging.info("Scheduler stopped by user")
        send_line_message("🛑 BTC DCA Scheduler Stopped")
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        send_line_message(f"💥 Scheduler fatal error: {e}")
        raise
    finally:
        if health_server:
            health_server.shutdown()
            logging.info("Health check server shutdown")
        release_scheduler_lock(scheduler_lock_conn)
