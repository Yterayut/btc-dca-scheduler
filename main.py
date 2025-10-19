import os
import json
import logging
from logging.handlers import RotatingFileHandler
import MySQLdb
import asyncio
import threading
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pytz import timezone, utc
from tenacity import retry, stop_after_attempt, wait_exponential
import requests
from collections.abc import Sequence
from notify import (
    send_line_message,
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
)
from exchanges.factory import get_adapter
from services.balance_service import fetch_balances
from strategies.base import StrategyActionType, ActionStatus, ActionResult, StrategyAction
from strategies.cdc import CdcDcaStrategy, WeeklyDcaDecisionInput, TransitionDecisionInput
from strategies.runtime import StrategyOrchestrator
from strategies.s4_utils import (
    plan_s4_rotation as _plan_s4_rotation,
    resolve_s4_target_allocations as _resolve_s4_target_allocations,
    fetch_okx_ratio_signal as _fetch_okx_ratio_signal,
)
from compliance import record_event as log_compliance_event
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from contextlib import contextmanager

# Load environment variables
load_dotenv()

try:
    from utils import get_btc_price, get_gold_price
except Exception:
    get_btc_price = None
    get_gold_price = None

# Validate environment variables
required_env_vars = {
    'DB_HOST': os.getenv('DB_HOST'),
    'DB_USER': os.getenv('DB_USER'),
    'DB_PASSWORD': os.getenv('DB_PASSWORD'),
    'DB_NAME': os.getenv('DB_NAME'),
    'BINANCE_API_KEY': os.getenv('BINANCE_API_KEY'),
    'BINANCE_API_SECRET': os.getenv('BINANCE_API_SECRET')
}
missing_vars = [key for key, value in required_env_vars.items() if value is None]
if missing_vars:
    raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

# Binance client setup (supports Testnet)
def _env_flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in ('1', 'true', 'yes', 'on')

USE_TESTNET = _env_flag('USE_BINANCE_TESTNET', False) or _env_flag('BINANCE_TESTNET', False) or _env_flag('OKX_TESTNET', False)
DRY_RUN = _env_flag('STRATEGY_DRY_RUN', False) or _env_flag('DRY_RUN', False)

client = Client(
    required_env_vars['BINANCE_API_KEY'],
    required_env_vars['BINANCE_API_SECRET'],
    testnet=USE_TESTNET,
    requests_params={'timeout': 15}
)

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
    if exchange.lower() == 'okx':
        cap = float(st.get('okx_max_usdt') or os.getenv('OKX_MAX_USDT') or 0.0)
    elif exchange.lower() == 'binance':
        cap = float(st.get('binance_max_usdt') or os.getenv('BINANCE_MAX_USDT') or 0.0)
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

# MySQL connection with retry
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_db_connection():
    """Connect to MySQL database with retry mechanism.

    Returns:
        MySQLdb.connection: Database connection object.
    """
    try:
        db = MySQLdb.connect(
            host=required_env_vars['DB_HOST'],
            user=required_env_vars['DB_USER'],
            passwd=required_env_vars['DB_PASSWORD'],
            db=required_env_vars['DB_NAME'],
            charset='utf8'
        )
        cursor = db.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        return db
    except MySQLdb.OperationalError as e:
        logging.error(f"Database connection error: {e}")
        raise

@contextmanager
def db_transaction():
    """Context manager for DB cursor with automatic commit/rollback."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        yield cursor, conn
        conn.commit()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
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
    """Return a raw strategy_state row as dict."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
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

    strategy_key = (strategy or 'unknown').strip().lower() or 'unknown'
    exchange_key = (exchange or 'unknown').strip().lower() or 'unknown'
    fee_type_key = 'sell' if fee_type == 'sell' else 'buy'
    asset_key = (fee_asset or ('USD' if fee_usd_val else 'UNKNOWN')).strip().upper() or 'UNKNOWN'

    now = datetime.utcnow()
    try:
        with db_transaction() as (cursor, _):
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
    setters = ["metadata_json=%s"]
    params = [json.dumps(metadata)]
    if extra:
        for key, value in extra.items():
            setters.append(f"{key}=%s")
            params.append(value)
    params.append(mode)
    try:
        with db_transaction() as (cursor, _):
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
    try:
        with db_transaction() as (cursor, _):
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
    active_asset = runtime.get('active_asset')
    if not active_asset:
        active_asset = 'BTC' if last_status == 'up' else 'GOLD'

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
            'order_id': order_id if order_id and order_id > 0 else None,
            'fee_usdt': fee_buy_usdt,
            'fee_asset': fee_buy_asset,
            'fee_asset_amount': fee_buy_asset_amount,
            'cdc_status': runtime.get('last_cdc_status') or last_status,
            'holdings': holdings_payload,
            'holdings_meta': holdings_meta,
        })
    except Exception:
        logging.debug("S4 DCA notification skipped", exc_info=True)

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
        error_message = f"Binance API Error in purchase_btc (orderId: {order.get('orderId', 'N/A') if order else 'N/A'}): Code={e.code}, Message='{e.message}'"
        logging.error(error_message) # Log again with order context if available
        print(error_message)
        send_line_message(error_message)
        raise # Important to re-raise to inform the scheduler loop
    except ValueError as e: # Catch ValueErrors, including from qty checks and float conversions
        # Specific logging for ValueError would have happened at the point of failure
        error_message = f"ValueError in purchase_btc (orderId: {order.get('orderId', 'N/A') if order else 'N/A'}): {e}"
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

