import os
from dotenv import load_dotenv
from binance.client import Client

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
USE_TESTNET = str(os.getenv('USE_BINANCE_TESTNET', os.getenv('BINANCE_TESTNET', '0'))).lower() in ('1','true','yes','on')

client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET, testnet=USE_TESTNET)

def get_btc_price():
    try:
        ticker = client.get_symbol_ticker(symbol="BTCUSDT")
        return float(ticker["price"])
    except Exception as e:
        print(f"Error fetching price: {e}")
        return None

def get_gold_price():
    """Return the current GOLD (PAXG/USDT) price.

    Allows overriding via env `S4_GOLD_PRICE_OVERRIDE` to support offline tests.
    """
    override = os.getenv("S4_GOLD_PRICE_OVERRIDE")
    if override:
        try:
            return float(override)
        except (TypeError, ValueError):
            pass
    try:
        ticker = client.get_symbol_ticker(symbol="PAXGUSDT")
        return float(ticker["price"])
    except Exception as e:
        print(f"Error fetching gold price: {e}")
        return None

def get_client():
    return client
