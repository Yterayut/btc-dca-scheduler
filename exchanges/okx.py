import os
import time
import hmac
import base64
import json
import requests
from urllib.parse import urlencode
from .base import ExchangeAdapter, OrderResult


class OkxAdapter(ExchangeAdapter):
    """OKX Spot adapter (skeleton). For safety, only dry_run is supported by default.
    Real order placement wiring should be completed after confirming OKX API params.
    """

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
        return "BTC-USDT"

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

    def get_price(self) -> float:
        r = requests.get(f"{self.base}/api/v5/market/ticker", params={"instId": self.symbol()}, timeout=(self.timeouts["connect"], self.timeouts["read"]))
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

    def get_filters(self) -> dict:
        r = requests.get(f"{self.base}/api/v5/public/instruments", params={"instType": "SPOT", "instId": self.symbol()}, timeout=(self.timeouts["connect"], self.timeouts["read"]))
        r.raise_for_status()
        inst = (r.json().get("data") or [{}])[0]
        lotSz = float(inst.get("lotSz") or 0.000001)
        minSz = float(inst.get("minSz") or lotSz)
        tickSz = float(inst.get("tickSz") or 0.01)
        return {"lotSz": lotSz, "minSz": minSz, "tickSz": tickSz}

    def place_market_buy_quote(self, usdt_amount: float) -> OrderResult:
        # Clamp max spend to 10 USDT by default
        spend = float(usdt_amount)
        if self.max_usdt > 0:
            spend = min(spend, self.max_usdt)

        # DRY_RUN path or live guard
        live_enabled = str(os.getenv('OKX_LIVE_ENABLED', '0')).strip().lower() in ('1','true','yes','on')
        if self.dry_run or not live_enabled:
            price = self.get_price()
            filters = self.get_filters()
            qty_raw = spend / max(price, 1e-9)
            qty, _ = self.quantize_step(qty_raw, filters["lotSz"])
            if qty < filters["minSz"]:
                raise ValueError("below minSz")
            cqq = qty * price
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=price)

        # LIVE placement: try tgtCcy=quote_ccy first
        path = "/api/v5/trade/order"
        base_payload = {
            "instId": self.symbol(),
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
            price = self.get_price()
            filters = self.get_filters()
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
        q = {"instId": self.symbol(), "ordId": ordId}
        r2 = self._request('GET', "/api/v5/trade/order", params=q)
        det = r2.json()
        od = (det.get('data') or [{}])[0]
        avgPx = float(od.get('avgPx') or 0.0)
        accFillSz = float(od.get('accFillSz') or 0.0)
        if accFillSz <= 0 or avgPx <= 0:
            # As a fallback, try latest price
            avgPx = self.get_price()
        cqq = accFillSz * avgPx
        return OrderResult(order_id=ordId, executed_qty=accFillSz, cummulative_quote_qty=cqq, avg_price=avgPx)

    def place_market_sell_qty(self, qty_btc: float) -> OrderResult:
        filters = self.get_filters()
        qty, qty_text = self.quantize_step(qty_btc, filters["lotSz"])
        if qty < filters["minSz"]:
            raise ValueError("below minSz")
        price = self.get_price()

        live_enabled = str(os.getenv('OKX_LIVE_ENABLED', '0')).strip().lower() in ('1','true','yes','on')
        if self.dry_run or not live_enabled:
            cqq = qty * price
            return OrderResult(order_id=-1, executed_qty=qty, cummulative_quote_qty=cqq, avg_price=price)

        # LIVE market sell by base size
        path = "/api/v5/trade/order"
        payload = {
            "instId": self.symbol(),
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
        q = {"instId": self.symbol(), "ordId": ordId}
        r2 = self._request('GET', "/api/v5/trade/order", params=q)
        od = (r2.json().get('data') or [{}])[0]
        avgPx = float(od.get('avgPx') or 0.0)
        accFillSz = float(od.get('accFillSz') or 0.0)
        if accFillSz <= 0 or avgPx <= 0:
            avgPx = self.get_price()
        cqq = accFillSz * avgPx
        return OrderResult(order_id=ordId, executed_qty=accFillSz, cummulative_quote_qty=cqq, avg_price=avgPx)

    # --- Extra helpers for app endpoints ---
    def get_fills_history(self, limit: int = 100) -> list[dict]:
        """Fetch recent fills history for BTC-USDT spot."""
        params = {"instType": "SPOT", "instId": self.symbol(), "limit": str(limit)}
        r = self._request('GET', "/api/v5/trade/fills-history", params=params)
        r.raise_for_status()
        data = r.json()
        if str(data.get('code')) != '0':
            raise ValueError(f"OKX fills error: {data}")
        return data.get('data') or []
