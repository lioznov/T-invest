"""Synthetic causal checks for the fixed close-time comparison; never downloads or trades."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from io import StringIO
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from . import close_timing_study as s
from . import morning_model as m
from .signals import Candle
from .management.commands.study_close_timing import freeze
from .management.commands import study_close_timing as command


def complete_history(days=80):
    """Include weekends and auction-transition bars to prove explicit exclusions."""
    start = datetime(2026, 5, 1, 10, tzinfo=m.MSK)
    bars = []
    for d in range(days):
        for j in range(54):
            price = 100. + d*.07 + j*.004
            bars.append(Candle(start+timedelta(days=d, minutes=15*j),
                               price, price+.3, price-.3, price+.02, 1000+d+j))
    return bars


class CloseTimingExamplesTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bars = complete_history()
        cls.rows, cls.coverage = s.build_examples(cls.bars, 'SBER', 10)
        cls.config = {variant['id']: variant for variant in s.VARIANTS}
        cls.examples = {key: rows[5] for key, rows in cls.rows.items()}

    def changed_row(self, bars, key, at):
        rows = s.build_examples(bars, 'SBER', 10)[0][key]
        return next(row for row in rows if row['at'] == at)

    def test_exact_three_preregistered_variants_and_cutoff_clocks(self):
        expected = {'control': ('17:45', '18:00'), 'main_hour': ('18:00', '18:15'),
                    'evening_hour': ('22:45', '23:00')}
        self.assertEqual(set(self.rows), set(expected))
        for key, (cutoff, entry) in expected.items():
            self.assertEqual(self.config[key]['cutoff'], cutoff)
            self.assertEqual(self.config[key]['entry'], entry)
            self.assertTrue(self.rows[key])
            for row in self.rows[key]:
                at = datetime.fromisoformat(row['at'])
                target = datetime.fromisoformat(row['target_at'])
                self.assertEqual(at.strftime('%H:%M'), cutoff)
                self.assertLessEqual(at.weekday(), 3)
                self.assertEqual(target, m.clock(at.date()+timedelta(days=1), 10))
                self.assertEqual(datetime.fromisoformat(row['label_end']), target+timedelta(minutes=15))
                self.assertEqual(len(row['x']), len(m.FEATURES))
                self.assertEqual(row['x'][-7:], [1., 0., 0., 0., 0., 0., 0.])

    def test_all_variants_use_only_completed_prefix_bars_at_their_own_cutoff(self):
        for key, expected in self.examples.items():
            with self.subTest(variant=key):
                at = datetime.fromisoformat(expected['at'])
                prefix = [bar for bar in self.bars if bar.time < at]
                row = self.changed_row(prefix, key, expected['at'])
                self.assertEqual(row['x'], expected['x'])
                self.assertEqual(row['anchor_price'], expected['anchor_price'])
                self.assertIsNone(row['entry_price'])
                self.assertIsNone(row['actual_price'])
                self.assertIsNone(row['y'])

    def test_future_price_and_volume_changes_never_change_current_features(self):
        for key, expected in self.examples.items():
            with self.subTest(variant=key):
                at = datetime.fromisoformat(expected['at'])
                changed = [replace(bar, open=1000., high=1001., low=999., close=1000., volume=100000)
                           if bar.time >= at else bar for bar in self.bars]
                row = self.changed_row(changed, key, expected['at'])
                self.assertEqual(row['x'], expected['x'])
                self.assertEqual(row['anchor_price'], expected['anchor_price'])
                self.assertNotEqual(row['y'], expected['y'])

    def test_early_forecasts_do_not_require_later_same_day_feature_coverage(self):
        for key in ('control', 'main_hour'):
            expected = self.examples[key]
            at = datetime.fromisoformat(expected['at'])
            # Remove the current evening window only; it is unknown at the early cutoff.
            changed = [bar for bar in self.bars
                       if not (bar.time.date() == at.date() and bar.time.hour >= 19)]
            row = self.changed_row(changed, key, expected['at'])
            self.assertEqual(row['x'], expected['x'])
            self.assertEqual(row['anchor_price'], expected['anchor_price'])

    def test_anchor_and_delayed_entry_are_separate_from_next_morning_target(self):
        by_time = {bar.time: bar for bar in self.bars}
        for key, row in self.examples.items():
            at = datetime.fromisoformat(row['at'])
            entry_hour, entry_minute = map(int, self.config[key]['entry'].split(':'))
            self.assertEqual(row['anchor_price'], by_time[at-timedelta(minutes=15)].close)
            self.assertEqual(row['entry_price'], by_time[m.clock(at.date(), entry_hour, entry_minute)].open)
            self.assertEqual(row['actual_price'], by_time[datetime.fromisoformat(row['target_at'])].open)
            self.assertAlmostEqual(row['y'], (row['actual_price']/row['anchor_price']-1)*100)

    def test_missing_exact_next_day_target_remains_unknown_never_moves_forward(self):
        for key, expected in self.examples.items():
            with self.subTest(variant=key):
                target = datetime.fromisoformat(expected['target_at'])
                row = self.changed_row([b for b in self.bars if b.time != target], key, expected['at'])
                self.assertEqual(row['target_at'], expected['target_at'])
                self.assertIsNone(row['actual_price'])
                self.assertIsNone(row['y'])

    def test_missing_entry_or_zero_volume_target_is_retained_not_filtered(self):
        for key, expected in self.examples.items():
            with self.subTest(variant=key):
                at = datetime.fromisoformat(expected['at'])
                entry = at+timedelta(minutes=15)
                target = datetime.fromisoformat(expected['target_at'])
                changed = [replace(b, volume=0) if b.time == target else b
                           for b in self.bars if b.time != entry]
                row = self.changed_row(changed, key, expected['at'])
                self.assertEqual(row['x'], expected['x'])
                self.assertIsNone(row['entry_price'])
                self.assertIsNone(row['actual_price'])
                self.assertIsNone(row['y'])

    def test_weekend_prices_and_volumes_do_not_enter_shared_weekday_history(self):
        changed = [replace(b, open=5000., high=5001., low=4999., close=5000., volume=100000)
                   if b.time.weekday() >= 5 else b for b in self.bars]
        rows = s.build_examples(changed, 'SBER', 10)[0]
        self.assertEqual(rows, self.rows)
        for variant in rows.values():
            for row in variant:
                day = datetime.fromisoformat(row['at']).date()
                prior_weekdays = sum((day-timedelta(days=i)).weekday() < 5
                                     for i in range(1, (day-self.bars[0].time.date()).days+1))
                self.assertGreaterEqual(prior_weekdays, 20)

    def test_auction_transition_bars_are_excluded_from_evening_features_and_history(self):
        changed = [replace(b, open=5000., high=5001., low=4999., close=5000., volume=100000)
                   if b.time.strftime('%H:%M') in ('18:30', '18:45', '19:00') else b for b in self.bars]
        self.assertEqual(s.build_examples(changed, 'SBER', 10)[0], self.rows)

    def test_missing_completed_feature_bar_excludes_only_affected_origin_variants(self):
        day = datetime.fromisoformat(self.examples['control']['at']).date()
        changed = [b for b in self.bars if b.time != m.clock(day, 22)]
        rows = s.build_examples(changed, 'SBER', 10)[0]
        self.assertTrue(any(r['at'][:10] == day.isoformat() for r in rows['control']))
        self.assertTrue(any(r['at'][:10] == day.isoformat() for r in rows['main_hour']))
        self.assertFalse(any(r['at'][:10] == day.isoformat() for r in rows['evening_hour']))

    def test_incomplete_prior_evening_is_excluded_from_shared_history_after_that_day_only(self):
        expected = self.examples['control']
        day = datetime.fromisoformat(expected['at']).date()
        changed = [b for b in self.bars if b.time != m.clock(day, 22)]
        rows = s.build_examples(changed, 'SBER', 10)[0]
        current = next(r for r in rows['control'] if r['at'] == expected['at'])
        self.assertEqual(current['x'], expected['x'])
        later = next(r for r in self.rows['control'] if r['at'][:10] > day.isoformat())
        new_later = next(r for r in rows['control'] if r['at'] == later['at'])
        self.assertNotEqual(new_later['x'], later['x'])

    def test_exact_feature_starts_are_completed_before_cutoff_and_exclude_transitions(self):
        day = datetime(2026, 7, 1).date()
        counts = {'control': 31, 'main_hour': 32, 'evening_hour': 48}
        for variant in s.VARIANTS:
            times = s.required_starts(day, variant)
            self.assertEqual(len(times), counts[variant['id']])
            self.assertEqual(len(times), len(set(times)))
            self.assertTrue(all(t+timedelta(minutes=15) <= s.at_clock(day, variant['cutoff']) for t in times))
            self.assertTrue(all(t.strftime('%H:%M') not in ('18:30', '18:45', '19:00') for t in times))

    def test_utc_equivalence_and_rejected_malformed_or_unsupported_inputs(self):
        utc = [replace(b, time=b.time.astimezone(timezone.utc)) for b in self.bars]
        self.assertEqual(s.build_examples(utc, 'SBER', 10), (self.rows, self.coverage))
        for bars in (self.bars+[self.bars[-1]], list(reversed(self.bars)),
                     [replace(self.bars[0], high=float('nan'))],
                     [replace(self.bars[0], time=self.bars[0].time.replace(tzinfo=None))],
                     [replace(self.bars[0], volume=-1)]):
            with self.subTest(first=bars[0]), self.assertRaises(ValueError):
                s.build_examples(bars, 'SBER', 10)
        for ticker, lot in (('INVALID', 10), ('SBER', 0), ('SBER', True)):
            with self.subTest(ticker=ticker, lot=lot), self.assertRaises(ValueError):
                s.build_examples([], ticker, lot)

    def test_empty_valid_history_returns_empty_variants(self):
        rows, _ = s.build_examples([], 'SBER', 10)
        self.assertEqual(rows, {key: [] for key in self.config})


class CloseTimingPairAndProtocolTests(SimpleTestCase):
    def test_paired_uses_stock_and_date_and_does_not_remove_unknown_outcomes(self):
        def row(ticker, day, hour, *, y=1.):
            at = datetime.fromisoformat(day).replace(hour=hour, tzinfo=m.MSK)
            return {'ticker': ticker, 'at': at.isoformat(), 'y': y, 'actual_price': None if y is None else 101.}
        control = [row('SBER', '2026-07-01', 17), row('GAZP', '2026-07-01', 17),
                   row('SBER', '2026-07-02', 17)]
        main = [row('SBER', '2026-07-01', 18, y=None), row('GAZP', '2026-07-01', 18)]
        evening = [row('SBER', '2026-07-01', 22), row('SBER', '2026-07-02', 22)]
        paired = s.paired({'control': control, 'main_hour': main, 'evening_hour': evening})
        self.assertEqual(paired['control'], [control[0]])
        self.assertEqual(paired['main_hour'], [main[0]])
        self.assertEqual(paired['evening_hour'], [evening[0]])
        self.assertIsNone(paired['main_hour'][0]['y'])

    def test_paired_empty_variant_makes_every_common_sample_empty(self):
        rows = {'control': [{'ticker': 'SBER', 'at': '2026-07-01T17:45:00+03:00', 'y': 1.}],
                'main_hour': [], 'evening_hour': []}
        self.assertEqual(s.paired(rows), {key: [] for key in rows})

    def test_pairing_is_by_moscow_date_even_with_utc_input(self):
        rows = {'control': [{'ticker': 'SBER', 'at': '2026-07-01T17:45:00+03:00'}],
                'main_hour': [{'ticker': 'SBER', 'at': '2026-07-01T15:00:00+00:00'}],
                'evening_hour': [{'ticker': 'SBER', 'at': '2026-07-01T19:45:00+00:00'}]}
        self.assertEqual(s.paired(rows), rows)

    def test_pairing_rejects_incomplete_variant_sets_naive_dates_and_duplicates(self):
        valid = {v['id']: [{'ticker': 'SBER', 'at': '2026-07-01T17:45:00+03:00'}] for v in s.VARIANTS}
        for bad in ({'control': []}, {**valid, 'control': valid['control']*2},
                    {**valid, 'control': [{'ticker': 'SBER', 'at': '2026-07-01T17:45:00'}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                s.paired(bad)

    def test_protocol_freeze_is_repeatable_and_rejects_changed_source_without_overwrite(self):
        with TemporaryDirectory() as directory, patch.object(s, 'source_hashes', return_value={'code': 'a'}):
            root = Path(directory)
            first = freeze(root)
            paths = list(root.glob('*protocol*.json'))
            self.assertEqual(len(paths), 1)
            original = paths[0].read_bytes()
            self.assertEqual(first, freeze(root))
            with patch.object(s, 'source_hashes', return_value={'code': 'b'}):
                with self.assertRaises(CommandError):
                    freeze(root)
            self.assertEqual(paths[0].read_bytes(), original)

    def test_command_freezes_before_reading_prices_and_preserves_all_three_models(self):
        events = []
        instrument = {'ticker': 'SBER', 'lot': 10}
        histories = {'instrument': instrument, 'candles': [], 'sources': [{'sha256': 'synthetic'}]}
        by_variant = {}
        day = datetime(2026, 7, 1).date()
        for variant in s.VARIANTS:
            target = m.clock(day+timedelta(days=1), 10)
            by_variant[variant['id']] = [{'ticker': 'SBER', 'lot': 10,
                'at': s.at_clock(day, variant['cutoff']).isoformat(), 'target_at': target.isoformat(),
                'label_end': (target+timedelta(minutes=15)).isoformat(), 'x': [0.]*len(m.FEATURES),
                'anchor_price': 100., 'entry_price': 100., 'actual_price': 101., 'y': 1., 'reason': 'ok'}]
        model = {'center': [0.]*len(m.FEATURES), 'scale': [1.]*len(m.FEATURES),
                 'weights': [.6]+[0.]*len(m.FEATURES), 'interval_abs_pct': .2, 'train_mean_pct': .1}
        original_freeze = freeze

        def tracked_freeze(path):
            events.append('freeze')
            return original_freeze(path)

        def tracked_history(*args):
            events.append('history')
            return histories

        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch.object(command, 'freeze', side_effect=tracked_freeze), \
                patch.object(command, 'load_history', side_effect=tracked_history), \
                patch.object(s, 'build_examples', return_value=(by_variant, {})), \
                patch.object(s, 'source_hashes', return_value={'code': 'synthetic'}), \
                patch.object(m, 'TICKERS', ('SBER',)), patch.object(m, 'fit', return_value=model) as fit:
            command.Command(stdout=StringIO()).handle()
            self.assertEqual(events, ['freeze', 'history'])
            self.assertEqual(fit.call_count, 3)
            paths = list((Path(directory)/'dashboard'/'ai_models').glob('close-timing-v1-*.json'))
            self.assertEqual(len(paths), 1)
            artifact = json.loads(paths[0].read_text(encoding='utf-8'))
            self.assertEqual([v['id'] for v in artifact['variants']], [v['id'] for v in s.VARIANTS])
            self.assertFalse(artifact['entry_allowed'])
            expected_digest = artifact.pop('artifact_sha256')
            self.assertEqual(s.digest(artifact), expected_digest)
            self.assertTrue(all('paired_report' in variant and 'report' in variant for variant in artifact['variants']))
