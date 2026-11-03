"""Synthetic checks for chronological validation; no real fitting or market access."""
import copy
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.test import SimpleTestCase

from . import morning_model as m
from . import temporal_validation as v


def constant_model(value=2., interval=.5, train_mean=1.):
    return {'center': [0.]*len(m.FEATURES), 'scale': [1.]*len(m.FEATURES),
            'weights': [value]+[0.]*len(m.FEATURES), 'interval_abs_pct': interval,
            'train_mean_pct': train_mean}


def row(day='2026-06-01', ticker='SBER', *, anchor=100., actual=101., entry=100.):
    at = datetime.fromisoformat(day).replace(hour=17, minute=45, tzinfo=m.MSK)
    target = m.clock(at.date()+timedelta(days=1), 10)
    return {'ticker': ticker, 'lot': 1, 'at': at.isoformat(), 'target_at': target.isoformat(),
            'label_end': (target+timedelta(minutes=15)).isoformat(), 'anchor_price': anchor,
            'entry_price': entry, 'actual_price': actual,
            'y': (actual/anchor-1)*100 if actual is not None else None,
            'x': [0.]*len(m.FEATURES), 'reason': 'ok' if actual is not None else 'missing'}


class TemporalSelectionTests(SimpleTestCase):
    def test_three_fixed_folds_and_same_current_year_range(self):
        self.assertEqual([fold['id'] for fold in v.FOLDS], ['test_2024', 'test_2025', 'test_2026'])
        self.assertEqual(v.CURRENT_YEAR, {'start': '2026-01-01', 'end_exclusive': '2026-10-23'})
        for year, fold in zip((2024, 2025, 2026), v.FOLDS):
            periods = fold['periods']
            self.assertEqual(periods['train'], {'start': '2018-03-07', 'end_exclusive': f'{year-1}-01-01'})
            self.assertEqual(periods['calibration'], {'start': f'{year-1}-01-01', 'end_exclusive': f'{year}-01-01'})
            self.assertEqual(periods['test']['start'], f'{year}-01-01')

    def test_labels_touching_or_crossing_end_are_purged_not_shifted_between_periods(self):
        periods = v.FOLDS[0]['periods']
        good = row('2022-12-29')
        crossing = row('2022-12-31')
        boundary = dict(row('2022-12-30'), label_end='2023-01-01T00:00:00+03:00')
        calibration = row('2023-06-01')
        test = row('2024-06-03')
        future = row('2025-06-02')
        groups = v.group_rows([good, crossing, boundary, calibration, test, future], periods)
        self.assertEqual(groups['train'], [good])
        self.assertEqual(groups['calibration'], [calibration])
        self.assertEqual(groups['test'], [test])

    def test_moscow_date_boundaries_compare_aware_instants_not_iso_string_dates(self):
        item = dict(row('2024-01-01'), at='2023-12-31T22:00:00+00:00')
        groups = v.group_rows([item], v.FOLDS[0]['periods'])
        self.assertEqual(groups['train'], [])
        self.assertEqual(groups['calibration'], [])
        self.assertEqual(groups['test'], [item])

    def test_start_is_inclusive_and_end_requires_already_known_label(self):
        first = row('2026-01-01')
        last_good = row('2026-10-21')
        crossing = row('2026-10-22')
        before = row('2025-12-30')
        self.assertEqual(v.select_rows([first, last_good, crossing, before], v.CURRENT_YEAR), [first, last_good])

    def test_naive_reversed_horizon_and_duplicate_instant_fail_closed(self):
        base = row()
        at = datetime.fromisoformat(base['at'])
        invalid = [dict(base, at=at.replace(tzinfo=None).isoformat()),
                   dict(base, label_end=at.replace(tzinfo=None).isoformat()),
                   dict(base, label_end=base['at']),
                   dict(base, label_end=(at-timedelta(minutes=1)).isoformat())]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises((ValueError, KeyError, TypeError)):
                v.select_rows([item], v.CURRENT_YEAR)
        duplicate = dict(base, at=at.astimezone(timezone.utc).isoformat())
        with self.assertRaises(ValueError):
            v.select_rows([base, duplicate], v.CURRENT_YEAR)

    def test_unknown_labels_remain_eligible_and_empty_selection_is_empty(self):
        item = row(actual=None)
        self.assertEqual(v.select_rows([item], v.CURRENT_YEAR), [item])
        self.assertEqual(v.select_rows([], v.CURRENT_YEAR), [])

    def test_overlapping_or_inverted_fold_periods_are_rejected(self):
        periods = copy.deepcopy(v.FOLDS[0]['periods'])
        periods['calibration']['start'] = '2022-01-01'
        with self.assertRaises(ValueError):
            v.group_rows([], periods)
        with self.assertRaises(ValueError):
            v.select_rows([], {'start': '2026-06-01', 'end_exclusive': '2026-01-01'})


