"""Synthetic feature causality, normalisation and stricter best-five checks."""
import copy
import unittest
from datetime import date, timedelta, timezone
from unittest.mock import patch
import numpy as np
from . import volatility_overnight_study as study
from .test_overnight_decision_study import row, sample
from .test_robust_overnight_models import FixedEstimator


def examples(start,count):
    rows=sample(start,count)
    for r in rows:
        r['x'][8]=abs(r['x'][8])+.25
    return study.enrich(rows)


class VolatilityStudyTests(unittest.TestCase):
    def test_units_horizon_scaling_and_floor_use_known_volatility(self):
        r=row(); r['x'][:11]=[2.,5.,20.,4.,2.,6.,.8,0.,2.,1.,.4]
        z,scale=study.normalised_x(r)
        self.assertEqual(scale,2.)
        self.assertEqual(z[0],1.)
        self.assertAlmostEqual(z[1],5/(2*np.sqrt(5)))
        self.assertAlmostEqual(z[2],20/(2*np.sqrt(20)))
        self.assertEqual(z[3],2.)
        self.assertEqual(z[6:9],[.8,0.,2.])
        r['x'][8]=0.
        self.assertEqual(study.normalised_x(r)[1],.25)
        r['x'][8]=-.1
        with self.assertRaises(ValueError): study.normalised_x(r)

    def test_peers_share_utc_instant_keep_unknown_future_and_exclude_self(self):
        rows=[row('SBER',change=3.),row('GAZP',change=-2.,actual=None),row('LKOH',change=4.)]
        for r in rows:r['x'][8]=1.
        rows[1]['at']=study.prior.temporal._time(rows[1]['at']).astimezone(timezone.utc).isoformat()
        result={r['ticker']:r for r in study.enrich(rows)}
        np.testing.assert_allclose(result['SBER']['z'][-5:],[1.,3.,.5,2.,2/6])
        self.assertIsNone(result['GAZP']['net_y'])
        self.assertEqual(result['GAZP']['z'][-1],2/6)

    def test_prior_and_future_peer_are_never_backfilled(self):
        rows=[row('SBER'),row('GAZP','2020-01-03'),row('LKOH','2020-01-07')]
        for r in rows:r['x'][8]=1.
        focus=next(r for r in study.enrich(rows) if r['ticker']=='SBER')
        self.assertEqual(focus['z'][-5:],[0.]*5)

    def test_future_prices_and_unknowns_do_not_change_features_or_mutate_inputs(self):
        rows=[row('SBER'),row('GAZP')]
        for r in rows:r['x'][8]=1.
        original=copy.deepcopy(rows)
        before=study.enrich(rows)
        changed=copy.deepcopy(rows)
        changed[0].update(entry_price=None,actual_price=None,y=None)
        changed[1].update(entry_price=110.,actual_price=300.,y=200.)
        after=study.enrich(changed)
        self.assertEqual(rows,original)
        self.assertEqual([r['z'] for r in before],[r['z'] for r in after])
        self.assertEqual([r['risk_scale'] for r in before],[r['risk_scale'] for r in after])

    def test_real_ridge_returns_original_units_from_feature_only_input(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        bundle=study.fit_variant(study.VARIANTS[0],train,cal)
        feature={'z':cal[0]['z'],'risk_scale':cal[0]['risk_scale']}
        value=study.predict(bundle,[feature])[0]
        x=np.clip(bundle['scaler'].transform(study.matrix([feature])),-8,8)
        expected=bundle['estimators'][0].predict(x)[0]*feature['risk_scale']
        self.assertAlmostEqual(value,expected)
        self.assertEqual(study.predict(bundle,[cal[0]]),[value])

    def test_mlp_target_bounds_and_scaler_never_fit_calibration_answers(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        changed=copy.deepcopy(cal)
        for r in changed:
            r['z'][0]=1e6
            r['actual_price']=500.; r['y']=400.
            r['net_y']=100*(500*.9995*.9995/(r['entry_price']*1.0005*1.0005)-1)
            r['profit_y']=1
        with patch('sklearn.neural_network.MLPRegressor',FixedEstimator):
            a,b=[study.fit_variant(study.VARIANTS[1],train,c) for c in (cal,changed)]
        target=np.asarray([r['net_y']/r['risk_scale'] for r in train])
        bounds=np.quantile(target,[.025,.975])
        np.testing.assert_allclose(a['target_clip_bounds'],bounds)
        np.testing.assert_array_equal(a['scaler'].mean_,b['scaler'].mean_)
        for model,seed in zip(a['estimators'],study.prior.SEEDS):
            self.assertEqual(model.params['random_state'],seed)
            np.testing.assert_allclose(model.received_y,np.clip(target,*bounds))
        self.assertEqual(a['target_clip_bounds'],b['target_clip_bounds'])
        self.assertNotEqual(a['interval_abs_pct'],b['interval_abs_pct'])

    def test_rules_use_known_price_conditions_not_future_labels(self):
        r=study.enrich([row()])[0]
        r['x'][2]=10.;r['x'][9]=1.;r['x'][7]=.1;r['x'][6]=.8
        r['z'][-3]=.5;r['z'][0]=-1.;r['z'][3]=1.
        for name in study.POLICIES+study.HEURISTICS:
            values=[.2] if name in study.POLICIES else None
            self.assertEqual(study.decisions(name,[r],values),[True])
            changed={**r,'net_y':None,'profit_y':None,'entry_price':None,'actual_price':None,'y':None}
            self.assertEqual(study.decisions(name,[r],values),study.decisions(name,[changed],values))
        self.assertEqual(study.decisions('plain',[r],[.1]),[False])
        r['z'][-3]=0.
        self.assertEqual(study.decisions('trend_guard',[r],[.2]),[False])
        self.assertEqual(study.decisions('strong_day',[r]),[False])

    def test_best_five_keeps_whole_dates_and_strict_screen(self):
        start=date(2025,1,1)
        rows=study.enrich([row(day=(start+timedelta(days=i)).isoformat(),actual=102.) for i in range(20)])
        levels=study.checks(rows,[True]*len(rows))
        self.assertEqual([d['requested_best_dates'] for d in levels[0]['drop_best_dates']],[1,3,5])
        drop=levels[0]['drop_best_dates'][-1]
        self.assertEqual(len(drop['omitted_dates']),5)
        self.assertEqual(drop['report']['selected_candidates'],15)
        self.assertTrue(study.passes(levels))
        bad=copy.deepcopy(levels)
        bad[1]['drop_best_dates'][-1]['report']['net_return_pct']=-.01
        self.assertFalse(study.passes(bad))
        bad=copy.deepcopy(levels);bad[0]['drop_best_dates'].pop()
        self.assertFalse(study.passes(bad))
        bad=copy.deepcopy(levels);bad[0]['baseline']['report']['net_return_pct']=float('nan')
        self.assertFalse(study.passes(bad))

    def test_selected_unknown_suppresses_best_days_and_any_partial_profit(self):
        rows=study.enrich([row(day='2025-01-06',actual=110.),row(day='2025-01-07',actual=None)])
        for level in study.checks(rows,[True,True]):
            self.assertEqual(level['baseline']['report']['status'],'incomplete')
            self.assertIsNone(level['baseline']['report']['net_return_pct'])
            self.assertEqual(level['drop_best_dates'],[])
        self.assertFalse(study.passes(study.checks(rows,[False,False])))

    def test_overlaps_bad_dimensions_unregistered_models_or_rules_fail(self):
        train,cal=examples('2019-01-01',200),examples('2023-01-01',60)
        for spec,t,c in ((study.VARIANTS[0],train,train[:60]),(study.VARIANTS[0],train[:30],cal),
                         ({'id':'unregistered','kind':'ridge'},train,cal)):
            with self.assertRaises(ValueError):study.fit_variant(spec,t,c)
        for values in ([0.], [float('nan')]*23):
            with self.assertRaises(ValueError):study.matrix([{'z':values,'risk_scale':1.}])
        self.assertEqual(study.predict({},[]),[])
        with self.assertRaises(ValueError):study.decisions('plain',cal,[])
        with self.assertRaises(ValueError):study.decisions('unknown',cal)
