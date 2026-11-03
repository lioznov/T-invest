"""Cross-check inference and ensure the independent audit rejects corrupted train state."""
import copy
import unittest
from unittest.mock import patch

import numpy as np

from . import conditional_payoff_study as study
from . import verify_conditional_payoff as audit
from .test_conditional_payoff_study import point
from .test_risk_distribution_study import examples


class ConditionalAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.train, cls.cal = examples('2019-01-01', 200), examples('2023-01-01', 60)
        cls.frozen = study.old.fit_family('payoff', cls.train, cls.cal)
        cls.bundle = study.fit_bundle(cls.train, cls.cal, cls.frozen)

    def test_manual_inference_matches_real_feature_only_api_without_predict_proba(self):
        rows = [{k: r[k] for k in ('u', 'market', 'risk_scale', 'ticker')} for r in self.cal]
        expected = study.predict(self.bundle, self.frozen, rows)
        with patch('sklearn.linear_model.LogisticRegression.predict_proba', side_effect=AssertionError('manual only')):
            actual = audit.inference(self.bundle, self.frozen, rows)
        np.testing.assert_allclose(study.numeric_outputs(actual), study.numeric_outputs(expected), rtol=0., atol=1e-12)
        self.assertEqual([p['ordinal_crossed'] for p in actual], [p['ordinal_crossed'] for p in expected])

    def test_independent_train_state_rejects_changed_support_map_and_ridge_weights(self):
        audit.train_state(self.bundle, self.frozen, self.train, self.cal)
        for change in ('stock_maps', 'support_bounds', 'ridge_heads'):
            bad = copy.deepcopy(self.bundle)
            if change == 'ridge_heads':bad[change][0].coef_[0] += .1
            else:bad[change][0][0] += .1
            with self.assertRaises(AssertionError):audit.train_state(bad, self.frozen, self.train, self.cal)

    def test_independent18_rules_match_boundaries_and_do_not_read_future_outcomes(self):
        points = [point(), point(old_probs=[.2, .4, .4]), point(ordinal_probs=[.31, .29, .4])]
        points[1]['scores']['ridge'] = .1
        for name in study.POLICIES:
            self.assertEqual(audit.selections(name, points, .4), study.decisions(name, points, .4))
            changed = [{**p, 'actual_net_pct': 100., 'entry_price': None} for p in points]
            self.assertEqual(audit.selections(name, changed, .4), audit.selections(name, points, .4))
