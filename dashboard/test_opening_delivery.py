import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase, Client, override_settings
from .opening_activity import VERSION, RULES, VARIANTS, TICKERS, PERIODS, SCENARIOS, aggregate
from .opening_activity_research import load_opening_research


class OpeningDeliveryTests(SimpleTestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.data=self.root/'data'
        self.data.mkdir()
        override=override_settings(BASE_DIR=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.patcher=patch('dashboard.opening_activity_research.hashes',return_value={'source':'test'})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def write(self,name,value):
        raw=json.dumps(value).encode()
        (self.data/name).write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def fixture(self):
        signature=self.write('opening-activity-protocol-v1.json',{'spec':{'source_sha256':{'source':'test'},'rules':RULES,'variants':VARIANTS},'prospective_start':'2026-10-14T10:00:00+03:00'})
        metric={'status':'ok','trades':0,'unverified_trades':0,'return_pct':0.,'max_drawdown_pct':0.,'trade_log':[],'entry_allowed':False}
        periods={p:{s:metric.copy() for s in SCENARIOS} for p in PERIODS}
        results={}
        for ticker in TICKERS:
            name=f'flow-input-{ticker}-20261003.json'
            results[ticker]={'source':{'file':name,'sha256':self.write(name,{})},'additional_source':None,
                             'variants':{k:periods for k in VARIANTS}}
        summaries=[{'key':k,'name':n,'periods':{p:{s:aggregate([metric for _ in TICKERS]) for s in SCENARIOS} for p in PERIODS},'passes_screen':False,'entry_allowed':False} for k,n in VARIANTS.items()]
        report={'version':VERSION,'source_sha256':{'source':'test'},'protocol_sha256':signature,'rules':RULES,'results':results,
                'summaries':summaries,'entry_allowed':False,'at':'2026-10-13T14:00:00+00:00','simulations':252,'sources':{}}
        self.write('opening-activity-study-fixture.json',report)
        return report

    def test_missing_or_unknown_ticker_has_no_returns(self):
        self.assertEqual(load_opening_research('SBER')['status'],'insufficient')
        self.fixture()
        self.assertEqual(load_opening_research('TATN')['variants'],[])

    def test_zero_trades_cannot_approve_buying(self):
        self.fixture()
        r=load_opening_research('SBER')
        self.assertEqual(r['status'],'ok')
        self.assertFalse(r['entry_allowed'])
        self.assertEqual(r['passed'],0)

    def test_input_changes_invalidate_report(self):
        self.fixture()
        self.write('flow-input-SBER-20261003.json',{'changed':True})
        self.assertEqual(load_opening_research('SBER')['status'],'insufficient')

    def test_fabricated_group_profit_is_rejected(self):
        report=self.fixture()
        report['summaries'][0]['periods']['recent']['base']['return_pct']=100
        self.write('opening-activity-study-fixture.json',report)
        self.assertEqual(load_opening_research('SBER')['status'],'insufficient')

    def test_demo_never_presents_real_market_study(self):
        self.assertNotIn('opening_activity',Client().get('/api/pattern-research/?ticker=SBER&mode=demo').json())
