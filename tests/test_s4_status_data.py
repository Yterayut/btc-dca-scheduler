import json
import os
import sys
from contextlib import contextmanager
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

with patch("binance.client.Client.ping", lambda self: {}):
    import app


@contextmanager
def fake_s4_status_cursor():
    class Cursor:
        def __init__(self):
            self.step = 0
            self.description = []

        def execute(self, query, params=None):
            self.step += 1
            if self.step == 1:
                self.description = [("metadata_json",)]
            elif self.step == 2:
                self.description = [
                    ("date",),
                    ("cdc_status",),
                    ("state",),
                    ("slope_pct",),
                    ("ema_gap_pct",),
                    ("eod_lag_days",),
                ]
            elif self.step == 3:
                self.description = [("executed_at",), ("from_asset",), ("to_asset",), ("reason",)]
            elif self.step == 4:
                self.description = [("count",)]
            elif self.step == 5:
                self.description = [
                    ("executed_at",),
                    ("from_asset",),
                    ("to_asset",),
                    ("notional_usd",),
                    ("cdc_status",),
                    ("reason",),
                    ("metadata_json",),
                ]

        def fetchone(self):
            if self.step == 1:
                metadata = {
                    "config": {"exchange": "okx"},
                    "runtime": {
                        "holding_asset": "BTC",
                        "active_asset": "BTC",
                        "signal_target_asset": "GOLD",
                        "last_cdc_status": "down",
                        "signal_source": "okx_ratio",
                        "last_signal_at": "2026-03-14T12:00:00+00:00",
                        "mismatch_streak_days": 2,
                    },
                }
                return (json.dumps(metadata),)
            if self.step == 2:
                return ("2026-03-12", "up", "weak_signal", 0.1, 0.2, 2)
            if self.step == 3:
                return None
            if self.step == 4:
                return (0,)
            return None

        def fetchall(self):
            return []

    yield Cursor(), None


def test_build_s4_status_data_exposes_signal_target_asset(monkeypatch):
    monkeypatch.setattr(app, "get_db_cursor", fake_s4_status_cursor)
    monkeypatch.setattr(app, "_load_s4_fifo_open_lots", lambda *args, **kwargs: [])

    data = app._build_s4_status_data()

    assert data["holding_asset"] == "BTC"
    assert data["active_asset"] == "BTC"
    assert data["signal_target_asset"] == "GOLD"
    assert data["signal_layers"]["runtime"]["holding_asset_runtime"] == "BTC"
    assert data["signal_layers"]["runtime"]["active_asset_runtime"] == "BTC"
    assert data["signal_layers"]["runtime"]["signal_target_asset_runtime"] == "GOLD"


def test_s4_route_renders_holding_and_target(monkeypatch):
    monkeypatch.setattr(
        app,
        "_build_s4_status_data",
        lambda: {
            "holding_asset": "BTC",
            "active_asset": "BTC",
            "signal_target_asset": "GOLD",
            "cdc_status": "DOWN",
            "signal_source": "okx_ratio",
            "signal_time": "2026-03-14T12:00:00+00:00",
            "exchange": "okx",
            "portfolio": {
                "total_usd": 1000.0,
                "cost_total": 900.0,
                "pnl_total": 100.0,
                "pnl_total_pct": 11.11,
                "btc": {"notional_usd": 600.0, "weight_pct": 60.0, "cost": 500.0, "pnl": 100.0, "pnl_pct": 20.0},
                "gold": {"notional_usd": 400.0, "weight_pct": 40.0, "cost": 400.0, "pnl": 0.0, "pnl_pct": 0.0},
            },
            "gates": {"signal_history_len": 0, "last_flip_at": "N/A", "flips_30d": 0, "max_flips_30d": 2, "last_hold_reason": "", "confirm_days": 2, "confirm_streak": 0},
            "last_status": {},
            "last_error": {},
            "last_rotation": {},
            "shadow_swap": {"count_90d": 0, "last": {}, "recent": []},
            "signal_layers": {
                "eod": {},
                "runtime": {
                    "active_asset_runtime": "BTC",
                    "holding_asset_runtime": "BTC",
                    "signal_target_asset_runtime": "GOLD",
                    "cdc_status_runtime": "down",
                    "signal_source_runtime": "okx_ratio",
                    "runtime_ts_utc": "2026-03-14T12:00:00+00:00",
                },
                "mismatch": False,
                "mismatch_streak_days": 0,
                "mismatch_severity": "match",
            },
            "why_not_flip": {
                "decision": "HOLD",
                "reason": "gate_cdc_down_required",
                "next_unlock_condition": "cdc_status must be down for 5 consecutive days",
                "next_unlock_min_days": 5,
                "days_since_last_swap": 10,
                "holding": "BTC",
                "target_asset": "GOLD",
            },
        },
    )

    client = app.app.test_client()
    response = client.get("/s4")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "target=GOLD" in body
    assert "holding=BTC, target=GOLD" in body


