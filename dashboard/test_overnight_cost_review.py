import unittest
from . import overnight_cost_review as review
from . import overnight_decision_study as decision
from .test_overnight_decision_study import row


class CostReviewTests(unittest.TestCase):
    def test_every_cost_level_keeps_identical_decisions(self):
        rows = decision.enrich_rows([row(day='2025-01-06'), row(day='2025-01-07')])
        levels = review.curve(rows, [True, False])
        self.assertEqual(len(levels), 6)
        self.assertTrue(all(p['paper']['selected_candidates'] == 1 for p in levels))
        self.assertGreater(levels[0]['paper']['net_return_pct'], levels[-1]['paper']['net_return_pct'])

    def test_unknown_selected_result_stays_unknown_at_every_cost(self):
        rows = decision.enrich_rows([row(actual=None)])
        for item in review.curve(rows, [True]):
            self.assertIsNone(item['paper']['net_return_pct'])
            self.assertEqual(item['paper']['status'], 'incomplete')
        for bad in ([], [1], [None]):
            with self.assertRaises(ValueError): review.curve(rows, bad)
