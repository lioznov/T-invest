from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace

from django.test import SimpleTestCase
from .market_provider import demo_snapshot, error_message, MarketUnavailable
from .signals import Candle, analyse, backtest, ema, features, simulate
from .forward_review import review_observations

NOW=datetime(2026,9,26,12,0,tzinfo=timezone.utc)


class SignalRulesTests(TestCase):
    def setUp(self):
        self.snapshot=demo_snapshot("SBER",NOW)

    def test_all_six_conditions_produce_candidate(self):
        result=analyse(self.snapshot,NOW)
        self.assertEqual(result["decision"],"LONG")
        self.assertEqual(result["passed"],6)
        self.assertEqual(result["score"],100)
        self.assertLess(result["plan"]["stop"],result["plan"]["entry"])

    def test_missing_book_history_cannot_be_rescaled_into_buy(self):
        self.snapshot["books"]=self.snapshot["books"][-1:]
        result=analyse(self.snapshot,NOW)
        self.assertEqual(result["decision"],"WAIT")
        self.assertIsNone(result["rules"][-1]["pass"])
        self.assertLess(result["score"],100)
        self.assertIsNone(result["plan"])

    def test_stale_and_closed_market_block_even_high_score(self):
        result=analyse(self.snapshot,NOW+timedelta(minutes=30))
        self.assertEqual(result["decision"],"WAIT")
        self.snapshot["trading"]=False
        result=analyse(self.snapshot,NOW)
        self.assertEqual(result["score"],100)
        self.assertEqual(result["decision"],"WAIT")

    def test_missing_and_unknown_trade_directions_do_not_become_buys(self):
        for t in self.snapshot["trades"]: t["side"]="unknown"
        result=analyse(self.snapshot,NOW)
        self.assertIsNone(result["buy_share"])
        self.assertEqual(result["decision"],"WAIT")

    def test_depth_drop_blocks_entry_without_claiming_spoofing(self):
        self.snapshot["books"][-1]["bids"]=[[p,q*.1] for p,q in self.snapshot["books"][-1]["bids"]]
        result=analyse(self.snapshot,NOW)
        self.assertFalse(result["rules"][-1]["pass"])
        self.assertEqual(result["decision"],"WAIT")

    def test_crossed_or_empty_orderbook_is_not_actionable(self):
        for bids,asks in [([],[]),([[310,100]],[[300,100]])]:
            snapshot=deepcopy(self.snapshot)
            snapshot["books"][-1].update(bids=bids,asks=asks)
            result=analyse(snapshot,NOW)
            self.assertIsNone(result["spread_bps"])
            self.assertEqual(result["decision"],"WAIT")

    def test_future_trade_is_ignored(self):
        self.snapshot["trades"]=[{"time":NOW+timedelta(seconds=1),"quantity":100000,"side":"buy"}]
        self.assertIsNone(analyse(self.snapshot,NOW)["buy_share"])

    def test_ema_and_features_do_not_look_into_future(self):
        candles=self.snapshot["candles"]
        self.assertEqual(features(candles[:300]),features(candles)[:300])
        self.assertEqual(ema([10,20],3),[10,15])

    def test_vwap_resets_at_moscow_midnight(self):
        candles=[Candle(datetime(2026,9,25,20,45,tzinfo=timezone.utc),100,100,100,100,100),
                 Candle(datetime(2026,9,25,21,0,tzinfo=timezone.utc),200,200,200,200,1)]
        self.assertEqual(features(candles)[-1]["vwap"],200)

    def test_empty_data_waits(self):
        result=analyse({"candles":[],"trades":[],"books":[]},NOW)
        self.assertEqual(result["decision"],"WAIT")
        self.assertIsNone(result["plan"])

    def test_exit_signal_requires_three_fresh_bearish_facts(self):
        snapshot=deepcopy(self.snapshot)
        start=snapshot['candles'][-100].close
        for i in range(100):
            old=snapshot['candles'][-100+i]
            price=start*(1-.0015*i)
            snapshot['candles'][-100+i]=Candle(old.time,price*1.001,price*1.002,price*.998,price,old.volume)
        for i,old in enumerate(snapshot['minutes']):
            price=start*(1-.0008*i)
            snapshot['minutes'][i]=Candle(old.time,price*1.001,price*1.002,price*.998,price,old.volume)
        last_price=snapshot['minutes'][-1].close*.999
        snapshot['books'][-1]['bids']=[[last_price*(1-.0002-i*.0003),900] for i in range(8)]
        snapshot['books'][-1]['asks']=[[last_price*(1+.0002+i*.0003),900] for i in range(8)]
        for trade in snapshot['trades']: trade['side']='sell'
        result=analyse(snapshot,NOW)
        self.assertEqual(result['decision'],'EXIT',msg=f"{result['exit_reasons']} / {result['blockers']}")
        self.assertEqual(len(result['exit_reasons']),3)
        self.assertIsNone(result['plan'])
        snapshot['trading']=False
        self.assertEqual(analyse(snapshot,NOW)['decision'],'WAIT')

    def test_completed_candle_remains_fresh_before_next_bar_closes(self):
        now=NOW+timedelta(minutes=14)
        self.assertEqual(analyse(demo_snapshot('SBER',now),now)['decision'],'LONG')


