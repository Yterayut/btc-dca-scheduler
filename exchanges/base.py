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


class ExchangeAdapter(ABC):
    def __init__(self, testnet: bool = False, dry_run: bool = False, timeouts: dict | None = None):
        self.testnet = testnet
        self.dry_run = dry_run
        self.timeouts = timeouts or {"read": 10, "connect": 10}

    @abstractmethod
    def symbol(self) -> str:
        """Return BTC/USDT symbol for this exchange (e.g., 'BTCUSDT' or 'BTC-USDT')."""

    @abstractmethod
    def get_price(self) -> float:
        """Return current last price for BTC/USDT."""

    @abstractmethod
    def get_balance(self, asset: str) -> dict:
        """Return balance dict {'free': float, 'locked': float} for given asset."""

    @abstractmethod
    def get_filters(self) -> dict:
        """Return precision/minimum constraints for trading the BTC/USDT symbol."""

    @abstractmethod
    def place_market_buy_quote(self, usdt_amount: float) -> OrderResult:
        """Place market buy by quote amount (USDT). Returns executed result."""

    @abstractmethod
    def place_market_sell_qty(self, qty_btc: float) -> OrderResult:
        """Place market sell by BTC quantity. Returns executed result."""

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
