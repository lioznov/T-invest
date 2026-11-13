"""Synthetic VK delivery only. Never sends a real message or broker order."""
import json
import os
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs
from django.test import Client, override_settings
from . import vk_alerts as vk
from . import vk_transport as transport
from .test_telegram_alerts import payload, NOW

ENV={'VK_COMMUNITY_TOKEN':'synthetic-vk-key','VK_USER_ID':'123456','INVEST_TOKEN':'synthetic-invest'}


class VKTests(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,ENV);self.env.start();self.addCleanup(self.env.stop)
        self.store=vk.Store(Path(self.temp.name)/'vk.sqlite3')
        self.sender=Mock(return_value={'status':'ok','message':'accepted'})

    def test_default_off_and_channel_config_independent(self):
        self.assertFalse(self.store.config()['enabled'])
        with override_settings(BASE_DIR=self.temp.name):
            from .telegram_alerts import Store
            vk.Store().save(True,['SBER'])
            self.assertFalse(Store().config()['enabled'])
        status=json.dumps(self.store.status(NOW))
        self.assertNotIn(ENV['VK_COMMUNITY_TOKEN'],status);self.assertNotIn(ENV['VK_USER_ID'],status)

    def test_vk_validation_does_not_require_telegram_credentials(self):
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'','TELEGRAM_CHAT_ID':''}):
            self.store.save(True,['SBER'])
        with patch.dict(os.environ,{'VK_USER_ID':'2000000001'}):
            with self.assertRaises(ValueError):self.store.save(True,['SBER'])

    def test_live_episode_persisted_and_unknown_delivery_not_retried(self):
        self.store.save(True,['SBER']);self.sender.return_value={'status':'unknown','message':'not confirmed'}
        self.assertEqual(self.store.observe('SBER',payload(),now=NOW,sender=self.sender),'unknown')
        reopened=vk.Store(self.store.path)
        self.assertEqual(reopened.observe('SBER',payload(),now=NOW,sender=self.sender),'suppressed')
        self.assertEqual(self.sender.call_count,1)

    def test_demo_stale_partial_and_no_book_cannot_send(self):
        self.store.save(True,['SBER'])
        for value in ({**payload(),'mode':'demo'},payload(now=NOW-60),payload(passed=5),payload(data_quality={})):
            self.assertEqual(self.store.observe('SBER',value,now=NOW,sender=self.sender),'blocked')
        self.sender.assert_not_called()

    def test_valid_candidate_then_reset_cooldown(self):
        self.store.save(True,['SBER'])
        self.assertEqual(self.store.observe('SBER',payload(),now=NOW,sender=self.sender),'ok')
        value=payload(NOW+60,decision='WAIT',passed=5)
        value['analysis']['rules'][0]['pass']=False
        self.assertEqual(self.store.observe('SBER',value,now=NOW+60,sender=self.sender),'waiting')
        self.assertEqual(self.store.observe('SBER',payload(NOW+90),now=NOW+90,sender=self.sender),'suppressed')
        self.assertEqual(self.store.observe('SBER',payload(NOW+1801),now=NOW+1801,sender=self.sender),'ok')

    def test_manual_test_is_rate_limited_and_does_not_enable_signals(self):
        with patch.object(transport,'send',return_value={'status':'unknown'}) as send:
            self.store.test_message()
            with self.assertRaises(ValueError):vk.Store(self.store.path).test_message()
            self.assertEqual(send.call_count,1)
        self.assertFalse(self.store.config()['enabled'])

    def test_real_sender_path_rechecks_expiry_without_network(self):
        self.store.save(True,['SBER'])
        with patch.object(transport,'send') as send, patch('dashboard.telegram_alerts.time.time',return_value=NOW+50):
            self.assertEqual(self.store.observe('SBER',payload(),now=NOW),'rejected')
            send.assert_not_called()

    def test_endpoint_local_only_and_csrf_for_mutations(self):
        with override_settings(BASE_DIR=self.temp.name,ALLOWED_HOSTS=['testserver']):
            with patch('dashboard.vk_views.ensure_worker'):
                self.assertEqual(Client().get('/api/vk-alerts/',REMOTE_ADDR='8.8.8.8').status_code,403)
                response=Client().get('/api/vk-alerts/')
                self.assertFalse(response.json()['enabled']);self.assertEqual(response['Cache-Control'],'no-store')
                self.assertNotIn('synthetic-vk-key',response.content.decode())
                client=Client(enforce_csrf_checks=True)
                self.assertEqual(client.post('/api/vk-alerts/',data='{}',content_type='application/json').status_code,403)

    def test_endpoint_test_is_explicit_post_and_never_happens_on_get(self):
        with override_settings(BASE_DIR=self.temp.name,ALLOWED_HOSTS=['testserver']),patch.object(transport,'send',return_value={'status':'ok','message':'accepted'}) as send:
            client=Client();client.get('/api/vk-alerts/');send.assert_not_called()
            result=client.post('/api/vk-alerts/',data=json.dumps({'action':'test'}),content_type='application/json')
            self.assertEqual(result.json()['test']['status'],'ok');self.assertEqual(send.call_count,1)
            self.assertFalse(vk.Store().config()['enabled'])

    def test_endpoint_cannot_change_owner_or_add_subscribers(self):
        with override_settings(BASE_DIR=self.temp.name,ALLOWED_HOSTS=['testserver']),patch.object(transport,'send') as send:
            client=Client()
            for field in ('user_id','user_ids','peer_id','peer_ids','chat_id','recipient','access_token'):
                result=client.post('/api/vk-alerts/',data=json.dumps({'action':'test',field:999}),content_type='application/json')
                self.assertEqual(result.status_code,400)
            result=client.post('/api/vk-alerts/',data=json.dumps({'enabled':True,'tickers':['SBER'],'user_id':999}),content_type='application/json')
            self.assertEqual(result.status_code,400)
            self.assertFalse(vk.Store().config()['enabled'])
            send.assert_not_called()

    def test_collector_reads_vk_watchlist(self):
        from django.core.management import call_command
        with override_settings(BASE_DIR=self.temp.name):
            vk.Store().save(False,['GAZP'])
            with patch('dashboard.management.commands.watch_vk.supervise',return_value=(Path('mock-session'),'deadline')) as run:
                call_command('watch_vk',seconds=10,max_mb=1)
                self.assertEqual(run.call_args.args[2],['GAZP'])


