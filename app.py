from flask import Flask, render_template, request, redirect, g, flash, jsonify
from flask_socketio import SocketIO, emit
import MySQLdb
import requests
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import logging
from apscheduler.schedulers.background import BackgroundScheduler
import functools
from contextlib import contextmanager
from math import isfinite
from pytz import timezone
from binance.client import Client
from notify import send_line_message_with_retry, notify_cdc_toggle
from exchanges.factory import get_adapter
from main import increment_reserve, increment_reserve_exchange

try:
    # Optional import for price/balance
    from utils import get_btc_price, get_client
except Exception:
    get_btc_price = None
    get_client = None

# ตั้งค่า logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

# โหลด environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
# Allow narrowing CORS origin in production via env CORS_ORIGIN (comma-separated allowed origins or '*')
_cors_origins = os.getenv('CORS_ORIGIN', '*')
if _cors_origins and _cors_origins != '*':
    _cors_origins = [o.strip() for o in _cors_origins.split(',') if o.strip()]
socketio = SocketIO(app, cors_allowed_origins=_cors_origins or "*")

# Serve a tiny in-memory favicon to avoid 404 noise
try:
    from flask import send_file
    import base64, io

    _FAVICON_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
    )

    @app.route('/favicon.ico')
    @app.route('/static/favicon.ico')
    def _serve_favicon():
        try:
            data = base64.b64decode(_FAVICON_PNG_B64)
            return send_file(io.BytesIO(data), mimetype='image/png')
        except Exception:
            # As a safe fallback, return 204 No Content
            from flask import Response
            return Response(status=204)
except Exception:
    pass

# Ensure API requests never return HTML error pages
@app.errorhandler(404)
def handle_404(e):
    try:
        if request.path.startswith('/api/'):
            return jsonify({'ok': False, 'error': 'not_found', 'path': request.path}), 404
    except Exception:
        pass
    return e

@app.errorhandler(405)
def handle_405(e):
    try:
        if request.path.startswith('/api/'):
            return jsonify({'ok': False, 'error': 'method_not_allowed', 'path': request.path}), 405
    except Exception:
        pass
    return e

# ตรวจสอบ environment variables
required_env_vars = ['DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME', 'LINE_CHANNEL_ACCESS_TOKEN']
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    logging.error(f"Missing environment variables: {', '.join(missing_vars)}")
    raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

# ตัวแปร global สำหรับติดตามสถานะ
last_scheduler_status = "Scheduler is running"
last_notify_time = None
NOTIFY_COOLDOWN = 300  # 5 นาที
migration_completed = False

# ====== CDC Action Zone Cache ======
_CDC_CACHE = {
    'data': None,     # last computed payload
    'expires': 0      # epoch seconds
}

def _ema(series, period: int):
    """Simple EMA implementation compatible enough for CDC.
    Returns list of EMA values with same length as input.
    """
    if not series:
        return []
    if period <= 1:
        return list(series)
    k = 2 / (period + 1)
    ema_vals = []
    prev = series[0]
    ema_vals.append(prev)
    for x in series[1:]:
        prev = (x * k) + (prev * (1 - k))
        ema_vals.append(prev)
    return ema_vals

def _last_true_index(flags):
    for i in range(len(flags) - 1, -1, -1):
        if flags[i]:
            return i
    return None

# ====== Custom Jinja2 filter ======
def floatformat(value, decimal_places=2):
    """Format ค่าทศนิยมเหมือน Django's floatformat"""
    try:
        return f"{float(value):.{decimal_places}f}"
    except (ValueError, TypeError) as e:
        logging.warning(f"floatformat error: value={value}, error={e}")
        return str(value)

app.jinja_env.filters['floatformat'] = floatformat

