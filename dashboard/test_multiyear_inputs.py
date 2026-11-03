"""Small mocked EXCHANGE downloads; no network, credentials, orders or training."""
import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from . import multiyear_inputs as h
from .management.commands.fetch_multiyear_history import Command, diagnostic


def metadata(ticker='SBER', first='2026-10-21T00:00:00+00:00'):
    day = datetime.fromisoformat(first)
    return h._signed({'version': h.VERSION, 'created_at': h.END.isoformat(),
        'instrument': {'ticker': ticker, 'uid': 'synthetic-uid', 'lot': 10, 'currency': 'rub', 'class_code': 'TQBR'},
        'first_1day_candle_date': first, 'first_1min_candle_date': first,
        'bounds': {'day': h._bounds(day, 'day'), '15m': h._bounds(day, '15m')}, 'entry_allowed': False})


def quote(value):
    return SimpleNamespace(units=int(value), nano=round((value-int(value))*1_000_000_000))


def broker_bar(at, **changes):
    values = {'time': at, 'open': quote(100.), 'high': quote(102.), 'low': quote(99.),
              'close': quote(101.), 'volume': 1000, 'is_complete': True,
              'candle_source': SimpleNamespace(name=h.SOURCE)}
    return SimpleNamespace(**{**values, **changes})


def chunk(meta, interval='15m', *, bars=None):
    first, until = next(h.windows(meta, interval))
    if bars is None:
        bars = [{'time': first.isoformat(), 'open': 100., 'high': 102., 'low': 99., 'close': 101.,
                 'volume': 1000, 'is_complete': True, 'source': h.SOURCE}]
    return h._signed({'version': h.VERSION, 'created_at': h.END.isoformat(),
        'ticker': meta['instrument']['ticker'], 'uid': meta['instrument']['uid'],
        'metadata_sha256': meta['artifact_sha256'], 'entry_allowed': False,
        'request': {'from': first.isoformat(), 'to_exclusive': until.isoformat(),
                    'interval': interval, 'source': h.SOURCE}, 'bars': bars})


