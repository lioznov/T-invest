"""Independent synthetic checks; no real training, network, orders or messages."""
import copy
import math
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from . import morning_model as m
from .morning_inputs import load_history
from .signals import Candle
from .management.commands.train_morning_forecast import freeze


def history(days=90):
    """Include observed weekend sessions to exercise explicit origin exclusions."""
    result = []
    start = datetime(2026, 5, 1, 10, tzinfo=m.MSK)
    for day in range(days):
        for j in range(34):
            price = 100. + day*.05 + j*.003
            result.append(Candle(start+timedelta(days=day, minutes=15*j),
                                 price, price+.25, price-.25, price+.015, 1000+j))
    return result


def constant_model(value=1., interval=.2):
    return {'center': [0.]*len(m.FEATURES), 'scale': [1.]*len(m.FEATURES),
            'weights': [value]+[0.]*len(m.FEATURES),
            'interval_abs_pct': interval, 'train_mean_pct': .1}


def example(day='2026-07-01', ticker='SBER', *, y=1., entry=100., actual=101., lot=10):
    at = datetime.fromisoformat(day).replace(hour=17, minute=45, tzinfo=m.MSK)
    target = (at+timedelta(days=1)).replace(hour=10, minute=0)
    return {'ticker': ticker, 'lot': lot, 'at': at.isoformat(),
            'target_at': target.isoformat(), 'label_end': (target+timedelta(minutes=15)).isoformat(),
            'anchor_price': 100., 'entry_price': entry, 'actual_price': actual,
            'x': [0.]*len(m.FEATURES), 'y': y, 'reason': 'ok' if y is not None else 'missing'}


def fitting_rows(start, days, per_day):
    result = []
    for d in range(days):
        day = (datetime.fromisoformat(start)+timedelta(days=d)).date().isoformat()
        for j in range(per_day):
            row = example(day, m.TICKERS[j % len(m.TICKERS)])
            t = d*per_day+j
            row['x'] = [math.sin((t+1)*(k+1)/17) for k in range(len(m.FEATURES))]
            row['y'] = .15+.7*row['x'][0]-.2*row['x'][1]
            result.append(row)
    return result