# ====== Database Connection Management ======
@contextmanager
def get_db_cursor():
    """Context manager สำหรับจัดการ database connection"""
    db = None
    cursor = None
    try:
        db = MySQLdb.connect(
            host=os.getenv('DB_HOST'),
            user=os.getenv('DB_USER'),
            passwd=os.getenv('DB_PASSWORD'),
            db=os.getenv('DB_NAME'),
            charset='utf8',
            autocommit=False
        )
        cursor = db.cursor()
        yield cursor, db
    except MySQLdb.Error as e:
        if db:
            db.rollback()
        logging.error(f"Database error: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if db:
            db.close()

def get_db_connection():
    """Legacy function สำหรับ backward compatibility"""
    try:
        if 'db' not in g:
            g.db = MySQLdb.connect(
                host=os.getenv('DB_HOST'),
                user=os.getenv('DB_USER'),
                passwd=os.getenv('DB_PASSWORD'),
                db=os.getenv('DB_NAME'),
                charset='utf8'
            )
            logging.debug("New database connection established")
        else:
            g.db.ping(reconnect=True)
            logging.debug("Database connection reused")
        return g.db
    except MySQLdb.OperationalError as e:
        logging.error(f"Database connection error: {e}")
        raise

@app.teardown_appcontext
def close_db_connection(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()
        logging.debug("Database connection closed")

# ====== Error Handler Decorator ======
def handle_db_errors(f):
    """Decorator สำหรับจัดการ database errors"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except MySQLdb.IntegrityError as e:
            logging.error(f"Database integrity error in {f.__name__}: {e}")
            flash("Database constraint error. Please check your data.", 'error')
            return redirect('/')
        except MySQLdb.Error as e:
            logging.error(f"Database error in {f.__name__}: {e}")
            flash("Database error occurred.", 'error')
            return redirect('/')
        except Exception as e:
            logging.error(f"Unexpected error in {f.__name__}: {e}")
            flash(f"Unexpected error: {str(e)}", 'error')
            return redirect('/')
    return wrapper

# ====== Line Notify Functions ======
def send_line_notify(message):
    """ส่งข้อความผ่าน Line Notify"""
    try:
        url = 'https://notify-api.line.me/api/notify'
        token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
        headers = {'Authorization': f'Bearer {token}'}
        data = {'message': message}
        
        response = requests.post(url, headers=headers, data=data, timeout=10)
        success = response.status_code == 200
        
        if success:
            logging.info("Line Notify sent successfully")
        else:
            logging.error(f"Failed to send Line Notify: {response.status_code}")
            
        return success
    except requests.RequestException as e:
        logging.error(f"Line Notify request error: {e}")
        return False
    except Exception as e:
        logging.error(f"Line Notify unexpected error: {e}")
        return False

def check_scheduler_status():
    """ตรวจสอบสถานะ Scheduler และแจ้งเตือน"""
    global last_scheduler_status, last_notify_time
    
    try:
        health_check_port = os.getenv('HEALTH_CHECK_PORT', '8001')
        response = requests.get(f'http://localhost:{health_check_port}', timeout=5)
        current_status = response.text if response.status_code == 200 else 'Scheduler is not responding'
    except requests.RequestException:
        current_status = 'Scheduler is not responding'

    # บันทึกการเปลี่ยนสถานะ
    if current_status != last_scheduler_status:
        logging.info(f"Scheduler status changed: {last_scheduler_status} -> {current_status}")
        last_scheduler_status = current_status

    # แจ้งเตือนถ้า scheduler ไม่ตอบสนอง
    if current_status == 'Scheduler is not responding':
        current_time = datetime.now()
        if last_notify_time is None or (current_time - last_notify_time).total_seconds() >= NOTIFY_COOLDOWN:
            timestamp = current_time.strftime('%Y-%m-%d %H:%M:%S')
            message = f"⚠️ Scheduler Alert: Not responding at {timestamp}"
            if send_line_notify(message):
                last_notify_time = current_time

# ====== Background Tasks ======
def cleanup_old_logs():
    """ลบ log เก่า"""
    try:
        log_file = 'app.log'
        if os.path.exists(log_file):
            # Keep only last 10MB of logs
            max_size = 10 * 1024 * 1024  # 10MB
            if os.path.getsize(log_file) > max_size:
                with open(log_file, 'rb') as f:
                    f.seek(-max_size, 2)  # Seek to last 10MB
                    data = f.read()
                
                with open(log_file, 'wb') as f:
                    f.write(data)
                    
                logging.info("Log file trimmed to 10MB")
    except Exception as e:
        logging.error(f"Error cleaning up logs: {e}")

def update_cache_schedules():
    """อัปเดต cache ของ schedules"""
    try:
        # Simple cache clear for now
        logging.debug("Cache refresh triggered")
    except Exception as e:
        logging.error(f"Error updating schedule cache: {e}")

# ====== Data Migration ======
def migrate_data_if_needed():
    """ตรวจสอบและ migrate ข้อมูลถ้าจำเป็น"""
    global migration_completed
    
    if migration_completed:
        return True
    
    try:
        with get_db_cursor() as (cursor, db):
            # 1. ตรวจสอบและสร้างตาราง schedules
            cursor.execute("SHOW TABLES LIKE 'schedules'")
            if not cursor.fetchone():
                logging.info("Creating schedules table...")
                cursor.execute("""
                    CREATE TABLE schedules (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        schedule_time VARCHAR(5) NOT NULL,
                        schedule_day VARCHAR(255) NOT NULL,
                        purchase_amount DECIMAL(10,2) NOT NULL,
                        is_active TINYINT(1) DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                """)
                db.commit()
                logging.info("Schedules table created")

            # 2. ตรวจสอบข้อมูลใน schedules
            cursor.execute("SELECT COUNT(*) FROM schedules")
            if cursor.fetchone()[0] == 0:
                logging.info("Migrating data...")
                
                # Migrate จาก config
                cursor.execute("SHOW TABLES LIKE 'config'")
                if cursor.fetchone():
                    cursor.execute("SELECT * FROM config WHERE id = 1")
                    config_data = cursor.fetchone()
                    if config_data:
                        _, purchase_amount, schedule_time, schedule_day = config_data
                        time_str = schedule_time.strftime('%H:%M') if hasattr(schedule_time, 'strftime') else str(schedule_time)
                        cursor.execute("""
                            INSERT INTO schedules (id, schedule_time, schedule_day, purchase_amount, is_active)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (1, time_str, schedule_day, purchase_amount, 1))

                # เพิ่ม schedules ที่หายไป
                cursor.execute("SHOW TABLES LIKE 'purchase_history'")
                if cursor.fetchone():
                    cursor.execute("""
                        SELECT DISTINCT schedule_id, usdt_amount 
                        FROM purchase_history 
                        WHERE schedule_id IS NOT NULL 
                        AND schedule_id NOT IN (SELECT id FROM schedules WHERE 1=1)
                        ORDER BY schedule_id
                    """)
                    
                    missing_schedules = cursor.fetchall()
                    for schedule_id, amount in missing_schedules:
                        if schedule_id == 2:
                            cursor.execute("""
                                INSERT INTO schedules (id, schedule_time, schedule_day, purchase_amount, is_active)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (2, '08:30', 'wednesday', amount, 1))
                        elif schedule_id == 3:
                            cursor.execute("""
                                INSERT INTO schedules (id, schedule_time, schedule_day, purchase_amount, is_active)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (3, '07:00', 'monday', amount, 1))
                        else:
                            cursor.execute("""
                                INSERT INTO schedules (id, schedule_time, schedule_day, purchase_amount, is_active)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (schedule_id, '12:00', 'monday', amount, 0))

                db.commit()
                logging.info("Data migration completed")

            # 3. ตรวจสอบ purchase_history table
            cursor.execute("SHOW TABLES LIKE 'purchase_history'")
            if not cursor.fetchone():
                logging.info("Creating purchase_history table...")
                cursor.execute("""
                    CREATE TABLE purchase_history (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        purchase_time DATETIME,
                        usdt_amount DECIMAL(10,2),
                        btc_quantity DECIMAL(18,8),
                        btc_price DECIMAL(18,2),
                        order_id BIGINT,
                        schedule_id INT,
                        INDEX idx_schedule_id (schedule_id),
                        INDEX idx_purchase_time (purchase_time)
                    )
                """)
                db.commit()

            # 3.1 ตรวจสอบ binance_trades table
            cursor.execute("SHOW TABLES LIKE 'binance_trades'")
            if not cursor.fetchone():
                logging.info("Creating binance_trades table...")
                cursor.execute(
                    """
                    CREATE TABLE binance_trades (
                        trade_id BIGINT PRIMARY KEY,
                        symbol VARCHAR(20) NOT NULL,
                        order_id BIGINT,
                        price DECIMAL(18,8),
                        qty DECIMAL(18,8),
                        quote_qty DECIMAL(18,8),
                        commission DECIMAL(18,8),
                        commission_asset VARCHAR(10),
                        is_buyer TINYINT(1),
                        is_maker TINYINT(1),
                        is_best_match TINYINT(1),
                        trade_time DATETIME,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_symbol_time (symbol, trade_time),
                        INDEX idx_order_id (order_id)
                    )
                    """
                )
                db.commit()
                logging.info("binance_trades table created")

            # 3.2 ตรวจสอบ strategy_state table
            cursor.execute("SHOW TABLES LIKE 'strategy_state'")
            if not cursor.fetchone():
                logging.info("Creating strategy_state table...")
                cursor.execute(
                    """
                    CREATE TABLE strategy_state (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        mode VARCHAR(32) NOT NULL,
                        last_cdc_status ENUM('up','down') NULL,
                        last_transition_at DATETIME NULL,
                        reserve_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                        red_epoch_active TINYINT(1) NOT NULL DEFAULT 0,
                        last_half_sell_at DATETIME NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uq_strategy_mode (mode)
                    )
                    """
                )
                db.commit()
                logging.info("strategy_state table created")

                # Seed default row for CDC strategy
                cursor.execute(
                    """
                    INSERT IGNORE INTO strategy_state (mode, last_cdc_status, reserve_usdt, red_epoch_active)
                    VALUES ('cdc_dca_v1', NULL, 0.00, 0)
                    """
                )
                db.commit()

            # 3.3 ตรวจสอบ sell_history table
            cursor.execute("SHOW TABLES LIKE 'sell_history'")
            if not cursor.fetchone():
                logging.info("Creating sell_history table...")
                cursor.execute(
                    """
                    CREATE TABLE sell_history (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        sell_time DATETIME NOT NULL,
                        symbol VARCHAR(16) NOT NULL DEFAULT 'BTCUSDT',
                        btc_quantity DECIMAL(18,8) NOT NULL,
                        usdt_received DECIMAL(18,2) NOT NULL,
                        price DECIMAL(18,2) NOT NULL,
                        order_id BIGINT,
                        schedule_id INT NULL,
                        note VARCHAR(255) NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_sell_time (sell_time),
                        UNIQUE KEY uq_sell_order (order_id)
                    )
                    """
                )
                db.commit()
                logging.info("sell_history table created")

            # 3.4 ตรวจสอบคอลัมน์ cdc_enabled ใน strategy_state
            try:
                cursor.execute("""
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='cdc_enabled'
                """, (os.getenv('DB_NAME'),))
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column cdc_enabled to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN cdc_enabled TINYINT(1) NOT NULL DEFAULT 1 AFTER mode")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure cdc_enabled column: {e}")

            # 3.5 ตรวจสอบ reserve_log table
            cursor.execute("SHOW TABLES LIKE 'reserve_log'")
            if not cursor.fetchone():
                logging.info("Creating reserve_log table...")
                cursor.execute(
                    """
                    CREATE TABLE reserve_log (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        event_time DATETIME NOT NULL,
                        change_usdt DECIMAL(18,2) NOT NULL,
                        reserve_after DECIMAL(18,2) NOT NULL,
                        reason VARCHAR(32) NOT NULL,
                        note VARCHAR(255) NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_event_time (event_time)
                    )
                    """
                )
                db.commit()
                logging.info("reserve_log table created")

            # 3.6 เพิ่มคอลัมน์ sell_percent ใน strategy_state ถ้ายังไม่มี
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='sell_percent'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column sell_percent (default 50) to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN sell_percent TINYINT NOT NULL DEFAULT 50 AFTER red_epoch_active")
                    db.commit()
                    cursor.execute("UPDATE strategy_state SET sell_percent = 50 WHERE mode='cdc_dca_v1'")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure sell_percent column: {e}")

            # 3.6.1 เพิ่มคอลัมน์ sell_percent แยกตาม exchange ถ้ายังไม่มี
            try:
                # sell_percent_binance
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='sell_percent_binance'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    logging.info("Adding column sell_percent_binance (default 50) to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN sell_percent_binance TINYINT NOT NULL DEFAULT 50 AFTER sell_percent")
                    db.commit()
                    try:
                        cursor.execute("UPDATE strategy_state SET sell_percent_binance = sell_percent WHERE mode='cdc_dca_v1'")
                        db.commit()
                    except Exception:
                        pass

                # sell_percent_okx
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='sell_percent_okx'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    logging.info("Adding column sell_percent_okx (default 50) to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN sell_percent_okx TINYINT NOT NULL DEFAULT 50 AFTER sell_percent_binance")
                    db.commit()
                    try:
                        cursor.execute("UPDATE strategy_state SET sell_percent_okx = sell_percent WHERE mode='cdc_dca_v1'")
                        db.commit()
                    except Exception:
                        pass
            except Exception as e:
                logging.warning(f"Could not ensure per-exchange sell_percent columns: {e}")

            # 3.7 เพิ่มคอลัมน์ sell_percent ใน sell_history เพื่อบันทึกสัดส่วนที่ขายจริง
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='sell_history' AND COLUMN_NAME='sell_percent'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column sell_percent to sell_history...")
                    cursor.execute("ALTER TABLE sell_history ADD COLUMN sell_percent TINYINT NULL AFTER order_id")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure sell_history.sell_percent: {e}")

            # 3.8 เพิ่มคอลัมน์ exchange ใน strategy_state ถ้ายังไม่มี
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='exchange'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column exchange (default 'binance') to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN exchange VARCHAR(16) NOT NULL DEFAULT 'binance' AFTER mode")
                    db.commit()
                    cursor.execute("UPDATE strategy_state SET exchange='binance' WHERE mode='cdc_dca_v1' AND (exchange IS NULL OR exchange='')")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure exchange column in strategy_state: {e}")

            # 3.9 เพิ่มคอลัมน์ exchange ใน purchase_history ถ้ายังไม่มี
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='purchase_history' AND COLUMN_NAME='exchange'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column exchange to purchase_history...")
                    cursor.execute("ALTER TABLE purchase_history ADD COLUMN exchange VARCHAR(16) NULL AFTER schedule_id")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure exchange column in purchase_history: {e}")

            # 3.10 เพิ่มคอลัมน์ exchange ใน sell_history ถ้ายังไม่มี
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='sell_history' AND COLUMN_NAME='exchange'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column exchange to sell_history...")
                    cursor.execute("ALTER TABLE sell_history ADD COLUMN exchange VARCHAR(16) NULL AFTER note")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure exchange column in sell_history: {e}")

            # 3.11 เพิ่มคอลัมน์ okx_max_usdt ใน strategy_state (default 10.00)
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='okx_max_usdt'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column okx_max_usdt (default 10.00) to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN okx_max_usdt DECIMAL(18,2) NOT NULL DEFAULT 10.00 AFTER sell_percent")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure okx_max_usdt column: {e}")

            # 3.12 สร้าง okx_trades table ถ้ายังไม่มี
            try:
                cursor.execute("SHOW TABLES LIKE 'okx_trades'")
                if not cursor.fetchone():
                    logging.info("Creating okx_trades table...")
                    cursor.execute(
                        """
                        CREATE TABLE okx_trades (
                            fill_id VARCHAR(64) PRIMARY KEY,
                            ord_id VARCHAR(64),
                            side VARCHAR(8),
                            price DECIMAL(18,8),
                            qty DECIMAL(18,8),
                            quote_qty DECIMAL(18,8),
                            fee DECIMAL(18,8),
                            fee_ccy VARCHAR(10),
                            trade_time DATETIME,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            INDEX idx_time (trade_time)
                        )
                        """
                    )
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not create okx_trades: {e}")

            # 3.13 เพิ่มคอลัมน์ exchange_mode/binance_amount/okx_amount ใน schedules
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='schedules' AND COLUMN_NAME='exchange_mode'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding columns exchange_mode/binance_amount/okx_amount to schedules...")
                    cursor.execute("ALTER TABLE schedules ADD COLUMN exchange_mode ENUM('global','binance','okx','both') NOT NULL DEFAULT 'global' AFTER purchase_amount")
                    db.commit()
                # amounts
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='schedules' AND COLUMN_NAME='binance_amount'
                    """,
                    (os.getenv('DB_NAME'),)
                );
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE schedules ADD COLUMN binance_amount DECIMAL(10,2) NULL AFTER exchange_mode")
                    db.commit()
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='schedules' AND COLUMN_NAME='okx_amount'
                    """,
                    (os.getenv('DB_NAME'),)
                );
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE schedules ADD COLUMN okx_amount DECIMAL(10,2) NULL AFTER binance_amount")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure schedules exchange columns: {e}")

            # 3.14 เพิ่มสำรองแยก per-exchange ใน strategy_state
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='reserve_binance_usdt'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN reserve_binance_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00 AFTER reserve_usdt")
                    db.commit()
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='reserve_okx_usdt'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN reserve_okx_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00 AFTER reserve_binance_usdt")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure per-exchange reserves: {e}")

            # 3.15 เพิ่ม half_sell_policy ใน strategy_state
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='half_sell_policy'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN half_sell_policy ENUM('auto_proportional','binance_only','okx_only') NOT NULL DEFAULT 'auto_proportional' AFTER okx_max_usdt")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure half_sell_policy: {e}")

            # 4. ตรวจสอบ Foreign Key
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'purchase_history' 
                AND COLUMN_NAME = 'schedule_id'
                AND REFERENCED_TABLE_NAME = 'schedules'
            """, (os.getenv('DB_NAME'),))
            
            if cursor.fetchone()[0] == 0:
                try:
                    cursor.execute("""
                        ALTER TABLE purchase_history 
                        ADD CONSTRAINT fk_purchase_history_schedule_id 
                        FOREIGN KEY (schedule_id) REFERENCES schedules(id) 
                        ON DELETE SET NULL ON UPDATE CASCADE
                    """)
                    db.commit()
                    logging.info("Foreign key constraint added")
                except MySQLdb.Error as e:
                    logging.warning(f"Could not add foreign key: {e}")

            migration_completed = True
            return True

    except Exception as e:
        logging.error(f"Migration error: {e}")
        return False

# ====== Helper Functions ======
def get_total_active_amount():
    """คำนวณยอดรวม active schedules"""
    try:
        with get_db_cursor() as (cursor, _):
            try:
                # รวมยอดให้ถูกต้องตามโหมด: global → purchase_amount,
                # binance → binance_amount, okx → okx_amount, both → ผลรวมสองฝั่ง
                cursor.execute(
                    """
                    SELECT SUM(
                        CASE 
                            WHEN exchange_mode = 'both' THEN COALESCE(binance_amount,0) + COALESCE(okx_amount,0)
                            WHEN exchange_mode = 'binance' THEN COALESCE(binance_amount,0)
                            WHEN exchange_mode = 'okx' THEN COALESCE(okx_amount,0)
                            ELSE COALESCE(purchase_amount,0)
                        END
                    ) AS total
                    FROM schedules WHERE is_active = 1
                    """
                )
            except Exception:
                # สคีม่าเก่า: ไม่มีคอลัมน์ exchange_mode/binance_amount/okx_amount
                cursor.execute("SELECT SUM(purchase_amount) FROM schedules WHERE is_active = 1")
            result = cursor.fetchone()[0]
            return float(result) if result else 0.0
    except Exception as e:
        logging.error(f"Error getting total amount: {e}")
        return 0.0

def validate_schedule_data(amount, time_str, days):
    """ตรวจสอบความถูกต้องของข้อมูล schedule"""
    errors = []
    
    try:
        float_amount = float(amount)
        if float_amount <= 0:
            errors.append("Amount must be positive")
    except (ValueError, TypeError):
        errors.append("Invalid amount format")
    
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        errors.append("Invalid time format. Use HH:MM")
    
    if not days:
        errors.append("Please select at least one day")
    
    return errors

# ====== Routes ======
@app.route('/')
def index():
    """หน้าแสดงผลหลัก"""
    try:
        # Run migration check
        migrate_data_if_needed()
        
        with get_db_cursor() as (cursor, _):
            # ดึงข้อมูล schedules
            try:
                cursor.execute("""
                    SELECT id, schedule_time, schedule_day, purchase_amount, is_active, exchange_mode, binance_amount, okx_amount
                    FROM schedules 
                    ORDER BY is_active DESC, schedule_time
                """)
            except Exception:
                cursor.execute("""
                    SELECT id, schedule_time, schedule_day, purchase_amount, is_active 
                    FROM schedules 
                    ORDER BY is_active DESC, schedule_time
                """)
            schedules = cursor.fetchall()

            # สร้าง next run time map ตาม schedule
            next_run_map = {}
            try:
                tz = timezone('Asia/Bangkok')
            except Exception:
                tz = None

            now = datetime.now(tz) if tz else datetime.now()
            weekday_map = {
                'monday': 0, 'tuesday': 1, 'wednesday': 2,
                'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6
            }

            for s in schedules:
                # Support both legacy (5 cols) and extended schema (>=7 cols)
                if len(s) >= 7:
                    sid, time_str, day_str, _purchase_amount, is_active, _ex_mode, _bz_amt, _okx_amt = s[0], s[1], s[2], s[3], s[4], s[5], s[6], (s[7] if len(s) > 7 else None)
                else:
                    sid, time_str, day_str, _purchase_amount, is_active = s
                if not is_active:
                    next_run_map[sid] = '-'
                    continue
                try:
                    hh, mm = map(int, str(time_str).split(':'))
                    days = [d.strip().lower() for d in str(day_str).split(',') if d.strip()]
                    target_weekdays = [weekday_map.get(d) for d in days if d in weekday_map]
                    if not target_weekdays:
                        next_run_map[sid] = '-'
                        continue

                    # compute next occurrence >= now
                    # iterate next 14 days to find the next run
                    base_date = now.date()
                    for i in range(0, 14):
                        day = base_date + timedelta(days=i)
                        dt = datetime(day.year, day.month, day.day, hh, mm)
                        if tz:
                            dt = tz.localize(dt)
                        if dt.weekday() in target_weekdays and dt >= now:
                            next_run_map[sid] = dt.strftime('%Y-%m-%d %H:%M')
                            break
                    if sid not in next_run_map:
                        next_run_map[sid] = '-'
                except Exception:
                    next_run_map[sid] = '-'
            
            # คำนวณยอดรวม
            total_amount = get_total_active_amount()
            
            # ดึงประวัติการซื้อ
            cursor.execute("""
                SELECT ph.id, ph.purchase_time, ph.usdt_amount, ph.btc_quantity, 
                       ph.btc_price, ph.order_id, ph.schedule_id, s.schedule_time
                FROM purchase_history ph
                LEFT JOIN schedules s ON ph.schedule_id = s.id
                ORDER BY ph.purchase_time DESC
                LIMIT 20
            """)
            history = cursor.fetchall()
            
            # เพิ่มข้อมูล stats สำหรับ template
            cursor.execute("SELECT COUNT(*) FROM schedules")
            total_schedules = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM schedules WHERE is_active = 1")
            active_schedules = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM purchase_history")
            total_purchases = cursor.fetchone()[0]
            
            # สร้าง stats object
            stats = {
                'total_schedules': total_schedules,
                'active_schedules': active_schedules,
                'total_purchases': total_purchases,
                'scheduler_status': last_scheduler_status
            }
            
        return render_template('index.html', 
                             schedules=schedules, 
                             history=history, 
                             total_amount=total_amount,
                             stats=stats,
                             next_run_map=next_run_map)
        
    except Exception as e:
        logging.error(f"Error in index route: {e}")
        flash("System error occurred", 'error')
        
        # สร้าง default stats สำหรับกรณี error
        default_stats = {
            'total_schedules': 0,
            'active_schedules': 0,
            'total_purchases': 0,
            'scheduler_status': 'Unknown'
        }
        
        return render_template('index.html', 
                             schedules=[], 
                             history=[], 
                             total_amount=0.0,
                             stats=default_stats)

@app.route('/check_duplicate_schedule', methods=['POST'])
def check_duplicate_schedule():
    """ตรวจสอบกำหนดการซ้ำ"""
    try:
        data = request.get_json()
        time_str = data['time']
        days = data['days']
        schedule_id = data.get('schedule_id')

        schedule_day = ",".join([d.lower() for d in days])
        
        with get_db_cursor() as (cursor, _):
            if schedule_id:
                cursor.execute("""
                    SELECT COUNT(*) FROM schedules 
                    WHERE schedule_time = %s AND schedule_day = %s AND id != %s
                """, (time_str, schedule_day, schedule_id))
            else:
                cursor.execute("""
                    SELECT COUNT(*) FROM schedules 
                    WHERE schedule_time = %s AND schedule_day = %s
                """, (time_str, schedule_day))
            
            count = cursor.fetchone()[0]
            
        return jsonify({'is_duplicate': count > 0})
        
    except Exception as e:
        logging.error(f"Error checking duplicate schedule: {e}")
        return jsonify({'is_duplicate': False, 'error': str(e)}), 500

@app.route('/get_total_amount')
def get_total_amount():
    """ดึงยอดรวม USDT"""
    try:
        total_amount = get_total_active_amount()
        return jsonify({'total_amount': total_amount})
    except Exception as e:
        logging.error(f"Error fetching total amount: {e}")
        return jsonify({'total_amount': 0.0, 'error': str(e)}), 500

@app.route('/check_schedule_usage/<int:schedule_id>')
def check_schedule_usage(schedule_id):
    """ตรวจสอบการใช้งาน schedule"""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("""
                SELECT COUNT(*) as count, COALESCE(SUM(usdt_amount), 0) as total
                FROM purchase_history 
                WHERE schedule_id = %s
            """, (schedule_id,))
            
            result = cursor.fetchone()
            purchase_count = result[0]
            total_amount = float(result[1])
            
        return jsonify({
            'canDelete': purchase_count == 0,
            'purchaseCount': purchase_count,
            'totalAmount': total_amount
        })
        
    except Exception as e:
        logging.error(f"Error checking schedule usage: {e}")
        return jsonify({'canDelete': False, 'error': str(e)}), 500

@app.route('/api/analytics')
def api_analytics():
    """Return analytics data for Investment Summary and DCA Performance.

    Response schema:
    {
      total_invested: float,
      total_btc: float,
      avg_price: float,
      success_rate: float,
      series: {
        timestamps: [str],               # ISO strings ascending
        cumulative_usdt: [float],        # cumulative invested
        cumulative_btc: [float],         # cumulative BTC acquired
        price: [float]                   # purchase price at each point
      },
      count: int
    }
    """
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT purchase_time, usdt_amount, btc_quantity, btc_price
                FROM purchase_history
                WHERE purchase_time IS NOT NULL
                ORDER BY purchase_time ASC
                """
            )
            rows = cursor.fetchall()

        timestamps = []
        cumulative_usdt = []
        cumulative_btc = []
        prices = []

        total_usdt = 0.0
        total_btc = 0.0

        for row in rows:
            ts, usdt, btc, price = row
            # Coalesce None to 0 for robustness
            usdt = float(usdt or 0.0)
            btc = float(btc or 0.0)
            price = float(price or 0.0)

            total_usdt += usdt
            total_btc += btc

            timestamps.append(str(ts))
            cumulative_usdt.append(total_usdt)
            cumulative_btc.append(total_btc)
            prices.append(price)

        # Weighted average price: total_usdt / total_btc
        avg_price = (total_usdt / total_btc) if total_btc > 0 else 0.0
        if not isfinite(avg_price):
            avg_price = 0.0

        # Success rate: we only record successful purchases in history
        success_rate = 100.0 if len(rows) > 0 else 0.0

        # Real-time price and unrealized PnL
        current_price = None
        portfolio_value = None
        pnl_abs = None
        pnl_pct = None

        try:
            if get_btc_price is not None:
                current_price = float(get_btc_price() or 0.0)
            else:
                current_price = 0.0
        except Exception:
            current_price = 0.0

        if current_price and total_btc > 0:
            portfolio_value = total_btc * current_price
            pnl_abs = portfolio_value - total_usdt
            pnl_pct = (pnl_abs / total_usdt * 100.0) if total_usdt > 0 else 0.0
        else:
            portfolio_value = 0.0
            pnl_abs = 0.0
            pnl_pct = 0.0

        payload = {
            'total_invested': round(total_usdt, 2),
            'total_btc': round(total_btc, 8),
            'avg_price': round(avg_price, 2),
            'success_rate': success_rate,
            'series': {
                'timestamps': timestamps,
                'cumulative_usdt': cumulative_usdt,
                'cumulative_btc': cumulative_btc,
                'price': prices,
            },
            'count': len(rows),
            'current_price': round(current_price, 2) if current_price is not None else 0.0,
            'portfolio_value': round(portfolio_value, 2) if portfolio_value is not None else 0.0,
            'pnl_abs': round(pnl_abs, 2) if pnl_abs is not None else 0.0,
            'pnl_pct': round(pnl_pct, 2) if pnl_pct is not None else 0.0,
        }

        return jsonify(payload)

    except Exception as e:
        logging.error(f"Error building analytics: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/add_schedule', methods=['POST'])
@handle_db_errors
def add_schedule():
    """เพิ่มกำหนดการใหม่"""
    amount = request.form.get('amount')
    time_str = request.form['time']
    days = request.form.getlist('day')
    is_active = request.form.get('is_active', '0') == '1'
    ex_mode = request.form.get('exchange_mode', 'global').strip().lower()
    bz_amt = request.form.get('binance_amount')
    okx_amt = request.form.get('okx_amount')

    # Validate input
    errors = validate_schedule_data(amount, time_str, days)
    if errors:
        for error in errors:
            flash(error, 'error')
        return redirect('/')

    float_amount = float(amount) if amount is not None and amount != '' else 0.0
    # Validate exchange amounts
    if ex_mode not in ('global','binance','okx','both'):
        flash('Invalid exchange mode', 'error'); return redirect('/')
    if ex_mode == 'binance':
        try:
            bz = float(bz_amt or 0)
            if bz <= 0: raise ValueError()
        except Exception:
            flash('Binance amount must be > 0', 'error'); return redirect('/')
    elif ex_mode == 'okx':
        try:
            ok = float(okx_amt or 0)
            if ok <= 0: raise ValueError()
        except Exception:
            flash('OKX amount must be > 0', 'error'); return redirect('/')
    elif ex_mode == 'both':
        try:
            bz = float(bz_amt or 0); ok = float(okx_amt or 0)
            if (bz + ok) <= 0: raise ValueError()
        except Exception:
            flash('Both-mode requires total amount > 0', 'error'); return redirect('/')
    schedule_day = ",".join([d.lower() for d in days])

    with get_db_cursor() as (cursor, db):
        # ตรวจสอบกำหนดการซ้ำ
        cursor.execute("""
            SELECT COUNT(*) FROM schedules 
            WHERE schedule_time = %s AND schedule_day = %s
        """, (time_str, schedule_day))
        
        if cursor.fetchone()[0] > 0:
            flash("A schedule with the same time and days already exists.", 'error')
            return redirect('/')

        try:
            cursor.execute("""
                INSERT INTO schedules (schedule_time, schedule_day, purchase_amount, is_active, exchange_mode, binance_amount, okx_amount) 
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (time_str, schedule_day, float_amount, is_active, ex_mode, (float(bz_amt) if bz_amt else None), (float(okx_amt) if okx_amt else None)))
        except Exception:
            # fallback to legacy columns
            cursor.execute("""
                INSERT INTO schedules (schedule_time, schedule_day, purchase_amount, is_active) 
                VALUES (%s, %s, %s, %s)
            """, (time_str, schedule_day, float_amount, is_active))
        
        schedule_id = cursor.lastrowid
        db.commit()
        
    logging.info(f"Schedule added: id={schedule_id}")
    flash("Schedule added successfully.", 'success')
    
    # Emit real-time updates
    socketio.emit('schedule_update', {
        'id': schedule_id,
        'schedule_time': time_str,
        'schedule_day': schedule_day,
        'purchase_amount': float_amount,
        'is_active': is_active,
        'exchange_mode': ex_mode,
        'binance_amount': float(bz_amt or 0) if ex_mode in ('binance','both') else None,
        'okx_amount': float(okx_amt or 0) if ex_mode in ('okx','both') else None
    })
    socketio.emit('total_amount_update', {'total_amount': get_total_active_amount()})
    
    return redirect('/')

