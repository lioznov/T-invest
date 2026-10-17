import copy
import json
from datetime import datetime,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase,Client,override_settings
from . import market_ai as ai
from .ai_service import analyze_snapshot,expire_payload,load_model
from .management.commands.train_market_ai import digest
from .signals import MSK
from .test_market_ai import candles

NOW=datetime(2026,10,13,10,30,10,tzinfo=MSK)


def fixture():
    return {'version':ai.VERSION,'features':list(ai.FEATURES),'source_sha256':{'fixture':'yes'},'rules':json.loads(json.dumps(ai.RULES)),
            'entry_allowed':False,'trained_at':(NOW-timedelta(days=1)).isoformat(),'tickers':['SBER'],'paper_ready':False,
            'artifact_sha256':'a'*64,'model':{'classifier':{'center':[0]*12,'scale':[1]*12,'weights':[1.5]+[0]*12},
                                            'calibrator':{'center':[0],'scale':[1],'weights':[0,1]}}}


def snapshot():
    return {'instrument':{'ticker':'SBER'},'candles':candles()[:101],'trading':True,'provider_version':'stream_exchange_v1',
            'data_quality':{'action_blocked':False},'books':[{'time':NOW-timedelta(seconds=1),'is_consistent':True,
                                                           'bids':[[100,100]],'asks':[[100.01,100]]}]}


class AIServiceTests(SimpleTestCase):
    def test_high_score_is_watch_only_until_independent_proof(self):
        with patch('dashboard.ai_service.load_model',return_value=fixture()):
            result=analyze_snapshot(snapshot(),now=NOW)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['decision'],'WATCH')
        self.assertGreater(result['score'],60)
        self.assertFalse(result['entry_allowed'])
        self.assertLess(result['plan']['stop'],result['plan']['entry'])

    def test_demo_never_uses_real_trained_model(self):
        with patch('dashboard.ai_service.load_model',side_effect=AssertionError('not called')):
            self.assertIsNone(analyze_snapshot(snapshot(),mode='demo',now=NOW)['score'])

    def test_stale_books_sources_and_unknown_symbols_block(self):
        with patch('dashboard.ai_service.load_model',return_value=fixture()):
            for change in ({'provider_version':'legacy'}, {'trading':False}, {'data_quality':{'action_blocked':True}},
                           {'instrument':{'ticker':'TATN'}}):
                result=analyze_snapshot({**snapshot(),**change},now=NOW)
                self.assertEqual(result['decision'],'WAIT')
                self.assertIsNone(result['score'])
            changed=snapshot()
            changed['books'][0]['time']=NOW-timedelta(seconds=6)
            self.assertIsNone(analyze_snapshot(changed,now=NOW)['score'])

    def test_old_model_and_old_closed_candle_block(self):
        model=fixture()
        model['trained_at']=(NOW-timedelta(days=31)).isoformat()
        with patch('dashboard.ai_service.load_model',return_value=model):
            self.assertIsNone(analyze_snapshot(snapshot(),now=NOW)['score'])
        with patch('dashboard.ai_service.load_model',return_value=fixture()):
            self.assertIsNone(analyze_snapshot(snapshot(),now=NOW+timedelta(minutes=2))['score'])

    def test_expiry_does_not_mutate_cached_payload(self):
        original={'analysis':{'ai':{'status':'ok','score':80,'decision':'WATCH','valid_until':(NOW-timedelta(seconds=1)).isoformat(),'plan':{'entry':100}}}}
        result=expire_payload(original,NOW)
        self.assertIsNone(result['analysis']['ai']['score'])
        self.assertEqual(original['analysis']['ai']['score'],80)

    def test_json_model_tamper_and_changed_training_source_are_rejected(self):
        with TemporaryDirectory() as d,override_settings(BASE_DIR=Path(d)),patch('dashboard.ai_service.source_hashes',return_value={'fixture':'yes'}):
            directory=Path(d)/'dashboard'/'ai_models'
            directory.mkdir(parents=True)
            model=fixture()
            model['artifact_sha256']=digest({k:v for k,v in model.items() if k!='artifact_sha256'})
            path=directory/'market-ai-v1-fixture.json'
            path.write_text(json.dumps(model),encoding='utf-8')
            self.assertIsNotNone(load_model())
            with patch('dashboard.ai_service.source_hashes',return_value={'fixture':'changed'}):
                self.assertIsNone(load_model())
            model['model']['classifier']['weights'][0]=100
            path.write_text(json.dumps(model),encoding='utf-8')
            self.assertIsNone(load_model())

    def test_api_returns_status_without_broker_or_training_calls(self):
        with patch('dashboard.ai_service.model_status',return_value={'status':'ok','entry_allowed':False}),patch('dashboard.signal_views.get_signal',side_effect=AssertionError('no broker request')):
            result=Client().get('/api/ai-model/')
        self.assertEqual(result.status_code,200)
        self.assertFalse(result.json()['entry_allowed'])
