"""Synthetic behavior only: no real Telegram messages or market orders."""
import json
import os
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from django.test import Client, override_settings
from . import telegram_alerts as alerts
from . import telegram_transport as transport

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc).timestamp()


def payload(now=NOW, **changes):
    at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    analysis = {'decision': 'LONG', 'passed': 6, 'score': 100, 'blockers': [],
        'data_quality': {'action_blocked': False, 'version': 'stream_exchange_v1', 'book_consistency_confirmed': True},
        'rules': [{'key': key, 'pass': True} for key in sorted(alerts.RULES)],
        'updated_at': at, 'data_time': at, 'plan': {'entry': 100., 'stop': 99., 'target': 102.}}
    analysis.update(changes)
    return {'status': 'ok', 'mode': 'live', 'instrument': {'ticker': 'SBER'},
            'analysis': analysis, 'trading_status': 'SECURITY_TRADING_STATUS_NORMAL_TRADING'}


class AlertTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = alerts.Store(Path(self.temp.name)/'alerts.sqlite3')
        self.env = patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'fake:token', 'TELEGRAM_CHAT_ID': 'fake-chat', 'INVEST_TOKEN': 'fake'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sender = Mock(return_value={'status': 'ok', 'message': 'Telegram подтвердил отправку.'})

    def enable(self):
        self.store.save(True, ['SBER'])

    def observe(self, value=None, now=NOW):
        return self.store.observe('SBER', value or payload(now), now=now, sender=self.sender)

    def test_default_off_and_no_send(self):
        self.assertFalse(self.store.config()['enabled'])
        self.assertEqual(self.observe(), 'disabled')
        self.sender.assert_not_called()

    def test_same_destination_secrets_not_in_status(self):
        status = json.dumps(self.store.status(NOW))
        self.assertNotIn('fake:token', status)
        self.assertNotIn('fake-chat', status)
        self.assertTrue(self.store.status(NOW)['configured'])

    def test_one_live_candidate_persistent_dedup_restart(self):
        self.enable()
        self.assertEqual(self.observe(), 'ok')
        self.assertEqual(self.observe(now=NOW+5), 'suppressed')
        self.assertEqual(alerts.Store(self.store.path).observe('SBER', payload(NOW+4000), now=NOW+4000, sender=self.sender), 'suppressed')
        self.sender.assert_called_once()

    def test_no_send_for_demo_or_errors_or_stale_or_unverified(self):
        self.enable()
        cases = []
        for key, value in [('mode', 'demo'), ('status', 'loading'), ('status', 'error'), ('refreshing', True),
                           ('instrument', {'ticker': 'GAZP'}), ('trading_status', 'closed')]:
            p = payload(); p[key] = value; cases.append(p)
        for changes in [{'blockers': ['blocked']}, {'data_quality': {}},
                        {'data_quality': {'action_blocked': True}}, {'plan': None},
                        {'passed': 5}, {'rules': []}, {'decision': 'WATCH'},
                        {'data_time': '2026-10-21T11:59:00+00:00'},
                        {'updated_at': '2026-10-21T11:59:00+00:00'},
                        {'updated_at': '2026-10-21T12:00:01+00:00'}]:
            cases.append(payload(**changes))
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.observe(case), 'blocked')
        self.sender.assert_not_called()

    def test_nonfinite_zero_inverted_and_boolean_prices_blocked(self):
        self.enable()
        for entry in [float('nan'), float('inf'), -100, 0, True, '100', 103]:
            self.assertEqual(self.observe(payload(plan={'entry': entry, 'stop': 99, 'target': 102})), 'blocked')
        self.sender.assert_not_called()

    def test_missing_or_duplicate_rules_blocked(self):
        self.enable()
        p = payload(); p['analysis']['rules'][0] = p['analysis']['rules'][1].copy()
        self.assertEqual(self.observe(p), 'blocked')

    def test_rearm_requires_fresh_failed_rule_and_cooldown(self):
        self.enable(); self.observe()
        wait = payload(NOW+20, decision='WAIT', passed=5, plan=None)
        wait['analysis']['rules'][0]['pass'] = False
        self.assertEqual(self.observe(wait, NOW+20), 'waiting')
        self.assertEqual(self.observe(now=NOW+25), 'suppressed')
        self.assertEqual(self.observe(now=NOW+alerts.COOLDOWN), 'ok')
        self.assertEqual(self.sender.call_count, 2)

    def test_data_gap_does_not_rearm(self):
        self.enable(); self.observe()
        self.observe({'status': 'error'}, NOW+100)
        self.assertEqual(self.observe(now=NOW+4000), 'suppressed')

    def test_off_on_does_not_reset_episode(self):
        self.enable(); self.observe()
        self.store.save(False, ['SBER']); self.enable()
        self.assertEqual(self.observe(now=NOW+4000), 'suppressed')

    def test_unknown_delivery_does_not_retry(self):
        self.enable(); self.sender.side_effect = TimeoutError('never logged')
        self.assertEqual(self.observe(), 'unknown')
        self.assertEqual(self.observe(now=NOW+4000), 'suppressed')
        self.sender.assert_called_once()

    def test_confirmed_rejection_backoff_then_only_fresh_retry(self):
        self.enable(); self.sender.return_value = {'status': 'rejected', 'retry_after': 90}
        self.assertEqual(self.observe(), 'rejected')
        self.assertEqual(self.observe(now=NOW+60), 'suppressed')
        self.sender.return_value = {'status': 'ok'}
        self.assertEqual(self.observe(payload(), NOW+90), 'blocked')
        self.assertEqual(self.observe(now=NOW+90), 'ok')

    def test_concurrent_observations_send_only_once(self):
        self.enable()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.observe(), range(4)))
        self.assertEqual(results.count('ok'), 1)
        self.sender.assert_called_once()

    def test_worker_lease_excludes_other_process_and_recovers(self):
        self.assertTrue(self.store.lease('a', NOW))
        self.assertFalse(self.store.lease('b', NOW+5))
        self.assertTrue(self.store.lease('b', NOW+alerts.LEASE_SECONDS+1))
        self.store.release('a')
        self.assertFalse(self.store.lease('a', NOW+alerts.LEASE_SECONDS+2))

    def test_status_delivery_survives_next_observation(self):
        self.enable(); self.observe(); self.observe(now=NOW+5)
        item = self.store.status(NOW+5)['items'][0]
        self.assertEqual(item['result'], 'ok')
        self.assertIn('подтвердил', item['delivery_detail'])

    def test_message_not_profit_probability_or_order(self):
        text = alerts.message('SBER', payload()['analysis'])
        self.assertIn('Кандидат покупки', text)
        self.assertIn('Стоп сценария: 99', text)
        self.assertIn('Доходность не доказана', text)
        self.assertIn('не рекомендация нейросети', text)
        self.assertNotIn('100%', text)

    def test_tiny_positive_quotes_never_become_zero_in_message(self):
        a = payload(plan={'entry': 2e-10, 'stop': 1e-10, 'target': 3e-10})['analysis']
        self.assertTrue(alerts.buy_candidate(a))
        self.assertIn('2e-10 ₽', alerts.message('SBER', a))

    def test_invalid_settings_and_missing_credentials(self):
        for value in [[], ['<SBER>'], 'SBER', [None]]:
            with self.assertRaises(ValueError): self.store.save(True, value)
        with self.assertRaises(ValueError): self.store.save('true', ['SBER'])
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': ''}):
            with self.assertRaises(ValueError): self.enable()
        with patch.dict(os.environ, {'INVEST_TOKEN': ''}):
            with self.assertRaises(ValueError): self.enable()

    def test_worker_uses_live_only_without_browser_and_releases_lease(self):
        self.enable()
        stop = threading.Event()
        def observed(ticker, value):
            stop.set()
            self.assertEqual(ticker, 'SBER')
        with patch('dashboard.signal_service.get_signal', return_value=payload()) as get, \
                patch.object(self.store, 'observe', side_effect=observed), \
                patch('dashboard.telegram_alerts.collector_status', return_value={'active_tickers': ['SBER']}):
            alerts.worker_loop(stop, self.store)
        get.assert_called_once_with('live', 'SBER')
        self.assertTrue(self.store.lease('new-worker', __import__('time').time()))


class TransportTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'fake:token', 'TELEGRAM_CHAT_ID': 'fake-chat'})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_post_same_bot_chat_timeout_and_confirmed_delivery(self):
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{"ok":true,"result":{"message_id":1}}')))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(transport.send('<b>SBER</b>')['status'], 'ok')
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.telegram.org/botfake:token/sendMessage')
        self.assertIn(b'chat_id=fake-chat', req.data)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 8)

    def test_timeout_has_no_secret_exception_text(self):
        with patch('urllib.request.build_opener', side_effect=TimeoutError('fake:token')):
            result = transport.send('test')
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('fake:token', json.dumps(result))

    def test_429_respects_retry_after_and_403_rejected(self):
        from io import BytesIO
        for code, data, retry in [(429, b'{"parameters":{"retry_after":180}}', 180), (403, b'{}', 300)]:
            error = urllib.error.HTTPError('secret-url', code, 'secret', {}, BytesIO(data))
            opener = Mock(); opener.open.side_effect = error
            with patch('urllib.request.build_opener', return_value=opener):
                result = transport.send('test')
            self.assertEqual(result['status'], 'rejected')
            self.assertEqual(result['retry_after'], retry)

    def test_redirects_never_forward_bot_secret(self):
        self.assertIsNone(transport.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))


class AlertEndpointTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.override = override_settings(BASE_DIR=Path(self.temp.name))
        self.override.enable(); self.addCleanup(self.override.disable)
        self.env = patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'fake:token', 'TELEGRAM_CHAT_ID': 'fake-chat', 'INVEST_TOKEN': 'fake'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.worker = patch('dashboard.telegram_views.ensure_worker')
        self.worker_mock = self.worker.start(); self.addCleanup(self.worker.stop)
        self.client = Client(enforce_csrf_checks=True)

    def test_get_readonly_post_csrf_and_local_only(self):
        response = self.client.get('/api/telegram-alerts/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertFalse(response.json()['enabled'])
        self.assertEqual(self.client.post('/api/telegram-alerts/', '{}', content_type='application/json').status_code, 403)
        token = self.client.cookies['csrftoken'].value
        response = self.client.post('/api/telegram-alerts/', json.dumps({'enabled': True, 'tickers': ['SBER']}),
            content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['enabled'])
        self.worker_mock.assert_called_once()
        self.assertEqual(self.client.get('/api/telegram-alerts/', REMOTE_ADDR='203.0.113.1').status_code, 403)

    def test_invalid_body_and_methods_do_not_enable(self):
        self.client.get('/api/telegram-alerts/')
        token = self.client.cookies['csrftoken'].value
        for body in ('[]', '{', '{"enabled":true,"tickers":[]}'):
            self.assertEqual(self.client.post('/api/telegram-alerts/', body, content_type='application/json', HTTP_X_CSRFTOKEN=token).status_code, 400)
        self.assertFalse(alerts.Store().config()['enabled'])
        self.assertEqual(self.client.delete('/api/telegram-alerts/', HTTP_X_CSRFTOKEN=token).status_code, 405)

    def test_panel_on_single_and_watchlist(self):
        for path in ('/', '/watchlist/'):
            response = self.client.get(path)
            self.assertContainsText(response.content.decode())

    def assertContainsText(self, text):
        for expected in ('id="telegram-form"', 'id="telegram-tickers"', 'js/telegram_alerts.js?v=2',
                         'csrfmiddlewaretoken', 'Доходность', 'Нужен запущенный сборщик'):
            self.assertIn(expected, text)

    def test_large_list_saves_over_old_4kb_limit_without_enabling(self):
        self.client.get('/api/telegram-alerts/')
        token = self.client.cookies['csrftoken'].value
        tickers = [f'STOCK{i}' for i in range(1000)]
        response = self.client.post('/api/telegram-alerts/', json.dumps({'enabled': False, 'tickers': tickers}),
            content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['watchlist_count'], 1000)
        self.assertEqual(alerts.Store().config()['tickers'], tickers)
        self.worker_mock.assert_not_called()
