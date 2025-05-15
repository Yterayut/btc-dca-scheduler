import os
from dotenv import load_dotenv
from linebot import LineBotApi
from linebot.models import TextSendMessage

load_dotenv()

line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
USER_ID = os.getenv("USER_ID")

def send_line_message(message):
    try:
        line_bot_api.push_message(USER_ID, TextSendMessage(text=message))
    except Exception as e:
        print(f"LINE error: {e}")