class MultiyearInputTests(SimpleTestCase):
    def write_cache(self, root, *, interval='15m', bars=None):
        meta = metadata()
        h._immutable(root/'SBER'/'metadata.json', meta)
        first, until = next(h.windows(meta, interval))
        path = root/'SBER'/h.chunk_name(interval, first, until)
        h._immutable(path, chunk(meta, interval, bars=bars))
        return meta, path

    def test_chunks_respect_1825_day_and21_day_documented_bounds(self):
        meta = metadata(first='2000-01-02T16:15:00+00:00')
        for interval, maximum in (('day', 1825), ('15m', 21)):
            windows = list(h.windows(meta, interval))
            self.assertEqual(windows[0][0], datetime(2000, 1, 2, tzinfo=timezone.utc))
            self.assertEqual(windows[-1][1], h.END)
            self.assertTrue(all(0 < (end-start).days <= maximum for start, end in windows))
            self.assertTrue(all(a[1] == b[0] for a, b in zip(windows, windows[1:])))

    def test_complete_cache_is_read_only_and_never_opens_broker(self):
        with TemporaryDirectory() as directory, patch.object(h, 'open_client') as broker:
            root = Path(directory)
            self.write_cache(root)
            result = h.load_history(root, 'SBER', '15m')
            resumed = h.collect_stock(root, 'SBER', ('15m',), fetch=True)
            self.assertEqual(result['coverage']['bars'], 1)
            self.assertEqual(resumed['intervals']['15m']['bars'], 1)
            self.assertEqual(result['instrument']['uid'], 'synthetic-uid')
            self.assertTrue(result['sources'][0]['sha256'])
            broker.assert_not_called()

    def test_missing_metadata_or_chunks_never_download_without_explicit_fetch(self):
        with TemporaryDirectory() as directory, patch.object(h, 'open_client') as broker:
            root = Path(directory)
            with self.assertRaises(CommandError):
                h.collect_stock(root, 'SBER', fetch=False)
            h._immutable(root/'SBER'/'metadata.json', metadata())
            with self.assertRaises(CommandError):
                h.load_history(root, 'SBER', '15m')
            with self.assertRaises(CommandError):
                h.collect_stock(root, 'SBER', ('15m',), fetch=False)
            broker.assert_not_called()

    def test_empty_chunks_are_legitimate_and_zero_volume_is_retained(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_cache(root, bars=[])
            result = h.load_history(root, 'SBER', '15m')
            self.assertEqual(result['candles'], [])
            self.assertEqual(result['coverage']['empty_chunks'], 1)
            self.assertIsNone(result['coverage']['first_actual'])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bars = chunk(metadata())['bars']
            bars[0]['volume'] = 0
            self.write_cache(root, bars=bars)
            self.assertEqual(h.load_history(root, 'SBER', '15m')['candles'][0].volume, 0)

    def test_weekend_daily_bars_are_never_dropped(self):
        first = '2026-10-17T00:00:00+00:00'  # Saturday
        with TemporaryDirectory() as directory:
            root = Path(directory)
            meta = metadata(first=first)
            h._immutable(root/'SBER'/'metadata.json', meta)
            start, until = next(h.windows(meta, 'day'))
            path = root/'SBER'/h.chunk_name('day', start, until)
            h._immutable(path, chunk(meta, 'day'))
            self.assertEqual(h.load_history(root, 'SBER', 'day')['candles'][0].time.weekday(), 5)

    def test_corrupt_cache_is_not_overwritten_or_refetched(self):
        with TemporaryDirectory() as directory, patch.object(h, 'open_client') as broker:
            root = Path(directory)
            _, path = self.write_cache(root)
            original = path.read_bytes()
            with patch.object(h, '_read', side_effect=ValueError('corrupted')):
                with self.assertRaises(ValueError):
                    h.collect_stock(root, 'SBER', ('15m',), fetch=True)
            self.assertEqual(path.read_bytes(), original)
            broker.assert_not_called()

    def test_resigned_provenance_changes_or_bad_ohlc_are_rejected(self):
        meta = metadata()
        start, until = next(h.windows(meta, '15m'))
        bad_changes = ({'uid': 'different'}, {'metadata_sha256': 'different'},
                       {'entry_allowed': True}, {'ticker': 'GAZP'})
        for changes in bad_changes:
            with TemporaryDirectory() as directory:
                path = Path(directory)/'chunk.json'
                value = {**chunk(meta), **changes}
                value.pop('artifact_sha256')
                h._immutable(path, h._signed(value))
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    h.load_chunk(path, meta, '15m', start, until)
        base = chunk(meta)['bars'][0]
        for changes in ({'volume': -1}, {'volume': True}, {'close': float('nan')},
                        {'low': 105.}, {'source': 'DEALER'}, {'is_complete': False},
                        {'time': start.replace(tzinfo=None).isoformat()},
                        {'time': until.isoformat()}, {'time': (start+timedelta(minutes=1)).isoformat()}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                h._candles([{**base, **changes}], start, until, '15m')

    def test_metadata_requires_exact_current_uid_lot_class_and_fixed_bounds(self):
        meta = metadata()
        self.assertIs(h.validate_metadata(meta, 'SBER'), meta)
        for changed in ({**meta, 'entry_allowed': True}, {**meta, 'bounds': {}},
                        {**meta, 'instrument': {**meta['instrument'], 'lot': True}},
                        {**meta, 'instrument': {**meta['instrument'], 'class_code': 'OTHER'}},
                        {**meta, 'first_1day_candle_date': '1970-01-01T00:00:00+00:00'}):
            with self.subTest(changed=changed), self.assertRaises((ValueError, KeyError)):
                h.validate_metadata(changed, 'SBER')

    def test_fetch_requests_explicit_exchange_without_invalid_limit_combination(self):
        client, limiter = MagicMock(), MagicMock()
        meta = metadata()
        first, until = next(h.windows(meta, '15m'))
        client.market_data.get_candles.return_value = SimpleNamespace(candles=[broker_bar(first), broker_bar(until)])
        value = h._fetch_chunk(client, meta, '15m', first, until, limiter)
        kwargs = client.market_data.get_candles.call_args.kwargs
        self.assertNotIn('limit', kwargs)
        self.assertEqual(kwargs['candle_source_type'].name, h.SOURCE)
        self.assertEqual(kwargs['from_'], first)
        self.assertEqual(kwargs['to'], until)
        self.assertEqual(len(value['bars']), 1)
        limiter.wait.assert_called_once()

    def test_fetch_rejects_source_incomplete_and_conflicting_duplicates(self):
        meta, client = metadata(), MagicMock()
        first, until = next(h.windows(meta, '15m'))
        for bars in ([broker_bar(first, candle_source=SimpleNamespace(name='UNKNOWN'))],
                     [broker_bar(first, is_complete=False)],
                     [broker_bar(first), broker_bar(first, close=quote(100.5))]):
            client.market_data.get_candles.return_value = SimpleNamespace(candles=bars)
            with self.subTest(bars=bars), self.assertRaises(ValueError):
                h._fetch_chunk(client, meta, '15m', first, until, MagicMock())

    def test_metadata_queries_share_by_uid_and_checks_both_first_dates(self):
        client, limiter = MagicMock(), MagicMock()
        first = datetime(2006, 1, 2, tzinfo=timezone.utc)
        share = SimpleNamespace(uid='uid', ticker='SBER', class_code='TQBR', lot=10, currency='rub',
                                figi='figi', isin='isin', first_1day_candle_date=first,
                                first_1min_candle_date=first+timedelta(days=100))
        client.instruments.share_by.return_value = SimpleNamespace(instrument=share)
        with patch.object(h, 'resolve', return_value={'ticker': 'SBER', 'uid': 'uid', 'lot': 10, 'currency': 'rub'}):
            value = h._fetch_metadata(client, 'SBER', limiter)
        self.assertEqual(value['first_1day_candle_date'], first.isoformat())
        self.assertEqual(client.instruments.share_by.call_args.kwargs['id'], 'uid')
        self.assertEqual(limiter.wait.call_count, 2)

    def test_resumption_fetches_only_missing_chunk_and_preserves_previous_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            meta = metadata(first='2026-09-01T00:00:00+00:00')
            h._immutable(root/'SBER'/'metadata.json', meta)
            expected = list(h.windows(meta, '15m'))
            first, until = expected[0]
            saved_path = root/'SBER'/h.chunk_name('15m', first, until)
            initial = chunk(meta)
            h._immutable(saved_path, initial)
            original = saved_path.read_bytes()
            broker = MagicMock()
            broker.__enter__.return_value = MagicMock()

            def missing(client, metadata_value, interval, start, end, limiter):
                return h._signed({**{k: v for k, v in initial.items() if k != 'artifact_sha256'},
                    'request': {'from': start.isoformat(), 'to_exclusive': end.isoformat(),
                                'interval': interval, 'source': h.SOURCE}, 'bars': []})

            with patch.object(h, 'open_client', return_value=broker), patch.object(h, '_fetch_chunk', side_effect=missing) as fetch:
                result = h.collect_stock(root, 'SBER', ('15m',), fetch=True)
            self.assertEqual(fetch.call_count, len(expected)-1)
            self.assertEqual(saved_path.read_bytes(), original)
            self.assertEqual(result['intervals']['15m']['chunks'], len(expected))

    def test_transient_rpc_errors_retry_at_most_three_times_and_rate_every_attempt(self):
        class TransientError(Exception):
            def code(self):
                return SimpleNamespace(name='UNAVAILABLE')
        limiter = MagicMock()
        call = MagicMock(side_effect=[TransientError(), TransientError(), 'ready'])
        with patch.object(h.time, 'sleep') as sleep:
            self.assertEqual(h._rpc(call, limiter), 'ready')
        self.assertEqual(limiter.wait.call_count, 3)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [2, 4])
        with patch.object(h.time, 'sleep'), self.assertRaises(TransientError):
            h._rpc(MagicMock(side_effect=TransientError()), limiter)
        self.assertEqual(limiter.wait.call_count, 6)

    def test_auth_source_and_cache_validation_errors_are_never_retried(self):
        class AuthError(Exception):
            def code(self):
                return SimpleNamespace(name='UNAUTHENTICATED')
        for error in (AuthError(), ValueError('invalid EXCHANGE source')):
            call, limiter = MagicMock(side_effect=error), MagicMock()
            with patch.object(h.time, 'sleep') as sleep, self.assertRaises(type(error)):
                h._rpc(call, limiter)
            limiter.wait.assert_called_once()
            call.assert_called_once()
            sleep.assert_not_called()

    def test_real_sdk_requesterror_status_property_matches_retryable_codes(self):
        import grpc
        from t_tech.invest.exceptions import RequestError
        for code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED,
                     grpc.StatusCode.RESOURCE_EXHAUSTED):
            call = MagicMock(side_effect=[RequestError(code, 'DO_NOT_PRINT', object()), 'ready'])
            limiter = MagicMock()
            with patch.object(h.time, 'sleep') as sleep:
                self.assertEqual(h._rpc(call, limiter), 'ready')
            self.assertEqual(limiter.wait.call_count, 2)
            sleep.assert_called_once_with(2)


