import hashlib
import hmac
import json
import os
import time
from decimal import Decimal
from urllib.parse import urlencode

import requests

from .base import ExchangeAdapter, OrderResult


class BitkubAdapter(ExchangeAdapter):
    def __init__(self, api_key: str | None = None, api_secret: str | None = None, **kw):
        super().__init__(**kw)
        self.api_key = api_key or os.getenv("BITKUB_API_KEY")
        self.api_secret = api_secret or os.getenv("BITKUB_API_SECRET")
        self.base = "https://api.bitkub.com"
        self._symbols_cache: dict | None = None
        self._symbols_cache_ts = 0.0

    def symbol(self) -> str:
        return "BTC_THB"

    @staticmethod
    def _normalize_symbol(symbol: str | None) -> str:
        return str(symbol or "BTC_THB").strip().upper()

    @staticmethod
    def _format_number(value: float | int | Decimal) -> str:
        txt = format(Decimal(str(value)), "f")
        return txt.rstrip("0").rstrip(".") or "0"

    def _server_timestamp_ms(self) -> int:
        try:
            r = requests.get(
                f"{self.base}/api/v3/servertime",
                timeout=(self.timeouts["connect"], self.timeouts["read"]),
            )
            r.raise_for_status()
            return int(r.json())
        except Exception:
            return int(time.time() * 1000)

    def _signed_headers(self, method: str, path_with_query: str, body_text: str, ts_ms: int) -> dict:
        if not self.api_key or not self.api_secret:
            raise ValueError("Bitkub API credentials are not configured")
        payload = f"{ts_ms}{method.upper()}{path_with_query}{body_text}"
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-BTK-APIKEY": self.api_key,
            "X-BTK-TIMESTAMP": str(ts_ms),
            "X-BTK-SIGN": sig,
        }

    def _signed_get(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        qs = urlencode(params)
        path_with_query = f"{path}?{qs}" if qs else path
        ts_ms = self._server_timestamp_ms()
        headers = self._signed_headers("GET", path_with_query, "", ts_ms)
        url = f"{self.base}{path_with_query}"
        r = requests.get(url, headers=headers, timeout=(self.timeouts["connect"], self.timeouts["read"]))
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and int(data.get("error", 0)) != 0:
            raise ValueError(f"Bitkub API error: {data}")
        return data

    def _signed_post(self, path: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        body_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        ts_ms = self._server_timestamp_ms()
        headers = self._signed_headers("POST", path, body_text, ts_ms)
        r = requests.post(
            f"{self.base}{path}",
            headers=headers,
            data=body_text,
            timeout=(self.timeouts["connect"], self.timeouts["read"]),
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and int(data.get("error", 0)) != 0:
            raise ValueError(f"Bitkub API error: {data}")
        return data

    def _load_symbols(self, force: bool = False) -> list[dict]:
        now = time.time()
        if (not force) and self._symbols_cache and (now - self._symbols_cache_ts) < 300:
            return self._symbols_cache
        r = requests.get(
            f"{self.base}/api/v3/market/symbols",
            timeout=(self.timeouts["connect"], self.timeouts["read"]),
        )
        r.raise_for_status()
        data = r.json()
        if int(data.get("error", 0)) != 0:
            raise ValueError(f"Bitkub symbols error: {data}")
        rows = data.get("result") or []
        self._symbols_cache = rows
        self._symbols_cache_ts = now
        return rows

    def _symbol_row(self, symbol: str) -> dict:
        sym = self._normalize_symbol(symbol)
        for row in self._load_symbols():
            if str(row.get("symbol", "")).upper() == sym:
                return row
        raise ValueError(f"Bitkub symbol not found: {sym}")

    def get_price_symbol(self, symbol: str) -> float:
        sym = self._normalize_symbol(symbol)
        r = requests.get(
            f"{self.base}/api/v3/market/ticker",
            params={"sym": sym},
            timeout=(self.timeouts["connect"], self.timeouts["read"]),
        )
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, list) and rows:
            return float(rows[0].get("last") or 0.0)
        raise ValueError(f"Bitkub ticker unavailable for {sym}")

    def get_balance(self, asset: str) -> dict:
        if self.dry_run:
            return {"free": 0.0, "locked": 0.0}
        data = self._signed_post("/api/v3/market/wallet", {})
        wallet = data.get("result") or {}
        free = float(wallet.get(asset.upper()) or 0.0)
        return {"free": free, "locked": 0.0}

    def get_symbol_filters(self, symbol: str) -> dict:
        row = self._symbol_row(symbol)
        min_notional = float(row.get("min_quote_size") or 10.0)
        tick = float(row.get("price_step") or 0.01)
        qty_scale = int(row.get("base_asset_scale") or row.get("quantity_scale") or 8)
        qty_step_int = float(row.get("quantity_step") or 1.0)
        step = qty_step_int * (10 ** (-qty_scale))
        if step <= 0:
            step = 0.00000001
        return {
            "stepSize": step,
            "minQty": step,
            "tickSize": tick,
            "minNotional": min_notional,
        }

    def place_market_buy_quote_symbol(self, symbol: str, quote_amount: float) -> OrderResult:
        sym = self._normalize_symbol(symbol)
        spend = float(quote_amount or 0.0)
        if spend <= 0:
            raise ValueError("amount must be > 0")
        filters = self.get_symbol_filters(sym)
        if spend < float(filters.get("minNotional") or 10.0):
            raise ValueError("below minNotional")

        if self.dry_run:
            price = self.get_price_symbol(sym)
            qty_raw = spend / max(price, 1e-12)
            qty, _ = self.quantize_step(qty_raw, float(filters["stepSize"]))
            cqq = qty * price
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=price)

        payload = {
            "sym": sym,
            "amt": float(self._format_number(spend)),
            "rat": 0,
            "typ": "market",
        }
        data = self._signed_post("/api/v3/market/place-bid", payload)
        result = data.get("result") or {}

        order_id = result.get("id") or result.get("hash") or "N/A"
        cqq = float(result.get("amt") or spend)
        qty = float(result.get("rec") or 0.0)
        avg_price = (cqq / qty) if qty > 0 else float(self.get_price_symbol(sym))
        fee_quote = float(result.get("fee") or 0.0)
        return OrderResult(
            order_id=order_id,
            executed_qty=qty,
            cummulative_quote_qty=cqq,
            avg_price=avg_price,
            fee_usd=fee_quote,
            fee_asset="THB",
            fee_asset_amount=fee_quote,
        )

    def place_market_sell_qty_symbol(self, symbol: str, quantity: float) -> OrderResult:
        raise NotImplementedError("Bitkub market sell is not implemented in this phase")

    def get_order_execution_symbol(
        self,
        symbol: str,
        order_id: str | int,
        side: str = "buy",
        *,
        retries: int = 3,
        retry_sleep_sec: float = 0.35,
    ) -> dict:
        """Fetch enriched order execution details from /order-info.

        Returns normalized keys:
        - qty: filled base quantity (BTC)
        - quote_spent: total quote amount spent (includes fee when available)
        - quote_filled: net quote amount converted to base
        - avg_price: weighted execution rate
        - fee_quote: quote fee (THB)
        - status: order status string
        """
        sym = self._normalize_symbol(symbol)
        oid = str(order_id or "").strip()
        sd = str(side or "buy").strip().lower()
        if not oid:
            return {}
        if sd not in ("buy", "sell"):
            sd = "buy"

        last_exc: Exception | None = None
        for attempt in range(max(1, int(retries))):
            try:
                data = self._signed_get(
                    "/api/v3/market/order-info",
                    {"sym": sym, "id": oid, "sd": sd},
                )
                result = (data or {}).get("result") or {}
                history = result.get("history") or []

                quote_filled = float(result.get("filled") or 0.0)
                quote_spent = float(result.get("total") or 0.0)
                fee_quote = float(result.get("fee") or 0.0)
                history_fee_total = 0.0
                qty = 0.0
                weighted_quote = 0.0

                for row in history:
                    try:
                        amt = float(row.get("amount") or 0.0)
                        rate = float(row.get("rate") or 0.0)
                        fee = float(row.get("fee") or 0.0)
                        if amt > 0 and rate > 0:
                            qty += amt / rate
                            weighted_quote += amt
                        if fee > 0:
                            history_fee_total += fee
                    except Exception:
                        continue

                if weighted_quote > 0:
                    quote_filled = weighted_quote
                if history_fee_total > 0:
                    fee_quote = history_fee_total
                avg_price = (quote_filled / qty) if (qty > 0 and quote_filled > 0) else 0.0
                if quote_spent <= 0 and quote_filled > 0:
                    quote_spent = quote_filled + max(fee_quote, 0.0)

                return {
                    "qty": qty,
                    "quote_spent": quote_spent,
                    "quote_filled": quote_filled,
                    "avg_price": avg_price,
                    "fee_quote": fee_quote,
                    "status": str(result.get("status") or "").strip().lower(),
                }
            except Exception as exc:
                last_exc = exc
                if attempt < max(1, int(retries)) - 1:
                    time.sleep(max(0.0, float(retry_sleep_sec)))

        if last_exc:
            raise last_exc
        return {}

    def get_order_execution_from_history_symbol(
        self,
        symbol: str,
        order_id: str | int,
        *,
        limit: int = 50,
    ) -> dict:
        """Best-effort execution fallback using /my-order-history."""
        sym = self._normalize_symbol(symbol)
        oid = str(order_id or "").strip()
        if not oid:
            return {}

        data = self._signed_get("/api/v3/market/my-order-history", {"sym": sym, "lmt": int(limit)})
        rows = (data or {}).get("result") or []
        target = None
        for row in rows:
            if str(row.get("order_id") or "").strip() == oid:
                target = row
                break
        if not target:
            return {}

        rate = float(target.get("rate") or 0.0)
        amount_total = float(target.get("amount") or 0.0)
        fee_quote = float(target.get("fee") or 0.0)
        # Bitkub history "amount" is quote total, and fee is quoted separately.
        quote_filled = max(amount_total - fee_quote, 0.0)
        qty = (quote_filled / rate) if (rate > 0 and quote_filled > 0) else 0.0
        if qty <= 0 and rate > 0 and amount_total > 0:
            qty = amount_total / rate
            quote_filled = amount_total

        return {
            "qty": qty,
            "quote_spent": amount_total,
            "quote_filled": quote_filled,
            "avg_price": rate,
            "fee_quote": fee_quote,
            "status": str(target.get("status") or "").strip().lower(),
        }
