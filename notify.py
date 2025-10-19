import os
import logging
import time
from datetime import datetime, timezone
from typing import Iterable

import requests
from dotenv import load_dotenv

from notifications.line_flex import build_basic_bubble, make_flex_message

# Load environment variables
load_dotenv()


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _parse_allowlist(value: str | None) -> set[str]:
    if not value:
        return set()
    tokens: Iterable[str] = (tok.strip().lower() for tok in value.split(','))
    return {tok for tok in tokens if tok}



def _refresh_flex_settings() -> None:
    global LINE_USE_FLEX, LINE_FLEX_ALLOWLIST
    LINE_USE_FLEX = _env_flag('LINE_USE_FLEX', False)
    LINE_FLEX_ALLOWLIST = _parse_allowlist(os.getenv('LINE_FLEX_ALLOWLIST'))


_refresh_flex_settings()


def flex_allowed(channel: str | None) -> bool:
    """Return True if Flex message delivery is permitted for a given channel."""
    if not LINE_USE_FLEX:
        return False
    if not LINE_FLEX_ALLOWLIST:
        return True
    if not channel:
        return False
    return str(channel).strip().lower() in LINE_FLEX_ALLOWLIST

# Exchange name mapping for user-facing notifications
_EXCHANGE_LABELS = {
    'binance': 'Binance',
    'okx': 'OKX',
}

_REASON_LABELS = {
    'sell_percent_zero': 'Configured percent is 0',
    'no_balance': 'No free BTC balance',
    'below_minQty': 'Quantity below minQty',
    'below_minNotional': 'Notional below minimum',
    'below_min_notional': 'Notional below minimum',
    'depth_insufficient': 'Orderbook depth below guard threshold',
    'depth_guard': 'Depth guard triggered',
    'twap_deviation': 'Price deviates from TWAP beyond guard',
    'twap_guard': 'TWAP guard triggered',
    'notional_cap': 'Notional exceeds configured cap',
}


def _reason_text(reason: str | None) -> str:
    if not reason:
        return 'Unspecified'
    return _REASON_LABELS.get(str(reason), str(reason))


def _utc_stamp(value=None) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    if value:
        return str(value)
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def _append_meta(lines: list[str], data: dict) -> None:
    for entry in _meta_entries(data):
        lines.append(entry)


def _meta_entries(data: dict) -> list[str]:
    rid = data.get('request_id')
    dedupe = data.get('dedupe_key')
    entries: list[str] = []
    if rid:
        entries.append(f"Req: {rid}")
    dedupe = data.get('dedupe_key')
    if dedupe:
        entries.append(f"Dedupe: {dedupe}")
    return entries


def format_exchange_label(name: str | None) -> str:
    """Return a human-friendly exchange name for notification text."""
    if not name:
        return 'Unknown'
    key = str(name).strip()
    if not key:
        return 'Unknown'
    return _EXCHANGE_LABELS.get(key.lower(), key.upper())


def _format_holdings_line(holdings: dict | None, meta: dict | None = None) -> str:
    """Render holdings dict into a single notification line."""
    if not isinstance(holdings, dict) or not holdings:
        return ""

    now = time.time()
    parts: list[str] = []
    oldest_age = 0.0
    has_stale = False
    meta_errors: list[str] = []

    for asset, entry in sorted(holdings.items()):
        if not isinstance(entry, dict):
            continue
        try:
            free = float(entry.get('free') or 0.0)
        except (TypeError, ValueError):
            free = 0.0
        try:
            locked = float(entry.get('locked') or 0.0)
        except (TypeError, ValueError):
            locked = 0.0

        part = f"{asset} {free:.6f}"
        if locked:
            part += f" (+{locked:.6f} locked)"
        parts.append(part)

        if entry.get('stale'):
            has_stale = True
        updated_at = entry.get('updated_at')
        if isinstance(updated_at, (int, float)):
            age = max(0.0, now - float(updated_at))
            oldest_age = max(oldest_age, age)
        error_text = entry.get('error')
        if isinstance(error_text, str) and error_text:
            meta_errors.append(error_text)

    if isinstance(meta, dict):
        meta_errors.extend(
            str(msg) for msg in (meta.get('errors') or {}).values() if isinstance(msg, str) and msg
        )

    if not parts and not meta_errors:
        return ""

    suffix_bits: list[str] = []
    if has_stale:
        suffix_bits.append(f"cached {int(oldest_age)}s" if oldest_age else "cached")
    if meta_errors and not suffix_bits:
        suffix_bits.append("error")

    line = "Holdings: " + (" | ".join(parts) if parts else "unavailable")
    if suffix_bits:
        line += f" ({', '.join(suffix_bits)})"
    return line

