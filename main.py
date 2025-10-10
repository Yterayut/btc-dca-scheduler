import os
import logging
import MySQLdb
import asyncio
import threading
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from binance.client import Client
from binance.exceptions import BinanceAPIException
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pytz import timezone
from tenacity import retry, stop_after_attempt, wait_exponential
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
)
from exchanges.factory import get_adapter

# Load environment variables
load_dotenv()

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

client = Client(required_env_vars['BINANCE_API_KEY'], required_env_vars['BINANCE_API_SECRET'], testnet=USE_TESTNET)

def is_dry_run() -> bool:
    return DRY_RUN

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('btc_purchase_log.log'),
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
async def purchase_btc(now: datetime, purchase_amount: float, schedule_id: int) -> None:
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
        logging.info(f"Calculated from client.get_order() details (orderId {order_id_from_details}): filled_price={filled_price}, filled_quantity={filled_quantity}")


        current_time = now.strftime("%Y-%m-%d %H:%M:%S")
        formatted_quantity = f"{filled_quantity:.8f}"

        message = (
            f"✅ DCA BTC Success (Schedule ID: {schedule_id})\n"
            f"{current_time} - Purchased {purchase_amount} USDT\n"
            f"BUY: {formatted_quantity} BTC, Price: ฿{filled_price:.2f}\n"
            f"Order ID: {order_id_from_details}"
        )

        logging.info(f"Success message: {message}")
        print(message)
        send_line_message(message)

        cursor.execute(
            """
            INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s)
            """,
            (purchase_amount, filled_quantity, filled_price, order_id_from_details, schedule_id, ex)
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

def increment_reserve(amount: float, *, reason: str | None = None, note: str | None = None) -> float:
    """Increase global reserve_usdt by amount and return new value."""
    try:
        amt = float(amount or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if amt <= 0:
        return 0.0
    try:
        db = get_db_connection(); cursor = db.cursor()
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
        db.commit()
        return val
    except Exception:
        return 0.0
    finally:
        try:
            cursor.close(); db.close()
        except Exception:
            pass

def increment_reserve_exchange(exchange: str, amount: float, *, reason: str | None = None, note: str | None = None) -> float:
    """Increase per-exchange reserve and return new value."""
    try:
        amt = float(amount or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if amt <= 0:
        return 0.0
    try:
        db = get_db_connection(); cursor = db.cursor()
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
        db.commit(); cursor.close(); db.close()
        return val
    except Exception:
        return 0.0

def purchase_on_exchange(now: datetime, exchange: str, amount: float, schedule_id: int | None) -> dict:
    """Place market buy on a specific exchange using adapter; record history; notify."""
    try:
        adapter = get_adapter(exchange, testnet=USE_TESTNET, dry_run=is_dry_run())
        if exchange == 'okx':
            from exchanges.okx import OkxAdapter
            st = load_strategy_state()
            maxu = float(st.get('okx_max_usdt', 0) or 0)
            adapter = OkxAdapter(testnet=USE_TESTNET, dry_run=is_dry_run(), max_usdt=maxu if maxu > 0 else None)
        res = adapter.place_market_buy_quote(amount)
        ex_qty = float(res.executed_qty); cqq = float(res.cummulative_quote_qty); avg = float(res.avg_price)
        order_id = res.order_id
        if ex_qty <= 0 or cqq <= 0:
            raise ValueError('not filled')
        db = get_db_connection(); cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s)
            """,
            (cqq, ex_qty, avg, order_id, schedule_id, exchange)
        ); db.commit(); cursor.close(); db.close()
        try:
            notify_weekly_dca_buy({
                'usdt': cqq,
                'btc_qty': ex_qty,
                'price': avg,
                'schedule_id': schedule_id,
                'order_id': order_id,
                'exchange': exchange,
            })
        except Exception:
            pass
        return {'executed': True, 'exchange': exchange, 'qty': ex_qty, 'usdt': cqq, 'price': avg, 'order_id': order_id}
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
            return qty
        from math import floor
        return floor(qty / step) * step
    except Exception:
        return qty

def _execute_half_sell_for_exchange(now: datetime, exchange: str, pct: int, state: dict | None = None) -> dict:
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
            notify_half_sell_skipped({'reason': 'sell_percent=0', 'btc_free': 0, 'step': '-', 'min_notional': '-', 'pct': pct, 'exchange': ex})
            return {'skipped': True, 'reason': 'sell_percent_zero', 'exchange': ex, 'pct': pct}

        balance = adapter.get_balance(asset='BTC')
        btc_free = float(balance.get('free') or 0)
        if btc_free <= 0:
            notify_half_sell_skipped({'reason': 'no balance', 'btc_free': btc_free, 'step': '-', 'min_notional': '-', 'pct': pct, 'exchange': ex})
            return {'skipped': True, 'reason': 'no_balance', 'exchange': ex, 'pct': pct}

        filters = get_symbol_filters('BTCUSDT', exchange=ex)
        step = float(filters['stepSize'])
        min_qty = float(filters['minQty'])
        min_notional = float(filters['minNotional'])

        sell_target = btc_free * (pct / 100.0)
        qty = adjust_qty_to_step(sell_target, step)
        if qty < min_qty:
            notify_half_sell_skipped({'reason': 'below minQty', 'btc_free': btc_free, 'step': step, 'min_notional': min_notional, 'pct': pct, 'exchange': ex})
            return {'skipped': True, 'reason': 'below_minQty', 'exchange': ex, 'pct': pct}

        price = float(adapter.get_price())
        notional = qty * price
        if notional < min_notional:
            notify_half_sell_skipped({'reason': 'below minNotional', 'btc_free': btc_free, 'step': step, 'min_notional': min_notional, 'pct': pct, 'exchange': ex})
            return {'skipped': True, 'reason': 'below_minNotional', 'exchange': ex, 'pct': pct}

        res = adapter.place_market_sell_qty(qty)
        order_id = res.order_id
        executed_qty = float(res.executed_qty)
        cummulative_quote_qty = float(res.cummulative_quote_qty)
        if executed_qty <= 0 or cummulative_quote_qty <= 0:
            raise ValueError('Sell order not filled or zero quantities')
        avg_price = cummulative_quote_qty / executed_qty if executed_qty else 0.0

        db = None; cursor = None
        try:
            db = get_db_connection()
            cursor = db.cursor()
            cursor.execute(
                """
                INSERT INTO sell_history (sell_time, symbol, btc_quantity, usdt_received, price, order_id, sell_percent, note, exchange)
                VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                ('BTCUSDT', executed_qty, cummulative_quote_qty, avg_price, order_id, pct, 'sell via CDC', ex)
            )
            db.commit()
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

        notify_half_sell_executed({
            'btc_qty': executed_qty,
            'price': avg_price,
            'usdt': cummulative_quote_qty,
            'order_id': order_id,
            'pct': pct,
            'exchange': ex,
        })
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

def execute_reserve_buy(now: datetime) -> dict:
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
            notify_reserve_buy_skipped_min_notional({'spend': spend, 'min_notional': min_notional, 'reserve': reserve})
            return {'skipped': True, 'reason': 'below_minNotional', 'spend': spend}

        # Execute via adapter (handles dry_run)
        res = adapter.place_market_buy_quote(spend)
        order_id = res.order_id
        executed_qty = float(res.executed_qty)
        cummulative_quote_qty = float(res.cummulative_quote_qty)
        if executed_qty <= 0 or cummulative_quote_qty <= 0:
            raise ValueError('Reserve buy not filled or zero quantities')
        avg_price = cummulative_quote_qty / executed_qty

        # Insert purchase history (schedule_id NULL)
        db = get_db_connection(); cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s)
            """,
            (cummulative_quote_qty, executed_qty, avg_price, order_id, None, ex)
        )
        # Decrease reserve by the amount spent and log
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
        db.commit(); cursor.close(); db.close()

        notify_reserve_buy_executed({
            'spend': cummulative_quote_qty,
            'btc_qty': executed_qty,
            'price': avg_price,
            'reserve_left': new_reserve,
            'order_id': order_id,
            'exchange': ex,
        })
        return {'executed': True, 'spend': cummulative_quote_qty, 'qty': executed_qty, 'price': avg_price, 'order_id': order_id}
    except Exception as e:
        logging.error(f"Reserve buy error: {e}")
        send_line_message(f"❌ Reserve buy error: {e}")
        return {'error': str(e)}

def execute_reserve_buy_exchange(now: datetime, exchange: str) -> dict:
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
            notify_reserve_buy_skipped_min_notional({'spend': spend, 'min_notional': min_notional, 'reserve': reserve})
            return {'skipped': True, 'reason': 'below_minNotional', 'exchange': exchange, 'spend': spend}
        res = adapter.place_market_buy_quote(spend)
        ex_qty = float(res.executed_qty); cqq = float(res.cummulative_quote_qty); avg = float(res.avg_price)
        if ex_qty <= 0 or cqq <= 0:
            raise ValueError('not filled')
        db = get_db_connection(); cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id, exchange)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s)
            """,
            (cqq, ex_qty, avg, res.order_id, None, exchange)
        )
        if exchange == 'binance':
            cursor.execute("UPDATE strategy_state SET reserve_binance_usdt = GREATEST(reserve_binance_usdt - %s, 0) WHERE mode='cdc_dca_v1'", (cqq,))
        else:
            cursor.execute("UPDATE strategy_state SET reserve_okx_usdt = GREATEST(reserve_okx_usdt - %s, 0) WHERE mode='cdc_dca_v1'", (cqq,))
        db.commit(); cursor.close(); db.close()
        notify_reserve_buy_executed({
            'spend': cqq,
            'btc_qty': ex_qty,
            'price': avg,
            'reserve_left': max(0.0, reserve - cqq),
            'order_id': res.order_id,
            'exchange': exchange,
        })
        return {'executed': True, 'exchange': exchange, 'spend': cqq, 'qty': ex_qty, 'price': avg}
    except Exception as e:
        logging.error(f"Reserve buy {exchange} error: {e}")
        send_line_message(f"❌ Reserve buy {exchange.upper()} error: {e}")
        return {'error': str(e), 'exchange': exchange}

