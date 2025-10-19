import unittest
from unittest.mock import patch

from notify import (
    _format_holdings_line,
    notify_s4_dca_buy,
    notify_s4_rotation,
    notify_weekly_dca_buy,
    notify_weekly_dca_skipped,
    notify_weekly_dca_skipped_exchange,
    notify_half_sell_executed,
    notify_reserve_buy_executed,
)


class NotifyHoldingsFormatTest(unittest.TestCase):
    def test_basic_holdings_format(self):
        base_time = 1000.0
        holdings = {
            'BTC': {'free': 0.1234567, 'locked': 0.0, 'updated_at': base_time, 'stale': False},
            'XAUT': {'free': 0.005, 'locked': 0.001, 'updated_at': base_time, 'stale': False},
        }

        with patch('notify.time.time', return_value=base_time):
            line = _format_holdings_line(holdings)

        self.assertIn('Holdings:', line)
        self.assertIn('BTC 0.123457', line)
        self.assertIn('XAUT 0.005000 (+0.001000 locked)', line)
        self.assertNotIn('cached', line)

    def test_stale_holdings_with_meta_error(self):
        base_time = 1000.0
        holdings = {
            'BTC': {'free': 0.1, 'locked': 0.0, 'updated_at': base_time - 100.0, 'stale': True},
        }
        meta = {'errors': {'binance': 'adapter_not_available'}}

        with patch('notify.time.time', return_value=base_time):
            line = _format_holdings_line(holdings, meta)

        self.assertIn('Holdings:', line)
        self.assertIn('BTC 0.100000', line)
        self.assertIn('cached 100s', line)

    def test_notify_weekly_dca_buy_with_holdings_line(self):
        base_time = 2000.0
        payload = {
            'timestamp': '2025-01-01T00:00:00Z',
            'exchange': 'binance',
            'usdt': 100.0,
            'btc_qty': 0.0025,
            'price': 40000.0,
            'schedule_id': 1,
            'order_id': 123,
            'holdings': {'BTC': {'free': 0.5, 'locked': 0.0, 'updated_at': base_time, 'stale': False}},
        }
        with patch('notify.flex_allowed', return_value=False), \
                patch('notify.send_line_message_with_retry') as mock_send, \
                patch('notify.time.time', return_value=base_time):
            notify_weekly_dca_buy(payload)
        message = mock_send.call_args[0][0]
        self.assertIn('Holdings: BTC 0.500000', message)

    def test_notify_weekly_dca_skipped_includes_holdings(self):
        base_time = 3000.0
        context = {
            'timestamp': '2025-01-01T00:05:00Z',
            'holdings': {'BTC': {'free': 0.2, 'locked': 0.0, 'updated_at': base_time, 'stale': True}},
        }
        with patch('notify.flex_allowed', return_value=False), \
                patch('notify.send_line_message_with_retry') as mock_send, \
                patch('notify.time.time', return_value=base_time + 45):
            notify_weekly_dca_skipped(50.0, 250.0, context=context)
        message = mock_send.call_args[0][0]
        self.assertIn('Holdings:', message)
        self.assertIn('cached 45s', message)

    def test_notify_weekly_dca_skipped_exchange_includes_holdings(self):
        base_time = 4000.0
        context = {
            'timestamp': '2025-01-01T00:10:00Z',
            'holdings': {'BTC': {'free': 0.3, 'locked': 0.01, 'updated_at': base_time, 'stale': False}},
        }
        with patch('notify.flex_allowed', return_value=False), \
                patch('notify.send_line_message_with_retry') as mock_send, \
                patch('notify.time.time', return_value=base_time):
            notify_weekly_dca_skipped_exchange('binance', 25.0, 300.0, context=context)
        message = mock_send.call_args[0][0]
        self.assertIn('Holdings: BTC 0.300000 (+0.010000 locked)', message)


class NotifyS4FormatTest(unittest.TestCase):
    def test_s4_header_includes_schedule_context(self):
        payload = {
            'asset': 'XAUT',
            'qty': 0.003537,
            'price': 4240.0,
            'usdt': 15.0,
            'exchange': 'okx',
            'schedule_id': 20,
            'schedule_label': 'BTC-Information',
            'dry_run': True,
            'cdc_status': 'down',
            'holdings': {'BTC': {'free': 0.0, 'locked': 0.0, 'updated_at': 0, 'stale': False}},
        }
        with patch('notify.send_line_message_with_retry') as mock_send:
            notify_s4_dca_buy(payload)
        message = mock_send.call_args[0][0]
        self.assertTrue(message.startswith('S4 DCA Buy'))
        self.assertNotIn('BTC-Information', message.splitlines()[0])
        self.assertIn('Amount: 15.00 USDT', message)
        self.assertIn('Qty: 0.003537 XAUT @ 4,240.00', message)
        self.assertIn('Schedule: #20', message)
        self.assertIn('CDC: DOWN', message)
        self.assertIn('Mode: DRY RUN', message)


