"""Synthetic causal-feature and fitting checks; no archived price data is read."""
import copy
import importlib.util
import math
import unittest
from datetime import date, timedelta, timezone
from unittest.mock import patch

import numpy as np

from . import morning_model as m
from . import overnight_decision_study as study


def row(ticker='SBER', day='2020-01-06', change=3., *, entry=100., actual=101.):
    at = m.clock(date.fromisoformat(day), 17, 45)
    target = m.clock(at.date() + timedelta(days=1), 10)
    return {'ticker': ticker, 'lot': 1, 'at': at.isoformat(),
            'target_at': target.isoformat(), 'label_end': (target + timedelta(minutes=15)).isoformat(),
            'anchor_price': 100., 'entry_price': entry, 'actual_price': actual,
            'y': (actual / 100. - 1) * 100 if actual is not None else None,
            'x': [change] + [0.] * 10 + [float(ticker == name) for name in m.TICKERS]}


def sample(start, count):
    day = date.fromisoformat(start)
    days = []
    while len(days) < math.ceil(count / 5):
        if day.weekday() <= 3:
            days.append(day)
        day += timedelta(days=1)
    result = []
    for index in range(count):
        item = row(m.TICKERS[index % 5], days[index // 5].isoformat(),
                   math.sin(index * .31), actual=101. if index % 2 else 99.)
        item['x'][1:11] = [math.cos(index * .11 + column * .2) for column in range(10)]
        result.append(item)
    return study.enrich_rows(result)


class CausalPeerTests(unittest.TestCase):
    def test_same_utc_instant_and_self_exclusion_give_exact_peer_features(self):
        rows = [row('SBER', change=3.), row('GAZP', change=-1.), row('LKOH', change=5.)]
        rows[1]['at'] = study.temporal._time(rows[1]['at']).astimezone(timezone.utc).isoformat()
        result = {item['ticker']: item for item in study.enrich_rows(rows)}
        np.testing.assert_allclose(result['SBER']['x_peer'][-5:], [2., 3., .5, 1., 2/6])
        np.testing.assert_allclose(result['GAZP']['x_peer'][-5:], [4., 1., 1., -5., 2/6])
        self.assertEqual(result['SBER']['x_peer'][:18], rows[0]['x'])

    def test_previous_or_later_peer_cannot_be_backfilled_into_cutoff(self):
        focus = row('SBER')
        prior = row('GAZP', '2020-01-03', 800.)
        later = row('LKOH', '2020-01-07', 900.)
        later_same_day = row('NVTK', change=1000.)
        later_same_day['at'] = (study.temporal._time(focus['at']) + timedelta(minutes=15)).isoformat()
        output = study.enrich_rows([focus, prior, later, later_same_day])
        self.assertEqual(output[0]['x_peer'][-5:], [0.] * 5)

    def test_unknown_peer_future_is_still_present_and_future_labels_do_not_change_x(self):
        rows = [row('SBER', change=3.), row('GAZP', change=-1.), row('LKOH', change=5.)]
        before = study.enrich_rows(rows)
        changed = copy.deepcopy(rows)
        changed[0].update(entry_price=500., actual_price=200., y=100.)
        changed[1].update(entry_price=None, actual_price=None, y=None)
        changed[2].update(entry_price=None)
        after = study.enrich_rows(changed)
        self.assertEqual([item['x_peer'] for item in before], [item['x_peer'] for item in after])
        self.assertEqual(after[0]['x_peer'][-1], 2/6)
        self.assertIsNone(after[1]['net_y'])
        self.assertIsNone(after[2]['net_y'])

    def test_no_peers_means_five_explicit_zeros_and_inputs_are_not_mutated(self):
        rows = [row()]
        original = copy.deepcopy(rows)
        result = study.enrich_rows(rows)
        self.assertEqual(rows, original)
        self.assertEqual(result[0]['x_peer'][-5:], [0.] * 5)
        self.assertEqual(len(result[0]['x_peer']), 23)

    def test_duplicate_ticker_at_same_instant_with_different_timezone_is_rejected(self):
        first = row()
        second = dict(first, at=study.temporal._time(first['at']).astimezone(timezone.utc).isoformat())
        with self.assertRaises(ValueError):
            study.enrich_rows([first, second])

    def test_net_label_includes_entry_cost_in_denominator_and_keeps_old_target(self):
        original = row(actual=101.)
        item = study.enrich_rows([original])[0]
        cash_paid = 100. * 1.0005 * 1.0005
        cash_received = 101. * .9995 * .9995
        self.assertAlmostEqual(item['net_y'], 100 * (cash_received / cash_paid - 1), places=12)
        self.assertEqual(item['profit_y'], 1)
        self.assertEqual(item['y'], original['y'])
        flat = study.enrich_rows([row(actual=100.)])[0]
        self.assertLess(flat['net_y'], 0.)
        self.assertEqual(flat['profit_y'], 0)

    def test_missing_entry_is_unknown_even_when_old_price_target_is_known(self):
        item = study.enrich_rows([row(entry=None)])[0]
        self.assertIsNotNone(item['y'])
        self.assertIsNone(item['net_y'])
        self.assertIsNone(item['profit_y'])
        item = study.enrich_rows([row(actual=None)])[0]
        self.assertIsNone(item['net_y'])
        self.assertIsNone(item['profit_y'])


class RecordingRegressor:
    def __init__(self, **params):
        self.params = params
        self.fit_count = 0

    def fit(self, x, y):
        self.fit_count += 1
        self.fit_x, self.fit_y = np.asarray(x).copy(), np.asarray(y).copy()
        self.offset = float(np.mean(y))
        return self

    def predict(self, x):
        return np.asarray(x)[:, 0] * .01 + self.offset


class RecordingClassifier(RecordingRegressor):
    def decision_function(self, x):
        return np.asarray(x)[:, 0] * .2 + self.offset


class RecordingCalibrator(RecordingRegressor):
    def predict_proba(self, x):
        score = np.asarray(x)[:, 0] * .3 + self.offset
        p = 1. / (1. + np.exp(-score))
        return np.column_stack((1. - p, p))


@unittest.skipUnless(importlib.util.find_spec('sklearn'), 'Run in isolated nonlinear research runtime')
class DecisionTrainingTests(unittest.TestCase):
    def setUp(self):
        self.train = sample('2020-01-06', 210)
        self.calibration = sample('2021-01-04', 70)
        self.patchers = [
            patch('sklearn.ensemble.HistGradientBoostingRegressor', RecordingRegressor),
            patch('sklearn.ensemble.HistGradientBoostingClassifier', RecordingClassifier),
            patch('sklearn.linear_model.LogisticRegression', RecordingCalibrator)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_base_weights_use_only_known_training_rows_and_calibrator_only_known_calibration(self):
        train, cal = copy.deepcopy(self.train), copy.deepcopy(self.calibration)
        train[0].update(entry_price=None, net_y=None, profit_y=None)
        cal[0].update(entry_price=None, net_y=None, profit_y=None)
        for spec in study.VARIANTS[1:]:
            with self.subTest(model=spec['id']):
                bundle = study.fit_variant(spec, train, cal)
                key = 'x_peer' if spec['features'] == 'peer' else 'x'
                for fitted in (bundle['regressor'], bundle['classifier']):
                    self.assertEqual(fitted.fit_count, 1)
                    np.testing.assert_array_equal(fitted.fit_x, [item[key] for item in train[1:]])
                np.testing.assert_array_equal(bundle['regressor'].fit_y, [item['net_y'] for item in train[1:]])
                np.testing.assert_array_equal(bundle['classifier'].fit_y, [item['profit_y'] for item in train[1:]])
                calibrator = bundle['calibrator']
                self.assertEqual(calibrator.fit_count, 1)
                self.assertEqual(calibrator.params, study.CAL_PARAMS)
                self.assertEqual(calibrator.fit_x.shape, (69, 1))
                scores = bundle['classifier'].decision_function(np.asarray([item[key] for item in cal[1:]]))
                np.testing.assert_array_equal(calibrator.fit_x[:, 0], scores)
                np.testing.assert_array_equal(calibrator.fit_y, [item['profit_y'] for item in cal[1:]])
                self.assertEqual(bundle['trained_rows'], 209)
                self.assertEqual(bundle['calibration_rows'], 69)

    def test_changed_calibration_outcomes_leave_regressor_and_classifier_weights_unchanged(self):
        changed_raw = [{key: value for key, value in item.items()
                        if key not in ('net_y', 'profit_y', 'x_peer')} for item in self.calibration]
        for item in changed_raw:
            item['actual_price'] = 99. if item['actual_price'] > 100. else 101.
            item['y'] = (item['actual_price'] / item['anchor_price'] - 1) * 100
        changed = study.enrich_rows(changed_raw)
        for spec in study.VARIANTS:
            with self.subTest(model=spec['id']):
                first = study.fit_variant(spec, self.train, self.calibration)
                second = study.fit_variant(spec, self.train, changed)
                if spec['kind'] == 'ridge':
                    for field in ('center', 'scale', 'weights'):
                        np.testing.assert_array_equal(first['regressor'][field], second['regressor'][field])
                else:
                    for name in ('regressor', 'classifier'):
                        np.testing.assert_array_equal(first[name].fit_x, second[name].fit_x)
                        np.testing.assert_array_equal(first[name].fit_y, second[name].fit_y)
                    self.assertFalse(np.array_equal(first['calibrator'].fit_y, second['calibrator'].fit_y))

    def test_inference_requires_features_only_and_returns_same_predictions_without_future_fields(self):
        for spec in study.VARIANTS:
            with self.subTest(model=spec['id']):
                bundle = study.fit_variant(spec, self.train, self.calibration)
                feature_only = [{'x': item['x'], 'x_peer': item['x_peer']} for item in self.calibration]
                self.assertEqual(study.predict(bundle, self.calibration), study.predict(bundle, feature_only))

    def test_ridge_uses_net_targets_and_train_only_baselines(self):
        bundle = study.fit_variant(study.VARIANTS[0], self.train, self.calibration)
        expected = m.fit([{**item, 'y': item['net_y']} for item in self.train],
                         [{**item, 'y': item['net_y']} for item in self.calibration])
        np.testing.assert_array_equal(bundle['regressor']['weights'], expected['weights'])
        self.assertAlmostEqual(bundle['train_net_mean'], np.mean([item['net_y'] for item in self.train]))
        self.assertAlmostEqual(bundle['train_net_median'], np.median([item['net_y'] for item in self.train]))
        self.assertIsNone(bundle['classifier'])
        self.assertIsNone(bundle['calibrator'])

    def test_equal_or_overlapping_information_boundary_is_rejected(self):
        train = copy.deepcopy(self.train)
        train[-1]['label_end'] = self.calibration[0]['at']
        for spec in study.VARIANTS:
            with self.subTest(model=spec['id']), self.assertRaises(ValueError):
                study.fit_variant(spec, train, self.calibration)

    def test_unknown_rows_do_not_count_toward_training_minimum(self):
        train = copy.deepcopy(self.train)
        for item in train[199:]:
            item.update(entry_price=None, net_y=None, profit_y=None)
        for spec in study.VARIANTS:
            with self.subTest(model=spec['id']), self.assertRaises(ValueError):
                study.fit_variant(spec, train, self.calibration)

    def test_single_class_calibration_is_rejected_for_probability_models(self):
        cal_raw = [{key: value for key, value in item.items()
                    if key not in ('net_y', 'profit_y', 'x_peer')} for item in self.calibration]
        for item in cal_raw:
            item.update(actual_price=101., y=(101. / 100. - 1) * 100)
        calibration = study.enrich_rows(cal_raw)
        for spec in study.VARIANTS[1:]:
            with self.subTest(model=spec['id']), self.assertRaises(ValueError):
                study.fit_variant(spec, self.train, calibration)


class OldControlReplayTests(unittest.TestCase):
    def setUp(self):
        self.rows = study.enrich_rows([row(entry=None), row(day='2020-01-07')])
        self.predictions = [.5, .5]
        normal = study.paper.paper_predictions(self.rows, self.predictions)
        stress = study.paper.paper_predictions(self.rows, self.predictions, multiplier=2)
        normal['stress_return_pct'] = stress['net_return_pct']
        self.prior = {'folds': [
            {'id': spec['id'], 'variants': [{'id': 'ridge_control', 'primary': {
                'report': {'paper': copy.deepcopy(normal), 'stress': copy.deepcopy(stress)},
                'predictions': [{'ticker': item['ticker'], 'at': item['at'],
                                 'predicted_change_pct': prediction}
                                for item, prediction in zip(self.rows, self.predictions)]}}]}
            for spec in study.temporal.FOLDS]}
        self.fold = study.temporal.FOLDS[0]

    def test_incomplete_replay_passes_only_with_matching_decisions_and_counts(self):
        result = study.old_control(self.prior, self.fold, self.rows)
        self.assertTrue(result['matched'])
        self.assertEqual(result['paper']['status'], 'incomplete')
        self.assertEqual(result['paper']['unknown_trades'], 1)
        self.assertIsNone(result['paper']['net_return_pct'])

    def test_equal_null_returns_do_not_hide_incorrect_candidate_or_unknown_counts(self):
        for side in ('paper', 'stress'):
            for key in ('trades', 'selected_candidates', 'selected_dates', 'unknown_trades'):
                prior = copy.deepcopy(self.prior)
                prior['folds'][0]['variants'][0]['primary']['report'][side][key] += 1
                with self.subTest(side=side, key=key), self.assertRaises(ValueError):
                    study.old_control(prior, self.fold, self.rows)

    def test_wrong_control_identity_fold_set_or_population_is_rejected(self):
        wrong_model = copy.deepcopy(self.prior)
        wrong_model['folds'][0]['variants'][0]['id'] = 'extra_trees'
        missing_fold = copy.deepcopy(self.prior)
        missing_fold['folds'].pop()
        missing_row = copy.deepcopy(self.prior)
        missing_row['folds'][0]['variants'][0]['primary']['predictions'].pop()
        for prior in (wrong_model, missing_fold, missing_row):
            with self.subTest(prior=prior), self.assertRaises(ValueError):
                study.old_control(prior, self.fold, self.rows)


if __name__ == '__main__':
    unittest.main()
