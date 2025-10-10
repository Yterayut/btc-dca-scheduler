import asyncio
from datetime import datetime
import os, sys, types
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
# Set dummy envs to satisfy main.py validation
os.environ.setdefault('DB_HOST', 'localhost')
os.environ.setdefault('DB_USER', 'user')
os.environ.setdefault('DB_PASSWORD', 'pass')
os.environ.setdefault('DB_NAME', 'db')
os.environ.setdefault('BINANCE_API_KEY', 'key')
os.environ.setdefault('BINANCE_API_SECRET', 'secret')

# Preload stubs for unavailable deps (MySQLdb, binance)
if 'MySQLdb' not in sys.modules:
    MySQLdb_stub = types.SimpleNamespace()
    class _OpErr(Exception):
        pass
    MySQLdb_stub.OperationalError = _OpErr
    def _connect(*a, **k):
        raise _OpErr('stubbed MySQLdb.connect called (should be overridden by test)')
    MySQLdb_stub.connect = _connect
    sys.modules['MySQLdb'] = MySQLdb_stub

if 'binance' not in sys.modules:
    binance_mod = types.ModuleType('binance')
    client_mod = types.ModuleType('binance.client')
    class _Client:
        KLINE_INTERVAL_1DAY = '1d'
        def __init__(self, *a, **k):
            pass
    client_mod.Client = _Client
    exceptions_mod = types.ModuleType('binance.exceptions')
    class _APIEx(Exception):
        def __init__(self, *a, **k):
            super().__init__('BinanceAPIException')
            self.code = -1
            self.message = 'stub'
    exceptions_mod.BinanceAPIException = _APIEx
    sys.modules['binance'] = binance_mod
    sys.modules['binance.client'] = client_mod
    sys.modules['binance.exceptions'] = exceptions_mod

# Stub dotenv
if 'dotenv' not in sys.modules:
    dotenv_mod = types.ModuleType('dotenv')
    def _load_env():
        return True
    dotenv_mod.load_dotenv = _load_env
    sys.modules['dotenv'] = dotenv_mod

# Stub pytz.timezone
if 'pytz' not in sys.modules:
    pytz_mod = types.ModuleType('pytz')
    def _timezone(name):
        class _TZ:
            def localize(self, dt):
                return dt
        return _TZ()
    pytz_mod.timezone = _timezone
    sys.modules['pytz'] = pytz_mod

# Stub tenacity decorators
if 'tenacity' not in sys.modules:
    tenacity_mod = types.ModuleType('tenacity')
    def _retry(*a, **k):
        def deco(f):
            return f
        return deco
    def _stop_after_attempt(*a, **k):
        return None
    def _wait_exponential(*a, **k):
        return None
    tenacity_mod.retry = _retry
    tenacity_mod.stop_after_attempt = _stop_after_attempt
    tenacity_mod.wait_exponential = _wait_exponential
    sys.modules['tenacity'] = tenacity_mod

import main as m


class StubCursor:
    def __init__(self, state):
        self.state = state
        self._last = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        sql_low = sql.lower().strip()
        self._last = (sql_low, params)
        if 'select last_cdc_status' in sql_low:
            # return one row
            self._fetchone = (
                self.state['last_cdc_status'],
                self.state['reserve_usdt'],
                1 if self.state['red_epoch_active'] else 0,
            )
        elif 'select reserve_usdt' in sql_low:
            self._fetchone = (self.state['reserve_usdt'],)
        elif sql_low.startswith('update strategy_state set reserve_usdt = reserve_usdt +'):
            inc = float(params[0])
            self.state['reserve_usdt'] += inc
            self.rowcount = 1
        elif sql_low.startswith('update strategy_state set'):
            # naive parser for updates used by save_strategy_state
            sets = sql.split('set',1)[1].split('where',1)[0].split(',')
            for i, s in enumerate(sets):
                key = s.strip().split('=')[0]
                self.state[key] = params[i]
            self.rowcount = 1
        elif sql_low.startswith('insert into purchase_history'):
            self.state['purchase_history'].append(params)
            self.rowcount = 1
        elif sql_low.startswith('insert into sell_history'):
            self.state['sell_history'].append(params)
            self.rowcount = 1
        else:
            # ignore others for this dry-run
            self.rowcount = 0

    def executemany(self, *args, **kwargs):
        pass

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return []

    def close(self):
        pass


class StubDB:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return StubCursor(self.state)

    def commit(self):
        pass

    def close(self):
        pass


