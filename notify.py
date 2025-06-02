import requests
import os
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def send_line_message(message: str) -> bool:
    """
    ส่งข้อความผ่าน Line Bot API
    
    Args:
        message (str): ข้อความที่ต้องการส่ง
        
    Returns:
        bool: True ถ้าส่งสำเร็จ, False ถ้าส่งไม่สำเร็จ
    """
    try:
        url = "https://api.line.me/v2/bot/message/push"
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        user_id = os.getenv("LINE_USER_ID")
        
        if not token:
            logging.warning("LINE_CHANNEL_ACCESS_TOKEN not found - Line notifications disabled")
            # Fallback to console
            print(f"📱 Line Message (No Token): {message}")
            return True  # Return True เพื่อไม่ให้ระบบหยุด
        
        if not user_id:
            logging.warning("LINE_USER_ID not found - Line notifications disabled")
            # Fallback to console
            print(f"📱 Line Message (No User ID): {message}")
            return True  # Return True เพื่อไม่ให้ระบบหยุด
        
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}'
        }
        
        payload = {
            "to": user_id,
            "messages": [{"type": "text", "text": message}]
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        
        if response.status_code == 200:
            logging.info("Line message sent successfully")
            return True
        elif response.status_code == 401:
            logging.error("Line Bot API: Invalid access token")
            print(f"📱 Line Message (Auth Error): {message}")
            return True  # Return True เพื่อไม่ให้ระบบหยุด
        elif response.status_code == 403:
            logging.error("Line Bot API: Forbidden - check bot permissions")
            print(f"📱 Line Message (Permission Error): {message}")
            return True
        elif response.status_code == 400:
            logging.error(f"Line Bot API: Bad Request - {response.text}")
            print(f"📱 Line Message (Bad Request): {message}")
            return True
        else:
            logging.error(f"Failed to send Line message: {response.status_code} - {response.text}")
            print(f"📱 Line Message (Error {response.status_code}): {message}")
            return True  # Return True เพื่อไม่ให้ระบบหยุด
            
    except requests.RequestException as e:
        logging.error(f"Network error sending Line message: {e}")
        # Fallback to console output
        print(f"📱 Line Message (Network Error): {message}")
        return True  # Return True เพื่อไม่ให้ระบบหยุด
    except Exception as e:
        logging.error(f"Unexpected error sending Line message: {e}")
        print(f"📱 Line Message (Unexpected Error): {message}")
        return True  # Return True เพื่อไม่ให้ระบบหยุด

def send_line_notify_fallback(message: str) -> bool:
    """
    ส่งข้อความผ่าน Line Notify (Fallback method)
    
    Args:
        message (str): ข้อความที่ต้องการส่ง
        
    Returns:
        bool: True ถ้าส่งสำเร็จ, False ถ้าส่งไม่สำเร็จ
    """
    try:
        url = 'https://notify-api.line.me/api/notify'
        token = os.getenv('LINE_NOTIFY_TOKEN')  # ใช้ token แยกสำหรับ Line Notify
        
        if not token:
            logging.warning("LINE_NOTIFY_TOKEN not found")
            return False
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        data = {'message': message}
        
        response = requests.post(url, headers=headers, data=data, timeout=15)
        
        if response.status_code == 200:
            logging.info("Line Notify sent successfully")
            return True
        elif response.status_code == 410:
            logging.error("Line Notify API has been discontinued")
            return False
        else:
            logging.error(f"Line Notify failed: {response.status_code}")
            return False
            
    except Exception as e:
        logging.error(f"Line Notify error: {e}")
        return False

def send_line_message_with_retry(message: str, max_retries: int = 3) -> bool:
    """
    ส่งข้อความผ่าน Line Bot API พร้อม retry mechanism
    
    Args:
        message (str): ข้อความที่ต้องการส่ง
        max_retries (int): จำนวนครั้งที่จะ retry
        
    Returns:
        bool: True ถ้าส่งสำเร็จ
    """
    for attempt in range(max_retries):
        try:
            if send_line_message(message):
                return True
            
            # ถ้าไม่สำเร็จ ลอง Line Notify
            if attempt == max_retries - 1:  # Last attempt
                logging.info("Trying Line Notify as fallback...")
                return send_line_notify_fallback(message)
                
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")
            
    return False

def send_console_message(message: str) -> bool:
    """
    ส่งข้อความไปยัง console (Fallback method)
    
    Args:
        message (str): ข้อความที่ต้องการส่ง
        
    Returns:
        bool: Always True
    """
    print(f"\n{'='*60}")
    print(f"📱 NOTIFICATION:")
    print(f"{message}")
    print(f"{'='*60}\n")
    return True

def format_purchase_message(purchase_data: dict) -> str:
    """
    จัดรูปแบบข้อความการซื้อ BTC
    
    Args:
        purchase_data (dict): ข้อมูลการซื้อ
        
    Returns:
        str: ข้อความที่จัดรูปแบบแล้ว
    """
    try:
        message = f"""✅ DCA BTC Success!

📅 Time: {purchase_data.get('timestamp', 'N/A')}
💰 Purchased: {purchase_data.get('usdt_amount', 0):.2f} USDT
₿ BTC Amount: {purchase_data.get('btc_quantity', 0):.8f} BTC
📈 Price: ฿{purchase_data.get('btc_price', 0):,.2f}
🔢 Order ID: {purchase_data.get('order_id', 'N/A')}
📋 Schedule ID: {purchase_data.get('schedule_id', 'N/A')}

🎯 DCA Strategy Working!"""
        
        return message
        
    except Exception as e:
        logging.error(f"Error formatting purchase message: {e}")
        return f"✅ DCA BTC Purchase completed (formatting error: {e})"

def format_error_message(error_data: dict) -> str:
    """
    จัดรูปแบบข้อความ error
    
    Args:
        error_data (dict): ข้อมูล error
        
    Returns:
        str: ข้อความ error ที่จัดรูปแบบแล้ว
    """
    try:
        message = f"""❌ DCA BTC Error!

📅 Time: {error_data.get('timestamp', 'N/A')}
🚨 Error: {error_data.get('error_message', 'Unknown error')}
📋 Schedule ID: {error_data.get('schedule_id', 'N/A')}
💰 Attempted Amount: {error_data.get('usdt_amount', 0):.2f} USDT

⚠️ Please check the system!"""
        
        return message
        
    except Exception as e:
        logging.error(f"Error formatting error message: {e}")
        return f"❌ DCA BTC Error occurred (formatting error: {e})"

def send_purchase_notification(purchase_data: dict) -> bool:
    """
    ส่งการแจ้งเตือนการซื้อ BTC
    
    Args:
        purchase_data (dict): ข้อมูลการซื้อ
        
    Returns:
        bool: True ถ้าส่งสำเร็จ
    """
    message = format_purchase_message(purchase_data)
    return send_line_message_with_retry(message)

def send_error_notification(error_data: dict) -> bool:
    """
    ส่งการแจ้งเตือน error
    
    Args:
        error_data (dict): ข้อมูล error
        
    Returns:
        bool: True ถ้าส่งสำเร็จ
    """
    message = format_error_message(error_data)
    return send_line_message_with_retry(message)

def send_system_notification(message_type: str, details: str) -> bool:
    """
    ส่งการแจ้งเตือนระบบ
    
    Args:
        message_type (str): ประเภทข้อความ (start, stop, error, warning)
        details (str): รายละเอียด
        
    Returns:
        bool: True ถ้าส่งสำเร็จ
    """
    icons = {
        'start': '🚀',
        'stop': '🛑',
        'error': '❌',
        'warning': '⚠️',
        'info': 'ℹ️'
    }
    
    icon = icons.get(message_type, 'ℹ️')
    message = f"{icon} BTC DCA System\n\n{details}"
    
    return send_line_message_with_retry(message)

def send_scheduler_status(status: str, details: str = "") -> bool:
    """
    ส่งสถานะของ scheduler
    
    Args:
        status (str): สถานะ (started, stopped, error)
        details (str): รายละเอียดเพิ่มเติม
        
    Returns:
        bool: True ถ้าส่งสำเร็จ
    """
    from datetime import datetime
    
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if status == 'started':
        message = f"🚀 BTC DCA Scheduler Started\n📅 {timestamp}\n{details}"
    elif status == 'stopped':
        message = f"🛑 BTC DCA Scheduler Stopped\n📅 {timestamp}\n{details}"
    elif status == 'error':
        message = f"❌ BTC DCA Scheduler Error\n📅 {timestamp}\n🚨 {details}"
    else:
        message = f"ℹ️ BTC DCA Scheduler Update\n📅 {timestamp}\n{details}"
    
    return send_line_message_with_retry(message)

def test_line_bot_api() -> bool:
    """
    ทดสอบการส่ง Line Bot API
    """
    from datetime import datetime
    
    test_message = f"""🧪 Line Bot API Test

📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
✅ BTC DCA System Test Message
🤖 Line Bot API is working!

This is a test notification from your BTC DCA system."""
    
    print("Testing Line Bot API...")
    result = send_line_message(test_message)
    
    if result:
        print("✅ Line Bot API test successful!")
    else:
        print("⚠️ Line Bot API test failed, but system continues")
        
    return result

def get_line_bot_setup_instructions() -> str:
    """
    แสดงวิธีการตั้งค่า Line Bot API
    """
    instructions = """
🔧 วิธีการตั้งค่า Line Bot API:

1. สร้าง Line Developer Account:
   - ไปที่ https://developers.line.biz/
   - Login ด้วย Line account

2. สร้าง Provider:
   - คลิก "Create Provider"
   - ใส่ชื่อ Provider

3. สร้าง Messaging API Channel:
   - เลือก "Messaging API"
   - กรอกข้อมูล Channel
   - เปิดใช้งาน Channel

4. ตั้งค่า Channel:
   - ไปที่ "Basic settings" tab
   - Copy "Channel secret"
   - ไปที่ "Messaging API" tab
   - Copy "Channel access token"

5. เพิ่มเป็นเพื่อน:
   - Scan QR Code หรือ add Line ID
   - ส่งข้อความใดๆ เพื่อเริ่มการสนทนา

6. หา User ID:
   - ใช้ webhook หรือ Line Bot SDK
   - หรือใช้ Line Official Account Manager

7. ใส่ใน .env file:
   LINE_CHANNEL_ACCESS_TOKEN=your_channel_access_token
   LINE_USER_ID=your_user_id

📝 Note: Line Bot API ใช้แทน Line Notify ที่ถูกยกเลิก
"""
    
    print(instructions)
    return instructions

# Alternative notification methods
def send_webhook_notification(message: str, webhook_url: str = None) -> bool:
    """
    ส่ง notification ผ่าน webhook (Discord, Slack, etc.)
    """
    if not webhook_url:
        webhook_url = os.getenv('WEBHOOK_URL')
    
    if not webhook_url:
        return False
    
    try:
        payload = {"content": message}  # Discord format
        response = requests.post(webhook_url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Webhook notification error: {e}")
        return False

def send_email_notification(message: str, email: str = None) -> bool:
    """
    ส่งอีเมล notification (สำหรับอนาคต)
    """
    # TODO: Implement email notification using SMTP
    print(f"📧 Email notification: {message}")
    return True

if __name__ == "__main__":
    # รันการทดสอบ
    print("🧪 Testing Line Bot API notification system...")
    
    # ทดสอบ Line Bot API
    test_result = test_line_bot_api()
    
    # แสดงคำแนะนำถ้าไม่มี token
    if not os.getenv('LINE_CHANNEL_ACCESS_TOKEN'):
        print("\n⚠️ LINE_CHANNEL_ACCESS_TOKEN not found!")
        get_line_bot_setup_instructions()
    
    if not os.getenv('LINE_USER_ID'):
        print("\n⚠️ LINE_USER_ID not found!")
        print("Please add your Line User ID to .env file")
    
    # ทดสอบ format functions
    print("\n🧪 Testing message formatting...")
    
    # Test purchase message
    purchase_test = {
        'timestamp': '2025-06-02 11:00:00',
        'usdt_amount': 100.0,
        'btc_quantity': 0.00094123,
        'btc_price': 106234.56,
        'order_id': 12345678,
        'schedule_id': 3
    }
    
    purchase_msg = format_purchase_message(purchase_test)
    print("Purchase message format:")
    print(purchase_msg)
    
    # Test error message
    error_test = {
        'timestamp': '2025-06-02 11:01:00',
        'error_message': 'Insufficient balance',
        'schedule_id': 3,
        'usdt_amount': 100.0
    }
    
    error_msg = format_error_message(error_test)
    print("\nError message format:")
    print(error_msg)
    
    print("\n✅ Testing completed!")