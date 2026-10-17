from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase
from django.core.management.base import CommandError
from .signals import Candle, MSK
from .opening_activity import scan, select_active, simulate, passes, SCENARIOS
from .management.commands.study_opening_activity import freeze


def bars(volume=200):
    at = datetime(2026,10,12,10,tzinfo=MSK)
    days, cursor = [], at-timedelta(days=1)
    while len(days)<14:
        if cursor.weekday()<5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    history = [Candle(d,100,101,99,100.5,100) for d in reversed(days)]
    current = [Candle(at,100,101,99,100.7,volume),
               Candle(at+timedelta(minutes=15),100.7,102,100.5,101.5,200),
               Candle(at+timedelta(minutes=30),101.5,101.7,100.95,101.2,100),
               Candle(at+timedelta(minutes=45),101.2,102,101.1,101.9,200)]
    current += [Candle(at+timedelta(minutes=15*i),101.9,102.5,101.5,102.3,100) for i in range(4,32)]
    return history+current


class OpeningActivityTests(SimpleTestCase):
    def run_model(self, data=None, key='plain', **kwargs):
        data = data or bars()
        return simulate(data,scan(data)['events'][key],'2026-10-12','2026-10-13',10,**kwargs)

    def test_opening_volume_excludes_today_and_waits_for_history(self):
        data = bars()
        events = scan(data)['events']['plain']
        self.assertIsNone(events[14])
        self.assertEqual(events[15]['rvol'],2)
        self.assertFalse(any(scan(data[1:])['events']['plain']))

    def test_no_future_prices_enter_detection(self):
        data = bars()
        full, prefix = scan(data), scan(data[:18])
        for key in full['events']:
            self.assertEqual(full['events'][key][:18],prefix['events'][key])

    def test_separate_retest_and_confirmation(self):
        events = scan(bars())['events']['retest']
        self.assertIsNone(events[15])
        self.assertIsNone(events[16])
        self.assertIsNotNone(events[17])
        self.assertEqual(events[17]['signal_at'],'2026-10-12T11:00:00+03:00')

    def test_missing_opening_and_intraday_gap_cancel_pattern(self):
        self.assertFalse(any(scan(bars()[:14]+bars()[15:])['events']['plain']))
        data = bars()
        self.assertFalse(any(scan(data[:15]+data[16:])['events']['plain']))

    def test_red_opening_prevents_long(self):
        data=bars()
        data[14]=replace(data[14],open=100.8)
        self.assertFalse(any(scan(data)['events']['plain']))

    def test_active_selection_uses_opening_volume_not_later_profit(self):
        datasets={name:{'candles':bars(volume)} for name,volume in (('A',200),('B',300),('C',400))}
        scans={name:scan(row['candles']) for name,row in datasets.items()}
        choices=select_active(datasets,scans)
        self.assertEqual(choices['2026-10-12'],['C','B'])
        self.assertFalse(any(scans['A']['events']['active']))
        self.assertTrue(any(scans['A']['events']['plain']))

    def test_next_open_whole_lots_risk_costs_and_session_close(self):
        r=self.run_model()
        t=r['trade_log'][0]
        self.assertEqual(t['entry_time'],'2026-10-12T10:30:00+03:00')
        self.assertEqual(t['exit_bar'],'2026-10-12T17:45:00+03:00')
        self.assertEqual(t['shares']%10,0)
        self.assertLessEqual(t['cost'],25000)
        self.assertLessEqual(t['planned_loss'],500)
        self.assertEqual(r['unverified_trades'],0)
        self.assertLess(self.run_model(multiplier=2)['return_pct'],r['return_pct'])

    def test_delayed_execution_uses_original_signal(self):
        r=self.run_model(delay=1)
        self.assertEqual(r['trade_log'][0]['entry_time'],'2026-10-12T10:45:00+03:00')
        self.assertEqual(r['trade_log'][0]['signal_at'],'2026-10-12T10:30:00+03:00')

    def test_stop_on_entry_bar_is_not_ignored(self):
        data=bars()
        data[16]=replace(data[16],low=98)
        r=self.run_model(data)
        self.assertEqual(r['trade_log'][0]['reason'],'stop')
        self.assertLess(r['return_pct'],0)

    def test_stop_gap_uses_worse_open(self):
        data=bars()
        data[17]=replace(data[17],open=95,high=96,low=94,close=95)
        r=self.run_model(data)
        self.assertEqual(r['trade_log'][0]['exit_quote'],95)
        self.assertEqual(r['trade_log'][0]['reason'],'gap_stop')

    def test_zero_current_volume_does_not_remove_losing_trade(self):
        data=bars()
        data[16]=replace(data[16],volume=0,low=98)
        r=self.run_model(data)
        self.assertEqual(r['trades'],1)
        self.assertEqual(r['unverified_trades'],1)
        self.assertLess(r['return_pct'],0)

    def test_missing_session_end_or_held_gap_marks_unknown(self):
        self.assertEqual(self.run_model(bars()[:-1])['unverified_trades'],1)
        data=bars()
        self.assertEqual(self.run_model(data[:20]+data[21:])['unverified_trades'],1)

    def test_no_trade_never_passes_screen(self):
        periods={p:{s:{'accounts':7,'trades':0,'unverified_trades':0,'return_pct':0,'max_stock_drawdown_pct':0,'positive_accounts':0}
                    for s in SCENARIOS} for p in ('development','selection','recent')}
        self.assertFalse(passes(periods))

    def test_frozen_code_cannot_change_after_registration(self):
        with TemporaryDirectory() as d,patch('dashboard.management.commands.study_opening_activity.hashes',return_value={'code':'a'}):
            freeze(Path(d))
            with patch('dashboard.management.commands.study_opening_activity.hashes',return_value={'code':'b'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d))
