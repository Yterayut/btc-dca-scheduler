import unittest
from contextlib import contextmanager
from unittest.mock import patch

import main


class FeeTotalsAccumulationTest(unittest.TestCase):
    def test_record_fee_totals_inserts_with_normalized_keys(self):
        captured = {}

        @contextmanager
        def fake_tx():
            class Cursor:
                def execute(self, query, params):
                    captured['query'] = query
                    captured['params'] = params

            yield Cursor(), None

        with patch('main.db_transaction', fake_tx):
            main.record_fee_totals('CDC_Weekly_DCA', 'Binance', 'buy', 1.5, 'bnb', 0.01)

        self.assertIn('INSERT INTO strategy_fee_totals', captured['query'])
        params = captured['params']
        self.assertEqual(params[0], 'binance')
        self.assertEqual(params[1], 'cdc_weekly_dca')
        self.assertEqual(params[2], 'buy')
        self.assertEqual(params[3], 'BNB')
        self.assertAlmostEqual(params[4], 1.5)
        self.assertAlmostEqual(params[5], 0.01)

    def test_record_fee_totals_skips_when_zero(self):
        invoked = {'value': False}

        @contextmanager
        def fake_tx():
            invoked['value'] = True
            class Cursor:
                def execute(self, query, params):
                    pass
            yield Cursor(), None

        with patch('main.db_transaction', fake_tx):
            main.record_fee_totals('cdc', 'binance', 'buy', 0.0, None, 0.0)

        self.assertFalse(invoked['value'])


if __name__ == '__main__':
    unittest.main()
