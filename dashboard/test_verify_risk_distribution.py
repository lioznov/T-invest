import unittest
from types import SimpleNamespace
import numpy as np
from . import verify_risk_distribution as audit
from . import risk_distribution_study as study
from .test_risk_distribution_study import examples,point


class IndependentRiskTests(unittest.TestCase):
    def test_manual_sigmoid_and_softmax_match_saved_logistic_parameters(self):
        from scipy.special import expit,softmax
        class Classifier:
            def __init__(self,classes,raw):self.classes_=np.asarray(classes);self.raw=np.asarray(raw)
            def decision_function(self,x):return self.raw
        rows=[{'u':[0.]*29,'market':[0.]*6,'risk_scale':1.} for _ in range(2)]
        scaler=SimpleNamespace(mean_=np.zeros(29),scale_=np.ones(29))
        cal=SimpleNamespace(classes_=np.asarray([0,1]),coef_=np.asarray([[2.]]),intercept_=np.asarray([-.5]))
        bundle={'family':'loss','scaler':scaler,'estimators':[Classifier([0,1],[.2,-.3]),cal]}
        np.testing.assert_allclose([p['p_loss'] for p in audit.inference(bundle,rows)],expit(np.asarray([.2,-.3])*2-.5))
        raw=np.asarray([[.2,.3,-.4],[-.1,.2,.5]])
        cal=SimpleNamespace(classes_=np.asarray([0,1,2]),coef_=np.eye(3),intercept_=np.asarray([.1,.2,.3]))
        bundle={'family':'payoff','scaler':scaler,'estimators':[Classifier([0,1,2],raw),cal],
                'class_means':[-1.,0.,1.],'class_medians':[-.8,-.2,.8]}
        got=audit.inference(bundle,rows);expected=softmax(raw+cal.intercept_,axis=1)
        np.testing.assert_allclose([p['p_classes'] for p in got],expected)
        np.testing.assert_allclose([p['mean_net'] for p in got],expected@np.asarray([-1.,0.,1.]))

    def test_independent_all_rule_formulas_match_and_ignore_actual_outcomes(self):
        points=[point(),point(lower=.1,p_loss=.05),point(q20=.8),point(mean_net=.1,p_classes=[.3,.3,.4])]
        for name in study.POLICIES:self.assertEqual(audit.selections(name,points),study.decisions(name,points))
        changed=[{**p,'actual_net_pct':None} for p in points]
        for name in study.POLICIES:self.assertEqual(audit.selections(name,points),audit.selections(name,changed))

    def test_real_binary_family_independent_inference_matches_feature_only_api(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        bundle=study.fit_family('loss',train,cal)
        rows=[{k:r[k] for k in ('u','market','risk_scale')} for r in cal]
        np.testing.assert_allclose([p['p_loss'] for p in audit.inference(bundle,rows)],
                                   [p['p_loss'] for p in study.predict(bundle,rows)],atol=1e-12)
