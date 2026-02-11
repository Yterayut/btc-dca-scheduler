import time
import unittest
from typing import Dict

from services.balance_service import (
    DEFAULT_CACHE_TTL_SECONDS,
    clear_cache,
    fetch_balances,
)


class DummyAdapter:
    def __init__(self, responses: Dict[str, object]):
        self.responses = responses
        self.calls: list[str] = []

    def get_balance(self, asset: str) -> dict:
        self.calls.append(asset)
        payload = self.responses[asset]
        if isinstance(payload, Exception):
            raise payload
        return payload


def make_factory(mapping: Dict[str, DummyAdapter]):
    def _factory(exchanges, **_kwargs):
        return {ex: mapping[ex] for ex in exchanges}

    return _factory


class BalanceServiceTest(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def test_fetch_balances_success_and_cache(self):
        adapter = DummyAdapter({"BTC": {"free": 1, "locked": 0}})
        factory = make_factory({"binance": adapter})

        result = fetch_balances(["binance"], ["BTC"], adapter_factory=factory)
        entry = result["binance"]["BTC"]
        self.assertAlmostEqual(entry["free"], 1.0)
        self.assertFalse(entry["stale"])
        self.assertIsNone(result.get("_meta"))

        # Subsequent call should reuse cache (no extra adapter call)
        adapter.responses["BTC"] = RuntimeError("cache should prevent new call")
        cached = fetch_balances(["binance"], ["BTC"], adapter_factory=factory)
        self.assertAlmostEqual(cached["binance"]["BTC"]["free"], 1.0)
        self.assertFalse(cached["binance"]["BTC"]["stale"])
        self.assertEqual(adapter.calls.count("BTC"), 1)

    def test_fetch_balances_marks_stale_on_error_with_cache(self):
        adapter = DummyAdapter({"BTC": {"free": 0.5, "locked": 0.1}})
        factory = make_factory({"okx": adapter})

        initial = fetch_balances(["okx"], ["BTC"], adapter_factory=factory, cache_ttl=1)
        self.assertFalse(initial["okx"]["BTC"]["stale"])

        adapter.responses["BTC"] = RuntimeError("simulated outage")
        time.sleep(1.1)  # ensure TTL expires
        fallback = fetch_balances(["okx"], ["BTC"], adapter_factory=factory, cache_ttl=1)
        entry = fallback["okx"]["BTC"]
        self.assertTrue(entry["stale"])
        self.assertIn("error", entry)
        self.assertGreaterEqual(adapter.calls.count("BTC"), 2)

    def test_fetch_balances_without_cache_returns_zero_on_failure(self):
        adapter = DummyAdapter({"BTC": RuntimeError("down")})
        factory = make_factory({"binance": adapter})

        data = fetch_balances(
            ["binance"],
            ["BTC"],
            cache_ttl=DEFAULT_CACHE_TTL_SECONDS,
            adapter_factory=factory,
        )
        entry = data["binance"]["BTC"]
        self.assertTrue(entry["stale"])
        self.assertEqual(entry["free"], 0.0)
        self.assertIn("error", entry)
        self.assertIn("binance", data["_meta"]["errors"])


if __name__ == "__main__":
    unittest.main()
