import copy
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase,override_settings,Client
from . import market_ai_v2 as ai
from . import ai_service as service
from .management.commands.train_market_ai import digest
from .test_market_ai_v2 import history
from .test_ai_service import fixture as original_fixture


def challenger_fixture():
    good={'classification':{'samples':500,'brier':.20,'baseline_brier':.25,'auc':.56},
          'base':{'trades':0,'unknown_trades':0,'return_pct':0},
          'double_cost':{'trades':0,'unknown_trades':0,'return_pct':0}}
    old={**good,'classification':{'samples':500,'brier':.21,'auc':.55}}
    return {'version':ai.VERSION,'features':list(ai.FEATURES),'rules':json.loads(json.dumps(ai.RULES)),
        'source_sha256':{'fixture':'yes'},'trained_at':history()[-1].time.isoformat(),'entry_allowed':False,
        'observation_ready':True,'paper_ready':False,'tickers':['SBER'],'artifact_sha256':'b'*64,
        'evaluations':{'test':good,'additional':good},'comparison_v1':{'test':old,'additional':old},
        'model':{'classifier':{'trees':[{'value':0.} for _ in range(ai.RULES['trees'])],
            'bias':1.5,'learning_rate':ai.RULES['learning_rate'],'median':[0.]*len(ai.FEATURES)},
            'calibrator':{'center':[0.],'scale':[1.],'weights':[0.,1.]}}}


class AIV2DeliveryTests(SimpleTestCase):
    def test_failed_challenger_does_not_replace_old_model(self):
        old=original_fixture()
        candidate={**challenger_fixture(),'observation_ready':False}
        with patch.object(service,'load_model',return_value=old),patch.object(service,'load_challenger',return_value=candidate):
            self.assertIs(service.selected_model(),old)
        with patch.object(service,'load_model',side_effect=AssertionError('not needed')),patch.object(service,'load_challenger',return_value=challenger_fixture()):
            self.assertEqual(service.selected_model()['version'],ai.VERSION)

    def test_live_challenger_still_never_allows_purchase(self):
        bars=history()
        last_day=bars[-1].time.date()
        bars=[b for b in bars if b.time.date()!=last_day or (b.time.hour==10 and b.time.minute<=15)]
        now=bars[-1].time+timedelta(minutes=15,seconds=10)
        model=challenger_fixture()
        model['trained_at']=(now-timedelta(days=1)).isoformat()
        price=bars[-1].close
        snapshot={'instrument':{'ticker':'SBER'},'candles':bars,'trading':True,'provider_version':'stream_exchange_v1',
            'data_quality':{'action_blocked':False},'books':[{'time':now-timedelta(seconds=1),'is_consistent':True,
            'bids':[[price,100]],'asks':[[price+.01,100]]}]}
        with patch.object(service,'selected_model',return_value=model):
            result=service.analyze_snapshot(snapshot,now=now)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['model_version'],ai.VERSION)
        self.assertEqual(result['decision'],'WATCH')
        self.assertFalse(result['entry_allowed'])
        self.assertLess(result['plan']['stop'],result['plan']['entry'])

    def test_artifact_validation_checks_trees_metadata_and_sources(self):
        with TemporaryDirectory() as d,override_settings(BASE_DIR=Path(d)),patch.object(service,'challenger_hashes',return_value={'fixture':'yes'}):
            root=Path(d)/'dashboard'/'ai_models'
            root.mkdir(parents=True)
            path=root/'market-ai-v2-fixture.json'

            def save(model):
                model['artifact_sha256']=digest({k:v for k,v in model.items() if k!='artifact_sha256'})
                path.write_text(json.dumps(model),encoding='utf-8')
                service._challenger_cache.clear()

            model=challenger_fixture()
            save(model)
            self.assertIsNotNone(service.load_challenger())
            with patch.object(service,'challenger_hashes',return_value={'fixture':'changed'}):
                self.assertIsNone(service.load_challenger())
            for update in ('tree','median','gate','entry'):
                changed=copy.deepcopy(model)
                if update=='tree':
                    changed['model']['classifier']['trees'][0]={'feature':100,'threshold':1.,'left':{'value':0},'right':{'value':0}}
                elif update=='median':
                    changed['model']['classifier']['median']=[0.]
                elif update=='gate':
                    changed['observation_ready']=False
                else:
                    changed['entry_allowed']=True
                save(changed)
                self.assertIsNone(service.load_challenger())

    def test_comparison_api_never_leaks_model_weights(self):
        old=original_fixture()
        old.update(trained_rows=100,calibration_rows=50,prospective_start='2026-10-14T07:00:00+00:00',evaluations={})
        candidate=challenger_fixture()
        candidate.update(observation_ready=False,trained_rows=500,calibration_rows=100)
        with patch.object(service,'load_model',return_value=old),patch.object(service,'load_challenger',return_value=candidate):
            response=Client().get('/api/ai-model/')
        self.assertEqual(response.status_code,200)
        body=response.json()
        self.assertEqual(body['version'],old['version'])
        self.assertEqual(body['comparison']['status'],'ok')
        self.assertFalse(body['comparison']['entry_allowed'])
        self.assertNotIn('model',body['comparison'])
        self.assertNotIn('trees',response.content.decode())

    def test_both_pages_include_comparison_and_new_script(self):
        for url in ('/','/watchlist/'):
            response=Client().get(url)
            self.assertContains(response,'id="ai-comparison"')
            self.assertContains(response,'signals.js?v=29')
