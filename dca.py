import datetime
from utils import get_client, get_btc_price
from line_notify import send_line_message

DCA_AMOUNT_USD = 100

def execute_dca():
    client = get_client()
    price = get_btc_price()
    if not price:
        send_line_message("❌ ไม่สามารถดึงราคา BTC ได้")
        return

    qty = round(DCA_AMOUNT_USD / price, 6)
    try:
        order = client.order_market_buy(symbol="BTCUSDT", quantity=qty)

        time_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"""✅ ซื้อ BTC สำเร็จ (DCA)
📈 ราคา: ${price:,.2f}
💵 มูลค่า: ${DCA_AMOUNT_USD}
🪙 จำนวน: {qty} BTC
🕒 เวลา: {time_now}"""
        send_line_message(message)
        return {
            "time": time_now,
            "price": f"${price:,.2f}",
            "amount": f"${DCA_AMOUNT_USD}",
            "qty": f"{qty} BTC"
        }

    except Exception as e:
        send_line_message(f"❌ การสั่งซื้อ BTC ล้มเหลว: {e}")
        print(f"Binance error: {e}")
        return None

