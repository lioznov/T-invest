from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase
from django.core.management.base import CommandError
from .test_range_study import candles
from .trader_tactics import features,setup,simulate,VARIANTS,Tactic,passes
from .management.commands.study_trader_tactics import freeze


class TraderTacticsTests(SimpleTestCase):
    def example(self):
        bars = candles(215)
        rows = features(bars)
        rows[199].update(sma200=1,rsi2=0,three_down=True)
        return bars,rows

    def test_sma_uses_200_closed_values_only(self):
        bars = candles(215)
        rows = features(bars)
        self.assertIsNone(rows[198]['sma200'])
        self.assertEqual(rows[199]['sma200'],sum(c.close for c in bars[:200])/200)
        self.assertEqual(rows[:205],features(bars[:205]))

    def test_three_down_is_close_to_close_not_red_body(self):
        bars = candles(215)
        for i in (197,198,199):
            bars[i] = replace(bars[i],open=120,high=121,low=110,close=119-(i-197))
        self.assertTrue(features(bars)[199]['three_down'])
        bars[199] = replace(bars[199],close=120)
        self.assertFalse(features(bars)[199]['three_down'])

    def test_setup_requires_trend_warmup_and_volume(self):
        bars,rows = self.example()
        self.assertTrue(setup(bars[199],rows[199],VARIANTS[0]))
        self.assertFalse(setup(replace(bars[199],volume=0),rows[199],VARIANTS[0]))
        rows[199]['sma200'] = None
        self.assertFalse(setup(bars[199],rows[199],VARIANTS[0]))

    def test_signal_enters_next_open_and_exits_after_closed_rebound(self):
        bars,rows = self.example()
        result = simulate(bars,rows,VARIANTS[0],'2026-01-01','2027-01-01')
        trade = result['trade_log'][0]
        self.assertEqual(trade['signal_time'],bars[199].time.isoformat())
        self.assertEqual(trade['entry_time'],bars[200].time.isoformat())
        self.assertEqual(trade['exit_bar'],bars[201].time.isoformat())
        self.assertLess(result['stress_return_pct'],result['return_pct'])

    def test_limit_touch_is_not_guaranteed_fill(self):
        bars,rows = self.example()
        limit = bars[199].close*.96
        bars[200] = replace(bars[200],low=limit)
        result = simulate(bars,rows,VARIANTS[1],'2026-01-01','2027-01-01')
        self.assertEqual(result['trades'],0)
        self.assertEqual(result['ambiguous_touches'],1)

    def test_limit_cross_and_whole_lots(self):
        bars,rows = self.example()
        limit = bars[199].close*.96
        bars[200] = replace(bars[200],low=limit-.5)
        result = simulate(bars,rows,VARIANTS[1],'2026-01-01','2027-01-01',lot=10)
        trade = result['trade_log'][0]
        self.assertEqual(trade['shares']%10,0)
        self.assertLessEqual(trade['entry_cost_per_share']/1.0005,limit+1e-9)
        self.assertLessEqual(trade['stress_entry_cost_per_share']/1.001,limit+1e-9)

    def test_limit_gap_below_stop_is_recorded_not_deleted(self):
        bars,rows = self.example()
        bars[200] = replace(bars[200],open=100,high=110,low=99,close=105)
        result = simulate(bars,rows,VARIANTS[1],'2026-01-01','2027-01-01')
        trade = result['trade_log'][0]
        self.assertEqual(trade['reason'],'entry_gap_through_stop')
        self.assertEqual(trade['entry_quote'],100)
        self.assertLess(trade['pnl'],0)

    def test_zero_current_volume_marks_unknown_not_future_entry_veto(self):
        bars,rows = self.example()
        bars[200] = replace(bars[200],volume=0)
        result = simulate(bars,rows,VARIANTS[0],'2026-01-01','2027-01-01')
        self.assertEqual(result['trades'],1)
        self.assertEqual(result['unverified_trades'],1)

    def test_stop_gap_uses_bad_open(self):
        bars,rows = self.example()
        bars[201] = replace(bars[201],open=100,high=110,low=99,close=105)
        result = simulate(bars,rows,VARIANTS[0],'2026-01-01','2027-01-01')
        self.assertEqual(result['trade_log'][0]['exit_quote'],100)
        self.assertEqual(result['trade_log'][0]['reason'],'gap_stop')

    def test_weekend_stop_not_skipped(self):
        bars,rows = self.example()
        at = bars[198].time+timedelta(days=1)
        while at.weekday()!=3:
            at += timedelta(days=1)
        for i in range(199,len(bars)):
            bars[i] = replace(bars[i],time=at+timedelta(days=i-199))
        bars[201] = replace(bars[201],open=100,high=110,low=99,close=105)
        result = simulate(bars,rows,VARIANTS[0],'2026-01-01','2027-01-01')
        self.assertEqual(bars[201].time.weekday(),5)
        self.assertEqual(result['trade_log'][0]['exit_bar'],bars[201].time.isoformat())

    def test_time_exit_is_next_open_after_ten_closed_bars(self):
        bars,rows = self.example()
        for f in rows[200:]:
            f['rsi2']=20
        result = simulate(bars,rows,VARIANTS[0],'2026-01-01','2027-01-01')
        self.assertEqual(result['trade_log'][0]['exit_bar'],bars[210].time.isoformat())
        self.assertEqual(result['trade_log'][0]['reason'],'time_next_open')

    def test_unregistered_and_empty_gates_fail(self):
        bars,rows = self.example()
        with self.assertRaises(ValueError):
            simulate(bars,rows,Tactic('unregistered'),'2026-01-01','2027-01-01')
        self.assertFalse(passes({}))

    def test_freeze_prevents_changes_before_prices(self):
        with TemporaryDirectory() as d,patch('dashboard.management.commands.study_trader_tactics.hashes',return_value={'code':'a'}):
            freeze(Path(d))
            with patch('dashboard.management.commands.study_trader_tactics.hashes',return_value={'code':'b'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d))