class MorningExamplesTests(SimpleTestCase):
    def setUp(self):
        self.bars = history()
        self.rows, self.coverage = m.build_examples(self.bars, 'SBER', 10)
        self.row = self.rows[5]
        self.at = datetime.fromisoformat(self.row['at'])

    def test_fixed_clocks_horizon_weekdays_and_feature_dimensions(self):
        self.assertTrue(self.rows)
        for row in self.rows:
            at, target = datetime.fromisoformat(row['at']), datetime.fromisoformat(row['target_at'])
            self.assertLessEqual(at.weekday(), 3)
            self.assertEqual((at.hour, at.minute), (17, 45))
            self.assertEqual(target.date(), at.date()+timedelta(days=1))
            self.assertEqual((target.hour, target.minute), (10, 0))
            self.assertEqual(datetime.fromisoformat(row['label_end']), target+timedelta(minutes=15))
            self.assertEqual(len(row['x']), len(m.FEATURES))
            self.assertTrue(all(math.isfinite(x) for x in row['x']))
            self.assertEqual(row['x'][-7:], [1., 0., 0., 0., 0., 0., 0.])
        self.assertGreater(self.coverage['excluded_friday_weekend'], 0)

    def test_features_available_at_cutoff_without_entry_or_target(self):
        prefix = [bar for bar in self.bars if bar.time < self.at]
        row = next(r for r in m.build_examples(prefix, 'SBER', 10)[0] if r['at'] == self.row['at'])
        self.assertEqual(row['x'], self.row['x'])
        self.assertEqual(row['anchor_price'], self.row['anchor_price'])
        self.assertIsNone(row['entry_price'])
        self.assertIsNone(row['actual_price'])
        self.assertIsNone(row['y'])

    def test_valid_future_perturbation_changes_label_not_features(self):
        changed = [replace(b, open=1000., high=1001., low=999., close=1000., volume=100000)
                   if b.time >= self.at else b for b in self.bars]
        row = next(r for r in m.build_examples(changed, 'SBER', 10)[0] if r['at'] == self.row['at'])
        self.assertEqual(row['x'], self.row['x'])
        self.assertEqual(row['anchor_price'], self.row['anchor_price'])
        self.assertNotEqual(row['y'], self.row['y'])

    def test_cutoff_anchor_entry_and_target_use_distinct_prices(self):
        by_time = {b.time: b for b in self.bars}
        anchor = by_time[self.at-timedelta(minutes=15)].close
        entry = by_time[self.at.replace(hour=18, minute=0)].open
        actual = by_time[datetime.fromisoformat(self.row['target_at'])].open
        self.assertEqual(self.row['anchor_price'], anchor)
        self.assertEqual(self.row['entry_price'], entry)
        self.assertEqual(self.row['actual_price'], actual)
        self.assertAlmostEqual(self.row['y'], (actual/anchor-1)*100)

    def test_target_and_entry_bar_close_are_not_labels_or_execution(self):
        target = datetime.fromisoformat(self.row['target_at'])
        entry = self.at.replace(hour=18, minute=0)
        changed = [replace(b, close=b.close+.1) if b.time in (target, entry) else b for b in self.bars]
        row = next(r for r in m.build_examples(changed, 'SBER', 10)[0] if r['at'] == self.row['at'])
        self.assertEqual(row, self.row)

    def test_missing_next_day_open_is_unknown_not_shifted_to_later_date(self):
        target = datetime.fromisoformat(self.row['target_at'])
        bars = [b for b in self.bars if b.time != target]
        rows, quality = m.build_examples(bars, 'SBER', 10)
        row = next(r for r in rows if r['at'] == self.row['at'])
        self.assertIsNone(row['y'])
        self.assertIsNone(row['actual_price'])
        self.assertEqual(row['target_at'], self.row['target_at'])
        self.assertGreater(quality['unknown_targets'], 0)

    def test_zero_volume_target_and_missing_entry_are_retained(self):
        target = datetime.fromisoformat(self.row['target_at'])
        entry = self.at.replace(hour=18, minute=0)
        bars = [replace(b, volume=0) if b.time == target else b for b in self.bars if b.time != entry]
        rows, quality = m.build_examples(bars, 'SBER', 10)
        row = next(r for r in rows if r['at'] == self.row['at'])
        self.assertIsNone(row['entry_price'])
        self.assertIsNone(row['actual_price'])
        self.assertIsNone(row['y'])
        self.assertGreater(quality['unknown_entries'], 0)

    def test_missing_feature_bar_drops_origin_not_filled_from_future(self):
        bars = [b for b in self.bars if b.time != self.at-timedelta(minutes=30)]
        rows, quality = m.build_examples(bars, 'SBER', 10)
        self.assertFalse(any(r['at'] == self.row['at'] for r in rows))
        self.assertGreater(quality['incomplete_day_features'], 0)

    def test_utc_conversion_preserves_exact_examples(self):
        converted = [replace(b, time=b.time.astimezone(timezone.utc)) for b in self.bars]
        self.assertEqual(m.build_examples(converted, 'SBER', 10), (self.rows, self.coverage))

    def test_invalid_or_duplicate_candles_and_bad_instruments_are_rejected(self):
        for bars in (self.bars+[self.bars[-1]], list(reversed(self.bars)),
                     [replace(self.bars[0], high=float('nan'))],
                     [replace(self.bars[0], low=200.)],
                     [replace(self.bars[0], time=self.bars[0].time.replace(tzinfo=None))],
                     [replace(self.bars[0], volume=-1)]):
            with self.subTest(bars=bars[:1]), self.assertRaises(ValueError):
                m.build_examples(bars, 'SBER', 10)
        for ticker, lot in (('INVALID', 10), ('SBER', 0), ('SBER', True)):
            with self.subTest(ticker=ticker, lot=lot), self.assertRaises(ValueError):
                m.build_examples([], ticker, lot)


class MorningLearningTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.train = fitting_rows('2026-06-01', 35, 6)
        cls.calibration = fitting_rows('2026-08-01', 10, 5)

    def test_chronological_splits_are_shared_and_purge_label_end(self):
        train = example('2026-07-30')
        cal = example('2026-08-03', 'GAZP')
        crossing = example('2026-07-31')
        groups = m.split([train, cal, crossing])
        self.assertEqual(groups['train'], [train])
        self.assertEqual(groups['calibration'], [cal])
        self.assertNotIn(crossing, sum(groups.values(), []))
        boundary = dict(train, label_end='2026-08-01T00:00:00+03:00')
        self.assertEqual(m.split([boundary])['train'], [])

    def test_split_boundaries_use_moscow_time_not_iso_string_date(self):
        row = dict(example(), at='2026-07-31T22:00:00+00:00', label_end='2026-08-02T07:15:00+00:00')
        self.assertEqual(m.split([row])['train'], [])
        self.assertEqual(m.split([row])['calibration'], [row])

    def test_fit_is_deterministic_and_calibration_does_not_refit_weights(self):
        model = m.fit(self.train, self.calibration)
        self.assertEqual(model, m.fit(self.train, self.calibration))
        changed = [dict(r, y=r['y']+5., x=[100.]*len(m.FEATURES)) for r in self.calibration]
        calibrated = m.fit(self.train, changed)
        for key in ('center', 'scale', 'weights', 'train_mean_pct'):
            self.assertEqual(model[key], calibrated[key])
        self.assertNotEqual(model['interval_abs_pct'], calibrated['interval_abs_pct'])
        expected = sum(r['x'][0] for r in self.train)/len(self.train)
        self.assertAlmostEqual(model['center'][0], expected)

    def test_unknown_labels_do_not_train_or_calibrate_as_zero(self):
        model = m.fit(self.train, self.calibration)
        unknown = dict(self.train[0], y=None, x=[10000.]*len(m.FEATURES))
        self.assertEqual(model, m.fit(self.train+[unknown], self.calibration+[unknown]))

    def test_training_label_touching_or_crossing_calibration_boundary_is_rejected(self):
        boundary = datetime.fromisoformat(self.calibration[0]['at'])
        for end in (boundary, boundary+timedelta(minutes=15)):
            train = copy.deepcopy(self.train)
            train[-1]['label_end'] = end.isoformat()
            with self.subTest(end=end), self.assertRaises(ValueError):
                m.fit(train, self.calibration)

    def test_unknown_training_label_does_not_create_a_fake_future_outcome(self):
        model = m.fit(self.train, self.calibration)
        unknown = dict(self.train[0], y=None, label_end='2026-12-31T10:15:00+03:00')
        self.assertEqual(model, m.fit(self.train+[unknown], self.calibration))

    def test_finite_well_sized_training_and_calibration_are_required(self):
        for part, field, bad in (('train', 'y', float('nan')), ('train', 'x', [0.]),
                                 ('train', 'x', [float('inf')]*len(m.FEATURES)),
                                 ('calibration', 'y', float('nan'))):
            train, calibration = copy.deepcopy(self.train), copy.deepcopy(self.calibration)
            rows = train if part == 'train' else calibration
            for row in rows:
                row[field] = bad
            with self.subTest(part=part, field=field), self.assertRaises(ValueError):
                m.fit(train, calibration)

    def test_independent_date_minimum_not_just_row_count(self):
        for train, calibration in ((self.train[:10], self.calibration),
                                   ([self.train[0]]*210, self.calibration),
                                   (self.train, [self.calibration[0]]*50)):
            with self.assertRaises(ValueError):
                m.fit(train, calibration)

    def test_predict_rejects_nonfinite_or_wrong_dimension_features(self):
        for x in ([0.], [float('nan')]*len(m.FEATURES), [True]*len(m.FEATURES)):
            with self.assertRaises(ValueError):
                m.predict(constant_model(), x)

    def test_intervals_prices_and_abstention_are_not_confidence_percent(self):
        row = example()
        for value, direction in ((1., 'up'), (-1., 'down'), (.1, 'uncertain'), (.2, 'uncertain')):
            f = m.forecast(constant_model(value, .2), row)
            self.assertEqual(f['direction'], direction)
            self.assertAlmostEqual(f['predicted_price'], 100.+value)
            self.assertAlmostEqual(f['upper_price']-f['lower_price'], .4)
            self.assertTrue(f['historical'])
            self.assertNotIn('confidence', f)

    def test_evaluation_baselines_intervals_and_unknown_counts(self):
        rows = [example(y=1.), example(y=-1.), example(y=None, actual=None)]
        result = m.evaluate(rows, constant_model(.5, .75))
        self.assertEqual(result['eligible_forecasts'], 3)
        self.assertEqual(result['samples'], 2)
        self.assertEqual(result['unknown_targets'], 1)
        self.assertEqual(result['mae_bps'], 100.)
        self.assertEqual(result['baseline_mae_bps'], 100.)
        self.assertAlmostEqual(result['mean_baseline_mae_bps'], 100.)
        self.assertEqual(result['coverage_pct'], 50.)
        self.assertEqual(result['interval_width_bps'], 150.)
        self.assertEqual(result['abstention_pct'], 100.)
        self.assertEqual(result['direction_accuracy_pct'], 50.)


