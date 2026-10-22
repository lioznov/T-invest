"""News semantics/context regression tests. Fixtures are not trading results."""
import base64
import json
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch,MagicMock
from urllib.error import URLError
import ssl

from django.test import SimpleTestCase,Client,override_settings

from .news_analysis import analyze,group_events
from .news_context import ContextStore,event,reaction,fetch_calendar,fetch_prices,reaction_payload
from .news_feed import parse_feed,windows_request,request_bytes,FeedError
from .news_sources import SOURCES
from .news_store import payload
from .signals import Candle

NOW = datetime(2026,10,21,8,tzinfo=timezone.utc)


def headline(title='Сбербанк: чистая прибыль выросла на 10 процентов',source='interfax',key='a'):
    publisher = SOURCES[source]
    return {'id':key*64,'title':title,'source':source,'source_name':publisher.name,'source_home':publisher.home,
            'url':publisher.home+key,'tickers':['SBER'],'topics':['results'],'official':publisher.official,
            'published_at':NOW.isoformat(),'first_seen_at':NOW.isoformat(),'observed_at':NOW.isoformat(),
            'author':'Редактор','license_url':publisher.license_url}


def candles(start=NOW,count=61):
    return [Candle(start+timedelta(minutes=i),100+i/10,102+i/10,99+i/10,101+i/10,10) for i in range(count)]


class SemanticsTests(SimpleTestCase):
    def test_growth_is_positive_but_not_verified_or_trade(self):
        row = analyze('Сбербанк: чистая прибыль выросла на 10 процентов',True)
        self.assertEqual(row['tone'],'positive')
        self.assertFalse(row['entry_allowed'])
        self.assertFalse(row['verified_fact'])
        self.assertTrue(row['reasons'])

    def test_decline_is_negative(self):
        self.assertEqual(analyze('Выручка Газпрома сократилась на 20%')['tone'],'negative')

    def test_cancelled_dividends_are_negative(self):
        for title in ('Сбербанк отменил выплату дивидендов','Совет не утвердил дивиденды','Компания отказалась от дивидендов'):
            with self.subTest(title=title):self.assertEqual(analyze(title)['tone'],'negative')

    def test_loss_reduction_positive(self):
        self.assertEqual(analyze('Убытки компании сократились')['tone'],'positive')

    def test_mixed_results_not_forced_positive(self):
        self.assertEqual(analyze('Прибыль снизилась, но выручка выросла')['tone'],'uncertain')

    def test_negation_not_blind_positive(self):
        self.assertEqual(analyze('Компания не увеличила прибыль')['tone'],'uncertain')

    def test_negation_not_blind_negative(self):
        for title in ('Выручка не снизилась','Убытки не увеличились','США не ввели санкции против компании'):
            with self.subTest(title=title):self.assertEqual(analyze(title)['tone'],'uncertain')

    def test_rumour_not_verified(self):
        row=analyze('Слухи: ожидается рост прибыли')
        self.assertEqual(row['kind'],'unconfirmed')
        self.assertFalse(row['verified_fact'])

    def test_forecast_separated_from_report(self):
        self.assertEqual(analyze('Аналитики ожидают рост прибыли Сбера')['kind'],'opinion')

    def test_neutral_does_not_mean_no_price_effect(self):
        row=analyze('Компания назначила дату собрания акционеров')
        self.assertEqual(row['tone'],'neutral')
        self.assertIn('не означает отсутствия влияния',row['caution'])

    def test_duplicate_group_preserves_originals(self):
        a,b=headline(),headline(source='prime',key='b')
        original=deepcopy([a,b]);groups=group_events([a,b])
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]['publications'],2)
        self.assertEqual(groups[0]['publisher_count'],2)
        self.assertEqual([a,b],original)
        self.assertIn('не равно',groups[0]['confirmation_note'])

    def test_numbers_never_fuzzy_merged(self):
        self.assertEqual(len(group_events([headline(),headline('Сбербанк: чистая прибыль выросла на 20 процентов',key='b')])),2)

    def test_opinion_and_event_not_merged(self):
        self.assertEqual(len(group_events([headline(),headline('Аналитики: Сбербанк, чистая прибыль выросла на 10 процентов',key='b')])),2)

    def test_different_company_or_day_not_merged(self):
        for b in ({**headline(key='b'),'tickers':['GAZP']},{**headline(key='b'),'published_at':(NOW+timedelta(days=2)).isoformat()}):
            self.assertEqual(len(group_events([headline(),b])),2)

    def test_official_display_keeps_first_observed_anchor_and_correct_credit(self):
        a=headline(source='finam');b={**headline(source='cbr',key='b'),'author':'Банк России','first_seen_at':(NOW+timedelta(minutes=2)).isoformat()}
        group=group_events([b,a])[0]
        self.assertEqual(group['source'],'cbr')
        self.assertEqual(group['id'],a['id'])
        self.assertEqual(group['first_seen_at'],a['first_seen_at'])
        self.assertEqual(group['author'],'Банк России')
        self.assertEqual(group['license_url'],'')


