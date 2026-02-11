#!/usr/bin/env python3
"""Synthetic backtest harness for CDC DCA strategy.

The backtest uses a deterministic price series (no external APIs) and reuses
main.py guard evaluation helpers to ensure parity with production rules.

Example:
    python scripts/backtest_cdc.py --weekly-usdt 100 --guard-report
"""
from __future__ import annotations

import argparse
import statistics
from dataclasses import dataclass

import main


@dataclass
class SyntheticAdapter:
    prices: list[float]

    def __post_init__(self) -> None:
        self.index = 0

    def get_price(self) -> float:
        return float(self.prices[self.index])

    def get_depth_snapshot(self, *, limit: int = 20) -> dict:
        price = self.get_price()
        band = 0.001
        bids = [(price * (1 - band), 1500.0)] * min(limit, 20)
        asks = [(price * (1 + band), 1500.0)] * min(limit, 20)
        return {'bids': bids, 'asks': asks}

    def get_recent_candles(self, *, interval: str = "1m", limit: int = 30) -> list[dict]:
        window = self.prices[max(0, self.index - limit + 1): self.index + 1]
        candles: list[dict] = []
        base_index = max(0, self.index - len(window) + 1)
        for idx, price in enumerate(window):
            candles.append({
                'open_time': (base_index + idx) * 60000,
                'close': price,
                'open': price,
                'high': price * 1.002,
                'low': price * 0.998,
                'volume': 5.0,
                'close_time': (base_index + idx + 1) * 60000,
            })
        return candles


PRICE_SERIES = [
    62500.0, 63250.0, 62880.0, 64010.0, 65125.0,
    64300.0, 63820.0, 63050.0, 62210.0, 61500.0,
    60780.0, 59840.0, 60220.0, 61010.0, 61850.0,
    61200.0, 60550.0, 59880.0, 59020.0, 58300.0,
    57540.0, 56880.0, 56050.0, 55420.0, 54800.0,
]


def run_backtest(weekly_usdt: float, cap_usdt: float, guard_report: bool = False) -> None:
    adapter = SyntheticAdapter(PRICE_SERIES)
    state = {'binance_max_usdt': cap_usdt}

    position_btc = 0.0
    invested_usdt = 0.0
    blocks = {'depth': 0, 'twap': 0, 'notional': 0}

    for idx, price in enumerate(PRICE_SERIES):
        adapter.index = idx
        depth_ok, depth_info = main.evaluate_depth_guard(adapter, 'binance', price)
        if not depth_ok:
            blocks['depth'] += 1
            continue
        twap_ok, twap_info = main.evaluate_twap_guard(adapter, 'binance', price)
        if not twap_ok:
            blocks['twap'] += 1
            continue
        cap_ok, _ = main.evaluate_notional_cap('binance', weekly_usdt, state)
        if not cap_ok:
            blocks['notional'] += 1
            continue
        position_btc += weekly_usdt / price
        invested_usdt += weekly_usdt

    current_price = PRICE_SERIES[-1]
    final_value = position_btc * current_price
    pnl = final_value - invested_usdt

    print("=== CDC Backtest (synthetic) ===")
    print(f"Weeks processed: {len(PRICE_SERIES)}")
    print(f"Weeks executed: {len(PRICE_SERIES) - sum(blocks.values())}")
    print(f"Weeks blocked (depth/twap/notional): {blocks['depth']} / {blocks['twap']} / {blocks['notional']}")
    print(f"Total invested: {invested_usdt:,.2f} USDT")
    print(f"BTC acquired: {position_btc:.6f} BTC")
    print(f"Final value @ {current_price:,.2f} = {final_value:,.2f} USDT")
    print(f"PnL: {pnl:,.2f} USDT")

    if guard_report:
        closes = PRICE_SERIES
        print("--- Guard Diagnostics ---")
        print(f"Close std dev: {statistics.pstdev(closes):,.2f}")
        print(f"Min/Max close: {min(closes):,.2f} / {max(closes):,.2f}")
        print(f"Depth guard enabled: {main.ENABLE_DEPTH_GUARD}")
        print(f"TWAP guard window: {main.TWAP_GUARD_WINDOW_MINUTES} minutes")
        print(f"Notional cap: {'Unlimited' if cap_usdt <= 0 else f'{cap_usdt:,.2f} USDT'}")


def main_cli() -> None:
    parser = argparse.ArgumentParser(description="Synthetic CDC DCA backtest")
    parser.add_argument('--weekly-usdt', type=float, default=50.0, help='Quote amount per tick (USDT)')
    parser.add_argument('--notional-cap', type=float, default=0.0, help='Binance notional cap (0=unlimited)')
    parser.add_argument('--guard-report', action='store_true', help='Print guard diagnostics')
    args = parser.parse_args()

    main.ENABLE_DEPTH_GUARD = True
    main.ENABLE_TWAP_GUARD = True

    run_backtest(args.weekly_usdt, args.notional_cap, guard_report=args.guard_report)


if __name__ == '__main__':
    main_cli()