class MultiyearCommandTests(SimpleTestCase):
    def test_command_prioritises_metadata_then_day_then_intraday_and_restores_logging(self):
        import logging
        calls = []
        logger = logging.getLogger('t_tech.invest')
        previous = logger.level
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch.object(h, 'collect_stock', side_effect=lambda root, ticker, intervals, **kw: calls.append((ticker, intervals, kw['fetch']))):
            Command(stdout=StringIO()).handle(fetch=True, daily=False, intraday=False, workers=1, ticker=['SBER'])
        self.assertEqual(calls, [('SBER', (), True), ('SBER', ('day',), True), ('SBER', ('15m',), True)])
        self.assertEqual(logger.level, previous)

    def test_no_fetch_flag_propagates_and_bad_workers_are_rejected(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch.object(h, 'collect_stock') as collect:
            Command(stdout=StringIO()).handle(fetch=False, daily=True, intraday=False, workers=1, ticker=['SBER'])
            self.assertTrue(all(call.kwargs['fetch'] is False for call in collect.call_args_list))
        for workers in (0, 4):
            with self.assertRaises(CommandError):
                Command(stdout=StringIO()).handle(fetch=False, daily=True, intraday=False, workers=workers, ticker=['SBER'])

    def test_raw_broker_exception_text_is_never_logged(self):
        out = StringIO()
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch.object(h, 'collect_stock', side_effect=RuntimeError('DO_NOT_PRINT_PRIVATE_VALUE')):
            with self.assertRaises(CommandError):
                Command(stdout=out).handle(fetch=True, daily=False, intraday=True, workers=1, ticker=['SBER'])
        self.assertNotIn('DO_NOT_PRINT_PRIVATE_VALUE', out.getvalue())

    def test_safe_diagnostic_supports_sdk_property_and_grpc_callable_code_without_details(self):
        import grpc
        from t_tech.invest.exceptions import RequestError
        error = RequestError(grpc.StatusCode.INVALID_ARGUMENT, 'PRIVATE_DETAILS', 'PRIVATE_METADATA')
        self.assertEqual(diagnostic(error), 'RequestError/INVALID_ARGUMENT')

        class CallableError(Exception):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

        self.assertEqual(diagnostic(CallableError('PRIVATE')), 'CallableError/UNAVAILABLE')
        self.assertEqual(diagnostic(RuntimeError('PRIVATE')), 'RuntimeError/NO_RPC_CODE')
