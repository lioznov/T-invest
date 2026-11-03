"""Synthetic economic/metric checks, without broker requests or model fitting."""
import copy
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.test import SimpleTestCase

from . import morning_model as m
from . import nonlinear_metrics as metrics


def example(day=0, ticker='SBER', y=1., *, anchor=100., entry=100., lot=1):
    at = datetime(2026, 6, 1, 17, 45, tzinfo=m.MSK) + timedelta(days=day)
    target = m.clock(at.date() + timedelta(days=1), 10)
    return {'ticker': ticker, 'lot': lot, 'at': at.isoformat(), 'target_at': target.isoformat(),
            'label_end': (target + timedelta(minutes=15)).isoformat(), 'anchor_price': anchor,
            'entry_price': entry, 'actual_price': anchor * (1 + y / 100) if y is not None else None,
            'y': y, 'x': [0.] * len(m.FEATURES)}


def model():
    return {'center': [0.] * len(m.FEATURES), 'scale': [1.] * len(m.FEATURES),
            'weights': [.5, .3] + [0.] * (len(m.FEATURES) - 1),
            'train_mean_pct': .2, 'interval_abs_pct': .8}


class PaperPredictionTests(SimpleTestCase):
    def test_exact_frozen_paper_parity_with_different_predictions_lots_and_costs(self):
        rows = []
        for day in range(8):
            for index, ticker in enumerate(m.TICKERS):
                row = example(day, ticker, y=(day - index) / 5,
                              anchor=100. + index * 20, entry=100. + index * 20 + day / 5,
                              lot=(index + 1) * 10)
                row['x'][0] = (index + day) % 5 - 2
                rows.append(row)
        frozen_model = model()
        predictions = [m.predict(frozen_model, row['x']) for row in rows]
        for multiplier in (0, 1, 2):
            with self.subTest(multiplier=multiplier):
                expected = m.paper(rows, frozen_model, multiplier=multiplier)
                actual = metrics.paper_predictions(rows, predictions, multiplier=multiplier)
                self.assertEqual({key: actual[key] for key in expected}, expected)
                self.assertEqual(actual['selected_dates'], 8)

    def test_unknown_selected_outcome_poisons_entire_curve_without_dropping_future_candidates(self):
        rows = [example(0), example(1, y=None), example(2), example(3, entry=None)]
        output = metrics.paper_predictions(rows, [1.] * 4)
        self.assertEqual(output['selected_candidates'], 4)
        self.assertEqual(output['selected_dates'], 4)
        self.assertEqual(output['trades'], 1)
        self.assertEqual(output['unknown_trades'], 2)
        self.assertEqual(output['status'], 'incomplete')
        self.assertIsNone(output['net_return_pct'])
        self.assertIsNone(output['max_drawdown_pct'])
        frozen_model = model()
        expected = m.paper(rows, frozen_model)
        actual = metrics.paper_predictions(rows, [m.predict(frozen_model, row['x']) for row in rows])
        self.assertEqual({key: actual[key] for key in expected}, expected)

    def test_unselected_unknown_does_not_poison_account(self):
        output = metrics.paper_predictions([example(), example(1, y=None)], [1., 0.])
        self.assertEqual(output['status'], 'ok')
        self.assertEqual(output['unknown_trades'], 0)
        self.assertEqual(output['trades'], 1)
        self.assertIsNotNone(output['net_return_pct'])

    def test_no_candidates_or_unaffordable_lots_have_no_profit_result(self):
        for rows, predictions in (([], []), ([example()], [.4]),
                                  ([example(anchor=1e6, entry=1e6, lot=100)], [1.])):
            with self.subTest(rows=rows):
                result = metrics.paper_predictions(rows, predictions)
                self.assertEqual(result['trades'], 0)
                self.assertIsNone(result['net_return_pct'])
                self.assertIsNone(result['max_drawdown_pct'])

    def test_target_labels_cannot_change_entry_candidates(self):
        first = [example(0, y=-10.), example(1, 'GAZP', y=20.), example(2, y=None)]
        changed = [example(0, y=40.), example(1, 'GAZP', y=-20.), example(2, y=5.)]
        prediction = [.5, -.5, 1.]
        one = metrics.paper_predictions(first, prediction)
        two = metrics.paper_predictions(changed, prediction)
        self.assertEqual(one['selected_candidates'], two['selected_candidates'])
        self.assertEqual(one['selected_dates'], two['selected_dates'])
        self.assertEqual(one['status'], 'incomplete')
        self.assertEqual(two['status'], 'ok')


