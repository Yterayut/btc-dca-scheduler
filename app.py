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
socketio = SocketIO(app, cors_allowed_origins="*")

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
            cursor.execute("""
                SELECT id, schedule_time, schedule_day, purchase_amount, is_active 
                FROM schedules 
                ORDER BY is_active DESC, schedule_time
            """)
            schedules = cursor.fetchall()
            
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
                             stats=stats)
        
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

@app.route('/add_schedule', methods=['POST'])
@handle_db_errors
def add_schedule():
    """เพิ่มกำหนดการใหม่"""
    amount = request.form['amount']
    time_str = request.form['time']
    days = request.form.getlist('day')
    is_active = request.form.get('is_active', '0') == '1'

    # Validate input
    errors = validate_schedule_data(amount, time_str, days)
    if errors:
        for error in errors:
            flash(error, 'error')
        return redirect('/')

    float_amount = float(amount)
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
        'is_active': is_active
    })
    socketio.emit('total_amount_update', {'total_amount': get_total_active_amount()})
    
    return redirect('/')

@app.route('/edit_schedule/<int:schedule_id>', methods=['POST'])
@handle_db_errors
def edit_schedule(schedule_id):
    """แก้ไขกำหนดการ"""
    amount = request.form['amount']
    time_str = request.form['time']
    days = request.form.getlist('day')
    is_active = request.form.get('is_active', '0') == '1'

    # Validate input
    errors = validate_schedule_data(amount, time_str, days)
    if errors:
        for error in errors:
            flash(error, 'error')
        return redirect('/')

    float_amount = float(amount)
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

# ====== Application Startup ======
if __name__ == '__main__':
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
