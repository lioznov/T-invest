"""Synthetic quarter boundaries, information cutoffs and aggregate accounting."""
import copy
import unittest
from datetime import date, timedelta
from statistics import mean
from unittest.mock import patch

from . import morning_model as m
from . import overnight_decision_study as decision
from . import quarterly_overnight_study as study


def row(day, *, entry=100., actual=101., ticker='SBER'):
    at = m.clock(date.fromisoformat(day), 17, 45)
    target = m.clock(at.date()+timedelta(days=1), 10)
    original = {'ticker': ticker, 'lot': 1, 'at': at.isoformat(), 'target_at': target.isoformat(),
        'label_end': (target+timedelta(minutes=15)).isoformat(), 'anchor_price': 100.,
        'entry_price': entry, 'actual_price': actual, 'y': (actual/100-1)*100 if actual is not None else None,
        'x': [0.] * 11 + [float(ticker == name) for name in m.TICKERS]}
    return decision.enrich_rows([original])[0]


def timeline():
    return [row(f'{year}-{month:02d}-08') for year in range(2019, 2027) for month in (1, 4, 7, 10)]


def bundle(train, calibration, *, number=0):
    return {'spec': study.MODEL_SPEC, 'regressor': None, 'classifier': None, 'calibrator': None,
            'train_net_mean': number*.1, 'train_net_median': number*.05,
            'train_profit_prior': .2+number*.05, 'calibration_profit_prior': .3+number*.05,
            'interval_abs_pct': .5+number*.1, 'trained_rows': sum(r['net_y'] is not None for r in train),
            'calibration_rows': sum(r['net_y'] is not None for r in calibration)}


def predictions(fitted, rows):
    return [.2] * len(rows), [.6] * len(rows)


class QuarterBoundaryTests(unittest.TestCase):
    def test_calendar_windows_are_24_or_48_months_then_three_month_calibration(self):
        period = study.temporal.FOLDS[0]['periods']['test']
        short, long = (study.quarters(period, months) for months in (24, 48))
        self.assertEqual(short[0]['train'], {'start': '2021-10-01', 'end_exclusive': '2023-10-01'})
        self.assertEqual(long[0]['train'], {'start': '2019-10-01', 'end_exclusive': '2023-10-01'})
        self.assertEqual(short[0]['calibration'], {'start': '2023-10-01', 'end_exclusive': '2024-01-01'})
        self.assertEqual(short[1]['train'], {'start': '2022-01-01', 'end_exclusive': '2024-01-01'})
        self.assertEqual(short[1]['calibration'], {'start': '2024-01-01', 'end_exclusive': '2024-04-01'})
        for a, b in zip(short, long):
            self.assertEqual(a['test'], b['test'])
            self.assertEqual(a['calibration'], b['calibration'])

    def test_last_2026_quarter_stops_at_original_partial_year_boundary(self):
        quarters = study.quarters(study.temporal.FOLDS[-1]['periods']['test'], 24)
        self.assertEqual(len(quarters), 4)
        self.assertEqual(quarters[-1]['test'], {'start': '2026-10-01', 'end_exclusive': '2026-10-23'})
        self.assertEqual(quarters[-1]['calibration'], {'start': '2026-07-01', 'end_exclusive': '2026-10-01'})

    def test_cross_quarter_forward_label_stays_with_its_forecast_quarter(self):
        period = study.temporal.FOLDS[1]['periods']['test']
        crossing, later = row('2025-03-31'), row('2025-04-01')
        outer = study.temporal.select_rows([crossing, later], period)
        quarters = study.quarters(period, 24)
        self.assertEqual(study.quarter_rows(outer, quarters[0]), [crossing])
        self.assertEqual(study.quarter_rows(outer, quarters[1]), [later])
        self.assertGreater(study.temporal._time(crossing['label_end']),
                           study.temporal._time(quarters[1]['origin']))

    def test_training_and_calibration_purge_boundary_labels_but_keep_unknowns(self):
        quarter = study.quarters(study.temporal.FOLDS[1]['periods']['test'], 24)[1]
        start = row('2023-01-01')
        train_good = row('2024-12-30', entry=None)
        train_cross = row('2024-12-31')
        cal_good = row('2025-03-27')
        cal_cross = row('2025-03-31')
        future = row('2025-04-01')
        groups = study.training_groups([start, train_good, train_cross, cal_good, cal_cross, future], quarter)
        self.assertEqual(groups['train'], [start, train_good])
        self.assertEqual(groups['calibration'], [cal_good])
        self.assertIsNone(groups['train'][-1]['net_y'])

    def test_labels_equal_to_information_boundary_are_excluded(self):
        quarter = study.quarters(study.temporal.FOLDS[1]['periods']['test'], 24)[1]
        train = dict(row('2024-12-30'), label_end='2025-01-01T00:00:00+03:00')
        cal = dict(row('2025-03-30'), label_end=quarter['origin'])
        groups = study.training_groups([train, cal], quarter)
        self.assertEqual(groups, {'train': [], 'calibration': []})

    def test_partition_has_no_holes_or_duplicates_and_does_not_drop_unknown_test(self):
        rows = timeline()+[row('2025-03-31', actual=None), row('2026-10-22')]
        for fold in study.temporal.FOLDS:
            outer = study.temporal.select_rows(rows, fold['periods']['test'])
            for months in (24, 48):
                parts = [item for quarter in study.quarters(fold['periods']['test'], months)
                         for item in study.quarter_rows(outer, quarter)]
                self.assertEqual([study.key(item) for item in parts], [study.key(item) for item in outer])
                if fold['id'] == 'test_2025':
                    self.assertEqual(sum(item['net_y'] is None for item in parts), 1)
                if fold['id'] == 'test_2026':
                    self.assertNotIn('2026-10-22', [item['at'][:10] for item in parts])


