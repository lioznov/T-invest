"""Synthetic chronology, class-boundary, calibration and risk-decision checks."""
import copy
import unittest
from unittest.mock import patch
import numpy as np
from . import risk_distribution_study as study
from .test_overnight_decision_study import sample,row


def examples(start,count):
    rows=sample(start,count)
    for index,r in enumerate(rows):
        r['x'][8]=abs(r['x'][8])+.25
        r['actual_price']=(99.,100.,101.)[index%3]
        r['y']=(r['actual_price']/r['anchor_price']-1)*100
    return study.features.enrich(rows)


def point(**updates):
    return {'q20':-.2,'q50':.3,'q80':.8,'lower':-.4,'upper':1.,'p_loss':.15,
            'p_classes':[.15,.35,.5],'mean_net':.3,'median_net':.2,'base_net':.25,**updates}


class FixedQuantile:
    def __init__(self,**params):self.params=params
    def fit(self,x,y,sample_weight):
        self.targets=np.array(y,copy=True);self.weights=np.array(sample_weight,copy=True)
        self.q=self.params['quantile'];return self
    def predict(self,x):return np.full(len(x),self.q-.5)


class RiskDistributionTests(unittest.TestCase):
    def test_class_boundaries_are_original_net_returns(self):
        self.assertEqual([study.class_label(v) for v in (-.501,-.5,0.,.5,.501)],[0,1,1,1,2])
        for bad in (None,float('nan'),True):
            with self.assertRaises(ValueError):study.class_label(bad)

    def test_range_correction_uses_finite_sample_rank_and_never_shrinks(self):
        self.assertEqual(study.corrected_range([0.]*5,[1.]*5,[.5]*5),0.)
        self.assertEqual(study.corrected_range([0.]*5,[1.]*5,[1.,2.,3.,4.,5.]),4.)
        for low,high,target in (([],[],[]),([0.],[1.,2.],[0.]),([float('nan')],[1.],[0.])):
            with self.assertRaises(ValueError):study.corrected_range(low,high,target)

    def test_quantile_training_and_scaler_exclude_calibration_answers(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        changed=copy.deepcopy(cal)
        for r in changed:
            r['u'][0]=1e6;r['actual_price']=500.;r['y']=400.
            r['net_y']=100*(500*.9995*.9995/(r['entry_price']*1.0005*1.0005)-1);r['profit_y']=1
        with patch('sklearn.ensemble.HistGradientBoostingRegressor',FixedQuantile):
            a,b=[study.fit_family('quantiles',train,c) for c in (cal,changed)]
        np.testing.assert_array_equal(a['scaler'].mean_,b['scaler'].mean_)
        expected=[r['net_y']/r['risk_scale'] for r in train]
        for first,second,q in zip(a['estimators'],b['estimators'],study.QUANTILES):
            np.testing.assert_array_equal(first.targets,expected)
            np.testing.assert_array_equal(first.targets,second.targets)
            self.assertEqual(first.params['quantile'],q)
        self.assertNotEqual(a['qhat'],b['qhat'])

    def test_real_three_class_model_maps_are_train_only_and_inference_needs_no_future(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        bundle=study.fit_family('payoff',train,cal)
        y=np.asarray([r['net_y'] for r in train]);labels=np.asarray([study.class_label(float(v)) for v in y])
        bounds=np.quantile(y,[.025,.975])
        np.testing.assert_allclose(bundle['payoff_clip_bounds'],bounds)
        np.testing.assert_allclose(bundle['class_means'],[np.clip(y,*bounds)[labels==c].mean() for c in range(3)])
        np.testing.assert_allclose(bundle['class_medians'],[np.median(y[labels==c]) for c in range(3)])
        feature={k:cal[0][k] for k in ('u','market','risk_scale')}
        value=study.predict(bundle,[feature])[0]
        self.assertEqual(value,study.predict(bundle,cal[:1])[0])
        self.assertAlmostEqual(sum(value['p_classes']),1.)
        self.assertAlmostEqual(value['mean_net'],sum(p*m for p,m in zip(value['p_classes'],bundle['class_means'])))

    def test_real_loss_model_has_separate_year_calibrator_and_valid_class_order(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        bundle=study.fit_family('loss',train,cal)
        classifier,calibrator=bundle['estimators']
        self.assertEqual(list(classifier.classes_),[0,1]);self.assertEqual(list(calibrator.classes_),[0,1])
        value=study.predict(bundle,[{k:cal[0][k] for k in ('u','market','risk_scale')}])[0]['p_loss']
        self.assertTrue(0<=value<=1)
        self.assertAlmostEqual(bundle['calibration_class_priors'][1],np.mean([r['net_y']<-.5 for r in cal]))

    def test_crossed_raw_quantiles_are_not_reordered_to_pass_risk_guards(self):
        crossed=point(q20=.6,q50=.3,q80=.8,lower=.4)
        self.assertEqual(study.decisions('q_median',[crossed]),[True])
        for name in ('q_risk','q_positive_lower','q_loss20'):
            self.assertEqual(study.decisions(name,[crossed]),[False])
        self.assertEqual(crossed['q20'],.6)

    def test_all_ten_fixed_rules_and_strict_thresholds(self):
        p=point()
        expected={'q_median':True,'q_risk':True,'q_positive_lower':False,'base_loss20':True,'base_loss10':False,
                  'q_loss20':True,'payoff_mean':True,'payoff_median':True,'payoff_safe_mean':True,'payoff_agree_safe':True}
        self.assertEqual({name:study.decisions(name,[p])[0] for name in study.POLICIES},expected)
        self.assertFalse(study.decisions('q_median',[point(q50=.1)])[0])
        self.assertTrue(study.decisions('base_loss20',[point(p_loss=.2)])[0])
        self.assertFalse(study.decisions('base_loss20',[point(base_net=.1)])[0])
        self.assertFalse(study.decisions('payoff_agree_safe',[point(median_net=.1)])[0])

    def test_changed_future_outcome_cannot_change_risk_decisions_and_unknown_poisons_account(self):
        points=[point(),point()];changed=copy.deepcopy(points)
        changed[0].update(actual_net_pct=None,entry_price=None,actual_price=None)
        changed[1].update(actual_net_pct=1000.)
        for name in study.POLICIES:self.assertEqual(study.decisions(name,points),study.decisions(name,changed))
        rows=study.features.enrich([row(day='2025-01-06',actual=None),row(day='2025-01-07')])
        levels=study.features.vol.checks(rows,study.decisions('base_loss20',points))
        self.assertTrue(all(l['baseline']['report']['status']=='incomplete' for l in levels))
        self.assertTrue(all(l['baseline']['report']['net_return_pct'] is None for l in levels))

    def test_missing_classes_overlapping_years_invalid_shapes_or_policy_fail(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        with self.assertRaises(ValueError):study.fit_family('loss',train,train[:60])
        with self.assertRaises(ValueError):study.fit_family('unknown',train,cal)
        one=copy.deepcopy(train)
        for r in one:r['net_y']=-.2;r['profit_y']=0;r['entry_price']=100.;r['actual_price']=100.;r['y']=0.
        one=study.features.enrich(one)
        with self.assertRaises(ValueError):study.fit_family('loss',one,cal)
        with self.assertRaises(ValueError):study.decisions('unknown',[point()])
        for bad in (point(p_loss=float('nan')),point(p_classes=[.1,.2]),point(p_loss=1.1)):
            with self.assertRaises(ValueError):study.decisions('q_median',[bad])
        self.assertEqual(study.predict({},[]),[])
