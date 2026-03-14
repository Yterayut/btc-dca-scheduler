import asyncio
from contextlib import contextmanager
from datetime import datetime
import os
import sys
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

with patch("binance.client.Client.ping", lambda self: {}):
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


class StubOkxAdapter:
    def get_price_symbol(self, symbol):
        if symbol == "BTC-USDT":
            return 100.0
        if symbol == "XAUT-USDT":
            return 50.0
        raise AssertionError(f"unexpected symbol: {symbol}")

    def get_symbol_filters(self, symbol):
        return {"lotSz": 0.01, "minSz": 0.01, "tickSz": 0.01}

    def quantize_step(self, qty, step):
        return round(float(qty), 2), f"{qty:.2f}"

    def round_to_tick(self, price, tick):
        return round(float(price), 2)

    def get_top_of_book(self, symbol):
        if symbol == "BTC-USDT":
            return {"bid": 100.0, "ask": 100.0}
        if symbol == "XAUT-USDT":
            return {"bid": 50.0, "ask": 50.0}
        raise AssertionError(f"unexpected symbol: {symbol}")

    def place_limit_sell_qty_symbol(self, symbol, qty, price, timeout_seconds, ord_type):
        return StubOrderResult(
            order_id="sell-1",
            executed_qty=float(qty),
            cummulative_quote_qty=float(qty) * float(price),
            avg_price=float(price),
        )

    def place_limit_buy_qty_symbol(self, symbol, qty, price, timeout_seconds, ord_type):
        return StubOrderResult(
            order_id="buy-1",
            executed_qty=float(qty),
            cummulative_quote_qty=float(qty) * float(price),
            avg_price=float(price),
        )

    def get_balance(self, asset):
        if asset == "BTC":
            return {"free": 0.0}
        if asset == "XAUT":
            return {"free": 2.0}
        return {"free": 0.0}


@contextmanager
def fake_tx():
    class Cursor:
        def execute(self, query, params=None):
            return None

        @property
        def description(self):
            return []

        def fetchone(self):
            return None

    yield Cursor(), None


def _aware_dt():
    return main.utc.localize(datetime(2026, 3, 14, 12, 0, 0))


def _enable_feature(monkeypatch):
    monkeypatch.setattr(
        main,
        "_env_flag",
        lambda name, default=False: True if name == "FEATURE_S4_ENABLED" else default,
    )


def test_run_s4_tick_marks_limit_first_execution_success(monkeypatch):
    _enable_feature(monkeypatch)
    monkeypatch.setattr(main, "S4_HARDENING_ENABLED", False)
    monkeypatch.setattr(main, "S4_SWAP_EXEC_ENABLED", True)
    monkeypatch.setattr(main, "S4_EXEC_HARDENING_ENABLED", True)
    monkeypatch.setattr(main, "S4_IOC_FALLBACK_ENABLED", False)
    monkeypatch.setattr(main, "is_dry_run", lambda: False)
    monkeypatch.setattr(main, "get_adapter", lambda *args, **kwargs: StubOkxAdapter())
    monkeypatch.setattr(
        main,
        "fetch_balances",
        lambda *args, **kwargs: {"okx": {"BTC": {"free": 1.0}, "XAUT": {"free": 0.0}}},
    )
    monkeypatch.setattr(
        main,
        "_fetch_okx_ratio_signal",
        lambda: {
            "status": "down",
            "updated_at": "2026-03-14T11:55:00+00:00",
            "ratio": 2.0,
            "btc_close": 100.0,
            "gold_close": 50.0,
        },
    )
    monkeypatch.setattr(main, "_fetch_okx_ratio_series", lambda: [])
    monkeypatch.setattr(main, "_compute_ema_series", lambda ratios, span: [])
    monkeypatch.setattr(main, "_s4_check_spread_okx", lambda *args, **kwargs: (True, {"spread_pct": 0.1, "threshold_pct": 0.5, "bid": 1.0, "ask": 1.0}))
    monkeypatch.setattr(main, "db_transaction", fake_tx)
    monkeypatch.setattr(main, "record_fee_totals", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "notify_s4_rotation", lambda payload: None)
    monkeypatch.setattr(main, "save_strategy_metadata", lambda *args, **kwargs: None)

    captured = {}
    monkeypatch.setattr(
        main,
        "record_rotation_event",
        lambda **kwargs: captured.update(kwargs),
    )

    runtime = {"last_cdc_status": "up", "active_asset": "BTC"}
    metadata = {"config": {"exchange": "okx", "min_flip_usd": 10.0}, "runtime": runtime}
    monkeypatch.setattr(
        main,
        "get_s4_state",
        lambda: ({"cdc_enabled": 1}, metadata, metadata["config"], runtime),
    )

    asyncio.run(main.run_s4_tick(_aware_dt()))

    assert runtime["holding_asset"] == "GOLD"
    assert runtime["last_flip_at"]
    assert runtime["active_asset"] == "GOLD"
    assert runtime["last_action"]["holding_asset"] == "BTC"
    assert runtime["last_action"]["target_asset"] == "GOLD"
    assert runtime["last_action"]["executed"]["executed_ok"] is True
    assert captured["metadata"]["holding_asset"] == "BTC"
    assert captured["metadata"]["target_asset"] == "GOLD"
    assert captured["metadata"]["executed_ok"] is True


