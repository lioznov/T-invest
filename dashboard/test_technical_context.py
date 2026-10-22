from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from math import sin
from django.test import SimpleTestCase,Client
from unittest.mock import patch
from .signals import Candle
from . import technical_context as tech
from pathlib import Path
from tempfile import TemporaryDirectory
from django.core.management.base import CommandError
from .management.commands.study_technical_channels import flags,freeze,describe
from .signals import features
import json
from .management.commands.train_market_ai import digest

NOW=datetime(2026,10,16,13,0,5,tzinfo=timezone.utc)


def bars(n=120):
    end=NOW.replace(second=0,microsecond=0)
    result=[]
    for i in range(n):
        price=100+i*.02+.25*sin(i/3)
        result.append(Candle(end-timedelta(minutes=15*(n-i)),price,price+.15,price-.15,price+.01,1000))
    return result


def book(at=NOW,large=False):
    return {'time':at,'is_consistent':True,
            'bids':[[round(100-i*.01,2),1000 if large and i==8 else 100] for i in range(20)],
            'asks':[[round(100.01+i*.01,2),100] for i in range(20)]}


def snapshot():
    return {'candles':bars(),'books':[book()],'provider_version':'stream_exchange_v1',
            'data_quality':{'action_blocked':False},'trading':True,'instrument':{'ticker':'SBER','lot':10}}


def level(price=99.92):
    return {'key':'s','kind':'support','price':price,'known_at':(NOW-timedelta(hours=1)).isoformat(),'name':'Поддержка'}


class TechnicalChannelTests(SimpleTestCase):
    def test_previous_limits_are_not_changed_by_current_wick(self):
        candles=bars()
        before=tech.channel_rows(candles)
        candles[-1]=replace(candles[-1],high=120,close=115)
        after=tech.channel_rows(candles)
        self.assertEqual(before[-1],after[-1])
        self.assertEqual(after[-1]['range']['upper'],max(c.high for c in candles[-21:-1]))

    def test_channel_history_is_causal(self):
        candles=bars()
        self.assertEqual(tech.channel_rows(candles[:100]),tech.channel_rows(candles)[:100])

    def test_regression_has_parallel_bounds_and_discloses_known_time(self):
        candles=bars()
        r=tech.regression(candles,119)
        self.assertAlmostEqual(r['upper']-r['lower'],r['start_upper']-r['start_lower'])
        self.assertAlmostEqual((r['upper']-r['start_upper'])/60,r['slope'])
        self.assertLessEqual(datetime.fromisoformat(r['known_at']),candles[-1].time)
        self.assertEqual(r['start_time'],candles[-61].time.isoformat())

    def test_unclosed_future_bar_does_not_affect_panel(self):
        s=snapshot()
        before=tech.panel(s,now=NOW)
        last=s['candles'][-1]
        future=replace(last,time=NOW.replace(second=0),open=500,high=501,low=499,close=500)
        self.assertEqual(before,tech.panel({**s,'candles':s['candles']+[future]},now=NOW))

    def test_zero_volume_invalid_history_and_insufficient_context(self):
        s=snapshot()
        self.assertEqual(tech.panel({**s,'candles':s['candles'][:50]},now=NOW)['status'],'insufficient')
        candles=s['candles'].copy();candles[-5]=replace(candles[-5],volume=0)
        self.assertEqual(tech.panel({**s,'candles':candles},now=NOW)['status'],'insufficient')
        candles=s['candles'].copy();candles[-1]=replace(candles[-1],high=float('nan'))
        self.assertEqual(tech.panel({**s,'candles':candles},now=NOW)['status'],'invalid')

    def test_stale_chart_stays_historical_and_never_confirms_orders(self):
        p=tech.panel(snapshot(),now=NOW+timedelta(hours=2))
        self.assertEqual(p['status'],'stale')
        self.assertIsNone(p['state'])
        self.assertNotIn(p['book']['status'],('ok','demo'))
        self.assertFalse(p['entry_allowed'])

    def test_study_flags_need_new_crossing_trend_and_volume(self):
        candles=bars()
        candles[-1]=replace(candles[-1],high=120,close=115,volume=2000)
        ch,fs=tech.channel_rows(candles),features(candles)
        self.assertTrue(all(flags(candles,ch,fs,len(candles)-1).values()))
        candles[-1]=replace(candles[-1],volume=100)
        self.assertFalse(any(flags(candles,tech.channel_rows(candles),features(candles),len(candles)-1).values()))

    def test_experiment_source_changes_are_rejected_before_prices(self):
        model={'tickers':['SBER'],'artifact_sha256':'a'*64}
        with TemporaryDirectory() as d,patch('dashboard.management.commands.study_technical_channels.source_hashes',return_value={'code':'a'}):
            freeze(Path(d),model)
            with patch('dashboard.management.commands.study_technical_channels.source_hashes',return_value={'code':'b'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d),model)

    def test_neural_diagnostic_counts_both_conditions_on_same_row(self):
        rows=[{'probability':.7,'expected_net_pct':-.1,'outcome':{'net_fraction':-.002}},
              {'probability':.3,'expected_net_pct':.2,'outcome':{'net_fraction':.001}}]
        d=describe(rows)
        self.assertEqual(d['passes_probability'],1)
        self.assertEqual(d['passes_expected_return'],1)
        self.assertEqual(d['passes_both'],0)

    def test_portable_research_checks_counts_and_neural_identity(self):
        from . import technical_research as research
        from .management.commands.study_technical_channels import RULES,PERIODS,VARIANTS
        from django.test import override_settings
        with TemporaryDirectory() as d,override_settings(BASE_DIR=Path(d)),patch.object(research,'source_hashes',return_value={'a':'b'}),patch.object(research,'load_model',return_value={'artifact_sha256':'a'*64}):
            root=Path(d)/'dashboard'/'ai_models';root.mkdir(parents=True)
            path=root/'technical-channels-study-v1-fixture.json'
            metric={'trades':0,'unknown_trades':0,'return_pct':0.,'max_realised_drawdown_pct':0.}
            report={'version':tech.VERSION,'at':NOW.isoformat(),'rules':RULES,'periods':PERIODS,
                    'source_sha256':{'a':'b'},'neural_model_id':'a'*64,'entry_allowed':False,'independent_prospective':False,
                    'diagnostics':{p:{'all':{'samples':0}} for p in PERIODS},
                    'results':{p:{k:{c:dict(metric) for c in ('base','double_cost')} for k in VARIANTS} for p in PERIODS}}
            def save(r):
                r['artifact_sha256']=digest({k:v for k,v in r.items() if k!='artifact_sha256'})
                path.write_text(json.dumps(r),encoding='utf-8')
            save(report)
            self.assertEqual(research.load_report()['status'],'ok')
            self.assertIsNone(research.load_report()['results']['test']['range_break']['base']['return_pct'])
            altered=deepcopy(report);altered['results']['test']['range_break']['base']['trades']=True
            save(altered);self.assertEqual(research.load_report()['status'],'unavailable')
            save({**report,'neural_model_id':'b'*64})
            self.assertEqual(research.load_report()['status'],'unavailable')


