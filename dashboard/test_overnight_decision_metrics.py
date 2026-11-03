"""Deterministic synthetic diagnostics; stdlib unittest, no fitting or broker."""
import copy
import math
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from . import morning_model as morning
from . import overnight_decision_metrics as metrics
from .nonlinear_metrics import paper_predictions


def example(day=0, ticker='SBER', net=1., *, entry=100., unknown_exit=False):
    at = datetime(2026, 6, 1, 17, 45, tzinfo=morning.MSK) + timedelta(days=day)
    actual = (100. * (1 + net / 100) * (1.0005 ** 2) / (.9995 ** 2)
              if not unknown_exit else None)
    net_y = (100 * (actual * .9995 * .9995 / (entry * 1.0005 * 1.0005) - 1)
             if actual is not None and entry is not None else None)
    return {'ticker': ticker, 'lot': 1, 'at': at.isoformat(),
            'label_end': morning.clock(at.date() + timedelta(days=1), 10, 15).isoformat(),
            'anchor_price': 100., 'entry_price': entry, 'actual_price': actual,
            'y': actual - 100 if actual is not None else None,
            'net_y': net_y, 'profit_y': int(net_y > 0) if net_y is not None else None,
            'x': [0.] * len(morning.FEATURES)}


def score(rows, net_predictions=None, probabilities=None):
    return metrics.evaluate(rows, [0.] * len(rows) if net_predictions is None else net_predictions,
                            probabilities, train_net_mean=.2, train_profit_prior=.6,
                            calibration_profit_prior=.4)


class DecisionRegressionTests(unittest.TestCase):
    def test_optional_training_median_is_independent_baseline_without_changing_mean_bootstrap(self):
        rows = [example(0, net=1.), example(1, net=-2.), example(2, net=4.)]
        prior = metrics.evaluate(rows, [.5, 1., 2.], None, .2, .6, .4)
        result = metrics.evaluate(rows, [.5, 1., 2.], None, .2, .6, .4, train_net_median=-1.)
        regression = result['regression']
        self.assertEqual(regression['train_median_pct'], -1.)
        self.assertAlmostEqual(regression['median_baseline_mae_pct'], 8 / 3)
        self.assertAlmostEqual(regression['median_baseline_rmse_pct'], math.sqrt(10))
        self.assertEqual(result['mae_difference_bootstrap'], prior['mae_difference_bootstrap'])
        self.assertIsNone(prior['regression']['median_baseline_mae_pct'])
        with self.assertRaises(ValueError):
            metrics.evaluate(rows, [.5, 1., 2.], None, .2, .6, .4, train_net_median=float('nan'))

    def test_net_errors_compare_actual_entry_economics_with_fixed_baselines(self):
        rows = [example(0, net=1.), example(1, net=-2.), example(2, unknown_exit=True)]
        result = score(rows, [.5, 1., 9.])
        regression = result['regression']
        self.assertEqual((result['samples'], result['eligible_forecasts'], result['unknown_targets']), (2, 3, 1))
        self.assertAlmostEqual(regression['mae_pct'], 1.75)
        self.assertAlmostEqual(regression['rmse_pct'], math.sqrt((.5 ** 2 + 3 ** 2) / 2))
        self.assertAlmostEqual(regression['mean_baseline_mae_pct'], 1.5)
        self.assertAlmostEqual(regression['mean_baseline_rmse_pct'], math.sqrt((.8 ** 2 + 2.2 ** 2) / 2))
        flat = 100 * ((.9995 / 1.0005) ** 2 - 1)
        self.assertAlmostEqual(regression['flat_gross_return_pct'], flat)
        self.assertAlmostEqual(regression['flat_gross_baseline_mae_pct'], (abs(1 - flat) + abs(-2 - flat)) / 2)
        self.assertAlmostEqual(result['mae_difference_bootstrap']['difference'], .25)
        self.assertIsNone(result['classification'])
        self.assertIsNone(result['brier_difference_bootstrap'])
        self.assertIsNone(result['policies']['joint'])

    def test_missing_entry_with_known_anchor_target_stays_unknown_net_not_loss(self):
        rows = [example(0, entry=None), example(1, unknown_exit=True)]
        result = score(rows, [1., 1.], [.8, .8])
        self.assertEqual(result['samples'], 0)
        self.assertEqual(result['unknown_targets'], 2)
        self.assertIsNone(result['regression']['mae_pct'])
        self.assertIsNone(result['classification']['brier'])
        self.assertEqual(result['classification']['negative_labels'], 0)
        self.assertEqual(result['classification']['positive_labels'], 0)
        self.assertEqual(sum(item['count'] for item in result['classification']['reliability']), 0)
        for policy in result['policies'].values():
            self.assertEqual(policy['paper']['status'], 'incomplete')
            self.assertEqual(policy['paper']['unknown_trades'], 2)
            self.assertIsNone(policy['paper']['net_return_pct'])

    def test_empty_report_has_no_zero_error_or_fictitious_profit(self):
        result = score([], [], [])
        self.assertEqual(result['samples'], 0)
        self.assertIsNone(result['regression']['mae_pct'])
        self.assertIsNone(result['classification']['brier'])
        self.assertEqual(len(result['classification']['reliability']), 10)
        self.assertIsNone(result['policies']['regression']['paper']['net_return_pct'])


