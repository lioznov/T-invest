import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, override_settings

from .range_levels import range_levels
from .range_research import load_range_research
from .range_study import registry, TICKERS, PERIODS
from .management.commands.study_ranges import START, END, VERSION as INPUT_VERSION
from .management.commands.study_ranges_full import freeze, hashes, VERSION
from .test_range_study import candles


class RangeLevelsTests(SimpleTestCase):
    def test_current_wick_not_part_of_previous_levels(self):
        bars = candles(60)
        bars[-1] = replace(bars[-1], high=999, close=200)
        result = range_levels({'candles': bars}, bars[-1].time+timedelta(minutes=16))
        self.assertEqual([r['bars'] for r in result['levels']], [20, 55])
        self.assertLess(result['levels'][0]['resistance'], 999)
        self.assertTrue(result['levels'][0]['above'])
        self.assertFalse(result['entry_allowed'])

    def test_unfinished_future_bar_excluded(self):
        bars = candles(60)
        now = bars[-2].time+timedelta(minutes=16)
        self.assertEqual(range_levels({'candles':bars}, now), range_levels({'candles':bars[:-1]}, now))

    def test_stale_invalid_empty_and_zero_volume_fail_closed(self):
        bars = candles(60)
        at = bars[-1].time+timedelta(minutes=16)
        self.assertEqual(range_levels({'candles':[]},at)['status'], 'insufficient')
        self.assertEqual(range_levels({'candles':bars},at+timedelta(hours=2))['status'], 'stale')
        self.assertEqual(range_levels({'candles':list(reversed(bars))},at)['status'], 'invalid')
        bars[-1] = replace(bars[-1], volume=0)
        self.assertEqual(range_levels({'candles':bars},at)['levels'], [])


class RangeResearchTests(SimpleTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        ctx = override_settings(BASE_DIR=self.base)
        ctx.enable()
        self.addCleanup(ctx.disable)
        self.directory = self.base/'data'

    def save(self):
        protocol = freeze(self.directory)
        results = []
        for ticker in TICKERS:
            saved = {'version':INPUT_VERSION, 'instrument':{'ticker':ticker, 'lot':1},
                     'request':{'from':START.isoformat(), 'to_exclusive':END.isoformat(), 'source':'CANDLE_SOURCE_EXCHANGE', 'interval':'DAY'},
                     'bars':[{'time':c.time.isoformat(), 'open':c.open, 'high':c.high, 'low':c.low, 'close':c.close,
                              'volume':c.volume, 'is_complete':True, 'source':'CANDLE_SOURCE_EXCHANGE'} for c in candles()]}
            name = f'range-input-{ticker}-20261003.json'
            path = self.directory/name
            path.write_text(json.dumps(saved), encoding='utf-8')
            model = {'status':'ok','trades':0,'return_pct':0,'stress_return_pct':0,'unverified_trades':0,
                     'max_drawdown_pct':0,'trade_log':[],'daily_curve':[]}
            results.append({'ticker':ticker,'quality':True, 'source':{'file':name,'bars':130,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},
                            'variants':{key:{p:model.copy() for p in PERIODS} for key in registry()}})
        self.report = {'version':VERSION, 'at':'2026-10-06T15:10:00+00:00','entry_allowed':False, 'source_sha256':hashes(),
                       'protocol_sha256':hashlib.sha256(protocol.read_bytes()).hexdigest(), 'results':results}
        self.path = self.directory/'range-swing-v2-study-test.json'
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.report), encoding='utf-8')

    def test_missing_result_not_zero_profit(self):
        self.assertEqual(load_range_research('SBER')['status'], 'insufficient')

    def test_no_trades_are_null_and_never_approval(self):
        self.save()
        result = load_range_research('SBER')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(result['variants']), 4)
        self.assertIsNone(result['variants'][0]['periods']['recent']['return_pct'])
        self.assertFalse(result['entry_allowed'])

    def test_unknown_stock_not_zero_profit(self):
        self.save()
        self.assertEqual(load_range_research('TATN')['variants'], [])

    def test_tampered_protocol_fails_closed(self):
        self.save()
        self.report['protocol_sha256'] = 'wrong'
        self.write()
        self.assertEqual(load_range_research('SBER')['status'], 'insufficient')

    def test_input_tamper_fails_closed(self):
        self.save()
        path = self.directory/'range-input-SBER-20261003.json'
        path.write_text(path.read_text(encoding='utf-8')+' ', encoding='utf-8')
        self.assertEqual(load_range_research('SBER')['status'], 'insufficient')

    def test_boolean_metric_rejected(self):
        self.save()
        self.report['results'][0]['variants'][next(iter(registry()))]['recent']['trades'] = True
        self.write()
        self.assertEqual(load_range_research('SBER')['status'], 'insufficient')

    def test_source_path_escape_rejected(self):
        self.save()
        self.report['results'][0]['source']['file'] = '../input.json'
        self.write()
        self.assertEqual(load_range_research('SBER')['status'], 'insufficient')