class BacktestTests(TestCase):
    def setUp(self):
        self.candles=demo_snapshot("SBER",NOW)["candles"]

    def test_costs_are_deducted_and_holdout_is_later(self):
        free=backtest(self.candles,0,0)
        paid=backtest(self.candles,10,0)
        self.assertGreater(free["holdout_start"],free["start"])
        for a,b in zip(free["strategies"],paid["strategies"]):
            self.assertEqual(a["holdout"]["trades"],b["holdout"]["trades"])
            self.assertGreaterEqual(a["holdout"]["return_pct"],b["holdout"]["return_pct"])

    def test_stop_wins_if_target_and_stop_hit_in_same_bar(self):
        prefix=[Candle(NOW+timedelta(minutes=15*i),100,100,100,100,100) for i in range(100)]
        candles=prefix+[Candle(NOW+timedelta(minutes=1500),100,110,90,105,100)]
        with patch('dashboard.signals.candle_signal',return_value=True):
            r=simulate(candles,'trend',100,101,0,0)
        self.assertEqual(r['trades'],1)
        self.assertEqual(r['recent_trades'][0]['reason'],'stop')
        self.assertLess(r['return_pct'],0)
        self.assertEqual(r['recent_trades'][0]['entry_time'],candles[100].time.isoformat())

    def test_flat_market_has_no_fabricated_trades(self):
        candles=[Candle(NOW+timedelta(minutes=i*15),100,100,100,100,100) for i in range(250)]
        for s in backtest(candles)["strategies"]:
            self.assertEqual(s["holdout"]["trades"],0)
            self.assertEqual(s["holdout"]["return_pct"],0)
            self.assertIsNone(s["holdout"]["win_rate"])

    def test_insufficient_history_does_not_produce_report(self):
        self.assertEqual(backtest(self.candles[:100])["status"],"insufficient")

    def test_time_exit_is_twelfth_bar_including_entry(self):
        candles=[Candle(NOW+timedelta(minutes=15*i),100,100,100,100,100) for i in range(125)]
        with patch('dashboard.signals.candle_signal',return_value=True):
            result=simulate(candles,'trend',100,125,0,0)
        self.assertEqual(result['recent_trades'][0]['exit_time'],candles[111].time.isoformat())

    def test_new_variants_enter_on_new_signal_at_most_once_per_day(self):
        candles=[Candle(NOW+timedelta(minutes=15*i),100,100,100,100,100) for i in range(260)]
        def pulse(c,feature,strategy):
            index=int((c.time-NOW).total_seconds()//900)
            return index%8<4
        with patch('dashboard.signals.candle_signal',side_effect=pulse):
            old=simulate(candles,'confirmed',100,260,0,0)
            restricted=simulate(candles,'confirmed_once',100,260,0,0)
        self.assertGreater(old['trades'],restricted['trades'])
        days=[datetime.fromisoformat(t['entry_time']).astimezone(timezone(timedelta(hours=3))).date() for t in restricted['recent_trades']]
        self.assertEqual(len(days),len(set(days)))

    def test_raw_errors_and_tokens_never_reach_api_message(self):
        self.assertNotIn("private-token",error_message(RuntimeError("private-token")))
        self.assertEqual(error_message(MarketUnavailable("Нет данных")),"Нет данных")


class ForwardReviewTests(TestCase):
    def test_uses_next_hour_quote_and_both_sides_of_costs(self):
        def row(minutes, decision, score, bid, ask):
            return ((NOW+timedelta(minutes=minutes)).isoformat(),
                    {"review_ready":True,"decision":decision,"score":score,
                     "book":{"bids":[[bid,10]],"asks":[[ask,10]]}})
        rows=[row(0,"LONG",100,100,101),row(30,"WAIT",20,120,121),
              row(60,"WAIT",40,103,104),row(120,"WAIT",40,99,100)]
        free=review_observations(rows,0,0)
        paid=review_observations(rows,5,5)
        self.assertEqual(free["sampled_hours"],3)
        self.assertEqual(free["completed"],2)
        self.assertEqual(free["buy_candidates"]["samples"],1)
        self.assertAlmostEqual(free["buy_candidates"]["average_pct"],(103/101-1)*100)
        self.assertLess(paid["buy_candidates"]["average_pct"],free["buy_candidates"]["average_pct"])
        self.assertEqual(paid["without_pair"],1)

    def test_missing_or_legacy_observations_cannot_create_an_outcome(self):
        def row(minutes, ready=True):
            return ((NOW+timedelta(minutes=minutes)).isoformat(),
                    {"review_ready":ready,"decision":"LONG","score":100,
                     "book":{"bids":[[100,1]],"asks":[[101,1]]}})
        report=review_observations([row(0),row(59),row(71),row(120,False)])
        self.assertEqual(report["completed"],0)
        self.assertEqual(report["buy_candidates"]["samples"],0)
        self.assertEqual(report["observations"],3)

    def test_action_event_is_counted_even_when_it_is_not_first_in_the_hour(self):
        rows=[]
        for minutes,decision in ((0,"WAIT"),(30,"LONG"),(90,"WAIT")):
            rows.append(((NOW+timedelta(minutes=minutes)).isoformat(),
                         {"review_ready":True,"decision":decision,"score":100 if decision=="LONG" else 40,
                          "book":{"bids":[[100,1]],"asks":[[101,1]]}}))
        report=review_observations(rows)
        self.assertEqual(report["candidate_events"],1)
        self.assertEqual(report["buy_candidates"]["samples"],1)


class SignalApiTests(SimpleTestCase):
    def test_pages_render_without_sdk_or_token_calls(self):
        for path in ('/','/watchlist/'):
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertContains(response,"vorby")

    def test_invalid_inputs_rejected(self):
        for path in ('/api/signals/?ticker=<script>', '/api/signals/?mode=bad',
                     '/api/research/?fee=NaN','/api/research/?slip=-1',
                     '/api/forward/?fee=NaN'):
            self.assertEqual(self.client.get(path).status_code,400)

    @patch('dashboard.signal_views.load_review')
    def test_demo_forward_review_does_not_read_real_archive(self, load_review):
        response=self.client.get('/api/forward/?mode=demo&ticker=SBER')
        self.assertEqual(response.json()['status'],'unavailable')
        load_review.assert_not_called()

    @patch('dashboard.signal_views.get_signal')
    def test_normalizes_ticker_and_disables_browser_cache(self,get_signal):
        get_signal.return_value={"status":"loading"}
        response=self.client.get('/api/signals/?ticker=sber&mode=demo')
        get_signal.assert_called_once_with('demo','SBER')
        self.assertEqual(response['Cache-Control'],'no-store')

    def test_cache_coalesces_requests_and_withholds_expired_candidate(self):
        from . import signal_service as service
        with patch.object(service,'ENTRIES',{}),patch.object(service.POOL,'submit') as submit:
            service.get_signal('demo','SBER')
            service.get_signal('demo','SBER')
            submit.assert_called_once()
            service.ENTRIES[('demo','SBER')]['response']={"status":"ok","analysis":{"decision":"LONG"}}
            self.assertEqual(service.get_signal('demo','SBER')['status'],'loading')

    def test_expired_wait_can_remain_visible_during_refresh(self):
        from . import signal_service as service
        with patch.object(service,'ENTRIES',{('live','SBER'):{'finished':0,'pending':True,
                'response':{'status':'ok','analysis':{'decision':'WAIT'}}}}):
            response=service.get_signal('live','SBER')
            self.assertEqual(response['analysis']['decision'],'WAIT')
            self.assertTrue(response['refreshing'])


class ProviderTests(TestCase):
    def test_sdk_schema_imports_supported_by_installed_library(self):
        from t_tech.invest.schemas import CandleInterval, TradeSourceType
        self.assertIsNotNone(CandleInterval.CANDLE_INTERVAL_15_MIN)
        self.assertIsNotNone(TradeSourceType.TRADE_SOURCE_EXCHANGE)

    def test_only_complete_closed_candles_are_used_and_duplicates_removed(self):
        from .market_provider import fetch_candles
        quote=SimpleNamespace(units=100,nano=0)
        def candle(at,complete=True):
            return SimpleNamespace(time=at,is_complete=complete,open=quote,high=quote,low=quote,close=quote,volume=10)
        closed=candle(NOW-timedelta(minutes=15))
        client=Mock()
        client.market_data.get_candles.return_value.candles=[closed,closed,candle(NOW),candle(NOW-timedelta(minutes=30),False)]
        rows=fetch_candles(client,'uid',NOW-timedelta(days=1),NOW,1,15)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].time,closed.time)

    def test_exact_ticker_and_board_required(self):
        from .market_provider import resolve
        client=Mock()
        def instrument(ticker,board):
            return SimpleNamespace(ticker=ticker,class_code=board,instrument_type='share',uid='uid',name='Stock',lot=10)
        client.instruments.find_instrument.return_value.instruments=[instrument('SBERP','TQBR'),instrument('SBER','SPBXM')]
        with self.assertRaises(MarketUnavailable): resolve(client,'SBER')
        client.instruments.find_instrument.return_value.instruments.append(instrument('SBER','TQBR'))
        self.assertEqual(resolve(client,'SBER')['ticker'],'SBER')

    def test_archive_failure_blocks_actionable_candidate(self):
        from . import signal_service as service
        with patch.object(service,'ENTRIES',{('live','SBER'): {}}),patch.object(service,'live_snapshot',return_value=demo_snapshot('SBER',NOW)),patch.object(service,'analyse',return_value=analyse(demo_snapshot('SBER',NOW),NOW)),patch.object(service,'archive',side_effect=OSError):
            service.update(('live','SBER'))
            a=service.ENTRIES[('live','SBER')]['response']['analysis']
            self.assertEqual(a['decision'],'WAIT')
            self.assertIsNone(a['plan'])
