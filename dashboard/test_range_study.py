import json
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from .signals import Candle
from .range_study import features, signal, simulate, RangeRule, passes, TICKERS
from .management.commands.study_ranges import freeze, load_input, START, END, VERSION


def candles(n=130):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    days = [start+timedelta(days=i) for i in range(n*2) if (start+timedelta(days=i)).weekday() < 5][:n]
    return [Candle(t, 100+i*.1, 101+i*.1, 99+i*.1, 100.5+i*.1, 100) for i, t in enumerate(days)]


class RangeStudyTests(SimpleTestCase):
    def test_levels_exclude_current_bar(self):
        bars = candles()
        bars[110] = replace(bars[110], high=999, low=1)
        rows = features(bars)
        self.assertEqual(rows[110]['highs'][20], max(c.high for c in bars[90:110]))
        self.assertEqual(rows[110]['lows'][20], min(c.low for c in bars[90:110]))

    def test_features_do_not_use_future(self):
        bars = candles()
        self.assertEqual(features(bars)[:115], features(bars[:115]))

    def test_breakout_requires_closed_price_not_wick(self):
        bars = candles()
        bars[110] = replace(bars[110], high=200)
        rows = features(bars)
        self.assertFalse(signal(bars, rows, 110, RangeRule('breakout_trailing', 20)))
        bars[110] = replace(bars[110], close=120)
        self.assertTrue(signal(bars, features(bars), 110, RangeRule('breakout_trailing', 20)))

    def test_warmup_and_volume(self):
        bars = candles()
        self.assertFalse(signal(bars, features(bars), 99, RangeRule('breakout_trailing', 20)))
        bars[110] = replace(bars[110], high=200, close=150, volume=0)
        self.assertFalse(signal(bars, features(bars), 110, RangeRule('breakout_trailing', 20)))

    def test_reclaim_needs_separate_confirmation(self):
        bars = candles()
        bars[109] = replace(bars[109], low=90)
        bars[110] = replace(bars[110], high=120, close=119)
        rule = RangeRule('low_reclaim', 20)
        self.assertTrue(signal(bars, features(bars), 110, rule))
        bars[110] = replace(bars[110], low=89)
        self.assertFalse(signal(bars, features(bars), 110, rule))

    def test_stop_not_tightened_with_current_high(self):
        bars = candles(114)
        bars[110] = replace(bars[110], high=116, close=115)
        bars[111] = replace(bars[111], open=115, high=1000, low=114, close=115)
        result = simulate(bars, features(bars), RangeRule('breakout_trailing', 20), '2026-01-01', '2027-01-01')
        self.assertEqual(result['trade_log'][0]['entry_time'], bars[111].time.isoformat())
        self.assertNotEqual(result['trade_log'][0]['exit_bar'], bars[111].time.isoformat())

    def test_gap_stop_fills_at_open_not_requested_stop(self):
        bars = candles(114)
        bars[110] = replace(bars[110], high=116, close=115)
        bars[111] = replace(bars[111], open=115, high=116, low=114, close=115)
        bars[112] = replace(bars[112], open=90, high=100, low=89, close=95)
        result = simulate(bars, features(bars), RangeRule('breakout_trailing', 20), '2026-01-01', '2027-01-01', lot=10)
        trade = result['trade_log'][0]
        self.assertEqual((trade['reason'], trade['exit_quote']), ('gap_stop', 90))
        self.assertEqual(trade['shares'] % 10, 0)
        self.assertLess(result['stress_return_pct'], result['return_pct'])

    def test_unknown_calendar_gap_not_verified(self):
        bars = candles(114)
        bars[110] = replace(bars[110], high=116, close=115)
        bars[111] = replace(bars[111], open=115, high=116, low=114, close=115)
        for i in range(112, len(bars)):
            bars[i] = replace(bars[i], time=bars[i].time+timedelta(days=14))
        result = simulate(bars, features(bars), RangeRule('breakout_trailing', 20), '2026-01-01', '2027-01-01')
        self.assertEqual(result['unverified_trades'], 1)

    def test_reject_unregistered_variant(self):
        bars = candles()
        with self.assertRaises(ValueError):
            simulate(bars, features(bars), RangeRule('breakout_trailing', 42), '2026-01-01', '2027-01-01')

    def test_gate_requires_each_period_and_all_quality(self):
        p = dict(accounts=7, return_pct=1, stress_return_pct=.1, trades=50, positive_accounts=4, unverified_trades=0, max_drawdown_pct=3)
        periods = {n: p.copy() for n in ('development', 'selection', 'recent')}
        quality = {t: True for t in TICKERS}
        self.assertTrue(passes(periods, quality))
        self.assertFalse(passes({}, quality))
        self.assertFalse(passes(periods, {'SBER': True}))
        periods['recent']['trades'] = 2
        self.assertFalse(passes(periods, quality))
        quality['SBER'] = False
        self.assertFalse(passes(periods, quality))

    def test_freeze_fails_before_price_read_when_hash_changes(self):
        with tempfile.TemporaryDirectory() as d, patch('dashboard.management.commands.study_ranges.hashes', return_value={'code': 'a'}):
            freeze(Path(d))
            freeze(Path(d))
            with patch('dashboard.management.commands.study_ranges.hashes', return_value={'code': 'b'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d))

    def test_loader_refuses_dealer_source(self):
        saved = {'version': VERSION, 'instrument': {'ticker': 'SBER', 'lot': 1},
                 'request': {'from': START.isoformat(), 'to_exclusive': END.isoformat(), 'source': 'CANDLE_SOURCE_EXCHANGE', 'interval': 'DAY'},
                 'bars': [{'time': START.isoformat(), 'open': 100, 'high': 101, 'low': 99, 'close': 100,
                           'volume': 1, 'is_complete': True, 'source': 'CANDLE_SOURCE_DEALER'}]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'input.json'
            path.write_text(json.dumps(saved), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_input(path, 'SBER')