@app.route('/edit_schedule/<int:schedule_id>', methods=['POST'])
@handle_db_errors
def edit_schedule(schedule_id):
    """แก้ไขกำหนดการ"""
    # Require ADMIN_TOKEN for edit (hardened)
    admin_env = os.getenv('ADMIN_TOKEN')
    admin_tok = request.form.get('admin_token')
    if not admin_env:
        flash('ADMIN_TOKEN is not configured on server', 'error')
        return redirect('/')
    if admin_tok != admin_env:
        flash('Forbidden: invalid ADMIN_TOKEN', 'error')
        return redirect('/')
    amount = request.form.get('amount')
    time_str = request.form['time']
    days = request.form.getlist('day')
    is_active = request.form.get('is_active', '0') == '1'
    ex_mode = request.form.get('exchange_mode', 'global').strip().lower()
    bz_amt = request.form.get('binance_amount')
    okx_amt = request.form.get('okx_amount')

    # Validate input
    errors = validate_schedule_data(amount, time_str, days)
    if errors:
        for error in errors:
            flash(error, 'error')
        return redirect('/')

    float_amount = float(amount) if amount is not None and amount != '' else 0.0
    # Validate exchange amounts
    if ex_mode not in ('global','binance','okx','both'):
        flash('Invalid exchange mode', 'error'); return redirect('/')
    if ex_mode == 'binance':
        try:
            bz = float(bz_amt or 0)
            if bz <= 0: raise ValueError()
        except Exception:
            flash('Binance amount must be > 0', 'error'); return redirect('/')
    elif ex_mode == 'okx':
        try:
            ok = float(okx_amt or 0)
            if ok <= 0: raise ValueError()
        except Exception:
            flash('OKX amount must be > 0', 'error'); return redirect('/')
    elif ex_mode == 'both':
        try:
            bz = float(bz_amt or 0); ok = float(okx_amt or 0)
            if (bz + ok) <= 0: raise ValueError()
        except Exception:
            flash('Both-mode requires total amount > 0', 'error'); return redirect('/')
    schedule_day = ",".join([d.lower() for d in days])

    with get_db_cursor() as (cursor, db):
        # ตรวจสอบกำหนดการซ้ำ
        cursor.execute("""
            SELECT COUNT(*) FROM schedules 
            WHERE schedule_time = %s AND schedule_day = %s AND id != %s
        """, (time_str, schedule_day, schedule_id))
        
        if cursor.fetchone()[0] > 0:
            flash("A schedule with the same time and days already exists.", 'error')
            return redirect('/')

        try:
            cursor.execute("""
                UPDATE schedules 
                SET schedule_time = %s, schedule_day = %s, purchase_amount = %s, is_active = %s,
                    exchange_mode=%s, binance_amount=%s, okx_amount=%s
                WHERE id = %s
            """, (time_str, schedule_day, float_amount, is_active, ex_mode, (float(bz_amt) if bz_amt else None), (float(okx_amt) if okx_amt else None), schedule_id))
        except Exception:
            cursor.execute("""
                UPDATE schedules 
                SET schedule_time = %s, schedule_day = %s, purchase_amount = %s, is_active = %s 
                WHERE id = %s
            """, (time_str, schedule_day, float_amount, is_active, schedule_id))
        
        if cursor.rowcount == 0:
            flash("Schedule not found.", 'error')
            return redirect('/')
            
        db.commit()

    logging.info(f"Schedule updated: id={schedule_id}")
    flash("Schedule updated successfully.", 'success')
    
    # Emit real-time updates
    socketio.emit('schedule_update', {
        'id': schedule_id,
        'schedule_time': time_str,
        'schedule_day': schedule_day,
        'purchase_amount': float_amount,
        'is_active': is_active
    })
    socketio.emit('total_amount_update', {'total_amount': get_total_active_amount()})
    
    return redirect('/')

