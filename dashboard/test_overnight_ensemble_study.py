"""Synthetic ensemble alignment and unknown-result checks; no archive reading."""
import copy
import unittest

from . import overnight_decision_study as decision
from . import overnight_ensemble_study as ensemble
from .test_overnight_decision_study import row


def variants(rows, predictions):
    return [{**spec, 'train_net_mean': -.1, 'train_net_median': -.12,
             'train_profit_prior': .4, 'calibration_profit_prior': .42,
             'predictions': [{'ticker': r['ticker'], 'at': r['at'], 'actual_net_pct': r['net_y'],
                              'predicted_net_pct': p} for r, p in zip(rows, values)]}
            for spec, values in zip(decision.VARIANTS, predictions)]


class EnsembleTests(unittest.TestCase):
    def test_key_alignment_is_independent_of_prediction_order(self):
        rows = decision.enrich_rows([row(day='2025-01-06'), row(day='2025-01-07')])
        models = variants(rows, [[.2, -.2], [.4, -.4], [.6, -.6]])
        expected = ensemble.combine(rows, models)
        models[1]['predictions'].reverse()
        self.assertEqual(ensemble.combine(rows, models), expected)
        self.assertAlmostEqual(expected['predictions'][0]['mean_net_pct'], .4)

    def test_strict_threshold_and_unanimity_distinguish_an_outlier(self):
        rows = decision.enrich_rows([row()])
        result = ensemble.combine(rows, variants(rows, [[.1], [.2], [2.]]))
        p = result['predictions'][0]
        self.assertTrue(p['selected_mean']); self.assertTrue(p['selected_median'])
        self.assertFalse(p['selected_unanimous'])
        self.assertEqual(p['median_net_pct'], .2)

    def test_selected_unknown_poisons_later_account(self):
        rows = decision.enrich_rows([row(day='2025-01-06', actual=None), row(day='2025-01-07')])
        result = ensemble.combine(rows, variants(rows, [[.3, .3]] * 3))
        for report in [r['policies']['regression'] for r in result['reports'].values()] + [result['unanimous']]:
            self.assertEqual(report['paper']['status'], 'incomplete')
            self.assertIsNone(report['paper']['net_return_pct'])
            self.assertEqual(report['paper']['trades'], 0)
            self.assertEqual(report['paper']['selected_candidates'], report['stress']['selected_candidates'])

    def test_changed_future_outcomes_do_not_change_selection(self):
        a = decision.enrich_rows([row(actual=101.)]); b = decision.enrich_rows([row(actual=95.)])
        first = ensemble.combine(a, variants(a, [[.2], [.3], [-.2]]))['predictions'][0]
        second = ensemble.combine(b, variants(b, [[.2], [.3], [-.2]]))['predictions'][0]
        for key in ('selected_mean', 'selected_median', 'selected_unanimous'):
            self.assertEqual(first[key], second[key])

    def test_mismatched_population_labels_or_training_baseline_fail(self):
        rows = decision.enrich_rows([row()]); original = variants(rows, [[.2]] * 3)
        for mutate in (
            lambda v: v[1]['predictions'].clear(),
            lambda v: v[1]['predictions'][0].update(actual_net_pct=999),
            lambda v: v[1].update(train_net_mean=.5),
            lambda v: v[1]['predictions'][0].update(predicted_net_pct=float('nan')),
            lambda v: v[1]['predictions'].append(copy.deepcopy(v[1]['predictions'][0])),
        ):
            bad = copy.deepcopy(original); mutate(bad)
            with self.assertRaises(ValueError):
                ensemble.combine(rows, bad)
