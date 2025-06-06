import os
from dotenv import load_dotenv
from binance.client import Client

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

def get_btc_price():
    try:
        ticker = client.get_symbol_ticker(symbol="BTCUSDT")
        return float(ticker["price"])
    except Exception as e:
        print(f"Error fetching price: {e}")
        return None

def get_client():
    return client

