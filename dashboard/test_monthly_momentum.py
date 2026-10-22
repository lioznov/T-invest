from dataclasses import replace
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase
from django.core.management.base import CommandError
from .signals import Candle
from .monthly_momentum import VARIANTS, prepare, rank, simulate, choose, bootstrap_family, monthly_returns, shift_month, buy_hold
from .management.commands.study_monthly_momentum import freeze


def sample(n=850):
    return [Candle(datetime(2023, 1, 1, tzinfo=timezone.utc)+timedelta(days=i),
                   100+i*.1, 101+i*.1, 99+i*.1, 100.2+i*.1, 10000) for i in range(n)]


class MonthlyMomentumTests(SimpleTestCase):
    def test_features_are_causal(self):
        bars = sample(420)
        self.assertEqual(prepare(bars, 1)['rows'][:300], prepare(bars[:300], 1)['rows'])
        self.assertIsNone(prepare(bars, 1)['rows'][198]['sma200'])

    def test_calendar_months_skip_recent_month(self):
        self.assertEqual(shift_month(date(2026, 1, 8), -1), date(2025, 12, 1))
        bars = sample()
        p = {'A': prepare(bars, 1)}
        wanted = rank(p, date(2024, 3, 1), VARIANTS[0])
        self.assertEqual(wanted, ['A'])
        # Change future prices; past ranks must stay identical.
        altered = [replace(b, open=b.open*3, high=b.high*3, low=b.low*3, close=b.close*3)
                   if b.time.date() >= date(2024, 3, 1) else b for b in bars]
        self.assertEqual(wanted, rank({'A': prepare(altered, 1)}, date(2024, 3, 1), VARIANTS[0]))

    def test_breadth_counts_unavailable_names_in_denominator(self):
        bars = sample()
        p = {'A': prepare(bars, 1), 'B': prepare(bars[:20], 1), 'C': prepare(bars[:20], 1)}
        self.assertEqual(rank(p, date(2024, 3, 1), VARIANTS[0]), ['A'])
        self.assertEqual(rank(p, date(2024, 3, 1), VARIANTS[1]), [])

    def run_example(self, changes=None, **kwargs):
        bars = sample()
        for day, change in (changes or {}).items():
            i = next(i for i, b in enumerate(bars) if b.time.date().isoformat() == day)
            bars[i] = replace(bars[i], **change)
        return simulate({'A': prepare(bars, 10)}, VARIANTS[0], '2024-03-01', '2024-04-01', **kwargs)

    def test_whole_lots_cost_and_allocation(self):
        result = self.run_example()
        t = result['trade_log'][0]
        self.assertEqual(t['entry_day'], '2024-03-01')
        self.assertEqual(t['shares'] % 10, 0)
        self.assertLessEqual(t['cost'], 25000)
        self.assertLessEqual(t['cost']-t['shares']*(t['entry_quote']-6)*.9995*.9995, 500)
        self.assertTrue(result['verified'])
        self.assertLess(self.run_example(cost_multiplier=2)['return_pct'], result['return_pct'])

    def test_delay_keeps_initial_decision_and_enters_next_weekday(self):
        ordinary, delayed = self.run_example(), self.run_example(delay=1)
        self.assertEqual(ordinary['decisions'][0]['wanted'], delayed['decisions'][0]['wanted'])
        self.assertEqual(delayed['trade_log'][0]['signal_day'], '2024-03-01')
        self.assertEqual(delayed['trade_log'][0]['entry_day'], '2024-03-04')

    def test_weekend_gap_stop_uses_open(self):
        r = self.run_example({'2024-03-02': {'open': 130, 'high': 135, 'low': 125, 'close': 132}})
        self.assertEqual(r['trade_log'][0]['exit_day'], '2024-03-02')
        self.assertEqual(r['trade_log'][0]['exit_quote'], 130)
        self.assertEqual(r['trade_log'][0]['reason'], 'gap_stop')

    def test_current_volume_never_vetoes_an_open_entry(self):
        r = self.run_example({'2024-03-01': {'volume': 0}})
        self.assertEqual(r['trades'], 1)
        self.assertFalse(r['verified'])
        self.assertEqual(r['unknown_trades'], 1)

    def test_entry_day_stop_uses_low_after_entry(self):
        r = self.run_example({'2024-03-01': {'low': 130}})
        self.assertEqual(r['trade_log'][0]['entry_day'], r['trade_log'][0]['exit_day'])
        self.assertEqual(r['trade_log'][0]['reason'], 'stop')
        self.assertLess(r['return_pct'], 0)

    def test_missing_held_bar_marks_result_unknown(self):
        bars = sample()
        short = [b for b in bars if b.time.date() != date(2024, 3, 2)]
        with patch('dashboard.monthly_momentum.rank', return_value=['A']):
            r = simulate({'A': prepare(short, 1), 'B': prepare(bars, 1)}, VARIANTS[0], '2024-03-01', '2024-04-01')
        self.assertFalse(r['verified'])
        self.assertIn('missing_held_bar', [i['reason'] for i in r['quality_incidents']])

    def test_trailing_does_not_use_current_high(self):
        r = self.run_example({'2024-03-02': {'high': 10000}})
        self.assertNotEqual(r['trade_log'][0]['exit_day'], '2024-03-02')

    def test_missing_scheduled_entry_is_not_dropped_silently(self):
        bars = sample()
        short = [b for b in bars if b.time.date() != date(2024, 3, 1)]
        with patch('dashboard.monthly_momentum.rank', return_value=['A']):
            r = simulate({'A': prepare(short, 1), 'B': prepare(bars, 1)}, VARIANTS[0], '2024-03-01', '2024-04-01')
        self.assertFalse(r['verified'])
        self.assertEqual(r['trades'], 0)

    def test_flat_price_loses_exact_round_trip_expenses(self):
        bars = [replace(b, open=100, high=101, low=99, close=100) for b in sample()]
        with patch('dashboard.monthly_momentum.rank', return_value=['A']):
            r = simulate({'A': prepare(bars, 1)}, VARIANTS[0], '2024-03-01', '2024-04-01')
        t = r['trade_log'][0]
        self.assertAlmostEqual(t['pnl'], t['shares']*100*((.9995*.9995)-(1.0005*1.0005)))
        self.assertLess(r['return_pct'], 0)

    def test_selection_rejects_unknown_or_tiny_samples(self):
        r = {'verified': True, 'trades': 10, 'return_pct': 1, 'max_drawdown_pct': 1}
        self.assertEqual(choose({'B': r, 'A': r}), 'A')
        self.assertIsNone(choose({'A': {**r, 'verified': False}, 'B': {**r, 'trades': 9}}))

    def test_monthly_returns_compound_and_no_observations_are_unknown(self):
        values = monthly_returns([{'day': '2024-01-31', 'equity': 110000}, {'day': '2024-02-29', 'equity': 99000}])
        self.assertAlmostEqual((1+values['2024-01'])*(1+values['2024-02']), .99)
        r = simulate({'A': prepare([], 1)}, VARIANTS[0], '2024-01-01', '2025-01-01')
        self.assertIsNone(r['return_pct'])
        self.assertFalse(r['verified'])

    def test_bootstrap_is_deterministic_and_cannot_approve_trading(self):
        curve = [{'day': f'{2024+i//12}-{i%12+1:02d}-28', 'equity': 100000*(1.002**(i+1))} for i in range(24)]
        models = {'A': {'verified': True, 'curve': curve}}
        first = bootstrap_family(models, draws=100)
        self.assertEqual(first, bootstrap_family(models, draws=100))
        self.assertFalse(first['entry_allowed'])
        self.assertEqual(bootstrap_family({'A': {'verified': False}})['status'], 'unverified_inputs')

    def test_protocol_freeze_precedes_prices_and_rejects_changes(self):
        with TemporaryDirectory() as d, patch('dashboard.management.commands.study_monthly_momentum.hashes', return_value={'source': 'original'}):
            freeze(Path(d))
            with patch('dashboard.management.commands.study_monthly_momentum.hashes', return_value={'source': 'changed'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d))

    def test_benchmark_round_trip_and_empty_history(self):
        bars = [replace(b, open=100, high=101, low=99, close=100) for b in sample()]
        r = buy_hold({'A': prepare(bars, 1)}, '2024-03-01', '2024-04-01', .25)
        shares = int(25000/(100*1.0005*1.0005))
        self.assertAlmostEqual(r['return_pct'], shares*100*(.9995*.9995-1.0005*1.0005)/1000)
        self.assertTrue(r['verified'])
        self.assertIsNone(buy_hold({'A': prepare([], 1)}, '2024-03-01', '2024-04-01', .25)['return_pct'])
