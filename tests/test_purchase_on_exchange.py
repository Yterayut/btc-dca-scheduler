from contextlib import contextmanager
from datetime import datetime

import main


class StubOrderResult:
    def __init__(
        self,
        *,
        order_id,
        executed_qty,
        cummulative_quote_qty,
        avg_price,
        fee_usd=0.0,
        fee_asset=None,
        fee_asset_amount=0.0,
    ):
        self.order_id = order_id
        self.executed_qty = executed_qty
        self.cummulative_quote_qty = cummulative_quote_qty
        self.avg_price = avg_price
        self.fee_usd = fee_usd
        self.fee_asset = fee_asset
        self.fee_asset_amount = fee_asset_amount


class StubOkxBuyAdapter:
    def get_price(self):
        return 100.0

    def place_market_buy_quote(self, amount):
        return StubOrderResult(
            order_id=12345,
            executed_qty=0.25,
            cummulative_quote_qty=float(amount),
            avg_price=100.0,
        )


class StubBitkubAdapter:
    def __init__(self):
        self.balance_calls = {"BTC": 0, "THB": 0}

    def get_price(self):
        return 1_000_000.0

    def symbol(self):
        return "BTC_THB"

    def place_market_buy_quote(self, amount):
        return StubOrderResult(
            order_id="hash-abc-123",
            executed_qty=0.0,
            cummulative_quote_qty=0.0,
            avg_price=0.0,
        )

    def get_balance(self, asset):
        self.balance_calls[asset] += 1
        if asset == "BTC":
            return {"free": 0.0} if self.balance_calls[asset] == 1 else {"free": 0.0002}
        if asset == "THB":
            return {"free": 1000.0} if self.balance_calls[asset] == 1 else {"free": 800.0}
        return {"free": 0.0}

    def get_order_execution_symbol(self, symbol, order_id, side="buy", retries=1, retry_sleep_sec=0.2):
        return {}

    def get_order_execution_from_history_symbol(self, symbol, order_id, limit=50):
        return {}


@contextmanager
def _fake_tx(captured):
    class Cursor:
        def execute(self, query, params=None):
            captured.append((query, params))

        @property
        def description(self):
            return []

        def fetchone(self):
            return None

    yield Cursor(), None


def test_purchase_on_exchange_bypasses_guards_for_okx_pure_dca(monkeypatch):
    captured_sql = []
    blocked = []
    notified = []
    fees = []
    compliance = []

    monkeypatch.setattr(main, "load_strategy_state", lambda: {"okx_max_usdt": 1000.0})
    monkeypatch.setattr(main, "is_dry_run", lambda: False)
    monkeypatch.setattr(main, "evaluate_depth_guard", lambda *args, **kwargs: (False, {"reason": "depth_insufficient"}))
    monkeypatch.setattr(main, "evaluate_twap_guard", lambda *args, **kwargs: (False, {"reason": "twap_deviation"}))
    monkeypatch.setattr(main, "evaluate_notional_cap", lambda *args, **kwargs: (False, {"cap": 10.0, "attempt": 25.0}))
    monkeypatch.setattr(main, "notify_liquidity_blocked", lambda action, payload: blocked.append((action, payload)))
    monkeypatch.setattr(main, "notify_weekly_dca_buy", lambda payload: notified.append(payload) or True)
    monkeypatch.setattr(main, "_attach_holdings_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "record_fee_totals", lambda *args: fees.append(args))
    monkeypatch.setattr(main, "log_compliance_event", lambda *args, **kwargs: compliance.append((args, kwargs)))
    monkeypatch.setattr(main, "send_line_message", lambda msg: (_ for _ in ()).throw(AssertionError(msg)))
    monkeypatch.setattr(main, "db_transaction", lambda: _fake_tx(captured_sql))

    from exchanges import okx as okx_module

    monkeypatch.setattr(okx_module, "OkxAdapter", lambda *args, **kwargs: StubOkxBuyAdapter())

    now = main.utc.localize(datetime(2026, 3, 14, 13, 30, 0))
    result = main.purchase_on_exchange(
        now,
        "okx",
        25.0,
        42,
        context={"cdc_status": "okx_pure_dca", "request_id": "req-1", "dedupe_key": "dedupe-1"},
    )

    assert result["executed"] is True
    assert result["exchange"] == "okx"
    assert result["usdt"] == 25.0
    assert blocked == []
    assert len(captured_sql) == 1
    assert "INSERT INTO purchase_history" in captured_sql[0][0]
    assert captured_sql[0][1][3] == 12345
    assert notified[0]["order_id"] == 12345
    assert notified[0]["cdc_status"] == "okx_pure_dca"
    assert fees[0][0] == "cdc_weekly_dca"
    assert compliance[0][1]["metadata"]["cdc_status"] == "okx_pure_dca"


def test_purchase_on_exchange_bitkub_uses_balance_delta_and_null_order_id(monkeypatch):
    captured_sql = []
    notified = []
    fees = []
    compliance = []
    adapter = StubBitkubAdapter()

    monkeypatch.setattr(main, "load_strategy_state", lambda: {})
    monkeypatch.setattr(main, "get_adapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(main, "is_dry_run", lambda: False)
    monkeypatch.setattr(main, "evaluate_depth_guard", lambda *args, **kwargs: (True, {}))
    monkeypatch.setattr(main, "evaluate_twap_guard", lambda *args, **kwargs: (True, {}))
    monkeypatch.setattr(main, "evaluate_notional_cap", lambda *args, **kwargs: (True, {"cap": 0.0, "attempt": 200.0}))
    monkeypatch.setattr(main, "notify_weekly_dca_buy", lambda payload: notified.append(payload) or True)
    monkeypatch.setattr(main, "_attach_holdings_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "record_fee_totals", lambda *args: fees.append(args))
    monkeypatch.setattr(main, "log_compliance_event", lambda *args, **kwargs: compliance.append((args, kwargs)))
    monkeypatch.setattr(main, "send_line_message", lambda msg: (_ for _ in ()).throw(AssertionError(msg)))
    monkeypatch.setattr(main, "db_transaction", lambda: _fake_tx(captured_sql))
    monkeypatch.setattr(main.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main.time, "time", lambda: 0.0)

    now = main.utc.localize(datetime(2026, 3, 14, 13, 35, 0))
    result = main.purchase_on_exchange(
        now,
        "bitkub",
        200.0,
        99,
        context={"cdc_status": "bitkub_pure_dca", "request_id": "req-2"},
    )

    assert result["executed"] is True
    assert result["exchange"] == "bitkub"
    assert result["quote_asset"] == "THB"
    assert result["order_id"] == "hash-abc-123"
    assert result["qty"] == 0.0002
    assert result["usdt"] == 200.0
    assert len(captured_sql) == 1
    assert "INSERT INTO purchase_history" in captured_sql[0][0]
    assert captured_sql[0][1][3] is None
    assert captured_sql[0][1][5] == "bitkub"
    assert notified[0]["order_id"] == "hash-abc-123"
    assert notified[0]["quote_asset"] == "THB"
    assert compliance[0][1]["metadata"]["order_id"] == "hash-abc-123"
    assert fees[0][1] == "bitkub"
