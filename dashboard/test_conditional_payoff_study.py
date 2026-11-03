"""Synthetic class-conditional causality, stock fallback, ordinal and risk-rule tests."""
import copy
import unittest
from unittest.mock import patch
import numpy as np
from . import conditional_payoff_study as study
from .test_risk_distribution_study import examples
from .test_overnight_decision_study import row


class FixedHead:
    def __init__(self,**params):self.params=params
    def fit(self,x,y,sample_weight):
        self.targets=np.asarray(y).copy();self.weights=np.asarray(sample_weight).copy()
        self.value=float(np.average(y,weights=sample_weight));return self
    def predict(self,x):return np.full(len(x),self.value)


def point(**updates):
    return {'old_probs':[.24,.36,.4],'ordinal_probs':[.3,.3,.4],
            'scores':{name:.25 for name in study.SCORES},**updates}


class ConditionalPayoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.train=examples('2019-01-01',200);cls.cal=examples('2023-01-01',60)
        cls.frozen=study.old.fit_family('payoff',cls.train,cls.cal)
        cls.bundle=study.fit_bundle(cls.train,cls.cal,cls.frozen)

    def test_ordinal_boundaries_projection_and_probability_conservation(self):
        self.assertEqual(study.ordinal_labels([-.501,-.5,0.,.5,.501]),[[0,1,1,1,1],[0,0,0,0,1]])
        p,crossed=study.ordinal_probabilities([.4,.8],[.6,.2])
        np.testing.assert_allclose(p,[[.5,0.,.5],[.2,.6,.2]])
        self.assertEqual(crossed.tolist(),[True,False])
        for a,b in (([float('nan')],[.2]),([1.1],[.2]),([.2],[.3,.4])):
            with self.assertRaises(ValueError):study.ordinal_probabilities(a,b)

    def test_stock_maps_shrink_and_unseen_stock_class_falls_back_to_global(self):
        rows=[{'ticker':'SBER'},{'ticker':'SBER'}]
        maps,counts=study.stock_maps(rows,np.asarray([-1.,1.]),np.asarray([0,2]),[-.8,-.2,.8])
        index=study.TICKERS.index('SBER');unused=study.TICKERS.index('YDEX')
        self.assertEqual(counts[index],[1,0,1])
        self.assertAlmostEqual(maps[index][0],(-1.-80)/101)
        self.assertEqual(maps[unused],[-.8,-.2,.8])

    def test_real_conditional_predictions_need_only_known_features_and_ticker(self):
        r=self.cal[0];feature={k:r[k] for k in ('u','market','risk_scale','ticker')}
        a=study.predict(self.bundle,self.frozen,[r])
        b=study.predict(self.bundle,self.frozen,[feature])
        np.testing.assert_allclose(study.numeric_outputs(a),study.numeric_outputs(b),atol=1e-12)
        changed={**r,'actual_price':None,'entry_price':None,'net_y':None,'profit_y':None,'y':None}
        np.testing.assert_allclose(study.numeric_outputs(a),study.numeric_outputs(study.predict(self.bundle,self.frozen,[changed])))
        self.assertEqual(study.predict({},self.frozen,[]),[])
        with self.assertRaises(ValueError):study.predict(self.bundle,self.frozen,[{**feature,'ticker':'UNKNOWN'}])

    def test_payoff_heads_cannot_predict_outside_training_class_support(self):
        feature={k:self.cal[0][k] for k in ('u','market','risk_scale','ticker')}
        feature['risk_scale']=100.
        p=study.predict(self.bundle,self.frozen,[feature])[0]
        for key in ('ridge_values','boost_values'):
            for v,(lo,hi) in zip(p[key],self.bundle['support_bounds']):self.assertTrue(lo<=v<=hi)
        self.assertLess(p['ridge_values'][0],-.5)
        self.assertGreater(p['ridge_values'][2],.5)

    def test_calibration_magnitudes_do_not_change_train_heads_scaler_or_stock_maps(self):
        changed=copy.deepcopy(self.cal)
        for index,r in enumerate(changed):
            r['actual_price']=(98.,100.1,102.)[index%3];r['y']=(r['actual_price']/r['anchor_price']-1)*100
        changed=study.old.features.enrich(changed)
        with patch('sklearn.linear_model.Ridge',FixedHead),patch('sklearn.ensemble.HistGradientBoostingRegressor',FixedHead):
            a,b=[study.fit_bundle(self.train,c,self.frozen) for c in (self.cal,changed)]
        np.testing.assert_array_equal(a['scaler'].mean_,b['scaler'].mean_)
        self.assertEqual(a['stock_maps'],b['stock_maps']);self.assertEqual(a['support_bounds'],b['support_bounds'])
        for aa,bb in zip(a['ridge_heads']+a['boost_heads'],b['ridge_heads']+b['boost_heads']):
            np.testing.assert_array_equal(aa.targets,bb.targets)
        self.assertNotEqual(a['interval_abs_pct'],b['interval_abs_pct'])

    def test_all18_rules_use_declared_absolute_or_prior_relative_risk(self):
        p=point()
        for score in study.SCORES:
            self.assertEqual(study.decisions(score+'/all',[p],.4),[True])
            self.assertEqual(study.decisions(score+'/fixed',[p],.4),[False])
            self.assertEqual(study.decisions(score+'/relative',[p],.4),[True])
            self.assertEqual(study.decisions(score+'/relative',[p],.25),[False])
        q=point(old_probs=[.2,.4,.4]);q['scores']['ridge']=.1
        self.assertEqual(study.decisions('ridge/all',[q],.4),[False])
        q['scores']['ridge']=.1001
        self.assertEqual(study.decisions('ridge/fixed',[q],.4),[True])
        self.assertEqual(len(study.POLICIES),18)

    def test_future_outcomes_do_not_change_selection_and_unknown_trade_poisoning_remains(self):
        points=[point(),point()];changed=[{**p,'actual_net_pct':None,'entry_price':None} for p in points]
        for name in study.POLICIES:self.assertEqual(study.decisions(name,points,.4),study.decisions(name,changed,.4))
        rows=study.old.features.enrich([row(day='2025-01-06',actual=None),row(day='2025-01-07')])
        levels=study.old.features.vol.checks(rows,study.decisions('ridge/all',points,.4))
        self.assertTrue(all(l['baseline']['report']['net_return_pct'] is None for l in levels))
        self.assertTrue(all(l['baseline']['report']['status']=='incomplete' for l in levels))

    def test_invalid_class_model_chronology_and_probabilities_fail(self):
        with self.assertRaises(ValueError):study.fit_bundle(self.train,self.train[:60],self.frozen)
        with self.assertRaises(ValueError):study.fit_bundle(self.train[:30],self.cal,self.frozen)
        changed={**self.frozen,'trained_rows':199}
        with self.assertRaises(ValueError):study.fit_bundle(self.train,self.cal,changed)
        with self.assertRaises(ValueError):study.decisions('new',[point()],.4)
        with self.assertRaises(ValueError):study.decisions('ridge/all',[point()],float('nan'))
        with self.assertRaises(ValueError):study.decisions('ridge/all',[point(old_probs=[.2,.3])],.4)
