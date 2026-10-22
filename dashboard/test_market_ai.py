from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase
from django.core.management.base import CommandError
from .signals import Candle,MSK
from . import market_ai as ai
from .management.commands.train_market_ai import freeze


def candles():
    at=datetime(2026,10,13,10,15,tzinfo=MSK)
    return [Candle(at+timedelta(minutes=15*(i-100)),100,100.2,99.8,100,1000) for i in range(105)]


class MarketAITests(SimpleTestCase):
    def model(self):
        xs=[[float(i)] for i in range(-40,40)]
        classifier=ai.fit(xs,[int(x[0]>0) for x in xs])
        calibrator={'center':[0.],'scale':[1.],'weights':[0.,1.]}
        return {'classifier':classifier,'calibrator':calibrator}

    def test_real_fitting_is_deterministic_and_learns_label_relation(self):
        a,b=self.model(),self.model()
        self.assertEqual(a,b)
        self.assertGreater(ai.predict(a,[30]),.85)
        self.assertLess(ai.predict(a,[-30]),.15)

    def test_single_class_or_nan_training_is_rejected(self):
        with self.assertRaises(ValueError):
            ai.fit([[0]]*20,[0]*20)
        with self.assertRaises(ValueError):
            ai.fit([[float('nan')]]*20,[0,1]*10)

    def test_feature_prefix_does_not_use_future_prices(self):
        bars=candles()
        full,_=ai.vectors(bars)
        prefix,_=ai.vectors(bars[:101])
        self.assertEqual(full[:101],prefix)
        bars[-1]=replace(bars[-1],open=1000,high=1001,low=999,close=1000)
        self.assertEqual(ai.vectors(bars)[0][:101],prefix)

    def test_flat_price_is_labelled_loss_after_costs(self):
        rows=ai.examples(candles(),'SBER',10)
        self.assertEqual(rows[0]['y'],0)
        self.assertLess(rows[0]['outcome']['net_fraction'],0)

    def test_future_gap_is_unknown_not_a_zero_loss(self):
        bars=candles()
        bars[103]=replace(bars[103],volume=0)
        row=ai.examples(bars,'SBER',10)[0]
        self.assertIsNone(row['y'])
        self.assertIsNone(row['outcome'])

    def test_entry_bar_stop_and_bad_gap_are_real_labels(self):
        bars=candles()
        bars[101]=replace(bars[101],low=98)
        row=ai.examples(bars,'SBER',10)[0]
        self.assertEqual(row['reason'],'stop')
        self.assertEqual(row['y'],0)
        bars=candles()
        bars[102]=replace(bars[102],open=98,low=97,high=99,close=98)
        self.assertEqual(ai.examples(bars,'SBER',10)[0]['reason'],'gap_stop')

    def test_label_crossing_split_boundary_is_purged(self):
        row={'at':'2026-07-31T23:30:00+00:00','outcome':{'label_end':'2026-08-01T00:30:00+00:00'}}
        self.assertEqual(ai.split([row])['train'],[])

    def test_auc_ties_and_baseline_are_honest(self):
        model={'classifier':{'center':[0],'scale':[1],'weights':[0,0],'prior':.5},
               'calibrator':{'center':[0],'scale':[1],'weights':[0,1]}}
        m=ai.metrics(model,[{'x':[0],'y':0},{'x':[0],'y':1},{'x':[0],'y':None}])
        self.assertEqual(m['auc'],.5)
        self.assertEqual(m['brier'],.25)
        self.assertEqual(m['unknown'],1)

    def test_no_trades_cannot_pass_profit_gate(self):
        empty={'classification':{'brier':.2,'baseline_brier':.25},
               'base':{'trades':0,'unknown_trades':0,'return_pct':0},'double_cost':{'trades':0,'unknown_trades':0,'return_pct':0}}
        self.assertFalse(ai.paper_ready({'test':empty,'additional':empty}))

    def test_training_freeze_precedes_prices(self):
        with TemporaryDirectory() as d,patch('dashboard.management.commands.train_market_ai.source_hashes',return_value={'code':'a'}):
            freeze(Path(d))
            with patch('dashboard.management.commands.train_market_ai.source_hashes',return_value={'code':'b'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d))
