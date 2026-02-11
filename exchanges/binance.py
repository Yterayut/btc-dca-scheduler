import os
import re
from binance.client import Client
from .base import ExchangeAdapter, OrderResult


class BinanceAdapter(ExchangeAdapter):
    def __init__(self, api_key: str | None = None, api_secret: str | None = None, **kw):
        super().__init__(**kw)
        self.api_key = api_key or os.getenv("BINANCE_API_KEY")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET")
        self.client = Client(
            self.api_key,
            self.api_secret,
            testnet=self.testnet,
            requests_params={'timeout': 15}
        )

    def symbol(self) -> str:
        return "BTCUSDT"

    def get_balance(self, asset: str) -> dict:
        b = self.client.get_asset_balance(asset=asset)
        return {"free": float(b.get("free") or 0), "locked": float(b.get("locked") or 0)}

    def _get_symbol_info(self, symbol: str) -> dict:
        info = self.client.get_symbol_info(symbol)
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

    def get_price_symbol(self, symbol: str) -> float:
        return float(self.client.get_symbol_ticker(symbol=symbol)["price"])

    def get_symbol_filters(self, symbol: str) -> dict:
        return self._get_symbol_info(symbol)

    def place_market_buy_quote_symbol(self, symbol: str, usdt_amount: float) -> OrderResult:
        if self.dry_run:
            price = self.get_price_symbol(symbol)
            qty = usdt_amount / price
            filters = self.get_symbol_filters(symbol)
            qty = self.floor_to_step(qty, filters["stepSize"])
            cqq = qty * price
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=price)

        order = self.client.order_market_buy(
            symbol=symbol,
            quoteOrderQty=usdt_amount,
            newOrderRespType='FULL'
        )
        order_id = order["orderId"]
        details = self.client.get_order(symbol=symbol, orderId=order_id)
        executed_qty = float(details.get("executedQty") or order.get("executedQty") or 0)
        cqq = float(details.get("cummulativeQuoteQty") or order.get("cummulativeQuoteQty") or 0)
        avg = cqq / executed_qty if executed_qty > 0 else 0

        fee_asset = None
        fee_asset_amount = 0.0
        fee_usd = 0.0
        fills = order.get('fills') or []
        for fill in fills:
            try:
                commission = float(fill.get('commission') or 0.0)
            except (TypeError, ValueError):
                commission = 0.0
            asset = str(fill.get('commissionAsset') or '').upper()
            if commission <= 0 or not asset:
                continue
            price = float(fill.get('price') or avg or 0.0)
            fee_asset_amount += commission
            fee_asset = asset
            if asset == 'USDT':
                fee_usd += commission
            else:
                if price > 0:
                    fee_usd += commission * price

        return OrderResult(
            order_id=order_id,
            executed_qty=executed_qty,
            cummulative_quote_qty=cqq,
            avg_price=avg,
            fee_usd=fee_usd,
            fee_asset=fee_asset,
            fee_asset_amount=fee_asset_amount,
        )

    def place_market_sell_qty_symbol(self, symbol: str, quantity: float) -> OrderResult:
        filters = self.get_symbol_filters(symbol)
        qty, qty_text = self.quantize_step(quantity, filters["stepSize"])
        if qty < filters["minQty"]:
            raise ValueError("below minQty")
        price = self.get_price_symbol(symbol)
        if qty * price < filters["minNotional"]:
            raise ValueError("below minNotional")

        if not re.fullmatch(r"\d+(?:\.\d+)?", qty_text):
            raise ValueError(f"invalid_quantity_format: {qty_text}")

        if self.dry_run:
            cqq = qty * price
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=price)

        order = self.client.order_market_sell(symbol=symbol, quantity=qty_text, newOrderRespType='FULL')
        order_id = order["orderId"]
        details = self.client.get_order(symbol=symbol, orderId=order_id)
        executed_qty = float(details.get("executedQty") or order.get("executedQty") or 0)
        cqq = float(details.get("cummulativeQuoteQty") or order.get("cummulativeQuoteQty") or 0)
        avg = cqq / executed_qty if executed_qty > 0 else 0

        fee_asset = None
        fee_asset_amount = 0.0
        fee_usd = 0.0
        fills = order.get('fills') or []
        for fill in fills:
            try:
                commission = float(fill.get('commission') or 0.0)
            except (TypeError, ValueError):
                commission = 0.0
            asset = str(fill.get('commissionAsset') or '').upper()
            if commission <= 0 or not asset:
                continue
            price_fill = float(fill.get('price') or avg or price or 0.0)
            fee_asset_amount += commission
            fee_asset = asset
            if asset == 'USDT':
                fee_usd += commission
            else:
                if price_fill > 0:
                    fee_usd += commission * price_fill

        return OrderResult(
            order_id=order_id,
            executed_qty=executed_qty,
            cummulative_quote_qty=cqq,
            avg_price=avg,
            fee_usd=fee_usd,
            fee_asset=fee_asset,
            fee_asset_amount=fee_asset_amount,
        )

    def get_top_of_book(self) -> dict:
        ticker = self.client.get_orderbook_ticker(symbol=self.symbol())
        bid = float(ticker.get("bidPrice") or 0.0)
        ask = float(ticker.get("askPrice") or 0.0)
        return {
            "bid": bid,
            "ask": ask,
            "bid_qty": float(ticker.get("bidQty") or 0.0),
            "ask_qty": float(ticker.get("askQty") or 0.0),
            "ts": ticker.get("time"),
        }

    def get_depth_snapshot(self, *, limit: int = 20) -> dict:
        depth = self.client.get_order_book(symbol=self.symbol(), limit=min(max(limit, 5), 500))
        bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
        return {"bids": bids, "asks": asks, "lastUpdateId": depth.get("lastUpdateId")}

    def get_recent_candles(self, *, interval: str = "1m", limit: int = 30) -> list[dict]:
        klines = self.client.get_klines(symbol=self.symbol(), interval=interval, limit=min(max(limit, 1), 500))
        candles: list[dict] = []
        for k in klines:
            candles.append(
                {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": int(k[6]),
                }
            )
        return candles