@app.route('/delete_schedule/<int:schedule_id>', methods=['POST'])
@handle_db_errors
def delete_schedule(schedule_id):
    """ลบกำหนดการ (Smart Delete)"""
    with get_db_cursor() as (cursor, db):
        # ตรวจสอบการใช้งาน
        cursor.execute("SELECT COUNT(*) FROM purchase_history WHERE schedule_id = %s", (schedule_id,))
        usage_count = cursor.fetchone()[0]
        
        if usage_count > 0:
            # Soft delete - deactivate
            cursor.execute("UPDATE schedules SET is_active = 0 WHERE id = %s", (schedule_id,))
            if cursor.rowcount > 0:
                flash(f"Schedule deactivated (has {usage_count} purchase records)", 'warning')
                logging.info(f"Schedule {schedule_id} deactivated")
            else:
                flash("Schedule not found", 'error')
                return redirect('/')
        else:
            # Hard delete - no purchase history
            cursor.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
            if cursor.rowcount > 0:
                flash("Schedule deleted successfully", 'success')
                logging.info(f"Schedule {schedule_id} deleted")
            else:
                flash("Schedule not found", 'error')
                return redirect('/')
        
        db.commit()

    # Emit real-time updates
    socketio.emit('schedule_delete', {'id': schedule_id})
    socketio.emit('total_amount_update', {'total_amount': get_total_active_amount()})
    
    return redirect('/')

@app.route('/force_delete_schedule/<int:schedule_id>', methods=['POST'])
@handle_db_errors
def force_delete_schedule(schedule_id):
    """Force delete schedule (สำหรับ admin)"""
    with get_db_cursor() as (cursor, db):
        # ลบ purchase_history ก่อน
        cursor.execute("DELETE FROM purchase_history WHERE schedule_id = %s", (schedule_id,))
        deleted_purchases = cursor.rowcount
        
        # ลบ schedule
        cursor.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
        if cursor.rowcount > 0:
            flash(f"Force deleted: schedule + {deleted_purchases} purchase records", 'warning')
            logging.warning(f"Force deleted schedule {schedule_id} with {deleted_purchases} purchases")
        else:
            flash("Schedule not found", 'error')
            return redirect('/')
            
        db.commit()

    # Emit real-time updates
    socketio.emit('schedule_delete', {'id': schedule_id})
    socketio.emit('total_amount_update', {'total_amount': get_total_active_amount()})
    
    return redirect('/')

@app.route('/scheduler_status')
def scheduler_status():
    """ตรวจสอบสถานะ Scheduler"""
    try:
        health_check_port = os.getenv('HEALTH_CHECK_PORT', '8001')
        response = requests.get(f"http://localhost:{health_check_port}", timeout=5)
        if response.status_code == 200:
            return {'status': response.text}
        return {'status': 'Scheduler is not responding'}
    except requests.RequestException:
        return {'status': 'Scheduler is not responding'}