class StubClient:
    def __init__(self, state):
        self.state = state
        self._last_order = None

    # Balances
    def get_asset_balance(self, asset='USDT'):
        if asset == 'USDT':
            return {'free': str(self.state['usdt_free']), 'locked': '0'}
        if asset == 'BTC':
            return {'free': str(self.state['btc_free']), 'locked': '0'}
        return {'free': '0', 'locked': '0'}

    # Symbol/ticker
    def get_symbol_ticker(self, symbol='BTCUSDT'):
        return {'symbol': symbol, 'price': str(self.state['price'])}

    def get_symbol_info(self, symbol='BTCUSDT'):
        return {
            'symbol': symbol,
            'filters': [
                {'filterType': 'LOT_SIZE', 'minQty': '0.00001', 'stepSize': '0.00001'},
                {'filterType': 'NOTIONAL', 'minNotional': '10'}
            ]
        }

    # Orders
    def order_market_buy(self, symbol='BTCUSDT', quoteOrderQty=0):
        price = float(self.state['price'])
        spend = float(quoteOrderQty)
        qty = spend / price
        # adjust balances
        self.state['usdt_free'] -= spend
        self.state['btc_free'] += qty
        self._last_order = {
            'side': 'BUY', 'orderId': self.state['next_order_id'],
            'executedQty': qty, 'cummulativeQuoteQty': spend
        }
        self.state['next_order_id'] += 1
        return {'symbol': symbol, 'orderId': self._last_order['orderId']}

    def order_market_sell(self, symbol='BTCUSDT', quantity=0):
        price = float(self.state['price'])
        qty = float(quantity)
        proceeds = qty * price
        # adjust balances
        self.state['btc_free'] -= qty
        self.state['usdt_free'] += proceeds
        self._last_order = {
            'side': 'SELL', 'orderId': self.state['next_order_id'],
            'executedQty': qty, 'cummulativeQuoteQty': proceeds
        }
        self.state['next_order_id'] += 1
        return {'symbol': symbol, 'orderId': self._last_order['orderId']}

    def get_order(self, symbol='BTCUSDT', orderId=None):
        # return details for last order
        lo = self._last_order
        return {
            'orderId': orderId,
            'status': 'FILLED',
            'executedQty': str(lo['executedQty']),
            'cummulativeQuoteQty': str(lo['cummulativeQuoteQty'])
        }

    def get_klines(self, symbol='BTCUSDT', interval='1d', limit=300):
        # Not used in dry-run (we mock get_cdc_status_1d directly), but keep signature
        return []


async def main():
    # Shared test state
    state = {
        'last_cdc_status': None,
        'reserve_usdt': 0.0,
        'red_epoch_active': 0,
        'purchase_history': [],
        'sell_history': [],
        'usdt_free': 500.0,
        'btc_free': 0.02,
        'price': 60000.0,
        'next_order_id': 1000,
    }

    # Patch DB and client
    m.get_db_connection = lambda: StubDB(state)
    m.client = StubClient(state)

    # Patch notifications to console no-op
    m.notify_cdc_transition = lambda prev, curr: print(f"[notify] CDC: {prev} -> {curr}") or True
    m.notify_half_sell_executed = lambda data: print(f"[notify] half sell executed: {data}") or True
    m.notify_half_sell_skipped = lambda data: print(f"[notify] half sell skipped: {data}") or True
    m.notify_weekly_dca_skipped = lambda amount, reserve: print(f"[notify] weekly skipped, amount={amount}, reserve={reserve}") or True
    m.notify_weekly_dca_skipped_exchange = lambda exchange, amount, reserve: print(f"[notify] weekly skipped ({exchange}), amount={amount}, reserve={reserve}") or True
    m.notify_reserve_buy_executed = lambda data: print(f"[notify] reserve buy executed: {data}") or True
    m.notify_reserve_buy_skipped_min_notional = lambda data: print(f"[notify] reserve buy skipped: {data}") or True

    now = datetime.utcnow()

    # 1) Weekly DCA while GREEN
    m.get_cdc_status_1d = lambda *args, **kwargs: {'status': 'up', 'updated_at': now.isoformat()+'Z'}
    print("\n[STEP 1] Weekly DCA while GREEN")
    await m.gate_weekly_dca(now, schedule_id=3, amount=100.0)
    print(f"balances: usdt={state['usdt_free']:.2f}, btc={state['btc_free']:.8f}")

    # 2) Transition to RED -> half sell once
    print("\n[STEP 2] Transition to RED -> half sell once")
    prev_now = now
    m.get_cdc_status_1d = lambda *args, **kwargs: {'status': 'down', 'updated_at': prev_now.isoformat()+'Z'}
    m.check_cdc_transition_and_act(now)
    print(f"balances after half-sell: usdt={state['usdt_free']:.2f}, btc={state['btc_free']:.8f}")

    # 3) Weekly tick while RED -> reserve accumulation twice
    print("\n[STEP 3] Weekly DCA while RED (reserve accumulation)")
    await m.gate_weekly_dca(now, schedule_id=3, amount=100.0)
    await m.gate_weekly_dca(now, schedule_id=3, amount=100.0)
    print(f"reserve={state['reserve_usdt']:.2f}")

    # 4) Transition back to GREEN with partial funds
    print("\n[STEP 4] Transition back to GREEN -> reserve buy (partial if needed)")
    # Make USDT less than reserve to test partial buy
    state['usdt_free'] = 150.0
    m.get_cdc_status_1d = lambda *args, **kwargs: {'status': 'up', 'updated_at': now.isoformat()+'Z'}
    m.check_cdc_transition_and_act(now)
    print(f"balances after reserve buy: usdt={state['usdt_free']:.2f}, btc={state['btc_free']:.8f}")
    print(f"reserve left={state['reserve_usdt']:.2f}")

    print("\n[SUMMARY]")
    print(f"purchase_history rows: {len(state['purchase_history'])}")
    print(f"sell_history rows: {len(state['sell_history'])}")


if __name__ == '__main__':
    asyncio.run(main())
