"""End-to-end settings and dispatch with synthetic market data; no network sends."""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from django.test import Client, override_settings
from django.core.management import call_command
from . import telegram_alerts as tg, vk_alerts as vk, radar_vk as radar
from . import telegram_transport, vk_transport
from .test_telegram_alerts import payload, NOW


class NotificationTests(TestCase):
    def setUp(self):
        # Import the legacy search module without starting its broker cache loader.
        with patch.dict(os.environ,{'INVEST_TOKEN':''}):
            from . import views
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=override_settings(BASE_DIR=self.temp.name,ALLOWED_HOSTS=['testserver']);self.settings.enable();self.addCleanup(self.settings.disable)
        self.env=patch.dict(os.environ,{'INVEST_TOKEN':'synthetic-invest','TELEGRAM_BOT_TOKEN':'synthetic-tg','TELEGRAM_CHAT_ID':'123',
                                      'VK_COMMUNITY_TOKEN':'synthetic-vk','VK_USER_ID':'123456'})
        self.env.start();self.addCleanup(self.env.stop)
        self.tg=Mock(return_value={'status':'ok','message':'Telegram принял сообщение.'})
        self.vk=Mock(return_value={'status':'ok','message':'ВК принял сообщение.'})
        for target,value in [('dashboard.telegram_transport.send',self.tg),('dashboard.vk_transport.send',self.vk),
                             ('dashboard.notification_views.ensure_telegram',Mock()),('dashboard.notification_views.ensure_vk',Mock())]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        self.client=Client(enforce_csrf_checks=True)

    def get(self,mode):return self.client.get('/api/notifications/',{'mode':mode})

    def post(self,mode,body):
        self.get(mode)
        return self.client.post('/api/notifications/?mode='+mode,data=json.dumps(body),content_type='application/json',
                                HTTP_X_CSRFTOKEN=self.client.cookies['csrftoken'].value)

    def save(self,mode,channel,enabled=True,tickers=None):
        body={'channel':channel,'enabled':enabled}
        if mode.startswith('analysis'):body['tickers']=tickers if tickers is not None else ['SBER']
        response=self.post(mode,body);self.assertEqual(response.status_code,200,response.content)
        return response.json()

    def observe(self,store,ticker='SBER',now=NOW,**changes):
        value=payload(now,**changes);value['instrument']={'ticker':ticker,'name':'Сбербанк' if ticker=='SBER' else ticker}
        # A deterministic sender still exercises canonical reservation/episode logic.
        return store.observe(ticker,value,now=now,sender=lambda text:store.transport.send(text))

    def test_all_modes_are_passive_on_get_and_both_channels_default_off(self):
        for mode in ('analysis-single','analysis-multi','radar-single','radar-multi'):
            r=self.get(mode);self.assertEqual(r['Cache-Control'],'no-store')
            self.assertFalse(any(c['enabled'] for c in r.json()['channels'].values()))
            self.assertNotIn('synthetic-',r.content.decode())
        self.tg.assert_not_called();self.vk.assert_not_called()

    def test_single_multi_and_channels_are_independent_and_persist(self):
        self.save('analysis-single','vk',tickers=['SBER'])
        self.save('analysis-multi','telegram',tickers=['GAZP','LKOH'])
        self.assertEqual(vk.Store().config()['tickers'],['SBER'])
        self.assertEqual(tg.Store().config()['tickers'],['GAZP','LKOH'])
        self.assertFalse(vk.Store().mode_config('multi')['enabled'])
        self.assertFalse(tg.Store().mode_config('single')['enabled'])
        self.save('analysis-single','vk',False)
        self.assertFalse(vk.Store().config()['enabled']);self.assertTrue(tg.Store().config()['enabled'])
        self.assertTrue(self.get('analysis-multi').json()['channels']['telegram']['enabled'])

    def test_one_episode_is_shared_by_both_analysis_modes_not_by_channels(self):
        for channel in ('vk','telegram'):
            self.save('analysis-single',channel);self.save('analysis-multi',channel)
        for cls in (tg.Store,vk.Store):
            self.assertEqual(self.observe(cls()),'ok')
            self.assertEqual(self.observe(cls()),'suppressed')
            self.save('analysis-single','telegram' if cls is tg.Store else 'vk',False)
            self.assertEqual(self.observe(cls(),now=NOW+10),'suppressed')
        self.assertEqual(self.vk.call_count,1);self.assertEqual(self.tg.call_count,1)

    def test_every_selected_share_gets_its_own_analysis_episode(self):
        self.save('analysis-multi','vk',tickers=['SBER','GAZP'])
        self.assertEqual(self.observe(vk.Store(),'SBER'),'ok');self.assertEqual(self.observe(vk.Store(),'GAZP'),'ok')
        self.assertEqual(self.observe(vk.Store(),'LKOH'),'disabled')
        self.assertEqual(self.vk.call_count,2)
        self.assertIn('Сбербанк (SBER)',self.vk.call_args_list[0].args[0])
        self.assertIn('GAZP',self.vk.call_args_list[1].args[0]);self.tg.assert_not_called()

    def test_failed_or_stale_or_demo_analysis_never_sends(self):
        self.save('analysis-single','vk')
        values=[{**payload(),'mode':'demo'},payload(NOW-60),payload(passed=5),payload(data_quality={}),
                payload(decision='WAIT'),{**payload(),'refreshing':True}]
        for value in values:
            self.assertEqual(vk.Store().observe('SBER',value,now=NOW,sender=self.vk),'blocked')
        self.vk.assert_not_called()

    def test_clear_then_reappear_requires_cooldown_and_unknown_does_not_retry(self):
        self.save('analysis-single','vk');store=vk.Store()
        self.vk.return_value={'status':'unknown','message':'Не подтверждено.'}
        self.assertEqual(self.observe(store),'unknown')
        self.assertEqual(self.observe(vk.Store(),now=NOW+4000),'suppressed')
        value=payload(NOW+4010,decision='WAIT',passed=5);value['analysis']['rules'][0]['pass']=False
        self.assertEqual(store.observe('SBER',value,now=NOW+4010,sender=self.vk),'waiting')
        self.assertEqual(self.observe(store,now=NOW+4020),'unknown');self.assertEqual(self.vk.call_count,2)

    def test_formatter_uses_only_available_data_and_escapes_company(self):
        a=payload()['analysis'];a['price']=100.25
        text=tg.message('SBER',a,{'name':'Сбер <script>'})
        self.assertIn('Анализ акции',text);self.assertIn('&lt;script&gt;',text)
        self.assertIn('100.25 ₽',text);self.assertIn('МСК',text);self.assertIn('6 из 6',text)
        self.assertIn('Стоп сценария: 99',text);self.assertNotIn('Радар',text)
        a.pop('price');self.assertNotIn('Цена по данным анализа:',tg.message('SBER',a))

    def test_selection_removal_cancels_queued_old_ticker_and_empty_list_is_idle(self):
        self.save('analysis-multi','telegram',tickers=['SBER'])
        self.save('analysis-multi','telegram',tickers=['GAZP'])
        self.assertEqual(self.observe(tg.Store(),'SBER'),'disabled')
        self.save('analysis-multi','telegram',tickers=[])
        self.assertEqual(tg.Store().config()['tickers'],[])
        self.assertEqual(self.observe(tg.Store(),'GAZP'),'disabled');self.tg.assert_not_called()

    def test_legacy_enabled_list_migrates_once_without_resetting_off_choice(self):
        path=Path(self.temp.name)/'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE config(id INTEGER PRIMARY KEY,enabled INTEGER,tickers TEXT,revision INTEGER)')
            db.execute('INSERT INTO config VALUES(1,1,?,4)',(json.dumps(['GAZP']),))
        store=tg.Store(path)
        self.assertTrue(store.mode_config('multi')['enabled']);self.assertEqual(store.config()['tickers'],['GAZP'])
        store.save_mode('multi',False,['GAZP'])
        self.assertFalse(tg.Store(path).config()['enabled'])

    def test_local_csrf_and_fixed_receiver_cannot_be_bypassed(self):
        url='/api/notifications/?mode=analysis-single'
        self.assertEqual(self.client.get(url,REMOTE_ADDR='8.8.8.8',HTTP_X_FORWARDED_FOR='127.0.0.1').status_code,403)
        self.assertEqual(self.client.post(url,data='{}',content_type='application/json').status_code,403)
        self.assertEqual(self.post('analysis-single',{'channel':'vk','enabled':True,'tickers':['SBER'],'peer_id':999}).status_code,400)
        self.assertEqual(self.post('analysis-single',{'channel':'vk','enabled':True,'tickers':['SBER','GAZP']}).status_code,400)
        self.assertEqual(self.post('radar-single',{'channel':'other','enabled':True}).status_code,400)
        self.vk.assert_not_called();self.tg.assert_not_called()

    def test_manual_test_is_rate_limited_independent_of_enabled_and_get(self):
        for channel,sender in (('vk',self.vk),('telegram',self.tg)):
            response=self.post('analysis-single',{'channel':channel,'action':'test'})
            self.assertEqual(response.json()['test']['status'],'ok')
            self.assertFalse(response.json()['channels'][channel]['enabled'])
            self.assertEqual(self.post('radar-multi',{'channel':channel,'action':'test'}).status_code,400)
            self.assertEqual(sender.call_count,1)

    def test_error_and_missing_configuration_do_not_claim_delivery(self):
        self.vk.return_value={'status':'rejected','message':'ВК (901): Нет разрешения.'}
        result=self.post('radar-multi',{'channel':'vk','enabled':True}).json()['channels']['vk']
        self.assertTrue(result['enabled']);self.assertEqual(result['result'],'rejected');self.assertIn('901',result['message'])
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':''}):
            result=self.post('analysis-single',{'channel':'telegram','enabled':True,'tickers':['SBER']})
            self.assertEqual(result.status_code,400)
            self.assertFalse(tg.Store().mode_config('single')['enabled'])

    def test_radar_settings_channels_modes_and_analysis_stay_separate(self):
        self.save('radar-single','telegram');self.save('radar-multi','vk')
        self.assertIn('Telegram',self.tg.call_args.args[0]);self.assertIn('Мульти-радар',self.vk.call_args.args[0])
        self.assertFalse(radar.Store().status('single')['enabled'])
        self.assertFalse(radar.TelegramStore().status('multi')['enabled'])
        self.assertFalse(tg.Store().config()['enabled']);self.assertFalse(vk.Store().config()['enabled'])
        self.save('radar-single','telegram',False)
        self.assertFalse(radar.TelegramStore().status('single')['enabled']);self.assertTrue(radar.Store().status('multi')['enabled'])

    def test_shared_radar_queue_routes_to_both_transports_and_isolates_vk_failure(self):
        radar.Store().set_enabled('single',True);radar.TelegramStore().set_enabled('single',True)
        self.vk.reset_mock();self.tg.reset_mock()
        pending=Mock();pending.get.side_effect=[('single','FIGI','event',NOW),StopIteration]
        with patch.object(radar,'PENDING',pending),patch.object(radar.time,'time',return_value=NOW),patch.object(radar,'Store',side_effect=OSError('storage')):
            with self.assertRaises(StopIteration):radar._worker()
        self.vk.assert_not_called();self.tg.assert_called_once();pending.task_done.assert_called_once()

    def test_union_collector_reads_both_channels_and_both_analysis_modes(self):
        self.save('analysis-single','telegram',tickers=['SBER']);self.save('analysis-multi','vk',tickers=['GAZP','LKOH'])
        with patch('dashboard.management.commands.watch_vk.supervise',return_value=(Path('synthetic'),'deadline')) as run:
            call_command('watch_notifications',seconds=10,max_mb=1)
        self.assertCountEqual(run.call_args.args[2],['SBER','GAZP','LKOH'])
        self.assertEqual(run.call_args.kwargs['max_bytes'],1_000_000)

    def test_legacy_analysis_endpoint_cannot_leave_hidden_subscription_enabled(self):
        store=tg.Store()
        store.save(True,['SBER'])
        self.assertTrue(store.mode_config('multi')['enabled'])
        store.save_mode('multi',False,['SBER'])
        self.assertFalse(store.config()['enabled'])
        self.assertEqual(self.observe(store),'disabled')
        self.tg.assert_not_called()

    def test_legacy_radar_poll_flags_do_not_send_or_enable_channels(self):
        from . import views
        with patch.multiple(views,INVEST_TOKEN='synthetic',ACTIVE_FIGI='FIGI',
                            TICKER_CACHE={'FIGI':'SBER'},GLOBAL_DATA={'FIGI':{'status':'ok'}},
                            MULTI_GLOBAL_DATA={},MULTI_STREAM_THREAD=Mock()), \
             patch.object(views,'send_telegram_message') as legacy,patch.object(radar,'submit') as submit:
            for flag in ('true','false'):
                self.assertEqual(self.client.get('/api/real-data/',{'figi':'FIGI','bot':flag}).status_code,200)
                self.assertEqual(self.client.get('/api/radar-data/',{'bot':flag}).status_code,200)
            legacy.assert_not_called();submit.assert_not_called()
        self.tg.assert_not_called();self.vk.assert_not_called()
        for cls in (radar.Store,radar.TelegramStore):
            self.assertFalse(cls().status('single')['enabled'])
            self.assertFalse(cls().status('multi')['enabled'])

    def test_watchlist_panel_is_next_to_results_and_has_only_one_pair_of_switches(self):
        html=self.client.get('/watchlist/').content.decode()
        self.assertEqual(html.count('id="notification-panel"'),1)
        self.assertEqual(html.count('id="notify-vk"'),1)
        self.assertEqual(html.count('id="notify-telegram"'),1)
        self.assertLess(html.index('id="focus-now"'),html.index('id="notification-panel"'))
        self.assertLess(html.index('id="notification-panel"'),html.index('id="watch-grid"'))
        self.assertIn('data-notification-mode="analysis-multi"',html)
        self.assertIn('СПИСОК НАБЛЮДЕНИЯ',html)

    def test_watchlist_worker_consumes_trusted_ready_results_without_extra_broker_calls(self):
        from . import signal_service
        tickers=['SBER','GAZP','VTBR','NVTK','RTKM']
        for channel in ('vk','telegram'):self.save('analysis-multi',channel,tickers=tickers)
        good=payload(NOW);good['instrument']={'ticker':'SBER','name':'Сбербанк'}
        blocked=payload(NOW);blocked['instrument']={'ticker':'GAZP'};blocked['analysis']['rules'][0]['pass']=False
        stale=payload(NOW-100);stale['instrument']={'ticker':'VTBR'}
        outside=payload(NOW);outside['instrument']={'ticker':'LKOH'}
        last=payload(NOW);last['instrument']={'ticker':'RTKM','name':'Ростелеком'}
        entries={('live',ticker):{'response':value,'pending':False,'finished':100}
                 for ticker,value in [('SBER',good),('GAZP',blocked),('VTBR',stale),('NVTK',{'status':'error'}),('RTKM',last),('LKOH',outside)]}
        with patch.dict(signal_service.ENTRIES,entries,clear=True), \
             patch.object(tg.time,'time',return_value=NOW),patch.object(tg.time,'monotonic',return_value=100), \
             patch.object(tg,'collector_status',return_value={'active_tickers':[]}), \
             patch.object(signal_service,'get_signal') as fetch:
            for cls in (tg.Store,vk.Store):
                for _ in range(2):
                    stop=Mock();stop.is_set.side_effect=[False,*([False]*5),True]
                    tg.worker_loop(stop,cls())
            fetch.assert_not_called()
        # Ready results are not held up by a pending batch: the fifth share also sends.
        for sender in (self.tg,self.vk):
            self.assertEqual(sender.call_count,2)
            self.assertIn('Сбербанк (SBER)',sender.call_args_list[0].args[0])
            self.assertIn('Ростелеком (RTKM)',sender.call_args_list[1].args[0])
            self.assertNotIn('LKOH',' '.join(call.args[0] for call in sender.call_args_list))

    def test_cached_page_result_is_blocked_during_refresh_and_after_cache_expiry(self):
        from . import signal_service
        self.save('analysis-multi','vk')
        for pending,age in ((True,0),(False,26)):
            with self.subTest(pending=pending,age=age), \
                 patch.dict(signal_service.ENTRIES,{('live','SBER'):{'response':payload(NOW),'pending':pending,'finished':100-age}},clear=True), \
                 patch.object(signal_service.time,'monotonic',return_value=100):
                value=signal_service.cached_signals(['SBER'])['SBER']
                self.assertTrue(value['refreshing'])
                self.assertEqual(vk.Store().observe('SBER',value,now=NOW),'blocked')
        self.vk.assert_not_called()
