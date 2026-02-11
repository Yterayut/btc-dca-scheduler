import os
import time
import hmac
import base64
import json
import requests
from urllib.parse import urlencode
from decimal import Decimal
import logging
from .base import ExchangeAdapter, OrderResult


SUPPORTED_SYMBOLS: dict[str, dict[str, str]] = {
    "BTC-USDT": {"base": "BTC", "quote": "USDT"},
    "XAUT-USDT": {"base": "XAUT", "quote": "USDT"},
}


class OkxAdapter(ExchangeAdapter):
    """OKX Spot adapter (skeleton). For safety, only dry_run is supported by default.
    Real order placement wiring should be completed after confirming OKX API params.
    """

    default_symbol = "BTC-USDT"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, passphrase: str | None = None, max_usdt: float | None = None, **kw):
        super().__init__(**kw)
        self.api_key = api_key or os.getenv("OKX_API_KEY")
        self.api_secret = api_secret or os.getenv("OKX_API_SECRET")
        self.passphrase = passphrase or os.getenv("OKX_PASSPHRASE")
        # OKX uses same domain; testnet behavior may vary by account
        self.base = "https://www.okx.com"
        # Per-order cap (can be set from DB via main engine)
        try:
            self.max_usdt = float(max_usdt) if max_usdt is not None else float(os.getenv('OKX_MAX_USDT') or 10.0)
        except Exception:
            self.max_usdt = 10.0

    def symbol(self) -> str:
        return self.default_symbol

    def _normalize_symbol(self, symbol: str | None = None) -> str:
        """Validate and normalize requested trading symbol."""
        sym = (symbol or self.symbol() or "").upper()
        if sym not in SUPPORTED_SYMBOLS:
            supported = ", ".join(sorted(SUPPORTED_SYMBOLS.keys()))
            raise NotImplementedError(f"OKX adapter supports spot symbols: {supported}")
        return sym

    def _iso_ts(self) -> str:
        # OKX recommends RFC3339/ISO8601 with milliseconds, UTC Z
        from datetime import datetime, timezone as _tz
        return datetime.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = self._iso_ts()
        if not self.api_secret or not self.api_key or not self.passphrase:
            raise ValueError("OKX API credentials are not configured")
        prehash = f"{ts}{method}{path}{body}"
        sign = base64.b64encode(
            hmac.new(self.api_secret.encode(), prehash.encode(), digestmod="sha256").digest()
        ).decode()
        headers = {
            "OK-ACCESS-KEY": self.api_key or "",
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase or "",
            "Content-Type": "application/json",
        }
        # If using demo trading (paper), add simulated header
        if str(os.getenv('OKX_SIMULATED', '0')).strip().lower() in ('1','true','yes','on'):
            headers['x-simulated-trading'] = '1'
        return headers

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
        body = ""
        url = self.base + path
        if method.upper() == 'GET' and params:
            # Keep a stable order for signing
            qs = urlencode(params, doseq=True)
            url = f"{url}?{qs}"
            # For signature, path must include query string
            headers = self._headers('GET', f"{path}?{qs}")
            r = requests.get(url, headers=headers, timeout=(self.timeouts["connect"], self.timeouts["read"]))
            return r
        else:
            if payload:
                body = json.dumps(payload, separators=(",", ":"))
            headers = self._headers(method.upper(), path, body)
            r = requests.request(method.upper(), url, headers=headers, data=body if body else None, timeout=(self.timeouts["connect"], self.timeouts["read"]))
            return r

    def get_price_symbol(self, symbol: str) -> float:
        inst = self._normalize_symbol(symbol)
        r = requests.get(
            f"{self.base}/api/v5/market/ticker",
            params={"instId": inst},
            timeout=(self.timeouts["connect"], self.timeouts["read"])
        )
        r.raise_for_status()
        data = r.json().get("data", [{}])[0]
        return float(data.get("last") or 0.0)

    def get_balance(self, asset: str) -> dict:
        path = "/api/v5/account/balance"
        r = requests.get(self.base + path, headers=self._headers("GET", path), timeout=(self.timeouts["connect"], self.timeouts["read"]))
        r.raise_for_status()
        payload = r.json()
        details = (payload.get("data") or [{}])[0].get("details") or []
        free = 0.0; locked = 0.0
        for d in details:
            if str(d.get("ccy", "")).upper() == asset.upper():
                # availBal = available; cashBal = cash balance; frozenBal = frozen
                try:
                    free = float(d.get("availBal") or d.get("cashBal") or 0.0)
                except Exception:
                    free = 0.0
                try:
                    locked = float(d.get("frozenBal") or 0.0)
                except Exception:
                    locked = 0.0
                break
        return {"free": free, "locked": locked}

    def get_symbol_filters(self, symbol: str) -> dict:
        inst = self._normalize_symbol(symbol)
        r = requests.get(
            f"{self.base}/api/v5/public/instruments",
            params={"instType": "SPOT", "instId": inst},
            timeout=(self.timeouts["connect"], self.timeouts["read"])
        )
        r.raise_for_status()
        inst = (r.json().get("data") or [{}])[0]
        lotSz = float(inst.get("lotSz") or 0.000001)
        minSz = float(inst.get("minSz") or lotSz)
        tickSz = float(inst.get("tickSz") or 0.01)
        return {"lotSz": lotSz, "minSz": minSz, "tickSz": tickSz}

    def _order_details(self, symbol: str, ord_id: str) -> dict:
        inst = self._normalize_symbol(symbol)
        q = {"instId": inst, "ordId": ord_id}
        r = self._request('GET', "/api/v5/trade/order", params=q)
        r.raise_for_status()
        data = r.json()
        if str(data.get('code')) != '0':
            raise ValueError(f"OKX order query error: {data}")
        return (data.get('data') or [{}])[0]

    def _cancel_order(self, symbol: str, ord_id: str) -> None:
        inst = self._normalize_symbol(symbol)
        payload = {"instId": inst, "ordId": ord_id}
        r = self._request('POST', "/api/v5/trade/cancel-order", payload=payload)
        r.raise_for_status()
        data = r.json()
        if str(data.get('code')) != '0':
            raise ValueError(f"OKX cancel error: {data}")
        logging.info("OKX order canceled | symbol=%s ordId=%s", inst, ord_id)

    def _wait_for_fill(self, symbol: str, ord_id: str, *, timeout_seconds: int = 45, poll_seconds: float = 2.0) -> dict:
        deadline = time.time() + max(int(timeout_seconds), 1)
        last = None
        while time.time() < deadline:
            od = self._order_details(symbol, ord_id)
            last = od
            state = str(od.get("state") or "").lower()
            if state in ("filled", "canceled", "cancelled"):
                return od
            time.sleep(max(float(poll_seconds), 0.25))
        # Timeout: try cancel and return latest details
        logging.warning("OKX order timeout | symbol=%s ordId=%s timeout_seconds=%s", symbol, ord_id, timeout_seconds)
        try:
            self._cancel_order(symbol, ord_id)
        except Exception:
            logging.warning("OKX cancel on timeout failed | symbol=%s ordId=%s", symbol, ord_id, exc_info=True)
            pass
        try:
            return self._order_details(symbol, ord_id)
        except Exception:
            return last or {}

    @staticmethod
    def _format_decimal(value: float | str) -> str:
        try:
            d = Decimal(str(value))
        except Exception:
            d = Decimal(0)
        s = format(d, "f")
        return s.rstrip("0").rstrip(".") or "0"

    def _limit_order_by_base(
        self,
        symbol: str,
        *,
        side: str,
        quantity: float,
        price: float,
        timeout_seconds: int = 45,
        ord_type: str = "limit",
    ) -> OrderResult:
        inst = self._normalize_symbol(symbol)
        side = str(side).lower()
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy|sell")
        filters = self.get_symbol_filters(inst)
        qty, qty_text = self.quantize_step(quantity, float(filters.get("lotSz") or 0.0))
        if qty < float(filters.get("minSz") or 0.0):
            raise ValueError("below minSz")
        px = float(price or 0.0)
        if px <= 0:
            raise ValueError("invalid_price")
        tick = float(filters.get("tickSz") or 0.01)
        px = self.round_to_tick(px, tick)
        px_text = self._format_decimal(px)

        live_enabled = str(os.getenv('OKX_LIVE_ENABLED', '0')).strip().lower() in ('1','true','yes','on')
        if self.dry_run or not live_enabled:
            cqq = qty * px
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=px)

        payload = {
            "instId": inst,
            "tdMode": "cash",
            "side": side,
            "ordType": str(ord_type).lower(),
            "sz": qty_text,
            "px": px_text,
        }
        r = self._request('POST', "/api/v5/trade/order", payload=payload)
        data = r.json()
        if r.status_code != 200 or str(data.get('code')) != '0':
            raise ValueError(f"OKX limit order error: {data}")
        ord_id = (data.get('data') or [{}])[0].get('ordId')
        if not ord_id:
            raise ValueError(f"OKX limit order missing ordId: {data}")
        logging.info(
            "OKX order placed | symbol=%s side=%s ordType=%s px=%s sz=%s ordId=%s",
            inst,
            side,
            str(ord_type).lower(),
            px_text,
            qty_text,
            ord_id,
        )

        od = self._wait_for_fill(inst, ord_id, timeout_seconds=timeout_seconds)
        avg_px = float(od.get('avgPx') or 0.0)
        acc_fill_sz = float(od.get('accFillSz') or 0.0)
        if acc_fill_sz > 0 and avg_px <= 0:
            avg_px = px
        cqq = acc_fill_sz * avg_px if acc_fill_sz > 0 else 0.0

        fee_asset = str(od.get('feeCcy') or '').upper()
        try:
            fee_asset_amount = abs(float(od.get('fee') or 0.0))
        except (TypeError, ValueError):
            fee_asset_amount = 0.0
        fee_usd = 0.0
        if fee_asset_amount > 0:
            if fee_asset == 'USDT':
                fee_usd = fee_asset_amount
            elif avg_px > 0:
                fee_usd = fee_asset_amount * avg_px

        return OrderResult(
            order_id=str(ord_id),
            executed_qty=acc_fill_sz,
            cummulative_quote_qty=cqq,
            avg_price=avg_px or px,
            fee_usd=fee_usd,
            fee_asset=fee_asset or None,
            fee_asset_amount=fee_asset_amount,
        )

    def place_limit_sell_qty_symbol(
        self,
        symbol: str,
        quantity: float,
        price: float,
        *,
        timeout_seconds: int = 45,
        ord_type: str = "limit",
    ) -> OrderResult:
        return self._limit_order_by_base(
            symbol,
            side="sell",
            quantity=quantity,
            price=price,
            timeout_seconds=timeout_seconds,
            ord_type=ord_type,
        )

    def place_limit_buy_qty_symbol(
        self,
        symbol: str,
        quantity: float,
        price: float,
        *,
        timeout_seconds: int = 45,
        ord_type: str = "limit",
    ) -> OrderResult:
        return self._limit_order_by_base(
            symbol,
            side="buy",
            quantity=quantity,
            price=price,
            timeout_seconds=timeout_seconds,
            ord_type=ord_type,
        )

    def place_market_buy_quote_symbol(self, symbol: str, usdt_amount: float) -> OrderResult:
        inst = self._normalize_symbol(symbol)
        symbol = inst
        # Clamp max spend to 10 USDT by default
        spend = float(usdt_amount)
        if self.max_usdt > 0:
            spend = min(spend, self.max_usdt)

        # DRY_RUN path or live guard
        live_enabled = str(os.getenv('OKX_LIVE_ENABLED', '0')).strip().lower() in ('1','true','yes','on')
        if self.dry_run or not live_enabled:
            price = self.get_price_symbol(inst)
            filters = self.get_symbol_filters(inst)
            qty_raw = spend / max(price, 1e-9)
            qty, _ = self.quantize_step(qty_raw, filters["lotSz"])
            if qty < filters["minSz"]:
                raise ValueError("below minSz")
            cqq = qty * price
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=price)

        # LIVE placement: try tgtCcy=quote_ccy first
        path = "/api/v5/trade/order"
        base_payload = {
            "instId": symbol,
            "tdMode": "cash",
            "side": "buy",
            "ordType": "market",
        }
        # Attempt quote-sized order
        payload = base_payload | {"tgtCcy": "quote_ccy", "sz": str(spend)}
        r = self._request('POST', path, payload=payload)
        data = r.json()
        if r.status_code != 200 or str(data.get('code')) != '0':
            # Fallback: size in base currency
            price = self.get_price_symbol(inst)
            filters = self.get_symbol_filters(inst)
            qty_raw = spend / max(price, 1e-9)
            qty, qty_text = self.quantize_step(qty_raw, filters["lotSz"])
            if qty < filters["minSz"]:
                raise ValueError(f"below minSz: {qty} < {filters['minSz']}")
            payload2 = base_payload | {"sz": qty_text}
            r = self._request('POST', path, payload=payload2)
            data = r.json()
            if r.status_code != 200 or str(data.get('code')) != '0':
                raise ValueError(f"OKX order error: {data}")

        ordId = (data.get('data') or [{}])[0].get('ordId')
        if not ordId:
            raise ValueError(f"OKX order missing ordId: {data}")
        # Fetch order details to compute fills
        q = {"instId": inst, "ordId": ordId}
        r2 = self._request('GET', "/api/v5/trade/order", params=q)
        det = r2.json()
        od = (det.get('data') or [{}])[0]
        avgPx = float(od.get('avgPx') or 0.0)
        accFillSz = float(od.get('accFillSz') or 0.0)
        if accFillSz <= 0 or avgPx <= 0:
            # As a fallback, try latest price
            avgPx = self.get_price_symbol(inst)
        cqq = accFillSz * avgPx
        fee_asset = str(od.get('feeCcy') or '').upper()
        try:
            fee_asset_amount = abs(float(od.get('fee') or 0.0))
        except (TypeError, ValueError):
            fee_asset_amount = 0.0
        fee_usd = 0.0
        if fee_asset_amount > 0:
            if fee_asset == 'USDT':
                fee_usd = fee_asset_amount
            elif avgPx > 0:
                fee_usd = fee_asset_amount * avgPx

        return OrderResult(
            order_id=ordId,
            executed_qty=accFillSz,
            cummulative_quote_qty=cqq,
            avg_price=avgPx,
            fee_usd=fee_usd,
            fee_asset=fee_asset or None,
            fee_asset_amount=fee_asset_amount,
        )

    def place_market_sell_qty_symbol(self, symbol: str, quantity: float) -> OrderResult:
        inst = self._normalize_symbol(symbol)
        symbol = inst
        filters = self.get_symbol_filters(inst)
        qty, qty_text = self.quantize_step(quantity, filters["lotSz"])
        if qty < filters["minSz"]:
            raise ValueError("below minSz")
        price = self.get_price_symbol(inst)

        live_enabled = str(os.getenv('OKX_LIVE_ENABLED', '0')).strip().lower() in ('1','true','yes','on')
        if self.dry_run or not live_enabled:
            cqq = qty * price
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=price)

        # LIVE market sell by base size
        path = "/api/v5/trade/order"
        payload = {
            "instId": symbol,
            "tdMode": "cash",
            "side": "sell",
            "ordType": "market",
            "sz": qty_text
        }
        r = self._request('POST', path, payload=payload)
        data = r.json()
        if r.status_code != 200 or str(data.get('code')) != '0':
            raise ValueError(f"OKX sell order error: {data}")
        ordId = (data.get('data') or [{}])[0].get('ordId')
        if not ordId:
            raise ValueError(f"OKX sell order missing ordId: {data}")
        # Fetch order details
        q = {"instId": inst, "ordId": ordId}
        r2 = self._request('GET', "/api/v5/trade/order", params=q)
        od = (r2.json().get('data') or [{}])[0]
        avgPx = float(od.get('avgPx') or 0.0)
        accFillSz = float(od.get('accFillSz') or 0.0)
        if accFillSz <= 0 or avgPx <= 0:
            avgPx = self.get_price_symbol(inst)
        cqq = accFillSz * avgPx
        fee_asset = str(od.get('feeCcy') or '').upper()
        try:
            fee_asset_amount = abs(float(od.get('fee') or 0.0))
        except (TypeError, ValueError):
            fee_asset_amount = 0.0
        fee_usd = 0.0
        if fee_asset_amount > 0:
            if fee_asset == 'USDT':
                fee_usd = fee_asset_amount
            elif avgPx > 0:
                fee_usd = fee_asset_amount * avgPx

        return OrderResult(
            order_id=ordId,
            executed_qty=accFillSz,
            cummulative_quote_qty=cqq,
            avg_price=avgPx,
            fee_usd=fee_usd,
            fee_asset=fee_asset or None,
            fee_asset_amount=fee_asset_amount,
        )

    def get_top_of_book(self, symbol: str | None = None) -> dict:
        inst = self._normalize_symbol(symbol)
        r = requests.get(
            f"{self.base}/api/v5/market/ticker",
            params={"instId": inst},
            timeout=(self.timeouts["connect"], self.timeouts["read"])
        )
        r.raise_for_status()
        payload = r.json()
        data = (payload.get("data") or [{}])[0]
        bid = float(data.get("bidPx") or 0.0)
        ask = float(data.get("askPx") or 0.0)
        return {
            "bid": bid,
            "ask": ask,
            "bid_qty": float(data.get("bidSz") or 0.0),
            "ask_qty": float(data.get("askSz") or 0.0),
            "ts": data.get("ts"),
        }

    # --- Extra helpers for app endpoints ---
    def get_fills_history(self, limit: int = 100, symbol: str | None = None) -> list[dict]:
        """Fetch recent fills history for a supported spot symbol."""
        inst = self._normalize_symbol(symbol)
        params = {"instType": "SPOT", "instId": inst, "limit": str(limit)}
        r = self._request('GET', "/api/v5/trade/fills-history", params=params)
        r.raise_for_status()
        data = r.json()
        if str(data.get('code')) != '0':
            raise ValueError(f"OKX fills error: {data}")
        return data.get('data') or []

    def get_depth_snapshot(self, *, limit: int = 20, symbol: str | None = None) -> dict:
        inst = self._normalize_symbol(symbol)
        params = {"instId": inst, "sz": str(min(max(limit, 5), 400))}
        r = requests.get(
            f"{self.base}/api/v5/market/books",
            params=params,
            timeout=(self.timeouts["connect"], self.timeouts["read"])
        )
        r.raise_for_status()
        payload = r.json()
        data = (payload.get("data") or [{}])[0]
        bids = [(float(p), float(q)) for p, q, *_ in data.get("bids", [])]
        asks = [(float(p), float(q)) for p, q, *_ in data.get("asks", [])]
        return {"bids": bids, "asks": asks, "ts": data.get("ts")}

    def get_recent_candles(self, *, interval: str = "1m", limit: int = 30, symbol: str | None = None) -> list[dict]:
        inst = self._normalize_symbol(symbol)
        params = {"instId": inst, "bar": interval, "limit": str(min(max(limit, 1), 300))}
        r = requests.get(
            f"{self.base}/api/v5/market/candles",
            params=params,
            timeout=(self.timeouts["connect"], self.timeouts["read"])
        )
        r.raise_for_status()
        payload = r.json()
        candles: list[dict] = []
        for entry in payload.get("data", []):
            # OKX returns most-recent first; reverse later if needed
            candles.append(
                {
                    "open_time": int(entry[0]),
                    "open": float(entry[1]),
                    "high": float(entry[2]),
                    "low": float(entry[3]),
                    "close": float(entry[4]),
                    "volume": float(entry[5]),
                    "close_time": int(entry[0]),  # OKX uses open timestamp; treat as close for guard usage
                }
            )
        return list(reversed(candles))
