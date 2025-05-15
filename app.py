from flask import Flask, render_template, request, redirect, g, flash
from flask_socketio import SocketIO, emit
import MySQLdb
import requests
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import logging
from apscheduler.schedulers.background import BackgroundScheduler

# ตั้งค่า logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

# โหลด environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app)

# ตรวจสอบ environment variables
required_env_vars = ['DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME', 'LINE_CHANNEL_ACCESS_TOKEN', 'LINE_USER_ID']
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    logging.error(f"Missing environment variables: {', '.join(missing_vars)}")
    raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

# ตัวแปร global สำหรับติดตามสถานะและเวลาแจ้งเตือน
last_scheduler_status = "Scheduler is running"
last_notify_time = None
NOTIFY_COOLDOWN = 300  # 5 นาที (วินาที)

# ====== ฟังก์ชันส่ง Line Notify ======
def send_line_notify(message):
    try:
        url = 'https://notify-api.line.me/api/notify'
        token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
        headers = {'Authorization': f'Bearer {token}'}
        data = {'message': message}
        logging.debug(f"Sending Line Notify: {message}")
        response = requests.post(url, headers=headers, data=data)
        if response.status_code == 200:
            logging.info(f"Line Notify sent successfully: {message}")
        else:
            logging.error(f"Failed to send Line Notify: {response.status_code} {response.text}")
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Error sending Line Notify: {e}")
        return False

# ====== ฟังก์ชันตรวจสอบสถานะ Scheduler และแจ้งเตือน ======
def check_scheduler_status():
    global last_scheduler_status, last_notify_time
    try:
        health_check_port = os.getenv('HEALTH_CHECK_PORT', '8001')
        response = requests.get(f'http://localhost:{health_check_port}', timeout=2)
        current_status = response.text if response.status_code == 200 else 'Scheduler is not responding'
    except requests.RequestException as e:
        current_status = 'Scheduler is not responding'
        logging.debug(f"Scheduler check failed: {e}")

    # บันทึกการเปลี่ยนสถานะ
    if current_status != last_scheduler_status:
        logging.debug(f"Scheduler status changed: {last_scheduler_status} -> {current_status}")
        last_scheduler_status = current_status

    # ส่ง Line Notify ถ้า scheduler ไม่ตอบสนองและอยู่ในช่วง cooldown
    if current_status == 'Scheduler is not responding':
        current_time = datetime.now()
        if last_notify_time is None or (current_time - last_notify_time).total_seconds() >= NOTIFY_COOLDOWN:
            timestamp = current_time.strftime('%Y-%m-%d %H:%M:%S')
            message = f"⚠️ Scheduler Alert: Scheduler is not responding at {timestamp}"
            if send_line_notify(message):
                last_notify_time = current_time
                logging.info(f"Updated last_notify_time: {last_notify_time}")

# ====== ฟังก์ชันเชื่อมต่อฐานข้อมูล ======
def get_db_connection():
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

# ====== หน้าแสดงผลหลัก ======
@app.route('/')
def index():
    try:
        db = get_db_connection()
        cursor = db.cursor()

        # ตรวจสอบตาราง config
        cursor.execute("SHOW TABLES LIKE 'config'")
        if cursor.fetchone() is None:
            logging.warning("Table config not found, creating")
            cursor.execute("""
                CREATE TABLE config (
                    id INT PRIMARY KEY,
                    purchase_amount DECIMAL(10,2),
                    schedule_time TIME,
                    schedule_day VARCHAR(255)
                )
            """)
            cursor.execute(
                "INSERT INTO config (id, purchase_amount, schedule_time, schedule_day) "
                "VALUES (1, %s, %s, %s)",
                (50.00, '17:00:00', 'tuesday')
            )
            db.commit()

        # ตรวจสอบและสร้างแถวใน config ถ้าว่าง
        cursor.execute("SELECT COUNT(*) FROM config WHERE id = 1")
        if cursor.fetchone()[0] == 0:
            logging.warning("No config found, inserting default")
            cursor.execute(
                "INSERT INTO config (id, purchase_amount, schedule_time, schedule_day) "
                "VALUES (1, %s, %s, %s)",
                (50.00, '17:00:00', 'tuesday')
            )
            db.commit()

        # ดึงค่าการตั้งค่าการซื้อ
        cursor.execute("SELECT purchase_amount, schedule_day, schedule_time FROM config WHERE id = 1")
        config = cursor.fetchone()
        if config is None:
            logging.error("Config fetch returned None after insert")
            flash("Failed to initialize configuration.", 'error')
            config = (0.0, '', '')

        # แปลงเวลา
        schedule_time = ''
        if config[2]:
            try:
                schedule_time = str(config[2])[:5]  # ตัดเป็น HH:MM
                logging.debug(f"Schedule time formatted: {schedule_time}")
            except Exception as e:
                logging.error(f"Error formatting schedule_time: {e}")
                flash("Invalid schedule time format.", 'error')

        # ตรวจสอบตาราง purchase_history
        cursor.execute("SHOW TABLES LIKE 'purchase_history'")
        if cursor.fetchone() is None:
            logging.warning("Table purchase_history not found, creating")
            cursor.execute("""
                CREATE TABLE purchase_history (
                    id INT PRIMARY KEY AUTO_INCREMENT,
                    purchase_time DATETIME,
                    usdt_amount DECIMAL(10,2),
                    btc_quantity DECIMAL(10,8),
                    btc_price DECIMAL(10,2),
                    order_id VARCHAR(255)
                )
            """)
            db.commit()

        # ดึงประวัติการซื้อ
        cursor.execute("SELECT id, purchase_time, usdt_amount, btc_quantity, btc_price, order_id FROM purchase_history ORDER BY purchase_time DESC")
        history = cursor.fetchall()
        cursor.close()

        return render_template('index.html', config=(config[0], config[1], schedule_time), history=history)
    except Exception as e:
        logging.error(f"Error in index route: {e}")
        flash(f"Internal server error: {str(e)}", 'error')
        return render_template('index.html', config=(0.0, '', ''), history=[])

