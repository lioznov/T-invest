"""Synthetic economic and missing-outcome checks; no network or archive reads."""
import copy
import unittest
from . import overnight_robustness_review as review
from .test_overnight_decision_study import row


class RobustnessReviewTests(unittest.TestCase):
    def test_daily_cash_ledger_includes_whole_lots_and_all_same_date_stocks(self):
        rows = [row('SBER', '2025-01-06', actual=104.), row('GAZP', '2025-01-06', actual=98.),
                row('SBER', '2025-01-07', actual=101.)]
        rows[0]['lot'] = 10
        result = review.replay(rows, [True]*3, 1)
        self.assertEqual(result['report']['trades'], 3)
        self.assertEqual(len(result['daily']), 2)
        self.assertEqual(result['daily'][0]['positions'][0]['quantity'] % 10, 0)
        self.assertEqual(result['daily'][1]['opening_cash'], result['daily'][0]['ending_cash'])
        self.assertAlmostEqual(sum(d['pnl_rub'] for d in result['daily']), result['ending_cash']-100000.)

    def test_hindsight_best_date_removal_removes_all_stocks_together(self):
        rows = [row('SBER', '2025-01-06', actual=110.), row('GAZP', '2025-01-06', actual=109.),
                row('SBER', '2025-01-07', actual=98.)]
        result = review.review(rows, [True]*3)
        drop = result['drop_best_dates'][0]
        self.assertEqual(drop['omitted_dates'], ['2025-01-06'])
        self.assertTrue(drop['hindsight_only'])
        self.assertEqual(drop['report']['selected_candidates'], 1)
        self.assertLess(drop['report']['net_return_pct'], 0)

    def test_selected_unknown_never_publishes_known_subset_profit_or_best_dates(self):
        rows = [row(day='2025-01-06', actual=110.), row(day='2025-01-07', actual=None)]
        result = review.review(rows, [True, True])
        self.assertEqual(result['status'], 'incomplete')
        self.assertIsNone(result['baseline']['ending_cash'])
        self.assertIsNone(result['baseline']['report']['net_return_pct'])
        self.assertEqual(result['baseline']['daily'], [])
        self.assertIsNone(result['concentration'])
        self.assertEqual(result['quarters'], [])
        self.assertEqual(result['drop_best_dates'], [])

    def test_no_trades_is_not_zero_return_or_positive_evidence(self):
        result = review.review([row()], [False])
        self.assertEqual(result['baseline']['report']['trades'], 0)
        self.assertIsNone(result['baseline']['report']['net_return_pct'])
        self.assertIsNone(result['concentration'])

    def test_quarters_are_fixed_calendar_groups_and_omission_replays_remaining_cash(self):
        rows = [row(day='2025-01-06'), row(day='2025-04-07'), row(day='2025-10-06')]
        result = review.review(rows, [True]*3)
        self.assertEqual([q['report']['selected_candidates'] for q in result['quarters']], [1,1,0,1])
        self.assertEqual([q['report']['selected_candidates'] for q in result['omit_quarters']], [2,2,3,2])
        self.assertIsNone(result['quarters'][2]['report']['net_return_pct'])
        self.assertAlmostEqual(result['quarters'][0]['report']['net_return_pct'],
                               review.replay(rows[:1], [True], 1)['report']['net_return_pct'])

    def test_cost_stress_preserves_candidates_and_inputs(self):
        rows = [row(day='2025-01-06'), row(day='2025-01-07')]
        original = copy.deepcopy(rows)
        normal, stress = [review.review(rows, [True, True], m) for m in (1,2)]
        self.assertEqual(rows, original)
        self.assertEqual(normal['baseline']['report']['selected_candidates'], stress['baseline']['report']['selected_candidates'])
        self.assertLess(stress['baseline']['ending_cash'], normal['baseline']['ending_cash'])
        for bad in ([], [1,1], [None,None]):
            with self.assertRaises(ValueError): review.review(rows, bad)
