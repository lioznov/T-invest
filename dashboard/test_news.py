from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
import io
import json

from django.test import SimpleTestCase,Client,override_settings
from django.core.management import call_command
from django.core.management.base import CommandError

from .news_sources import SOURCES
from .news_feed import (FeedError,parse_feed,plain,classify,safe_url,publication,robots,
                        fetch_feed,request_bytes,RestrictedRedirect,MAX_BYTES)
from .news_store import NewsStore,collect_once,payload

NOW = datetime(2026,10,21,8,tzinfo=timezone.utc)
SOURCE = SOURCES['interfax']


def rss(title='Сбербанк объявил дивиденды',link='https://www.interfax.ru/business/123',date='Wed, 21 Oct 2026 07:00:00 GMT'):
    return f'<rss version="2.0"><channel><item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate><description>FULL ARTICLE MUST NOT BE STORED</description></item></channel></rss>'.encode()


def item(title='Сбербанк объявил дивиденды'):
    return parse_feed(rss(title),SOURCE,NOW)[0]


class FeedTests(SimpleTestCase):
    def test_only_title_link_and_metadata(self):
        row = item()
        self.assertEqual(row['title'],'Сбербанк объявил дивиденды')
        self.assertNotIn('FULL ARTICLE',json.dumps(row))
        self.assertEqual(row['tickers'],['SBER'])
        self.assertEqual(row['topics'],['dividends'])
        self.assertFalse(row['entry_allowed'])

    def test_atom_namespace(self):
        raw = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>MOEX news</title><link href="https://www.moex.com/n1"/><published>2026-10-21T07:00:00Z</published></entry></feed>'
        rows = parse_feed(raw,SOURCES['moex'],NOW)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['date_status'],'publisher')

    def test_html_title_is_text(self):
        self.assertEqual(plain('<b>SBER</b><script>alert(1)</script> &amp; GAZP'),'SBER & GAZP')

    def test_future_or_missing_date_not_fabricated(self):
        for value in ('','2026-10-22T07:00:00Z','nonsense','2026-10-21T07:00:00'):
            published,status = publication(value,NOW)
            self.assertIsNone(published)
            self.assertNotEqual(status,'publisher')

    def test_publisher_timezone_normalized(self):
        at,_ = publication('2026-10-21T10:00:00+03:00',NOW)
        self.assertEqual(at,'2026-10-21T07:00:00+00:00')

    def test_tracking_deduplicates(self):
        a = parse_feed(rss(link='https://www.interfax.ru/business/123?utm_source=test#share'),SOURCE,NOW)[0]
        self.assertEqual(a['id'],item()['id'])

    def test_duplicate_items_collapsed(self):
        raw = rss().replace(b'</channel>',rss().split(b'<channel>')[1].split(b'</channel>')[0]+b'</channel>')
        self.assertEqual(len(parse_feed(raw,SOURCE,NOW)),1)

    def test_wrong_host_or_dangerous_link_rejected(self):
        for url in ('javascript:alert(1)','file:///etc/passwd','https://127.0.0.1/x',
                    'https://www.interfax.ru.evil.test/x','https://user:pass@www.interfax.ru/x',
                    'https://www.interfax.ru:8080/x','https://evil.test/x','https://www.interfax.ru\\@evil.test/x'):
            self.assertIsNone(safe_url(url,SOURCE),url)

    def test_relative_link_keeps_registered_host(self):
        self.assertEqual(safe_url('/business/1',SOURCE),'https://www.interfax.ru/business/1')

    def test_xxe_and_dtd_rejected_including_utf16(self):
        raw = '<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///secret">]><rss><channel>&x;</channel></rss>'
        for data in (raw.encode(),raw.encode('utf-16')):
            with self.assertRaises(FeedError):
                parse_feed(data,SOURCE,NOW)

    def test_html_challenge_not_parsed_as_news(self):
        with self.assertRaises(FeedError):
            parse_feed(b'<html><body>CAPTCHA</body></html>',SOURCE,NOW)

    def test_oversized_feed_rejected(self):
        with self.assertRaises(FeedError):
            parse_feed(b'x'*(MAX_BYTES+1),SOURCE,NOW)

    def test_malformed_xml_rejected(self):
        with self.assertRaises(FeedError):
            parse_feed(b'<rss><channel>',SOURCE,NOW)

    def test_rbc_disabled_at_every_entry_point(self):
        with patch('dashboard.news_feed.request_bytes') as request:
            with self.assertRaises(FeedError):
                fetch_feed(SOURCES['rbc'],{},NOW)
            with self.assertRaises(FeedError):
                parse_feed(rss(),SOURCES['rbc'],NOW)
            request.assert_not_called()

    def test_expired_policy_stops_requests(self):
        with patch('dashboard.news_feed.request_bytes') as request:
            with self.assertRaises(FeedError):
                fetch_feed(SOURCE,{},NOW+timedelta(days=40))
            request.assert_not_called()

    def test_policy_review_date_uses_moscow_not_host_timezone(self):
        self.assertTrue(SOURCE.available(datetime(2026,10,20,22,tzinfo=timezone.utc)))

    def test_tickers_use_word_boundaries(self):
        self.assertEqual(classify('NOTSBER и газпромбанк')['tickers'],[])
        self.assertEqual(classify('NOTSBER и МТСБанк')['tickers'],[])
        self.assertIn('LKOH',classify('Результаты Лукойла по МСФО')['tickers'])

    def test_no_implied_buy_or_probability(self):
        tags = classify('Газпром увеличил прибыль и дивиденды')
        self.assertFalse(tags['entry_allowed'])
        self.assertNotIn('probability',tags)
        self.assertNotIn('decision',tags)

    def test_robots_disallow_prevents_feed_fetch(self):
        with patch('dashboard.news_feed.request_bytes',return_value=(b'User-agent: *\nDisallow: /rss.asp',{})) as request:
            with self.assertRaises(FeedError) as ctx:
                fetch_feed(SOURCE,{},NOW)
            self.assertEqual(ctx.exception.status,'robots_blocked')
            self.assertEqual(request.call_count,1)

    def test_unknown_robots_fails_closed(self):
        with patch('dashboard.news_feed.request_bytes',side_effect=FeedError('unavailable','no network')) as request:
            with self.assertRaises(FeedError):
                fetch_feed(SOURCE,{},NOW)
            self.assertEqual(request.call_count,1)

    def test_robots_404_allows_registered_feed(self):
        with patch('dashboard.news_feed.request_bytes',side_effect=[FeedError('not_found','404'),(rss(),{})]):
            items,_,_ = fetch_feed(SOURCE,{},NOW)
            self.assertEqual(len(items),1)

    def test_robots_cached_and_conditional_feed_request(self):
        cached = {'robots_text':'User-agent: *\nAllow: /','robots_checked':NOW.isoformat(),'etag':'"test"'}
        with patch('dashboard.news_feed.request_bytes',return_value=(None,{})) as request:
            items,_,_ = fetch_feed(SOURCE,cached,NOW)
            self.assertIsNone(items)
            self.assertEqual(request.call_count,1)
            self.assertEqual(request.call_args.args[2]['If-None-Match'],'"test"')

    def test_crawl_delay_defers_second_request(self):
        with patch('dashboard.news_feed.request_bytes',return_value=(b'User-agent: *\nCrawl-delay: 30\nAllow: /',{})) as request:
            items,metadata,interval = fetch_feed(SOURCE,{},NOW)
            self.assertIsNone(items)
            self.assertTrue(metadata['deferred'])
            self.assertGreaterEqual(interval,600)
            self.assertEqual(request.call_count,1)

    def test_redirect_outside_source_or_downgrade_rejected(self):
        redirect = RestrictedRedirect(SOURCE)
        for url in ('https://127.0.0.1/x','https://evil.test/x','http://www.interfax.ru/x'):
            with self.assertRaises(FeedError):
                redirect.redirect_request(Request(SOURCE.feed),None,302,'',{},url)

    def test_http_429_retry_after_respected(self):
        error = HTTPError(SOURCE.feed,429,'limit',{'Retry-After':'3600'},None)
        with patch('dashboard.news_feed.build_opener') as builder:
            builder.return_value.open.side_effect = error
            with self.assertRaises(FeedError) as ctx:
                request_bytes(SOURCE,SOURCE.feed)
        self.assertEqual(ctx.exception.retry,3600)
        self.assertEqual(ctx.exception.status,'rate_limited')

    def test_http_403_is_not_bypassed(self):
        with patch('dashboard.news_feed.build_opener') as builder:
            builder.return_value.open.side_effect = HTTPError(SOURCE.feed,403,'blocked',{},None)
            with self.assertRaises(FeedError) as ctx:
                request_bytes(SOURCE,SOURCE.feed)
        self.assertEqual(ctx.exception.status,'access_denied')


