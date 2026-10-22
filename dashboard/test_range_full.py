import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase
from .range_study import features, simulate, RangeRule
from .test_range_study import candles
from .management.commands.study_ranges import START, END, VERSION as INPUT_VERSION
from .management.commands.study_ranges_full import load_input, freeze


class FullDailyPathTests(SimpleTestCase):
    def test_held_stop_checked_on_saturday(self):
        bars = candles(114)
        at = bars[108].time+timedelta(days=1)
        while at.weekday() != 3:
            at += timedelta(days=1)
        for i in range(109,114):
            bars[i] = replace(bars[i], time=at+timedelta(days=i-109))
        bars[109] = replace(bars[109], high=116, close=115)
        bars[110] = replace(bars[110], open=115, high=116, low=114, close=115)
        bars[111] = replace(bars[111], open=90, high=100, low=89, close=95)
        result = simulate(bars, features(bars), RangeRule('breakout_trailing',20),'2026-01-01','2027-01-01')
        t = result['trade_log'][0]
        self.assertEqual(bars[111].time.weekday(),5)
        self.assertEqual(t['exit_bar'],bars[111].time.isoformat())
        self.assertEqual(t['exit_quote'],90)

    def test_loader_keeps_weekend_bar_and_original_provenance(self):
        bars = candles()
        extra = replace(bars[-1],time=bars[-1].time+timedelta(days=1))
        while extra.time.weekday() != 5:
            extra = replace(extra,time=extra.time+timedelta(days=1))
        bars.append(extra)
        saved = {'version':INPUT_VERSION,'instrument':{'ticker':'SBER','lot':1},
                 'request':{'from':START.isoformat(),'to_exclusive':END.isoformat(),'source':'CANDLE_SOURCE_EXCHANGE','interval':'DAY'},
                 'bars':[{'time':c.time.isoformat(),'open':c.open,'high':c.high,'low':c.low,'close':c.close,'volume':c.volume,'is_complete':True,'source':'CANDLE_SOURCE_EXCHANGE'} for c in bars]}
        with TemporaryDirectory() as d:
            path = Path(d)/'input.json'
            path.write_text(json.dumps(saved),encoding='utf-8')
            result = load_input(path,'SBER')
        self.assertEqual(len(result['candles']),131)
        self.assertEqual(result['source']['weekend_bars'],1)

    def test_correction_protocol_not_old_version_and_locked(self):
        with TemporaryDirectory() as d, patch('dashboard.management.commands.study_ranges_full.hashes',return_value={'code':'a'}):
            p = freeze(Path(d))
            self.assertEqual(p.name,'range-swing-protocol-v2.json')
            with patch('dashboard.management.commands.study_ranges_full.hashes',return_value={'code':'b'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d))
