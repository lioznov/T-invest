"""Synthetic train/calibration isolation; never opens market caches or fits real prices."""
import copy
import importlib.util
import math
import unittest
from datetime import date, timedelta
from statistics import median
from unittest.mock import patch

import numpy as np

from . import morning_model as m
from . import nonlinear_study as study


def examples(start, count):
    """Five independent tickers per synthetic date, with no borrowed market data."""
    day = date.fromisoformat(start)
    days = []
    while len(days) < math.ceil(count / 5):
        if day.weekday() < 4:
            days.append(day)
        day += timedelta(days=1)
    rows = []
    for index in range(count):
        ticker = m.TICKERS[index % 5]
        at = m.clock(days[index // 5], 17, 45)
        target = m.clock(at.date() + timedelta(days=1), 10)
        x = [math.sin(index * .31 + column * .13) for column in range(11)]
        x += [float(ticker == name) for name in m.TICKERS]
        y = .2 * x[0] - .1 * x[2] + .03 * (index % 7)
        rows.append({'ticker': ticker, 'at': at.isoformat(),
                     'label_end': (target + timedelta(minutes=15)).isoformat(),
                     'x': x, 'y': y})
    return rows


class RecordingEstimator:
    """Simple deterministic spy exposing exactly the rows supplied to fit."""
    def __init__(self, **params):
        self.params = params
        self.fit_count = 0

    def fit(self, x, y):
        self.fit_count += 1
        self.fit_x = np.asarray(x).copy()
        self.fit_y = np.asarray(y).copy()
        self.offset = float(np.mean(y))
        return self

    def predict(self, x):
        return np.asarray(x)[:, 0] * .1 + self.offset


@unittest.skipUnless(importlib.util.find_spec('sklearn'), 'Run in the isolated nonlinear research runtime')
class NonlinearTrainingTests(unittest.TestCase):
    def setUp(self):
        self.train = examples('2020-01-06', 210)
        self.calibration = examples('2021-01-04', 70)

    def test_ridge_control_matches_original_weights_predictions_and_interval(self):
        expected = m.fit(self.train, self.calibration)
        result = study.train_variant(study.VARIANTS[0], self.train, self.calibration)
        for field in ('center', 'scale', 'weights'):
            np.testing.assert_allclose(result['estimator'][field], expected[field], rtol=0, atol=0)
        expected_predictions = [m.predict(expected, row['x']) for row in self.calibration]
        np.testing.assert_allclose(result['predict'](self.calibration), expected_predictions, rtol=0, atol=0)
        self.assertEqual(result['interval_abs_pct'], expected['interval_abs_pct'])
        self.assertEqual(result['train_mean'], expected['train_mean_pct'])
        self.assertEqual(result['train_median'], median(row['y'] for row in self.train))

    def test_changing_calibration_answers_changes_interval_not_fitted_predictions(self):
        changed = copy.deepcopy(self.calibration)
        for row in changed:
            row['y'] += 100.
        with patch('sklearn.ensemble.HistGradientBoostingRegressor', RecordingEstimator), \
                patch('sklearn.ensemble.ExtraTreesRegressor', RecordingEstimator):
            for spec in study.VARIANTS:
                with self.subTest(model=spec['id']):
                    first = study.train_variant(spec, self.train, self.calibration)
                    second = study.train_variant(spec, self.train, changed)
                    np.testing.assert_allclose(first['predict'](self.calibration),
                                               second['predict'](self.calibration), rtol=0, atol=0)
                    self.assertGreater(second['interval_abs_pct'], first['interval_abs_pct'] + 90.)
                    self.assertEqual(first['train_mean'], second['train_mean'])
                    self.assertEqual(first['train_median'], second['train_median'])

    def test_nonlinear_fit_receives_only_known_training_labels_once(self):
        missing = copy.deepcopy(self.train[-1])
        missing.update(y=None, x=[1_000_000.] * len(m.FEATURES))
        train = self.train + [missing]
        original_train, original_cal = copy.deepcopy(train), copy.deepcopy(self.calibration)
        with patch('sklearn.ensemble.HistGradientBoostingRegressor', RecordingEstimator), \
                patch('sklearn.ensemble.ExtraTreesRegressor', RecordingEstimator):
            for spec in study.VARIANTS[1:]:
                with self.subTest(model=spec['id']):
                    result = study.train_variant(spec, train, self.calibration)
                    estimator = result['estimator']
                    self.assertEqual(estimator.fit_count, 1)
                    self.assertEqual(estimator.params, spec['params'])
                    np.testing.assert_array_equal(estimator.fit_x, [row['x'] for row in self.train])
                    np.testing.assert_array_equal(estimator.fit_y, [row['y'] for row in self.train])
                    self.assertEqual(result['train_n'], 210)
                    self.assertEqual(result['calibration_n'], 70)
        self.assertEqual(train, original_train)
        self.assertEqual(self.calibration, original_cal)

    def test_calibration_interval_is_the_fixed_absolute_residual_order_statistic(self):
        with patch('sklearn.ensemble.HistGradientBoostingRegressor', RecordingEstimator):
            result = study.train_variant(study.VARIANTS[1], self.train, self.calibration)
        errors = sorted(abs(row['y'] - prediction) for row, prediction in
                        zip(self.calibration, result['predict'](self.calibration)))
        rank = min(len(errors), math.ceil((len(errors) + 1) * .8))
        self.assertEqual(result['interval_abs_pct'], errors[rank - 1])

    def test_all_families_reject_overlapping_training_labels_and_calibration(self):
        train = copy.deepcopy(self.train)
        train[-1]['label_end'] = self.calibration[0]['at']
        for spec in study.VARIANTS:
            with self.subTest(model=spec['id']), self.assertRaises(ValueError):
                study.train_variant(spec, train, self.calibration)

    def test_all_families_require_enough_independent_dates(self):
        for collapse in ('train', 'calibration'):
            train, calibration = copy.deepcopy(self.train), copy.deepcopy(self.calibration)
            group = train if collapse == 'train' else calibration
            at, until = group[0]['at'], group[0]['label_end']
            for row in group:
                row.update(at=at, label_end=until)
            for spec in study.VARIANTS:
                with self.subTest(group=collapse, model=spec['id']), self.assertRaises(ValueError):
                    study.train_variant(spec, train, calibration)

    def test_unknown_answers_do_not_satisfy_minimum_sample_count(self):
        for group_name, known_count in (('train', 199), ('calibration', 49)):
            train, calibration = copy.deepcopy(self.train), copy.deepcopy(self.calibration)
            group = train if group_name == 'train' else calibration
            for row in group[known_count:]:
                row['y'] = None
            for spec in study.VARIANTS:
                with self.subTest(group=group_name, model=spec['id']), self.assertRaises(ValueError):
                    study.train_variant(spec, train, calibration)

    def test_nonfinite_and_wrong_width_features_are_rejected_before_estimator_fit(self):
        for field, value in (('x', [0.] * (len(m.FEATURES) - 1)),
                             ('x', [float('nan')] * len(m.FEATURES)), ('y', float('inf'))):
            train = copy.deepcopy(self.train)
            train[0][field] = value
            with patch('sklearn.ensemble.HistGradientBoostingRegressor') as factory:
                with self.subTest(field=field, value=str(value)[:30]), self.assertRaises(ValueError):
                    study.train_variant(study.VARIANTS[1], train, self.calibration)
                factory.assert_not_called()

    def test_short_or_nonfinite_calibration_predictions_are_rejected(self):
        for fault in ('short', 'nan', 'infinity'):
            class BadEstimator(RecordingEstimator):
                def predict(self, x):
                    predictions = super().predict(x)
                    if fault == 'short':
                        return predictions[:-1]
                    predictions[0] = float('nan') if fault == 'nan' else float('inf')
                    return predictions
            with patch('sklearn.ensemble.HistGradientBoostingRegressor', BadEstimator):
                with self.subTest(fault=fault), self.assertRaises(ValueError):
                    study.train_variant(study.VARIANTS[1], self.train, self.calibration)


class FrozenControlComparisonTests(unittest.TestCase):
    def setUp(self):
        self.hashes = {'synthetic-source.py': 'a' * 64}
        self.folds, old = [], []
        for index, spec in enumerate(study.temporal.FOLDS):
            paper = {'status': 'incomplete' if index == 0 else 'ok',
                     'selected_candidates': 5, 'trades': 2,
                     'unknown_trades': 1 if index == 0 else 0,
                     'stress_trades': 2, 'stress_unknown_trades': 1 if index == 0 else 0,
                     'net_return_pct': None if index == 0 else 1.25,
                     'stress_return_pct': None if index == 0 else .5}
            report = {'samples': 30, 'eligible_forecasts': 31, 'unknown_targets': 1,
                      'days': 7, 'mae_pct': .6, 'rmse_pct': .8, 'paper': paper}
            old_report = copy.deepcopy(report)
            old_report['mae_bps'] = old_report.pop('mae_pct') * 100
            old_report['rmse_bps'] = old_report.pop('rmse_pct') * 100
            self.folds.append({'id': spec['id'], 'variants': [
                {'id': variant['id'], 'primary': {'report': copy.deepcopy(report)},
                 'common_2026': {'report': copy.deepcopy(report)}} for variant in study.VARIANTS]})
            old.append({'id': spec['id'], 'report': copy.deepcopy(old_report),
                        'current_year_report': copy.deepcopy(old_report)})
        self.prior = {'folds': old, 'source_sha256': self.hashes}

    def compare(self, folds=None, prior=None):
        with patch.object(study, 'checked', return_value=self.prior if prior is None else prior), \
                patch.object(study.temporal, 'source_hashes', return_value=self.hashes):
            return study.compare_old_control(self.folds if folds is None else folds)

    def test_identical_primary_and_common_reports_pass_including_null_incomplete_return(self):
        result = self.compare()
        self.assertEqual(result, [{'fold': fold['id'], 'matched': True} for fold in self.folds])

    def test_truncated_reordered_or_missing_variants_cannot_pass_zip_comparison(self):
        changed_variants = copy.deepcopy(self.folds)
        changed_variants[0]['variants'].pop()
        for folds in (self.folds[:-1], list(reversed(self.folds)), changed_variants):
            with self.subTest(ids=[fold['id'] for fold in folds]), self.assertRaises(ValueError):
                self.compare(folds)
        prior = copy.deepcopy(self.prior)
        prior['folds'].pop()
        with self.assertRaises(ValueError):
            self.compare(prior=prior)

    def test_old_source_hash_mismatch_is_rejected(self):
        prior = copy.deepcopy(self.prior)
        prior['source_sha256'] = {'synthetic-source.py': 'b' * 64}
        with self.assertRaises(ValueError):
            self.compare(prior=prior)

    def test_common_year_and_primary_metric_or_execution_mismatches_are_rejected(self):
        for period in ('primary', 'common_2026'):
            mutations = [('samples', 29), ('eligible_forecasts', 32), ('unknown_targets', 2),
                         ('days', 8), ('mae_pct', .7), ('rmse_pct', .9),
                         ('paper.status', 'ok'), ('paper.selected_candidates', 6),
                         ('paper.trades', 3), ('paper.unknown_trades', 0),
                         ('paper.stress_trades', 3), ('paper.stress_unknown_trades', 0),
                         ('paper.net_return_pct', 0.), ('paper.stress_return_pct', 0.)]
            for field, value in mutations:
                folds = copy.deepcopy(self.folds)
                report = folds[0]['variants'][0][period]['report']
                if field.startswith('paper.'):
                    report['paper'][field.split('.')[1]] = value
                else:
                    report[field] = value
                with self.subTest(period=period, field=field), self.assertRaises(ValueError):
                    self.compare(folds)


if __name__ == '__main__':
    unittest.main()