async def gate_weekly_dca(now: datetime, schedule_id: int, amount: float, extra: dict | None = None) -> dict:
    """Gate weekly DCA by CDC status per schedule. Supports per-exchange and both modes."""
    state = load_strategy_state()
    mode = (extra or {}).get('exchange_mode') or 'global'
    bz_amt = float((extra or {}).get('binance_amount') or 0)
    okx_amt = float((extra or {}).get('okx_amount') or 0)
    if mode == 'global':
        if int(state.get('cdc_enabled', 1)) == 0:
            await purchase_btc(now, amount, schedule_id)
            return {'decision': 'buy', 'amount': amount, 'cdc': 'disabled', 'mode': mode}
        status = get_cdc_status_1d().get('status')
        if status == 'up':
            await purchase_btc(now, amount, schedule_id)
            return {'decision': 'buy', 'amount': amount, 'cdc': 'up', 'mode': mode}
        else:
            new_reserve = increment_reserve(amount)
            try:
                notify_weekly_dca_skipped(amount, new_reserve)
            except Exception:
                pass
            return {'decision': 'reserve', 'reserve_usdt': new_reserve, 'cdc': 'down', 'mode': mode}

    # non-global modes
    status = 'up' if int(state.get('cdc_enabled', 1)) == 0 else get_cdc_status_1d().get('status')
    results = []
    if mode in ('binance','both') and bz_amt > 0:
        if status == 'up':
            results.append(purchase_on_exchange(now, 'binance', bz_amt, schedule_id))
        else:
            rb = increment_reserve_exchange('binance', bz_amt)
            try:
                notify_weekly_dca_skipped_exchange('binance', bz_amt, rb)
            except Exception:
                pass
            results.append({'decision': 'reserve', 'exchange': 'binance', 'reserve': rb})
    if mode in ('okx','both') and okx_amt > 0:
        if status == 'up':
            results.append(purchase_on_exchange(now, 'okx', okx_amt, schedule_id))
        else:
            ro = increment_reserve_exchange('okx', okx_amt)
            try:
                notify_weekly_dca_skipped_exchange('okx', okx_amt, ro)
            except Exception:
                pass
            results.append({'decision': 'reserve', 'exchange': 'okx', 'reserve': ro})
    return {'mode': mode, 'cdc': status, 'results': results}

