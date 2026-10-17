import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase,Client,override_settings
from .market_structure import VERSION,RULES,VARIANTS
from .market_ai import PERIODS
from .structure_research import load_structure_research,expire_structure_payload
from .management.commands.train_market_ai import digest
from .test_ai_service import NOW


class StructureDeliveryTests(SimpleTestCase):
    def fixture(self):
        zero={'trades':0,'unknown_trades':0,'return_pct':0.,'accounts':7,'positive_accounts':0}
        return {'version':VERSION,'at':NOW.isoformat(),'rules':json.loads(json.dumps(RULES)),
            'source_sha256':{'fixture':'yes'},'entry_allowed':False,'periods':json.loads(json.dumps(PERIODS)),
            'results':{k:{p:{'base':zero.copy(),'double_cost':zero.copy()} for p in PERIODS} for k in VARIANTS}}

    def test_portable_report_validation_and_no_trade_is_null(self):
        with TemporaryDirectory() as d,override_settings(BASE_DIR=Path(d)),patch('dashboard.structure_research.source_hashes',return_value={'fixture':'yes'}):
            root=Path(d)/'dashboard'/'ai_models'
            root.mkdir(parents=True)
            path=root/'market-structure-study-v1-fixture.json'
            def save(report):
                report['artifact_sha256']=digest({k:v for k,v in report.items() if k!='artifact_sha256'})
                path.write_text(json.dumps(report),encoding='utf-8')
            original=self.fixture()
            save(original)
            report=load_structure_research()
            self.assertEqual(report['status'],'ok')
            self.assertFalse(report['entry_allowed'])
            self.assertIsNone(report['variants'][0]['periods']['test']['base']['return_pct'])
            for change in ('source','nan_metric','trade_count','approval'):
                modified=copy.deepcopy(original)
                if change=='source':
                    modified['source_sha256']={}
                elif change=='nan_metric':
                    modified['results']['pivot_breakout']['test']['base']['return_pct']='not a number'
                elif change=='trade_count':
                    modified['results']['pivot_breakout']['test']['base']['trades']=True
                else:
                    modified['entry_allowed']=True
                save(modified)
                self.assertEqual(load_structure_research()['status'],'unavailable')

    def test_expiry_keeps_historical_drawing_not_current_event(self):
        panel={'status':'ok','entry_allowed':False,'valid_until':'2000-01-01T00:00:00+00:00',
               'latest_event':{'key':'breakout'},'levels':[{'price':100}]}
        payload={'analysis':{'market_structure':panel}}
        changed=expire_structure_payload(payload,NOW)['analysis']['market_structure']
        self.assertEqual(changed['status'],'stale')
        self.assertIsNone(changed['latest_event'])
        self.assertEqual(changed['levels'],panel['levels'])
        self.assertEqual(panel['status'],'ok')
        with patch('dashboard.signal_views.get_signal',return_value=payload):
            api=Client().get('/api/signals/?ticker=SBER').json()
        self.assertIsNone(api['analysis']['market_structure']['latest_event'])

    def test_current_page_has_layers_and_shared_research_on_both_pages(self):
        page=Client().get('/')
        for name in ('levels','trends','pivots','events'):
            self.assertContains(page,f'data-structure-layer="{name}"')
        self.assertContains(page,'market_structure.js?v=1')
        self.assertContains(page,'structure-status')
        self.assertContains(Client().get('/watchlist/'),'id="structure-research"')

    def test_snapshot_pipeline_attaches_structure_without_changing_ai_model(self):
        from . import signal_service
        key=('demo','SBER')
        previous=signal_service.ENTRIES.get(key)
        try:
            signal_service.ENTRIES[key]={}
            signal_service.update(key)
            response=signal_service.ENTRIES[key]['response']
            self.assertEqual(response['status'],'ok')
            self.assertEqual(response['analysis']['market_structure']['version'],VERSION)
            self.assertFalse(response['analysis']['market_structure']['entry_allowed'])
            self.assertEqual(response['analysis']['ai']['status'],'unavailable')
        finally:
            if previous is None:
                signal_service.ENTRIES.pop(key,None)
            else:
                signal_service.ENTRIES[key]=previous