def _channel_credentials() -> tuple[str | None, str | None]:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.getenv("LINE_USER_ID")
    return token, user_id


def _push_line_messages(messages: list[dict]) -> bool:
    url = "https://api.line.me/v2/bot/message/push"
    token, user_id = _channel_credentials()

    if not token:
        logging.warning("LINE_CHANNEL_ACCESS_TOKEN not found - Line notifications disabled")
        print(f"📱 Line Message (No Token): {messages}")
        return False

    if not user_id:
        logging.warning("LINE_USER_ID not found - Line notifications disabled")
        print(f"📱 Line Message (No User ID): {messages}")
        return False

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}'
    }

    payload = {
        "to": user_id,
        "messages": messages,
    }

    response = requests.post(url, headers=headers, json=payload, timeout=15)

    if response.status_code == 200:
        logging.info("Line message sent successfully")
        return True
    elif response.status_code == 401:
        logging.error("Line Bot API: Invalid access token")
        print(f"📱 Line Message (Auth Error): {messages}")
        return False
    elif response.status_code == 403:
        logging.error("Line Bot API: Forbidden - check bot permissions")
        print(f"📱 Line Message (Permission Error): {messages}")
        return False
    elif response.status_code == 400:
        logging.error(f"Line Bot API: Bad Request - {response.text}")
        print(f"📱 Line Message (Bad Request): {messages}")
        return False
    else:
        logging.error(f"Failed to send Line message: {response.status_code} - {response.text}")
        print(f"📱 Line Message (Error {response.status_code}): {messages}")
        return False


def send_line_message(message: str) -> bool:
    """
    ส่งข้อความผ่าน Line Bot API
    
    Args:
        message (str): ข้อความที่ต้องการส่ง
        
    Returns:
        bool: True ถ้าส่งสำเร็จ, False ถ้าส่งไม่สำเร็จ
    """
    try:
        return _push_line_messages([{"type": "text", "text": message}])
    except requests.RequestException as e:
        logging.error(f"Network error sending Line message: {e}")
        print(f"📱 Line Message (Network Error): {message}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error sending Line message: {e}")
        print(f"📱 Line Message (Unexpected Error): {message}")
        return False

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

            if attempt == max_retries - 1:
                logging.info("Trying Line Notify as fallback...")
                return send_line_notify_fallback(message)

        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")

        if attempt < max_retries - 1:
            delay = min(2 ** attempt, 30)
            time.sleep(delay)

    return False


def send_line_flex_message(flex_message: dict) -> bool:
    """Send a Flex payload (already wrapped with type/altText/contents)."""
    try:
        return _push_line_messages([flex_message])
    except requests.RequestException as e:
        logging.error(f"Network error sending Flex message: {e}")
        print(f"📱 Line Flex (Network Error): {flex_message.get('altText')}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error sending Flex message: {e}")
        return False


def send_line_flex_with_retry(flex_message: dict, max_retries: int = 3) -> bool:
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        if send_line_flex_message(flex_message):
            return True
        time.sleep(min(2 ** attempt, 10))
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

