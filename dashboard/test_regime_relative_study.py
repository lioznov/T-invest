"""Synthetic causal basket/gate/expert and future-independent ranking checks."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from . import regime_relative_study as study
from .test_overnight_decision_study import row,sample
from .test_robust_overnight_models import FixedEstimator


def examples(start,count):
    rows=sample(start,count)
    for r in rows:r['x'][8]=abs(r['x'][8])+.25
    return study.enrich(rows)


class RegimeRelativeTests(unittest.TestCase):
    def test_market_is_one_same_instant_feature_vector_including_unknown_peers(self):
        rows=[row('SBER',change=3.),row('GAZP',change=-2.,actual=None),row('LKOH',change=1.)]
        for r in rows:r['x'][8]=1.
        actual=study.enrich(rows)
        self.assertEqual(len({tuple(r['market']) for r in actual}),1)
        self.assertEqual(actual[0]['market'][0],1.)
        self.assertAlmostEqual(actual[0]['market'][5],2/3)
        self.assertTrue(all(len(r['u'])==29 for r in actual))
        self.assertIsNone(next(r for r in actual if r['ticker']=='GAZP')['net_y'])

    def test_future_labels_never_change_market_or_neighbour_features(self):
        raw=[row('SBER'),row('GAZP')]
        original=copy.deepcopy(raw)
        before=study.enrich(raw)
        raw[0].update(actual_price=None,y=None,entry_price=None)
        raw[1].update(actual_price=300.,y=200.)
        after=study.enrich(raw)
        self.assertEqual([r['u'] for r in before],[r['u'] for r in after])
        self.assertEqual(original[0]['actual_price'],101.)

    def test_gate_uses_unique_training_instants_and_no_outcomes(self):
        train=examples('2019-01-01',200)
        only_features=[{k:r[k] for k in ('at','u','market','risk_scale')} for r in train]
        gate=study.fit_gate(only_features)
        expected=np.asarray(list({study.instant(r):r['market'] for r in train}.values()))
        np.testing.assert_allclose(gate['scaler'].mean_,expected.mean(axis=0))
        self.assertEqual(gate['dates'],40)
        self.assertEqual(gate['model'].n_components,2)
        p=study.responsibilities(gate,only_features)
        np.testing.assert_allclose(p.sum(axis=1),1.)
        self.assertTrue(np.isfinite(p).all())

    def test_expert_training_weights_sum_to_original_date_weights(self):
        train,cal=examples('2019-01-01',205),examples('2023-01-01',60)
        train[0].update(entry_price=None,net_y=None,profit_y=None)
        gate=study.fit_gate(train)
        with patch('sklearn.linear_model.Ridge',FixedEstimator):
            bundle=study.fit_variant(study.VARIANTS[1],train,cal,gate)
        known=[r for r in train if r['net_y'] is not None]
        total=sum(model.weights for model in bundle['estimators'])
        np.testing.assert_allclose(total,study.vol.prior.date_weights(known))
        self.assertAlmostEqual(sum(bundle['effective_weight_sums']),len(known))
        self.assertEqual(bundle['trained_rows'],204)

    def test_soft_mixture_restores_original_units_from_features_only(self):
        class Constant:
            def __init__(self,value):self.value=value
            def predict(self,x):return np.full(len(x),self.value)
        class Gate:
            def predict_proba(self,x):return np.tile([.25,.75],(len(x),1))
        scaler=SimpleNamespace(transform=lambda x:x)
        bundle={'spec':study.VARIANTS[1],'scaler':scaler,'estimators':[Constant(1.),Constant(3.)],
                'gate':{'scaler':scaler,'model':Gate()}}
        feature={'u':[0.]*29,'market':[0.]*6,'risk_scale':2.}
        self.assertEqual(study.predict(bundle,[feature]),[5.])
        self.assertEqual(study.predict(bundle,[]),[])

    def test_clipped_neural_targets_and_scaling_exclude_calibration_extremes(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        changed=copy.deepcopy(cal)
        for r in changed:
            r['u'][0]=1e6
            r['actual_price']=500.;r['y']=400.
            r['net_y']=100*(500*.9995*.9995/(r['entry_price']*1.0005*1.0005)-1)
            r['profit_y']=1
        with patch('sklearn.neural_network.MLPRegressor',FixedEstimator):
            a,b=[study.fit_variant(study.VARIANTS[3],train,c,None) for c in (cal,changed)]
        np.testing.assert_array_equal(a['scaler'].mean_,b['scaler'].mean_)
        np.testing.assert_allclose(a['target_clip_bounds'],np.quantile([r['net_y']/r['risk_scale'] for r in train],[.025,.975]))
        self.assertEqual(a['target_clip_bounds'],b['target_clip_bounds'])
        self.assertNotEqual(a['interval_abs_pct'],b['interval_abs_pct'])
        self.assertEqual([m.params['random_state'] for m in a['estimators']],list(study.vol.prior.SEEDS))

    def test_top_two_is_per_instant_ties_by_ticker_and_unknowns_stay_selected(self):
        rows=study.enrich([row('SBER'),row('GAZP',actual=None),row('LKOH')])
        by_name={r['ticker']:r for r in rows}
        rows=[by_name[n] for n in ('SBER','LKOH','GAZP')]
        mask=study.decisions('top2',rows,[.3,.2,.2])
        self.assertEqual(mask,[True,False,True])
        changed=copy.deepcopy(rows)
        for r in changed:r.update(actual_price=None,entry_price=None,net_y=None,profit_y=None,y=None)
        self.assertEqual(study.decisions('top2',changed,[.3,.2,.2]),mask)
        levels=study.vol.checks(rows,mask)
        self.assertTrue(all(l['baseline']['report']['status']=='incomplete' for l in levels))
        self.assertEqual(study.decisions('all',rows,[.1,.11,-.2]),[False,True,False])

    def test_relative_ranking_uses_only_known_cross_section_and_coverage(self):
        raw=[row(t) for t in ('SBER','GAZP','LKOH','NVTK','GMKN')]
        for r,one,five in zip(raw,[2.,1.,-2.,-3.,1.],[5.,3.,1.,2.,4.]):
            r['x'][0]=one;r['x'][1]=five;r['x'][2]=10.;r['x'][9]=1.;r['x'][8]=1.
        rows=study.enrich(raw)
        chosen=lambda name:{r['ticker'] for r,keep in zip(rows,study.decisions(name,rows)) if keep}
        self.assertEqual(chosen('relative_momentum'),{'SBER','GMKN'})
        self.assertEqual(chosen('relative_pullback'),{'LKOH','NVTK'})
        short=study.enrich(raw[:3])
        for name in study.HEURISTICS:self.assertFalse(any(study.decisions(name,short)))

    def test_real_pooled_ridge_inference_accepts_no_future_fields(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        bundle=study.fit_variant(study.VARIANTS[0],train,cal,None)
        feature={k:cal[0][k] for k in ('u','market','risk_scale')}
        self.assertEqual(study.predict(bundle,[feature]),study.predict(bundle,cal[:1]))

    def test_bad_shapes_duplicates_invalid_probabilities_and_overlap_fail(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        with self.assertRaises(ValueError):study.fit_variant(study.VARIANTS[0],train,train[:60],None)
        with self.assertRaises(ValueError):study.fit_variant({'id':'new','kind':'ridge','experts':1},train,cal,None)
        with self.assertRaises(ValueError):study.decisions('all',train,[])
        with self.assertRaises(ValueError):study.decisions('unknown',train)
        with self.assertRaises(ValueError):study.decisions('top2',[train[0],train[0]],[.2,.3])
        bad=copy.deepcopy(train[:1]);bad[0]['u']=[0.]
        with self.assertRaises(ValueError):study.matrix(bad)
        gate={'scaler':SimpleNamespace(transform=lambda x:x),
              'model':SimpleNamespace(predict_proba=lambda x:np.tile([1.2,-.2],(len(x),1)))}
        with self.assertRaises(ValueError):study.responsibilities(gate,train[:1])