class StoreTests(SimpleTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = NewsStore(Path(self.temp.name)/'news.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def save(self,rows,now=NOW,source=SOURCE):
        token,_ = self.store.claim(source,now)
        self.assertIsNotNone(token)
        self.assertTrue(self.store.finish(source,token,{'status':'ok','last_success':now.isoformat()},rows,now,600))

    def test_first_seen_is_receipt_not_publication(self):
        self.save([item()])
        row = self.store.items(NOW)[0]
        self.assertEqual(row['first_seen_at'],NOW.isoformat())
        self.assertNotEqual(row['published_at'],row['first_seen_at'])
        self.assertEqual(self.store.as_known(NOW-timedelta(seconds=1)),[])

    def test_duplicate_fetch_preserves_first_seen(self):
        self.save([item()])
        later = NOW+timedelta(minutes=11)
        self.save([item()],later)
        row = self.store.items(later)[0]
        self.assertEqual(row['first_seen_at'],NOW.isoformat())
        self.assertEqual(row['last_seen_at'],later.isoformat())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM news_revisions').fetchone()[0],1)

    def test_title_changes_preserve_historical_version_and_reversion(self):
        a = item()
        self.save([a])
        b = {**a,'title':'Исправление: дивиденды Сбера не утверждены'}
        self.save([b],NOW+timedelta(minutes=11))
        self.save([a],NOW+timedelta(minutes=22))
        self.assertEqual(self.store.as_known(NOW)[0]['title'],a['title'])
        self.assertEqual(self.store.as_known(NOW+timedelta(minutes=12))[0]['title'],b['title'])
        self.assertEqual(self.store.as_known(NOW+timedelta(minutes=23))[0]['title'],a['title'])

    def test_cross_process_lease_and_poll_limit(self):
        token,_ = self.store.claim(SOURCE,NOW)
        other = NewsStore(self.store.path)
        self.assertIsNone(other.claim(SOURCE,NOW))
        self.store.finish(SOURCE,token,{},[],NOW,600)
        self.assertIsNone(other.claim(SOURCE,NOW+timedelta(seconds=599)))
        self.assertIsNotNone(other.claim(SOURCE,NOW+timedelta(seconds=600)))

    def test_expired_worker_cannot_commit_over_new_lease(self):
        token,_ = self.store.claim(SOURCE,NOW)
        new,_ = self.store.claim(SOURCE,NOW+timedelta(seconds=181))
        self.assertFalse(self.store.finish(SOURCE,token,{},[item()],NOW,600))
        self.assertTrue(self.store.finish(SOURCE,new,{},[],NOW+timedelta(seconds=181),600))

    def test_filter_company_topic_and_query(self):
        self.save([item()])
        self.assertEqual(len(self.store.items(NOW,ticker='SBER',topic='dividends',query='сбер')),1)
        self.assertEqual(self.store.items(NOW,ticker='GAZP'),[])
        self.assertEqual(self.store.items(NOW,topic='results'),[])

    def test_macro_opt_in(self):
        self.save([item('Банк России сохранил ключевую ставку')])
        self.assertEqual(self.store.items(NOW,ticker='SBER'),[])
        self.assertEqual(len(self.store.items(NOW,ticker='SBER',include_macro=True)),1)

    def test_market_feed_hides_unrelated_headlines_without_deleting(self):
        self.save([item('Вечером можно увидеть метеорный поток')])
        self.assertEqual(self.store.items(NOW),[])
        self.assertEqual(len(self.store.items(NOW,market_only=False)),1)
        self.assertEqual(len(self.store.as_known(NOW)),1)

    def test_network_failure_keeps_old_headlines(self):
        self.save([item()])
        with patch('dashboard.news_store.fetch_feed',side_effect=FeedError('unavailable','network')):
            collect_once(self.store,[SOURCE],NOW+timedelta(minutes=11))
        self.assertEqual(len(self.store.items(NOW+timedelta(minutes=11))),1)
        self.assertEqual(self.store.source_states()[SOURCE.key]['status'],'unavailable')

    def test_304_preserves_items(self):
        self.save([item()])
        with patch('dashboard.news_store.fetch_feed',return_value=(None,{},600)):
            collect_once(self.store,[SOURCE],NOW+timedelta(minutes=11))
        self.assertEqual(len(self.store.items(NOW+timedelta(minutes=11))),1)

    def test_backoff_not_defeated_by_browser_refresh(self):
        with patch('dashboard.news_store.fetch_feed',side_effect=FeedError('rate_limited','limit',3600)) as fetch:
            collect_once(self.store,[SOURCE],NOW)
            collect_once(self.store,[SOURCE],NOW+timedelta(minutes=11))
            self.assertEqual(fetch.call_count,1)

    def test_disabled_source_has_no_network(self):
        with patch('dashboard.news_feed.request_bytes') as request:
            collect_once(self.store,[SOURCES['rbc']],NOW)
            request.assert_not_called()
        self.assertEqual(self.store.source_states()['rbc']['status'],'policy_blocked')

    def test_public_payload_does_not_expose_robots_or_full_text(self):
        with override_settings(BASE_DIR=self.temp.name),patch('dashboard.news_store.schedule'):
            data = payload(now=NOW,refresh=False)
        self.assertFalse(data['entry_allowed'])
        self.assertNotIn('robots_text',json.dumps(data))


class NewsDeliveryTests(SimpleTestCase):
    def test_page_and_sidebar(self):
        response = Client().get('/news/')
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Новости рынка')
        self.assertContains(response,'news-filters')

    def test_public_or_forwarded_client_is_rejected(self):
        client = Client(REMOTE_ADDR='203.0.113.1',HTTP_X_FORWARDED_FOR='127.0.0.1')
        self.assertEqual(client.get('/api/news/').status_code,403)
        self.assertEqual(client.get('/news/').status_code,403)

    def test_get_only(self):
        self.assertEqual(Client().post('/api/news/').status_code,405)

    def test_api_filter_validation(self):
        for query in ({'source':'https://127.0.0.1'},{'ticker':'../../'},{'topic':'buy'},{'macro':'yes'},{'q':'a'*101}):
            self.assertEqual(Client().get('/api/news/',query).status_code,400)

    def test_api_passes_safe_filters_and_no_cache(self):
        with patch('dashboard.news_views.payload',return_value={'status':'ok','items':[],'entry_allowed':False}) as data:
            response = Client().get('/api/news/',{'ticker':'sber','source':'cbr','macro':'1'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response['Cache-Control'],'no-store')
        self.assertEqual(data.call_args.kwargs['ticker'],'SBER')
        self.assertTrue(data.call_args.kwargs['include_macro'])

    def test_command_rejects_aggressive_polling(self):
        for kwargs in ({'interval':1},{'cycles':0},{'cycles':145}):
            with self.assertRaises(CommandError):
                call_command('collect_news',stdout=io.StringIO(),**kwargs)
