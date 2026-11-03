"""Synthetic causality, training and strict robustness-screen tests; no archive/network."""
import copy
import unittest
from datetime import date, timedelta
from unittest.mock import patch
import numpy as np

from . import overnight_decision_study as decision
from . import robust_overnight_models as study
from .test_overnight_decision_study import row, sample


class FixedEstimator:
    def __init__(self, **params):
        self.params = params
        self.n_iter_ = 200

    def fit(self, x, y, sample_weight):
        self.received_x = np.array(x, copy=True)
        self.received_y = np.array(y, copy=True)
        self.weights = np.array(sample_weight, copy=True)
        return self

    def predict(self, x):
        return np.full(len(x), .25)


class RobustModelsTests(unittest.TestCase):
    def test_equal_date_weights_keep_same_day_equities_together(self):
        rows = [row('SBER'), row('GAZP'), row('SBER', '2020-01-07')]
        weights = study.date_weights(rows)
        self.assertAlmostEqual(sum(weights[:2]), weights[2])
        self.assertAlmostEqual(weights.mean(), 1.)
        with self.assertRaises(ValueError): study.date_weights([])

    def test_clipping_keeps_rows_and_uses_training_quantiles_only(self):
        train, cal = sample('2019-01-01', 200), sample('2023-01-01', 60)
        train[0] = decision.enrich_rows([row('SBER', train[0]['at'][:10], actual=140.)])[0]
        original = copy.deepcopy(train)
        with patch('sklearn.neural_network.MLPRegressor', FixedEstimator):
            bundle = study.fit_variant(study.VARIANTS[1],train,cal)
        expected = np.quantile([r['net_y'] for r in train],[.025,.975])
        np.testing.assert_allclose(bundle['target_clip_bounds'], expected)
        self.assertGreater(bundle['clipped_train_targets'], 0)
        self.assertEqual(bundle['trained_rows'], len(train))
        self.assertEqual(train, original)
        for model,seed in zip(bundle['estimators'],study.SEEDS):
            self.assertEqual(model.params['random_state'],seed)
            self.assertFalse(model.params['early_stopping'])
            self.assertFalse(model.params['shuffle'])
            self.assertEqual(len(model.received_y),len(train))
            self.assertLessEqual(model.received_y.max(),expected[1])

    def test_calibration_extremes_do_not_change_scaling_or_training_targets(self):
        train, cal = sample('2019-01-01', 200), sample('2023-01-01', 60)
        changed = copy.deepcopy(cal)
        for r in changed:
            r['x'][0] = 1e6
            r['actual_price'] = 500.
            r['y'] = 400.
        changed = decision.enrich_rows(changed)
        with patch('sklearn.neural_network.MLPRegressor', FixedEstimator):
            a,b = [study.fit_variant(study.VARIANTS[1],train,c) for c in (cal,changed)]
        np.testing.assert_array_equal(a['scaler'].mean_,b['scaler'].mean_)
        self.assertEqual(a['target_clip_bounds'],b['target_clip_bounds'])
        np.testing.assert_array_equal(a['estimators'][0].received_y,b['estimators'][0].received_y)
        self.assertNotEqual(a['interval_abs_pct'],b['interval_abs_pct'])

    def test_real_huber_inference_uses_only_features_and_keeps_original_answers(self):
        train,cal = sample('2019-01-01',200),sample('2023-01-01',60)
        bundle = study.fit_variant(study.VARIANTS[2],train,cal)
        test=decision.enrich_rows([row(day='2026-01-05')])
        a=study.predict(bundle,test)
        b=study.predict(bundle,[{'x':test[0]['x']}])
        self.assertEqual(a,b)
        changed=decision.enrich_rows([row(day='2026-01-05',actual=50.)])
        self.assertEqual(study.predict(bundle,changed),a)
        self.assertTrue(all(np.isfinite(a)))
        self.assertIsNone(bundle['target_clip_bounds'])

    def test_unknown_selected_target_is_retained_and_disables_profit_screen(self):
        train,cal=sample('2019-01-01',200),sample('2023-01-01',60)
        with patch('sklearn.neural_network.MLPRegressor',FixedEstimator):
            bundle=study.fit_variant(study.VARIANTS[0],train,cal)
        test=decision.enrich_rows([row(day='2026-01-05',actual=None),row(day='2026-01-06')])
        scored=study.score(bundle,test)
        self.assertEqual(len(scored['predictions']),2)
        self.assertIsNone(scored['predictions'][0]['actual_net_pct'])
        for check in scored['robustness']:
            self.assertEqual(check['status'],'incomplete')
            self.assertIsNone(check['baseline']['report']['net_return_pct'])
        self.assertFalse(study.passes(scored['robustness']))

    def test_screen_requires_both_costs_and_survives_three_best_dates(self):
        start=date(2025,1,1)
        rows=[row(day=(start+timedelta(days=i)).isoformat(),actual=102.) for i in range(20)]
        checks=[study.robustness.review(rows,[True]*len(rows),m) for m in (1,2)]
        self.assertTrue(study.passes(checks))
        for mutate in (
            lambda v:v.pop(),
            lambda v:v[0]['baseline']['report'].update(selected_dates=19),
            lambda v:v[1]['drop_best_dates'][1]['report'].update(net_return_pct=-.01),
            lambda v:v[0]['baseline']['report'].update(net_return_pct=None),
        ):
            changed=copy.deepcopy(checks); mutate(changed)
            self.assertFalse(study.passes(changed))

    def test_overlap_insufficient_data_and_unregistered_variants_fail(self):
        train,cal=sample('2019-01-01',200),sample('2023-01-01',60)
        for spec,t,c in ((study.VARIANTS[0],train,train[:60]),
                         (study.VARIANTS[0],train[:30],cal),
                         ({**study.VARIANTS[0],'id':'new'},train,cal)):
            with self.assertRaises(ValueError): study.fit_variant(spec,t,c)

    def test_invalid_or_missing_feature_vector_fails_and_empty_inference_is_empty(self):
        self.assertEqual(study.predict({},[]),[])
        for values in ([0.], [float('nan')]*18):
            with self.assertRaises(ValueError): study.matrix([{'x':values}])
