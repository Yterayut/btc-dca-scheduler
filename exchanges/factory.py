import os
from .binance import BinanceAdapter
from .okx import OkxAdapter


def get_adapter(exchange: str | None = None, testnet: bool = False, dry_run: bool = False):
    ex = (exchange or os.getenv("EXCHANGE") or "binance").strip().lower()
    if ex == "okx":
        return OkxAdapter(testnet=testnet, dry_run=dry_run)
    return BinanceAdapter(testnet=testnet, dry_run=dry_run)