@contextmanager
def fake_shadow_swaps_cursor():
    class Cursor:
        def __init__(self):
            self.step = 0
            self.description = []

        def execute(self, query, params=None):
            self.step += 1
            self.description = [
                ("executed_at",),
                ("from_asset",),
                ("to_asset",),
                ("notional_usd",),
                ("cdc_status",),
                ("reason",),
                ("metadata_json",),
            ]

        def fetchall(self):
            if self.step != 1:
                return []
            return [
                (
                    "2026-03-14T12:00:00+00:00",
                    "BTC",
                    "GOLD",
                    0.0,
                    "down",
                    "shadow_swap_heartbeat",
                    json.dumps(
                        {
                            "analytics_runtime_mismatch": True,
                            "mismatch_severity": "warn",
                            "mismatch_streak_days": 2,
                            "eod_asof_date": "2026-03-12",
                            "runtime_signal_ts": "2026-03-14T12:00:00+00:00",
                            "holding_asset": "BTC",
                            "target_asset": "GOLD",
                            "gate": {
                                "decision": "HOLD",
                                "reason": "gate_cdc_down_required",
                                "next_unlock_condition": "cdc_status must be down for 5 consecutive days",
                                "next_unlock_min_days": 5,
                            },
                        }
                    ),
                ),
                (
                    "2026-03-13T12:00:00+00:00",
                    "GOLD",
                    "BTC",
                    250.0,
                    "up",
                    "shadow_swap_plan",
                    json.dumps(
                        {
                            "analytics_runtime_mismatch": False,
                            "holding_asset": "GOLD",
                            "target_asset": "BTC",
                        }
                    ),
                ),
            ]

    yield Cursor(), None


@contextmanager
def fake_shadow_summary_cursor():
    class Cursor:
        def __init__(self):
            self.step = 0

        def execute(self, query, params=None):
            self.step += 1

        def fetchall(self):
            return [
                (
                    "shadow_swap_heartbeat",
                    json.dumps(
                        {
                            "analytics_runtime_mismatch": True,
                            "gate": {"decision": "HOLD"},
                        }
                    ),
                ),
                (
                    "shadow_swap_plan",
                    json.dumps({"gate": {"decision": "SWAP_TO_BTC"}}),
                ),
            ]

    yield Cursor(), None


def test_api_s4_shadow_swaps_returns_unlock_and_mismatch(monkeypatch):
    monkeypatch.setattr(app, "get_db_cursor", fake_shadow_swaps_cursor)

    client = app.app.test_client()
    response = client.get("/api/s4_shadow_swaps?decision=all&reason=all&include_mismatch=false")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 2
    assert payload["items"][0]["decision"] == "HOLD"
    assert payload["items"][0]["analytics_runtime_mismatch"] is True
    assert payload["items"][0]["next_unlock_condition"] == "cdc_status must be down for 5 consecutive days"
    assert payload["items"][0]["metadata_json"]["holding_asset"] == "BTC"
    assert payload["items"][0]["metadata_json"]["target_asset"] == "GOLD"
    assert payload["items"][1]["decision"] == "SWAP_TO_BTC"


def test_api_s4_shadow_swaps_summary_counts_decisions_and_mismatch(monkeypatch):
    monkeypatch.setattr(app, "get_db_cursor", fake_shadow_summary_cursor)

    client = app.app.test_client()
    response = client.get("/api/s4_shadow_swaps_summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["windows"] == [30, 60, 90]
    for key in ("30", "60", "90"):
        summary = payload["summary"][key]
        assert summary["count"] == 2
        assert summary["mismatch_count"] == 1
        assert summary["decision_counts"]["HOLD"] == 1
        assert summary["decision_counts"]["SWAP_TO_BTC"] == 1


@contextmanager
def fake_strategies_cursor():
    class Cursor:
        def __init__(self):
            self.last_query = ""

        def execute(self, query, params=None):
            self.last_query = query

        def fetchall(self):
            if "FROM strategy_state" in self.last_query:
                metadata = {
                    "config": {"exchange": "okx"},
                    "runtime": {
                        "active_asset": "BTC",
                        "signal_target_asset": "GOLD",
                        "last_cdc_status": "down",
                    },
                }
                return [
                    (
                        "s4_multi_leg",
                        1,
                        "down",
                        None,
                        None,
                        0.0,
                        0.0,
                        0.0,
                        35.0,
                        35.0,
                        "active",
                        json.dumps(metadata),
                        "auto_proportional",
                        50,
                        50,
                        0,
                    )
                ]
            return []

        def fetchone(self):
            if "COUNT(*), COALESCE(SUM(purchase_amount), 0)" in self.last_query:
                return (0, 0.0)
            if "FROM purchase_history" in self.last_query:
                return None
            return None

    yield Cursor(), None


def test_api_strategies_normalizes_holding_asset_runtime(monkeypatch):
    monkeypatch.setattr(app, "get_db_cursor", fake_strategies_cursor)
    monkeypatch.setattr(app, "_env_flag", lambda name, default=False: True if name == "FEATURE_S4_ENABLED" else default)
    monkeypatch.setattr(app, "get_total_active_amount", lambda: 0.0)
    monkeypatch.setattr(app, "get_adapter", lambda *args, **kwargs: None)

    client = app.app.test_client()
    response = client.get("/api/strategies")

    assert response.status_code == 200
    payload = response.get_json()
    s4 = next(item for item in payload["strategies"] if item["id"] == "s4_multi_leg")
    assert s4["runtime"]["holding_asset"] == "BTC"
    assert s4["runtime"]["active_asset"] == "BTC"
    assert s4["runtime"]["signal_target_asset"] == "GOLD"