# ====== บันทึกค่าการตั้งค่าใหม่ ======
@app.route('/update', methods=['POST'])
def update():
    try:
        amount = request.form['amount']
        time_str = request.form['time']  # HH:MM
        days = request.form.getlist('day')

        float_amount = float(amount)
        if float_amount <= 0:
            flash("Amount must be positive.", 'error')
            return redirect('/')

        # ตรวจสอบรูปแบบเวลา
        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            flash("Invalid time format. Use HH:MM.", 'error')
            return redirect('/')

        # เพิ่มวินาทีให้เป็น HH:MM:SS
        schedule_time = time_str + ':00'

        schedule_day = ",".join([d.lower() for d in days])
        if not schedule_day:
            flash("Please select at least one day.", 'error')
            return redirect('/')

        db = get_db_connection()
        cursor = db.cursor()
        cursor.execute(
            "UPDATE config SET purchase_amount = %s, schedule_day = %s, schedule_time = %s WHERE id = 1",
            (float_amount, schedule_day, schedule_time)
        )
        if cursor.rowcount == 0:
            cursor.execute(
                "INSERT INTO config (id, purchase_amount, schedule_day, schedule_time) VALUES (1, %s, %s, %s)",
                (float_amount, schedule_day, schedule_time)
            )
        db.commit()
        cursor.close()
        logging.info(f"Config updated: amount={float_amount}, day={schedule_day}, time={schedule_time}")

        flash("Configuration updated successfully.", 'success')
        socketio.emit('config_update', {
            'amount': float_amount,
            'time': schedule_time,
            'day': schedule_day
        })
        return redirect('/')
    except Exception as e:
        logging.error(f"Error in update route: {e}")
        flash(f"Error updating config: {str(e)}", 'error')
        return redirect('/')

# ====== Endpoint ตรวจสอบสถานะ Scheduler ======
@app.route('/scheduler_status')
def scheduler_status():
    try:
        response = requests.get(f"http://localhost:{os.getenv('HEALTH_CHECK_PORT', '8001')}", timeout=2)
        if response.status_code == 200:
            logging.debug(f"Scheduler status: {response.text}")
            return {'status': response.text}
        logging.warning("Scheduler not responding")
        return {'status': 'Scheduler is not responding'}
    except requests.RequestException as e:
        logging.error(f"Error checking scheduler status: {e}")
        return {'status': 'Scheduler is not responding'}

# ====== Endpoint ทดสอบ Line Notify ======
@app.route('/test_line_notify')
def test_line_notify():
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    message = f"🔔 Test Line Notify: Sent at {timestamp}"
    if send_line_notify(message):
        flash("Test Line Notify sent successfully.", 'success')
    else:
        flash("Failed to send test Line Notify.", 'error')
    return redirect('/')

# ====== SocketIO: ส่งข้อมูลประวัติล่าสุด ======
@socketio.on('request_latest')
def handle_request_latest():
    try:
        db = get_db_connection()
        cursor = db.cursor()
        cursor.execute("SELECT purchase_time, usdt_amount, btc_quantity, btc_price, order_id FROM purchase_history ORDER BY purchase_time DESC LIMIT 10")
        results = cursor.fetchall()
        cursor.close()

        data = [
            {
                "purchase_time": str(row[0]),
                "usdt_amount": float(row[1]),
                "btc_quantity": float(row[2]) if row[2] is not None else 0.0,
                "btc_price": float(row[3]) if row[3] is not None else 0.0,
                "order_id": row[4]
            }
            for row in results
        ]
        logging.debug("Emitting latest_data")
        socketio.emit('latest_data', data)
    except Exception as e:
        logging.error(f"Error fetching latest data: {e}")
        socketio.emit('latest_data', {'error': 'Failed to fetch data'})

# ====== SocketIO: การเชื่อมต่อ ======
@socketio.on('connect')
def handle_connect():
    logging.info("Client connected")
    handle_request_latest()

# ====== เริ่มต้น Flask + SocketIO และ Scheduler ======
if __name__ == '__main__':
    try:
        # เริ่ม BackgroundScheduler สำหรับตรวจสอบสถานะ
        scheduler = BackgroundScheduler()
        scheduler.add_job(check_scheduler_status, 'interval', seconds=30)
        scheduler.start()
        logging.info("Background scheduler started for status checks")

        socketio.run(app, host='0.0.0.0', port=5001, debug=True)
    except Exception as e:
        logging.error(f"Failed to start Flask server or scheduler: {e}")
        raise
    finally:
        # ปิด scheduler เมื่อ Flask ปิด
        if 'scheduler' in locals():
            scheduler.shutdown()
            logging.info("Background scheduler shutdown")
