from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase
from django.core.management.base import CommandError
from . import market_ai as v1, market_ai_v2 as ai
from .signals import Candle,MSK
from .management.commands.train_market_ai_v2 import freeze


def history():
    rows=[]
    day=datetime(2026,9,1,10,tzinfo=MSK)
    for n in range(12):
        at=day+timedelta(days=n)
        if at.weekday()>=5:
            continue
        for j in range(32):
            price=100+n*.1+j*.01
            rows.append(Candle(at+timedelta(minutes=15*j),price,price+.12,price-.12,price+.01,1000+j))
    return rows


class MarketAIV2Tests(SimpleTestCase):
    def test_new_features_have_no_future_dependency(self):
        bars=history()
        rows,_=ai.vectors(bars)
        i=next(i for i in range(200,len(rows)) if rows[i] is not None)
        prefix,_=ai.vectors(bars[:i+1])
        self.assertEqual(rows[:i+1],prefix)
        bars[-1]=replace(bars[-1],open=1000,high=1001,low=999,close=1000,volume=1000000)
        self.assertEqual(ai.vectors(bars)[0][:i+1],prefix)
        self.assertEqual(len(rows[i]),len(ai.FEATURES))

    def test_vwap_requires_complete_session_and_prior_clock_observations(self):
        bars=history()
        self.assertTrue(all(x is None for x in ai.vectors(bars[:100])[0]))
        last_day=bars[-1].time.date()
        missing=[b for b in bars if not (b.time.date()==last_day and b.time.hour==10 and b.time.minute==15)]
        rows,_=ai.vectors(missing)
        self.assertTrue(all(x is None for b,x in zip(missing,rows) if b.time.date()==last_day and b.time.hour>=11))

    def test_clock_volume_excludes_own_and_future_volumes(self):
        bars=history()
        index=next(i for i in range(200,len(bars)) if ai.vectors(bars[:i+1])[0][-1] is not None)
        base=ai.vectors(bars)[0][index]
        bars[index]=replace(bars[index],volume=bars[index].volume*2)
        changed=ai.vectors(bars)[0][index]
        from math import log
        self.assertAlmostEqual(changed[16]-base[16],log(2))

    def test_labels_and_timezone_identity_are_unchanged(self):
        bars=history()
        baseline={r['at']:r for r in v1.examples(bars,'SBER',10)}
        rows=ai.examples(bars,'SBER',10)
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r['outcome'],baseline[r['at']]['outcome'])
            self.assertEqual(r['y'],baseline[r['at']]['y'])
        utc=[replace(b,time=b.time.astimezone(timezone.utc)) for b in bars]
        self.assertEqual(rows,ai.examples(utc,'SBER',10))

    def test_boosting_learns_conditional_nonlinearity_and_is_deterministic(self):
        xs=[[i/20,j/20]+[0.]*(len(ai.FEATURES)-2) for i in range(-20,21) for j in range(-20,21)]
        ys=[int(x[0]>(.4 if x[1]>0 else -.4)) for x in xs]
        a,b=ai.fit(xs,ys),ai.fit(xs,ys)
        self.assertEqual(a,b)
        strong=[.8,.8]+[0.]*(len(ai.FEATURES)-2)
        weak=[-.8,.8]+[0.]*(len(ai.FEATURES)-2)
        self.assertGreater(v1.sigmoid(ai.raw(a,strong)),.85)
        self.assertLess(v1.sigmoid(ai.raw(a,weak)),.15)
        self.assertTrue(all(len(t)<=4 for t in a['trees']))

    def test_bad_or_insufficient_training_is_rejected(self):
        x=[[0.]*len(ai.FEATURES)]*20
        for ys in ([0]*20,[0,1]*10):
            with self.assertRaises(ValueError):
                ai.fit(x,ys)

    def test_split_equality_and_projection_preserve_predictions(self):
        tree={'feature':0,'threshold':1.,'left':{'value':.2},'right':{'value':-.3}}
        classifier={'bias':-.4,'trees':[tree],'learning_rate':.08,'prior':.3,'median':[0.]*len(ai.FEATURES)}
        model={'classifier':classifier,'calibrator':{'center':[0.],'scale':[1.],'weights':[0.,1.]}}
        x=[1.]+[0.]*(len(ai.FEATURES)-1)
        self.assertEqual(ai.tree_value(tree,x),.2)
        proxy,rows=ai.projection(model,[{'x':x,'y':1}])
        self.assertAlmostEqual(ai.predict(model,x),v1.predict(proxy,rows[0]['x']))
        self.assertTrue(ai.explanations(model,[2.]+x[1:]))

    def test_one_improved_period_is_not_enough_to_replace_v1(self):
        old={'classification':{'samples':500,'brier':.21,'auc':.55}}
        good={'classification':{'samples':500,'brier':.20,'auc':.56}}
        worse={'classification':{'samples':500,'brier':.22,'auc':.53}}
        self.assertTrue(ai.observation_ready({'test':good,'additional':good},{'test':old,'additional':old}))
        self.assertFalse(ai.observation_ready({'test':good,'additional':worse},{'test':old,'additional':old}))

    def test_protocol_rejects_changes_before_prices(self):
        with TemporaryDirectory() as d,patch('dashboard.management.commands.train_market_ai_v2.source_hashes',return_value={'code':'a'}):
            freeze(Path(d))
            with patch('dashboard.management.commands.train_market_ai_v2.source_hashes',return_value={'code':'b'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d))