@app.route('/test_line_notify')
def test_line_notify():
    """ทดสอบ Line Notify"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    message = f"🔔 Test Line Notify: {timestamp}"
    if send_line_notify(message):
        flash("Test Line Notify sent successfully.", 'success')
    else:
        flash("Failed to send test Line Notify.", 'error')
    return redirect('/')

# ====== Admin Routes ======
@app.route('/admin')
def admin_dashboard():
    """Admin dashboard"""
    try:
        with get_db_cursor() as (cursor, _):
            # System stats
            cursor.execute("SELECT COUNT(*) FROM schedules")
            total_schedules = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM schedules WHERE is_active = 1")
            active_schedules = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM purchase_history")
            total_purchases = cursor.fetchone()[0]
            
            # Recent activity
            cursor.execute("""
                SELECT 'purchase' as type, purchase_time as timestamp, 
                       CONCAT('Purchase: ', usdt_amount, ' USDT') as description
                FROM purchase_history
                ORDER BY purchase_time DESC
                LIMIT 10
            """)
            recent_activity = cursor.fetchall()
            
            stats = {
                'total_schedules': total_schedules,
                'active_schedules': active_schedules,
                'total_purchases': total_purchases,
                'scheduler_status': last_scheduler_status,
                'recent_activity': [{
                    'type': row[0],
                    'timestamp': str(row[1]),
                    'description': row[2]
                } for row in recent_activity]
            }
            
        return render_template('admin.html', stats=stats)
        
    except Exception as e:
        logging.error(f"Error in admin dashboard: {e}")
        flash("Error loading admin dashboard", 'error')
        return redirect('/')

@app.route('/health')
def health_check():
    """Health check endpoint"""
    try:
        # Check database connection
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT 1")
            cursor.fetchone()
        
        # Check scheduler status
        scheduler_ok = last_scheduler_status == "Scheduler is running"
        
        status = {
            'status': 'healthy' if scheduler_ok else 'degraded',
            'timestamp': datetime.now().isoformat(),
            'database': 'connected',
            'scheduler': last_scheduler_status,
            'cache_size': 0  # Simple placeholder
        }
        
        return jsonify(status), 200 if scheduler_ok else 503
        
    except Exception as e:
        logging.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'unhealthy',
            'timestamp': datetime.now().isoformat(),
            'error': str(e)
        }), 503

# ====== SocketIO Events ======
@socketio.on('request_latest')
def handle_request_latest():
    """ส่งข้อมูลประวัติล่าสุด"""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("""
                SELECT ph.purchase_time, ph.usdt_amount, ph.btc_quantity, 
                       ph.btc_price, ph.order_id, ph.schedule_id, s.schedule_time
                FROM purchase_history ph
                LEFT JOIN schedules s ON ph.schedule_id = s.id
                ORDER BY ph.purchase_time DESC 
                LIMIT 10
            """)
            results = cursor.fetchall()

        data = [{
            "purchase_time": str(row[0]),
            "usdt_amount": float(row[1]) if row[1] else 0.0,
            "btc_quantity": float(row[2]) if row[2] else 0.0,
            "btc_price": float(row[3]) if row[3] else 0.0,
            "order_id": row[4],
            "schedule_id": row[5],
            "schedule_time": row[6]
        } for row in results]
        
        emit('latest_data', data)
        
    except Exception as e:
        logging.error(f"Error fetching latest data: {e}")
        emit('latest_data', {'error': 'Failed to fetch data'})

@socketio.on('connect')
def handle_connect():
    """การเชื่อมต่อ SocketIO"""
    logging.info("Client connected")
    handle_request_latest()

@socketio.on('disconnect')
def handle_disconnect():
    """การตัดการเชื่อมต่อ SocketIO"""
    logging.info("Client disconnected")

# ====== Error Handlers ======
@app.errorhandler(404)
def not_found_error(error):
    # สร้าง default stats สำหรับ error page
    default_stats = {
        'total_schedules': 0,
        'active_schedules': 0,
        'total_purchases': 0,
        'scheduler_status': 'Unknown'
    }
    return render_template('index.html', 
                         schedules=[], 
                         history=[], 
                         total_amount=0.0,
                         stats=default_stats), 404

@app.errorhandler(500)
def internal_error(error):
    logging.error(f"Internal server error: {error}")
    # สร้าง default stats สำหรับ error page
    default_stats = {
        'total_schedules': 0,
        'active_schedules': 0,
        'total_purchases': 0,
        'scheduler_status': 'Error'
    }
    return render_template('index.html', 
                         schedules=[], 
                         history=[], 
                         total_amount=0.0,
                         stats=default_stats), 500

# ====== Wallet API ======
@app.route('/api/wallet')
def api_wallet():
    """Return wallet snapshots for Binance and OKX plus totals."""

    def _safe_float(val, default=0.0):
        try:
            if val is None:
                return default
            return float(val)
        except (TypeError, ValueError):
            return default

    def _snapshot(exchange: str, reserve_value: float, testnet: bool, dry_run: bool) -> dict:
        snap = {
            'usdt_free': 0.0,
            'usdt_locked': 0.0,
            'btc_free': 0.0,
            'btc_locked': 0.0,
            'price': 0.0,
            'portfolio_value': 0.0,
            'reserve': _safe_float(reserve_value, 0.0),
            'error': None,
        }
        try:
            adapter = get_adapter(exchange, testnet=testnet, dry_run=dry_run)
        except Exception as exc:
            snap['error'] = str(exc)
            logging.warning(f"wallet snapshot init {exchange}: {exc}")
            return snap

        try:
            usdt = adapter.get_balance('USDT')
            btc = adapter.get_balance('BTC')
            snap['usdt_free'] = _safe_float(usdt.get('free'))
            snap['usdt_locked'] = _safe_float(usdt.get('locked'))
            snap['btc_free'] = _safe_float(btc.get('free'))
            snap['btc_locked'] = _safe_float(btc.get('locked'))
        except Exception as exc:
            snap['error'] = str(exc)
            logging.warning(f"wallet snapshot balance {exchange}: {exc}")

        try:
            price = float(adapter.get_price() or 0.0)
        except Exception as exc:
            price = 0.0
            if snap['error'] is None:
                snap['error'] = str(exc)
            logging.warning(f"wallet snapshot price {exchange}: {exc}")
        snap['price'] = price
        snap['portfolio_value'] = snap['usdt_free'] + snap['btc_free'] * price
        return snap

    payload = {
        'exchange': 'binance',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'binance': {},
        'okx': {},
        'totals': {
            'usdt_free': 0.0,
            'btc_free': 0.0,
            'portfolio_value': 0.0,
            'reserve': 0.0,
        }
    }

    try:
        snapshot_row = {}
        try:
            with get_db_cursor() as (cursor, _):
                cursor.execute("SELECT * FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
                row = cursor.fetchone()
                if row:
                    cols = [col[0] for col in cursor.description]
                    snapshot_row = dict(zip(cols, row))
        except Exception as db_exc:
            logging.warning(f"wallet strategy_state load: {db_exc}")

        current_exchange = str(snapshot_row.get('exchange') or 'binance').lower()
        payload['exchange'] = current_exchange

        reserve_binance = _safe_float(snapshot_row.get('reserve_binance_usdt'))
        reserve_okx = _safe_float(snapshot_row.get('reserve_okx_usdt'))
        total_reserve = _safe_float(snapshot_row.get('reserve_usdt'), reserve_binance + reserve_okx)

        testnet = _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET') or _env_flag('OKX_TESTNET')
        dry_run = _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')

        binance_snapshot = _snapshot('binance', reserve_binance, testnet, dry_run)
        okx_snapshot = _snapshot('okx', reserve_okx, testnet, dry_run)

        payload['binance'] = binance_snapshot
        payload['okx'] = okx_snapshot

        payload['totals']['usdt_free'] = binance_snapshot['usdt_free'] + okx_snapshot['usdt_free']
        payload['totals']['btc_free'] = binance_snapshot['btc_free'] + okx_snapshot['btc_free']
        payload['totals']['portfolio_value'] = binance_snapshot['portfolio_value'] + okx_snapshot['portfolio_value']
        payload['totals']['reserve'] = total_reserve if total_reserve else (reserve_binance + reserve_okx)

        return jsonify(payload)
    except Exception as e:
        logging.error(f"Error fetching wallet: {e}")
        return jsonify(payload), 200

# ====== CDC Action Zone (1D, BTCUSDT) ======
import time

def _env_flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ('1','true','yes','on')

def _get_binance_client():
    try:
        if get_client is not None:
            c = get_client()
            if c is not None:
                return c
    except Exception:
        pass
    # Fallback to env keys
    try:
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        if api_key and api_secret:
            testnet = _env_flag('USE_BINANCE_TESTNET', False) or _env_flag('BINANCE_TESTNET', False)
            return Client(api_key=api_key, api_secret=api_secret, testnet=testnet)
    except Exception:
        return None
    return None

@app.route('/api/cdc_action_zone')
def api_cdc_action_zone():
    """Return CDC Action Zone status using bullish/bearish logic on 1D BTCUSDT.
    Caches result for 60 seconds.
    """
    try:
        now = time.time()
        if _CDC_CACHE['data'] is not None and now < _CDC_CACHE['expires']:
            return jsonify(_CDC_CACHE['data'])

        client = _get_binance_client()
        if client is None:
            payload = {'status': 'down', 'symbol': 'BTCUSDT', 'timeframe': '1d', 'error': 'binance client unavailable'}
            _CDC_CACHE.update({'data': payload, 'expires': now + 60})
            return jsonify(payload)

        klines = client.get_klines(symbol='BTCUSDT', interval=Client.KLINE_INTERVAL_1DAY, limit=300)
        closes = [float(k[4]) for k in klines]
        if len(closes) < 50:
            payload = {'status': 'down', 'symbol': 'BTCUSDT', 'timeframe': '1d', 'error': 'insufficient data'}
            _CDC_CACHE.update({'data': payload, 'expires': now + 60})
            return jsonify(payload)

        xprice = _ema(closes, 1)
        fast = _ema(xprice, 12)
        slow = _ema(xprice, 26)

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

        last_buy = _last_true_index(buycond)
        last_sell = _last_true_index(sellcond)
        cur = n - 1
        inf = float('inf')
        bars_since_buy = (cur - last_buy) if last_buy is not None else inf
        bars_since_sell = (cur - last_sell) if last_sell is not None else inf

        if bars_since_buy == inf and bars_since_sell == inf:
            # fallback: use bull/bear of last bar
            is_bullish = bull[-1]
        else:
            is_bullish = bars_since_buy < bars_since_sell

        status = 'up' if is_bullish else 'down'

        payload = {
            'status': status,
            'symbol': 'BTCUSDT',
            'timeframe': '1d',
            'updated_at': datetime.utcnow().isoformat() + 'Z'
        }
        _CDC_CACHE.update({'data': payload, 'expires': now + 60})
        return jsonify(payload)
    except Exception as e:
        logging.error(f"CDC endpoint error: {e}")
        payload = {'status': 'down', 'symbol': 'BTCUSDT', 'timeframe': '1d', 'error': str(e)}
        # cache errors shortly to avoid hammering
        _CDC_CACHE.update({'data': payload, 'expires': time.time() + 30})
    return jsonify(payload), 200

@app.route('/api/strategy_state')
def api_strategy_state():
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT cdc_enabled, last_cdc_status, reserve_usdt, last_transition_at, sell_percent, exchange, okx_max_usdt, reserve_binance_usdt, reserve_okx_usdt, half_sell_policy, sell_percent_binance, sell_percent_okx FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
            row = cursor.fetchone()
        if not row:
            return jsonify({'cdc_enabled': False, 'last_cdc_status': None, 'reserve_usdt': 0.0, 'reserve_binance_usdt': 0.0, 'reserve_okx_usdt': 0.0, 'last_transition_at': None, 'sell_percent': 50, 'exchange': 'binance', 'okx_max_usdt': float(os.getenv('OKX_MAX_USDT') or 10.0), 'half_sell_policy': 'auto_proportional', 'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET'), 'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')})
        # Build response with backward compatibility
        exchange = (row[5] or 'binance')
        sell_percent_global = int(row[4] or 50)
        sp_bz = int(row[10] if row[10] is not None else sell_percent_global)
        sp_okx = int(row[11] if row[11] is not None else sell_percent_global)
        current_sp = sp_okx if str(exchange).lower() == 'okx' else sp_bz
        return jsonify({
            'cdc_enabled': bool(row[0]),
            'last_cdc_status': row[1],
            'reserve_usdt': float(row[2] or 0),
            'last_transition_at': str(row[3]) if row[3] else None,
            'sell_percent': current_sp,  # alias for UI
            'sell_percent_binance': sp_bz,
            'sell_percent_okx': sp_okx,
            'exchange': exchange,
            'okx_max_usdt': float(row[6] or (os.getenv('OKX_MAX_USDT') or 10.0)),
            'reserve_binance_usdt': float(row[7] or 0),
            'reserve_okx_usdt': float(row[8] or 0),
            'half_sell_policy': row[9] or 'auto_proportional',
            'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET'),
            'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')
        })
    except Exception as e:
        logging.error(f"strategy_state error: {e}")
        return jsonify({'cdc_enabled': False, 'reserve_usdt': 0.0, 'exchange': 'binance', 'error': str(e), 'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET'), 'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')}), 200

@app.route('/api/strategy_update', methods=['POST'])
@app.route('/api/strategy_update/', methods=['POST'])
def api_strategy_update():
    """Update CDC strategy settings.
    Supports per-exchange sell percent by passing { sell_percent, exchange }.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        sell_percent = data.get('sell_percent')
        sell_exchange = str(data.get('exchange') or '').strip().lower()
        half_sell_policy = data.get('half_sell_policy')
        updates = []
        params = []
        if sell_percent is not None:
            try:
                sell_percent = int(sell_percent)
            except Exception:
                return jsonify({'ok': False, 'error': 'sell_percent must be integer'}), 400
            if sell_percent < 0 or sell_percent > 100:
                return jsonify({'ok': False, 'error': 'sell_percent must be between 0 and 100'}), 400
            if sell_exchange in ('binance','okx'):
                col = 'sell_percent_okx' if sell_exchange == 'okx' else 'sell_percent_binance'
                updates.append(f"{col}=%s"); params.append(sell_percent)
            else:
                # legacy/global fallback
                updates.append('sell_percent=%s'); params.append(sell_percent)
        if half_sell_policy is not None:
            if str(half_sell_policy) not in ('auto_proportional','binance_only','okx_only'):
                return jsonify({'ok': False, 'error': 'invalid half_sell_policy'}), 400
            updates.append('half_sell_policy=%s'); params.append(half_sell_policy)
        if not updates:
            return jsonify({'ok': False, 'error': 'no fields to update'}), 400
        with get_db_cursor() as (cursor, db):
            cursor.execute(f"UPDATE strategy_state SET {', '.join(updates)} WHERE mode='cdc_dca_v1'", tuple(params))
            db.commit()
        return jsonify({'ok': True, 'sell_percent': sell_percent, 'exchange': sell_exchange or None, 'half_sell_policy': half_sell_policy})
    except Exception as e:
        logging.error(f"strategy_update error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/strategy_toggle', methods=['POST'])
@app.route('/api/strategy_toggle/', methods=['POST'])
def api_strategy_toggle():
    try:
        data = request.get_json(force=True, silent=True) or {}
        enabled = bool(data.get('enabled'))
        with get_db_cursor() as (cursor, db):
            cursor.execute("UPDATE strategy_state SET cdc_enabled = %s WHERE mode='cdc_dca_v1'", (1 if enabled else 0,))
            db.commit()
        # Notify via LINE
        try:
            notify_cdc_toggle(enabled, {
                'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET'),
                'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN'),
            })
        except Exception as e:
            logging.warning(f"CDC toggle notify failed: {e}")
        return jsonify({'ok': True, 'cdc_enabled': enabled})
    except Exception as e:
        logging.error(f"strategy_toggle error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/okx_config', methods=['POST'])
def api_okx_config():
    """Update OKX config (e.g., okx_max_usdt). Admin-only."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        if not admin_token:
            return jsonify({'ok': False, 'error': 'admin_token_not_configured'}), 400
        if token != admin_token:
            return jsonify({'ok': False, 'error': 'invalid_admin_token'}), 403
        max_usdt = data.get('okx_max_usdt')
        try:
            max_usdt = float(max_usdt)
            if max_usdt <= 0:
                raise ValueError('okx_max_usdt must be > 0')
        except Exception:
            return jsonify({'ok': False, 'error': 'invalid okx_max_usdt'}), 400
        with get_db_cursor() as (cursor, db):
            cursor.execute("UPDATE strategy_state SET okx_max_usdt=%s WHERE mode='cdc_dca_v1'", (max_usdt,))
            db.commit()
        return jsonify({'ok': True, 'okx_max_usdt': max_usdt})
    except Exception as e:
        logging.error(f"okx_config error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/okx_trades_sync', methods=['POST'])
def api_okx_trades_sync():
    """Sync recent OKX spot fills (BTC-USDT) into okx_trades table."""
    try:
        from exchanges.okx import OkxAdapter
        ad = OkxAdapter()
        fills = ad.get_fills_history(limit=100)
        inserted = 0
        rows = []
        for f in fills:
            fill_id = str(f.get('billId') or f.get('tradeId') or f.get('ordId'))
            ord_id = str(f.get('ordId') or '')
            side = str(f.get('side') or '').upper()
            px = float(f.get('fillPx') or f.get('px') or 0)
            sz = float(f.get('fillSz') or f.get('sz') or 0)
            q = px * sz
            fee = float(f.get('fee') or 0)
            fee_ccy = str(f.get('feeCcy') or '')
            ts = f.get('fillTime') or f.get('ts')
            from datetime import datetime
            try:
                t = datetime.fromtimestamp(int(ts)/1000.0)
            except Exception:
                t = datetime.utcnow()
            rows.append((fill_id, ord_id, side, px, sz, q, fee, fee_ccy, t))
        if rows:
            with get_db_cursor() as (cursor, db):
                cursor.executemany(
                    """
                    INSERT IGNORE INTO okx_trades (fill_id, ord_id, side, price, qty, quote_qty, fee, fee_ccy, trade_time)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    rows
                )
                inserted = cursor.rowcount
                db.commit()
        return jsonify({'ok': True, 'synced': inserted})
    except Exception as e:
        logging.error(f"okx_trades_sync error: {e}")
        return jsonify({'ok': False, 'error': str(e), 'synced': 0}), 500