class ContextArchiveTests(SimpleTestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=ContextStore(Path(self.temp.name)/'news.sqlite3')

    def save(self,title,at=NOW):
        raw=('<rss><channel><item><title>'+title+'</title><link>https://www.interfax.ru/business/1</link></item></channel></rss>').encode()
        rows=parse_feed(raw,SOURCES['interfax'],at)
        token,_=self.store.claim(SOURCES['interfax'],at)
        self.store.finish(SOURCES['interfax'],token,{'status':'ok'},rows,at,600)
        return rows[0]['id']

    def test_archive_query_as_of_not_latest_correction(self):
        key=self.save('Сбербанк: прибыль выросла')
        self.save('Сбербанк: прибыль снизилась',NOW+timedelta(minutes=11))
        self.assertIn('выросла',self.store.items(NOW)[0]['title'])
        self.assertIn('выросла',self.store.original(key)['title'])
        self.assertIn('снизилась',self.store.items(NOW+timedelta(minutes=12))[0]['title'])

    def test_author_survives_archive(self):
        raw=b'<rss><channel><item><title>SBER</title><link>https://www.finam.ru/a</link><author>Author</author></item></channel></rss>'
        rows=parse_feed(raw,SOURCES['finam'],NOW);token,_=self.store.claim(SOURCES['finam'],NOW)
        self.store.finish(SOURCES['finam'],token,{},rows,NOW,600)
        row=self.store.items(NOW)[0]
        self.assertEqual(row['author'],'Author')
        self.assertIn('creativecommons',row['license_url'])

    def test_calendar_not_invented_or_future_snapshot(self):
        row=event('SBER','record',NOW+timedelta(days=2),'Реестр',NOW)
        self.store.put_calendar('SBER',[row],NOW+timedelta(seconds=1))
        self.assertEqual(self.store.calendar(NOW)['events'],[])
        self.assertEqual(self.store.calendar(NOW+timedelta(seconds=2))['events'][0]['precision'],'day')

    def test_failed_refresh_preserves_qualified_old_dates(self):
        row=event('SBER','record',NOW+timedelta(days=2),'Реестр',NOW)
        self.store.put_calendar('SBER',[row],NOW)
        self.store.put_calendar('SBER',[],NOW+timedelta(minutes=1),'unavailable','network')
        result=self.store.calendar(NOW+timedelta(minutes=2))
        self.assertTrue(result['events'][0]['stale'])
        self.assertEqual(result['coverage'][0]['status'],'unavailable')

    def test_calendar_expired_dates_hidden(self):
        row=event('SBER','record',NOW-timedelta(days=2),'Реестр',NOW)
        self.store.put_calendar('SBER',[row],NOW)
        self.assertEqual(self.store.calendar(NOW)['events'],[])

    def test_invalid_calendar_date_has_no_fake_time(self):
        for at in (None,datetime(1970,1,1,tzinfo=timezone.utc),datetime(2026,10,21)):
            self.assertIsNone(event('SBER','record',at,'Реестр',NOW))

    def test_fetch_leases_across_instances(self):
        other=ContextStore(self.store.path)
        self.assertTrue(self.store.job_claim('prices:SBER',NOW,180))
        self.assertFalse(other.job_claim('prices:SBER',NOW,180))
        self.store.job_finish('prices:SBER',NOW,'ok','done',900)
        self.assertFalse(other.job_claim('prices:SBER',NOW+timedelta(seconds=899),180))
        self.assertTrue(other.job_claim('prices:SBER',NOW+timedelta(seconds=900),180))

    def test_only_completed_prices_archived(self):
        self.store.put_prices('SBER',candles(count=2),NOW+timedelta(minutes=1))
        self.assertEqual(len(self.store.prices('SBER')),1)

    def test_price_correction_does_not_silently_overwrite(self):
        self.store.put_prices('SBER',candles(count=1),NOW+timedelta(minutes=2))
        with self.assertRaises(ValueError):self.store.put_prices('SBER',[Candle(NOW,101,102,99,101,10)],NOW+timedelta(minutes=2))
        self.assertEqual(self.store.prices('SBER')[0].open,100)

    def test_invalid_prices_rejected(self):
        for c in (Candle(NOW,0,1,0,1,1),Candle(NOW,100,99,98,100,1),Candle(NOW,float('nan'),101,99,100,1),Candle(NOW.replace(tzinfo=None),100,101,99,100,1)):
            with self.assertRaises(ValueError):self.store.put_prices('SBER',[c],NOW)

    def test_payload_filters_tone_without_training_or_orders(self):
        with override_settings(BASE_DIR=self.temp.name):
            actual=ContextStore();old=self.store;self.store=actual
            self.save('Сбербанк: прибыль выросла')
            self.store=old
            positive=payload(now=NOW,refresh=False,tone='positive')
            negative=payload(now=NOW,refresh=False,tone='negative')
        self.assertEqual(len(positive['items']),1)
        self.assertEqual(negative['items'],[])
        self.assertFalse(positive['entry_allowed'])


class ReactionTests(SimpleTestCase):
    def test_exact_open_to_open_change(self):
        row=reaction(candles(),NOW,NOW+timedelta(hours=2))['horizons'][0]
        self.assertEqual(row['status'],'ok')
        self.assertAlmostEqual(row['change_pct'],1.5)
        self.assertLess(row['after_assumed_cost_pct'],row['change_pct'])

    def test_observation_inside_minute_rounds_up_not_back(self):
        result=reaction(candles(),NOW+timedelta(seconds=1),NOW+timedelta(hours=2))
        self.assertEqual(result['anchor'],(NOW+timedelta(minutes=1)).isoformat())

    def test_future_horizon_pending(self):
        result=reaction(candles(),NOW,NOW+timedelta(minutes=15))
        self.assertEqual(result['horizons'][0]['status'],'pending')
        self.assertIsNone(result['horizons'][0]['change_pct'])

    def test_missing_session_bar_not_interpolated(self):
        rows=candles();del rows[7]
        result=reaction(rows,NOW,NOW+timedelta(hours=2))
        self.assertEqual(result['horizons'][0]['status'],'unavailable')

    def test_zero_volume_anchor_not_tradable(self):
        rows=candles();rows[0]=Candle(NOW,100,101,99,100,0)
        self.assertIsNone(reaction(rows,NOW,NOW+timedelta(hours=2))['horizons'][0]['change_pct'])

    def test_day_change_not_confused_with_intraday_continuity(self):
        rows=[candles(count=1)[0],Candle(NOW+timedelta(days=1),110,111,109,110,10)]
        result=reaction(rows,NOW,NOW+timedelta(days=1,minutes=2))
        self.assertEqual(result['horizons'][2]['status'],'ok')
        self.assertAlmostEqual(result['horizons'][2]['change_pct'],10)
        self.assertFalse(result['entry_allowed'])

    def test_current_book_rejected_if_stale_demo_or_unverified(self):
        store=MagicMock();store.original.return_value={'title':'Original','first_seen_at':NOW.isoformat()};store.prices.return_value=[]
        valid={'mode':'live','analysis':{'data_time':NOW.isoformat(),'data_quality':{'action_blocked':False},'spread_bps':2,'buy_share':.6}}
        for live in ({**valid,'mode':'demo'},{'mode':'live','analysis':{'data_time':NOW.isoformat()}},
                     {'mode':'live','analysis':{**valid['analysis'],'data_time':(NOW-timedelta(seconds=46)).isoformat()}}):
            with patch('dashboard.news_context.ContextStore',return_value=store),patch('dashboard.news_context.schedule'),patch('dashboard.signal_service.get_signal',return_value=live):
                result=reaction_payload('a'*64,'SBER',NOW)
                self.assertEqual(result['current_market']['status'],'unverified')

    def test_current_book_not_historical_and_not_trade(self):
        store=MagicMock();store.original.return_value={'title':'Original','first_seen_at':NOW.isoformat()};store.prices.return_value=[]
        live={'mode':'live','analysis':{'data_time':NOW.isoformat(),'data_quality':{'action_blocked':False},'spread_bps':2,'buy_share':.6}}
        with patch('dashboard.news_context.ContextStore',return_value=store),patch('dashboard.news_context.schedule'),patch('dashboard.signal_service.get_signal',return_value=live):
            result=reaction_payload('a'*64,'SBER',NOW)
        self.assertEqual(result['current_market']['status'],'ok')
        self.assertIn('не исторический',result['current_market']['message'])
        self.assertFalse(result['entry_allowed'])


class ContextDeliveryTests(SimpleTestCase):
    def test_storage_failure_never_fabricates_dates_or_prices(self):
        with patch('dashboard.news_context.ContextStore',side_effect=OSError('storage unavailable')):
            self.assertEqual(Client().get('/api/news/calendar/').json()['status'],'unavailable')
            self.assertEqual(Client().get('/api/news/reaction/',{'ticker':'SBER','id':'a'*64}).json()['status'],'unavailable')

    def test_local_only_and_get_only(self):
        for route in ('/api/news/calendar/','/api/news/reaction/'):
            self.assertEqual(Client(REMOTE_ADDR='203.0.113.1').get(route).status_code,403)
            self.assertEqual(Client().post(route).status_code,405)

    def test_invalid_inputs_rejected(self):
        for query in ({'tone':'LONG'},{'kind':'verified'}):self.assertEqual(Client().get('/api/news/',query).status_code,400)
        self.assertEqual(Client().get('/api/news/reaction/',{'ticker':'SBER','id':'../../'}).status_code,400)
        self.assertEqual(Client().get('/api/news/calendar/',{'ticker':'VTBR'}).json()['status'],'unsupported')

    def test_context_fetch_rejects_unknown_tickers_before_network(self):
        for method in (fetch_calendar,fetch_prices):
            with self.assertRaises(ValueError):method('NONEXISTENT')

    def test_calendar_api_read_only_endpoints(self):
        client=MagicMock();client.instruments.get_dividends.return_value.dividends=[];client.instruments.get_asset_reports.return_value.events=[]
        store=MagicMock();store.job_claim.return_value=True
        with patch('dashboard.market_provider.open_client') as opened:
            opened.return_value.__enter__.return_value=client
            result=fetch_calendar('SBER',store,NOW)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(client.instruments.get_dividends.call_args.kwargs['instrument_id'],'SBER_TQBR')
        client.orders.post_order.assert_not_called()
        client.operations.get_portfolio.assert_not_called()


class WindowsTransportTests(SimpleTestCase):
    def process(self,status=200,headers=None,raw=b'hello'):
        return SimpleNamespace(stdout=json.dumps({'status':status,'headers':headers or {},'body':base64.b64encode(raw).decode()}).encode())

    def test_verified_windows_transport_result(self):
        with patch('dashboard.news_feed.subprocess.run',return_value=self.process()) as run:
            raw,_=windows_request(SOURCES['prime'],'https://1prime.ru/robots.txt',{},100)
        self.assertEqual(raw,b'hello')
        script=run.call_args.args[0][-1]
        self.assertNotIn('SkipCertificateCheck',script)
        self.assertNotIn('ServerCertificateCustomValidationCallback',script)

    def test_windows_cross_host_redirect_not_followed(self):
        with patch('dashboard.news_feed.subprocess.run',return_value=self.process(302,{'Location':'https://evil.example/a'})) as run:
            with self.assertRaises(FeedError):windows_request(SOURCES['prime'],'https://1prime.ru/robots.txt',{},100)
        self.assertEqual(run.call_count,1)

    def test_windows_limit_and_auth_block(self):
        for process in (self.process(raw=b'x'*101),self.process(403)):
            with patch('dashboard.news_feed.subprocess.run',return_value=process):
                with self.assertRaises(FeedError):windows_request(SOURCES['prime'],'https://1prime.ru/robots.txt',{},100)

    def test_certificate_failure_uses_windows_not_insecure_tls(self):
        with patch('dashboard.news_feed.os.name','nt'),patch('dashboard.news_feed.build_opener') as opener,patch('dashboard.news_feed.windows_request',return_value=(b'valid',{})) as native:
            opener.return_value.open.side_effect=URLError(ssl.SSLCertVerificationError('certificate error'))
            self.assertEqual(request_bytes(SOURCES['prime'],'https://1prime.ru/robots.txt')[0],b'valid')
            native.assert_called_once()