def load_strategy_state():
    """Load CDC strategy state with graceful handling for legacy schemas."""
    defaults = {
        'last_cdc_status': None,
        'reserve_usdt': 0.0,
        'red_epoch_active': 0,
        'cdc_enabled': 1,
        'sell_percent': 50,
        'exchange': 'binance',
        'sell_percent_binance': 50,
        'sell_percent_okx': 50,
        'okx_max_usdt': 0.0,
        'binance_max_usdt': 0.0,
        'half_sell_policy': 'auto_proportional',
        'reserve_binance_usdt': 0.0,
        'reserve_okx_usdt': 0.0,
        'last_half_sell_at': None,
    }

    db = None
    cursor = None
    try:
        db = get_db_connection()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
        row = cursor.fetchone()
        if not row:
            return defaults
        columns = [desc[0] for desc in cursor.description]
        record = dict(zip(columns, row))
    except Exception as exc:
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

    sell_percent = _to_int(record.get('sell_percent'), defaults['sell_percent'])
    data = {
        'last_cdc_status': record.get('last_cdc_status', defaults['last_cdc_status']),
        'reserve_usdt': _to_float(record.get('reserve_usdt'), defaults['reserve_usdt']),
        'red_epoch_active': _to_int(record.get('red_epoch_active'), defaults['red_epoch_active']),
        'cdc_enabled': _to_int(record.get('cdc_enabled'), defaults['cdc_enabled']),
        'sell_percent': sell_percent,
        'exchange': (record.get('exchange') or defaults['exchange']),
        'sell_percent_binance': _to_int(record.get('sell_percent_binance'), sell_percent),
        'sell_percent_okx': _to_int(record.get('sell_percent_okx'), sell_percent),
        'okx_max_usdt': _to_float(record.get('okx_max_usdt'), defaults['okx_max_usdt']),
        'binance_max_usdt': _to_float(record.get('binance_max_usdt'), defaults['binance_max_usdt']),
        'half_sell_policy': str(record.get('half_sell_policy') or defaults['half_sell_policy']),
        'reserve_binance_usdt': _to_float(record.get('reserve_binance_usdt'), defaults['reserve_binance_usdt']),
        'reserve_okx_usdt': _to_float(record.get('reserve_okx_usdt'), defaults['reserve_okx_usdt']),
        'last_half_sell_at': record.get('last_half_sell_at', defaults['last_half_sell_at']),
    }
    return data

