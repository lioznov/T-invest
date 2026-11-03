import copy
import unittest
import numpy as np
from . import volatility_overnight_study_v2 as study
from . import verify_volatility_overnight as audit
from .test_overnight_decision_study import row


class IndependentVolatilityTests(unittest.TestCase):
    def test_independent_feature_formula_matches_and_keeps_unknown_peers(self):
        rows=[row('SBER',change=3.),row('GAZP',change=-1.,actual=None),row('LKOH',change=4.)]
        for i,r in enumerate(rows):r['x'][8]=.5+i
        actual=study.enrich(rows);expected=audit.recreate_features(rows)
        self.assertEqual(len(actual),len(expected))
        for a,b in zip(actual,expected):
            np.testing.assert_allclose(a['z'],b['z'],atol=1e-12)
            self.assertEqual(a['risk_scale'],b['risk_scale'])
        changed=copy.deepcopy(rows)
        changed[1].update(actual_price=200.,y=100.)
        self.assertEqual([r['z'] for r in expected],[r['z'] for r in audit.recreate_features(changed)])

    def test_separate_daily_ranking_cash_and_unknown_suppression(self):
        rows=study.enrich([row('SBER','2025-01-06',actual=105.),row('GAZP','2025-01-06',actual=99.),
                           row('SBER','2025-01-07',actual=98.)])
        days=audit.independent_days(rows,[True]*3,1)
        self.assertEqual(len(days),2)
        self.assertGreater(days[0]['pnl_rub'],0)
        self.assertLess(days[1]['pnl_rub'],0)
        levels=study.checks(rows,[True]*3)
        self.assertEqual(audit.check_levels(rows,[True]*3,levels),24)
        unknown=study.enrich([row(actual=None)])
        self.assertEqual(audit.independent_days(unknown,[True],1),[])
