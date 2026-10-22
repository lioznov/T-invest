"""Scalability tests with synthetic data only; no broker or Telegram side effects."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch
from django.core.management import call_command
from django.test import override_settings

from .watch_universe import normalize, collection_group, AnalysisQueue, COLLECTION_BATCH, ROTATION_SECONDS
from .wide_watch import WideArchive, supervise
from .continuous_watch import requests, load_health, RESERVE_BYTES
from .microstructure_archive import StreamControl, validate_options
from .market_provider import MarketUnavailable
from .test_microstructure_archive import NOW, INSTRUMENT


class UniverseTests(TestCase):
    def test_unrestricted_count_normalization_and_order_preserving_dedup(self):
        values = [f'STOCK{i}' for i in range(10000)]
        self.assertEqual(normalize(values), values)
        self.assertEqual(normalize(['sber', 'GAZP', 'SBER']), ['SBER', 'GAZP'])
        for value in ([], 'SBER', [None], ['<SBER>'], ['A;cmd'], ['A'*16]):
            with self.assertRaises(ValueError): normalize(value)

    def test_groups_cover_all_without_truncation_and_wrap_after_last(self):
        symbols = [f'S{i}' for i in range(123)]
        groups = [collection_group(symbols, n*ROTATION_SECONDS) for n in range(4)]
        self.assertEqual([len(item[0]) for item in groups], [50, 50, 23, 50])
        self.assertEqual(sum((item[0] for item in groups[:3]), []), symbols)
        self.assertEqual(groups[0][0], groups[3][0])
        self.assertEqual(collection_group(symbols, ROTATION_SECONDS-1)[1], 0)
        self.assertEqual(collection_group(symbols, ROTATION_SECONDS)[1], 1)

    def test_queue_bounds_pending_and_fairly_reaches_tail_beyond_cache_40(self):
        queue = AnalysisQueue(); observed = []
        values = [f'S{i}' for i in range(121)]
        fetch = Mock(return_value={'status': 'ok'})
        for at in range(41):
            before = fetch.call_count
            queue.step(values, fetch, lambda t, p: observed.append(t), at*5)
            self.assertLessEqual(fetch.call_count-before, 3)
            self.assertLessEqual(len(queue.pending), 3)
        self.assertEqual(set(observed), set(values))
        self.assertEqual(observed[:3], values[:3])

    def test_loading_queue_waits_before_adding_another_batch_then_times_out(self):
        queue = AnalysisQueue(); observed = []
        values = [f'S{i}' for i in range(100)]
        fetch = Mock(return_value={'status': 'loading'})
        queue.step(values, fetch, lambda *args: observed.append(args), 0)
        queue.step(values, fetch, lambda *args: observed.append(args), 89)
        self.assertEqual(set(queue.pending), set(values[:3]))
        self.assertFalse(observed)
        queue.step(values, fetch, lambda *args: observed.append(args), 90)
        self.assertEqual(len(observed), 3)
        queue.step(values, fetch, lambda *args: observed.append(args), 95)
        self.assertEqual(set(queue.pending), set(values[3:6]))

    def test_group_change_removes_old_pending_and_empty_group_does_not_fetch(self):
        queue = AnalysisQueue(); fetch = Mock(return_value={'status': 'loading'})
        queue.step(['SBER', 'GAZP'], fetch, Mock(), 0)
        queue.step(['LKOH'], fetch, Mock(), 5)
        self.assertEqual(set(queue.pending), {'LKOH'})
        before = fetch.call_count
        queue.step([], fetch, Mock(), 10)
        self.assertFalse(queue.pending)
        self.assertEqual(fetch.call_count, before)

    def test_stale_response_not_observed_as_new_result(self):
        queue = AnalysisQueue(); observe = Mock()
        queue.step(['SBER'], Mock(return_value={'status':'ok','refreshing':True}), observe, 0)
        observe.assert_not_called()
        self.assertEqual(list(queue.pending), ['SBER'])


class WideWatchTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'watch'

    def test_wide_archive_records_all_50_legacy_three_rule_unchanged(self):
        symbols = [f'S{i}' for i in range(50)]
        with WideArchive(Path(self.temp.name)/'wide.sqlite3') as archive:
            archive.begin_run(symbols, 5, NOW)
            saved = json.loads(archive.db.execute('SELECT tickers FROM collection_runs').fetchone()[0])
            self.assertEqual(saved, symbols)
            archive.finish_run('deadline', {}, NOW)
            with self.assertRaises(ValueError): archive.begin_run(symbols+['OTHER'], 5, NOW)
        with self.assertRaises(ValueError): validate_options(symbols, 5)

    def test_50_shares_use_200_counted_subscriptions_not_300_limit(self):
        instruments = [{**INSTRUMENT, 'uid': f'uid{i}'} for i in range(50)]
        control = StreamControl(5)
        gen = requests(instruments, control)
        fields = [next(gen) for _ in range(4)]
        count = (len(fields[0].subscribe_order_book_request.instruments)
                 +len(fields[1].subscribe_trades_request.instruments)
                 +len(fields[2].subscribe_candles_request.instruments))
        self.assertEqual(count, 200)
        self.assertEqual(len(fields[3].subscribe_info_request.instruments), 50)
        control.cancel()

    def test_quota_and_invalid_limits_stop_without_broker_or_deletion(self):
        with patch('dashboard.wide_watch.disk_usage', return_value=1_000_000_000), patch('dashboard.wide_watch.bounded_client') as client:
            _, status = supervise(self.root, 'fake', [f'S{i}' for i in range(500)], 5, 5)
        self.assertEqual(status, 'disk_limit'); client.assert_not_called()
        self.assertFalse((self.root/'active.lock').exists())
        for changes in ({'seconds':True}, {'segment_seconds':4}, {'max_bytes':RESERVE_BYTES-1}):
            with self.assertRaises(ValueError): supervise(self.root, 'fake', ['SBER'], **changes)

    def test_reconnects_bounded_and_metadata_lists_active_not_whole_universe(self):
        @contextmanager
        def client(token, control):
            yield NS()
        with patch('dashboard.wide_watch.bounded_client', client), \
                patch('dashboard.wide_watch.resolve', side_effect=lambda client, t: {**INSTRUMENT, 'ticker':t}), \
                patch('dashboard.wide_watch.consume_stream', return_value=('stream_ended', {}, None)), \
                patch('dashboard.wide_watch.time.sleep'):
            session, status = supervise(self.root, 'fake', [f'S{i}' for i in range(121)], 60, 5)
        self.assertEqual(status, 'retries_exhausted')
        self.assertEqual(len(list(session.glob('*-book.sqlite3'))), 8)
        health = load_health(self.root)
        self.assertEqual(health['watchlist_count'], 121)
        self.assertEqual(health['batch_count'], 3)
        self.assertEqual(len(health['tickers']), 50)
        self.assertFalse(health['alive'])

    def test_unknown_symbol_does_not_prevent_valid_instrument_collection(self):
        def resolve(client, t):
            if t == 'BAD': raise MarketUnavailable('unknown')
            return {**INSTRUMENT, 'ticker':t}
        @contextmanager
        def client(token, control): yield NS()
        with patch('dashboard.wide_watch.bounded_client', client), patch('dashboard.wide_watch.resolve', side_effect=resolve), \
                patch('dashboard.wide_watch.consume_stream', return_value=('interrupted', {}, None)) as consume:
            _, status = supervise(self.root, 'fake', ['BAD', 'SBER'], 10, 5)
        self.assertEqual(status, 'interrupted')
        self.assertEqual([i['ticker'] for i in consume.call_args.args[2]], ['SBER'])
        self.assertEqual(load_health(self.root)['unsupported_tickers'], ['BAD'])

    def test_supervisor_really_switches_stream_and_health_to_second_group(self):
        clock = [0.]
        groups = []
        @contextmanager
        def client(token, control): yield NS()
        def consume(adapter, archive, instruments, control, **kwargs):
            groups.append([i['ticker'] for i in instruments])
            if len(groups) == 1:
                clock[0] = ROTATION_SECONDS
                return 'deadline', {}, None
            return 'interrupted', {}, None
        values = [f'S{i}' for i in range(121)]
        with patch('dashboard.wide_watch.time.monotonic', side_effect=lambda: clock[0]), \
                patch('dashboard.wide_watch.bounded_client', client), \
                patch('dashboard.wide_watch.resolve', side_effect=lambda client, t: {**INSTRUMENT,'ticker':t}), \
                patch('dashboard.wide_watch.consume_stream', side_effect=consume):
            _, status = supervise(self.root, 'fake', values, 2000, 300)
        self.assertEqual(status, 'interrupted')
        self.assertEqual(groups, [values[:50], values[50:100]])
        self.assertEqual(load_health(self.root)['tickers'], values[50:100])
        self.assertEqual(load_health(self.root)['batch'], 2)

    def test_all_unknown_symbols_stop_instead_of_retrying_forever(self):
        @contextmanager
        def client(token, control): yield NS()
        with patch('dashboard.wide_watch.bounded_client', client), \
                patch('dashboard.wide_watch.resolve', side_effect=MarketUnavailable('unknown')), \
                patch('dashboard.wide_watch.consume_stream') as consume:
            _, status = supervise(self.root, 'fake', ['BAD'], 5, 5)
        self.assertEqual(status, 'no_supported')
        consume.assert_not_called()
        self.assertFalse(load_health(self.root)['alive'])

    def test_all_invalid_first_group_does_not_starve_following_groups(self):
        def resolve(client, t):
            if t != 'SBER': raise MarketUnavailable('unknown')
            return {**INSTRUMENT, 'ticker':t}
        @contextmanager
        def client(token, control): yield NS()
        with patch('dashboard.wide_watch.bounded_client', client), patch('dashboard.wide_watch.resolve', side_effect=resolve), \
                patch('dashboard.wide_watch.consume_stream', return_value=('interrupted', {}, None)) as consume:
            _, status = supervise(self.root, 'fake', [f'BAD{i}' for i in range(50)]+['SBER'], 10, 5)
        self.assertEqual(status, 'interrupted')
        self.assertEqual([i['ticker'] for i in consume.call_args.args[2]], ['SBER'])

    def test_command_reads_saved_list_not_command_line_truncation(self):
        from .telegram_alerts import Store
        with override_settings(BASE_DIR=Path(self.temp.name)), patch.dict('os.environ', {'INVEST_TOKEN':'fake'}):
            values = [f'S{i}' for i in range(500)]
            Store().save(False, values)
            with patch('dashboard.management.commands.watch_telegram.supervise', return_value=(Path('fixture'), 'deadline')) as run:
                call_command('watch_telegram', seconds=10, verbosity=0)
            self.assertEqual(run.call_args.args[2], values)