def save_strategy_state(patch: dict) -> None:
    """Upsert selected fields in strategy_state for mode='cdc_dca_v1'."""
    allowed = ['last_cdc_status', 'last_transition_at', 'reserve_usdt', 'red_epoch_active', 'last_half_sell_at']
    cols = ['mode']
    values = ['cdc_dca_v1']
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
        db = get_db_connection()
        cursor = db.cursor()
        placeholders = ', '.join(['%s'] * len(cols))
        update_clause = ', '.join(updates)
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
        price = float(adapter.get_price())
        depth_ok, depth_info = evaluate_depth_guard(adapter, exchange, price)
        if not depth_ok:
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
            notify_liquidity_blocked('dca_buy', payload)
            return {'skipped': True, 'reason': depth_info.get('reason', 'depth_guard'), 'exchange': exchange, 'detail': depth_info}
        twap_ok, twap_info = evaluate_twap_guard(adapter, exchange, price)
        if not twap_ok:
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
            notify_liquidity_blocked('dca_buy', payload)
            return {'skipped': True, 'reason': twap_info.get('reason', 'twap_guard'), 'exchange': exchange, 'detail': twap_info}
        cap_ok, cap_info = evaluate_notional_cap(exchange, amount, state)
        if not cap_ok:
            payload = {
                'exchange': exchange,
                'reason': 'notional_cap',
                'cap': cap_info.get('cap'),
                'attempt': cap_info.get('attempt'),
                'timestamp': now,
            }
            notify_liquidity_blocked('dca_buy', payload)
            return {'skipped': True, 'reason': 'notional_cap', 'exchange': exchange, 'detail': cap_info}

        res = adapter.place_market_buy_quote(amount)
        ex_qty = float(res.executed_qty);
        cqq = float(res.cummulative_quote_qty);
        avg = float(res.avg_price)
        order_id = res.order_id
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
                    order_id,
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
                'btc_qty': ex_qty,
                'price': avg,
                'schedule_id': schedule_id,
                'order_id': order_id,
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
                assets=("BTC", "USDT"),
                force_refresh=True,
            )
            notify_weekly_dca_buy(notify_payload)
        except Exception:
            pass

        record_fee_totals('cdc_weekly_dca', exchange, 'buy', fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)

        try:
            meta = {
                'schedule_id': schedule_id,
                'order_id': order_id,
            }
            if context:
                for key in ('request_id', 'dedupe_key', 'cdc_status'):
                    val = context.get(key)
                    if val:
                        meta[key] = val
            log_compliance_event(now, 'buy', exchange, cqq, ex_qty, avg, 0.0, metadata=meta)
            if cqq >= ANOMALY_NOTIONAL_THRESHOLD_USDT:
                notify_security_alert(
                    "High notional DCA buy",
                    {
                        'exchange': exchange.upper(),
                        'notional': f"{cqq:,.2f} USDT",
                        'threshold': f"{ANOMALY_NOTIONAL_THRESHOLD_USDT:,.2f} USDT",
                        'order_id': order_id,
                    },
                )
        except Exception:
            logging.debug("Compliance log skipped for buy", exc_info=True)
        result_payload = {'executed': True, 'exchange': exchange, 'qty': ex_qty, 'usdt': cqq, 'price': avg, 'order_id': order_id}
        if context:
            for key in ('request_id', 'dedupe_key', 'cdc_status'):
                val = context.get(key)
                if val:
                    result_payload[key] = val
        return result_payload
    except Exception as e:
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
    if ratio_snapshot and ratio_snapshot.get('status'):
        cdc_snapshot = ratio_snapshot
        signal_source = str(ratio_snapshot.get('source') or 'okx_ratio')
    else:
        cdc_snapshot = get_cdc_status_1d()
        signal_source = 'binance_cdc'

    cdc_status = str(cdc_snapshot.get('status') or 'down').lower()
    target_asset = 'BTC' if cdc_status == 'up' else 'GOLD'
    previous_status = runtime.get('last_cdc_status')
    runtime['last_cdc_status'] = cdc_status
    runtime['signal_source'] = signal_source
    runtime['last_signal_snapshot'] = {
        'status': cdc_status,
        'updated_at': cdc_snapshot.get('updated_at'),
        'ratio': cdc_snapshot.get('ratio'),
        'btc_close': cdc_snapshot.get('btc_close'),
        'gold_close': cdc_snapshot.get('gold_close'),
    }

    target_btc_pct, target_gold_pct = _resolve_s4_target_allocations(config, cdc_status)
    target_alloc = runtime.setdefault('target_allocations', {})
    target_alloc['btc_pct'] = target_btc_pct
    target_alloc['gold_pct'] = target_gold_pct

    min_flip_usd = _safe_float((config or {}).get('min_flip_usd'), 500.0)
    rotation_executed = False
    executed_meta = None
    rotation_amount_usd = 0.0
    rotation_plan = None

    if previous_status != cdc_status:
        rotation_plan = _plan_s4_rotation(
            current_btc_usd=usd_map.get('BTC', 0.0),
            current_gold_usd=usd_map.get('GOLD', 0.0),
            target_btc_pct=target_btc_pct,
            min_usd=max(min_flip_usd, 0.0),
        )

    if rotation_plan:
        from_asset = str(rotation_plan['from_asset'])
        to_asset = str(rotation_plan['to_asset'])
        plan_usd = float(rotation_plan['rotate_usd'])

        price_from = gold_price if from_asset == 'GOLD' else btc_price
        price_to = btc_price if to_asset == 'BTC' else gold_price
        symbol_from = gold_symbol if from_asset == 'GOLD' else btc_symbol
        symbol_to = btc_symbol if to_asset == 'BTC' else gold_symbol
        available_units = gold_units if from_asset == 'GOLD' else btc_units

        if adapter is not None and not dry_run_mode and price_from > 0 and price_to > 0 and available_units > 0:
            sell_units_target = min(available_units, plan_usd / price_from if price_from > 0 else 0.0)
            if sell_units_target <= 0:
                rotation_plan = None
            else:
                try:
                    sell_res = adapter.place_market_sell_qty_symbol(symbol_from, sell_units_target)
                    rotation_amount_usd = float(sell_res.cummulative_quote_qty or 0.0)
                    buy_res = adapter.place_market_buy_quote_symbol(symbol_to, rotation_amount_usd)
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
                    }
                    if adapter is not None and not dry_run_mode:
                        sell_fee_usdt = float(getattr(sell_res, 'fee_usd', 0.0) or 0.0)
                        sell_fee_asset = getattr(sell_res, 'fee_asset', None)
                        sell_fee_asset_amount = float(getattr(sell_res, 'fee_asset_amount', 0.0) or 0.0)
                        buy_fee_usdt = float(getattr(buy_res, 'fee_usd', 0.0) or 0.0)
                        buy_fee_asset = getattr(buy_res, 'fee_asset', None)
                        buy_fee_asset_amount = float(getattr(buy_res, 'fee_asset_amount', 0.0) or 0.0)
                        sell_symbol = symbol_from.replace('-', '')
                        buy_symbol = symbol_to.replace('-', '')
                        try:
                            with db_transaction() as (cursor, _):
                                cursor.execute(
                                    """
                                    INSERT INTO sell_history (sell_time, symbol, btc_quantity, usdt_received, price, order_id, sell_percent, note, exchange, fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                    """,
                                    (now, sell_symbol, float(sell_res.executed_qty or 0.0), float(sell_res.cummulative_quote_qty or 0.0), float(sell_res.avg_price or 0.0), sell_res.order_id, None, 's4 rotation sell', exchange_label.lower(), sell_fee_usdt or None, sell_fee_asset, sell_fee_asset_amount or None)
                                )
                                cursor.execute(
                                    """
                                    INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                    """,
                                    (now, float(buy_res.cummulative_quote_qty or 0.0), float(buy_res.executed_qty or 0.0), float(buy_res.avg_price or 0.0), buy_res.order_id, None, exchange_label.lower(), buy_fee_usdt or None, buy_fee_asset, buy_fee_asset_amount or None)
                                )
                        except Exception as exc:
                            logging.warning(f"S4 rotation logging error: {exc}")

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
            runtime['last_flip_at'] = now.isoformat()
            runtime['active_asset'] = target_asset
            runtime['last_action'] = {
                'result': 'rotation',
                'from': from_asset,
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
                'executed': executed_meta,
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
                    'amount_usd': round(rotation_amount_usd or plan_usd, 2),
                    'cdc_status': cdc_status,
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
        runtime['active_asset'] = target_asset
        runtime['last_action'] = {
            'result': 'noop',
            'total_usd': exposure['total_usd'],
            'dry_run': dry_run_mode or adapter is None,
            'target_btc_pct': target_btc_pct,
            'target_gold_pct': target_gold_pct,
        }

    runtime['exposure'] = exposure
    runtime['last_action_result'] = [{'status': 'EXECUTED' if rotation_executed else 'NOOP'}]

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
    state = load_strategy_state()
    # Respect global toggle
    if int(state.get('cdc_enabled', 1)) == 0:
        return
    curr = get_cdc_status_1d().get('status')
    prev = state.get('last_cdc_status')
    if prev == curr:
        return

    try:
        notify_cdc_transition(prev, curr)
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
                if current_day in schedule_days and time_diff <= 15:
                    last_run = last_run_times.get(schedule_id)
                    current_schedule_time = f"{now.strftime('%Y-%m-%d')} {schedule_time_str}"
                    if last_run != current_schedule_time:
                        logging.info(f"⏰ Matched schedule ID {schedule_id} at {current_time_str}. Applying CDC gate...")
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
            db.commit(); cursor.close(); db.close()
        except Exception as _e:
            logging.warning(f"Strategy tables ensure failed (will rely on app migration): {_e}")
        # Start health check server
        health_server = start_health_check()
        
        if health_server:
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
