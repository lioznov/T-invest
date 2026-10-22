import copy
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase, Client, override_settings
from django.core.management.base import CommandError
from . import neural_trader as nn, neural_service as service, portfolio_replay
from .ai_service import expire_payload
from .management.commands.train_market_ai import digest
from .management.commands.verify_neural_trader import freeze, START, END, load_extension
from .test_ai_service import NOW, snapshot


def fixture():
    sizes = [20,16,8,2]
    net = {'center':[0.]*20,'scale':[1.]*20,'median':[0.]*20,'return_center':.2,'return_scale':1.,'prior':.3,
           'layers':[{'weights':[[0.]*b for _ in range(a)],'bias':[0.]*b} for a,b in zip(sizes,sizes[1:])]}
    net['layers'][-1]['bias'][0] = 2.
    metric = {'trades':0,'unknown_trades':0,'return_pct':0.,'max_realised_drawdown_pct':0.,'trade_log':[],'daily_pnl':{}}
    e = {'classification':{'samples':200,'auc':.53,'brier':.2,'baseline_brier':.25},'candidates':0,
         'expected_return_mae_pct':.3,'base':metric,'double_cost':metric}
    return {'version':nn.VERSION,'trained_at':(NOW-timedelta(days=1)).isoformat(),'features':list(nn.FEATURES),
            'rules':nn.RULES,'portfolio_rules':portfolio_replay.RULES,'source_sha256':{'fixture':'yes'},
            'entry_allowed':False,'paper_ready':False,'trained_rows':2000,'calibration_rows':500,'tickers':['SBER'],
            'model':{'network':net,'calibrator':{'center':[0.],'scale':[1.],'weights':[0.,1.]},'return_offset':0.},
            'evaluations':{'test':e,'additional':e},'comparison':{p:{name:{'base':metric,'double_cost':metric} for name in ('linear','trees')} for p in ('test','additional')}}


class NeuralDeliveryTests(SimpleTestCase):
    def test_live_neural_uses_same_freshness_guards(self):
        model = fixture()
        model['artifact_sha256'] = 'a'*64
        s = snapshot()
        with patch.object(service,'load_model',return_value=model),patch.object(nn,'vectors',return_value=([[0.]*20]*101,[{'atr':.5}]*101)):
            a = service.analyze_snapshot(s,now=NOW)
            self.assertEqual(a['status'],'ok')
            self.assertEqual(a['decision'],'WATCH')
            self.assertAlmostEqual(a['expected_net_pct'],.2)
            self.assertFalse(a['entry_allowed'])
            self.assertEqual(service.analyze_snapshot(s,'demo',NOW)['status'],'unavailable')
            self.assertEqual(service.analyze_snapshot(s,now=NOW+timedelta(minutes=2))['decision'],'WAIT')
            s['books'][0]['time'] = NOW-timedelta(seconds=6)
            self.assertEqual(service.analyze_snapshot(s,now=NOW)['decision'],'WAIT')

    def test_neural_expiry_clears_expected_returns_without_mutating_cache(self):
        value = {'status':'ok','score':80,'expected_net_pct':1.,'valid_until':(NOW-timedelta(seconds=1)).isoformat(),'plan':{'entry':1}}
        result = expire_payload({'analysis':{'neural':value}},NOW)
        self.assertIsNone(result['analysis']['neural']['expected_net_pct'])
        self.assertEqual(value['expected_net_pct'],1.)

    def test_saved_model_shapes_identity_and_gate_are_validated(self):
        with TemporaryDirectory() as d,override_settings(BASE_DIR=Path(d)),patch.object(service,'source_hashes',return_value={'fixture':'yes'}):
            root = Path(d)/'dashboard'/'ai_models'
            root.mkdir(parents=True)
            path = root/'neural-trader-v1-fixture.json'
            def save(value):
                value['artifact_sha256'] = digest({k:v for k,v in value.items() if k!='artifact_sha256'})
                path.write_text(json.dumps(value),encoding='utf-8')
                service._cache.clear()
            original = fixture()
            save(original)
            self.assertIsNotNone(service.load_model())
            for change in ('shape','nonfinite','approval','gate','source'):
                altered = copy.deepcopy(original)
                if change=='shape': altered['model']['network']['layers'][1]['weights'].pop()
                elif change=='nonfinite': altered['model']['network']['return_scale']=-1.
                elif change=='approval': altered['entry_allowed']=True
                elif change=='gate': altered['paper_ready']=True
                else: altered['source_sha256']={}
                save(altered)
                self.assertIsNone(service.load_model())

    def test_public_api_and_both_pages_expose_results_without_weights(self):
        with patch.object(service,'load_model',return_value={**fixture(),'artifact_sha256':'b'*64}),patch.object(service,'check_status',return_value={'status':'unavailable'}):
            response = Client().get('/api/ai-model/')
            self.assertEqual(response.status_code,200)
            body = response.json()['neural']
            self.assertEqual(body['status'],'ok')
            self.assertFalse(body['entry_allowed'])
            self.assertNotIn('weights',json.dumps(body))
            self.assertNotIn('trade_log',json.dumps(body))
        for url in ('/','/watchlist/'):
            response = Client().get(url)
            self.assertContains(response,'id="neural-report"')
            self.assertContains(response,'neural_trader.js?v=1')

    def test_fresh_date_check_locks_weights_before_new_prices(self):
        with TemporaryDirectory() as d,patch('dashboard.management.commands.verify_neural_trader.source_hashes',return_value={'code':'a'}):
            model = {**fixture(),'artifact_sha256':'a'*64}
            freeze(Path(d),model)
            with self.assertRaises(CommandError):
                freeze(Path(d),{**model,'artifact_sha256':'b'*64})

    def test_missing_new_history_does_not_trigger_download_or_fabricate_outcomes(self):
        with TemporaryDirectory() as d:
            with self.assertRaises(CommandError):
                load_extension(Path(d),'SBER',{'uid':'fixture'},fetch=False)

    def test_new_date_report_must_match_model_and_interval(self):
        with TemporaryDirectory() as d,override_settings(BASE_DIR=Path(d)),patch('dashboard.management.commands.verify_neural_trader.source_hashes',return_value={'check':'a'}):
            root=Path(d)/'dashboard'/'ai_models'
            root.mkdir(parents=True)
            path=root/'neural-check-v1-fixture.json'
            model={**fixture(),'artifact_sha256':'a'*64}
            report={'version':nn.VERSION,'model_id':'a'*64,'source_sha256':{'check':'a'},'entry_allowed':False,
                    'independent_prospective':False,'start':START.isoformat(),'end_exclusive':END.isoformat(),
                    'at':NOW.isoformat(),'inputs':{'SBER':{}},'result':model['evaluations']['test']}
            def save(r):
                r['artifact_sha256']=digest({k:v for k,v in r.items() if k!='artifact_sha256'})
                path.write_text(json.dumps(r),encoding='utf-8')
            save(report)
            self.assertEqual(service.check_status(model)['status'],'ok')
            save({**report,'model_id':'b'*64})
            self.assertEqual(service.check_status(model)['status'],'unavailable')
            save({**report,'end_exclusive':'2026-10-18T00:00:00+00:00'})
            self.assertEqual(service.check_status(model)['status'],'unavailable')
