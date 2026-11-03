"""Synthetic DAY-model causality and no-executable-close checks; no API."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from django.test import SimpleTestCase
from . import morning_model as m
from . import multiyear_daily as daily
from .signals import Candle


def history(days=90):
    start = datetime(2023,11,1,tzinfo=timezone.utc)
    return [Candle(start+timedelta(days=i),100.+i*.05,101.+i*.05,99.+i*.05,100.1+i*.05,1000+i)
            for i in range(days)]


def constant_model(value=.5, interval=.75):
    return {'center':[0.]*len(m.FEATURES), 'scale':[1.]*len(m.FEATURES),
            'weights':[value]+[0.]*len(m.FEATURES), 'interval_abs_pct':interval, 'train_mean_pct':.1}


class MultiyearDailyExamplesTests(SimpleTestCase):
    def setUp(self):
        self.bars = history()
        self.rows,self.coverage = daily.build_examples(self.bars,'SBER',10)
        self.row,self.index = self.rows[5],25

    def test_dimension_completed_day_knowledge_and_no_entry(self):
        self.assertEqual(len(daily.FEATURES),18)
        self.assertEqual(len(self.rows),70)
        self.assertGreater(self.coverage['observed_weekend_dates'],0)
        for i,row in enumerate(self.rows,20):
            self.assertEqual(datetime.fromisoformat(row['at']),self.bars[i].time+timedelta(days=1))
            self.assertEqual(row['origin_date'],self.bars[i].time.date().isoformat())
            self.assertEqual(len(row['x']),18)
            self.assertEqual(row['x'][-7:],[1.,0.,0.,0.,0.,0.,0.])
            self.assertIsNone(row['entry_price'])
            self.assertFalse(row['entry_allowed'])
        self.assertEqual(self.coverage['unknown_entries'],70)

    def test_prefix_same_features_and_no_future_outcome(self):
        row = daily.build_examples(self.bars[:self.index+1],'SBER',10)[0][-1]
        for key in ('at','origin_date','anchor_price','x'):
            self.assertEqual(row[key],self.row[key])
        for key in ('actual_price','target_date','target_at','y'):
            self.assertIsNone(row[key])
        self.assertTrue(row['label_end_is_placeholder'])
        self.assertEqual(row['reason'],'next_observed_bar_missing')
        self.assertEqual(datetime.fromisoformat(row['label_end']),datetime.fromisoformat(row['at'])+timedelta(days=1))

    def test_future_perturbation_changes_label_not_features(self):
        bars = [replace(b,open=200.,high=201.,low=199.,close=200.2,volume=30000) if i>self.index else b
                for i,b in enumerate(self.bars)]
        row = daily.build_examples(bars,'SBER',10)[0][5]
        self.assertEqual(row['x'],self.row['x'])
        self.assertEqual(row['anchor_price'],self.row['anchor_price'])
        self.assertNotEqual(row['y'],self.row['y'])

    def test_label_uses_target_open_not_close_and_waits_for_bucket_end(self):
        target = self.bars[self.index+1]
        self.assertEqual(self.row['actual_price'],target.open)
        self.assertEqual(self.row['target_at'],target.time.isoformat())
        self.assertEqual(datetime.fromisoformat(self.row['label_end']),target.time+timedelta(days=1))
        self.assertAlmostEqual(self.row['y'],(target.open/self.bars[self.index].close-1)*100)
        changed = [replace(b,close=b.close+.1) if i==self.index+1 else b for i,b in enumerate(self.bars)]
        self.assertEqual(daily.build_examples(changed,'SBER',10)[0][5],self.row)

    def test_exact_twenty_observation_feature_formulas(self):
        bar,prior,x = self.bars[self.index],self.bars[self.index-20:self.index],self.row['x']
        for k,lag in enumerate((1,5,20)):
            self.assertAlmostEqual(x[k],(bar.close/prior[-lag].close-1)*100)
        self.assertAlmostEqual(x[3],(bar.close/bar.open-1)*100)
        self.assertAlmostEqual(x[4],(bar.high-bar.low)/bar.close*100)
        self.assertAlmostEqual(x[5],(bar.close-bar.low)/(bar.high-bar.low))
        self.assertAlmostEqual(x[9],(bar.open/prior[-1].close-1)*100)
        self.assertAlmostEqual(x[10],(prior[-1].open/prior[-2].close-1)*100)

    def test_gap_over_four_days_is_unknown_not_bridge_or_zero(self):
        bars = [replace(b,time=b.time+timedelta(days=5)) if i>self.index else b for i,b in enumerate(self.bars)]
        rows,quality = daily.build_examples(bars,'SBER',10)
        row = rows[5]
        self.assertEqual(row['target_gap_days'],6)
        self.assertEqual(row['reason'],'target_gap_exceeds_four_days')
        self.assertIsNone(row['actual_price'])
        self.assertIsNone(row['y'])
        self.assertEqual(row['x'],self.row['x'])
        self.assertEqual(quality['target_gap_exceeds_four_days'],1)

    def test_four_day_next_observed_gap_is_explicit_and_permitted(self):
        bars = [replace(b,time=b.time+timedelta(days=3)) if i>self.index else b for i,b in enumerate(self.bars)]
        row = daily.build_examples(bars,'SBER',10)[0][5]
        self.assertEqual(row['target_gap_days'],4)
        self.assertEqual(row['reason'],'ok')
        self.assertIsNotNone(row['y'])
        self.assertIsNone(row['entry_price'])

    def test_zero_volume_target_not_shifted_to_positive_record(self):
        bars = [replace(b,volume=0) if i==self.index+1 else b for i,b in enumerate(self.bars)]
        rows,quality = daily.build_examples(bars,'SBER',10)
        row = next(row for row in rows if row['at']==self.row['at'])
        self.assertEqual(row['target_date'],self.row['target_date'])
        self.assertEqual(row['reason'],'target_zero_volume')
        self.assertIsNone(row['y'])
        self.assertEqual(quality['zero_volume_targets'],1)
        self.assertGreater(quality['unavailable_feature_dates'],0)

    def test_zero_volume_prior_not_replaced_by_older_bar(self):
        bars = [replace(b,volume=0) if i==self.index-1 else b for i,b in enumerate(self.bars)]
        rows = daily.build_examples(bars,'SBER',10)[0]
        self.assertFalse(any(row['at']==self.row['at'] for row in rows))

    def test_timezone_preserves_features_and_instants(self):
        bars = [replace(b,time=b.time.astimezone(m.MSK)) for b in self.bars]
        row = daily.build_examples(bars,'SBER',10)[0][5]
        self.assertEqual(row['x'],self.row['x'])
        self.assertEqual(row['origin_date'],self.row['origin_date'])
        self.assertEqual(datetime.fromisoformat(row['at']),datetime.fromisoformat(self.row['at']))
        self.assertEqual(datetime.fromisoformat(row['label_end']),datetime.fromisoformat(self.row['label_end']))

    def test_invalid_grid_price_order_identity_rejected(self):
        for bars in (self.bars+[self.bars[-1]],list(reversed(self.bars)),
                     [replace(self.bars[0],close=float('nan'))],
                     [replace(self.bars[0],time=self.bars[0].time.replace(tzinfo=None))],
                     [self.bars[0],replace(self.bars[1],time=self.bars[1].time-timedelta(hours=1))]):
            with self.subTest(bars=bars[:1]),self.assertRaises(ValueError):
                daily.build_examples(bars,'SBER',10)
        for ticker,lot in (('INVALID',10),('SBER',0),('SBER',True)):
            with self.subTest(ticker=ticker,lot=lot),self.assertRaises(ValueError):
                daily.build_examples([],ticker,lot)

    def test_no_warmup_features_or_fake_last_label(self):
        self.assertEqual(daily.build_examples(history(20),'SBER',1)[0],[])
        self.assertEqual(self.coverage['warmup_dates'],20)
        self.assertEqual(self.coverage['unknown_targets'],1)
        self.assertIsNone(self.rows[-1]['y'])


class MultiyearDailyEvaluationTests(SimpleTestCase):
    def setUp(self):
        self.rows = daily.build_examples(history(),'SBER',10)[0]

    def test_metrics_without_simulator_or_fake_performance(self):
        rows = [dict(self.rows[0],y=1.),dict(self.rows[1],y=-1.),dict(self.rows[2],y=None)]
        with patch.object(m,'paper',side_effect=AssertionError('No trade simulation')):
            result = daily.evaluate(rows,constant_model())
        self.assertEqual(result['samples'],2)
        self.assertEqual(result['eligible_forecasts'],3)
        self.assertEqual(result['unknown_targets'],1)
        self.assertEqual(result['mae_bps'],100.)
        self.assertEqual(result['baseline_mae_bps'],100.)
        self.assertAlmostEqual(result['mean_baseline_mae_bps'],100.)
        self.assertEqual(result['direction_accuracy_pct'],50.)
        self.assertEqual(result['coverage_pct'],50.)
        self.assertEqual(result['interval_width_bps'],150.)
        self.assertEqual(result['abstention_pct'],100.)
        self.assertFalse(result['entry_allowed'])
        for key in ('trades','selected_candidates','unknown_trades','stress_trades','stress_unknown_trades'):
            self.assertEqual(result['paper'][key],0)
        for key in ('net_return_pct','max_drawdown_pct','stress_return_pct','mean_anchor_to_entry_pct'):
            self.assertIsNone(result['paper'][key])

    def test_empty_or_unknown_only_metrics_null_not_zero_profit(self):
        for rows in ([],[dict(self.rows[0],y=None)]):
            result = daily.evaluate(rows,constant_model())
            self.assertIsNone(result['mae_bps'])
            self.assertIsNone(result['direction_accuracy_pct'])
            self.assertIsNone(result['paper']['net_return_pct'])

    def test_nonfinite_calibration_target_rejected(self):
        for key,value in (('interval_abs_pct',float('nan')),('interval_abs_pct',-1.),('train_mean_pct',float('inf'))):
            model = constant_model(); model[key] = value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                daily.evaluate(self.rows,model)
        for target in (float('nan'),float('inf'),True):
            with self.subTest(target=target),self.assertRaises(ValueError):
                daily.evaluate([dict(self.rows[0],y=target)],constant_model())

    def test_zero_direction_denominator_and_no_frozen_mutation(self):
        rows = [dict(self.rows[0],y=0.)]
        before,rules,features = deepcopy(rows),deepcopy(m.RULES),m.FEATURES
        result = daily.evaluate(rows,constant_model(0.))
        self.assertEqual(result['direction_nonzero_samples'],0)
        self.assertIsNone(result['direction_accuracy_pct'])
        self.assertEqual(rows,before)
        self.assertEqual(m.RULES,rules)
        self.assertEqual(m.FEATURES,features)

    def test_holdout_label_not_available_at_current_close(self):
        from . import multiyear_model as study
        row = next(row for row in self.rows if row['origin_date']=='2023-12-30')
        self.assertEqual(datetime.fromisoformat(row['at']).year,2023)
        self.assertEqual(datetime.fromisoformat(row['label_end']).year,2024)
        boundary = datetime(2024,1,1,tzinfo=timezone.utc)
        self.assertFalse(datetime.fromisoformat(row['label_end']) < boundary)
        self.assertEqual(study.split([row])['train'],[])
        earlier = next(row for row in self.rows if row['origin_date']=='2023-12-29')
        self.assertEqual(study.split([earlier])['train'],[earlier])
        self.assertIsNone(row['entry_price'])