class DecisionClassificationTests(unittest.TestCase):
    def test_known_brier_logloss_auc_accuracy_and_priors(self):
        rows = [example(day, net=1. if day >= 2 else -1.) for day in range(4)]
        result = score(rows, [0.] * 4, [.1, .4, .35, .8])['classification']
        self.assertAlmostEqual(result['brier'], (.01 + .16 + .65 ** 2 + .04) / 4)
        self.assertAlmostEqual(result['log_loss'], -sum(map(math.log, (.9, .6, .35, .8))) / 4)
        self.assertAlmostEqual(result['auc'], .75)
        self.assertEqual(result['accuracy_pct'], 75.)
        self.assertEqual(result['positive_labels'], 2)
        for name, prior in (('train_baseline', .6), ('calibration_baseline', .4)):
            self.assertAlmostEqual(result[name]['brier'], .26)
            self.assertEqual(result[name]['probability'], prior)
            self.assertEqual(result[name]['auc'], .5)
            self.assertEqual(result[name]['accuracy_pct'], 50.)

    def test_auc_ties_are_half_credit_and_single_class_is_undefined(self):
        rows = [example(0, net=-1.), example(1, net=1.)]
        self.assertEqual(score(rows, [0., 0.], [.5, .5])['classification']['auc'], .5)
        only_positive = score([example(day, net=1.) for day in range(3)], [0.] * 3, [.1, .5, .9])
        self.assertIsNone(only_positive['classification']['auc'])

    def test_exact_probability_endpoints_have_finite_logloss_and_fixed_ten_bins(self):
        rows = [example(0, net=-1.), example(1, net=1.), example(2, net=-1.), example(3, net=1.)]
        output = score(rows, [0.] * 4, [0., 1., .1, .9])['classification']
        self.assertTrue(math.isfinite(output['log_loss']))
        self.assertEqual(len(output['reliability']), 10)
        self.assertEqual([item['count'] for item in output['reliability']], [1, 1, 0, 0, 0, 0, 0, 0, 0, 2])
        self.assertEqual(output['reliability'][0]['mean_probability'], 0.)
        self.assertEqual(output['reliability'][9]['mean_probability'], .95)
        self.assertEqual(output['reliability'][9]['observed_profit_rate'], 1.)
        self.assertIsNone(output['reliability'][2]['observed_profit_rate'])
        wrong = score(rows[:2], [0., 0.], [1., 0.])['classification']
        self.assertTrue(math.isfinite(wrong['log_loss']))
        self.assertGreater(wrong['log_loss'], 30.)


