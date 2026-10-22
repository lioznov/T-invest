import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from . import neural_trader as nn, portfolio_replay as portfolio, market_ai as linear
from .management.commands.train_neural_trader import freeze, paper_ready
from .test_market_ai_v2 import history


def row(ticker='SBER', hour=10, minute=30, expected=.2, exit_price=101, unknown=False):
    at = datetime(2026,9,1,hour,minute,tzinfo=timezone.utc)
    end = (at+timedelta(hours=1)).isoformat()
    outcome = {'entry':100.,'stop':99.,'exit':exit_price,'exit_at':end,'label_end':end}
    return {'ticker':ticker,'at':at.isoformat(),'label_end':end,'selected':True,'expected_net_pct':expected,
            'entry':100.,'stop':99.,'lot':10,'outcome':None if unknown else outcome}


class NeuralTraderTests(SimpleTestCase):
    def test_network_learns_both_heads_repeatably(self):
        xs = [[i/10,j/10]+[0.]*(len(nn.FEATURES)-2) for i in range(-12,13) for j in range(-12,13)]
        returns = [.6*x[0]+.3*x[0]*x[1] for x in xs]
        ys = [int(r>0) for r in returns]
        a, b = nn.fit(xs,ys,returns), nn.fit(xs,ys,returns)
        self.assertEqual(a,b)
        high, low = [1.,1.]+[0.]*18, [-1.,1.]+[0.]*18
        self.assertGreater(nn.raw(a,high)[0],nn.raw(a,low)[0])
        self.assertGreater(nn.raw(a,high)[1]-nn.raw(a,low)[1],.6)
        model = {'network':a,'calibrator':{'center':[0.],'scale':[1.],'weights':[0.,1.]},'return_offset':0.}
        self.assertTrue(nn.valid_model(model))
        self.assertTrue(nn.candidate(model,high))
        self.assertFalse(nn.candidate(model,low))
        proxy, rows = nn.projection(model,[{'x':high,'y':1}])
        self.assertAlmostEqual(nn.predict(model,high),linear.predict(proxy,rows[0]['x']))
        broken = copy.deepcopy(model)
        broken['network']['layers'][0]['weights'][0].append(0.)
        self.assertFalse(nn.valid_model(broken))

    def test_invalid_training_and_inference_rejected(self):
        with self.assertRaises(ValueError):
            nn.fit([[0.]*20]*40,[0]*40,[0.]*40)
        with self.assertRaises(ValueError):
            nn.raw({},[float('nan')]*20)

    def test_replay_features_and_candidates_never_read_future(self):
        bars = history()
        rows = nn.replay_rows(bars,'SBER',10)
        first = rows[0]
        cut = next(i for i,b in enumerate(bars) if (b.time+timedelta(minutes=15)).astimezone(timezone.utc).isoformat()==first['at'])
        prefix = nn.replay_rows(bars[:cut+1],'SBER',10)[-1]
        self.assertEqual(first['x'],prefix['x'])
        self.assertIsNone(prefix['outcome'])
        self.assertIsNone(prefix['entry'])

    def test_freeze_refuses_changed_method_before_prices(self):
        with TemporaryDirectory() as d, patch('dashboard.management.commands.train_neural_trader.source_hashes',return_value={'code':'a'}):
            freeze(Path(d))
            with patch('dashboard.management.commands.train_neural_trader.source_hashes',return_value={'code':'b'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d))

    def test_zero_trades_and_unknowns_cannot_pass_readiness(self):
        p = {'trades':40,'unknown_trades':0,'return_pct':1.,'max_realised_drawdown_pct':2.}
        good = {'classification':{'brier':.2,'baseline_brier':.25},'base':p,'double_cost':p}
        self.assertTrue(paper_ready({'test':good,'additional':good}))
        for change in ({'trades':0},{'unknown_trades':1,'return_pct':None},{'return_pct':-1.}):
            bad = {**good,'base':{**p,**change}}
            self.assertFalse(paper_ready({'test':good,'additional':bad}))


class SharedPortfolioTests(SimpleTestCase):
    def test_simultaneous_candidates_share_cash_and_risk(self):
        rows = [row(f'T{i}',expected=i/10) for i in range(7)]
        result = portfolio.simulate(rows)
        self.assertLessEqual(result['max_committed_cash'],100000)
        self.assertLessEqual(result['max_positions'],4)
        self.assertTrue(all(t['shares']%10==0 for t in result['trade_log']))
        self.assertLessEqual(sum(t['planned_loss'] for t in result['trade_log']),1500)
        self.assertEqual(result['trade_log'][0]['ticker'],'T6')

    def test_future_profit_does_not_fund_simultaneous_entries(self):
        a = portfolio.simulate([row('A',exit_price=200),row('B',expected=.3)])
        b = portfolio.simulate([row('A',exit_price=50),row('B',expected=.3)])
        self.assertEqual({t['ticker']:t['shares'] for t in a['trade_log']},{t['ticker']:t['shares'] for t in b['trade_log']})

    def test_unknown_result_is_withheld_and_overlap_blocked(self):
        rows = [row(unknown=True),row(hour=10,minute=45)]
        result = portfolio.simulate(rows)
        self.assertIsNone(result['return_pct'])
        self.assertIsNone(result['final_capital'])
        self.assertEqual(result['unknown_trades'],1)
        self.assertEqual(result['skipped']['overlap'],1)

    def test_unknown_entry_not_fabricated(self):
        candidate = {**row(unknown=True),'entry':None,'stop':None}
        result = portfolio.simulate([candidate])
        self.assertEqual(result['trades'],0)
        self.assertEqual(result['unknown_trades'],1)
        self.assertIsNone(result['return_pct'])

    def test_daily_gap_loss_blocks_new_positions(self):
        result = portfolio.simulate([row(exit_price=80),row('B',hour=11,minute=45)])
        self.assertEqual(result['trades'],1)
        self.assertEqual(result['skipped']['daily_loss'],1)
        self.assertGreater(result['max_realised_drawdown_pct'],1.5)

    def test_cost_stress_and_cash_baseline(self):
        ordinary, stressed = portfolio.simulate([row()]), portfolio.simulate([row()],2)
        self.assertLess(stressed['return_pct'],ordinary['return_pct'])
        no_trade = portfolio.simulate([{**row(),'selected':False}])
        self.assertEqual(no_trade['trades'],0)
        self.assertEqual(no_trade['return_pct'],0)
