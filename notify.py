import logging
import requests
import os
from dotenv import load_dotenv

load_dotenv()

def send_line_message(message: str) -> None:
    """Send a message to LINE Notify.

    Args:
        message (str): The message to send.
    """
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {os.getenv("LINE_CHANNEL_ACCESS_TOKEN")}'
    }
    payload = {
        "to": os.getenv("LINE_USER_ID"),
        "messages": [{"type": "text", "text": message}]
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            logging.error(f"Failed to send LINE message: {response.text}")
        else:
            logging.info("LINE message sent successfully")
    except Exception as e:
        logging.error(f"LINE message error: {e}")