class DecisionPolicyTests(unittest.TestCase):
    def test_fixed_thresholds_and_stress_use_identical_selected_candidates(self):
        rows = [example(day) for day in range(4)]
        predictions, probabilities = [.1, .10001, 1., 1.], [.9, .55, .549, .8]
        result = score(rows, predictions, probabilities)
        regression = [0., 1., 1., 1.]
        joint = [0., 1., 0., 1.]
        for name, encoded in (('regression', regression), ('joint', joint)):
            policy = result['policies'][name]
            self.assertEqual(policy['paper'], paper_predictions(rows, encoded, threshold=.5))
            self.assertEqual(policy['stress'], paper_predictions(rows, encoded, multiplier=2, threshold=.5))
            self.assertEqual(policy['paper']['selected_candidates'], policy['stress']['selected_candidates'])
            self.assertLess(policy['stress']['net_return_pct'], policy['paper']['net_return_pct'])

    def test_unknown_selected_outcome_halts_curve_and_future_labels_never_select_entries(self):
        first = [example(0), example(1, unknown_exit=True), example(2, net=-5.)]
        changed = [example(0, net=-10.), example(1, net=30.), example(2, net=50.)]
        predictions, probabilities = [1., 1., 0.], [.8, .8, .9]
        one, two = score(first, predictions, probabilities), score(changed, predictions, probabilities)
        for policy in ('regression', 'joint'):
            initial, altered = one['policies'][policy]['paper'], two['policies'][policy]['paper']
            self.assertEqual(initial['selected_candidates'], altered['selected_candidates'])
            self.assertEqual(initial['selected_dates'], altered['selected_dates'])
            self.assertEqual(initial['status'], 'incomplete')
            self.assertIsNone(initial['net_return_pct'])
            self.assertEqual(altered['status'], 'ok')

    def test_always_buy_is_separate_and_preserves_unknown_candidates(self):
        rows = [example(), example(1, entry=None)]
        result = metrics.always_buy(rows)
        self.assertEqual(result['paper'], paper_predictions(rows, [1., 1.], threshold=.5))
        self.assertEqual(result['paper']['status'], 'incomplete')
        self.assertIsNone(result['stress']['net_return_pct'])


class DecisionValidationTests(unittest.TestCase):
    def test_public_row_validator_accepts_generators_and_checks_missing_labels(self):
        rows = [example(), example(1, entry=None)]
        self.assertEqual(metrics.validate_rows(row for row in rows), rows)
        with self.assertRaises(ValueError):
            metrics.validate_rows([dict(rows[1], profit_y=0)])

    def test_probabilities_and_predictions_are_checked_even_for_unknown_rows(self):
        for predictions, probabilities in (([], [.5]), ([1., 2.], [.5]), ([1.], []),
                                           ([float('nan')], [.5]), ([1.], [float('inf')]),
                                           ([1.], [-.001]), ([1.], [1.001]), ([True], [.5])):
            with self.subTest(predictions=predictions, probabilities=probabilities), self.assertRaises(ValueError):
                score([example(unknown_exit=True)], predictions, probabilities)

    def test_net_and_profit_labels_must_match_prices_including_unknown_entries(self):
        good = example()
        invalid = [dict(good, net_y=10.), dict(good, profit_y=0), dict(good, profit_y=None),
                   dict(good, net_y=None), dict(good, profit_y='1'),
                   dict(example(unknown_exit=True), profit_y=0),
                   dict(example(entry=None), net_y=-1.)]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                score([row], [1.], [.8])

    def test_original_price_time_and_duplicate_validation_remains_active(self):
        base = example()
        for row in (dict(base, y=100.), dict(base, lot=0), dict(base, at='2026-06-01T17:45:00')):
            with self.subTest(row=row), self.assertRaises(ValueError):
                score([row], [0.], [.5])
        duplicate = dict(base, at=datetime.fromisoformat(base['at']).astimezone(timezone.utc).isoformat())
        with self.assertRaises(ValueError):
            score([base, duplicate], [0., 0.], [.5, .5])

    def test_baselines_cannot_be_nonfinite_or_invalid_probabilities(self):
        for net_mean, train_prior, calibration_prior in ((float('nan'), .5, .5), (0., -.1, .5), (0., .5, 1.1)):
            with self.assertRaises(ValueError):
                metrics.evaluate([example()], [0.], [.5], net_mean, train_prior, calibration_prior)


