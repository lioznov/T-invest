"""Synthetic-only checks of multi-year information boundaries."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from django.test import SimpleTestCase
from . import morning_model as m
from . import multiyear_model as study
from .signals import Candle


def history(days=80):
    start=datetime(2022,3,1,10,tzinfo=m.MSK)
    bars=[]
    for day in range(days):
        for j in range(56):
            p=100+day*.02+j*.001
            bars.append(Candle(start+timedelta(days=day,minutes=15*j),p,p+.1,p-.1,p+.01,1000+j))
    return bars


class MultiyearModelTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bars=history()
        cls.rows,cls.quality=study.build_intraday(cls.bars,'SBER',10)

    def test_all_three_clocks_features_and_exact_next_morning(self):
        self.assertEqual(set(self.rows),{'control','main_hour','evening_hour'})
        for v in study.VARIANTS:
            self.assertTrue(self.rows[v['id']])
            for row in self.rows[v['id']]:
                at=datetime.fromisoformat(row['at'])
                self.assertLess(at.weekday(),4)
                self.assertEqual(at.strftime('%H:%M'),v['cutoff'])
                self.assertEqual(len(row['x']),18)
                self.assertEqual(row['x'][-7:],[1.,0.,0.,0.,0.,0.,0.])
                self.assertIsNotNone(row['entry_price'])
                target=datetime.fromisoformat(row['target_at'])
                self.assertEqual(target,m.clock(at.date()+timedelta(days=1),10))
                self.assertEqual(datetime.fromisoformat(row['label_end']),target+timedelta(minutes=15))

    def test_prefix_preserves_features_but_has_no_future_outcomes(self):
        for key,rows in self.rows.items():
            row=rows[4];at=datetime.fromisoformat(row['at'])
            prefix=[b for b in self.bars if b.time<at]
            current=next(r for r in study.build_intraday(prefix,'SBER',10)[0][key] if r['at']==row['at'])
            self.assertEqual(current['x'],row['x'])
            self.assertEqual(current['anchor_price'],row['anchor_price'])
            self.assertIsNone(current['entry_price']);self.assertIsNone(current['actual_price'])

    def test_future_perturbation_cannot_change_known_features(self):
        for key,rows in self.rows.items():
            row=rows[3];at=datetime.fromisoformat(row['at'])
            changed=[replace(b,open=999.,high=1000.,low=998.,close=999.5,volume=3333)
                     if b.time>=at else b for b in self.bars]
            current=next(r for r in study.build_intraday(changed,'SBER',10)[0][key] if r['at']==row['at'])
            self.assertEqual(current['x'],row['x'])
            self.assertNotEqual(current['y'],row['y'])

    def test_early_years_do_not_require_nonexistent_evening_sessions(self):
        no_evening=[b for b in self.bars if b.time.hour<19]
        result,_=study.build_intraday(no_evening,'SBER',10)
        self.assertEqual(result['control'],self.rows['control'])
        self.assertEqual(result['main_hour'],self.rows['main_hour'])
        self.assertEqual(result['evening_hour'],[])

    def test_missing_entry_and_exact_target_do_not_shift_to_later_bars(self):
        for key,rows in self.rows.items():
            row=rows[3];at=datetime.fromisoformat(row['at'])
            entry=study.at_clock(at.date(),next(v['entry'] for v in study.VARIANTS if v['id']==key))
            target=datetime.fromisoformat(row['target_at'])
            edited=[b for b in self.bars if b.time not in (entry,target)]
            current=next(r for r in study.build_intraday(edited,'SBER',10)[0][key] if r['at']==row['at'])
            self.assertEqual(current['x'],row['x'])
            self.assertIsNone(current['entry_price']);self.assertIsNone(current['y'])

    def test_weekend_prices_do_not_change_weekday_reference_features(self):
        changed=[replace(b,open=999.,high=1000.,low=998.,close=999.5,volume=3333)
                 if b.time.weekday()>=5 else b for b in self.bars]
        self.assertEqual(study.build_intraday(changed,'SBER',10)[0],self.rows)

    def test_timezone_conversion_is_invariant(self):
        converted=[replace(b,time=b.time.astimezone(timezone.utc)) for b in self.bars]
        self.assertEqual(study.build_intraday(converted,'SBER',10),(self.rows,self.quality))

    def test_bad_stock_lot_candles_are_rejected(self):
        for ticker,lot in (('UNKNOWN',1),('SBER',True),('SBER',0)):
            with self.assertRaises(ValueError):study.build_intraday(self.bars,ticker,lot)
        for bars in (self.bars+[self.bars[-1]],list(reversed(self.bars)),
                     [replace(self.bars[0],close=float('nan'))]):
            with self.assertRaises(ValueError):study.build_intraday(bars,'SBER',10)

    def test_year_splits_and_label_availability_purge(self):
        template=self.rows['control'][0]
        train=dict(template,at='2023-12-28T17:45:00+03:00',label_end='2023-12-29T10:15:00+03:00')
        cross=dict(template,at='2023-12-31T17:45:00+03:00',label_end='2024-01-01T10:15:00+03:00')
        cal=dict(template,at='2024-06-03T17:45:00+03:00',label_end='2024-06-04T10:15:00+03:00')
        test=dict(template,at='2025-06-03T17:45:00+03:00',label_end='2025-06-04T10:15:00+03:00')
        late=dict(template,at='2026-06-03T17:45:00+03:00',label_end='2026-06-04T10:15:00+03:00')
        self.assertEqual(study.split([train,cross,cal,test,late]),
                         {'train':[train],'calibration':[cal],'test':[test],'later':[late]})

    def test_information_horizon_must_be_aware_and_forward(self):
        template=self.rows['control'][0]
        for field,value in (('at','2023-12-28T17:45:00'),('label_end',template['at'])):
            with self.assertRaises(ValueError):study.split([{**template,field:value}])