class TemporalMetricsTests(SimpleTestCase):
    def setUp(self):
        self.rows = [row('2026-06-01', anchor=100., actual=110.),
                     row('2026-06-02', anchor=200., actual=180., entry=200.),
                     row('2026-06-03', 'GAZP', anchor=100., actual=100.),
                     row('2026-06-04', actual=None)]

    def test_return_error_rmse_baselines_and_signed_error_use_exact_anchor_units(self):
        result = v.summarize(self.rows, constant_model())
        report = result['report']
        self.assertEqual(report['samples'], 3)
        self.assertEqual(report['eligible_forecasts'], 4)
        self.assertEqual(report['unknown_targets'], 1)
        self.assertAlmostEqual(report['mae_bps'], 100*(8+12+2)/3)
        self.assertAlmostEqual(report['rmse_bps'], 100*math.sqrt((8**2+12**2+2**2)/3))
        self.assertAlmostEqual(report['baseline_rmse_bps'], 100*math.sqrt((10**2+10**2)/3))
        self.assertAlmostEqual(report['mean_baseline_rmse_bps'], 100*math.sqrt((9**2+11**2+1**2)/3))
        self.assertAlmostEqual(report['mean_signed_error_bps'], 200.)
        self.assertEqual((report['actual_up'], report['actual_down'], report['actual_zero']), (1, 1, 1))
        self.assertEqual(report['direction_baseline_pct'], 50.)
        self.assertEqual(report['paper']['status'], 'incomplete')
        self.assertIsNone(report['paper']['net_return_pct'])

    def test_ruble_errors_and_mape_are_not_percentage_point_error(self):
        result = v.summarize(self.rows, constant_model())
        sber = result['stocks']['SBER']
        self.assertEqual(sber['samples'], 2)
        self.assertAlmostEqual(sber['mae_rub'], 16.)
        self.assertAlmostEqual(sber['rmse_rub'], math.sqrt((8**2+24**2)/2))
        self.assertAlmostEqual(sber['baseline_mae_rub'], 15.)
        self.assertAlmostEqual(sber['baseline_rmse_rub'], math.sqrt((10**2+20**2)/2))
        self.assertAlmostEqual(sber['mean_baseline_rmse_rub'], math.sqrt((9**2+22**2)/2))
        self.assertAlmostEqual(sber['baseline_mape_pct'], 100*(10/110+20/180)/2)
        self.assertAlmostEqual(sber['mean_baseline_mape_pct'], 100*(9/110+22/180)/2)
        self.assertAlmostEqual(sber['mape_pct'], 100*(8/110+24/180)/2)
        self.assertAlmostEqual(result['report']['mape_pct'], 100*(8/110+24/180+2/100)/3)
        self.assertAlmostEqual(result['report']['baseline_mape_pct'], 100*(10/110+20/180)/3)
        self.assertAlmostEqual(result['report']['mean_baseline_mape_pct'], 100*(9/110+22/180+1/100)/3)

    def test_direction_baseline_uses_training_sign_not_test_majority(self):
        rows = [row('2026-06-01', actual=101.), row('2026-06-02', actual=102.),
                row('2026-06-03', actual=99.), row('2026-06-04', actual=100.)]
        result = v.summarize(rows, constant_model(train_mean=-1.))['report']
        self.assertAlmostEqual(result['direction_baseline_pct'], 100/3)
        self.assertEqual(result['direction_nonzero_samples'], 3)
        self.assertIsNone(v.summarize(rows, constant_model(train_mean=0.))['report']['direction_baseline_pct'])

    def test_inconsistent_or_nonpositive_actual_price_labels_are_rejected(self):
        for changed in (dict(row(), actual_price=0.), dict(row(), actual_price=float('nan')),
                        dict(row(), actual_price=None), dict(row(), y=10.)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                v.summarize([changed], constant_model(value=0.))

    def test_examples_are_last_ten_known_sber_not_best_trades_and_largest_relative_error(self):
        rows = [row((datetime(2026, 6, 1)+timedelta(days=i)).date().isoformat(), actual=102.)
                for i in range(14)]
        rows += [row('2026-06-15', actual=None),
                 row('2026-06-16', 'GAZP', anchor=100000., actual=100000.),
                 row('2026-06-17', 'LKOH', anchor=1., actual=2.)]
        result = v.summarize(list(reversed(rows)), constant_model(value=1.))
        latest = [item for item in result['examples'] if item['kind'] == 'latest']
        largest = [item for item in result['examples'] if item['kind'] == 'largest_error']
        self.assertEqual(len(latest), 10)
        self.assertEqual([item['as_of'] for item in latest], [item['at'] for item in rows[4:14]])
        self.assertTrue(all(item['ticker'] == 'SBER' and item['actual_price'] is not None for item in latest))
        self.assertEqual(len(largest), 1)
        self.assertEqual(largest[0]['ticker'], 'LKOH')
        self.assertAlmostEqual(largest[0]['predicted_price'], 1.01)
        self.assertAlmostEqual(largest[0]['absolute_error_rub'], .99)
        self.assertAlmostEqual(largest[0]['absolute_error_pct'], 49.5)
        self.assertNotIn('profit', largest[0])

    def test_empty_or_all_unknown_data_never_fabricate_zero_error_or_zero_profit(self):
        for rows in ([], [row(actual=None)]):
            with self.subTest(rows=rows):
                result = v.summarize(rows, constant_model())
                self.assertEqual(result['report']['samples'], 0)
                for name in ('mae_bps', 'rmse_bps', 'baseline_rmse_bps', 'mape_pct', 'mean_signed_error_bps'):
                    self.assertIsNone(result['report'][name])
                self.assertEqual(result['examples'], [])
                self.assertIsNone(result['report']['paper']['net_return_pct'])


class TemporalRunTests(SimpleTestCase):
    def test_three_train_calibration_fits_never_read_their_test_labels(self):
        rows = [row('2019-06-03'), row('2022-06-01'), row('2023-06-01'),
                row('2024-06-03'), row('2025-06-02'), row('2026-06-01')]
        captured = []

        def fake_fit(train, calibration):
            captured.append((copy.deepcopy(train), copy.deepcopy(calibration)))
            return constant_model(value=sum(item['y'] for item in train)/len(train))

        with patch.object(m, 'fit', side_effect=fake_fit):
            result = v.run_folds(list(reversed(rows)))
        self.assertEqual(len(captured), 3)
        self.assertEqual(len(result), 3)
        for fold, (train, calibration), output in zip(v.FOLDS, captured, result):
            expected = v.group_rows(rows, fold['periods'])
            self.assertEqual(train, expected['train'])
            self.assertEqual(calibration, expected['calibration'])
            self.assertLess(max(datetime.fromisoformat(item['label_end']) for item in train),
                            min(datetime.fromisoformat(item['at']) for item in calibration))
            self.assertEqual(output['trained_rows'], len(train))
            self.assertEqual(output['calibration_rows'], len(calibration))
            self.assertEqual(output['current_year_report']['samples'], 1)
            self.assertEqual(output['current_year_examples'][0]['as_of'], rows[-1]['at'])
        self.assertEqual([fold['id'] for fold in result], [fold['id'] for fold in v.FOLDS])

    def test_current_year_future_outcomes_cannot_change_any_fitted_model(self):
        rows = [row('2019-06-03'), row('2022-06-01'), row('2023-06-01'),
                row('2024-06-03'), row('2025-06-02'), row('2026-06-01')]

        def fake_fit(train, calibration):
            return constant_model(value=sum(item['y'] for item in train)/len(train),
                                  interval=sum(abs(item['y']) for item in calibration)/len(calibration))

        with patch.object(m, 'fit', side_effect=fake_fit):
            first = v.run_folds(rows)
            changed = copy.deepcopy(rows)
            changed[-1].update(y=500., actual_price=600.)
            second = v.run_folds(changed)
        self.assertEqual([item['model'] for item in first], [item['model'] for item in second])
        self.assertNotEqual(first[-1]['report']['mae_bps'], second[-1]['report']['mae_bps'])