class QuarterlyExecutionTests(unittest.TestCase):
    def run_synthetic(self, rows, fold=None, window=None, changing_baselines=False):
        calls = []
        def fit(spec, train, calibration):
            self.assertEqual(spec, study.MODEL_SPEC)
            calls.append((copy.deepcopy(train), copy.deepcopy(calibration)))
            return bundle(train, calibration, number=len(calls)-1 if changing_baselines else 0)
        with patch.object(study.decision, 'fit_variant', side_effect=fit), \
                patch.object(study.decision, 'predict', side_effect=predictions):
            result = study.run_window(rows, fold or study.temporal.FOLDS[1], window or study.WINDOWS[0])
        return result, calls

    def test_exact_registered_budget_is_24_bundles_without_any_extra_fit_for_scoring(self):
        rows = timeline()
        total = 0
        for fold in study.temporal.FOLDS:
            for window in study.WINDOWS:
                result, calls = self.run_synthetic(rows, fold, window)
                self.assertEqual(len(calls), 4)
                total += len(calls)
                self.assertEqual(len(result['predictions']), 4)
                for quarter, (train, cal) in zip(result['quarters'], calls):
                    origin = study.temporal._time(quarter['origin'])
                    cal_start = study.temporal._bounds(quarter['calibration'])[0]
                    self.assertTrue(all(study.temporal._time(item['label_end']) < cal_start for item in train))
                    self.assertTrue(all(study.temporal._time(item['label_end']) < origin for item in cal))
        self.assertEqual(total, 24)

    def test_later_quarter_outcomes_never_change_an_earlier_fit_input(self):
        rows = timeline()
        _, before = self.run_synthetic(rows)
        changed = copy.deepcopy(rows)
        for index, item in enumerate(changed):
            if item['at'][:10] >= '2025-04-01':
                changed[index] = row(item['at'][:10], actual=90.)
        _, after = self.run_synthetic(changed)
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
        self.assertNotEqual(before[2], after[2])  # Q2's outcomes become legal past data for Q3.

    def test_full_year_incomplete_account_is_not_reset_after_first_quarter_unknown(self):
        rows = timeline()
        rows = [row('2025-01-08', entry=None) if item['at'][:10] == '2025-01-08' else item for item in rows]
        result, _ = self.run_synthetic(rows)
        annual = result['report']['policies']['joint']['paper']
        self.assertEqual(annual['status'], 'incomplete')
        self.assertEqual(annual['selected_candidates'], 4)
        self.assertEqual(annual['unknown_trades'], 1)
        self.assertIsNone(annual['net_return_pct'])
        self.assertEqual(result['quarters'][1]['report']['policies']['joint']['paper']['status'], 'ok')

    def test_january_and_adaptive_baselines_use_only_their_own_available_statistics(self):
        result, _ = self.run_synthetic(timeline(), changing_baselines=True)
        self.assertEqual(result['baseline_reference']['quarter'], '2025Q1')
        self.assertEqual(result['report']['regression']['train_mean_pct'], 0.)
        actual = [item['actual_net_pct'] for item in result['predictions']]
        expected_mean_mae = mean(abs(value-index*.1) for index, value in enumerate(actual))
        expected_median_mae = mean(abs(value-index*.05) for index, value in enumerate(actual))
        adaptive = result['report']['adaptive_baselines']
        self.assertAlmostEqual(adaptive['train_mean']['mae_pct'], expected_mean_mae)
        self.assertAlmostEqual(adaptive['train_median']['mae_pct'], expected_median_mae)
        expected_brier = mean((.3+index*.05-1)**2 for index in range(4))
        self.assertAlmostEqual(adaptive['calibration_prior']['brier'], expected_brier)
        self.assertAlmostEqual(adaptive['mae_difference_bootstrap']['difference'],
                               result['report']['regression']['mae_pct']-expected_mean_mae)
        self.assertAlmostEqual(adaptive['brier_difference_bootstrap']['difference'],
                               result['report']['classification']['brier']-expected_brier)

    def test_aggregate_rejects_missing_or_duplicate_forecasts(self):
        rows = timeline()
        result, _ = self.run_synthetic(rows)
        outer = study.temporal.select_rows(rows, study.temporal.FOLDS[1]['periods']['test'])
        for duplicate in (True, False):
            quarters = copy.deepcopy(result['quarters'])
            if duplicate:
                quarters[0]['predictions'].append(quarters[0]['predictions'][0])
            else:
                quarters[0]['predictions'].pop()
            with self.subTest(duplicate=duplicate), self.assertRaises(ValueError):
                study.aggregate(outer, quarters)

    def test_unregistered_window_or_missing_quarter_population_fails_before_training(self):
        with self.assertRaises(ValueError):
            study.quarters(study.temporal.FOLDS[0]['periods']['test'], 36)
        with patch.object(study.decision, 'fit_variant') as fit:
            with self.assertRaises(ValueError):
                study.run_window([], study.temporal.FOLDS[0], study.WINDOWS[0])
            fit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