def test_run_s4_tick_shadow_mode_preserves_actual_active_asset(monkeypatch):
    _enable_feature(monkeypatch)
    monkeypatch.setattr(main, "S4_HARDENING_ENABLED", False)
    monkeypatch.setattr(main, "S4_SWAP_EXEC_ENABLED", False)
    monkeypatch.setattr(main, "is_dry_run", lambda: True)
    monkeypatch.setattr(main, "get_adapter", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "fetch_symbol_price_fallback", lambda symbol, exchange: 100.0 if symbol == "BTC-USDT" else 50.0)
    monkeypatch.setattr(
        main,
        "fetch_balances",
        lambda *args, **kwargs: {"okx": {"BTC": {"free": 1.0}, "XAUT": {"free": 0.0}}},
    )
    monkeypatch.setattr(
        main,
        "_fetch_okx_ratio_signal",
        lambda: {
            "status": "down",
            "updated_at": "2026-03-14T11:55:00+00:00",
            "ratio": 2.0,
            "btc_close": 100.0,
            "gold_close": 50.0,
        },
    )
    monkeypatch.setattr(main, "_fetch_okx_ratio_series", lambda: [])
    monkeypatch.setattr(main, "_compute_ema_series", lambda ratios, span: [])
    monkeypatch.setattr(main, "_s4_latest_eod_snapshot", lambda: {})
    monkeypatch.setattr(main, "save_strategy_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "record_rotation_event", lambda **kwargs: None)

    captured = {}

    def fake_gate_decision(*, runtime, cdc_status, now):
        captured["active_asset"] = runtime.get("active_asset")
        captured["signal_target_asset"] = runtime.get("signal_target_asset")
        return {
            "holding": "BTC",
            "target_asset": "GOLD",
            "decision": "HOLD",
            "reason": "gate_cdc_down_required",
            "cdc_status": cdc_status,
            "neutral_state": "",
            "slope_pct": 0.0,
            "gap_pct": 0.0,
            "days_since_last_swap": 9999,
            "next_unlock_condition": "cdc_status must be down for 5 consecutive days",
            "next_unlock_min_days": 5,
        }

    monkeypatch.setattr(main, "_s4_shadow_swap_gate_decision", fake_gate_decision)

    runtime = {"last_cdc_status": "up", "active_asset": "BTC"}
    metadata = {"config": {"exchange": "okx"}, "runtime": runtime}
    monkeypatch.setattr(
        main,
        "get_s4_state",
        lambda: ({"cdc_enabled": 1}, metadata, metadata["config"], runtime),
    )

    asyncio.run(main.run_s4_tick(_aware_dt()))

    assert runtime["holding_asset"] == "BTC"
    assert runtime["active_asset"] == "BTC"
    assert runtime["signal_target_asset"] == "GOLD"
    assert runtime["last_action"]["holding_asset"] == "BTC"
    assert runtime["last_action"]["target_asset"] == "GOLD"
    assert captured["active_asset"] == "BTC"
    assert captured["signal_target_asset"] == "GOLD"


def test_run_s4_tick_suppresses_lagged_mismatch_security_alert(monkeypatch):
    _enable_feature(monkeypatch)
    monkeypatch.setattr(main, "S4_HARDENING_ENABLED", False)
    monkeypatch.setattr(main, "S4_SWAP_EXEC_ENABLED", False)
    monkeypatch.setattr(main, "is_dry_run", lambda: True)
    monkeypatch.setattr(main, "get_adapter", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "fetch_symbol_price_fallback", lambda symbol, exchange: 100.0 if symbol == "BTC-USDT" else 50.0)
    monkeypatch.setattr(
        main,
        "fetch_balances",
        lambda *args, **kwargs: {"okx": {"BTC": {"free": 1.0}, "XAUT": {"free": 0.0}}},
    )
    monkeypatch.setattr(
        main,
        "_fetch_okx_ratio_signal",
        lambda: {
            "status": "down",
            "updated_at": "2026-03-14T11:55:00+00:00",
            "ratio": 2.0,
            "btc_close": 100.0,
            "gold_close": 50.0,
        },
    )
    monkeypatch.setattr(main, "_fetch_okx_ratio_series", lambda: [])
    monkeypatch.setattr(main, "_compute_ema_series", lambda ratios, span: [])
    monkeypatch.setattr(
        main,
        "_s4_latest_eod_snapshot",
        lambda: {"cdc_status": "up", "date": "2026-03-12", "eod_lag_days": 2},
    )
    monkeypatch.setattr(
        main,
        "_s4_shadow_swap_gate_decision",
        lambda **kwargs: {
            "holding": "BTC",
            "target_asset": "GOLD",
            "decision": "HOLD",
            "reason": "gate_cdc_down_required",
            "cdc_status": "down",
            "neutral_state": "",
            "slope_pct": 0.0,
            "gap_pct": 0.0,
            "days_since_last_swap": 9999,
            "next_unlock_condition": "cdc_status must be down for 5 consecutive days",
            "next_unlock_min_days": 5,
        },
    )
    monkeypatch.setattr(main, "save_strategy_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "record_rotation_event", lambda **kwargs: None)

    alerts = []
    monkeypatch.setattr(main, "notify_security_alert", lambda *args, **kwargs: alerts.append((args, kwargs)))

    runtime = {
        "last_cdc_status": "up",
        "holding_asset": "BTC",
        "active_asset": "BTC",
        "mismatch_counter_mode": "daily_eod",
        "mismatch_streak_days": 1,
        "mismatch_last_counted_date": "2026-03-11",
    }
    metadata = {"config": {"exchange": "okx"}, "runtime": runtime}
    monkeypatch.setattr(
        main,
        "get_s4_state",
        lambda: ({"cdc_enabled": 1}, metadata, metadata["config"], runtime),
    )

    asyncio.run(main.run_s4_tick(_aware_dt()))

    assert runtime["mismatch_severity"] == "warn"
    assert runtime["mismatch_streak_days"] == 2
    assert alerts == []