class MorningPaperTests(SimpleTestCase):
    def test_flat_executable_prices_lose_money_after_fees_and_slippage(self):
        result = m.paper([example(actual=100., y=0.)], constant_model())
        self.assertEqual(result['trades'], 1)
        self.assertLess(result['net_return_pct'], 0.)
        stress = m.paper([example(actual=100., y=0.)], constant_model(), multiplier=2)
        self.assertLess(stress['net_return_pct'], result['net_return_pct'])

    def test_unknown_selected_trade_poisoned_account_never_releases_cash(self):
        rows = [example('2026-07-01'), example('2026-07-02', actual=None, y=None),
                example('2026-07-06', actual=10000., y=9900.)]
        result = m.paper(rows, constant_model())
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['trades'], 1)
        self.assertEqual(result['selected_candidates'], 3)
        self.assertEqual(result['unknown_trades'], 1)
        self.assertIsNone(result['net_return_pct'])
        self.assertIsNone(result['max_drawdown_pct'])

    def test_missing_entry_is_unknown_and_no_trades_are_not_zero_percent(self):
        result = m.paper([example(entry=None)], constant_model())
        self.assertEqual(result['unknown_trades'], 1)
        self.assertEqual(result['status'], 'incomplete')
        self.assertIsNone(result['net_return_pct'])
        result = m.paper([example()], constant_model(.4))
        self.assertEqual(result['selected_candidates'], 0)
        self.assertIsNone(result['net_return_pct'])
        self.assertIsNone(result['max_drawdown_pct'])

    def test_whole_lots_and_allocation_ceiling_prevent_full_capital_single_stock(self):
        result = m.paper([example(lot=1, actual=110.)], constant_model())
        self.assertGreater(result['net_return_pct'], 0.)
        self.assertLess(result['net_return_pct'], 2.5)
        impossible = m.paper([example(lot=1000000)], constant_model())
        self.assertEqual(impossible['trades'], 0)
        self.assertIsNone(impossible['net_return_pct'])

    def test_large_list_shares_budget_without_leverage(self):
        rows = [example(ticker=t, lot=1, actual=110.) for t in m.TICKERS]
        result = m.paper(rows, constant_model())
        self.assertEqual(result['trades'], len(m.TICKERS))
        self.assertGreater(result['net_return_pct'], 0.)
        self.assertLess(result['net_return_pct'], 10.)

    def test_anchor_forecast_is_not_executable_entry_profit(self):
        result = m.paper([example(entry=105., actual=101.)], constant_model(1.))
        self.assertLess(result['net_return_pct'], 0.)
        self.assertAlmostEqual(result['mean_anchor_to_entry_pct'], 5.)
        self.assertFalse(m.RULES['entry_allowed'])


class MorningInputsAndProtocolTests(SimpleTestCase):
    def test_protocol_freeze_rejects_changed_spec_without_overwriting(self):
        with TemporaryDirectory() as directory, patch.object(m, 'source_hashes', return_value={'code': 'a'}):
            path = Path(directory)
            first = freeze(path)
            original = (path/'morning-ridge-protocol-v1.json').read_bytes()
            self.assertEqual(first, freeze(path))
            with patch.object(m, 'source_hashes', return_value={'code': 'b'}):
                with self.assertRaises(CommandError):
                    freeze(path)
            self.assertEqual((path/'morning-ridge-protocol-v1.json').read_bytes(), original)

    def source_mocks(self, stack, *, quality=True, extension=None):
        instrument = {'ticker': 'SBER', 'uid': 'synthetic-only', 'lot': 10}
        bar = history(1)[0]
        base = stack.enter_context(patch('dashboard.management.commands.study_pattern_flow.acquire', return_value={
            'quality': {'exchange_requested': quality, 'exchange_source_verified': quality},
            'saved': {'instrument': instrument}, 'candles': [bar], 'source': {'sha256': 'base'}}))
        loaders = [stack.enter_context(patch(name, return_value=(extension or [], {'sha256': str(i)})))
                   for i, name in enumerate((
                       'dashboard.management.commands.study_opening_activity.extension',
                       'dashboard.management.commands.verify_neural_trader.load_extension',
                       'dashboard.management.commands.study_diverse_long.fresh_input'))]
        return base, loaders, instrument, bar

    def test_history_requests_no_download_and_identical_overlap_is_not_duplicated(self):
        with ExitStack() as stack:
            base, loaders, instrument, bar = self.source_mocks(stack)
            loaders[0].return_value = ([bar], {'sha256': 'overlap'})
            result = load_history(Path('synthetic-no-read'), 'SBER')
            self.assertEqual(result['candles'], [bar])
            self.assertEqual(len(result['sources']), 4)
            base.assert_called_once_with(Path('synthetic-no-read'), 'SBER', fetch=False)
            for loader in loaders:
                loader.assert_called_once_with(Path('synthetic-no-read'), 'SBER', instrument, False)

    def test_unverified_source_or_conflicting_overlap_is_rejected(self):
        with ExitStack() as stack:
            self.source_mocks(stack, quality=False)
            with self.assertRaises(ValueError):
                load_history(Path('synthetic-no-read'), 'SBER')
        with ExitStack() as stack:
            _, loaders, _, bar = self.source_mocks(stack)
            loaders[0].return_value = ([replace(bar, close=bar.close+.01)], {'sha256': 'conflict'})
            with self.assertRaises(ValueError):
                load_history(Path('synthetic-no-read'), 'SBER')
