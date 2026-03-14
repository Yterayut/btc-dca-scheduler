from contextlib import contextmanager
from datetime import datetime

import main


class StubOrderResult:
    def __init__(self, *, order_id, executed_qty, cummulative_quote_qty, avg_price):
        self.order_id = order_id
        self.executed_qty = executed_qty
        self.cummulative_quote_qty = cummulative_quote_qty
        self.avg_price = avg_price
        self.fee_usd = 0.0
        self.fee_asset = None
        self.fee_asset_amount = 0.0


class StubBuyAdapter:
    def __init__(self, available_usdt=100.0):
        self.available_usdt = float(available_usdt)

    def get_balance(self, asset="USDT"):
        if asset == "USDT":
            return {"free": self.available_usdt}
        return {"free": 0.0}

    def get_price(self):
        return 100.0

    def place_market_buy_quote(self, amount):
        return StubOrderResult(
            order_id=98765,
            executed_qty=float(amount) / 100.0,
            cummulative_quote_qty=float(amount),
            avg_price=100.0,
        )


def _dt():
    return main.utc.localize(datetime(2026, 3, 14, 14, 0, 0))


@contextmanager
def _fake_tx(captured, fetchone_values):
    values = list(fetchone_values)

    class Cursor:
        def execute(self, query, params=None):
            captured.append((query, params))

        def fetchone(self):
            if values:
                return values.pop(0)
            return None

        @property
        def description(self):
            return []

    yield Cursor(), None


def test_execute_reserve_buy_executes_and_updates_reserve(monkeypatch):
    captured_sql = []
    notified = []
    fees = []
    compliance = []

    state = {"reserve_usdt": 75.0, "exchange": "binance"}
    monkeypatch.setattr(main, "load_strategy_state", lambda: dict(state))
    monkeypatch.setattr(main, "get_adapter", lambda *args, **kwargs: StubBuyAdapter(available_usdt=100.0))
    monkeypatch.setattr(main, "is_dry_run", lambda: False)
    monkeypatch.setattr(main, "get_symbol_filters", lambda *args, **kwargs: {"minNotional": 10.0})
    monkeypatch.setattr(main, "evaluate_depth_guard", lambda *args, **kwargs: (True, {}))
    monkeypatch.setattr(main, "evaluate_twap_guard", lambda *args, **kwargs: (True, {}))
    monkeypatch.setattr(main, "evaluate_notional_cap", lambda *args, **kwargs: (True, {"cap": 0.0, "attempt": 75.0}))
    monkeypatch.setattr(main, "assess_liquidity", lambda *args, **kwargs: (True, {}))
    monkeypatch.setattr(main, "notify_reserve_buy_executed", lambda payload: notified.append(payload) or True)
    monkeypatch.setattr(main, "record_fee_totals", lambda *args: fees.append(args))
    monkeypatch.setattr(main, "log_compliance_event", lambda *args, **kwargs: compliance.append((args, kwargs)))
    monkeypatch.setattr(main, "send_line_message", lambda msg: (_ for _ in ()).throw(AssertionError(msg)))
    monkeypatch.setattr(main, "db_transaction", lambda: _fake_tx(captured_sql, [(12.5,)]))

    result = main.execute_reserve_buy(_dt(), context={"request_id": "req-rb", "cdc_status": "up"})

    assert result["executed"] is True
    assert result["spend"] == 75.0
    assert result["qty"] == 0.75
    assert notified[0]["reserve_left"] == 12.5
    assert notified[0]["cdc_status"] == "up"
    assert fees[0][0] == "cdc_reserve_buy"
    assert compliance[0][1]["metadata"]["mode"] == "global"
    assert any("UPDATE strategy_state SET reserve_usdt" in query for query, _ in captured_sql)


def test_execute_reserve_buy_exchange_skips_below_min_notional(monkeypatch):
    skipped = []

    monkeypatch.setattr(main, "load_strategy_state", lambda: {"reserve_okx_usdt": 5.0, "okx_max_usdt": 100.0})
    monkeypatch.setattr(main, "get_adapter", lambda *args, **kwargs: StubBuyAdapter(available_usdt=100.0))
    monkeypatch.setattr(main, "is_dry_run", lambda: False)
    monkeypatch.setattr(main, "get_symbol_filters", lambda *args, **kwargs: {"minNotional": 10.0})
    monkeypatch.setattr(main, "notify_reserve_buy_skipped_min_notional", lambda payload: skipped.append(payload) or True)
    monkeypatch.setattr(main, "send_line_message", lambda msg: (_ for _ in ()).throw(AssertionError(msg)))
    from exchanges import okx as okx_module
    monkeypatch.setattr(okx_module, "OkxAdapter", lambda *args, **kwargs: StubBuyAdapter(available_usdt=100.0))

    result = main.execute_reserve_buy_exchange(_dt(), "okx", context={"request_id": "req-okx"})

    assert result["skipped"] is True
    assert result["reason"] == "below_minNotional"
    assert result["exchange"] == "okx"
    assert skipped[0]["request_id"] == "req-okx"