class NotifyFlexRoutingTest(unittest.TestCase):
    def setUp(self):
        self.payload = {
            'timestamp': '2025-01-01T00:00:00Z',
            'exchange': 'binance',
            'usdt': 100.0,
            'btc_qty': 0.0025,
            'price': 40000.0,
            'schedule_id': 1,
            'order_id': 123,
            'cdc_status': 'up',
        }

    def test_weekly_dca_buy_prefers_flex_when_allowed(self):
        with patch('notify.flex_allowed', return_value=True), \
                patch('notify.send_line_flex_with_retry', return_value=True) as mock_flex, \
                patch('notify.send_line_message_with_retry') as mock_text:
            notify_weekly_dca_buy(self.payload)
        mock_flex.assert_called_once()
        mock_text.assert_not_called()

    def test_weekly_dca_buy_fallback_when_flex_fails(self):
        with patch('notify.flex_allowed', return_value=True), \
                patch('notify.send_line_flex_with_retry', return_value=False), \
                patch('notify.send_line_message_with_retry') as mock_text:
            notify_weekly_dca_buy(self.payload)
        mock_text.assert_called_once()

    def test_weekly_dca_skipped_exchange_uses_flex(self):
        context = {'timestamp': '2025-01-01T00:05:00Z', 'cdc_status': 'down'}
        with patch('notify.flex_allowed', return_value=True), \
                patch('notify.send_line_flex_with_retry', return_value=True) as mock_flex, \
                patch('notify.send_line_message_with_retry') as mock_text:
            notify_weekly_dca_skipped_exchange('binance', 25.0, 300.0, context=context)
        mock_flex.assert_called_once()
        mock_text.assert_not_called()

    def test_reserve_buy_uses_flex(self):
        payload = {
            'timestamp': '2025-01-01T00:10:00Z',
            'exchange': 'binance',
            'spend': 200.0,
            'btc_qty': 0.0045,
            'price': 44500.0,
            'reserve_left': 800.0,
            'order_id': 555,
            'cdc_status': 'down',
        }
        with patch('notify.flex_allowed', return_value=True), \
                patch('notify.send_line_flex_with_retry', return_value=True) as mock_flex, \
                patch('notify.send_line_message_with_retry') as mock_text:
            notify_reserve_buy_executed(payload)
        mock_flex.assert_called_once()
        mock_text.assert_not_called()

    def test_half_sell_uses_flex(self):
        payload = {
            'timestamp': '2025-01-01T00:15:00Z',
            'exchange': 'okx',
            'btc_qty': 0.01,
            'price': 43000.0,
            'usdt': 430.0,
            'order_id': 777,
            'pct': 50,
            'cdc_status': 'up',
        }
        with patch('notify.flex_allowed', return_value=True), \
                patch('notify.send_line_flex_with_retry', return_value=True) as mock_flex, \
                patch('notify.send_line_message_with_retry') as mock_text:
            notify_half_sell_executed(payload)
        mock_flex.assert_called_once()
        mock_text.assert_not_called()

    def test_s4_dca_uses_flex(self):
        payload = {
            'timestamp': '2025-01-01T00:20:00Z',
            'asset': 'XAUT',
            'exchange': 'okx',
            'usdt': 15.0,
            'qty': 0.0035,
            'price': 4240.0,
            'schedule_id': 20,
            'order_id': 999,
            'cdc_status': 'up',
        }
        with patch('notify.flex_allowed', return_value=True), \
                patch('notify.send_line_flex_with_retry', return_value=True) as mock_flex, \
                patch('notify.send_line_message_with_retry') as mock_text:
            notify_s4_dca_buy(payload)
        mock_flex.assert_called_once()
        mock_text.assert_not_called()

    def test_s4_rotation_uses_flex(self):
        payload = {
            'timestamp': '2025-01-01T00:25:00Z',
            'exchange': 'okx',
            'amount_usd': 5000.0,
            'from': 'BTC',
            'to': 'GOLD',
            'cdc_status': 'down',
        }
        with patch('notify.flex_allowed', return_value=True), \
                patch('notify.send_line_flex_with_retry', return_value=True) as mock_flex, \
                patch('notify.send_line_message_with_retry') as mock_text:
            notify_s4_rotation(payload)
        mock_flex.assert_called_once()
        mock_text.assert_not_called()

if __name__ == '__main__':
    unittest.main()