class TechnicalBookTests(SimpleTestCase):
    def compare(self,s=None,levels=None,mode='live'):
        return tech.compare_book(s or snapshot(),levels or [level()],.1,mode,NOW,NOW+timedelta(minutes=1))

    def test_large_order_is_volume_evidence_not_buy_authorisation(self):
        s=snapshot();s['books']=[book(large=True)]
        r=self.compare(s)['matches'][0]
        self.assertEqual(r['coverage'],'full')
        self.assertEqual(r['lots'],1400)
        self.assertEqual(r['shares'],14000)
        self.assertEqual(r['state'],'visible')
        self.assertFalse(r['entry_allowed'])

    def test_partial_and_outside_depth_are_not_zero_support(self):
        r=self.compare(levels=[level(99.81),level(98)])['matches']
        self.assertEqual(r[0]['coverage'],'partial')
        self.assertEqual(r[0]['state'],'partial')
        self.assertEqual(r[1]['coverage'],'outside')
        self.assertEqual(r[1]['state'],'outside')

    def test_same_price_band_retention_uses_only_prior_known_snapshots(self):
        s=snapshot();s['books']=[book(NOW-timedelta(seconds=40),True),book(NOW-timedelta(seconds=20),True),book()]
        r=self.compare(s)['matches'][0]
        self.assertEqual(r['state'],'reduced')
        self.assertEqual(r['observations'],3)
        self.assertLess(r['retention_fraction'],.65)
        late=level();late['known_at']=(NOW-timedelta(seconds=10)).isoformat()
        r=self.compare(s,[late])['matches'][0]
        self.assertIsNone(r['retention_fraction'])

    def test_quality_trading_and_stale_or_future_book_block_comparison(self):
        for change in ({'provider_version':'legacy'},{'data_quality':{'action_blocked':True}},{'trading':False},
                       {'books':[book(NOW-timedelta(seconds=11))]},{'books':[book(NOW+timedelta(seconds=1))]}):
            self.assertNotEqual(self.compare({**snapshot(),**change})['status'],'ok')
        s=snapshot();s['books'][0]['bids'].reverse()
        self.assertEqual(self.compare(s)['status'],'unavailable')

    def test_demo_comparison_is_explicitly_synthetic(self):
        r=self.compare(mode='demo')
        self.assertEqual(r['status'],'demo')
        self.assertIn('вымышлены',r['message'])

    def test_existing_demo_books_work_without_adding_fake_live_consistency(self):
        from .market_provider import demo_snapshot
        s=demo_snapshot('SBER',NOW)
        p=tech.panel(s,mode='demo',now=NOW)
        self.assertEqual(p['book']['status'],'demo')
        self.assertNotIn('is_consistent',s['books'][-1])
        self.assertNotEqual(tech.panel(s,mode='live',now=NOW)['book']['status'],'ok')

    def test_expiry_removes_current_book_but_keeps_historical_channel(self):
        p=tech.panel(snapshot(),now=NOW)
        payload={'analysis':{'technical':p}}
        changed=tech.expire_payload(payload,NOW+timedelta(seconds=11))['analysis']['technical']
        self.assertEqual(changed['book']['status'],'stale')
        self.assertEqual(changed['book']['matches'],[])
        self.assertEqual(changed['regression'],p['regression'])
        self.assertEqual(p['book']['status'],'ok')
        with patch('dashboard.signal_views.get_signal',return_value=payload):
            body=Client().get('/api/signals/?ticker=SBER').json()
            self.assertEqual(body['analysis']['technical']['book']['matches'],[])

    def test_both_scripts_and_chart_switches_are_present(self):
        page=Client().get('/')
        for text in ('technical_chart.js?v=1','data-structure-layer="channels"','data-structure-layer="book"','id="technical-result"'):
            self.assertContains(page,text)
