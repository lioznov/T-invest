"""Private radar notifications, simulated provider and market stream only."""
import asyncio
import json
import os
import queue
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from django.template.loader import render_to_string
from django.test import Client, override_settings
from . import radar_vk as radar
from . import vk_transport

ENV = {'VK_COMMUNITY_TOKEN':'synthetic-vk-key','VK_USER_ID':'123456','INVEST_TOKEN':''}


class RadarVKTests(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,ENV);self.env.start();self.addCleanup(self.env.stop)
        self.store=radar.Store(Path(self.temp.name)/'radar.sqlite3')
        self.sender=Mock(return_value={'status':'ok','message':'accepted'})
        self.send=patch.object(vk_transport,'send',self.sender);self.send.start();self.addCleanup(self.send.stop)

    def test_channels_default_off_no_message_on_status_or_event(self):
        self.assertFalse(self.store.status('single')['enabled'])
        self.assertFalse(self.store.status('multi')['enabled'])
        self.assertEqual(self.store.observe('single','figi','event',now=100),'disabled')
        self.sender.assert_not_called()
        self.assertNotIn('synthetic-vk-key',json.dumps(self.store.status('single')))

    def test_toggle_sends_named_confirmation_once_and_survives_restart(self):
        self.store.set_enabled('single',True)
        self.assertIn('Одиночный радар',self.sender.call_args.args[0])
        radar.Store(self.store.path).set_enabled('single',True)
        self.assertEqual(self.sender.call_count,1)
        self.assertFalse(self.store.status('multi')['enabled'])
        self.store.set_enabled('single',False)
        self.assertIn('выключены',self.sender.call_args.args[0])
        self.assertEqual(self.store.observe('single','figi','event',now=100),'disabled')
        self.assertEqual(self.sender.call_count,2)

    def test_missing_configuration_cannot_enable_but_can_disable(self):
        self.store.set_enabled('multi',True)
        with patch.dict(os.environ,{'VK_COMMUNITY_TOKEN':''}):
            with self.assertRaises(ValueError):self.store.set_enabled('single',True)
            self.store.set_enabled('multi',False)
        self.assertFalse(self.store.status('multi')['enabled'])

    def test_cooldown_distinct_events_and_independent_radars(self):
        self.store.set_enabled('single',True);self.store.set_enabled('multi',True)
        self.sender.reset_mock()
        self.assertEqual(self.store.observe('single','figi','event A',now=100),'ok')
        self.assertEqual(self.store.observe('single','figi','event B',now=101),'suppressed')
        self.assertEqual(self.store.observe('multi','figi','event A',now=101),'ok')
        self.assertEqual(self.store.observe('single','figi','event B',now=111),'ok')
        self.assertEqual(self.sender.call_count,3)
        self.assertIn('не рекомендация покупать',self.sender.call_args.args[0])

    def test_unknown_event_not_automatically_retried_after_restart_or_toggle(self):
        self.store.set_enabled('single',True)
        self.sender.return_value={'status':'unknown','message':'not confirmed'}
        self.sender.reset_mock()
        self.assertEqual(self.store.observe('single','figi','same event',now=100),'unknown')
        self.assertEqual(radar.Store(self.store.path).observe('single','figi','same event',now=5000),'suppressed')
        self.assertEqual(self.sender.call_count,1)
        self.store.set_enabled('single',False);self.store.set_enabled('single',True)
        self.assertEqual(self.store.observe('single','figi','same event',now=6000),'suppressed')

    def test_concurrent_transition_and_event_are_reserved_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _:self.store.set_enabled('multi',True),range(2)))
            outcomes=list(pool.map(lambda _:self.store.observe('multi','figi','event',now=100),range(2)))
        self.assertEqual(self.sender.call_count,2)
        self.assertCountEqual(outcomes,['ok','suppressed'])

    def test_endpoint_read_is_passive_post_requires_csrf_and_local_origin(self):
        with override_settings(BASE_DIR=self.temp.name,ALLOWED_HOSTS=['testserver']):
            c=Client(enforce_csrf_checks=True)
            url='/api/radar-vk/?scope=single'
            response=c.get(url)
            self.assertEqual(response['Cache-Control'],'no-store');self.sender.assert_not_called()
            self.assertEqual(c.get(url,REMOTE_ADDR='8.8.8.8').status_code,403)
            self.assertEqual(c.post(url,data='{"enabled":true}',content_type='application/json').status_code,403)
            csrf=c.cookies['csrftoken'].value
            response=c.post(url,data='{"enabled":true}',content_type='application/json',HTTP_X_CSRFTOKEN=csrf)
            self.assertTrue(response.json()['enabled']);self.assertEqual(self.sender.call_count,1)
            c.get(url);self.assertEqual(self.sender.call_count,1)
            response=c.post(url,data='{"enabled":true,"peer_id":999}',content_type='application/json',HTTP_X_CSRFTOKEN=csrf)
            self.assertEqual(response.status_code,400);self.assertEqual(self.sender.call_count,1)

    def test_delivery_queue_drops_stale_jobs_and_isolates_storage_failure(self):
        with patch('dashboard.radar_vk.time.time',return_value=100),patch('dashboard.radar_vk.Store') as store:
            self.assertEqual(radar._deliver(('single','figi','event',1)),'stale');store.assert_not_called()
            store.side_effect=OSError('private diagnostics')
            self.assertEqual(radar._deliver(('single','figi','event',100)),'unavailable')

    def test_expiry_is_checked_again_after_acquiring_database_lock(self):
        self.store.set_enabled('single',True);self.sender.reset_mock()
        with patch('dashboard.radar_vk.time.time',return_value=200):
            self.assertEqual(self.store.observe('single','figi','event',now=100,created=100),'stale')
        self.sender.assert_not_called()

    def test_queue_is_bounded_without_blocking_market_stream(self):
        pending=queue.Queue(maxsize=1)
        thread=Mock();thread.is_alive.return_value=True
        with patch.object(radar,'PENDING',pending),patch.object(radar,'THREAD',thread):
            self.assertEqual(radar.submit('single','figi','one'),'queued')
            self.assertEqual(radar.submit('single','figi','two'),'busy')
        self.sender.assert_not_called()

    def test_radar_pages_have_independent_scope_and_keep_telegram_switches(self):
        for template,scope,tg in [('real_market.html','single','botToggle'),('radar.html','multi','multiBotToggle')]:
            page=render_to_string(template)
            self.assertIn(f'data-vk-radar="{scope}"',page)
            self.assertIn(f'id="{tg}"',page)
            self.assertIn('js/radar_vk.js',page)

    def test_both_real_stream_processors_forward_anomalies_to_vk_and_telegram(self):
        # Calibrate on 30 quiet snapshots, then introduce a large bid. No SDK connection.
        from . import views
        from t_tech.invest import Quotation
        async def snapshots():
            for i in range(31):
                book=SimpleNamespace(figi='FIGI',bids=[SimpleNamespace(price=Quotation(units=100,nano=0),quantity=10000 if i==30 else 100)],
                                     asks=[SimpleNamespace(price=Quotation(units=101,nano=0),quantity=100)])
                yield SimpleNamespace(subscribe_order_book_response=None,orderbook=book)
            raise asyncio.CancelledError()
        class FakeClient:
            market_data_stream=SimpleNamespace(market_data_stream=lambda _:snapshots())
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
        for scope in ('single','multi'):
            with self.subTest(scope=scope):
                ticks=iter(range(1000,1100))
                with patch.multiple(views,ACTIVE_FIGI='FIGI',TRACKED_FIGIS={'FIGI'},GLOBAL_DATA={},MULTI_GLOBAL_DATA={},
                                    TICKER_CACHE={'FIGI':'SBER'},BOT_ALERTS_ENABLED=True,MULTI_BOT_ALERTS_ENABLED=True), \
                     patch.object(views,'AsyncClient',return_value=FakeClient()), \
                     patch.object(views.time,'time',side_effect=lambda:next(ticks,1100)), \
                     patch.object(views,'send_telegram_message') as tg,patch.object(radar,'submit') as vk:
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(views.stream_data('FIGI') if scope=='single' else views.multi_stream_data())
                    self.assertEqual(vk.call_count,1)
                    self.assertEqual(vk.call_args.args[:2],(scope,'FIGI'))
                    self.assertIn('КАСКАД ПОКУПОК',vk.call_args.args[2])
                    tg.assert_called_once()