def check_cdc_transition_and_act(now: datetime) -> None:
    """Detect CDC transitions and execute actions (half-sell or reserve-buy)."""
    state = load_strategy_state()
    # Respect global toggle
    if int(state.get('cdc_enabled', 1)) == 0:
        return
    curr = get_cdc_status_1d().get('status')
    prev = state.get('last_cdc_status')
    if prev != curr:
        try:
            notify_cdc_transition(prev, curr)
        except Exception:
            pass
        # Transition actions
        if curr == 'down':
            # entering red
            if int(state.get('red_epoch_active') or 0) == 0:
                res = execute_half_sell(now)
                # Mark epoch active regardless of execution to avoid hammering
                save_strategy_state({'red_epoch_active': 1, 'last_half_sell_at': now.strftime('%Y-%m-%d %H:%M:%S')})
        elif curr == 'up':
            # leaving red -> attempt reserve buy (legacy + per-exchange)
            try:
                execute_reserve_buy(now)
            except Exception:
                pass
            try:
                execute_reserve_buy_exchange(now, 'binance')
            except Exception:
                pass
            try:
                execute_reserve_buy_exchange(now, 'okx')
            except Exception:
                pass
            save_strategy_state({'red_epoch_active': 0})

        save_strategy_state({'last_cdc_status': curr, 'last_transition_at': now.strftime('%Y-%m-%d %H:%M:%S')})
# Main scheduler loop
async def run_loop_scheduler():
    """Run the DCA scheduler to purchase BTC based on multiple schedules."""
    print("⏳ Real-time BTC DCA scheduler started...")
    config_cache = []
    cache_expiry = datetime.now(timezone('Asia/Bangkok'))
    last_run_times = {}  # Track last run time for each schedule_id
    last_transition_check = datetime.now(timezone('Asia/Bangkok')) - timedelta(seconds=60)

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
                    check_cdc_transition_and_act(now)
                    last_transition_check = now
            except Exception as e:
                logging.error(f"CDC transition check error: {e}")

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