class VKTransportTests(TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,ENV);self.env.start();self.addCleanup(self.env.stop)

    def result(self,value):
        opener=Mock();response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps(value).encode()
        opener.open.return_value=response
        with patch('dashboard.vk_transport.urllib.request.build_opener',return_value=opener):
            outcome=transport.send('<b>VORBY &amp; SBER</b>')
        return outcome,opener

    def test_post_body_destination_random_id_plain_text_and_timeout(self):
        outcome,opener=self.result({'response':123})
        self.assertEqual(outcome['status'],'ok')
        request=opener.open.call_args.args[0];data=parse_qs(request.data.decode())
        self.assertEqual(request.full_url,'https://api.vk.com/method/messages.send')
        self.assertNotIn('synthetic-vk-key',request.full_url)
        self.assertEqual(data['peer_id'],['123456']);self.assertEqual(data['message'],['VORBY & SBER'])
        self.assertTrue(0<int(data['random_id'][0])<2**31)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],8)

    def test_known_refusals_and_rate_limits(self):
        for code in (5,6,7,9,14,15,27,28,29,100,113,200,900,901,902):
            value,_=self.result({'error':{'error_code':code,'error_msg':'synthetic-vk-key'}})
            self.assertEqual(value['status'],'rejected');self.assertNotIn('synthetic-vk-key',json.dumps(value))
            self.assertIn(f'({code})',value['message'])

    def test_missing_permission_is_distinguished_from_invalid_token_and_privacy(self):
        for code,reason in ((901,'Нет разрешения'),(902,'приватности'),(27,'авторизацию'),(113,'Получатель не найден')):
            value,_=self.result({'error':{'error_code':code,'error_msg':'synthetic-vk-key','request_params':[{'key':'access_token','value':'synthetic-vk-key'}]}})
            self.assertIn(reason,value['message'])
            self.assertNotIn('synthetic-vk-key',json.dumps(value))
        self.assertIn('Подписки на группу недостаточно',self.result({'error':{'error_code':901}})[0]['message'])

    def test_malformed_and_server_errors_are_ambiguous(self):
        for value in ({'response':False},{'response':0},{'error':{'error_code':10}},{'error':{'error_code':'synthetic-vk-key'}},{'error':{'error_code':[]}},{}):
            self.assertEqual(self.result(value)[0]['status'],'unknown')

    def test_timeout_does_not_leak_diagnostics(self):
        with patch('dashboard.vk_transport.urllib.request.build_opener',side_effect=TimeoutError('synthetic-vk-key')):
            value=transport.send('test')
        self.assertEqual(value['status'],'unknown');self.assertNotIn('synthetic-vk-key',json.dumps(value))

    def test_invalid_recipient_never_calls_network(self):
        for user in ('','-123','community','2000000001','123456,999999','1'*5000):
            with patch.dict(os.environ,{'VK_USER_ID':user}),patch('dashboard.vk_transport.urllib.request.build_opener') as opener:
                self.assertEqual(transport.send('test')['status'],'rejected');opener.assert_not_called()