def notify_s4_rotation(payload: dict) -> bool:
    """Send a LINE notification when S4 rotation action is emitted."""
    try:
        amount = float(payload.get('amount_usd') or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    from_leg = str(payload.get('from') or 'BTC').upper()
    to_leg = str(payload.get('to') or 'GOLD').upper()
    cdc_status = str(payload.get('cdc_status') or 'unknown').upper()
    btc_price = payload.get('btc_price')
    gold_price = payload.get('gold_price')
    notes = payload.get('notes') or {}

    exchange = str(payload.get('exchange') or 'BINANCE').upper()
    lines = [
        "🔄 S4 Rotation Triggered",
        f"{from_leg} → {to_leg} | {amount:,.2f} USDT",
        f"CDC: {cdc_status} | Exchange: {exchange}",
    ]
    try:
        if btc_price:
            lines.append(f"BTC: {float(btc_price):,.2f} USD")
        if gold_price:
            lines.append(f"GOLD: {float(gold_price):,.2f} USD")
    except (TypeError, ValueError):
        pass

    if isinstance(notes, dict) and notes:
        delta = notes.get('delta_pct')
        target = notes.get('target_btc_pct')
        if delta is not None:
            try:
                delta_val = float(delta)
                lines.append(f"Δ BTC weight: {delta_val:.2f}%")
            except (TypeError, ValueError):
                pass
        if target is not None:
            try:
                target_val = float(target)
                current = float(notes.get('exposure_btc_pct', 0))
                lines.append(f"Target BTC weight: {target_val:.2f}%")
                lines.append(f"Explanation: current BTC weight {current:.2f}% vs target {target_val:.2f}% → rotate towards {to_leg}")
            except (TypeError, ValueError):
                pass

    executed = payload.get('executed')
    if isinstance(executed, dict):
        sell = executed.get('sell_order') or {}
        buy = executed.get('buy_order') or {}
        try:
            lines.append(f"Sell: {sell.get('symbol','-')} qty {float(sell.get('executed_qty') or 0):.6f} → {float(sell.get('quote_usd') or 0):,.2f} USDT")
        except (TypeError, ValueError):
            pass
        try:
            avg = float(buy.get('avg_price') or 0)
            qty = float(buy.get('executed_qty') or 0)
            lines.append(f"Buy: {buy.get('symbol','-')} qty {qty:.6f} @ {avg:,.2f}")
        except (TypeError, ValueError):
            pass
        realized = executed.get('realized_usd')
        if realized:
            try:
                lines.append(f"Realized notional: {float(realized):,.2f} USDT")
            except (TypeError, ValueError):
                pass

    message = "\n".join(lines)
    return send_line_message_with_retry(message)


def notify_s4_dca_buy(payload: dict) -> bool:
    """Notify when S4 performs a DCA buy on the active leg."""
    try:
        usdt = float(payload.get('usdt') or 0.0)
    except (TypeError, ValueError):
        usdt = 0.0
    try:
        qty = float(payload.get('qty') or 0.0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        price = float(payload.get('price') or 0.0)
    except (TypeError, ValueError):
        price = 0.0

    asset = str(payload.get('asset') or 'BTC').upper()
    exchange = str(payload.get('exchange') or 'BINANCE').upper()
    dry_run = bool(payload.get('dry_run'))
    schedule_id = payload.get('schedule_id')
    schedule_label = payload.get('schedule_label')
    order_id = payload.get('order_id')
    cdc_status = payload.get('cdc_status')
    try:
        fee_usdt = float(payload.get('fee_usdt') or 0.0)
    except (TypeError, ValueError):
        fee_usdt = 0.0
    fee_asset = payload.get('fee_asset')
    try:
        fee_asset_amount = float(payload.get('fee_asset_amount') or 0.0)
    except (TypeError, ValueError):
        fee_asset_amount = 0.0

    lines = [
        "S4 DCA Buy",
        f"Asset: {asset} | Exchange: {exchange}",
        f"Amount: {usdt:,.2f} USDT",
    ]
    if qty and price:
        lines.append(f"Qty: {qty:.6f} {asset} @ {price:,.2f}")
    elif qty:
        lines.append(f"Qty: {qty:.6f} {asset}")
    elif price:
        lines.append(f"Avg: {price:,.2f}")

    status_bits: list[str] = []
    if schedule_id:
        status_bits.append(f"Schedule: #{schedule_id}")
    elif schedule_label:
        status_bits.append(f"Schedule: {schedule_label}")
    if cdc_status:
        status_bits.append(f"CDC: {str(cdc_status).upper()}")
    if status_bits:
        lines.append(" | ".join(status_bits))
    mode_bits: list[str] = []
    if dry_run:
        mode_bits.append("Mode: DRY RUN")
    else:
        mode_bits.append("Mode: LIVE")
    if order_id:
        mode_bits.append(f"Order: {order_id}")
    if mode_bits:
        lines.append(" | ".join(mode_bits))
    fee_lines: list[str] = []
    if fee_usdt:
        fee_lines.append(f"{fee_usdt:,.6f} USDT")
    if fee_asset_amount and fee_asset:
        fee_lines.append(f"{fee_asset_amount:,.6f} {str(fee_asset).upper()}")
    if fee_lines:
        lines.append("Fee: " + " + ".join(fee_lines))

    holdings_line = _format_holdings_line(
        payload.get('holdings'),
        payload.get('holdings_meta'),
    )
    if holdings_line:
        lines.append(holdings_line)

    return send_line_message_with_retry("\n".join(lines))

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

def notify_exchange_changed(exchange: str, flags: dict | None = None) -> bool:
    """
    แจ้งเตือนเมื่อเปลี่ยน Exchange สำหรับ DCA (global)
    flags: { 'testnet': bool, 'dry_run': bool }
    """
    suffix = []
    try:
        if flags:
            if flags.get('testnet'):
                suffix.append('TESTNET')
            if flags.get('dry_run'):
                suffix.append('DRY_RUN')
    except Exception:
        pass
    suffix_text = f" ({'/'.join(suffix)})" if suffix else ''
    ex = (exchange or '').upper()
    msg = f"🔄 เปลี่ยน Exchange สำหรับ DCA เป็น: {ex}{suffix_text}"
    return send_line_message_with_retry(msg)

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

# ====== Strategy notifications (stubs ready to use) ======
def notify_cdc_transition(prev_status: str, curr_status: str) -> bool:
    icon = '🟢' if (curr_status or '').lower() == 'up' else '🔻'
    lines = [
        f"{icon} CDC Action Zone Transition (1D)",
        f"Prev: {prev_status or 'unknown'}",
        f"Curr: {curr_status or 'unknown'}",
        f"Time: {_utc_stamp()}",
    ]
    return send_line_message_with_retry("\n".join(lines))

def notify_half_sell_executed(data: dict) -> bool:
    pct = data.get('pct')
    header = f"✅ Half-Sell {pct}% Executed" if pct is not None else "✅ Half-Sell Executed"
    lines = [
        header,
        f"Time: {_utc_stamp(data.get('timestamp'))}",
        f"Exchange: {format_exchange_label(data.get('exchange'))}",
        f"Qty: {data.get('btc_qty', 0):.8f} BTC",
        f"Price: ฿{data.get('price', 0):,.2f}",
        f"Proceeds: {data.get('usdt', 0):,.2f} USDT",
        f"Order: {data.get('order_id', 'N/A')}",
    ]
    if data.get('cdc_status'):
        lines.append(f"CDC: {str(data['cdc_status']).upper()}")
    holdings_line = _format_holdings_line(data.get('holdings'), data.get('holdings_meta'))
    if holdings_line:
        lines.append(holdings_line)
    _append_meta(lines, data)
    return send_line_message_with_retry("\n".join(lines))

def notify_half_sell_skipped(data: dict) -> bool:
    pct = data.get('pct')
    header = f"⚠️ Sell {pct}% Skipped" if pct is not None else "⚠️ Half-Sell Skipped"
    lines = [
        header,
        f"Time: {_utc_stamp(data.get('timestamp'))}",
        f"Reason: {_reason_text(data.get('reason'))}",
        f"BTC Free: {data.get('btc_free', 0):.8f}",
        f"stepSize: {data.get('step', '-')}",
        f"MinNotional: {data.get('min_notional', '-')}",
    ]
    if data.get('cdc_status'):
        lines.append(f"CDC: {str(data['cdc_status']).upper()}")
    if data.get('exchange'):
        lines.append(f"Exchange: {format_exchange_label(data.get('exchange'))}")
    _append_meta(lines, data)
    return send_line_message_with_retry("\n".join(lines))

def notify_weekly_dca_buy(data: dict) -> bool:
    schedule = data.get('schedule_id')
    schedule_label = schedule if schedule not in (None, '') else '-'
    cdc_status = data.get('cdc_status')
    holdings_line = _format_holdings_line(
        data.get('holdings'),
        data.get('holdings_meta'),
    )
    meta_entries = _meta_entries(data)

    if flex_allowed('weekly_dca'):
        sections = [
            ("Exchange", format_exchange_label(data.get('exchange'))),
            ("Amount", f"{data.get('usdt', 0):,.2f} USDT"),
            ("Filled", f"{data.get('btc_qty', 0):.8f} BTC @ ฿{data.get('price', 0):,.2f}"),
            ("Schedule", f"#{schedule_label}"),
            ("Order", str(data.get('order_id', 'N/A'))),
        ]
        if cdc_status:
            sections.append(("CDC", str(cdc_status).upper()))

        footer_bits: list[str] = []
        if holdings_line:
            footer_bits.append(holdings_line)
        if meta_entries:
            footer_bits.append(" | ".join(meta_entries))

        bubble = build_basic_bubble(
            "Weekly DCA Buy",
            sections,
            subtitle=f"Time: {_utc_stamp(data.get('timestamp'))}",
            theme="success",
            footer_note="\n".join(footer_bits) if footer_bits else None,
        )
        flex_message = make_flex_message(
            f"Weekly DCA Buy {data.get('usdt', 0):,.2f} USDT",
            bubble,
        )
        if send_line_flex_with_retry(flex_message):
            return True
        logging.warning("Flex send failed for weekly DCA buy; falling back to text message")

    lines = [
        "✅ Weekly DCA Buy",
        f"Time: {_utc_stamp(data.get('timestamp'))}",
        f"Exchange: {format_exchange_label(data.get('exchange'))}",
        f"Amount: {data.get('usdt', 0):,.2f} USDT",
        f"Filled: {data.get('btc_qty', 0):.8f} BTC @ ฿{data.get('price', 0):,.2f}",
        f"Schedule: #{schedule_label}",
        f"Order: {data.get('order_id', 'N/A')}",
    ]
    if cdc_status:
        lines.append(f"CDC: {str(cdc_status).upper()}")
    if holdings_line:
        lines.append(holdings_line)
    lines.extend(meta_entries)
    return send_line_message_with_retry("\n".join(lines))

def notify_weekly_dca_skipped(amount: float, reserve: float, context: dict | None = None) -> bool:
    amt = float(amount or 0.0)
    res_val = float(reserve or 0.0)
    ctx = context or {}
    cdc_status = ctx.get('cdc_status')
    timestamp = _utc_stamp(ctx.get('timestamp'))
    holdings_line = _format_holdings_line(
        ctx.get('holdings'),
        ctx.get('holdings_meta'),
    )
    meta_entries = _meta_entries(ctx)

    if flex_allowed('weekly_dca'):
        sections = [
            ("Reserve Added", f"+{amt:,.2f} USDT"),
            ("Total Reserve", f"{res_val:,.2f} USDT"),
        ]
        if cdc_status:
            sections.append(("CDC", str(cdc_status).upper()))

        footer_bits: list[str] = []
        if holdings_line:
            footer_bits.append(holdings_line)
        if meta_entries:
            footer_bits.append(" | ".join(meta_entries))

        bubble = build_basic_bubble(
            "Weekly DCA Skipped",
            sections,
            subtitle=f"Time: {timestamp}",
            theme="warning",
            footer_note="\n".join(footer_bits) if footer_bits else None,
        )
        flex_message = make_flex_message(
            f"Weekly DCA Skipped +{amt:,.2f} USDT to reserve",
            bubble,
        )
        if send_line_flex_with_retry(flex_message):
            return True
        logging.warning("Flex send failed for weekly DCA skipped; falling back to text message")

    lines = [
        "⏸ Weekly DCA Skipped",
        f"Time: {timestamp}",
        f"Reserve +{amt:,.2f} USDT",
        f"Total Reserve: {res_val:,.2f} USDT",
    ]
    if cdc_status:
        lines.append(f"CDC: {str(cdc_status).upper()}")
    if holdings_line:
        lines.append(holdings_line)
    lines.extend(meta_entries)
    return send_line_message_with_retry("\n".join(lines))


def notify_weekly_dca_skipped_exchange(exchange: str, amount: float, reserve: float, context: dict | None = None) -> bool:
    amt = float(amount or 0.0)
    res_val = float(reserve or 0.0)
    ctx = context or {}
    cdc_status = ctx.get('cdc_status')
    timestamp = _utc_stamp(ctx.get('timestamp'))
    holdings_line = _format_holdings_line(
        ctx.get('holdings'),
        ctx.get('holdings_meta'),
    )
    meta_entries = _meta_entries(ctx)
    exchange_label = format_exchange_label(exchange)

    if flex_allowed('weekly_dca'):
        sections = [
            ("Exchange", exchange_label),
            ("Reserve Added", f"+{amt:,.2f} USDT"),
            ("Total Reserve", f"{res_val:,.2f} USDT"),
        ]
        if cdc_status:
            sections.append(("CDC", str(cdc_status).upper()))

        footer_bits: list[str] = []
        if holdings_line:
            footer_bits.append(holdings_line)
        if meta_entries:
            footer_bits.append(" | ".join(meta_entries))

        bubble = build_basic_bubble(
            "Weekly DCA Skipped",
            sections,
            subtitle=f"Time: {timestamp}",
            theme="warning",
            footer_note="\n".join(footer_bits) if footer_bits else None,
        )
        flex_message = make_flex_message(
            f"Weekly DCA Skipped ({exchange_label})",
            bubble,
        )
        if send_line_flex_with_retry(flex_message):
            return True
        logging.warning("Flex send failed for weekly DCA skipped exchange; falling back to text message")

    lines = [
        "⏸ Weekly DCA Skipped",
        f"Time: {timestamp}",
        f"Exchange: {exchange_label}",
        f"Reserve +{amt:,.2f} USDT",
        f"Total Reserve: {res_val:,.2f} USDT",
    ]
    if cdc_status:
        lines.append(f"CDC: {str(cdc_status).upper()}")
    if holdings_line:
        lines.append(holdings_line)
    lines.extend(meta_entries)
    return send_line_message_with_retry("\n".join(lines))

def notify_reserve_buy_executed(data: dict) -> bool:
    lines = [
        "✅ Reserve Buy Executed",
        f"Time: {_utc_stamp(data.get('timestamp'))}",
        f"Exchange: {format_exchange_label(data.get('exchange'))}",
        f"Spend: {data.get('spend', 0):,.2f} USDT",
        f"Filled: {data.get('btc_qty', 0):.8f} BTC @ ฿{data.get('price', 0):,.2f}",
        f"Reserve Left: {data.get('reserve_left', 0):,.2f} USDT",
        f"Order: {data.get('order_id', 'N/A')}",
    ]
    if data.get('cdc_status'):
        lines.append(f"CDC: {str(data['cdc_status']).upper()}")
    _append_meta(lines, data)
    return send_line_message_with_retry("\n".join(lines))

def notify_reserve_buy_skipped_min_notional(data: dict) -> bool:
    lines = [
        "⚠️ Reserve Buy Skipped",
        f"Time: {_utc_stamp(data.get('timestamp'))}",
        f"Spend: {data.get('spend', 0):,.2f} < {data.get('min_notional', 0):,.2f}",
        f"Reserve: {data.get('reserve', 0):,.2f} USDT",
    ]
    if data.get('exchange'):
        lines.append(f"Exchange: {format_exchange_label(data.get('exchange'))}")
    _append_meta(lines, data)
    return send_line_message_with_retry("\n".join(lines))


def notify_liquidity_blocked(action: str, data: dict) -> bool:
    action_label = action.replace('_', ' ').title()
    lines = [
        "🛑 Liquidity Block",
        f"Action: {action_label}",
        f"Time: {_utc_stamp(data.get('timestamp'))}",
        f"Exchange: {format_exchange_label(data.get('exchange'))}",
    ]
    if data.get('spread_pct') is not None:
        lines.append(f"Spread: {data.get('spread_pct', 0):.3f}% (max {data.get('threshold_pct', 0):.3f}%)")
    if data.get('reason'):
        lines.append(f"Reason: {_reason_text(data.get('reason'))}")
    if data.get('expected_notional') is not None:
        lines.append(f"Notional: {data.get('expected_notional', 0):,.2f} USDT")
    depth_info = data.get('depth')
    if isinstance(depth_info, dict):
        bid_notional = depth_info.get('bid_notional')
        ask_notional = depth_info.get('ask_notional')
        if bid_notional is not None and ask_notional is not None:
            lines.append(f"Depth Bid/Ask: {bid_notional:,.0f} / {ask_notional:,.0f} USDT within ±{depth_info.get('band_pct', 0):.2f}%")
    twap_info = data.get('twap')
    if isinstance(twap_info, dict):
        twap_val = twap_info.get('twap')
        deviation = twap_info.get('deviation_pct')
        if twap_val is not None:
            lines.append(f"TWAP: {twap_val:,.2f} USDT (Δ {deviation or 0:.2f}% / max {twap_info.get('threshold_pct', 0):.2f}%)")
    if data.get('cap') is not None:
        lines.append(f"Cap: {float(data.get('cap') or 0):,.2f} USDT")
    if data.get('attempt') is not None:
        lines.append(f"Attempt: {float(data.get('attempt') or 0):,.2f} USDT")
    _append_meta(lines, data)
    return send_line_message_with_retry("\n".join(lines))

def notify_security_alert(title: str, details: dict | None = None) -> bool:
    lines = [
        "🚨 Security Alert",
        title,
        f"Time: {_utc_stamp()}",
    ]
    if details:
        for key, value in details.items():
            if value is None:
                continue
            lines.append(f"{key}: {value}")
    return send_line_message_with_retry("\n".join(lines))

def notify_strategy_error(context: str, error: str) -> bool:
    msg = f"❌ Strategy Error\n{context}\n🚨 {error}"
    return send_line_message_with_retry(msg)

def notify_cdc_toggle(enabled: bool, flags: dict | None = None) -> bool:
    """แจ้งเตือนเมื่อสลับสถานะ CDC Trading แบบ Global
    flags: { 'testnet': bool, 'dry_run': bool }
    """
    suffix = []
    try:
        if flags:
            if flags.get('testnet'):
                suffix.append('TESTNET')
            if flags.get('dry_run'):
                suffix.append('DRY_RUN')
    except Exception:
        pass
    suffix_text = f" ({'/'.join(suffix)})" if suffix else ''

    if enabled:
        msg = f"🟢 CDC Trading Enabled (1D){suffix_text}\nระบบจะทำ DCA ตาม CDC Action Zone"
    else:
        msg = f"⏸ CDC Trading Disabled{suffix_text}\nระบบจะทำ DCA ตามตารางปกติ ไม่พิจารณา CDC"
    return send_line_message_with_retry(msg)

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
