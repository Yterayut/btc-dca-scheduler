from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from math import floor


@dataclass
class OrderResult:
    order_id: int | str
    executed_qty: float
    cummulative_quote_qty: float
    avg_price: float
    fee_usd: float = 0.0
    fee_asset: str | None = None
    fee_asset_amount: float = 0.0


class ExchangeAdapter(ABC):
    def __init__(self, testnet: bool = False, dry_run: bool = False, timeouts: dict | None = None):
        self.testnet = testnet
        self.dry_run = dry_run
        self.timeouts = timeouts or {"read": 10, "connect": 10}

    @abstractmethod
    def symbol(self) -> str:
        """Return BTC/USDT symbol for this exchange (e.g., 'BTCUSDT' or 'BTC-USDT')."""

    def get_price(self) -> float:
        """Return current last price for BTC/USDT."""
        return self.get_price_symbol(self.symbol())

    @abstractmethod
    def get_balance(self, asset: str) -> dict:
        """Return balance dict {'free': float, 'locked': float} for given asset."""

    def get_filters(self) -> dict:
        """Return precision/minimum constraints for trading the BTC/USDT symbol."""
        return self.get_symbol_filters(self.symbol())

    def place_market_buy_quote(self, usdt_amount: float) -> OrderResult:
        """Place market buy by quote amount (USDT). Returns executed result."""
        return self.place_market_buy_quote_symbol(self.symbol(), usdt_amount)

    def place_market_sell_qty(self, qty_btc: float) -> OrderResult:
        """Place market sell by BTC quantity. Returns executed result."""
        return self.place_market_sell_qty_symbol(self.symbol(), qty_btc)

    # --- Symbol-aware helpers (default to BTC symbol) ---
    @abstractmethod
    def get_price_symbol(self, symbol: str) -> float:
        raise NotImplementedError

    @abstractmethod
    def get_symbol_filters(self, symbol: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    def place_market_buy_quote_symbol(self, symbol: str, quote_amount: float) -> OrderResult:
        raise NotImplementedError

    @abstractmethod
    def place_market_sell_qty_symbol(self, symbol: str, quantity: float) -> OrderResult:
        raise NotImplementedError

    def get_top_of_book(self) -> dict:
        """Return best bid/ask snapshot as {'bid': float, 'ask': float, 'ts': float|None}."""
        raise NotImplementedError("top-of-book access not implemented for this adapter")

    # --- Optional market data helpers for advanced guards ---
    def get_depth_snapshot(self, *, limit: int = 20) -> dict:
        """Return depth snapshot as {'bids': [(price, qty)], 'asks': [...]}. Override when supported."""
        raise NotImplementedError("depth snapshot not implemented for this adapter")

    def get_recent_candles(self, *, interval: str = "1m", limit: int = 30) -> list[dict]:
        """Return recent candles as [{'open_time','close','high','low','close'}]. Override when supported."""
        raise NotImplementedError("recent candles not implemented for this adapter")

    # Generic helpers
    @staticmethod
    def floor_to_step(value: float, step: float) -> float:
        if not step or step <= 0:
            return value
        return floor(value / step) * step

    @staticmethod
    def round_to_tick(price: float, tick: float) -> float:
        if not tick or tick <= 0:
            return price
        return floor(price / tick) * tick

    @staticmethod
    def quantize_step(value: float, step: float) -> tuple[float, str]:
        """Return (float_value, string_value) aligned to exchange step size."""
        if not step or step <= 0:
            qty_float = float(value)
            qty_str = format(qty_float, 'f').rstrip('0').rstrip('.') or '0'
            return qty_float, qty_str

        step_dec = Decimal(str(step))
        places = max(-step_dec.as_tuple().exponent, 0)
        try:
            qty_dec = Decimal(str(value)).quantize(step_dec, rounding=ROUND_DOWN)
        except (InvalidOperation, ValueError):
            qty_dec = Decimal(0)
        qty_float = float(qty_dec)
        qty_str = format(qty_dec, f'.{places}f')
        qty_str = qty_str.rstrip('0').rstrip('.') or '0'
        return qty_float, qty_str
