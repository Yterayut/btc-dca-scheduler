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
from notify import send_line_message

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

# Binance client setup
client = Client(required_env_vars['BINANCE_API_KEY'], required_env_vars['BINANCE_API_SECRET'])

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

        balance = client.get_asset_balance(asset='USDT')
        available_usdt = float(balance['free'])
        logging.info(f"Available USDT balance: {available_usdt}")
        if available_usdt < purchase_amount:
            raise ValueError(f"Insufficient USDT balance: {available_usdt} < {purchase_amount}")

        symbol = 'BTCUSDT'
        symbol_info = client.get_symbol_info(symbol)
        min_notional = float([f['minNotional'] for f in symbol_info['filters'] if f['filterType'] == 'NOTIONAL'][0])
        logging.info(f"Minimum notional for {symbol}: {min_notional}")
        if purchase_amount < min_notional:
            raise ValueError(f"Amount ({purchase_amount}) less than minimum notional ({min_notional})")

        order = None  # Initialize order to None
        try:
            @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
            def place_order_with_retry():
                return client.order_market_buy(symbol=symbol, quoteOrderQty=purchase_amount)
            
            order = place_order_with_retry()
            logging.info(f"Initial market order response: {order}")
        except BinanceAPIException as e:
            logging.error(f"Binance API exception during market buy order: {e.status_code=} {e.code=} {e.message=} {e.response=}")
            raise  # Re-raise to be caught by the main exception handler

        order_id_for_get_order = order['orderId']
        try:
            logging.info(f"Fetching order details for orderId {order_id_for_get_order} using client.get_order()...")
            order_details = client.get_order(symbol=symbol, orderId=order_id_for_get_order)
            logging.info(f"Full order details from client.get_order(): {order_details}")
        except BinanceAPIException as e:
            logging.error(f"Binance API exception during get_order for orderId {order_id_for_get_order}: {e.status_code=} {e.code=} {e.message=} {e.response=}")
            # If get_order fails, we might not have order_details, so we can't proceed with its parsing
            raise # Re-raise to be caught by the main exception handler

        order_status_from_details = order_details['status']
        logging.info(f"Order status from client.get_order(): {order_status_from_details}")
        if order_status_from_details != 'FILLED':
            # This specific error message is for when the order is found but not 'FILLED'
            error_msg = f"Order (id: {order_id_for_get_order}) status is '{order_status_from_details}', not 'FILLED', based on client.get_order(). Full details: {order_details}"
            logging.error(error_msg)
            raise ValueError(error_msg)

        raw_executed_qty = order_details['executedQty']
        raw_cummulative_quote_qty = order_details['cummulativeQuoteQty']
        logging.info(f"Received raw order details from client.get_order() (orderId {order_id_for_get_order}): executedQty='{raw_executed_qty}', cummulativeQuoteQty='{raw_cummulative_quote_qty}'")

        try:
            executed_qty = float(raw_executed_qty)
            cummulative_quote_qty = float(raw_cummulative_quote_qty)
        except ValueError as e:
            # This error occurs if the string to float conversion fails.
            error_msg = f"Could not convert executedQty ('{raw_executed_qty}') or cummulativeQuoteQty ('{raw_cummulative_quote_qty}') to float for orderId {order_id_for_get_order}. Error: {e}"
            logging.error(error_msg)
            raise ValueError(error_msg)


        if executed_qty <= 0:
            # This is the check for anomalous 'FILLED' orders
            error_msg = f"FILLED order (id: {order_id_for_get_order}) has non-positive executed quantity: {executed_qty} based on client.get_order(). Full details: {order_details}"
            logging.error(error_msg)
            raise ValueError(error_msg)

        # Calculate price and quantity from order_details
        filled_price = cummulative_quote_qty / executed_qty
        filled_quantity = executed_qty 
        # order_id_from_details is the same as order_id_for_get_order at this point if no prior exception.
        order_id_from_details = order_details['orderId'] 
        logging.info(f"Calculated from client.get_order() details (orderId {order_id_from_details}): filled_price={filled_price}, filled_quantity={filled_quantity}")


        current_time = now.strftime("%Y-%m-%d %H:%M:%S")
        formatted_quantity = f"{filled_quantity:.8f}"

        message = (
<<<<<<< HEAD
            f"✅ DCA BTC Success (Schedule ID: {schedule_id})\n"
=======
            f"✅ DCA BTC Success (details from client.get_order())\n"
>>>>>>> 40d1fdef18c34c513b667ba427b04a661ba6c268
            f"{current_time} - Purchased {purchase_amount} USDT\n"
            f"BUY: {formatted_quantity} BTC, Price: ฿{filled_price:.2f}\n"
            f"Order ID: {order_id_from_details}"
        )

        logging.info(f"Success message: {message}")
        print(message)
        send_line_message(message)

        cursor.execute("""
<<<<<<< HEAD
            INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id)
            VALUES (NOW(), %s, %s, %s, %s, %s)
        """, (purchase_amount, filled_quantity, filled_price, order['orderId'], schedule_id))
=======
            INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id)
            VALUES (NOW(), %s, %s, %s, %s)
        """, (purchase_amount, filled_quantity, filled_price, order_id_from_details))
>>>>>>> 40d1fdef18c34c513b667ba427b04a661ba6c268
        db.commit()
        logging.info(f"Purchase record for order ID {order_id_from_details} saved to database.")

<<<<<<< HEAD
    except Exception as e:
        error_message = f"Error in purchase_btc (Schedule ID: {schedule_id}): {e}"
        logging.error(error_message)
=======
    except BinanceAPIException as e: # This will catch BinanceAPIExceptions re-raised from inner blocks
        # The specific logging for BinanceAPIException already happened in the inner try-except blocks
        error_message = f"Binance API Error in purchase_btc (orderId: {order.get('orderId', 'N/A') if order else 'N/A'}): Code={e.code}, Message='{e.message}'"
        logging.error(error_message) # Log again with order context if available
>>>>>>> 40d1fdef18c34c513b667ba427b04a661ba6c268
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
        if order and 'orderId' in order:
            current_order_id = order['orderId']
        elif 'order_id_for_get_order' in locals() and order_id_for_get_order:
            current_order_id = order_id_for_get_order
        
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

# Main scheduler loop
async def run_loop_scheduler():
    """Run the DCA scheduler to purchase BTC based on multiple schedules."""
    print("⏳ Real-time BTC DCA scheduler started...")
    config_cache = []
    cache_expiry = datetime.now(timezone('Asia/Bangkok'))
    last_run_times = {}  # Track last run time for each schedule_id

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

            for schedule in config_cache:
                schedule_id, schedule_time_str, schedule_day, purchase_amount = schedule

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
                        logging.info(f"⏰ Matched schedule ID {schedule_id} at {current_time_str}, executing purchase...")
                        await purchase_btc(now, purchase_amount, schedule_id)
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
