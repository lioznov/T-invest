import hashlib
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase,override_settings,RequestFactory
from .test_range_study import candles
from .trader_tactics import VERSION,registry
from .trader_tactics_research import load_trader_research
from .range_study import TICKERS,PERIODS
from .management.commands.study_trader_tactics import freeze,hashes
from .management.commands.study_ranges import START,END,VERSION as INPUT_VERSION
from .signal_views import pattern_research


class TraderDeliveryTests(SimpleTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)/'data'
        ctx = override_settings(BASE_DIR=Path(temp.name))
        ctx.enable()
        self.addCleanup(ctx.disable)

    def save(self):
        protocol = freeze(self.directory)
        results = []
        for ticker in TICKERS:
            bars = candles(260)
            source = {'version':INPUT_VERSION,'instrument':{'ticker':ticker,'lot':1},
                      'request':{'from':START.isoformat(),'to_exclusive':END.isoformat(),'source':'CANDLE_SOURCE_EXCHANGE','interval':'DAY'},
                      'bars':[{'time':(c.time-timedelta(days=365)).isoformat(),'open':c.open,'high':c.high,'low':c.low,'close':c.close,
                               'volume':c.volume,'is_complete':True,'source':'CANDLE_SOURCE_EXCHANGE'} for c in bars]}
            name = f'range-input-{ticker}-20261003.json'
            path = self.directory/name
            path.write_text(json.dumps(source),encoding='utf-8')
            model = {'status':'ok','trades':0,'return_pct':0,'stress_return_pct':0,'max_drawdown_pct':0,
                     'unverified_trades':0,'trade_log':[],'daily_curve':[]}
            results.append({'ticker':ticker,'quality':True,'source':{'file':name,'bars':260,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},
                            'variants':{k:{p:model.copy() for p in PERIODS} for k in registry()}})
        self.report = {'version':VERSION,'entry_allowed':False,'at':'2026-10-06T16:30:00+00:00','results':results,
                       'source_sha256':hashes(),'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest()}
        self.path = self.directory/'trader-tactics-study-test.json'
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.report),encoding='utf-8')

    def test_no_report_does_not_mean_zero_profit(self):
        self.assertEqual(load_trader_research('SBER')['status'],'insufficient')

    def test_zero_trades_are_null_never_approval(self):
        self.save()
        r = load_trader_research('SBER')
        self.assertEqual(r['status'],'ok')
        self.assertFalse(r['entry_allowed'])
        self.assertIsNone(r['variants'][0]['periods']['recent']['return_pct'])

    def test_positive_report_stays_exploratory(self):
        self.save()
        m = self.report['results'][0]['variants']['rsi_open']['recent']
        m.update(trades=1,return_pct=1,stress_return_pct=.1,trade_log=[{'return_pct':1}],daily_curve=[{'day':'2026-09-01','equity':1.01}])
        self.write()
        r = load_trader_research('SBER')
        self.assertEqual(r['status'],'ok')
        self.assertFalse(r['entry_allowed'])

    def test_bad_metric_fails_closed(self):
        self.save()
        self.report['results'][0]['variants']['rsi_open']['recent']['trades'] = True
        self.write()
        self.assertEqual(load_trader_research('SBER')['status'],'insufficient')

    def test_changed_input_and_protocol_fail_closed(self):
        self.save()
        self.report['protocol_sha256'] = 'wrong'
        self.write()
        self.assertEqual(load_trader_research('SBER')['status'],'insufficient')

    def test_changed_input_fails_closed(self):
        self.save()
        p = self.directory/'range-input-SBER-20261003.json'
        p.write_text(p.read_text(encoding='utf-8')+' ',encoding='utf-8')
        self.assertEqual(load_trader_research('SBER')['status'],'insufficient')

    def test_unknown_stock_no_fabricated_zeros(self):
        self.save()
        self.assertEqual(load_trader_research('TATN')['variants'],[])

    def test_api_live_adds_table_demo_does_not(self):
        factory = RequestFactory()
        live = json.loads(pattern_research(factory.get('/api/pattern-research/',{'mode':'live','ticker':'SBER'})).content)
        demo = json.loads(pattern_research(factory.get('/api/pattern-research/',{'mode':'demo','ticker':'SBER'})).content)
        self.assertIn('trader_tactics',live)
        self.assertNotIn('trader_tactics',demo)
