import copy
import unittest
import numpy as np
from . import regime_relative_study as study
from . import verify_regime_relative as audit
from .test_regime_relative_study import examples
from .test_overnight_decision_study import row


class IndependentRegimeTests(unittest.TestCase):
    def test_independent_basket_features_keep_unknown_members(self):
        raw=[row('SBER'),row('GAZP',actual=None),row('LKOH',change=-2.)]
        for i,r in enumerate(raw):r['x'][8]=.5+i
        a,b=study.enrich(raw),audit.recreate_features(raw)
        for first,second in zip(a,b):np.testing.assert_allclose(first['u'],second['u'],atol=1e-12)
        changed=copy.deepcopy(raw);changed[1].update(actual_price=200.,y=100.)
        for first,second in zip(audit.recreate_features(raw),audit.recreate_features(changed)):
            np.testing.assert_array_equal(first['u'],second['u'])

    def test_manual_gaussian_probabilities_match_library_on_feature_only_rows(self):
        train=examples('2019-01-01',200);gate=study.fit_gate(train)
        stripped=[{k:r[k] for k in ('u','market','risk_scale')} for r in train]
        np.testing.assert_allclose(audit.gate_probabilities(gate,stripped),study.responsibilities(gate,stripped),atol=1e-10)

    def test_separate_rankings_match_all_policies_and_keep_unknown_selection(self):
        raw=[row(t) for t in ('SBER','GAZP','LKOH','NVTK','GMKN')]
        for r,one,five in zip(raw,[2.,1.,-2.,-3.,1.],[5.,3.,1.,2.,4.]):
            r['x'][0]=one;r['x'][1]=five;r['x'][2]=10.;r['x'][9]=1.;r['x'][8]=1.
        rows=study.enrich(raw);values=[.2,.2,.1,.3,-.1]
        for name in study.POLICIES+study.HEURISTICS:
            forecasts=values if name in study.POLICIES else None
            self.assertEqual(audit.selections(name,rows,forecasts),study.decisions(name,rows,forecasts))