class PredictionValidationTests(SimpleTestCase):
    def test_all_predictions_including_unknown_targets_require_finite_aligned_values(self):
        for prediction in ([], [1., 2.], [float('nan')], [float('inf')], [True], ['1']):
            with self.subTest(prediction=prediction), self.assertRaises(ValueError):
                metrics.paper_predictions([example(y=None)], prediction)

    def test_price_label_consistency_positive_lots_and_aware_forward_timestamps(self):
        base = example()
        invalid = [dict(base, y=9.), dict(base, actual_price=0.), dict(base, actual_price=None),
                   dict(base, anchor_price=0.), dict(base, entry_price=-1.),
                   dict(base, lot=0), dict(base, lot=1.5), dict(base, lot=True),
                   dict(base, at='2026-06-01T17:45:00'), dict(base, label_end='2026-06-02T10:15:00'),
                   dict(base, label_end=base['at']), dict(base, y=None),
                   dict(base, actual_price=float('inf'))]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                metrics.evaluate_predictions([row], [.5], 0., 0.)

    def test_duplicate_ticker_same_instant_with_other_offset_is_rejected(self):
        first = example()
        second = dict(first, at=datetime.fromisoformat(first['at']).astimezone(timezone.utc).isoformat())
        with self.assertRaises(ValueError):
            metrics.paper_predictions([first, second], [1., 1.])

    def test_invalid_costs_and_training_statistics_are_rejected(self):
        for cost in (-1, float('inf'), True, 2000):
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                metrics.paper_predictions([example()], [.5], multiplier=cost)
        for train_mean, train_median in ((float('nan'), 0.), (0., float('inf'))):
            with self.assertRaises(ValueError):
                metrics.evaluate_predictions([example()], [.5], train_mean, train_median)


class PredictionMetricTests(SimpleTestCase):
    def test_exact_mae_rmse_train_baselines_and_direction_zero_handling(self):
        rows = [example(0, y=10.), example(1, y=-10.), example(2, y=0.), example(3, y=None)]
        output = metrics.evaluate_predictions(rows, [2., 0., -1., 20.], train_mean=-2., train_median=1.)
        self.assertEqual((output['samples'], output['eligible_forecasts'], output['unknown_targets']), (3, 4, 1))
        self.assertAlmostEqual(output['mae_pct'], 19 / 3)
        self.assertAlmostEqual(output['rmse_pct'], math.sqrt(165 / 3))
        self.assertAlmostEqual(output['baseline_mae_pct'], 20 / 3)
        self.assertAlmostEqual(output['baseline_rmse_pct'], math.sqrt(200 / 3))
        self.assertAlmostEqual(output['mean_baseline_mae_pct'], 22 / 3)
        self.assertAlmostEqual(output['median_baseline_mae_pct'], 21 / 3)
        self.assertEqual(output['direction_accuracy_pct'], 50.)
        self.assertEqual(output['direction_nonzero_samples'], 2)
        self.assertEqual(output['direction_zero_predictions'], 1)
        self.assertEqual(output['direction_baseline_pct'], 50.)
        self.assertEqual(output['paper']['status'], 'incomplete')
        self.assertEqual(output['stress']['status'], 'incomplete')

    def test_zero_training_mean_does_not_silently_choose_down_direction(self):
        result = metrics.evaluate_predictions([example(y=-1.)], [-1.], 0., 0.)
        self.assertEqual(result['direction_accuracy_pct'], 100.)
        self.assertEqual(result['direction_baseline_pct'], 0.)
        self.assertEqual(result['direction_baseline_label'], 'zero')

    def test_empty_all_unknown_and_flat_targets_have_no_fabricated_metrics(self):
        for rows in ([], [example(y=None)]):
            result = metrics.evaluate_predictions(rows, [0.] * len(rows), 0., 0.)
            for name in ('mae_pct', 'rmse_pct', 'baseline_mae_pct', 'baseline_rmse_pct',
                         'mean_baseline_mae_pct', 'median_baseline_mae_pct', 'direction_accuracy_pct'):
                self.assertIsNone(result[name])
            self.assertIsNone(result['mae_difference_bootstrap']['lower_pct'])
            self.assertIsNone(result['paper']['net_return_pct'])
        self.assertIsNone(metrics.evaluate_predictions([example(y=0.)], [1.], 1., 1.)['direction_accuracy_pct'])

    def test_stress_is_exactly_separate_double_cost_paper(self):
        rows = [example(day) for day in range(5)]
        output = metrics.evaluate_predictions(rows, [1.] * 5, 0., 0.)
        expected = metrics.paper_predictions(rows, [1.] * 5, multiplier=2)
        self.assertEqual(output['stress'], expected)
        self.assertEqual(output['paper']['stress_return_pct'], expected['net_return_pct'])
        self.assertLess(expected['net_return_pct'], output['paper']['net_return_pct'])