class DecisionBootstrapTests(unittest.TestCase):
    def test_timezone_offsets_do_not_split_one_moscow_date_into_two_groups(self):
        rows = []
        for day in range(10):
            one, two = example(day, 'SBER'), example(day, 'GAZP')
            early = datetime.fromisoformat(one['at']).replace(hour=0, minute=30)
            one['at'] = early.isoformat()
            two['at'] = early.astimezone(timezone.utc).isoformat()
            rows += [one, two]
        result = score(rows, [1.] * 20, [.8] * 20)
        self.assertEqual(result['days'], 10)
        self.assertEqual(result['mae_difference_bootstrap']['dates'], 10)
        self.assertEqual(result['brier_difference_bootstrap']['dates'], 10)

    def test_paired_mae_and_brier_bootstrap_cluster_stocks_on_each_date(self):
        rows, preds, probs = [], [], []
        for day in range(12):
            rows += [example(day, 'SBER', net=1.), example(day, 'GAZP', net=1.)]
            preds += [1., 2.]
            probs += [.8, .4]
        result = metrics.evaluate(rows, preds, probs, .5, .5, .5)
        mae, brier = result['mae_difference_bootstrap'], result['brier_difference_bootstrap']
        self.assertAlmostEqual(mae['difference'], 0., places=12)
        self.assertAlmostEqual(mae['lower'], mae['upper'], places=12)
        # Brier differences -.21 and +.11 stay paired, so every date is -.05.
        self.assertAlmostEqual(brier['difference'], -.05)
        self.assertAlmostEqual(brier['lower'], -.05)
        self.assertAlmostEqual(brier['upper'], -.05)
        self.assertEqual(brier['dates'], 12)
        self.assertEqual(brier['samples'], 24)

    def test_moving_blocks_resample_five_consecutive_dates_not_individual_rows(self):
        rows = [example(day, net=1.) for day in range(10)]
        with patch.object(metrics.random, 'Random') as rng:
            rng.return_value.randrange.side_effect = [0, 0, 5, 5] * 500
            result = metrics.evaluate(rows, [1.] * 5 + [3.] * 5, None, 0., .5, .5)
            self.assertEqual(rng.return_value.randrange.call_count, 2000)
            self.assertTrue(all(call.args == (6,) for call in rng.return_value.randrange.call_args_list))
        bootstrap = result['mae_difference_bootstrap']
        self.assertAlmostEqual(bootstrap['lower'], -1.)
        self.assertAlmostEqual(bootstrap['upper'], 1.)

    def test_deterministic_bootstrap_omits_unknowns_and_does_not_mutate_input(self):
        rows = [example(day, net=1. if day % 2 else -1.) for day in range(12)] + [example(12, unknown_exit=True)]
        preds = [day / 10 for day in range(13)]
        probs = [.3 + (day % 4) / 10 for day in range(13)]
        original = copy.deepcopy(rows)
        first = score(rows, preds, probs)
        second = score(list(reversed(rows)), list(reversed(preds)), list(reversed(probs)))
        self.assertEqual(first['mae_difference_bootstrap'], second['mae_difference_bootstrap'])
        self.assertEqual(first['brier_difference_bootstrap'], second['brier_difference_bootstrap'])
        self.assertEqual(first['brier_difference_bootstrap']['samples'], 12)
        self.assertEqual(rows, original)
        small = score(rows[:9], preds[:9], probs[:9])
        self.assertEqual(small['mae_difference_bootstrap']['status'], 'insufficient_dates')
        self.assertIsNone(small['brier_difference_bootstrap']['lower'])


if __name__ == '__main__':
    unittest.main()
