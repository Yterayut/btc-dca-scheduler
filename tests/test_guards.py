import main


class StubAdapter:
    def __init__(self, price: float, bids=None, asks=None, closes=None):
        self.price = price
        self._bids = bids or [(price * 0.999, 1000)]
        self._asks = asks or [(price * 1.001, 1000)]
        self._closes = closes or [price] * 10

    def get_price(self):
        return self.price

    def get_depth_snapshot(self, *, limit: int = 20):
        return {'bids': self._bids, 'asks': self._asks}

    def get_recent_candles(self, *, interval: str = "1m", limit: int = 30):
        candles = []
        for idx, close in enumerate(self._closes[-limit:]):
            candles.append(
                {
                    'open_time': idx * 60000,
                    'open': close,
                    'high': close,
                    'low': close,
                    'close': close,
                    'volume': 1.0,
                    'close_time': (idx + 1) * 60000,
                }
            )
        return candles


def test_depth_guard_blocks_thin_book():
    adapter = StubAdapter(60000.0, bids=[(100.0, 0.1)], asks=[(60010.0, 0.1)])
    ok, info = main.evaluate_depth_guard(adapter, 'binance', 60000.0)
    assert not ok
    assert info['reason'] == 'depth_insufficient'


def test_depth_guard_passes_when_notional_sufficient():
    adapter = StubAdapter(60000.0, bids=[(59950.0, 5000)], asks=[(60050.0, 5000)])
    ok, _ = main.evaluate_depth_guard(adapter, 'binance', 60000.0)
    assert ok


def test_twap_guard_detects_deviation():
    closes = [60000.0] * 14 + [62000.0]
    adapter = StubAdapter(62000.0, closes=closes)
    main.ENABLE_TWAP_GUARD = True
    ok, info = main.evaluate_twap_guard(adapter, 'binance', 62000.0)
    assert not ok
    assert info['reason'] == 'twap_deviation'


def test_notional_cap_blocks_when_exceeded():
    original = main.is_dry_run
    try:
        main.is_dry_run = lambda: False  # ensure guard active
        ok, info = main.evaluate_notional_cap('binance', 2000.0, {'binance_max_usdt': 1000.0})
        assert not ok
        assert info['cap'] == 1000.0
    finally:
        main.is_dry_run = original


def test_notional_cap_allows_within_limit():
    ok, info = main.evaluate_notional_cap('binance', 500.0, {'binance_max_usdt': 1000.0})
    assert ok
    assert info['cap'] == 1000.0