class PairedBootstrapTests(SimpleTestCase):
    def test_tickers_on_same_date_are_never_sampled_apart(self):
        # Paired differences are exactly -1 and +1 on every date. Resampling
        # individual rows would invent uncertainty; grouped dates give zero.
        rows, predictions = [], []
        for day in range(15):
            rows += [example(day, 'SBER', y=1.), example(day, 'GAZP', y=1.)]
            predictions += [1., 3.]
        result = metrics.paired_mae_bootstrap(rows, predictions)
        self.assertEqual(result['dates'], 15)
        self.assertEqual(result['samples'], 30)
        self.assertEqual(result['difference_pct'], 0.)
        self.assertEqual(result['lower_pct'], 0.)
        self.assertEqual(result['upper_pct'], 0.)

    def test_fixed_contiguous_blocks_preserve_a_known_boundary_distribution(self):
        rows = [example(day, y=1.) for day in range(10)]
        predictions = [1.] * 5 + [3.] * 5  # day differences: five -1 then five +1
        # Force complete early/late blocks; verify each draw samples two of
        # the six possible contiguous blocks, not individual observations.
        with patch.object(metrics.random, 'Random') as rng:
            rng.return_value.randrange.side_effect = [0, 0, 5, 5] * 500
            result = metrics.paired_mae_bootstrap(rows, predictions)
            self.assertEqual(rng.return_value.randrange.call_count, 2000)
            self.assertTrue(all(call.args == (6,) for call in rng.return_value.randrange.call_args_list))
        self.assertEqual(result['difference_pct'], 0.)
        self.assertEqual(result['lower_pct'], -1.)
        self.assertEqual(result['upper_pct'], 1.)
        self.assertEqual(result['repetitions'], 1000)
        self.assertEqual(result['seed'], 20261024)
        self.assertEqual(result['block_dates'], 5)

    def test_bootstrap_is_deterministic_order_invariant_and_does_not_mutate_inputs(self):
        rows = [example(day, y=(day % 4) - 1.) for day in range(30)]
        predictions = [day / 20 for day in range(30)]
        original = copy.deepcopy(rows)
        first = metrics.paired_mae_bootstrap(rows, predictions)
        second = metrics.paired_mae_bootstrap(list(reversed(rows)), list(reversed(predictions)))
        self.assertEqual(first, second)
        self.assertEqual(rows, original)
        self.assertLess(first['lower_pct'], first['upper_pct'])

    def test_bootstrap_uses_moscow_calendar_date_and_requires_two_blocks(self):
        rows = []
        for day in range(10):
            first = example(day, 'SBER', y=1.)
            # Same Moscow date, different date representation in UTC text.
            at = datetime.fromisoformat(first['at']).replace(hour=0, minute=30)
            first['at'] = at.isoformat()
            second = example(day, 'GAZP', y=1.)
            second['at'] = at.astimezone(timezone.utc).isoformat()
            rows += [first, second]
        result = metrics.paired_mae_bootstrap(rows, [1., 3.] * 10)
        self.assertEqual(result['dates'], 10)
        self.assertEqual(result['lower_pct'], 0.)
        too_short = metrics.paired_mae_bootstrap(rows[:18], [1., 3.] * 9)
        self.assertEqual(too_short['status'], 'insufficient_dates')
        self.assertIsNone(too_short['lower_pct'])
        self.assertIsNone(too_short['upper_pct'])
