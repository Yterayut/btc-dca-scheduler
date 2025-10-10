import os
from binance.client import Client
from .base import ExchangeAdapter, OrderResult


class BinanceAdapter(ExchangeAdapter):
    def __init__(self, api_key: str | None = None, api_secret: str | None = None, **kw):
        super().__init__(**kw)
        self.api_key = api_key or os.getenv("BINANCE_API_KEY")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET")
        self.client = Client(self.api_key, self.api_secret, testnet=self.testnet)

    def symbol(self) -> str:
        return "BTCUSDT"

    def get_price(self) -> float:
        return float(self.client.get_symbol_ticker(symbol=self.symbol())["price"])

    def get_balance(self, asset: str) -> dict:
        b = self.client.get_asset_balance(asset=asset)
        return {"free": float(b.get("free") or 0), "locked": float(b.get("locked") or 0)}

    def get_filters(self) -> dict:
        info = self.client.get_symbol_info(self.symbol())
        step = tick = min_qty = min_notional = None
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step = float(f.get("stepSize") or 0)
                min_qty = float(f.get("minQty") or 0)
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f.get("tickSize") or 0)
            if f["filterType"] == "NOTIONAL":
                min_notional = float(f.get("minNotional") or 0)
        return {
            "stepSize": step or 0.000001,
            "minQty": min_qty or 0.000001,
            "tickSize": tick or 0.01,
            "minNotional": min_notional or 10.0,
        }

    def place_market_buy_quote(self, usdt_amount: float) -> OrderResult:
        if self.dry_run:
            price = self.get_price()
            qty = usdt_amount / price
            filters = self.get_filters()
            qty = self.floor_to_step(qty, filters["stepSize"])
            cqq = qty * price
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=price)

        order = self.client.order_market_buy(symbol=self.symbol(), quoteOrderQty=usdt_amount)
        order_id = order["orderId"]
        details = self.client.get_order(symbol=self.symbol(), orderId=order_id)
        executed_qty = float(details.get("executedQty") or 0)
        cqq = float(details.get("cummulativeQuoteQty") or 0)
        avg = cqq / executed_qty if executed_qty > 0 else 0
        return OrderResult(order_id=order_id, executed_qty=executed_qty, cummulative_quote_qty=cqq, avg_price=avg)

    def place_market_sell_qty(self, qty_btc: float) -> OrderResult:
        filters = self.get_filters()
        qty, qty_text = self.quantize_step(qty_btc, filters["stepSize"])
        if qty < filters["minQty"]:
            raise ValueError("below minQty")
        price = self.get_price()
        if qty * price < filters["minNotional"]:
            raise ValueError("below minNotional")

        if self.dry_run:
            cqq = qty * price
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=price)

        order = self.client.order_market_sell(symbol=self.symbol(), quantity=qty_text)
        order_id = order["orderId"]
        details = self.client.get_order(symbol=self.symbol(), orderId=order_id)
        executed_qty = float(details.get("executedQty") or 0)
        cqq = float(details.get("cummulativeQuoteQty") or 0)
        avg = cqq / executed_qty if executed_qty > 0 else 0
        return OrderResult(order_id=order_id, executed_qty=executed_qty, cummulative_quote_qty=cqq, avg_price=avg)