@app.route('/api/okx_trades')
def api_okx_trades():
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, side, price, qty, quote_qty, fee, fee_ccy, ord_id, fill_id
                FROM okx_trades ORDER BY trade_time DESC LIMIT 100
                """
            )
            rows = cursor.fetchall()
        data = [{
            'trade_time': str(r[0]), 'side': r[1], 'price': float(r[2] or 0), 'qty': float(r[3] or 0),
            'quote_qty': float(r[4] or 0), 'fee': float(r[5] or 0), 'fee_ccy': r[6], 'ord_id': r[7], 'fill_id': r[8]
        } for r in rows]
        return jsonify({'count': len(data), 'trades': data})
    except Exception as e:
        logging.error(f"okx_trades error: {e}")
        return jsonify({'count': 0, 'error': str(e)}), 500

@app.route('/api/okx_trades_analytics')
def api_okx_trades_analytics():
    """Compute PnL/avg price from okx_trades using moving-average method (similar to Binance)."""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, side, price, qty, quote_qty, fee, fee_ccy
                FROM okx_trades
                ORDER BY trade_time ASC
                """
            )
            rows = cursor.fetchall()

        position_btc = 0.0
        cost_usdt = 0.0
        total_buy_usdt = 0.0
        total_sell_usdt = 0.0
        realized_pnl = 0.0

        for r in rows:
            trade_dt, side, price, qty, quote_qty, fee, fee_ccy = r
            price = float(price or 0.0)
            qty = float(qty or 0.0)
            quote_qty = float(quote_qty or (price * qty))
            fee = float(fee or 0.0)
            fee_ccy = (fee_ccy or '').upper()

            fee_usdt = fee if fee_ccy == 'USDT' else 0.0
            fee_btc = fee if fee_ccy == 'BTC' else 0.0
            fee_other_usdt = 0.0  # not converted for simplicity

            if (side or '').upper() == 'BUY':
                adj_qty = max(qty - fee_btc, 0.0)
                adj_cost = quote_qty + fee_usdt + fee_other_usdt
                total_buy_usdt += adj_cost
                new_position = position_btc + adj_qty
                if new_position <= 0:
                    position_btc = 0.0
                    cost_usdt = 0.0
                else:
                    cost_usdt += adj_cost
                    position_btc = new_position
            else:  # SELL
                proceeds = max(quote_qty - fee_usdt - fee_other_usdt, 0.0)
                total_sell_usdt += proceeds
                if position_btc <= 0:
                    realized_pnl += proceeds
                    continue
                avg_cost = cost_usdt / position_btc if position_btc > 0 else 0.0
                qty_to_close = min(qty, position_btc)
                realized_pnl += proceeds - (avg_cost * qty_to_close)
                cost_usdt -= avg_cost * qty_to_close
                position_btc -= qty_to_close
                if fee_btc > 0 and position_btc > 0:
                    extra_close = min(fee_btc, position_btc)
                    realized_pnl += 0.0 - (avg_cost * extra_close)
                    cost_usdt -= avg_cost * extra_close
                    position_btc -= extra_close

        # Current price from adapter (or utils)
        current_price = 0.0
        try:
            if get_btc_price is not None:
                current_price = float(get_btc_price() or 0.0)
        except Exception:
            current_price = 0.0
        portfolio_value = position_btc * current_price
        unrealized_pnl = portfolio_value - cost_usdt
        avg_price = (cost_usdt / position_btc) if position_btc > 0 else 0.0

        return jsonify({
            'total_buys_usdt': round(total_buy_usdt, 2),
            'total_sells_usdt': round(total_sell_usdt, 2),
            'position_btc': round(position_btc, 8),
            'avg_price': round(avg_price, 2),
            'realized_pnl': round(realized_pnl, 2),
            'unrealized_pnl': round(unrealized_pnl, 2),
            'current_price': round(current_price, 2),
            'portfolio_value': round(portfolio_value, 2),
            'count': len(rows),
        })
    except Exception as e:
        logging.error(f"okx_trades_analytics error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy_exchange', methods=['POST'])
def api_strategy_exchange():
    """Update global exchange for trading (binance|okx). Admin-only."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        exchange = str(data.get('exchange') or '').strip().lower()
        if not admin_token:
            return jsonify({'ok': False, 'error': 'admin_token_not_configured'}), 400
        if token != admin_token:
            return jsonify({'ok': False, 'error': 'invalid_admin_token'}), 403
        if exchange not in ('binance', 'okx'):
            return jsonify({'ok': False, 'error': 'invalid_exchange'}), 400

        with get_db_cursor() as (cursor, db):
            cursor.execute("UPDATE strategy_state SET exchange=%s WHERE mode='cdc_dca_v1'", (exchange,))
            db.commit()

        try:
            from notify import notify_exchange_changed
            flags = {
                'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET') or _env_flag('OKX_TESTNET'),
                'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')
            }
            notify_exchange_changed(exchange, flags)
        except Exception as ne:
            logging.warning(f"exchange change notify failed: {ne}")

        return jsonify({'ok': True, 'exchange': exchange})
    except Exception as e:
        logging.error(f"strategy_exchange error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/reserve_transfer', methods=['POST'])
def api_reserve_transfer():
    """Move available USDT into strategy reserve (per exchange or global). Admin-only."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        if not admin_token or token != admin_token:
            return jsonify({'ok': False, 'error': 'forbidden'}), 403

        exchange = str(data.get('exchange') or '').strip().lower() or 'global'
        amount_raw = data.get('amount')
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid_amount'}), 400
        if amount <= 0:
            return jsonify({'ok': False, 'error': 'amount_must_be_positive'}), 400

        note = str(data.get('note') or '').strip() or None
        testnet = _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET') or _env_flag('OKX_TESTNET')
        dry_run = _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')

        new_value = 0.0
        if exchange == 'global':
            new_value = increment_reserve(amount, reason='manual_deposit', note=note or 'Manual reserve deposit (global)')
        elif exchange in ('binance', 'okx'):
            try:
                adapter = get_adapter(exchange, testnet=testnet, dry_run=dry_run)
                balance = adapter.get_balance('USDT')
                free_bal = float(balance.get('free') or 0.0)
                if not dry_run and not testnet and amount > free_bal:
                    return jsonify({'ok': False, 'error': f'insufficient_{exchange}_balance', 'free': free_bal}), 400
            except Exception as exc:
                logging.warning(f"reserve_transfer balance check failed ({exchange}): {exc}")
            reason = f'manual_deposit_{exchange}'
            default_note = f'Manual reserve deposit from {exchange.upper()} balance'
            new_value = increment_reserve_exchange(exchange, amount, reason=reason, note=note or default_note)
        else:
            return jsonify({'ok': False, 'error': 'invalid_exchange'}), 400

        reserves = {'binance': 0.0, 'okx': 0.0, 'total': 0.0}
        try:
            with get_db_cursor() as (cursor, _):
                cursor.execute("SELECT reserve_binance_usdt, reserve_okx_usdt, reserve_usdt FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
                row = cursor.fetchone()
                if row:
                    reserves['binance'] = float(row[0] or 0.0)
                    reserves['okx'] = float(row[1] or 0.0)
                    reserves['total'] = float(row[2] or 0.0)
        except Exception as exc:
            logging.warning(f"reserve_transfer state fetch failed: {exc}")

        return jsonify({'ok': True, 'exchange': exchange, 'reserve': new_value, 'reserves': reserves})
    except Exception as e:
        logging.error(f"reserve_transfer error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/reserve_log')
def api_reserve_log():
    try:
        limit = int(request.args.get('limit') or 50)
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT event_time, change_usdt, reserve_after, reason, note FROM reserve_log ORDER BY event_time DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()
        data = [{
            'event_time': str(r[0]),
            'change_usdt': float(r[1]),
            'reserve_after': float(r[2]),
            'reason': r[3],
            'note': r[4]
        } for r in rows]
        return jsonify({'items': data})
    except Exception as e:
        logging.error(f"reserve_log error: {e}")
        return jsonify({'items': [], 'error': str(e)}), 200

@app.route('/api/sell_history')
def api_sell_history():
    try:
        limit = int(request.args.get('limit') or 50)
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT sell_time, symbol, btc_quantity, usdt_received, price, order_id, sell_percent
                FROM sell_history ORDER BY sell_time DESC LIMIT %s
                """,
                (limit,)
            )
            rows = cursor.fetchall()
        data = [{
            'sell_time': str(r[0]),
            'symbol': r[1],
            'btc_quantity': float(r[2]),
            'usdt_received': float(r[3]),
            'price': float(r[4]),
            'order_id': r[5],
            'sell_percent': (int(r[6]) if r[6] is not None else None)
        } for r in rows]
        return jsonify({'items': data})
    except Exception as e:
        logging.error(f"sell_history api error: {e}")
        return jsonify({'items': [], 'error': str(e)}), 200

@app.route('/api/purchase_history_export')
def api_purchase_history_export():
    """Export purchase_history as CSV. Optional query: exchange=binance|okx|all (default all)."""
    try:
        exch = (request.args.get('exchange') or 'all').strip().lower()
        q = (
            "SELECT purchase_time, COALESCE(exchange,''), usdt_amount, btc_quantity, btc_price, order_id, schedule_id "
            "FROM purchase_history"
        )
        params = []
        if exch in ('binance','okx'):
            q += " WHERE exchange = %s"
            params.append(exch)
        q += " ORDER BY purchase_time DESC"
        with get_db_cursor() as (cursor, _):
            cursor.execute(q, tuple(params))
            rows = cursor.fetchall()

        import io, csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['time','exchange','usdt_amount','btc_quantity','btc_price','order_id','schedule_id'])
        for r in rows:
            writer.writerow([
                str(r[0]) if r[0] else '', r[1] or '', float(r[2] or 0.0), float(r[3] or 0.0), float(r[4] or 0.0), r[5] or '', r[6] or ''
            ])
        csv_data = output.getvalue().encode('utf-8')
        from flask import Response
        import datetime as _dt
        fname = f"purchase_history_{exch}_{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
        return Response(csv_data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})
    except Exception as e:
        logging.error(f"purchase_history_export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/sell_history_export')
def api_sell_history_export():
    """Export sell_history as CSV. Optional query: exchange=binance|okx|all (default all)."""
    try:
        exch = (request.args.get('exchange') or 'all').strip().lower()
        q = (
            "SELECT sell_time, symbol, COALESCE(exchange,''), btc_quantity, usdt_received, price, order_id, sell_percent "
            "FROM sell_history"
        )
        params = []
        if exch in ('binance','okx'):
            q += " WHERE exchange = %s"
            params.append(exch)
        q += " ORDER BY sell_time DESC"
        with get_db_cursor() as (cursor, _):
            cursor.execute(q, tuple(params))
            rows = cursor.fetchall()

        import io, csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['time','symbol','exchange','btc_quantity','usdt_received','price','order_id','sell_percent'])
        for r in rows:
            writer.writerow([
                str(r[0]) if r[0] else '', r[1] or '', r[2] or '', float(r[3] or 0.0), float(r[4] or 0.0), float(r[5] or 0.0), r[6] or '', r[7] if r[7] is not None else ''
            ])
        data = output.getvalue().encode('utf-8')
        from flask import Response
        import datetime as _dt
        fname = f"sell_history_{exch}_{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
        return Response(data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})
    except Exception as e:
        logging.error(f"sell_history_export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/binance_trades_export')
def api_binance_trades_export():
    """Export last N (default 100) binance_trades rows to CSV."""
    try:
        limit = int(request.args.get('limit') or 100)
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, is_buyer, price, qty, quote_qty, commission, commission_asset, order_id, trade_id
                FROM binance_trades
                WHERE symbol='BTCUSDT'
                ORDER BY trade_time DESC
                LIMIT %s
                """,
                (limit,)
            )
            rows = cursor.fetchall()
        import io, csv
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(['time','side','price','qty','quote_qty','commission','commission_asset','order_id','trade_id'])
        for r in rows:
            w.writerow([
                str(r[0]), ('BUY' if r[1] else 'SELL'), float(r[2] or 0), float(r[3] or 0), float(r[4] or 0), float(r[5] or 0), r[6] or '', r[7] or '', r[8] or ''
            ])
        data = out.getvalue().encode('utf-8')
        from flask import Response
        import datetime as _dt
        fname = f"binance_trades_{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
        return Response(data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})
    except Exception as e:
        logging.error(f"binance_trades_export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/okx_trades_export')
def api_okx_trades_export():
    """Export last N (default 100) okx_trades rows to CSV."""
    try:
        limit = int(request.args.get('limit') or 100)
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, side, price, qty, quote_qty, fee, fee_ccy, ord_id, fill_id
                FROM okx_trades
                ORDER BY trade_time DESC
                LIMIT %s
                """,
                (limit,)
            )
            rows = cursor.fetchall()
        import io, csv
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(['time','side','price','qty','quote_qty','fee','fee_ccy','order_id','fill_id'])
        for r in rows:
            w.writerow([
                str(r[0]), r[1] or '', float(r[2] or 0), float(r[3] or 0), float(r[4] or 0), float(r[5] or 0), r[6] or '', r[7] or '', r[8] or ''
            ])
        data = out.getvalue().encode('utf-8')
        from flask import Response
        import datetime as _dt
        fname = f"okx_trades_{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
        return Response(data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})
    except Exception as e:
        logging.error(f"okx_trades_export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/use_reserve_now', methods=['POST'])
def api_use_reserve_now():
    """Admin-only: Use reserve immediately to buy BTC with available USDT up to reserve amount."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        if not admin_token or token != admin_token:
            return jsonify({'ok': False, 'error': 'forbidden'}), 403

        client = _get_binance_client()
        if client is None:
            return jsonify({'ok': False, 'error': 'binance client unavailable'}), 500

        # Load reserve
        with get_db_cursor() as (cursor, db):
            cursor.execute("SELECT reserve_usdt FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
            row = cursor.fetchone(); reserve = float(row[0] or 0) if row else 0.0

        usdt = client.get_asset_balance(asset='USDT')
        available = float(usdt.get('free') or 0)
        spend = min(available, reserve)

        # Filters
        info = client.get_symbol_info('BTCUSDT')
        min_notional = None
        for f in info['filters']:
            if f['filterType'] == 'NOTIONAL':
                min_notional = float(f.get('minNotional') or 0)
                break
        min_notional = min_notional or 10.0
        if spend < min_notional:
            return jsonify({'ok': False, 'error': 'below minNotional', 'spend': spend, 'reserve': reserve}), 200

        # Place order (support DRY_RUN)
        dry = _env_flag('STRATEGY_DRY_RUN', False) or _env_flag('DRY_RUN', False)
        if dry:
            price = float(client.get_symbol_ticker(symbol='BTCUSDT')['price'])
            executed_qty = spend / price
            cqq = spend
            avg_price = price
            order_id = -1
        else:
            order = client.order_market_buy(symbol='BTCUSDT', quoteOrderQty=spend)
            order_id = order['orderId']
            details = client.get_order(symbol='BTCUSDT', orderId=order_id)
            executed_qty = float(details.get('executedQty') or 0)
            cqq = float(details.get('cummulativeQuoteQty') or 0)
            avg_price = cqq / executed_qty if executed_qty > 0 else 0

        with get_db_cursor() as (cursor, db):
            # purchase history
            cursor.execute(
                """
                INSERT INTO purchase_history (purchase_time, usdt_amount, btc_quantity, btc_price, order_id, schedule_id)
                VALUES (NOW(), %s, %s, %s, %s, %s)
                """,
                (cqq, executed_qty, avg_price, order_id, None)
            )
            # reduce reserve and log
            cursor.execute("UPDATE strategy_state SET reserve_usdt = GREATEST(reserve_usdt - %s, 0) WHERE mode='cdc_dca_v1'", (cqq,))
            cursor.execute("SELECT reserve_usdt FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
            new_reserve = float(cursor.fetchone()[0] or 0)
            cursor.execute(
                """
                INSERT INTO reserve_log (event_time, change_usdt, reserve_after, reason, note)
                VALUES (NOW(), %s, %s, %s, %s)
                """,
                (-cqq, new_reserve, 'reserve_buy_now', 'Manual reserve buy via API')
            )
            db.commit()

        return jsonify({'ok': True, 'spend': cqq, 'btc_qty': executed_qty, 'price': avg_price, 'order_id': order_id, 'reserve_after': new_reserve})
    except Exception as e:
        logging.error(f"use_reserve_now error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

# ====== Binance Trades Sync & Analytics ======
@app.route('/api/sync_trades', methods=['POST', 'GET'])
@app.route('/api/sync_trades/', methods=['POST', 'GET'])
def api_sync_trades():
    """Sync BTCUSDT trades from Binance into binance_trades table.
    Incremental by trade_id.
    """
    try:
        if get_client is None:
            return jsonify({'synced': 0, 'message': 'Binance client unavailable'}), 200

        client = get_client()

        # find max trade_id
        with get_db_cursor() as (cursor, db):
            cursor.execute("SELECT COALESCE(MAX(trade_id), 0) FROM binance_trades WHERE symbol = 'BTCUSDT'")
            last_id = int(cursor.fetchone()[0] or 0)

        symbol = 'BTCUSDT'
        limit = 1000
        total_inserted = 0
        next_from_id = last_id + 1 if last_id > 0 else None

        while True:
            params = {'symbol': symbol, 'limit': limit}
            if next_from_id:
                params['fromId'] = next_from_id
            trades = client.get_my_trades(**params)
            if not trades:
                break

            rows = []
            for t in trades:
                rows.append((
                    int(t['id']),
                    symbol,
                    int(t.get('orderId') or 0),
                    float(t['price'] or 0),
                    float(t['qty'] or 0),
                    float(t.get('quoteQty') or (float(t['price'] or 0) * float(t['qty'] or 0))),
                    float(t.get('commission') or 0),
                    str(t.get('commissionAsset') or ''),
                    1 if t.get('isBuyer') else 0,
                    1 if t.get('isMaker') else 0,
                    1 if t.get('isBestMatch') else 0,
                    datetime.fromtimestamp(int(t['time'])/1000.0)
                ))

            with get_db_cursor() as (cursor, db):
                cursor.executemany(
                    """
                    INSERT IGNORE INTO binance_trades
                    (trade_id, symbol, order_id, price, qty, quote_qty, commission, commission_asset,
                     is_buyer, is_maker, is_best_match, trade_time)
                    VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    rows
                )
                total_inserted += cursor.rowcount
                db.commit()

            # prepare next page
            if len(trades) < limit:
                break
            next_from_id = int(trades[-1]['id']) + 1

        return jsonify({'synced': total_inserted})
    except Exception as e:
        logging.error(f"Sync trades error: {e}")
        return jsonify({'synced': 0, 'error': str(e)}), 500


@app.route('/api/binance_trades')
def api_binance_trades():
    """Return recent trades from binance_trades table (BTCUSDT)."""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, is_buyer, price, qty, quote_qty, commission, commission_asset, order_id, trade_id
                FROM binance_trades
                WHERE symbol = 'BTCUSDT'
                ORDER BY trade_time DESC
                LIMIT 100
                """
            )
            rows = cursor.fetchall()

        data = [{
            'trade_time': str(r[0]),
            'side': 'BUY' if r[1] else 'SELL',
            'price': float(r[2] or 0),
            'qty': float(r[3] or 0),
            'quote_qty': float(r[4] or 0),
            'commission': float(r[5] or 0),
            'commission_asset': r[6],
            'order_id': r[7],
            'trade_id': r[8],
        } for r in rows]

        return jsonify({'count': len(data), 'trades': data})
    except Exception as e:
        logging.error(f"Fetch trades error: {e}")
        return jsonify({'count': 0, 'error': str(e)}), 500


@app.route('/api/binance_trades_analytics')
def api_binance_trades_analytics():
    """Compute PnL/avg price from binance_trades using moving-average method."""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, is_buyer, price, qty, quote_qty, commission, commission_asset
                FROM binance_trades
                WHERE symbol = 'BTCUSDT'
                ORDER BY trade_time ASC
                """
            )
            rows = cursor.fetchall()

        # Helper: convert non-USDT fee to USDT using trade-time price
        price_cache = {}

        def get_usdt_price_for_asset_at(asset: str, trade_dt, fallback_price_for_btc: float) -> float:
            asset = (asset or '').upper()
            if asset in ('', 'USDT'):
                return 1.0
            if asset == 'BTC':
                # For BTC fee inside BTCUSDT trades, use trade price
                return float(fallback_price_for_btc or 0.0)
            # For other assets like BNB, fetch approximate 1m close at trade time
            key = (asset, int(trade_dt.timestamp() // 60))
            if key in price_cache:
                return price_cache[key]
            symbol = f"{asset}USDT"
            usdt_px = 0.0
            try:
                if get_client is not None:
                    client = get_client()
                    # fetch 1 minute kline covering the trade time
                    start_ms = int((trade_dt.timestamp() - 60) * 1000)
                    end_ms = int((trade_dt.timestamp() + 60) * 1000)
                    kl = client.get_klines(symbol=symbol, interval='1m', startTime=start_ms, endTime=end_ms, limit=1)
                    if kl:
                        # use close price
                        usdt_px = float(kl[0][4])
                if not usdt_px and get_btc_price is not None:
                    # last resort: use current price (less accurate)
                    ticker_client = get_client() if get_client is not None else None
                    if ticker_client:
                        t = ticker_client.get_symbol_ticker(symbol=symbol)
                        usdt_px = float(t['price'])
            except Exception:
                usdt_px = 0.0
            if usdt_px <= 0.0:
                usdt_px = 0.0
            price_cache[key] = usdt_px
            return usdt_px

        position_btc = 0.0
        cost_usdt = 0.0
        total_buy_usdt = 0.0
        total_sell_usdt = 0.0
        realized_pnl = 0.0

        for r in rows:
            trade_time_dt, is_buyer, price, qty, quote_qty, commission, commission_asset = r
            price = float(price or 0.0)
            qty = float(qty or 0.0)
            quote_qty = float(quote_qty or (price * qty))
            commission = float(commission or 0.0)
            commission_asset = (commission_asset or '').upper()

            # Convert fees to components
            fee_usdt = commission if commission_asset == 'USDT' else 0.0
            fee_btc = commission if commission_asset == 'BTC' else 0.0
            fee_other_usdt = 0.0
            if commission > 0.0 and commission_asset not in ('', 'USDT', 'BTC'):
                px = get_usdt_price_for_asset_at(commission_asset, trade_time_dt, price)
                fee_other_usdt = commission * float(px or 0.0)

            if is_buyer:  # BUY
                # Net BTC received after BTC fee
                adj_qty = max(qty - fee_btc, 0.0)
                # Add USDT-equivalent of non-USDT fees (e.g., BNB)
                adj_cost = quote_qty + fee_usdt + fee_other_usdt
                total_buy_usdt += adj_cost
                # update moving average
                new_position = position_btc + adj_qty
                if new_position <= 0:
                    position_btc = 0.0
                    cost_usdt = 0.0
                else:
                    cost_usdt = cost_usdt + adj_cost
                    position_btc = new_position
            else:  # SELL
                # Proceeds after USDT/other-asset fees (BNB converted to USDT)
                proceeds = max(quote_qty - fee_usdt - fee_other_usdt, 0.0)
                total_sell_usdt += proceeds

                if position_btc <= 0:
                    realized_pnl += proceeds
                    continue

                avg_cost = cost_usdt / position_btc if position_btc > 0 else 0.0
                qty_to_close = min(qty, position_btc)
                realized_pnl += proceeds - (avg_cost * qty_to_close)
                cost_usdt -= avg_cost * qty_to_close
                position_btc -= qty_to_close

                # If fee charged in BTC on a sell, deduct extra BTC with zero proceeds
                if fee_btc > 0 and position_btc > 0:
                    extra_close = min(fee_btc, position_btc)
                    realized_pnl += 0.0 - (avg_cost * extra_close)
                    cost_usdt -= avg_cost * extra_close
                    position_btc -= extra_close

        # current price
        try:
            current_price = float(get_btc_price() or 0.0) if get_btc_price is not None else 0.0
        except Exception:
            current_price = 0.0

        portfolio_value = position_btc * current_price
        unrealized_pnl = portfolio_value - cost_usdt
        avg_price = (cost_usdt / position_btc) if position_btc > 0 else 0.0

        payload = {
            'total_buys_usdt': round(total_buy_usdt, 2),
            'total_sells_usdt': round(total_sell_usdt, 2),
            'position_btc': round(position_btc, 8),
            'avg_price': round(avg_price, 2),
            'realized_pnl': round(realized_pnl, 2),
            'unrealized_pnl': round(unrealized_pnl, 2),
            'current_price': round(current_price, 2),
            'portfolio_value': round(portfolio_value, 2),
            'count': len(rows),
        }

        return jsonify(payload)
    except Exception as e:
        logging.error(f"Trades analytics error: {e}")
        return jsonify({'error': str(e)}), 500

# ====== Backfill Trades with Progress ======
from threading import Thread
import uuid

_BACKFILL_TASKS = {}

@app.route('/api/sync_trades_range', methods=['POST'])
def api_sync_trades_range():
    """Start a background backfill of BTCUSDT trades between start and end (UTC).
    Body JSON: { start: ISO8601, end: ISO8601 }
    Returns: { task_id }
    """
    try:
        if get_client is None:
            return jsonify({'error': 'Binance client unavailable'}), 400

        data = request.get_json(force=True, silent=True) or {}
        start_str = data.get('start') or request.args.get('start')
        end_str = data.get('end') or request.args.get('end')
        if not start_str or not end_str:
            return jsonify({'error': 'start and end required'}), 400

        start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
        if end_dt <= start_dt:
            return jsonify({'error': 'end must be after start'}), 400

        task_id = str(uuid.uuid4())
        _BACKFILL_TASKS[task_id] = {
            'status': 'running', 'synced': 0,
            'start': start_dt.isoformat(), 'end': end_dt.isoformat(),
            'progress': 0
        }

        def worker(task_id_local: str):
            try:
                client = get_client()
                symbol = 'BTCUSDT'
                start_ms = int(start_dt.timestamp() * 1000)
                end_ms = int(end_dt.timestamp() * 1000)
                limit = 1000
                from_id = None
                total_inserted = 0
                last_time = start_ms

                while True:
                    params = {'symbol': symbol, 'limit': limit, 'startTime': start_ms, 'endTime': end_ms}
                    if from_id:
                        params['fromId'] = from_id
                    trades = client.get_my_trades(**params)
                    if not trades:
                        break

                    rows = []
                    for t in trades:
                        tr_time = int(t['time'])
                        last_time = max(last_time, tr_time)
                        rows.append((
                            int(t['id']), symbol, int(t.get('orderId') or 0),
                            float(t['price'] or 0), float(t['qty'] or 0),
                            float(t.get('quoteQty') or (float(t['price'] or 0) * float(t['qty'] or 0))),
                            float(t.get('commission') or 0), str(t.get('commissionAsset') or ''),
                            1 if t.get('isBuyer') else 0, 1 if t.get('isMaker') else 0, 1 if t.get('isBestMatch') else 0,
                            datetime.fromtimestamp(tr_time/1000.0)
                        ))

                    with get_db_cursor() as (cursor, db):
                        cursor.executemany(
                            """
                            INSERT IGNORE INTO binance_trades
                            (trade_id, symbol, order_id, price, qty, quote_qty, commission, commission_asset,
                             is_buyer, is_maker, is_best_match, trade_time)
                            VALUES
                            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            rows
                        )
                        total_inserted += cursor.rowcount
                        db.commit()

                    if len(trades) < limit:
                        break
                    from_id = int(trades[-1]['id']) + 1
                    # Update progress by time coverage
                    covered = max(0, last_time - start_ms)
                    total = max(1, end_ms - start_ms)
                    _BACKFILL_TASKS[task_id_local]['progress'] = min(99, int(covered * 100 / total))
                    _BACKFILL_TASKS[task_id_local]['synced'] = total_inserted

                _BACKFILL_TASKS[task_id_local]['synced'] = total_inserted
                _BACKFILL_TASKS[task_id_local]['progress'] = 100
                _BACKFILL_TASKS[task_id_local]['status'] = 'done'
            except Exception as e:
                _BACKFILL_TASKS[task_id_local]['status'] = 'error'
                _BACKFILL_TASKS[task_id_local]['error'] = str(e)

        Thread(target=worker, args=(task_id,), daemon=True).start()
        return jsonify({'task_id': task_id})
    except Exception as e:
        logging.error(f"Backfill start error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sync_trades_progress')
def api_sync_trades_progress():
    task_id = request.args.get('task_id')
    if not task_id or task_id not in _BACKFILL_TASKS:
        return jsonify({'error': 'invalid task_id'}), 400
    return jsonify(_BACKFILL_TASKS[task_id])


# ====== Reconcile purchase_history with binance_trades ======
@app.route('/api/reconcile_trades', methods=['POST'])
def api_reconcile_trades():
    """Attach schedule_id to binance_trades by matching purchase_history via order_id/time.
    1) Ensure column schedule_id exists in binance_trades
    2) Match by order_id
    3) Fallback: match by near time (±5m) and similar amount when is_buyer=1
    """
    try:
        updated = 0
        with get_db_cursor() as (cursor, db):
            # 1) Ensure column exists
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'binance_trades' AND COLUMN_NAME = 'schedule_id'
            """, (os.getenv('DB_NAME'),))
            if cursor.fetchone()[0] == 0:
                cursor.execute("ALTER TABLE binance_trades ADD COLUMN schedule_id INT NULL, ADD INDEX idx_sched (schedule_id)")
                db.commit()

            # 2) Match by order_id
            cursor.execute(
                """
                UPDATE binance_trades bt
                JOIN purchase_history ph ON ph.order_id = bt.order_id
                SET bt.schedule_id = ph.schedule_id
                WHERE bt.symbol = 'BTCUSDT' AND bt.schedule_id IS NULL AND ph.schedule_id IS NOT NULL
                """
            )
            updated += cursor.rowcount
            db.commit()

            # 3) Fallback time-based for buys only where order_id=0 or mismatch
            cursor.execute(
                """
                SELECT bt.trade_id, bt.trade_time, bt.quote_qty
                FROM binance_trades bt
                WHERE bt.symbol='BTCUSDT' AND bt.is_buyer=1 AND (bt.schedule_id IS NULL)
                ORDER BY bt.trade_time ASC
                LIMIT 2000
                """
            )
            to_check = cursor.fetchall()

            for trade_id, tr_time, q_qty in to_check:
                # find nearest purchase within ±5 minutes and similar amount (±5% or ±1 USDT)
                cursor.execute(
                    """
                    SELECT id, schedule_id FROM purchase_history
                    WHERE purchase_time BETWEEN %s AND %s
                    ORDER BY ABS(TIMESTAMPDIFF(SECOND, purchase_time, %s)) ASC
                    LIMIT 1
                    """,
                    (tr_time - timedelta(minutes=5), tr_time + timedelta(minutes=5), tr_time)
                )
                row = cursor.fetchone()
                if not row:
                    continue
                ph_id, sched_id = row
                if not sched_id:
                    continue
                # Optional: amount match check
                cursor.execute("SELECT usdt_amount FROM purchase_history WHERE id=%s", (ph_id,))
                amt_row = cursor.fetchone()
                if not amt_row:
                    continue
                usdt_amt = float(amt_row[0] or 0.0)
                tol = max(1.0, usdt_amt * 0.05)
                if abs(float(q_qty or 0.0) - usdt_amt) > tol:
                    continue
                cursor.execute("UPDATE binance_trades SET schedule_id=%s WHERE trade_id=%s AND schedule_id IS NULL", (sched_id, trade_id))
                updated += cursor.rowcount
            db.commit()

        return jsonify({'updated': updated})
    except Exception as e:
        logging.error(f"Reconcile error: {e}")
        return jsonify({'updated': 0, 'error': str(e)}), 500

# Final catch-all for unknown API routes to always return JSON instead of HTML
@app.route('/api/<path:subpath>')
def api_not_found(subpath):
    return jsonify({'ok': False, 'error': 'not_found', 'path': f'/api/{subpath}'}), 404

# ====== Application Startup ======
if __name__ == '__main__':
    # Single-instance guard to avoid multiple servers on the same port
    # This prevents flapping between different app versions.
    try:
        import fcntl  # type: ignore
        _lock_fh = open('web.lock', 'w')
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
    except Exception:
        # If we cannot acquire the lock, exit silently to avoid duplicate servers
        print('Another instance appears to be running. Exiting.')
        raise SystemExit(0)
    scheduler = None  # Initialize scheduler variable
    
    try:
        logging.info("🚀 Starting BTC DCA Dashboard...")
        
        # ตรวจสอบและ migrate ข้อมูล
        if migrate_data_if_needed():
            logging.info("✅ Data migration check completed")
        else:
            logging.error("❌ Data migration failed")
            exit(1)
            
        # เริ่ม BackgroundScheduler
        scheduler = BackgroundScheduler()
        
        # Status check every 30 seconds
        scheduler.add_job(
            func=check_scheduler_status,
            trigger='interval',
            seconds=30,
            id='scheduler_status_check',
            name='Check Scheduler Status',
            replace_existing=True
        )
        
        # Log cleanup every hour
        scheduler.add_job(
            func=cleanup_old_logs,
            trigger='interval',
            hours=1,
            id='log_cleanup',
            name='Cleanup Old Logs',
            replace_existing=True
        )
        
        # Cache refresh every 5 minutes
        scheduler.add_job(
            func=update_cache_schedules,
            trigger='interval',
            minutes=5,
            id='cache_refresh',
            name='Refresh Cache',
            replace_existing=True
        )
        
        scheduler.start()
        logging.info("📋 Background scheduler started with 3 jobs")

        # แสดงข้อมูลระบบ
        logging.info(f"🏠 Server will run on http://0.0.0.0:5001")
        logging.info(f"📊 Admin panel available at http://0.0.0.0:5001/admin")
        logging.info(f"🔍 Health check at http://0.0.0.0:5001/health")

        # รัน Flask app
        socketio.run(
            app, 
            host='0.0.0.0', 
            port=5001, 
            debug=False,
            use_reloader=False,
            log_output=True
        )
        
    except KeyboardInterrupt:
        logging.info("🛑 Application stopped by user")
    except Exception as e:
        logging.error(f"💥 Failed to start application: {e}")
        raise
    finally:
        if scheduler and scheduler.running:
            scheduler.shutdown()
            logging.info("📋 Background scheduler shutdown")
        logging.info("👋 BTC DCA Dashboard stopped")
