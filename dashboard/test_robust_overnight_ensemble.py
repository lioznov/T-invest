"""Synthetic alignment/missing-label/strict-threshold tests; no archive or fits."""
import copy
import unittest
from . import robust_overnight_ensemble as ensemble
from . import overnight_decision_study as decision
from .test_overnight_decision_study import row


def variants(rows, values):
    return [{'id':name,'predictions':[{'ticker':r['ticker'],'at':r['at'],
        'actual_net_pct':r['net_y'],'predicted_net_pct':p} for r,p in zip(rows,ps)]}
        for name,ps in zip(ensemble.MEMBERS,values)]


class RobustEnsembleTests(unittest.TestCase):
    def test_row_order_is_not_model_alignment(self):
        rows=decision.enrich_rows([row(day='2025-01-06'),row(day='2025-01-07')])
        members=variants(rows,[[.2,-.2],[.3,-.3],[.4,-.4]])
        expected=ensemble.combine(rows,members)
        members[1]['predictions'].reverse()
        self.assertEqual(ensemble.combine(rows,members),expected)

    def test_single_outlier_does_not_pass_median_or_unanimity(self):
        rows=decision.enrich_rows([row()])
        p=ensemble.combine(rows,variants(rows,[[2.],[.1],[-.2]]))[0]
        self.assertTrue(p['selected_mean'])
        self.assertFalse(p['selected_median'])
        self.assertFalse(p['selected_unanimous'])

    def test_unknown_outcome_and_changed_future_never_change_selection(self):
        a=decision.enrich_rows([row()]); b=decision.enrich_rows([row(actual=None)])
        first=ensemble.combine(a,variants(a,[[.2],[.3],[.4]]))[0]
        second=ensemble.combine(b,variants(b,[[.2],[.3],[.4]]))[0]
        self.assertIsNone(second['actual_net_pct'])
        for name in ensemble.POLICIES:
            self.assertEqual(first['selected_'+name],second['selected_'+name])

    def test_missing_duplicate_nonfinite_and_wrong_labels_are_rejected(self):
        rows=decision.enrich_rows([row()]); members=variants(rows,[[.2],[.3],[.4]])
        for mutation in (
            lambda v:v[0]['predictions'].clear(),
            lambda v:v[1]['predictions'].append(copy.deepcopy(v[1]['predictions'][0])),
            lambda v:v[0]['predictions'][0].update(predicted_net_pct=float('nan')),
            lambda v:v[2]['predictions'][0].update(actual_net_pct=100.),
            lambda v:v[2].update(id='another'),
        ):
            changed=copy.deepcopy(members); mutation(changed)
            with self.assertRaises(ValueError): ensemble.combine(rows,changed)
